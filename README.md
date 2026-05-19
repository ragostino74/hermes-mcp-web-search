# Hermes MCP Server

MCP (Model Context Protocol) server che espone strumenti di ricerca web alla tua AI.  
Permette a qualsiasi client MCP (llama.cpp WebUI, Claude Desktop, o altri) di cercare informazioni su internet usando la potenza del tuo LLM locale per la sintesi.

## Funzionalità

- **web_search** — Ricerca rapida via SearXNG (se configurato) o DuckDuckGo + sintesi con il tuo LLM
- **deep_search** — Ricerca profonda con analisi strutturata dell'LLM
- **read_webpage** — Legge e sintetizza pagine web

## Requisiti

- Python 3.11+
- [`mcp[serve]`](https://pypi.org/project/mcp/) >= 1.26
- [`duckduckgo-search`](https://pypi.org/project/duckduckgo-search/)
- [`httpx`](https://pypi.org/project/httpx/) (opzionale, per fetch web)

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
export SEARXNG_URL="http://127.0.0.1:8888"

# Avvia in modalità stdio (per Claude Desktop, VS Code, ecc.)
python hermes_mcp_server.py

# Oppure in modalità HTTP/StreamableHTTP (per llama.cpp WebUI)
export HERMES_MCP_TRANSPORT=http
export HERMES_MCP_PORT=18760
python hermes_mcp_server.py
```

## Configurazione Variabili d'Ambiente

| Variabile | Default | Descrizione |
|----------|---------|-------------|
| `LLM_ENDPOINT` | `http://localhost:10000/v1` | Endpoint del server LLM locale (OpenAI-compatible) |
| `LLM_MODEL` | `Qwen3.6-35B-A3B-Q8_0.gguf` | Nome del modello da usare per la sintesi |
| `SEARXNG_URL` | *(disabilitato)* | URL di un'istanza SearXNG. Se impostata, motore principale con fallback su DuckDuckGo. Es: `http://127.0.0.1:8888` |
| `HERMES_MCP_TRANSPORT` | `stdio` | Modalità di trasporto: `stdio`, `http`, o `dual` |
| `HERMES_MCP_PORT` | `18760` | Porta per la modalità HTTP/StreamableHTTP |
| `HERMES_MCP_RATE_LIMIT` | `5` | Max chiamate/minuto per token bucket (rate limiting) |
| `HERMES_MCP_CONCURRENCY` | `3` | Max chiamate HTTP parallele (semaphore cap) |
| `HERMES_MCP_CORS_ORIGINS` | `http://localhost:*,https://localhost:*` | CORS origins, comma-separated. Imposta a `[]` per same-origin-only |
| `HERMES_MCP_BIND_ADDR` | `0.0.0.0` | Bind IP per il server MCP HTTP (default su tutte le interfacce) |

## Note sulla Sicurezza (v2.0.0)

La versione 2.0.0 introduce modifiche breaking alla sicurezza:

1. **SSRF guard estesa**: ora protegge anche `SEARXNG_URL` e `LLM_ENDPOINT` (prima solo `read_webpage`)
2. **DNS rebinding protection**: attivata di default nel framework MCP (rimossa la workaround v1.5.3)
3. **Bind su 0.0.0.0**: il server HTTP ascolta su tutte le interfacce; usa `HERMES_MCP_BIND_ADDR=127.0.0.1` per limitare a localhost
4. **CORS con credentials**: abilitato per supporto autenticazione cross-origin (richiede origins esplicite, no wildcard)

## Integrazione con llama.cpp WebUI

1. Apri la WebUI nel browser
2. Vai alla sezione **MCP Servers**
3. Aggiungi un nuovo server con:
   - **URL**: `http://localhost:18760/mcp` (o l'IP della tua macchina)
   - **Transport**: `streamable_http`
4. Il server dovrebbe connettersi e mostrare i 3 tools disponibili

## Integrazione con altri client MCP

Lo script supporta anche la modalità **stdio** per:
- **Claude Desktop** — aggiungi al config JSON
- **VS Code** — estensioni MCP
- Qualsiasi altro client che supporti il protocollo MCP via stdio

## Struttura del Progetto

```
hermes-mcp-server/
├── hermes_mcp_server.py    # Server principale (stdio + HTTP/StreamableHTTP)
├── README.md               # Documentazione e configurazione
├── LICENSE                 # Licenza MIT
├── .gitignore
└── requirements.txt        # Dipendenze Python
```

## Changelog

### v2.0.0
- 🔒 SSRF guard estesa a SearXNG e LLM_ENDPOINT
- 🔒 DNS rebinding protection riattivata di default
- 🌐 Bind default: 127.0.0.1 → 0.0.0.0 (accessibile da rete esterna)
- 🔧 CORS: aggiunta `allow_credentials=True`
- 🛡️ deep_search: query sanitizzata prima di iniezione nel prompt LLM
- 🧹 Rimosso `TransportSecuritySettings(enable_dns_rebinding_protection=False)`

## Licenza

MIT License — vedi file [LICENSE](LICENSE).
