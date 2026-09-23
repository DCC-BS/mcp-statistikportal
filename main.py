import asyncio
import html
import json
import os
import re
import time
from pathlib import Path

import httpx
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.responses import JSONResponse
from starlette.routing import Route


def _read_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            values[key.strip()] = value.strip().strip('"').strip("'")
    return values


_ENV_FILE = _read_env_file(Path(__file__).parent / ".env")


# Configuration comes from the environment first (container, compose, systemd)
# and falls back to a local .env next to this module, so the same checkout
# works locally and in a container.
def _env(name: str, default: str = "") -> str:
    value = os.environ.get(name, "").strip()
    if value:
        return value
    return _ENV_FILE.get(name, "").strip() or default


DOMAIN = (
    _env("STATISTIK_PORTAL_DOMAIN", "statistik.bs.ch")
    .removeprefix("https://")
    .removeprefix("http://")
    .strip("/")
)
BASE_URL = f"https://{DOMAIN}"
INDEX_PATH = "/indikatoren/metadata/portal/indikatoren.json"
INDEX_CACHE_TTL = int(_env("INDEX_CACHE_TTL", "3600"))

# Optional full-text search over all portal products (articles, tables, OGD
# datasets, ...) via the portal's Meilisearch. Only enabled when both are set;
# use a search-only key.
MEILISEARCH_URL = _env("MEILISEARCH_URL").rstrip("/")
MEILISEARCH_KEY = _env("MEILISEARCH_KEY")


def _transport_security() -> TransportSecuritySettings:
    """DNS-rebinding protection for the HTTP endpoint. FastMCP auto-enables
    Host-header validation limited to localhost when no host is given, which
    would reject real deployment hosts with 421. Configure the allowed hosts
    via MCP_ALLOWED_HOSTS (comma-separated, e.g. "mcp.bs.ch:*"); when unset,
    protection is disabled so the server is reachable on any Host header."""
    allowed = [h.strip() for h in _env("MCP_ALLOWED_HOSTS").split(",") if h.strip()]
    if not allowed:
        return TransportSecuritySettings(enable_dns_rebinding_protection=False)
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=allowed,
    )


mcp = FastMCP(
    DOMAIN,
    instructions=(
        "Statistics of the canton of Basel-Stadt (Statistisches Amt Basel-Stadt). "
        "Find indicators with search_indicators (German terms work best), read their "
        "explanation with get_indicator, then load the time series with "
        "get_indicator_data. Always cite the source (quellenangabe) and link portal_url."
    ),
    stateless_http=True,
    streamable_http_path="/mcp",
    transport_security=_transport_security(),
)


async def fetch(endpoint: str, params: dict[str, str | int] | None = None) -> dict | list:
    async with httpx.AsyncClient(base_url=BASE_URL, timeout=30.0) as client:
        response = await client.get(endpoint, params=params)
        response.raise_for_status()
        # Some source files carry a UTF-8 BOM, which json.loads rejects.
        return json.loads(response.content.decode("utf-8-sig"))


# --- Indicator index -------------------------------------------------------

_index_cache: dict = {"data": None, "loaded_at": 0.0}
_index_lock = asyncio.Lock()


async def _load_index() -> list[dict]:
    """The portal index (~2 MB) holds full metadata of every indicator shown in
    the Indikatorenportal. It changes at most daily, so keep it in memory."""
    async with _index_lock:
        fresh = time.monotonic() - _index_cache["loaded_at"] < INDEX_CACHE_TTL
        if _index_cache["data"] is None or not fresh:
            _index_cache["data"] = await fetch(INDEX_PATH)
            _index_cache["loaded_at"] = time.monotonic()
        return _index_cache["data"]


_TAG_RE = re.compile(r"<[^>]+>")
_BR_RE = re.compile(r"<br\s*/?>", re.IGNORECASE)
_LINK_RE = re.compile(r"""<a\s[^>]*href\s*=\s*['"]([^'"]+)['"][^>]*>(.*?)</a>""", re.IGNORECASE)


def _clean_html(value) -> str:
    if not value:
        return ""
    text = _BR_RE.sub("\n", str(value))
    return html.unescape(_TAG_RE.sub("", text)).strip()


