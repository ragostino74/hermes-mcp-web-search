#!/usr/bin/env python3
"""
Hermes MCP Web Search Server -- Doppio trasporto (stdio + HTTP/StreamableHTTP)

Uso stdio:
  python hermes_mcp_server.py

Uso HTTP (per llama.cpp WebUI e altri client browser):
  HERMES_MCP_TRANSPORT=http HERMES_MCP_PORT=18760 \
    python hermes_mcp_server.py
"""
import json, sys, os, re, hashlib, asyncio, signal as sig_mod
from datetime import datetime, timezone
from urllib.parse import urlparse

try:
    from mcp.server import Server
    from mcp.server.stdio import stdio_server
    from mcp.types import Tool, TextContent, InitializeRequest
    try:
        from mcp.types import MethodTypes
    except ImportError:
        MethodTypes = None
except ImportError as e:
    print(f"ERROR: Cannot import MCP packages: {e}", file=sys.stderr)
    sys.exit(1)

try:
    from mcp.server.fastmcp import FastMCP
    FASTMCP_AVAILABLE = True
except ImportError:
    FASTMCP_AVAILABLE = False

try:
    import httpx
    HTTPX_AVAILABLE = True
except ImportError:
    HTTPX_AVAILABLE = False

try:
    from duckduckgo_search import DDGS
    DDG_AVAILABLE = True
except ImportError:
    DDG_AVAILABLE = False

TRANSPORT = os.environ.get("HERMES_MCP_TRANSPORT", "stdio")
LLM_ENDPOINT = os.environ.get("LLM_ENDPOINT", "http://localhost:10000/v1")
LLM_MODEL = os.environ.get("LLM_MODEL", "Qwen3.6-35B-A3B-Q8_0.gguf")
HERMES_BRIDGE_URL = os.environ.get("HERMES_BRIDGE_URL", "")
def _is_safe_url(url: str) -> bool:
    """Block access to localhost, private IPs, link-local, metadata endpoints."""
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()

    # Block by hostname
    blocked_hosts = {"localhost", "127.0.0.1", "::1", "0.0.0.0"}
    if host in blocked_hosts:
        return False

    # Resolve IP to catch localhost aliases
    import socket
    try:
        addrinfo = socket.getaddrinfo(host, None)
        for family, socktype, proto, canonname, sockaddr in addrinfo:
            ip = sockaddr[0]

            # ── IPv4 private / loopback ──────────────────────────────
            if ip == "127.0.0.1":
                return False
            if ip.startswith("127."):
                return False
            if ip.startswith("10.") or ip.startswith("192.168."):
                return False
            if ip.startswith("172."):
                parts = ip.split(".")
                if len(parts) == 4 and 16 <= int(parts[1]) <= 31:
                    return False
            if ip.startswith("169.254."):
                return False

            # ── IPv6 private ranges ─────────────────────────────────
            lower = ip.lower()
            # Unique Local Addresses (ULA) fc00::/7  (includes fd00::/8)
            if lower.startswith("fc") or lower.startswith("fd"):
                return False
            # Loopback ::1
            if ip == "::1":
                return False
            # Link-local fe80::/10
            if lower.startswith("fe8") or lower.startswith("fe9") or \
               lower.startswith("fea") or lower.startswith("feb"):
                return False
            # Site-local (deprecated) fec0::/10 — still block
            if lower.startswith("fec"):
                return False
            # IPv4-mapped IPv6 ::ffff:x.x.x.x  → unmask and check private
            if ip.startswith("::ffff:"):
                mapped = ip.split(":")[-1]  # e.g. "127.0.0.1"
                if mapped == "127.0.0.1":
                    return False
                if mapped.startswith("127.") or mapped.startswith("10.") \
                   or mapped.startswith("192.168."):
                    return False
                if mapped.startswith("172."):
                    parts = mapped.split(".")
                    if len(parts) == 4 and 16 <= int(parts[1]) <= 31:
                        return False
                if mapped.startswith("169.254."):
                    return False

            # Block metadata endpoints (same as before)
            if ip == "0.0.0.0":
                return False
    except (socket.gaierror, OSError):
        # If DNS resolution fails, block to be safe
        return False

    return True

_cache: dict[str, dict] = {}  # Simple dict with LRU eviction
_CACHE_MAX_SIZE = 100
_CACHE_TTL = 1800


def _cache_key(text):
    return hashlib.md5(text.encode()).hexdigest()


