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

Healthcheck: `GET /healthz -> {"status":"ok"}`. MCP endpoint: `/mcp`.

> **Remote hosting requires `MCP_ALLOWED_HOSTS`** when DNS-rebinding protection
> should be on: list the public hostname(s), e.g. `MCP_ALLOWED_HOSTS="mcp.bs.ch:*"`.
> When unset, protection is disabled so any `Host` header is accepted.

`compose.yml` pulls the published GHCR image: `docker compose up -d`.
CI and publishing use the DCC reusable workflows, same as mcp-data-bs.

## Connecting clients

- **ChatGPT / OpenWebUI**: add the hosted URL (e.g. `https://mcp.your-domain/mcp`), no auth.
- **opencode / local stdio**:
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

## Tools

### `search_indicators`
Ranked search over the indicator index (title > subtitle > subtopic > topic/set >
reading aid). German terms, umlauts may be written as `ae/oe/ue`. Filters:
`thema`, `unterthema`, `kennzahlenset`, `raeumliche_gliederung`.

```
search_indicators(search="Arbeitslosenquote")
search_indicators(search="Leerwohnungen", raeumliche_gliederung="Wohnviertel")
search_indicators(kennzahlenset="Legislaturplan", limit=50)
```

### `get_indicator`
Metadata of one indicator: reading aid (`lesehilfe`), explanations, sources,
external links, spatial units, last update.

```
get_indicator(indicator_id=10027)
```

### `get_indicator_data`
Time series as `headers` + `rows` (numbers parsed). Resolves the shared data file
(`data-id`) from the metadata. Long tables return the last `max_rows` rows.

```
get_indicator_data(indicator_id=7510, max_rows=24)
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
