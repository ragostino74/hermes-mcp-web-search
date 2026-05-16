# Changelog

Tutte le modifiche importanti a questo progetto saranno documentate in questo file.

Il formato è basato su [Keep a Changelog](https://keepachangelog.com/it/1.1.0/),
e il versionamento segue [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [v2.2.0] — 2026-05-16

### 🛡️ Sicurezza
- **Prompt injection hardening completo** — pipeline di sanitizzazione a 3 fasi su TUTTO il testo esterno:
  - Fase 1: Strip caratteri zero-width (U+200B, BOM, ZWJ) + conversione fullwidth → ASCII
  - Fase 2: Neutralizzazione role markers (`SYSTEM:`, `ASSISTANT:`, `系统指令`, `you are...`)
  - Fase 3: Code fences → `[CODE_BLOCK]`, HTML comments → `[HTML_COMMENT]`
- Copertura completa: tutti gli input utente e output LLM (web_search, deep_search, read_webpage, bridge API)
- 19/19 test di sicurezza superati, 0 falsi positivi

### 🐛 Fix
- Allineamento decoratore `@rate_limited` come outermost su tutte le MCP tools
- Rate-limit applicato anche ai bridge endpoints (`/api/search`, `/api/deep-search`)
- Deadlock prevention via thread-local gating sul rate limiter

---

## [v2.1.0] — 2026-05-16

### ✨ Features
- Integrazione **SearXNG** come motore di ricerca alternativo a DuckDuckGo
- Rimosso tool `hermes_search` (non utilizzato, bridge non configurato) — ora 4 tools invece di 5

### 🐛 Fix
- README: rimossi riferimenti a hermes_search e configurazione bridge
- README: aggiornato numero tools da 5 a 4 nelle istruzioni llama.cpp WebUI

---

## [v2.0.0] — 2026-05-16

> **MAJOR RELEASE** — Nuova architettura con rate limiting, API bridge e gestione errori migliorata.

### ⚡ Breaking Changes
- Bridge REST API su porta dedicata `:18761` (prima portava collisione)
- Rate limiter attivo di default (configurabile via env var)

### ✨ Features
- **Token bucket + semaphore rate limiter** — configurabile via variabili d'ambiente:
  - `RATE_LIMIT_MAX_TOKENS` (default: 20)
  - `RATE_LIMIT_REFILL_RATE` (default: 1 token/sec)
  - `RATE_LIMIT_CONCURRENCY` (default: 5)
- **Bridge REST API** su `http://localhost:18761`:
  - `GET /health` — health check
  - `POST /api/search` — web search via LLM
  - `POST /api/deep-search` — deep research
- **CORS middleware** per browser-based WebUI
- **Graceful shutdown** con `asyncio.Event` al posto di `sys.exit()`
- **MCP crash resilience**: errori HTTP MCP non uccidono il processo bridge

### 🐛 Fix
- Risolto collisione porte tra MCP server e bridge (prima entrambi su :18760)
- `HERMES_BRIDGE_URL` ora solo via env var, valore di default rimosso

---

## [v1.2.0] — 2026-05-16

### ✨ Features
- Nuovo tool `get_current_datetime()` con timezone italiana (`Europe/Rome`)

---

## [v1.1.1] — 2026-05-16

### 🛡️ Sicurezza
- SSRF guard esteso: blocchi IPv6 ULA (`fc00::/7`), link-local (`fe80::/10`), site-local (`fec0::/10`)
- Rilevamento e blocco indirizzi IPv4-mapped in IPv6 (`::ffff:192.168.x.x`)
- Aggiunto `_sanitize_for_llm()`: troncamento input oversize, neutralizzazione markers prompt injection

### 🐛 Fix
- Lettura tag HTML `<title>` safe da XSS (output JSON sicuro)

---

## [v1.1.0] — 2026-05-16

### ✨ Features
- **SSRF protection** in `read_webpage()`: blocco localhost, IP privati RFC 1918, link-local (`169.254.x.x`), metadata endpoints
- **LRU cache** (max 100 entry + TTL) al posto di cache non limitata

### Documentazione
- Documentata attivazione bridge su porta alternativa nel README

---

## [v1.0.0] — 2026-05-15

> **Prima release stabile.**

### ✨ Features
- Web search tools tramite DuckDuckGo + sintesi LLM
- StreamableHTTP transport support (CORS-enabled)
- README, LICENSE MIT, requirements.txt

---

## [v2.3.0] — 2026-05-16

### 🛡️ Sicurezza (CRITICAL + HIGH)

- **Fix SSRF via redirect** (CVSS 9.8): disabilitato `follow_redirects=True` in `read_webpage()` e health check SearXNG, sostituito con loop manuale max 3 redirect verificati da `_is_safe_url()` su ogni hop
- **DNS Rebinding Protection**: riattivata (`enable_dns_rebinding_protection=True`) nel TransportSecuritySettings di FastMCP
- **CORS hardening** (CVSS 7.5): wildcard `"*"` sostituita con origins configurabili via `HERMES_MCP_CORS_ORIGINS` (default: localhost:*) per bridge e MCP HTTP server

### ⚡ Performance

- **Timeout granulare** (MEDIUM): deep search timeout cambiato da `180s` flat a `httpx.Timeout(connect=30, read=60, write=30, pool=5)` — previene thread-pool saturation DoS

---

[Unreleased]: https://github.com/ragostino74/hermes-mcp-server/compare/v2.3.0...HEAD
[v2.2.0]: https://github.com/ragostino74/hermes-mcp-server/compare/v2.1.2...v2.2.0
[v2.1.2]: https://github.com/ragostino74/hermes-mcp-server/compare/v2.1.1...v2.1.2
[v2.1.1]: https://github.com/ragostino74/hermes-mcp-server/compare/v2.1.0...v2.1.1
[v2.1.0]: https://github.com/ragostino74/hermes-mcp-server/compare/v2.0.0...v2.1.0
[v2.0.0]: https://github.com/ragostino74/hermes-mcp-server/compare/v1.2.0...v2.0.0
[v1.2.0]: https://github.com/ragostino74/hermes-mcp-server/compare/v1.1.1...v1.2.0
[v1.1.1]: https://github.com/ragostino74/hermes-mcp-server/compare/v1.1.0...v1.1.1
[v1.1.0]: https://github.com/ragostino74/hermes-mcp-server/compare/v1.0.0...v1.1.0
[v2.3.0]: https://github.com/ragostino74/hermes-mcp-server/compare/v2.2.0...v2.3.0
[v1.0.0]: https://github.com/ragostino74/hermes-mcp-server/releases/tag/v1.0.0
