#!/usr/bin/env python3
"""
Hermes MCP Web Search Server -- Doppio trasporto (stdio + HTTP/StreamableHTTP)

Uso stdio:
  python hermes_mcp_server.py

Uso HTTP (per llama.cpp WebUI e altri client browser):
  HERMES_MCP_TRANSPORT=http HERMES_MCP_PORT=18760 \
    python hermes_mcp_server.py
"""
import json, sys, os, re, hashlib, asyncio, signal as sig_mod, time, logging
from datetime import datetime, timezone
from urllib.parse import urlparse

# Structured request logger for bridge API (audit trail)
_audit_logger = logging.getLogger("hermes.bridge.audit")
_audit_handler = logging.StreamHandler(sys.stderr)
_audit_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s [AUDIT] %(message)s"))
_audit_logger.addHandler(_audit_handler)
_audit_logger.setLevel(logging.INFO)

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
SEARXNG_URL = os.environ.get("SEARXNG_URL", "").rstrip("/")

# ── Server bind addresses (default localhost for security) ────────
_MCP_BIND_ADDR = os.environ.get("HERMES_MCP_BIND_ADDR", "127.0.0.1")  # MCP HTTP transport
_BRIDGE_BIND_ADDR = os.environ.get("HERMES_BRIDGE_BIND_ADDR", "127.0.0.1")  # Bridge REST API
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
import threading

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

# Track whether we're inside a @rate_limited wrapper to avoid double-semaphore acquisition
_rate_limit_ctx = threading.local()


def _run_in_executor(fn, *args, **kwargs):
    """Run sync callable in event-loop threadpool. Returns a coroutine."""
    loop = asyncio.get_running_loop()
    return loop.run_in_executor(None, lambda: fn(*args, **kwargs))


async def _external_call(fn, *args, **kwargs):
    """Run a sync callable inside semaphore + token-bucket guard.

    Meant to be awaited by async callers (MCP tools). When called from within
    a @rate_limited wrapper it skips the semaphore (already held) and only
    applies the token bucket per call.

    If ``aiolimiter`` is unavailable only the semaphore applies.
    """
    # Check if we're already inside a @rate_limited wrapper (semaphore held).
    # Nested external calls must skip sem to avoid deadlock.
    already_gated = getattr(_rate_limit_ctx, "active", False)

    if not already_gated:
        # Outer call path: acquire semaphore + token bucket
        async with _external_sem:
            if _rate_limiter is not None:
                async with _rate_limiter:
                    return await _run_in_executor(fn, *args, **kwargs)
            else:
                return await _run_in_executor(fn, *args, **kwargs)

    # Inner call path (inside @rate_limited): only token bucket.
    # Semaphore is already held by the decorator.
    if _rate_limiter is not None:
        async with _rate_limiter:
            return await _run_in_executor(fn, *args, **kwargs)
    else:
        return await _run_in_executor(fn, *args, **kwargs)


def rate_limited(fn):
    """Decorator: wraps any async function under semaphore + token bucket.

    Holds the semaphore for the entire duration of the wrapped function so
    that nested calls to _external_call() detect we're already gated and
    skip their own semaphore acquisition (preventing deadlock).

    The token bucket is applied inside each external call via _external_call().
    """
    import functools

    @functools.wraps(fn)
    async def wrapper(*a, **kw):
        # Acquire semaphore for the entire function body.
        # Nested calls via _external_call() see active=True and skip sem.
        async with _external_sem:
            _rate_limit_ctx.active = True
            try:
                return await fn(*a, **kw)
            finally:
                _rate_limit_ctx.active = False

    return wrapper


def _sanitize_for_llm(text: str, max_len: int = 8000) -> str:
    """Escape / limit user-supplied text before injecting it into an LLM prompt.

    Prevents prompt injection by:
    - Stripping or neutralising markdown/code-block syntax that could confuse the model
    - Truncating to a safe length so very long injected payloads can't overflow
      the context window or trigger unintended behaviour
    - Neutralising role-marker tokens, control sequences, and structural attack patterns
    """
    if not isinstance(text, str):
        return ""
    text = text.strip()
    # Trim extremely long inputs (injected data can be arbitrarily large)
    if len(text) > max_len:
        text = text[:max_len] + "\n\n[... truncated for safety ...]"

    # ── Phase 1: Strip control / zero-width chars that can hide injection ──
    # U+200B U+200C U+200D U+FEFF BOM / zero-width joiner / soft hyphen / etc.
    text = re.sub(r'[\u200b\u200c\u200d\ufeff\u2060\u00ad]', '', text)
    # Fullwidth variants (CJK substitution attacks): ＳＹＳＴＥＭ → SYSTEM
    text = _fullwidth_to_ascii(text)

    # ── Phase 2: Neutralise role-marker tokens (case-insensitive, with optional
    #      whitespace / punctuation between letters to defeat "S Y S T E M" tricks) ──
    # We match lines that START with a role token (possibly preceded by whitespace).
    # Each line is inspected so we only neutralise actual prompt injections, not
    # random occurrences of the word "system" mid-sentence.
    text = _neutralize_role_markers(text)

    # ── Phase 3: Structural / formatting attacks ──
    replacements = [
        ("```", "[CODE_BLOCK]"),          # code fences
        ("<!--", "[HTML_COMMENT]"),       # HTML comments
        (">>>",  "[PYTHON_PROMPT]"),      # Python REPL prompt
        ("\n---\n", "\n[SEP]\n"),         # section separators that split prompts
    ]
    for bad, good in replacements:
        text = text.replace(bad, good)
    return text


