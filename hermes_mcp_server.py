#!/usr/bin/env python3
"""
Hermes MCP Server v1.5.0 — Web Search & LLM Synthesis Bridge + Scientific Computing Suite

MCP (Model Context Protocol) server che espone strumenti di ricerca web e calcolo scientifico:
  - web_search    : Ricerca rapida via DuckDuckGo / SearXNG + sintesi LLM
  - deep_search   : Ricerca profonda con analisi strutturata dell'LLM
  - read_webpage  : Lettura e sintesi LLM di pagine web (con SSRF guard)

Note: lo strumento `get_current_datetime` è stato spostato nel server dedicato
[hermes-mcp-timedata](https://github.com/ragostino74/hermes-mcp-timedata).

  - solve_equation : Risolve equazioni algebriche (lineari, quadratiche, sistemi)
  - differentiate  : Derivate prime, seconde e parziali
  - integrate      : Integrali definiti e non definiti
  - limit_func     : Calcolo di limiti di funzioni
  - simplify_expr  : Semplificazione di espressioni simboliche
  - numerical_calculate : Calcoli numerici complessi (NumPy/SciPy)
  - matrix_operations : Operazioni matriciali (det, autovalori, inversa, SVD)
  - statistics     : Statistica descrittiva e regressioni (NumPy/SciPy)

Caratteristiche:
  - Doppio trasporto: stdio (Claude Desktop, VS Code) + HTTP/StreamableHTTP
  - Rate limiting configurabile (token bucket + semaphore)
  - Bridge REST API su :18761 per integrazioni esterne
  - SSRF protection completa (IP privati, IPv6, metadata endpoints, DNS rebinding)
  - Prompt injection sanitization (3 fasi: control chars, role markers, structural)
  - Cache LRU con TTL e SHA-256

Modi di esecuzione:
  # STDIO (default — per Claude Desktop, VS Code, Hermes Agent)
  python hermes_mcp_server.py

  # HTTP/StreamableHTTP (per llama.cpp WebUI e browser)
  HERMES_MCP_TRANSPORT=http HERMES_MCP_PORT=18760 \
    python hermes_mcp_server.py

  # DUAL (entrambi insieme)
  HERMES_MCP_TRANSPORT=dual HERMES_MCP_PORT=18760 \
    python hermes_mcp_server.py

Variabili d'ambiente:
  LLM_ENDPOINT        : Endpoint LLM OpenAI-compatible (default: localhost:10000/v1)
  LLM_MODEL           : Nome modello (default: Qwen3.6-35B-A3B-Q8_0.gguf)
  SEARXNG_URL         : Istanzа SearXNG per ricerca avanzata (opzionale)
  HERMES_MCP_PORT     : Porta HTTP MCP (default: 18760)
  HERMES_MCP_BRIDGE_PORT : Porta bridge API (default: 18761)
  HERMES_MCP_TRANSPORT : stdio | http | dual (default: stdio)
  HERMES_MCP_RATE_LIMIT : Max chiamate/minute per token bucket (default: 5)
  HERMES_MCP_CONCURRENCY : Max chiamate parallele (default: 3)
  HERMES_BRIDGE_BIND_ADDR : Bind bridge API (default: 127.0.0.1)
  HERMES_MCP_BIND_ADDR    : Bind MCP HTTP (default: 127.0.0.1)
  HERMES_MCP_CORS_ORIGINS : CORS origins comma-separated (default: localhost:*)
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

# ── Scientific computing imports (graceful fallback) ────────────────
try:
    import sympy as _sympy_mod
    from sympy import symbols, Eq, solve, diff, integrate, limit as sympy_limit, simplify as sympy_simplify, sympify
    SYMPY_AVAILABLE = True
except ImportError:
    SYMPY_AVAILABLE = False

try:
    import numpy as np
    NUMPY_AVAILABLE = True
except ImportError:
    NUMPY_AVAILABLE = False
    np = None

try:
    from scipy import linalg as scipy_linalg
    from scipy import stats as scipy_stats
    SCIPY_AVAILABLE = True
except ImportError:
    SCIPY_AVAILABLE = False
    scipy_linalg = None
    scipy_stats = None

TRANSPORT = os.environ.get("HERMES_MCP_TRANSPORT", "stdio")
LLM_ENDPOINT = os.environ.get("LLM_ENDPOINT", "http://localhost:10000/v1")
LLM_MODEL = os.environ.get("LLM_MODEL", "Qwen3.6-35B-A3B-Q8_0.gguf")
HERMES_BRIDGE_URL = os.environ.get("HERMES_BRIDGE_URL", "")
SEARXNG_URL = os.environ.get("SEARXNG_URL", "").rstrip("/")

# ── Server bind addresses (default localhost for security) ────────
_MCP_BIND_ADDR = os.environ.get("HERMES_MCP_BIND_ADDR", "127.0.0.1")  # MCP HTTP transport
_BRIDGE_BIND_ADDR = os.environ.get("HERMES_BRIDGE_BIND_ADDR", "127.0.0.1")  # Bridge REST API
def _is_safe_url(url: str) -> bool:
    """Block access to localhost, private IPs, link-local, metadata endpoints.

    Also blocks IDN homograph attacks (e.g., xn--p1ai lookalikes), Unicode
    confusion characters, and punycode-encoded hostnames that could bypass
    hostname-based allowlists via visual spoofing.
    """
    import socket

    parsed = urlparse(url)
    raw_host = (parsed.hostname or "").lower()

    # ── IDN Homograph / Punycode pre-check ──────────────────────────────
    # If the hostname contains 'xn--', it's a punycode-encoded domain.
    # These can be used to visually spoof legitimate domains (e.g.,
    # "amazоn.com" with Cyrillic о vs Latin o, or xn-- domains that
    # look like English words). Block all punycode hostnames as a
    # defense-in-depth measure.
    if "xn--" in raw_host:
        return False

    # Check for Unicode confusion / homoglyph characters (non-ASCII chars
    # that look like ASCII but resolve differently in IDN contexts):
    #   - Cyrillic о (U+043E) looks like Latin o
    #   - Greek ω (U+03C9), Arabic waw, etc.
    # If the hostname contains ANY non-ASCII character after punycode check,
    # it's potentially a homograph attack.
    for ch in raw_host:
        if ord(ch) > 127:
            return False

    # Block by hostname (ASCII-safe after above checks)
    blocked_hosts = {"localhost", "127.0.0.1", "::1", "0.0.0.0"}
    if raw_host in blocked_hosts:
        return False

    # Resolve IP to catch localhost aliases and expand punycode
    try:
        addrinfo = socket.getaddrinfo(raw_host, None)
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


# ── Cache salt (anti-poisoning): random per-process, prevents targeted eviction ──
_CACHE_SALT = os.urandom(16).hex()


def _cache_key(text: str) -> str:
    """Compute a cache key with process salt to prevent cache poisoning attacks.

    Salt makes it impossible for an attacker to predict or target specific
    cache entries (mitigates CVE-level cache eviction amplification).
    The salt is per-process — each restart generates a new random value.
    Uses SHA-256 (not MD5) to prevent intentional collision attacks.
    """
    return hashlib.sha256((_CACHE_SALT + text).encode("utf-8", errors="replace")).hexdigest()


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
    """Use local llama.cpp to summarize content.

    This is a SYNC function called via _external_call() which wraps it in
    asyncio.to_thread()/run_in_executor to prevent blocking the event loop.
    """
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
    except Exception:
        sys.stderr.write("LLM summarize error: [hidden]\n")
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
    except Exception:
        sys.stderr.write("DDG search error: [hidden]\n")
        return {"error": "Ricerca fallita (errore interno)", "results": []}


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
            resp = client.get(url, headers={"User-Agent": "hermes-mcp-server/1.5.0"})
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
    except Exception:
        sys.stderr.write("SearXNG error: [hidden]\n")
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
        # MEDIUM #4: Granular timeout instead of flat 180s (was causing thread-pool saturation)
        with httpx.Client(timeout=httpx.Timeout(connect=30, read=60, write=30, pool=5)) as client:
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
            enable_dns_rebinding_protection=True,  # HIGH #2: Re-enabled DNS rebinding protection
        ),
    )
else:
    mcp_server = FastMCP(name="hermes-web-mcp")


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
        # FIX CRITICAL #1 (SSRF via redirect): Disable automatic redirects.
        # If a user-controlled URL redirects to a private/metadata IP (e.g. 169.254.169.254),
        # httpx would follow it and exfiltrate cloud credentials. Instead we allow at most
        # 3 manual redirects, verifying _is_safe_url() at each hop.
        final_url = url
        max_redirects = 3
        for _ in range(max_redirects):
            with httpx.Client(timeout=30, follow_redirects=False) as client:
                resp = client.get(
                    final_url,
                    headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64)"},
                )
            # Check if this is a redirect (3xx)
            if 300 <= resp.status_code < 400:
                redirect_location = resp.headers.get("location")
                if not redirect_location:
                    return json.dumps({"error": "Redirect senza location header", "url": url}, indent=2)
                # Resolve relative URLs to absolute
                from urllib.parse import urljoin as _urljoin
                if not redirect_location.startswith(("http://", "https://")):
                    redirect_location = _urljoin(final_url, redirect_location)
                # Verify the redirect target is safe
                if not _is_safe_url(redirect_location):
                    return json.dumps({
                        "error": f"Accesso bloccato: redirect verso URL non sicuro ({redirect_location})",
                        "url": url,
                        "redirect_from": final_url,
                        "redirect_to": redirect_location,
                    }, indent=2)
                final_url = redirect_location
            else:
                # Not a redirect — proceed with response
                break

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
        return json.dumps({
            "error": "Errore durante la lettura della pagina",
            "url": url,
        }, indent=2)


# ── Scientific Computing Tools (SymPy + NumPy/SciPy) ────────────────────

@mcp_server.tool()
@rate_limited
async def solve_equation(
    equation: str,
    variable: str = "x",
    system: str = "",
) -> str:
    """Risolve equazioni algebriche (lineari, quadratiche, sistemi).

    Args:
        equation: Equazione da risolvere (es: "x**2 - 4", "2*x + 3 = 7").
                  Se '=' presente, risolve equation.lhs = equation.rhs.
        variable: Variabile rispetto a cui risolvere (default: "x").
        system: (opzionale) Se fornito, risolve un sistema di equazioni.
                Formato: "eq1|eq2|eq3" con separatori '|'.
                L'equazione principale è il primo elemento.

    Examples:
        - "x**2 - 4" → x = ±2
        - "2*x + 3 = 7" → x = 2
        - system="x + y - 5|2*x - y - 1" → sistema lineare 2x2
    """
    if not SYMPY_AVAILABLE:
        return json.dumps({"error": "SymPy non installato"}, indent=2)

    try:
        x, y, z = symbols('x y z')
        var_map = {'x': x, 'y': y, 'z': z}
        sym_var = var_map.get(variable, symbols(variable))

        # Helper: parse equation string into SymPy expression
        def parse_eq(eq_str):
            eq_str = eq_str.strip()
            if '=' in eq_str:
                lhs, rhs = eq_str.split('=', 1)
                return Eq(sympify(lhs.strip()), sympify(rhs.strip()))
            return sympify(eq_str)

        if system and '|' in system:
            # System of equations
            eqs = [parse_eq(s.strip()) for s in system.split('|')]
            eqs[0] = parse_eq(equation)
            vars_to_solve = [sym_var]
            # Detect additional variables
            for eq in eqs:
                free = eq.free_symbols
                for fs in free:
                    if str(fs) not in ('x', 'y', 'z') or len(eqs) > 1:
                        s = str(fs)
                        if s not in var_map:
                            var_map[s] = symbols(s)
                        vars_to_solve.append(var_map[s])
            vars_to_solve = list(dict.fromkeys(vars_to_solve))  # dedupe
            solution = solve(eqs, vars_to_solve)
            return json.dumps({
                "type": "system",
                "equations": eqs,
                "variables": [str(v) for v in vars_to_solve],
                "solution": str(solution),
                "numerical": [
                    {str(k): float(v) if hasattr(v, 'evalf') else str(v)
                     for k, v in sol.items()}
                    if isinstance(sol, dict) else str(sol)
                    for sol in solution
                ] if isinstance(solution, list) and solution and isinstance(solution[0], dict) else str(solution),
            }, ensure_ascii=False)

        # Single equation
        eq = parse_eq(equation)
        solutions = solve(eq, sym_var)

        result = {
            "equation": equation,
            "variable": variable,
            "type": "single",
            "solutions_count": len(solutions),
        }

        if len(solutions) == 1:
            result["solution"] = str(solutions[0])
            try:
                result["numerical_value"] = float(solutions[0].evalf())
            except (TypeError, ValueError):
                result["numerical_value"] = None
        else:
            result["solutions"] = [str(s) for s in solutions]
            try:
                result["numerical_values"] = [float(s.evalf()) for s in solutions]
            except (TypeError, ValueError):
                result["numerical_values"] = result["solutions"]

        return json.dumps(result, ensure_ascii=False, indent=2)

    except Exception as e:
        return json.dumps({
            "error": f"Errore nella risoluzione: {str(e)}",
            "equation": equation,
        }, indent=2)


@mcp_server.tool()
@rate_limited
async def differentiate(
    expression: str,
    variable: str = "x",
    order: int = 1,
) -> str:
    """Calcola la derivata di una funzione.

    Args:
        expression: Espressione matematica (es: "x**3 + 2*x", "sin(x)*exp(x)").
        variable: Variabile di derivazione (default: "x").
        order: Ordine della derivata (default: 1, prima derivata).

    Examples:
        - "x**3 + 2*x", variable="x", order=1 → 3*x**2 + 2
        - "sin(x)", variable="x", order=2 → -sin(x)
        - "x**2*y + x*y**2", variable="x", order=1 → derivata parziale
    """
    if not SYMPY_AVAILABLE:
        return json.dumps({"error": "SymPy non installato"}, indent=2)

    try:
        x, y, z = symbols('x y z')
        var_map = {'x': x, 'y': y, 'z': z}
        sym_var = var_map.get(variable, symbols(variable))
        expr = sympify(expression)

        if order == 1:
            result = diff(expr, sym_var)
        else:
            result = diff(expr, sym_var, order)

        simplified = simplify(result) if not isinstance(result, (int, float)) else result

        return json.dumps({
            "expression": expression,
            "variable": variable,
            "order": order,
            "derivative": str(result),
            "simplified": str(simplified),
            "latex": str(result),
        }, ensure_ascii=False, indent=2)

    except Exception as e:
        return json.dumps({
            "error": f"Errore nel calcolo della derivata: {str(e)}",
            "expression": expression,
        }, indent=2)


@mcp_server.tool()
@rate_limited
async def integrate(
    expression: str,
    variable: str = "x",
    a: str = "",
    b: str = "",
) -> str:
    """Calcola integrali definiti e non definiti.

    Args:
        expression: Espressione da integrare (es: "x**2", "sin(x)", "1/x").
        variable: Variabile di integrazione (default: "x").
        a: Limite inferiore (opzionale). Se fornito + b, integrale definito.
        b: Limite superiore (opzionale). Se fornito + a, integrale definito.

    Examples:
        - "x**2", variable="x" → x**3/3 + C
        - "sin(x)", variable="x", a="0", b="pi" → 2
    """
    if not SYMPY_AVAILABLE:
        return json.dumps({"error": "SymPy non installato"}, indent=2)

    try:
        x, y, z = symbols('x y z')
        var_map = {'x': x, 'y': y, 'z': z}
        sym_var = var_map.get(variable, symbols(variable))
        expr = sympify(expression)

        indefinite = integrate(expr, sym_var)

        result = {
            "expression": expression,
            "variable": variable,
            "indefinite_integral": str(indefinite),
        }

        if a and b:
            try:
                a_val = sympify(a)
                b_val = sympify(b)
                definite = indefinite.subs(sym_var, b_val) - indefinite.subs(sym_var, a_val)
                result["definite"] = str(definite)
                result["numerical_value"] = float(definite.evalf())
                result["limits"] = {"lower": str(a_val), "upper": str(b_val)}
            except Exception as e:
                result["definite_error"] = str(e)
        else:
            result["note"] = "Integrali indefiniti includono una costante arbitraria +C"

        return json.dumps(result, ensure_ascii=False, indent=2)

    except Exception as e:
        return json.dumps({
            "error": f"Errore nel calcolo dell'integrale: {str(e)}",
            "expression": expression,
        }, indent=2)


@mcp_server.tool()
@rate_limited
async def limit_func(
    expression: str,
    variable: str = "x",
    point: str = "0",
    direction: str = "-",
) -> str:
    """Calcola il limite di una funzione in un punto.

    Args:
        expression: Espressione della funzione (es: "sin(x)/x", "1/x").
        variable: Variabile (default: "x").
        point: Punto in cui calcolare il limite (default: "0").
        direction: Direzione: "-" per dx (da sinistra), "+" per dx+ (da destra).

    Examples:
        - "sin(x)/x", x→0 → 1
        - "1/x", x→0, direction="+" → ∞
    """
    if not SYMPY_AVAILABLE:
        return json.dumps({"error": "SymPy non installato"}, indent=2)

    try:
        x, y, z = symbols('x y z')
        var_map = {'x': x, 'y': y, 'z': z}
        sym_var = var_map.get(variable, symbols(variable))
        expr = sympify(expression)
        point_val = sympify(point)

        # SymPy direction: '-' for left limit (x→a-), '+' for right limit (x→a+)
        dir_sym = {'-': '-', '+': '+'}.get(direction, '-')

        lim = sympy_limit(expr, sym_var, point_val, dir_sym)

        result = {
            "expression": expression,
            "variable": variable,
            "point": str(point_val),
            "direction": direction,
            "limit": str(lim),
        }

        # Try numerical approximation
        try:
            result["numerical_value"] = float(lim.evalf())
        except (TypeError, ValueError, NotImplementedError):
            if str(lim) in ('oo', '-oo', 'zoo', 'nan'):
                result["numerical_value"] = lim
            else:
                result["numerical_value"] = None

        return json.dumps(result, ensure_ascii=False, indent=2)

    except Exception as e:
        return json.dumps({
            "error": f"Errore nel calcolo del limite: {str(e)}",
            "expression": expression,
        }, indent=2)


@mcp_server.tool()
@rate_limited
async def simplify_expr(
    expression: str,
) -> str:
    """Semplifica espressioni matematiche simboliche.

    Args:
        expression: Espressione da semplificare (es: "x**2 + 2*x + x**2", "sin(x)**2 + cos(x)**2").

    Examples:
        - "x**2 + 2*x + x**2" → 2*x**2 + 2*x
        - "sin(x)**2 + cos(x)**2" → 1
    """
    if not SYMPY_AVAILABLE:
        return json.dumps({"error": "SymPy non installato"}, indent=2)

    try:
        expr = sympify(expression)
        simplified = sympy_simplify(expr)
        # Also try trigsimp and radsimp for better results
        from sympy import trigsimp, radsimp
        trigsimp_result = trigsimp(expr)
        radsimp_result = radsimp(expr)

        return json.dumps({
            "expression": expression,
            "simplified": str(simplified),
            "trigsimp": str(trigsimp_result),
            "radsimp": str(radsimp_result),
            "original_type": str(type(expr).__name__),
        }, ensure_ascii=False, indent=2)

    except Exception as e:
        return json.dumps({
            "error": f"Errore nella semplificazione: {str(e)}",
            "expression": expression,
        }, indent=2)


@mcp_server.tool()
@rate_limited
async def numerical_calculate(
    operation: str,
    a: str = "",
    b: str = "",
    array_a: str = "",
    array_b: str = "",
) -> str:
    """Calcoli numerici complessi con NumPy/SciPy.

    Args:
        operation: Tipo di calcolo:
            - "arithmetic": operazioni base (a op b): +, -, *, /, **, %
            - "trigonometric": sin, cos, tan, asin, acos, atan (valore o array)
            - "logarithmic": log, log2, log10, exp (valore o array)
            - "power": sqrt, cbrt, power(base, exp)
            - "statistics_basic": media, mediana, deviazione_std, varianza su array
            - "regression_linear": regressione lineare su array_a e array_b
            - "matrix_multiply": moltiplicazione matrici (array_a, array_b come liste)
            - "probability_normal": pdf/cdf normale (mu, sigma, x)
            - "probability_poisson": pmf/cdf Poisson (lambda, k)
        a: Primo operando / primo array (JSON) / parametro 1
        b: Secondo operando / parametro 2
        array_a: Array JSON per operazioni su array (es: "[1,2,3,4,5]")
        array_b: Secondo array JSON per operazioni binarie su array

    Examples:
        - operation="arithmetic", a="10", b="3" → 10 op 3
        - operation="trigonometric", a="3.14159" (sin)
        - operation="statistics_basic", array_a="[2,4,6,8,10]"
        - operation="regression_linear", array_a="[1,2,3,4,5]", array_b="[2,4,5,4,5]"
        - operation="probability_normal", a="0", b="1", array_a="1.96"
    """
    if not NUMPY_AVAILABLE:
        return json.dumps({"error": "NumPy non installato"}, indent=2)

    try:
        def parse_array(s):
            if not s:
                return None
            data = json.loads(s)
            if isinstance(data, list):
                return np.array(data, dtype=float)
            return np.array([float(data)], dtype=float)

        if operation == "arithmetic":
            if not a or not b:
                return json.dumps({"error": "Operazione arithmetic richiede a e b"}, indent=2)
            val_a, val_b = float(a), float(b)
            ops = {
                '+': lambda x, y: x + y, '-': lambda x, y: x - y,
                '*': lambda x, y: x * y, '/': lambda x, y: x / y if y != 0 else None,
                '**': lambda x, y: x ** y, '%': lambda x, y: x % y if y != 0 else None,
            }
            # Detect operator from 'a' if it's an operator
            if a in ops and b:
                op_sym = a
                result_val = ops[op_sym](val_b, float(b))
                return json.dumps({
                    "operation": "arithmetic",
                    "expression": f"1{op_sym}1",
                    "result": result_val,
                }, ensure_ascii=False, indent=2)

            op_sym = '+'  # default
            if '/' in str(b):
                op_sym = '/'
            elif '*' in str(b):
                op_sym = '*'

            result = eval(f"{a} {op_sym} {b}")
            return json.dumps({
                "operation": "arithmetic",
                "expression": f"{a} {op_sym} {b}",
                "result": result,
            }, ensure_ascii=False, indent=2)

        elif operation == "trigonometric":
            val = float(a) if a else 0
            funcs = {'sin': np.sin, 'cos': np.cos, 'tan': np.tan,
                     'asin': np.arcsin, 'acos': np.arccos, 'atan': np.arctan}
            func_name = a.lower() if a and a.lower() in funcs else 'sin'
            val = float(b) if a and a.lower() not in funcs else val
            func = funcs[func_name]
            result_val = func(val)
            return json.dumps({
                "operation": "trigonometric",
                "function": func_name,
                "input": val,
                "result": result_val,
                "degrees": float(np.degrees(result_val)) if result_val is not None else None,
            }, ensure_ascii=False, indent=2)

        elif operation == "logarithmic":
            val = float(a) if a else 1
            funcs = {'log': np.log, 'log2': np.log2, 'log10': np.log10, 'exp': np.exp}
            func_name = a.lower() if a and a.lower() in funcs else 'log'
            val = float(b) if a and a.lower() not in funcs else val
            func = funcs[func_name]
            return json.dumps({
                "operation": "logarithmic",
                "function": func_name,
                "input": val,
                "result": func(val),
            }, ensure_ascii=False, indent=2)

        elif operation == "power":
            if array_a is not None:
                arr = parse_array(array_a)
                return json.dumps({
                    "operation": "power",
                    "sqrt": np.sqrt(arr),
                    "cbrt": np.cbrt(arr),
                }, ensure_ascii=False, indent=2)
            base = float(a) if a else 0
            exp = float(b) if b else 0.5
            return json.dumps({
                "operation": "power",
                "base": base,
                "exponent": exp,
                "result": base ** exp,
                "sqrt": float(np.sqrt(base)),
                "cbrt": float(np.cbrt(base)),
            }, ensure_ascii=False, indent=2)

        elif operation == "statistics_basic":
            arr = parse_array(array_a)
            if arr is None:
                return json.dumps({"error": "statistics_basic richiede array_a"}, indent=2)
            return json.dumps({
                "operation": "statistics_basic",
                "data": arr.tolist(),
                "mean": float(np.mean(arr)),
                "median": float(np.median(arr)),
                "std": float(np.std(arr)),
                "variance": float(np.var(arr)),
                "min": float(np.min(arr)),
                "max": float(np.max(arr)),
                "sum": float(np.sum(arr)),
                "count": len(arr),
            }, ensure_ascii=False, indent=2)

        elif operation == "regression_linear":
            arr_a = parse_array(array_a)
            arr_b = parse_array(array_b)
            if arr_a is None or arr_b is None:
                return json.dumps({"error": "regression_linear richiede array_a e array_b"}, indent=2)
            if len(arr_a) != len(arr_b):
                return json.dumps({"error": "array_a e array_b devono avere la stessa lunghezza"}, indent=2)
            # Linear regression: y = mx + c
            slope, intercept, r_value, p_value, std_err = scipy_stats.linregress(arr_a, arr_b)
            return json.dumps({
                "operation": "regression_linear",
                "slope": float(slope),
                "intercept": float(intercept),
                "r_value": float(r_value),
                "r_squared": float(r_value ** 2),
                "p_value": float(p_value),
                "std_error": float(std_err),
                "equation": f"y = {slope:.6f}x + {intercept:.6f}",
                "data_points": len(arr_a),
            }, ensure_ascii=False, indent=2)

        elif operation == "matrix_multiply":
            arr_a = parse_array(array_a)
            arr_b = parse_array(array_b)
            if arr_a is None or arr_b is None:
                return json.dumps({"error": "matrix_multiply richiede array_a e array_b"}, indent=2)
            result = np.dot(arr_a.reshape(-1, len(arr_a)), arr_b.reshape(len(arr_b), -1))
            return json.dumps({
                "operation": "matrix_multiply",
                "shape_a": list(arr_a.shape),
                "shape_b": list(arr_b.shape),
                "result": result.tolist(),
            }, ensure_ascii=False, indent=2)

        elif operation == "probability_normal":
            mu = float(a) if a else 0
            sigma = float(b) if b else 1
            x_val = float(array_a) if array_a else 0
            from scipy.stats import norm
            pdf_val = norm.pdf(x_val, mu, sigma)
            cdf_val = norm.cdf(x_val, mu, sigma)
            return json.dumps({
                "operation": "probability_normal",
                "mu": mu,
                "sigma": sigma,
                "x": x_val,
                "pdf": pdf_val,
                "cdf": cdf_val,
                "ppf": norm.ppf(cdf_val, mu, sigma),  # inverse CDF
            }, ensure_ascii=False, indent=2)

        elif operation == "probability_poisson":
            lam = float(a) if a else 1
            k = int(float(b)) if b else 0
            from scipy.stats import poisson
            pmf_val = poisson.pmf(k, lam)
            cdf_val = poisson.cdf(k, lam)
            return json.dumps({
                "operation": "probability_poisson",
                "lambda": lam,
                "k": k,
                "pmf": pmf_val,
                "cdf": cdf_val,
            }, ensure_ascii=False, indent=2)

        else:
            return json.dumps({
                "error": f"Operazione '{operation}' non riconosciuta",
                "supported": ["arithmetic", "trigonometric", "logarithmic", "power",
                              "statistics_basic", "regression_linear", "matrix_multiply",
                              "probability_normal", "probability_poisson"],
            }, indent=2)

    except json.JSONDecodeError:
        return json.dumps({"error": f"Formato JSON non valido in array_a o array_b"}, indent=2)
    except Exception as e:
        return json.dumps({
            "error": f"Errore nel calcolo numerico: {str(e)}",
            "operation": operation,
        }, indent=2)


@mcp_server.tool()
@rate_limited
async def matrix_operations(
    matrix: str,
    operation: str = "det",
) -> str:
    """Operazioni matriciali: determinante, autovalori, inversa, SVD, trasposta.

    Args:
        matrix: Matrice in formato JSON (es: "[[1,2],[3,4]]").
        operation: Tipo di operazione:
            - "det": determinante
            - "eigenvalues": autovalori e autovettori
            - "inverse": matrice inversa
            - "svd": decomposizione SVD
            - "transpose": trasposta
            - "rank": rango
            - "trace": traccia
            - "norm": norma matriciale

    Examples:
        - matrix="[[1,2],[3,4]]", operation="det" → -2
        - matrix="[[1,2],[3,4]]", operation="inverse" → [[-2,1],[1.5,-0.5]]
    """
    if not NUMPY_AVAILABLE or not SCIPY_AVAILABLE:
        return json.dumps({"error": "NumPy/SciPy non installati"}, indent=2)

    try:
        mat_data = json.loads(matrix)
        mat = np.array(mat_data, dtype=float)
        rows, cols = mat.shape

        result = {
            "operation": operation,
            "matrix_shape": [rows, cols],
            "matrix": mat.tolist(),
        }

        if operation == "det":
            if rows != cols:
                return json.dumps({"error": "Il determinante richiede una matrice quadrata"}, indent=2)
            result["determinant"] = float(np.linalg.det(mat))

        elif operation == "eigenvalues":
            if rows != cols:
                return json.dumps({"error": "Autovalori richiedono una matrice quadrata"}, indent=2)
            eigenvalues, eigenvectors = np.linalg.eig(mat)
            result["eigenvalues"] = eigenvalues.real.tolist() if np.all(eigenvalues.imag == 0) else eigenvalues.tolist()
            result["eigenvectors"] = eigenvectors.real.tolist() if np.all(eigenvectors.imag == 0) else eigenvectors.tolist()

        elif operation == "inverse":
            if rows != cols:
                return json.dumps({"error": "L'inversa richiede una matrice quadrata"}, indent=2)
            det = np.linalg.det(mat)
            if abs(det) < 1e-12:
                return json.dumps({"error": "La matrice è singolare (determinante ≈ 0), non invertibile"}, indent=2)
            result["inverse"] = np.linalg.inv(mat).tolist()
            result["determinant"] = float(det)

        elif operation == "svd":
            U, s, Vt = np.linalg.svd(mat)
            result["U"] = U.tolist()
            result["singular_values"] = s.tolist()
            result["Vt"] = Vt.tolist()

        elif operation == "transpose":
            result["transpose"] = mat.T.tolist()

        elif operation == "rank":
            result["rank"] = int(np.linalg.matrix_rank(mat))

        elif operation == "trace":
            if rows != cols:
                return json.dumps({"error": "La traccia richiede una matrice quadrata"}, indent=2)
            result["trace"] = float(np.trace(mat))

        elif operation == "norm":
            result["frobenius_norm"] = float(np.linalg.norm(mat, 'fro'))
            result["spectral_norm"] = float(np.linalg.norm(mat, 2))
            result["infinity_norm"] = float(np.linalg.norm(mat, np.inf))
            result["1_norm"] = float(np.linalg.norm(mat, 1))

        else:
            return json.dumps({
                "error": f"Operazione '{operation}' non riconosciuta",
                "supported": ["det", "eigenvalues", "inverse", "svd", "transpose", "rank", "trace", "norm"],
            }, indent=2)

        return json.dumps(result, ensure_ascii=False, indent=2)

    except json.JSONDecodeError:
        return json.dumps({"error": "Formato JSON non valido per la matrice"}, indent=2)
    except Exception as e:
        return json.dumps({
            "error": f"Errore operazioni matriciali: {str(e)}",
            "operation": operation,
        }, indent=2)


@mcp_server.tool()
@rate_limited
async def statistics(
    data: str,
    operation: str = "full",
    confidence: float = 0.95,
) -> str:
    """Statistica descrittiva e regressioni con NumPy/SciPy.

    Args:
        data: Dati in formato JSON (es: "[1,2,3,4,5]" o "[[1,2,3],[4,5,6]]" per dati multivariati).
        operation: Tipo di analisi:
            - "full": analisi completa (tutte le metriche)
            - "descriptive": solo statistiche descrittive
            - "correlation": matrice di correlazione (dati multivariati)
            - "hypothesis_ttest": test t per campioni singoli/accoppiati
            - "chi_square": test del chi-quadrato
            - "normality": test di normalità (Shapiro-Wilk)
            - "regression": regressione lineare
            - "anomaly": rilevamento anomalie (z-score)
        confidence: Livello di confidenza per intervalli (default: 0.95)

    Examples:
        - data="[2,4,6,8,10,12,14]", operation="full"
        - data="[[1,2,3],[4,5,6],[7,8,9]]", operation="correlation"
        - data="[3,5,7,9,11]", operation="normality"
        - data="[1,2,3,4,5]", operation="anomaly"
    """
    if not NUMPY_AVAILABLE or not SCIPY_AVAILABLE:
        return json.dumps({"error": "NumPy/SciPy non installati"}, indent=2)

    try:
        data_list = json.loads(data)
        from scipy import stats as sp_stats
        if isinstance(data_list[0], list):
            arr = np.array(data_list, dtype=float)
            multi_variate = True
        else:
            arr = np.array(data_list, dtype=float).flatten()
            multi_variate = False

        result = {
            "operation": operation,
            "data_count": len(arr),
            "data_type": "multivariate" if multi_variate else "univariate",
        }

        if operation in ("full", "descriptive"):
            result["mean"] = float(np.mean(arr))
            result["median"] = float(np.median(arr))
            result["std"] = float(np.std(arr, ddof=1))
            result["variance"] = float(np.var(arr, ddof=1))
            result["min"] = float(np.min(arr))
            result["max"] = float(np.max(arr))
            result["sum"] = float(np.sum(arr))
            result["range"] = float(np.ptp(arr))
            result["quartile_25"] = float(np.percentile(arr, 25))
            result["quartile_75"] = float(np.percentile(arr, 75))
            result["iqr"] = float(np.percentile(arr, 75) - np.percentile(arr, 25))

            # Skewness and kurtosis
            if len(arr) >= 3:
                result["skewness"] = float(sp_stats.skew(arr))
                result["kurtosis"] = float(sp_stats.kurtosis(arr))

            # Confidence interval
            if len(arr) >= 2:
                se = float(np.std(arr, ddof=1) / np.sqrt(len(arr)))
                t_crit = sp_stats.t.ppf((1 + confidence) / 2, len(arr) - 1)
                result["confidence_interval"] = {
                    "level": confidence,
                    "margin_of_error": float(t_crit * se),
                    "lower": float(np.mean(arr) - t_crit * se),
                    "upper": float(np.mean(arr) + t_crit * se),
                }

        if operation in ("full", "correlation"):
            if multi_variate:
                corr_matrix = np.corrcoef(arr)
                result["correlation_matrix"] = corr_matrix.tolist()
                result["covariance_matrix"] = np.cov(arr).tolist()
            else:
                result["correlation_note"] = "La correlazione richiede dati multivariati (array di array)"

        if operation in ("full", "normality"):
            if 8 <= len(arr) <= 5000:
                stat, p_value = sp_stats.shapiro(arr)
                result["normality_test"] = {
                    "test": "Shapiro-Wilk",
                    "statistic": float(stat),
                    "p_value": float(p_value),
                    "is_normal": p_value > 0.05,
                    "note": "H0: i dati provengono da una distribuzione normale" if p_value > 0.05 else "H0 rifiutata: i dati NON sono normali",
                }
            else:
                result["normality_test_note"] = f"Shapiro-Wilk richiede 8-5000 campioni (hai {len(arr)})"

        if operation in ("full", "anomaly"):
            if not multi_variate:
                z_scores = np.abs(sp_stats.zscore(arr))
                threshold = 2.0
                anomalies = np.where(z_scores > threshold)[0]
                result["anomaly_detection"] = {
                    "method": "z-score",
                    "threshold": threshold,
                    "anomaly_indices": anomalies.tolist(),
                    "anomaly_values": arr[anomalies].tolist(),
                    "anomaly_count": len(anomalies),
                    "z_scores": z_scores.tolist(),
                }

        if operation in ("full", "regression"):
            if multi_variate and len(arr[0]) >= 2:
                # Use first column as x, second as y
                x_data = arr[:, 0]
                y_data = arr[:, 1]
            elif not multi_variate and len(arr) >= 2:
                x_data = np.arange(len(arr), dtype=float)
                y_data = arr
            else:
                return json.dumps({"error": "La regressione richiede almeno 2 dati"}, indent=2)

            slope, intercept, r_value, p_value, std_err = sp_stats.linregress(x_data, y_data)
            result["regression"] = {
                "slope": float(slope),
                "intercept": float(intercept),
                "r_squared": float(r_value ** 2),
                "p_value": float(p_value),
                "std_error": float(std_err),
                "equation": f"y = {slope:.6f}x + {intercept:.6f}",
                "interpretation": "Relazione significativa" if p_value < 0.05 else "Relazione NON significativa",
            }

        return json.dumps(result, ensure_ascii=False, indent=2)

    except json.JSONDecodeError:
        return json.dumps({"error": "Formato JSON non valido per i dati"}, indent=2)
    except Exception as e:
        return json.dumps({
            "error": f"Errore statistico: {str(e)}",
            "operation": operation,
        }, indent=2)


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


# HIGH #3: CORS origins — configurable via HERMES_MCP_CORS_ORIGINS env var (comma-separated).
# Default: localhost:* only. Setting to "[]" disables all CORS (same-origin only).
_CORS_RAW = os.environ.get("HERMES_MCP_CORS_ORIGINS", "").strip()
if _CORS_RAW.lower() == "[]":
    cors_origins_list: list[str] = []  # Disable entirely — same-origin only
elif _CORS_RAW:
    cors_origins_list = [o.strip() for o in _CORS_RAW.split(",") if o.strip()]
else:
    cors_origins_list = ["http://localhost:*", "https://localhost:*"]  # Default: localhost only


if HAS_FASTAPI:
    bridge_app = FastAPI(
        title="Hermes Web Search Bridge",
        version="1.5.0",
        lifespan=_bridge_lifespan,
    )

    bridge_app.add_middleware(
        CORSMiddleware,
        allow_origins=cors_origins_list,  # HIGH #3: Configurable via HERMES_MCP_CORS_ORIGINS
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["*"],
        allow_credentials=True,  # Required for cookies/auth when origins are restricted
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

    @rate_limited
    @bridge_app.get("/health")
    async def health():
        """Health check endpoint — minimal info, no config disclosure. Rate-limited."""
        return {"status": "ok", "version": "1.5.0"}

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
        """Deep search API endpoint — runs web search first, then LLM analysis. Rate-limited."""
        safe_query = _sanitize_for_llm(query.strip(), max_len=200) if isinstance(query, str) else str(query)
        # First: run actual web search to gather results (not just LLM hallucination)
        search_result = await _external_call(_search_web, safe_query)
        raw_content = "\n---\n".join([
            f"# {_sanitize_search_result(r['title'], 200)}\n{_sanitize_search_result(r.get('snippet', ''), 500)}"
            for r in search_result.get("results", [])
        ])
        # Then: LLM analysis of actual search results
        llm_prompt = (
            f'Sei un assistente AI. Analizza questi risultati per: {safe_query}\n\n'
            f"Risultati:\n{raw_content[:8000]}\n\n"
            "Fornisci una risposta completa in italiano con punti chiave, fonti e incertezze."
        )
        llm_answer = await _external_call(_summarize_with_llm, llm_prompt)
        return {
            "query": safe_query,
            "search_results_count": len(search_result.get("results", [])),
            "llm_analysis": llm_answer or "LLM summarization not available",
            "source_results": search_result.get("results", []),
        }


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
    except Exception:
        sys.stderr.write("Bridge: failed to start [hidden]\n")
        return None


async def main():
    # ── Start bridge server (if FastAPI available) ────────────────────────
    _bridge_task = await _start_http_bridge()

    print(f"🔮 Hermes MCP Server v1.5.0", file=sys.stderr)
    print(f"   Transport: {TRANSPORT}", file=sys.stderr)
    print(f"   LLM: {LLM_ENDPOINT}", file=sys.stderr)
    print(f"   Bridge: {HERMES_BRIDGE_URL}", file=sys.stderr)

    # SearXNG status check
    if SEARXNG_URL and HTTPX_AVAILABLE:
        try:
            with httpx.Client(timeout=5) as c:
                # CRITICAL #1b: No redirect following — prevents SSRF via metadata endpoint (169.254.169.254)
                r = c.get(SEARXNG_URL + "/search", params={"q": "test", "format": "json"}, follow_redirects=False)
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
                allow_origins=cors_origins_list,  # HIGH #3: Same list as bridge (configurable)
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