def _evict_lru():
    """Remove oldest entry when cache is full (FIFO eviction for simplicity)."""
    if len(_cache) >= _CACHE_MAX_SIZE:
        oldest_key = next(iter(_cache))
        del _cache[oldest_key]


def _get_cached(key):
    entry = _cache.get(key)
    if entry and (datetime.now(timezone.utc) - entry["time"]).seconds < _CACHE_TTL:
        return entry["data"]
    return None


def _set_cache(key, data):
    """Cache with TTL and LRU eviction (max 100 entries)."""
    # Evict oldest if at capacity
    _evict_lru()
    _cache[key] = {"data": data, "time": datetime.now(timezone.utc)}


# ── Rate Limiter / External Call Guard ───────────────────────────────────
import asyncio
from contextlib import asynccontextmanager

_RATE_LIMIT_MAX = int(os.environ.get("HERMES_MCP_RATE_LIMIT", "5"))       # calls/minute
_RATE_LIMIT_WINDOW = 60                                                     # seconds
_SEMAPHORE_MAX  = int(os.environ.get("HERMES_MCP_CONCURRENCY", "3"))         # max parallel ext calls

# Token bucket: _RATE_LIMIT_MAX tokens per _RATE_LIMIT_WINDOW seconds
try:
    from aiolimiter import AsyncLimiter as _AsyncLimiter
    _rate_limiter = _AsyncLimiter(_RATE_LIMIT_MAX, _RATE_LIMIT_WINDOW)
except ImportError:
    _rate_limiter = None

# Semaphore: hard cap on concurrent external calls
_external_sem = asyncio.Semaphore(_SEMAPHORE_MAX)


async def _external_call(fn, *args, **kwargs):
    """Run a sync callable inside semaphore + token-bucket guard.

    Meant to be awaited by async callers (MCP tools, FastAPI routes).
    If ``aiolimiter`` is unavailable only the semaphore applies.
    """
    async with _external_sem:
        if _rate_limiter is not None:
            async with _rate_limiter:
                return await asyncio.get_running_loop().run_in_executor(
                    None, lambda: fn(*args, **kwargs))
        else:
            return await asyncio.get_running_loop().run_in_executor(
                None, lambda: fn(*args, **kwargs))


def rate_limited(fn):
    """Decorator: wraps any async function under semaphore + token bucket."""
    import functools

    @functools.wraps(fn)
    async def wrapper(*a, **kw):
        async with _external_sem:
            if _rate_limiter is not None:
                async with _rate_limiter:
                    return await fn(*a, **kw)
            else:
                return await fn(*a, **kw)

    return wrapper


def _sanitize_for_llm(text: str, max_len: int = 8000) -> str:
    """Escape / limit user-supplied text before injecting it into an LLM prompt.

    Prevents prompt injection by:
    - Stripping or neutralising markdown/code-block syntax that could confuse the model
    - Truncating to a safe length so very long injected payloads can't overflow
      the context window or trigger unintended behaviour
    """
    if not isinstance(text, str):
        return ""
    text = text.strip()
    # Trim extremely long inputs (injected data can be arbitrarily large)
    if len(text) > max_len:
        text = text[:max_len] + "\n\n[... truncated for safety ...]"
    # Neutralise common prompt-injection markers that an attacker might use to
    # "escape" the injected content and control subsequent LLM behaviour.
    replacements = [
        ("```", "[CODE_BLOCK]"),          # code fences
        ("<!--", "[HTML_COMMENT]"),       # HTML comments
        (">>>",  "[PYTHON_PROMPT]"),      # Python REPL prompt
        ("SYSTEM:", "USER_NOTE: "),       # force-role tokens
        ("ASSISTANT:", "ASSISTANT_NOTE: "),
        ("USER:", "QUERY: "),
    ]
    for bad, good in replacements:
        text = text.replace(bad, good)
    return text


def _summarize_with_llm(prompt_text: str, max_tokens: int = 1500, temperature: float = 0.3) -> str:
    """Use local llama.cpp to summarize content."""
    try:
        import http.client as hc
        p = urlparse(LLM_ENDPOINT)
        host = p.hostname or "localhost"
        port = p.port or 80

        # Pre-sanitise the full prompt (which may contain injected user data)
        safe_prompt = _sanitize_for_llm(prompt_text, max_len=6000)

        body = json.dumps({
            "model": LLM_MODEL,
            "messages": [{"role": "user", "content": safe_prompt}],
            "max_tokens": max_tokens,
            "temperature": temperature,
            "stream": False,
        })
        c = hc.HTTPConnection(host, port, timeout=45)
        c.request("POST", "/chat/completions", body=body, headers={"Content-Type": "application/json"})
        r = c.getresponse()
        data = json.loads(r.read().decode())
        c.close()
        if data.get("choices"):
            return data["choices"][0]["message"]["content"].strip()
    except Exception as e:
        sys.stderr.write(f"LLM summarize error: {e}\n")
    return ""