def _parse_links(values) -> list[dict]:
    links = []
    for value in values or []:
        match = _LINK_RE.search(str(value))
        if match:
            links.append({"title": _clean_html(match.group(2)), "url": match.group(1)})
    return links


def _portal_url(indicator_id) -> str:
    return f"{BASE_URL}/indikatorenportal/{indicator_id}"


def _simplify_indicator(data: dict) -> dict:
    return {
        "id": data.get("id"),
        "title": data.get("title"),
        "subtitle": data.get("subtitle"),
        "thema": data.get("thema"),
        "unterthema": data.get("unterthema"),
        "kennzahlenset": data.get("kennzahlenset"),
        "raeumliche_gliederung": data.get("raeumlicheGliederung") or [],
        "darstellungsart": data.get("darstellungsart"),
        "aktualisierungsdatum": data.get("aktualisierungsdatum"),
        "portal_url": _portal_url(data.get("id")),
    }


_UMLAUTS = str.maketrans({"ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss"})

# Field weights for ranking search hits; a term found in the title counts most.
_SEARCH_FIELDS = (
    ("title", 5.0),
    ("subtitle", 3.0),
    ("unterthema", 2.0),
    ("thema", 1.5),
    ("kennzahlenset", 1.5),
    ("lesehilfe", 1.0),
    ("erlaeuterungen", 0.5),
)


def _normalize(value) -> str:
    return " ".join(str(value or "").casefold().translate(_UMLAUTS).split())


def _score(indicator: dict, terms: list[str]) -> tuple[int, float]:
    """(matched term count, weighted score). Substring matching keeps German
    compounds findable, e.g. "arbeitslos" hits "Arbeitslosenquote"."""
    fields = [(_normalize(indicator.get(name)), weight) for name, weight in _SEARCH_FIELDS]
    matched, score = 0, 0.0
    for term in terms:
        best = max((weight for text, weight in fields if term in text), default=0.0)
        if best:
            matched += 1
            score += best
    return matched, score


def _matches_filter(value, wanted: str | None) -> bool:
    if not wanted:
        return True
    wanted_norm = _normalize(wanted)
    values = value if isinstance(value, list) else [value]
    return any(wanted_norm in _normalize(v) for v in values)


@mcp.tool(
    title="Search Indicators",
    description=(
        f"Search the statistical indicators of the canton of Basel-Stadt ({DOMAIN}). "
        "Matches German search terms in title, subtitle, topic, indicator set and "
        "explanation, ranked by relevance. Combine with filters (see get_facets for "
        "valid values). Returns indicator ids for get_indicator / get_indicator_data."
    ),
)
async def search_indicators(
    search: str | None = None,
    thema: str | None = None,
    unterthema: str | None = None,
    kennzahlenset: str | None = None,
    raeumliche_gliederung: str | None = None,
    limit: int = 10,
    offset: int = 0,
) -> dict:
    """
    Search indicators in the Indikatorenportal.

    Args:
        search: German search terms (e.g. "Arbeitslosenquote", "Leerwohnungen Wohnviertel").
            All terms should match; if none match all terms, the best partial matches
            are returned. Leave empty to list by filters only.
        thema: Filter by topic, substring match (e.g. "Bevölkerung", "03")
        unterthema: Filter by subtopic, substring match (e.g. "Arbeitslose")
        kennzahlenset: Filter by indicator set (e.g. "Legislaturplan", "Städtevergleich")
        raeumliche_gliederung: Filter by spatial unit (e.g. "Wohnviertel", "Gemeinde", "Kanton")
        limit: Number of items to return (default: 10, max: 100)
        offset: Index of first item to return (default: 0)

    Returns:
        Dictionary with total_count, match ("all_terms" or "partial") and results.
    """
    index = await _load_index()
    candidates = [
        ind
        for ind in index
        if _matches_filter(ind.get("thema"), thema)
        and _matches_filter(ind.get("unterthema"), unterthema)
        and _matches_filter(ind.get("kennzahlenset"), kennzahlenset)
        and _matches_filter(ind.get("raeumlicheGliederung"), raeumliche_gliederung)
    ]

    terms = _normalize(search).split()
    match = "all_terms"
    if terms:
        scored = [(ind, *_score(ind, terms)) for ind in candidates]
        hits = [(ind, s) for ind, m, s in scored if m == len(terms)]
        if not hits:
            match = "partial"
            hits = [(ind, m * 10 + s) for ind, m, s in scored if m]
        hits.sort(key=lambda hit: hit[1], reverse=True)
        candidates = [ind for ind, _ in hits]
    else:
        candidates.sort(key=lambda ind: str(ind.get("aktualisierungsdatum") or ""), reverse=True)

    limit = max(1, min(limit, 100))
    return {
        "total_count": len(candidates),
        "match": match,
        "results": [_simplify_indicator(i) for i in candidates[offset : offset + limit]],
    }


