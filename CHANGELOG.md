# Changelog

Tutti i cambiamenti degni di nota in questo progetto saranno documentati in questo file.

Il formato è basato su [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
e questo progetto aderisce a [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.5.0] - 2026-05-17
### Added
- **Scientific Computing Suite**: 8 nuovi strumenti di calcolo scientifico:
  - `solve_equation` — Risoluzione equazioni algebriche (lineari, quadratiche, sistemi) con SymPy
  - `differentiate` — Calcolo derivate prime, seconde e parziali con SymPy
  - `integrate` — Integrali definiti e non definiti con SymPy
  - `limit_func` — Calcolo limiti di funzioni con SymPy
  - `simplify_expr` — Semplificazione espressioni simboliche con SymPy
  - `numerical_calculate` — Calcoli numerici complessi (aritmetica, trigonometria, logaritmi, regressione lineare, probabilità normale/Poisson) con NumPy/SciPy
  - `matrix_operations` — Operazioni matriciali complete (det, autovalori, inversa, SVD, trasposta, rango, traccia, norme) con NumPy/SciPy
  - `statistics` — Statistica descrittiva completa, correlazione, test di normalità (Shapiro-Wilk), regressione lineare, rilevamento anomalie (z-score) con NumPy/SciPy
- **Dipendenze scientifiche**: Aggiunti sympy, numpy, scipy a requirements.txt

## [1.4.0] - 2026-05-16 (Stabile)
### Fixed
- **DNS Rebinding Protection**: Ripristinata la protezione nativa del framework MCP (`enable_dns_rebinding_protection=True`).
- **SSRF via Redirect**: Implementato loop manuale di redirect verificati su ogni hop con `_is_safe_url()`, disabilitando `follow_redirects` automatico per prevenire bypass.

### Changed
- **Audit Logging**: Aggiunto logging strutturato per le richieste API bridge (metodo, percorso, codice stato).
- **Bind Address Default**: I server Bridge e MCP ora sono di default su `127.0.0.1` (localhost) invece che `0.0.0.0`.

## [1.3.0] - 2026-05-16
### Changed
- **Deep Search**: L'API `deep_search` ora utilizza risultati web intermedi per guidare il LLM, migliorando accuratezza e riducendo allucinazioni.
- **Anti-Cache Poisoning**: Aggiunta salatura casuale alle chiavi di cache per prevenire attacchi di evizione mirata.

### Fixed
- **Rate Limit su Health Endpoint**: L'endpoint `/health` è ora protetto da abusivi.

## [1.2.0] - 2026-05-16
### Changed
- **IDN Homograph Protection**: Bloccati hostname Punycode (`xn--`) e caratteri non-ASCII in `_is_safe_url()` per prevenire spoofing IDN.

### Fixed
- **Config Leaks**: `GET /health` non espone più config interne, limiti di rate o statistiche token.
- **Error Leak**: I traceback Python sono nascosti dalle risposte HTTP (restano solo messaggi generici).
- **Cache Key Hash**: Upgrade da MD5 a SHA-256 per resistenza alle collisioni nelle chiavi di cache.

## [1.1.1] - 2026-05-16
### Fixed
- **Prompt Injection**: Sanificazione migliorata su tutti i punti di input (`web_search`, `read_webpage`).

## [1.1.0] - 2026-05-16
### Added
- **Audit Logging**: Log strutturato delle richieste API bridge (metodo, path, status, duration).
- **Bind Localhost**: Default di binding sui server MCP e Bridge cambiato a `127.0.0.1`.

## [1.0.1] - 2026-05-16
### Fixed
- **SSRF Guard (IPv6)**: Aggiunti controlli per range IPv6 privati e indirizzi IPv4-mapped in `_is_safe_url()`.

## [1.0.0] - 2026-05-16
### Added
- **Bridge HTTP API**: Esposizione REST degli strumenti (search, read) con supporto CORS.
- **MCP Crash Handling**: Logica di riconezione resiliente per il protocollo MCP.
- **Rate Limiter**: Implementazione rate limiting globale e per-endpoint.

## [0.5.1] - 2026-05-16
### Fixed
- **Deadlock Prevention**: Risolti race condition negli endpoint bridge sotto alto carico.
- **Rate Limit Integration**: Applicato il rate limiting agli endpoint HTTP del bridge.

## [0.5.0] - 2026-05-16
### Changed
- Rimosso strumento `hermes_search` (non utilizzato/disconfigurato) per semplificare l'interfaccia.

## [0.4.0] - 2026-05-16
### Added
- **SearXNG Integration**: Integrazione di SearXNG come motore di ricerca fallback per risultati più completi.

## [0.3.0] - 2026-05-16
### Added
- **DateTime Tool**: Nuovo strumento `get_current_datetime` con supporto fuso orario Italia (Europe/Rome).

## [0.2.1] - 2026-05-16
### Fixed
- **Decorator Order**: Corretto lo stack dei decorator (`@rate_limited`) per garantire l'applicazione corretta su tutti gli strumenti MCP.

## [0.2.0] - 2026-05-16
### Added
- **SSRF Protection**: Validazione URL iniziale contro range privati e loopback.
- **LRU Cache**: Implementata cache LRU (max 100 item) per risultati di ricerca e performance.

## [0.1.1] - 2026-05-16
### Fixed
- **Bridge URL Config**: Corretto porta bridge hardcodata; ora configurabile via `HERMES_BRIDGE_URL`.

## [0.1.0] - 2026-05-16
### Added
- Release iniziale di Hermes MCP Web Search Tool (integrazione DuckDuckGo).

[Unreleased]: https://github.com/ragostino74/hermes-mcp-server/compare/v1.5.0...HEAD
[1.4.0]: https://github.com/ragostino74/hermes-mcp-server/compare/v1.3.0...v1.4.0
[1.3.0]: https://github.com/ragostino74/hermes-mcp-server/compare/v1.2.0...v1.3.0
[1.2.0]: https://github.com/ragostino74/hermes-mcp-server/compare/v1.1.1...v1.2.0
[1.1.1]: https://github.com/ragostino74/hermes-mcp-server/compare/v1.1.0...v1.1.1
[1.1.0]: https://github.com/ragostino74/hermes-mcp-server/compare/v1.0.1...v1.1.0
[1.0.1]: https://github.com/ragostino74/hermes-mcp-server/compare/v1.0.0...v1.0.1
[1.0.0]: https://github.com/ragostino74/hermes-mcp-server/compare/v0.5.1...v1.0.0
[0.5.1]: https://github.com/ragostino74/hermes-mcp-server/compare/v0.5.0...v0.5.1
[0.5.0]: https://github.com/ragostino74/hermes-mcp-server/compare/v0.4.0...v0.5.0
[0.4.0]: https://github.com/ragostino74/hermes-mcp-server/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/ragostino74/hermes-mcp-server/compare/v0.2.1...v0.3.0
[0.2.1]: https://github.com/ragostino74/hermes-mcp-server/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/ragostino74/hermes-mcp-server/compare/v0.1.1...v0.2.0
[0.1.1]: https://github.com/ragostino74/hermes-mcp-server/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/ragostino74/hermes-mcp-server/tree/v0.1.0