def _search_ddg(query, max_results=5):
    """Search via DuckDuckGo."""
    if not DDG_AVAILABLE:
        return {"error": "duckduckgo-search non installato", "results": []}
    ck = _cache_key(f"ddg:{query}:{max_results}")
    cached = _get_cached(ck)
    if cached:
        return cached
    try:
        with DDGS() as ddgs:
            results = list(ddgs.text(query, max_results=max_results))
        result = {
            "query": query,
            "results": [
                {"title": r.get("title", ""), "url": r.get("href", ""), "snippet": r.get("body", "")}
                for r in results
            ],
            "total": len(results),
            "source": "duckduckgo",
        }
        _set_cache(ck, result)
        return result
    except Exception as e:
        return {"error": str(e), "results": []}


def _deep_search_via_hermes(query):
    """Send to Hermes bridge for deep search with reasoning."""
    if not HTTPX_AVAILABLE or not HERMES_BRIDGE_URL:
        return None
    try:
        with httpx.Client(timeout=180) as client:
            resp = client.post(
                f"{HERMES_BRIDGE_URL}/api/search",
                json={"query": query},
                headers={"Content-Type": "application/json"},
            )
            return resp.json() if resp.status_code == 200 else None
    except Exception:
        return None


# -- FastMCP server instance (con CORS disabled per consentire accessi dalla WebUI) --
try:
    from mcp.server.transport_security import TransportSecuritySettings
except ImportError:
    TransportSecuritySettings = None

if FASTMCP_AVAILABLE and TransportSecuritySettings is not None:
    mcp_server = FastMCP(
        name="hermes-web-mcp",
        host="0.0.0.0",
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=False,
        ),
    )
else:
    mcp_server = FastMCP(name="hermes-web-mcp")


@mcp_server.tool()
@rate_limited
async def web_search(query: str, max_results: int = 5) -> str:
    """Ricerca informazioni su internet con DuckDuckGo + sintesi LLM."""
    query = query.strip()
    max_r = min(max(1, int(max_results)), 10)
    result = await _external_call(_search_ddg, query, max_r)
    if "results" in result and result.get("results") and len(result["results"]) > 0:
        raw = "\n---\n".join([f"{r['title']}: {r['snippet'][:200]}" for r in result["results"]])
        summary_prompt = (
            f"Sintetizza in italiano questi risultati di ricerca per: '{query}'\n\n"
            f"{raw}\n\nRispondi con 3-5 punti chiave."
        )
        llm_result = await _external_call(_summarize_with_llm, summary_prompt)
        if llm_result:
            result["llm_summary"] = llm_result
    return json.dumps(result, ensure_ascii=False, indent=2)


@mcp_server.tool()
@rate_limited
async def deep_search(query: str) -> str:
    """Ricerca profonda con analisi del tuo LLM locale."""
    query = query.strip()
    search_result = await _external_call(_search_ddg, query)
    if search_result.get("error"):
        return json.dumps(search_result, indent=2)
    raw_content = "\n---\n".join([f"# {r['title']}\n{r['snippet']}" for r in search_result.get("results", [])])
    llm_prompt = (
        f'Sei un assistente AI. Analizza questi risultati per: "{query}"\n\n'
        f"Risultati:\n{raw_content[:10000]}\n\n"
        "Fornisci una risposta completa in italiano con punti chiave, fonti e incertezze."
    )
    llm_answer = await _external_call(_summarize_with_llm, llm_prompt)
    output = {
        "status": "success",
        "query": query,
        "llm_analysis": llm_answer or "LLM summarization not available",
        "source_results": search_result.get("results", []),
    }
    return json.dumps(output, ensure_ascii=False, indent=2)