@mcp.tool(
    title="Get Indicator Metadata",
    description=(
        "Get the full description of one indicator: title, reading aid (lesehilfe), "
        "explanations, sources, spatial units, topic and last update. Read this before "
        "interpreting the data from get_indicator_data."
    ),
)
async def get_indicator(indicator_id: int) -> dict:
    """
    Get detailed metadata for an indicator.

    Args:
        indicator_id: The indicator id (Kennzahl, e.g. 10027)

    Returns:
        Indicator metadata including lesehilfe, erlaeuterungen, quellenangabe and links.
    """
    data = await fetch(f"/api/indikator-meta/{int(indicator_id)}")
    return {
        **_simplify_indicator(data),
        "lesehilfe": _clean_html(data.get("lesehilfe")),
        "erlaeuterungen": _clean_html(data.get("erlaeuterungen")),
        "quellenangabe": [_clean_html(q) for q in data.get("quellenangabe") or []],
        "external_links": _parse_links(data.get("externalLinks")),
        "visible_in_portal": data.get("visibleInPortal"),
        "data_id": data.get("data-id") or data.get("id"),
        "data_filter": data.get("filter") or None,
    }


def _to_number(value: str):
    try:
        number = float(value)
    except ValueError:
        return value
    return int(number) if number.is_integer() and "." not in value else number


@mcp.tool(
    title="Get Indicator Data",
    description=(
        "Get the time series behind an indicator as a table (headers + rows). Long "
        "series are truncated to the most recent rows; use max_rows to get more. "
        "Numbers are returned as numbers, empty cells as empty strings."
    ),
)
async def get_indicator_data(indicator_id: int, max_rows: int = 200) -> dict:
    """
    Get the data table of an indicator.

    Args:
        indicator_id: The indicator id (Kennzahl, e.g. 10027)
        max_rows: Maximum rows to return (default: 200, max: 5000). When the table is
            longer, the last max_rows rows are returned (usually the most recent years).

    Returns:
        Dictionary with headers, rows, total_rows and truncated flag.
    """
    # Several indicators share one data file (data-id differs from id), so
    # resolve it from the metadata instead of assuming data file == id.
    meta = await fetch(f"/api/indikator-meta/{int(indicator_id)}")
    data_id = meta.get("data-id") or meta.get("id") or indicator_id
    data = await fetch(f"/api/indikator-data/{int(data_id)}")

    rows = [[_to_number(cell) for cell in row] for row in data.get("rows", [])]
    max_rows = max(1, min(max_rows, 5000))
    result = {
        "indicator_id": int(indicator_id),
        "title": meta.get("title"),
        "subtitle": meta.get("subtitle"),
        "headers": data.get("headers", []),
        "rows": rows[-max_rows:],
        "total_rows": len(rows),
        "truncated": len(rows) > max_rows,
        "source": [_clean_html(q) for q in meta.get("quellenangabe") or []],
        "portal_url": _portal_url(indicator_id),
    }
    if meta.get("filter"):
        # The portal chart shows only a subset of this shared data file.
        result["note"] = (
            "The portal chart shows a filtered subset of this table "
            f"(filter: {meta['filter']}). Rows for other categories are included."
        )
    return result


_FACETS = {
    "thema": "thema",
    "unterthema": "unterthema",
    "kennzahlenset": "kennzahlenset",
    "raeumliche_gliederung": "raeumlicheGliederung",
    "darstellungsart": "darstellungsart",
}