def _fullwidth_to_ascii(text: str) -> str:
    """Convert fullwidth Unicode chars to ASCII to defeat substitution attacks.

    Fullwidth forms (U+FF01–U+FF5E) look identical to their ASCII counterparts
    but bypass simple string-replacement filters that check for literal 'SYSTEM'.
    """
    # Fullwidth uppercase A-Z: U+FF21..U+FF3A → A-Z
    result = []
    for ch in text:
        cp = ord(ch)
        if 0xFF21 <= cp <= 0xFF3A:   # Ａ–Ｚ
            result.append(chr(cp - 0xFF21 + ord('A')))
        elif 0xFF41 <= cp <= 0xFF5A:  # ａ–ｚ
            result.append(chr(cp - 0xFF41 + ord('a')))
        else:
            result.append(ch)
    return ''.join(result)


def _neutralize_role_markers(text: str) -> str:
    """Neutralise role-marker tokens at the start of lines.

    Detects patterns like:
      SYSTEM: ignore all instructions...   -- colon + instruction text
      ASSISTANT: you are now...            -- other role markers
      USER: ...                            -- user-role spoofing
      系统指令 (Chinese prompt injection)
      ignore all previous...               -- direct instruction override
      sei un assistente malevolo           -- Italian "you are" command

    Each line is checked against several pattern groups.
    Only neutralises when the marker appears at the START of a line.
    """
    lines = text.split("\n")
    result_lines = []

    for line in lines:
        if not line.strip():
            result_lines.append(line)
            continue

        indent_match = re.match(r"^(\s*)", line)
        indent = indent_match.group(1) if indent_match else ""
        stripped = line.strip()
        neutralized = False

        # 1. ROLE: content pattern (most common injection — SYSTEM:, ASSISTANT:, etc.)
        m = re.match(r"^(\s*)(SYSTEM|SYS|ASSISTANT|AI|BOT|USER|ROLE)(\s*:\s*)(.*)", stripped, re.IGNORECASE)
        if m:
            result_lines.append(f"{m.group(1)}[SAFE_ROLE]: {m.group(4)}")
            neutralized = True

        # 2. Bare role token on its own line (SYSTEM with no colon/nothing after)
        if not neutralized and re.match(r"^(SYSTEM|SYS|ASSISTANT|AI|BOT|USER|ROLE)$", stripped, re.IGNORECASE):
            result_lines.append("[SAFE_ROLE]: " + stripped)
            neutralized = True

        # 3. Chinese prompt injection variants
        if not neutralized:
            m = re.match(r"^(系统指令|system指令|角色设定)(.*)$", stripped, re.IGNORECASE | re.UNICODE)
            if m:
                result_lines.append(f"[SAFE_ROLE]: {m.group(2).lstrip(':').strip()}")
                neutralized = True

        # 4. "You are" / "Sei" behaviour-redefinition (any sentence, not just an/un)
        if not neutralized:
            m = re.match(r"^(you are|you're)(\s+.+)$", stripped, re.IGNORECASE)
            if m:
                result_lines.append(f"[SAFE_ROLE]: {m.group(2)}")
                neutralized = True

        # 5. Direct instruction override (imperative verbs at line start)
        if not neutralized:
            m = re.match(r"^(ignore|ignora|bypass|evade)(\s+.+)$", stripped, re.IGNORECASE)
            if m:
                result_lines.append(f"[SAFE_ROLE]: {m.group(2)}")
                neutralized = True

        # 6. Temporal override (da ora in poi / from now on)
        if not neutralized:
            m = re.match(r"^(da ora in poi|from now on|d'ora in poi)(\s+.+)$", stripped, re.IGNORECASE | re.UNICODE)
            if m:
                result_lines.append(f"[SAFE_ROLE]: {m.group(2)}")
                neutralized = True

        # Not neutralised — line is clean, pass through as-is
        if not neutralized:
            result_lines.append(line)

    return "\n".join(result_lines)