@mcp_server.tool()
@rate_limited
async def read_webpage(url: str) -> str:
    """Leggi il contenuto di una pagina web con riassunto LLM."""
    if not url.startswith(("http://", "https://")):
        return json.dumps({"error": "URL invalido"}, indent=2)
    if not _is_safe_url(url):
        return json.dumps({"error": "Accesso bloccato: localhost, IP privati e link-local non sono permessi"}, indent=2)
    try:
        with httpx.Client(timeout=30) as client:
            resp = client.get(
                url,
                headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64)"},
                follow_redirects=True,
            )
            text = re.sub(r"<[^>]+>", " ", resp.text)
            text = re.sub(r"\s+", " ", text).strip()[:15000]
        title_match = re.search(r'<title[^>]*>([^<]+)</title>', resp.text, re.I)
        title = title_match.group(1) if title_match else "N/A"
        summary = None
        if len(text) > 200:
            prompt = f"Sintetizza in italiano:\n\n{text[:8000]}\n\nFatti principali in max 5 punti."
            summary = await _external_call(_summarize_with_llm, prompt)
        return json.dumps(
            {
                "status": "success",
                "url": url,
                "title": title,
                "summary": summary,
                "content_preview": text[:2000],
                "total_chars": len(text),
            },
            ensure_ascii=False,
            indent=2,
        )
    except Exception as e:
        return json.dumps({"error": str(e), "url": url}, indent=2)


@mcp_server.tool()
@rate_limited
async def hermes_search(query: str) -> str:
    """Ricerca completa tramite Hermes Agent (browser + web)."""
    query = query.strip()
    result = await _external_call(_deep_search_via_hermes, query)
    if result and result.get("status") == "ok" and result.get("response"):
        return json.dumps(
            {
                "status": "success",
                "method": "hermes_agent",
                "query": query,
                "answer": result["response"],
            },
            ensure_ascii=False,
            indent=2,
        )
    return json.dumps(
        {
            "status": "partial",
            "method": "local_fallback",
            "message": "Hermes bridge unavailable, using DuckDuckGo + LLM fallback",
            "query": query,
        },
        indent=2,
    )


# ── HTTP Bridge Server (standalone, per Hermes Agent integration) ────────
_BRIDGE_PORT = int(os.environ.get("HERMES_MCP_BRIDGE_PORT", "18761"))

try:
    from fastapi import FastAPI, Query
    from fastapi.middleware.cors import CORSMiddleware
    from contextlib import asynccontextmanager
    HAS_FASTAPI = True
except ImportError:
    HAS_FASTAPI = False


@asynccontextmanager
async def _bridge_lifespan(_app):
    yield  # startup / shutdown hooks here if needed


if HAS_FASTAPI:
    bridge_app = FastAPI(
        title="Hermes Web Search Bridge",
        version="4.0.0",
        lifespan=_bridge_lifespan,
    )

    bridge_app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["*"],
    )

    @bridge_app.get("/health")
    async def health():
        """Health check endpoint."""
        return {
            "status": "ok",
            "version": "4.0.0",
            "rate_limit_max": _RATE_LIMIT_MAX,
            "concurrency_max": _SEMAPHORE_MAX,
            "token_bucket_available": _rate_limiter is not None,
        }

    @bridge_app.api_route("/api/search", methods=["GET", "POST"])
    async def api_search(query: str = Query(..., description="Search query"), max_results: int = 5):
        """Web search API endpoint (rate-limited)."""
        result = await _external_call(_search_ddg, query, min(max(1, max_results), 10))
        return result

    @bridge_app.api_route("/api/deep-search", methods=["GET", "POST"])
    async def api_deep_search(query: str = Query(..., description="Search query")):
        """Deep search API endpoint (rate-limited, uses LLM summarization)."""
        return await _external_call(_summarize_with_llm, query)


# ── Startup helpers ──────────────────────────────────────────────────────

async def _start_http_bridge():
    """Launch the FastAPI bridge server on HERMES_MCP_BRIDGE_PORT."""
    if not HAS_FASTAPI:
        sys.stderr.write("Bridge: skipping (fastapi not installed)\n")
        return
    try:
        import uvicorn
        config = uvicorn.Config(bridge_app, host="0.0.0.0", port=_BRIDGE_PORT, log_level="warning")
        server = uvicorn.Server(config)
        t = asyncio.create_task(server.serve())
        sys.stderr.write(f"Bridge: listening on :{_BRIDGE_PORT}\n")
        return t
    except Exception as e:
        sys.stderr.write(f"Bridge: failed to start ({e})\n")
        return None