@mcp.tool(
    title="Get Facet Values",
    description=(
        "List the values available for filtering indicators, with counts: topics "
        "(thema), subtopics (unterthema), indicator sets (kennzahlenset), spatial units "
        "(raeumliche_gliederung) or display type (darstellungsart)."
    ),
)
async def get_facets(facet: str = "thema") -> dict:
    """
    Get facet values for filtering search_indicators.

    Args:
        facet: One of "thema", "unterthema", "kennzahlenset", "raeumliche_gliederung",
            "darstellungsart" (default: "thema")

    Returns:
        Dictionary with facet name and values sorted by name, each with a count.
    """
    if facet not in _FACETS:
        raise ValueError(f"Unknown facet {facet!r}. Use one of: {', '.join(_FACETS)}")
    counts: dict[str, int] = {}
    for indicator in await _load_index():
        value = indicator.get(_FACETS[facet])
        for item in value if isinstance(value, list) else [value]:
            if item:
                counts[item] = counts.get(item, 0) + 1
    return {
        "facet": facet,
        "values": [{"name": name, "count": counts[name]} for name in sorted(counts)],
    }


# --- Optional: search across all portal products -----------------------------


async def meili_search(index: str, body: dict) -> dict:
    async with httpx.AsyncClient(base_url=MEILISEARCH_URL, timeout=30.0) as client:
        response = await client.post(
            f"/indexes/{index}/search",
            json=body,
            headers={"Authorization": f"Bearer {MEILISEARCH_KEY}"},
        )
        response.raise_for_status()
        return response.json()


def _simplify_product(hit: dict) -> dict:
    url = hit.get("url")
    # Indicator hits point at the legacy GitHub Pages viewer; prefer the portal.
    if hit.get("produkt") == "Indikator" and hit.get("stata_id"):
        url = _portal_url(hit["stata_id"])
    return {
        "title": hit.get("bezeichnung"),
        "description": _clean_html(hit.get("beschreibung")),
        "type": hit.get("suchprodukt"),
        "product": hit.get("produkt"),
        "thema": (hit.get("thema") or {}).get("name_short"),
        "published": hit.get("publikationsdatum"),
        "indicator_id": hit.get("stata_id") if hit.get("produkt") == "Indikator" else None,
        "url": url,
    }


async def search_portal(search: str, product_type: str | None = None, limit: int = 10) -> dict:
    """
    Full-text search across everything published on the portal.

    Args:
        search: Search terms (German)
        product_type: Optional product type: "Artikel", "Dashboard", "Indikator", "Indikatorenset",
            "Karte", "OGD-Datensatz", "Publikation", "Tabelle"
        limit: Number of items to return (default: 10, max: 50)

    Returns:
        Dictionary with estimated_total_hits and results (title, description, type, url).
    """
    body: dict = {"q": search, "limit": max(1, min(limit, 50))}
    if product_type:
        body["filter"] = f'suchprodukt = "{product_type.replace(chr(34), "")}"'
    data = await meili_search("product", body)
    return {
        "estimated_total_hits": data.get("estimatedTotalHits"),
        "results": [_simplify_product(h) for h in data.get("hits", [])],
    }


if MEILISEARCH_URL and MEILISEARCH_KEY:
    mcp.tool(
        title="Search Portal",
        description=(
            f"Full-text search across all content published on {DOMAIN}: articles, "
            "reports, web tables, maps, dashboards, OGD datasets and indicators. Use it "
            "when the user looks for publications or tables rather than a single "
            "indicator time series."
        ),
    )(search_portal)


# --- HTTP app ---------------------------------------------------------------


def _healthz(request) -> JSONResponse:
    return JSONResponse({"status": "ok"})


def create_http_app():
    """Build a fresh ASGI app. Call once per process; tests create one per case
    because the underlying session manager may run only once per instance."""
    app = mcp.streamable_http_app()
    app.router.routes.insert(0, Route("/healthz", _healthz))
    return app


http_app = create_http_app()


def main():
    mcp.run(transport="stdio")


def main_http():
    import uvicorn

    port = int(os.environ.get("PORT", "8000"))
    uvicorn.run(http_app, host="0.0.0.0", port=port, log_level="info")


if __name__ == "__main__":
    main()