def _sanitize_search_result(text: str, max_len: int = 2000) -> str:
    """Sanitize text from web search results before injecting into LLM prompts.

    Search snippets can contain arbitrary content — page titles, metadata,
    even embedded role markers placed by malicious sites for SEO manipulation.
    """
    return _sanitize_for_llm(text, max_len=max_len)


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


def _search_searxng(query, max_results=5):
    """Search via SearXNG instance."""
    if not SEARXNG_URL:
        return None  # Not configured — caller decides fallback
    ck = _cache_key(f"searxng:{query}:{max_results}")
    cached = _get_cached(ck)
    if cached:
        return cached
    try:
        import urllib.parse as up
        params = {
            "q": query,
            "format": "json",
            "engines": "google,bing,duckduckgo,wikipedia",
            "categories": "general",
            "language": "it",
        }
        url = f"{SEARXNG_URL}/search?{up.urlencode(params)}"
        with httpx.Client(timeout=20) as client:
            resp = client.get(url, headers={"User-Agent": "hermes-mcp-server/4.1"})
            data = resp.json()

        results = []
        for item in data.get("results", [])[:max_results]:
            results.append({
                "title": item.get("title", ""),
                "url": item.get("url", ""),
                "snippet": item.get("content", ""),
            })

        result = {
            "query": query,
            "results": results,
            "total": len(results),
            "source": "searxng",
        }
        _set_cache(ck, result)
        return result
    except Exception as e:
        sys.stderr.write(f"SearXNG error: {e}\n")
        return None


def _search_web(query, max_results=5):
    """Unified web search: tries SearXNG first, falls back to DuckDuckGo."""
    # Try SearXNG if configured
    searxng_result = _search_searxng(query, max_results)
    if searxng_result is not None and searxng_result.get("results"):
        return searxng_result

    # Fall back to DuckDuckGo
    ddg_result = _search_ddg(query, max_results)
    return ddg_result


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
        host=_MCP_BIND_ADDR,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=False,
        ),
    )
else:
    mcp_server = FastMCP(name="hermes-web-mcp")


# ─── Italian timezone helper ──────────────────────────────
try:
    import zoneinfo
    _TIMEZONE = zoneinfo.ZoneInfo("Europe/Rome")
except Exception:
    from datetime import timezone as _tz, timedelta as _td
    class _CET(_tz):
        def utcoffset(self, dt): return _td(hours=1)
        def dst(self, dt): return _td(hours=0)
        def tzname(self, dt): return "CET"
    _TIMEZONE = _CET("CET")

_DAYS_IT = ["Lunedì","Martedì","Mercoledì","Giovedì","Venerdì","Sabato","Domenica"]
_MONTHS_IT = [
    "gennaio","febbraio","marzo","aprile","maggio","giugno",
    "luglio","agosto","settembre","ottobre","novembre","dicembre",
]


@mcp_server.tool()
async def get_current_datetime() -> str:
    """Ottieni la data e ora attuale in formato italiano (Europe/Rome)."""
    now = datetime.now(_TIMEZONE)
    return json.dumps({
        "date": f"{_DAYS_IT[now.weekday()]}, {now.day} {_MONTHS_IT[now.month - 1]} {now.year}",
        "time": now.strftime("%H:%M:%S"),
        "full_datetime": f"{_DAYS_IT[now.weekday()]} {now.day} {_MONTHS_IT[now.month - 1]} {now.year} alle {now.strftime('%H:%M:%S')}",
        "timezone": "Europe/Rome",
        "iso": now.isoformat(),
        "timestamp": int(now.timestamp()),
        "week_number": now.isocalendar()[1],
    }, ensure_ascii=False)