async def main():
    # ── Start bridge server (if FastAPI available) ────────────────────────
    _bridge_task = await _start_http_bridge()

    print(f"\U0001f52e Hermes MCP Server v3.0", file=sys.stderr)
    print(f"   Transport: {TRANSPORT}", file=sys.stderr)
    print(f"   LLM: {LLM_ENDPOINT}", file=sys.stderr)
    print(f"   Bridge: {HERMES_BRIDGE_URL}", file=sys.stderr)

    if HERMES_BRIDGE_URL and HTTPX_AVAILABLE:
        try:
            with httpx.Client(timeout=3) as c:
                r = c.get(f"{HERMES_BRIDGE_URL}/health")
                print(
                    f"   Bridge status: {r.json().get('status', 'unknown')}",
                    file=sys.stderr,
                )
        except Exception as e:
            print(f"   Bridge: unavailable ({e})", file=sys.stderr)

    try:
        llm_summary = _summarize_with_llm("Rispondi solo 'OK'", max_tokens=5)
        if llm_summary == "OK":
            print(f"   Local LLM: connected ({LLM_MODEL})", file=sys.stderr)
        else:
            print(
                f"   Local LLM: responding (got '{llm_summary[:20]}')",
                file=sys.stderr,
            )
    except Exception as e:
        print(f"   Local LLM: not available ({e})", file=sys.stderr)

    if TRANSPORT == "stdio":
        print("\nRunning in STDIO mode...", file=sys.stderr)
        await mcp_server.run_stdio_async()

    elif TRANSPORT in ("http", "dual"):
        port = int(os.environ.get("HERMES_MCP_PORT", "18760"))

        if FASTMCP_AVAILABLE:
            print(f"\nRunning in HTTP (StreamableHTTP) mode on :{port}...", file=sys.stderr)

            # Build app with CORS support (WebUI browser needs cross-origin headers)
            from starlette.applications import Starlette
            from starlette.routing import Mount
            from starlette.middleware.cors import CORSMiddleware

            # Get MCP's internal ASGI app
            mcp_app = mcp_server.streamable_http_app()

            # Wrap in CORSMiddleware so browser requests work
            cors_app = CORSMiddleware(
                app=mcp_app,
                allow_origins=["*"],
                allow_methods=["POST", "OPTIONS"],
                allow_headers=["*"],
                expose_headers=["Mcp-Session-Id", "Cache-Control", "Content-Disposition"],
            )

            import uvicorn
            config = uvicorn.Config(cors_app, host="0.0.0.0", port=port, log_level="info")
            server = uvicorn.Server(config)

            # Shared shutdown signal — replaces sys.exit(0) in signal handlers
            _shutdown_event = asyncio.Event()
            _mcpx_flag = False  # True if MCP server crashed (not graceful shutdown)

            def _on_signal(_sig, _frame):
                print("\nShutting down...", file=sys.stderr)
                _shutdown_event.set()

            sig_mod.signal(sig_mod.SIGINT, _on_signal)
            sig_mod.signal(sig_mod.SIGTERM, _on_signal)

            try:
                await server.serve()
            except SystemExit as e:
                print(f"\nMCP HTTP server exited (code {e.code})", file=sys.stderr)
                if e.code != 0:
                    _mcpx_flag = True
                    if _bridge_task is not None:
                        print("   Bridge keeps running on port " + str(_BRIDGE_PORT), file=sys.stderr)

            # If MCP crashed, keep event loop alive so the bridge stays up
            # until SIGINT/SIGTERM arrives
            if _mcpx_flag and _bridge_task is not None:
                await _shutdown_event.wait()  # blocks until signal handler fires

            # Graceful shutdown (signal or MCP exited normally)
            if _shutdown_event.is_set():
                print("Shutting down...", file=sys.stderr)
                if _bridge_task is not None:
                    _bridge_task.cancel()
                    try:
                        await _bridge_task
                    except asyncio.CancelledError:
                        pass

            # If MCP crashed but no signal received, stay alive for bridge
            elif _mcpx_flag and _bridge_task is not None:
                print("   Keeping event loop alive for bridge...", file=sys.stderr)
                try:
                    while True:
                        await asyncio.sleep(1)
                except (asyncio.CancelledError, KeyboardInterrupt):
                    pass  # killed by external force — bridge was already cancelled above? No.

        else:
            print(
                "\nERROR: FastMCP with HTTP requires 'mcp[serve]' package.",
                file=sys.stderr,
            )
            print("Install with: pip install 'mcp[serve]'", file=sys.stderr)

    elif TRANSPORT == "dual" and not FASTMCP_AVAILABLE:
        print(
            "\nDual mode requires mcp[serve]. Falling back to stdio.",
            file=sys.stderr,
        )
        await mcp_server.run_stdio_async()


if __name__ == "__main__":
    asyncio.run(main())
