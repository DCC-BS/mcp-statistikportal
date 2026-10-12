# statistik-bs-mcp

MCP server for the statistics portal of the canton of Basel-Stadt
([statistik.bs.ch](https://statistik.bs.ch)). It gives chatbots access to the
~1'000 indicators of the Indikatorenportal: search, metadata (reading aid,
explanations, sources) and the underlying time series.

It uses the public JSON endpoints of the statistikportal Nuxt app (see
[`public/llms.txt`](https://statistik.bs.ch/llms.txt)). No authentication.

It runs in two modes:

- **stdio** — for local MCP clients (opencode, Cursor, Claude Desktop).
- **streamable HTTP** — hosted as a container, so it can be wired into ChatGPT
  connectors and OpenWebUI. Also ships a **skill** as a no-server alternative.

Sister project: [mcp-data-bs](https://github.com/DCC-BS/mcp-data-bs) (data.bs.ch).

## Installation

The toolchain (uv only; Python version comes from `pyproject.toml`) and the task
runner are managed by [mise](https://mise.jdx.dev/). Enter the project and trust
the config once:

```bash
mise trust
mise install            # provisions uv, runs the postinstall hook
```

Then set up the virtual environment and dependencies:

```bash
mise run install        # alias: i — uv sync --locked
```

## Tasks

| Task                 | Alias | Description                                       |
|----------------------|-------|---------------------------------------------------|
| `mise run install`   | `i`   | Create venv and install deps (`uv sync --locked`) |
| `mise run dev`       | `d`   | MCP server over stdio (local clients)             |
| `mise run dev:http`  | `dh`  | Streamable HTTP on `:8000` for local testing      |
| `mise run check`     | `c`   | Verify lockfile, format, and lint (ruff)          |
| `mise run test:unit` | `t`   | Run the unit test suite (pytest)                  |

## Configuration

Read from the environment first, else a local `.env` next to `main.py`
(`cp .env.example .env`). All settings are optional.

| Variable                  | Default           | Meaning                                              |
|---------------------------|-------------------|------------------------------------------------------|
| `STATISTIK_PORTAL_DOMAIN` | `statistik.bs.ch` | Portal serving `/api/indikator-*` (e.g. a test host) |
| `INDEX_CACHE_TTL`         | `3600`            | Seconds the indicator index is kept in memory        |
| `MEILISEARCH_URL`         | –                 | Enables `search_portal` (together with the key)      |
| `MEILISEARCH_KEY`         | –                 | **Search-only** key for the `product` index          |
| `MCP_ALLOWED_HOSTS`       | –                 | Allowed `Host` headers, e.g. `mcp.bs.ch:*`           |

## Hosting (streamable HTTP)

```bash
docker build -f Dockerfile -t mcp-statistikportal .
docker run --rm -p 8000:8000 mcp-statistikportal
```

The MCP endpoint is mounted at `/mcp` (streamable HTTP).

Healthcheck: `GET /healthz -> {"status":"ok"}`.

### API reference

The HTTP server exposes an OpenAPI description of the MCP endpoint with a
rendered Swagger UI:

- `GET /docs` — interactive reference (Swagger UI). Each tool appears as a
  `tools/call` request schema with a working example you can execute against
  the server via *Try it out*.
- `GET /openapi.json` — the raw OpenAPI 3.1 spec.

The spec is generated from the live MCP tool registry at request time, so new
tools appear automatically (the optional `search_portal` shows up only when
Meilisearch is configured). It covers all endpoints (`POST /mcp`,
`GET /healthz`, `GET /`). Responses are `text/event-stream`; each `data:`
frame carries a JSON-RPC message. The server is stateless — requests need no
`initialize` handshake and no session id.

Client compatibility: POST requests with incomplete `Accept` headers (e.g. only
`application/json` or `*/*`) are rewritten to
`application/json, text/event-stream` by a middleware before the transport
validates them, so plain JSON-RPC clients work unmodified. `GET /` returns a
JSON status page (server, transport, endpoint links) instead of holding an SSE
connection open — clients expecting a traditional SSE handshake get an
immediate, meaningful response.

> **Remote hosting requires `MCP_ALLOWED_HOSTS`.** The MCP HTTP endpoint has
> DNS-rebinding protection on by default and, without config, accepts only
> localhost `Host` headers — a remote deploy would get `421 Invalid Host header`.
> When the server is reachable via a public hostname, list it (comma-separated,
> port-wildcard allowed): `MCP_ALLOWED_HOSTS="mcp.bs.ch:*"`. When unset,
> protection is disabled so any `Host` header is accepted.

`compose.yml` pulls the published GHCR image: `docker compose up -d`.
CI and publishing use the DCC reusable workflows, same as mcp-data-bs.

## Connecting clients

The snippets below are self-contained: stdio clients launch the server from
GitHub via `uvx` (needs [uv](https://docs.astral.sh/uv/) installed), HTTP
clients point at the hosted deployment.

**Claude Desktop / Cursor** — add to `claude_desktop_config.json`
(Claude Desktop → Settings → Developer → Edit Config) or `~/.cursor/mcp.json`:

```json
{
  "mcpServers": {
    "statistik-bs": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/DCC-BS/mcp-statistikportal", "statistik-bs-mcp"]
    }
  }
}
```

**OpenWebUI / ChatGPT** — point at the streamable HTTP endpoint
`https://<hosted-domain>/mcp` (no auth):

- OpenWebUI: Settings → Tools → add `https://<hosted-domain>/mcp`
- ChatGPT (developer-mode connector): Settings → Connectors → Create → add
  `https://<hosted-domain>/mcp`; the interactive API reference lives at
  `https://<hosted-domain>/docs`.

### Other clients

- **opencode**: add to OpenCode config:
  ```json
  {
    "mcpServers": {
      "statistik-bs": {
        "command": "uv",
        "args": ["--directory", "/ABSOLUTE/PATH/TO/mcp-statistikportal", "run", "main.py"]
      }
    }
  }
  ```
- **uvx** (anywhere): all settings are optional; defaults work out of the box.
  ```bash
  uvx --from git+https://github.com/DCC-BS/mcp-statistikportal statistik-bs-mcp
  ```

## Tools

Tool descriptions are kept to 1-3 sentences on purpose (they cost tokens on every
call of a small model). Every result that can be cited carries `title`,
`portal_url` and `aktualisierungsdatum` (Stand).

### `search_indicators`
Ranked search over the indicator index (title > subtitle > subtopic > topic/set >
reading aid). **Use German terms**; umlauts may be written `ä`, `ae` or `a`, and
plural/case endings and compounds are tolerated (`Leerwohnungen` finds
`Leerwohnungsquote`). Ties go to an exact title, then the shorter title, then the
newer update. Filters: `thema`, `unterthema`, `kennzahlenset`,
`raeumliche_gliederung`.

Each hit has `id`, `title`, `subtitle`, `unit`, `aktualisierungsdatum`, `portal_url`.
`match` is `all_terms`, `partial` (only some terms matched; hits list
`matched_terms` and a `hint` explains) or `none`.

```
search_indicators(search="Arbeitslosenquote")
search_indicators(search="Leerwohnungen", raeumliche_gliederung="Wohnviertel")
search_indicators(kennzahlenset="Legislaturplan", limit=50)
```

**Limitation:** the index holds the ~1,080 indicators shown in the Indikatorenportal,
not all ~2,500 indicator files. The others (e.g. 10027) are only reachable by id
via `get_indicator` / `get_indicator_data`; the portal offers no list of them
(no sitemap entries, `children` ids are print variants). Use the optional
`search_portal` for content outside the index.

### `get_indicator`
Metadata of one indicator: `unit`, reading aid (`lesehilfe`), explanations,
sources (`quellenangabe`), external links, spatial units, last update. HTML is
converted to plain text.

```
get_indicator(indicator_id=5813)
```

### `get_indicator_data`
Values as `headers` + `rows` (numbers parsed; the portal delivers strings). Besides
the table the result has `unit` (derived from `subtitle`, e.g. "in %" -> `%`,
"Anzahl Personen" -> `Personen`, `null` if unclear), `subtitle`, `source`,
`aktualisierungsdatum`, `portal_url`, `title`, and `notes` (erlaeuterungen +
lesehilfe, at most 600 characters, so definitions and series breaks reach the
model). If the table has a year/date column, `latest` = `{period, values}` holds the
newest period that has numbers.

The resolved shared data file (`data-id`) is used. Output is capped at about
12,000 characters: the newest rows are kept, `truncated: true` and a `hint` say how
to get more (`max_rows`, default 200, max 5000; the character cap still applies).
`indicator_id` must be a positive integer (a string of digits is accepted);
anything else gives a `ToolError` that points to `search_indicators`.

```
get_indicator_data(indicator_id=5813, max_rows=24)
```

### `get_facets`
Values with counts for `thema`, `unterthema`, `kennzahlenset`,
`raeumliche_gliederung`, `darstellungsart`.

### `search_portal` (optional)
Only registered when `MEILISEARCH_URL` and `MEILISEARCH_KEY` are set. Full-text
search over all portal content (articles, web tables, maps, dashboards, OGD
datasets, indicators) via the portal's Meilisearch `product` index.

## Skills (no MCP needed)

See `skills/statistik-bs/SKILL.md` — teaches an agent to call the public
endpoints directly with plain HTTP.

## Debug

```bash
npx @modelcontextprotocol/inspector uv run main.py
```

## Known API gaps

See [docs/api-analyse.md](docs/api-analyse.md).
