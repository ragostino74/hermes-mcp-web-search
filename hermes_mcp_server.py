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
HERMES_BRIDGE_URL = os.environ.get("HERMES_BRIDGE_URL", "http://localhost:18760")

_cache = {}
_CACHE_TTL = 1800


def _cache_key(text):
    return hashlib.md5(text.encode()).hexdigest()


def _get_cached(key):
    entry = _cache.get(key)
    if entry and (datetime.now(timezone.utc) - entry["time"]).seconds < _CACHE_TTL:
        return entry["data"]
    return None


def _set_cache(key, data):
    _cache[key] = {"data": data, "time": datetime.now(timezone.utc)}


def _summarize_with_llm(prompt_text, max_tokens=1500, temperature=0.3):
    """Use local llama.cpp to summarize content."""
    try:
        import http.client as hc
        p = urlparse(LLM_ENDPOINT)
        host = p.hostname or "localhost"
        port = p.port or 80
        body = json.dumps({
            "model": LLM_MODEL,
            "messages": [{"role": "user", "content": prompt_text}],
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
    return None


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
async def web_search(query: str, max_results: int = 5) -> str:
    """Ricerca informazioni su internet con DuckDuckGo + sintesi LLM."""
    query = query.strip()
    max_r = min(max(1, int(max_results)), 10)
    result = _search_ddg(query, max_r)
    if "results" in result and result.get("results") and len(result["results"]) > 0:
        raw = "\n---\n".join([f"{r['title']}: {r['snippet'][:200]}" for r in result["results"]])
        summary_prompt = (
            f"Sintetizza in italiano questi risultati di ricerca per: '{query}'\n\n"
            f"{raw}\n\nRispondi con 3-5 punti chiave."
        )
        llm_result = _summarize_with_llm(summary_prompt)
        if llm_result:
            result["llm_summary"] = llm_result
    return json.dumps(result, ensure_ascii=False, indent=2)


@mcp_server.tool()
async def deep_search(query: str) -> str:
    """Ricerca profonda con analisi del tuo LLM locale."""
    query = query.strip()
    search_result = _search_ddg(query)
    if search_result.get("error"):
        return json.dumps(search_result, indent=2)
    raw_content = "\n---\n".join([f"# {r['title']}\n{r['snippet']}" for r in search_result.get("results", [])])
    llm_prompt = (
        f'Sei un assistente AI. Analizza questi risultati per: "{query}"\n\n'
        f"Risultati:\n{raw_content[:10000]}\n\n"
        "Fornisci una risposta completa in italiano con punti chiave, fonti e incertezze."
    )
    llm_answer = _summarize_with_llm(llm_prompt)
    output = {
        "status": "success",
        "query": query,
        "llm_analysis": llm_answer or "LLM summarization not available",
        "source_results": search_result.get("results", []),
    }
    return json.dumps(output, ensure_ascii=False, indent=2)


@mcp_server.tool()
async def read_webpage(url: str) -> str:
    """Leggi il contenuto di una pagina web con riassunto LLM."""
    if not url.startswith(("http://", "https://")):
        return json.dumps({"error": "URL invalido"}, indent=2)
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
            summary = _summarize_with_llm(prompt)
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
async def hermes_search(query: str) -> str:
    """Ricerca completa tramite Hermes Agent (browser + web)."""
    query = query.strip()
    result = _deep_search_via_hermes(query)
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


async def main():
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

            async def shutdown_handler():
                print("\nShutting down...", file=sys.stderr)
                sys.exit(0)

            sig_mod.signal(sig_mod.SIGINT, lambda s, f: asyncio.create_task(shutdown_handler()))
            sig_mod.signal(sig_mod.SIGTERM, lambda s, f: asyncio.create_task(shutdown_handler()))

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
            await server.serve()

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