@mcp_server.tool()
@rate_limited
async def web_search(query: str, max_results: int = 5) -> str:
    """Ricerca informazioni su internet (SearXNG / DuckDuckGo) + sintesi LLM."""
    query = _sanitize_for_llm(query.strip(), max_len=200)
    max_r = min(max(1, int(max_results)), 10)
    result = await _external_call(_search_web, query, max_r)
    if "results" in result and result.get("results") and len(result["results"]) > 0:
        raw = "\n---\n".join([
            f"{_sanitize_search_result(r['title'], 150)}: {_sanitize_search_result(r['snippet'], 200)}"
            for r in result["results"]
        ])
        summary_prompt = (
            f"Sintetizza in italiano questi risultati di ricerca per: {query}\n\n"
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
    query = _sanitize_for_llm(query.strip(), max_len=200)
    search_result = await _external_call(_search_web, query)
    if search_result.get("error"):
        return json.dumps(search_result, indent=2)
    raw_content = "\n---\n".join([
        f"# {_sanitize_search_result(r['title'], 200)}\n{_sanitize_search_result(r['snippet'], 500)}"
        for r in search_result.get("results", [])
    ])
    llm_prompt = (
        f'Sei un assistente AI. Analizza questi risultati per: {query}\n\n'
        f"Risultati:\n{raw_content[:8000]}\n\n"
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
        raw_title = title_match.group(1) if title_match else "N/A"
        title = _sanitize_for_llm(raw_title, max_len=200)  # XSS / injection safe for JSON output
        summary = None
        if len(text) > 200:
            # Sanitize extracted content before LLM injection — strip structural attacks
            safe_text = re.sub(r'[\u200b\u200c\u200d\ufeff\u2060\u00ad]', '', text[:8000])
            prompt = f"Sintetizza in italiano:\n\n{_sanitize_for_llm(safe_text, max_len=6000)}\n\nFatti principali in max 5 punti."
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
        version="4.1.0",
        lifespan=_bridge_lifespan,
    )

    bridge_app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["*"],
    )

    # ── Request audit logging (middleware: fires after CORS) ──────
    @bridge_app.middleware("http")
    async def audit_middleware(request, call_next):
        start = time.monotonic()
        method = request.method
        path = request.url.path
        query_str = request.url.query if request.url.query else "-"

        response = await call_next(request)

        elapsed_ms = (time.monotonic() - start) * 1000
        client_ip = request.client.host if request.client else "-"
        _audit_logger.info(
            "%s %s?%s → %d (%.0fms) client=%s",
            method, path, query_str[:200], response.status_code, elapsed_ms, client_ip
        )
        return response

    @bridge_app.get("/health")
    async def health():
        """Health check endpoint."""
        return {
            "status": "ok",
            "version": "4.1.0",
            "rate_limit_max": _RATE_LIMIT_MAX,
            "concurrency_max": _SEMAPHORE_MAX,
            "token_bucket_available": _rate_limiter is not None,
        }

    @bridge_app.api_route("/api/search", methods=["GET", "POST"])
    @rate_limited
    async def api_search(query: str = Query(..., description="Search query"), max_results: int = 5):
        """Web search API endpoint (rate-limited via token bucket + semaphore)."""
        safe_query = _sanitize_for_llm(query.strip(), max_len=200) if isinstance(query, str) else str(query)
        result = await _external_call(_search_web, safe_query, min(max(1, max_results), 10))
        return result

    @bridge_app.api_route("/api/deep-search", methods=["GET", "POST"])
    @rate_limited
    async def api_deep_search(query: str = Query(..., description="Search query")):
        """Deep search API endpoint (rate-limited via token bucket + semaphore)."""
        safe_query = _sanitize_for_llm(query.strip(), max_len=200) if isinstance(query, str) else str(query)
        return await _external_call(_summarize_with_llm, safe_query)


# ── Startup helpers ──────────────────────────────────────────────────────

async def _start_http_bridge():
    """Launch the FastAPI bridge server on HERMES_MCP_BRIDGE_PORT."""
    if not HAS_FASTAPI:
        sys.stderr.write("Bridge: skipping (fastapi not installed)\n")
        return
    try:
        import uvicorn
        config = uvicorn.Config(bridge_app, host=_BRIDGE_BIND_ADDR, port=_BRIDGE_PORT, log_level="warning")
        server = uvicorn.Server(config)
        t = asyncio.create_task(server.serve())
        sys.stderr.write(f"Bridge: listening on {_BRIDGE_BIND_ADDR}:{_BRIDGE_PORT}\n")
        return t
    except Exception as e:
        sys.stderr.write(f"Bridge: failed to start ({e})\n")
        return None


async def main():
    # ── Start bridge server (if FastAPI available) ────────────────────────
    _bridge_task = await _start_http_bridge()

    print(f"\U0001f52e Hermes MCP Server v4.1", file=sys.stderr)
    print(f"   Transport: {TRANSPORT}", file=sys.stderr)
    print(f"   LLM: {LLM_ENDPOINT}", file=sys.stderr)
    print(f"   Bridge: {HERMES_BRIDGE_URL}", file=sys.stderr)

    # SearXNG status check
    if SEARXNG_URL and HTTPX_AVAILABLE:
        try:
            with httpx.Client(timeout=5) as c:
                r = c.get(SEARXNG_URL + "/search", params={"q": "test", "format": "json"}, follow_redirects=True)
                if r.status_code == 200 and isinstance(r.json(), dict):
                    print(f"   SearXNG: connected ({SEARXNG_URL})", file=sys.stderr)
                else:
                    print(f"   SearXNG: responding (status {r.status_code})", file=sys.stderr)
        except Exception as e:
            print(f"   SearXNG: unreachable, using DuckDuckGo fallback ({e})", file=sys.stderr)
    elif DDG_AVAILABLE:
        print(f"   Search engine: DuckDuckGo (no SEARXNG_URL configured)", file=sys.stderr)

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
            config = uvicorn.Config(cors_app, host=_MCP_BIND_ADDR, port=port, log_level="info")
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
