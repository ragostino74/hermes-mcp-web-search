# Hermes MCP Server

MCP (Model Context Protocol) server che espone strumenti di ricerca web alla tua AI.  
Permette a qualsiasi client MCP (llama.cpp WebUI, Claude Desktop, o altri) di cercare informazioni su internet usando la potenza del tuo LLM locale per la sintesi.

## Funzionalità

- **web_search** — Ricerca rapida via SearXNG (se configurato) o DuckDuckGo + sintesi con il tuo LLM
- **deep_search** — Ricerca profonda con analisi strutturata dell'LLM
- **read_webpage** — Legge e sintetizza pagine web
- **get_current_datetime** — Data e ora attuale in italiano (fuso Europe/Rome)

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

| Variable | Default | Descrizione |
|----------|---------|-------------|
| `LLM_ENDPOINT` | `http://localhost:10000/v1` | Endpoint del server LLM locale |
| `LLM_MODEL` | `Qwen3.6-35B-A3B-Q8_0.gguf` | Nome del modello da usare per la sintesi |
| `HERMES_MCP_TRANSPORT` | `stdio` | Modalità di trasporto: `stdio`, `http`, o `dual` |
| `HERMES_MCP_PORT` | `18760` | Porta per la modalità HTTP |
|| `SEARXNG_URL` | *(disabilitato)* | URL dell'istanza SearXNG. Se impostata, viene usata come motore di ricerca principale con fallback automatico su DuckDuckGo. Esempio: `http://10.0.0.154:8888` |

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

## Struttura del Progetto

```
hermes-mcp-web-search/
├── hermes_mcp_server.py    # Server principale (stdio + HTTP)
├── README.md
├── LICENSE
├── .gitignore
└── requirements.txt
```

## Licenza

MIT License — vedi file [LICENSE](LICENSE).
