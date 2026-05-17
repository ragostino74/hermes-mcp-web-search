# Hermes MCP Server

MCP (Model Context Protocol) server che espone strumenti di ricerca web alla tua AI.  
Permette a qualsiasi client MCP (llama.cpp WebUI, Claude Desktop, o altri) di cercare informazioni su internet usando la potenza del tuo LLM locale per la sintesi.

## Funzionalità

- **web_search** — Ricerca rapida via SearXNG (se configurato) o DuckDuckGo + sintesi con il tuo LLM
- **deep_search** — Ricerca profonda con analisi strutturata dell'LLM
- **read_webpage** — Legge e sintetizza pagine web

Note: lo strumento `get_current_datetime` è stato spostato nel server dedicato
[hermes-mcp-timedata](https://github.com/ragostino74/hermes-mcp-timedata).

## Requisiti

- Python 3.11+
- [`mcp[serve]`](https://pypi.org/project/mcp/) >= 1.26
- [`duckduckgo-search`](https://pypi.org/project/duckduckgo-search/)
- [`httpx`](https://pypi.org/project/httpx/) (opzionale, per bridge e fetch web)

## Installazione

```bash
# Crea un ambiente virtuale
python3 -m venv .venv
source .venv/bin/activate

# Installa le dipendenze
pip install mcp[serve] duckduckgo-search httpx aiolimiter

# Configura l'endpoint LLM (opzionale, usa default localhost:10000)
export LLM_ENDPOINT="http://localhost:10000/v1"
export LLM_MODEL="Qwen3.6-35B-A3B-Q8_0.gguf"

# Opzionale: configura SearXNG (se non impostato, usa DuckDuckGo)
export SEARXNG_URL="http://10.0.0.154:8888"

# Avvia in modalità stdio (per Claude Desktop, VS Code, ecc.)
python hermes_mcp_server.py

# Oppure in modalità HTTP/StreamableHTTP (per llama.cpp WebUI)
export HERMES_MCP_TRANSPORT=http
export HERMES_MCP_PORT=18760
python hermes_mcp_server.py
```

## Configurazione Environment Variables

| Variabile | Default | Descrizione |
|----------|---------|-------------|
| `LLM_ENDPOINT` | `http://localhost:10000/v1` | Endpoint del server LLM locale (OpenAI-compatible) |
| `LLM_MODEL` | `Qwen3.6-35B-A3B-Q8_0.gguf` | Nome del modello da usare per la sintesi |
| `SEARXNG_URL` | *(disabilitato)* | URL dell'istanza SearXNG. Se impostata, motore principale con fallback su DuckDuckGo. Es: `http://10.0.0.154:8888` |
| `HERMES_MCP_TRANSPORT` | `stdio` | Modalità di trasporto: `stdio`, `http`, o `dual` |
| `HERMES_MCP_PORT` | `18760` | Porta per la modalità HTTP/StreamableHTTP |
| `HERMES_MCP_BRIDGE_PORT` | `18761` | Porta per la Bridge REST API (integrazioni esterne) |
| `HERMES_MCP_RATE_LIMIT` | `5` | Max chiamate/minute per token bucket (rate limiting) |
| `HERMES_MCP_CONCURRENCY` | `3` | Max chiamate HTTP parallele (semaphore cap) |
| `HERMES_MCP_CORS_ORIGINS` | `http://localhost:*,https://localhost:*` | CORS origins, comma-separated. Imposta a `[]` per same-origin-only |
| `HERMES_MCP_BIND_ADDR` | `127.0.0.1` | Bind IP per il server MCP HTTP |
| `HERMES_BRIDGE_BIND_ADDR` | `127.0.0.1` | Bind IP per la bridge API REST |

## Integrazione con llama.cpp WebUI

1. Apri la WebUI in browser
2. Vai alla sezione **MCP Servers**
3. Aggiungi un nuovo server con:
   - **URL**: `http://localhost:18760/mcp` (o l'IP della tua macchina)
   - **Transport**: `streamable_http`
4. Il server dovrebbe connettersi e mostrare i 4 tools disponibili

## Integrazione con altri client MCP

Lo script supporta anche la modalità **stdio** per:
- **Claude Desktop** — aggiungi al config JSON
- **VS Code** — estensioni MCP
- Qualsiasi altro client che supporti il protocollo MCP via stdio

## Bridge REST API

Il bridge espone un'API REST su `http://localhost:<HERMES_MCP_BRIDGE_PORT>` per integrazioni esterne:

| Endpoint | Metodo | Descrizione |
|----------|--------|-------------|
| `/health` | GET | Health check — restituisce `{"status": "ok", "version": "1.5.2"}` (rate-limited) |
| `/api/search` | GET/POST | Web search con parametri `query` e `max_results` |
| `/api/deep-search` | GET/POST | Deep research: query + web search + analisi LLM strutturata |

Esempio di richiesta:
```bash
curl "http://localhost:18761/api/search?query=ultime+notizie+AI"
```

## Sicurezza

- **SSRF Protection**: blocco accesso a localhost, IP privati (RFC 1918), link-local, IPv6 ULA/link-local, e metadata cloud endpoints (169.254.169.254)
- **Redirect safety**: i redirect HTTP sono limitati a 3 salti con verifica `_is_safe_url()` su ogni hop (`follow_redirects=False` di default)
- **DNS Rebinding Protection**: attiva di default nel framework MCP
- **CORS configurabile**: origins limitate a localhost per default, estendibili via `HERMES_MCP_CORS_ORIGINS`
- **Rate Limiting**: token bucket (configurabile) + semaphore per prevenire saturazione risorse
- **Prompt Injection Sanitization**: pipeline a 3 fasi su tutto il testo esterno
- **Cache Poisoning Protection**: chiave di cache con salt casuale per-processo (anti-targeted eviction)

## Struttura del Progetto

```
hermes-mcp-server/
├── hermes_mcp_server.py    # Server principale (stdio + HTTP/StreamableHTTP)
├── README.md               # Documentazione e configurazione
├── SKILL.md                # Skill per Hermes Agent
├── LICENSE                 # Licenza MIT
├── .gitignore
└── requirements.txt        # Dipendenze Python
```

## Licenza

MIT License — vedi file [LICENSE](LICENSE).
