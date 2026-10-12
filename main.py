import asyncio
import html
import json
import os
import re
import time
import unicodedata
from pathlib import Path

import httpx
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import HTMLResponse, JSONResponse
from starlette.routing import Route

import api_docs


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
# Limits that keep tool output small enough for a ~30B model context.
NOTES_MAX_CHARS = 600
DATA_MAX_CHARS = 12000

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


class RelaxedAcceptHeaderMiddleware(BaseHTTPMiddleware):
    """Tolerate JSON-RPC clients that send incomplete Accept headers.

    The streamable HTTP transport rejects POST requests without an Accept
    header listing both application/json and text/event-stream (406), which
    plain JSON-RPC clients routinely omit. Rewrite those Accept headers to
    what the transport requires before the request reaches it."""

    async def dispatch(self, request, call_next):
        if request.method == "POST":
            accept = request.headers.get("accept", "")
            if "text/event-stream" not in accept or "application/json" not in accept:
                request.scope["headers"] = [
                    (k, v) for k, v in request.scope["headers"] if k.lower() != b"accept"
                ] + [(b"accept", b"application/json, text/event-stream")]
        return await call_next(request)


mcp = MCPServer(
    DOMAIN,
    instructions=(
        "Statistics of the canton of Basel-Stadt (Statistisches Amt Basel-Stadt). "
        "Find indicators with search_indicators (German terms work best), read their "
        "explanation with get_indicator, then load the time series with "
        "get_indicator_data. Always cite the source (quellenangabe) and link portal_url."
    ),
)

MCP_HTTP_SETTINGS = {
    "streamable_http_path": "/mcp",
    "stateless_http": True,
    "transport_security": _transport_security(),
}


async def fetch(endpoint: str, params: dict[str, str | int] | None = None) -> dict | list:
    """GET from the portal. Failures are raised as ToolError with a reason the calling model can act on
    (unknown id, portal down); any other exception would reach it only as "Error executing tool <name>"."""
    try:
        async with httpx.AsyncClient(base_url=BASE_URL, timeout=30.0) as client:
            response = await client.get(endpoint, params=params)
    except httpx.TimeoutException as exc:
        raise ToolError(f"{DOMAIN} did not answer within 30 s") from exc
    except httpx.HTTPError as exc:
        raise ToolError(f"{DOMAIN} is not reachable: {type(exc).__name__}") from exc
    if response.status_code == 404:
        raise ToolError(
            f"not found on {DOMAIN}: {endpoint} (check the indicator id with search_indicators)"
        )
    if response.status_code >= 400:
        raise ToolError(f"{DOMAIN} answered {response.status_code} for {endpoint}")
    # Some source files carry a UTF-8 BOM, which json.loads rejects.
    try:
        return json.loads(response.content.decode("utf-8-sig"))
    except ValueError as exc:
        raise ToolError(f"{DOMAIN} returned no JSON for {endpoint}") from exc


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
_SUP_RE = re.compile(r"<sup>\s*([23])\s*</sup>", re.IGNORECASE)
_LINK_RE = re.compile(r"""<a\s[^>]*href\s*=\s*['"]([^'"]+)['"][^>]*>(.*?)</a>""", re.IGNORECASE)
_SUPERSCRIPT = {"2": "²", "3": "³"}


def _clean_html(value) -> str:
    """Plain text from portal HTML: <br> becomes a newline, tags are dropped, entities decoded."""
    if not value:
        return ""
    text = _SUP_RE.sub(lambda m: _SUPERSCRIPT[m.group(1)], str(value))
    text = _BR_RE.sub("\n", text)
    return html.unescape(_TAG_RE.sub("", text)).strip()


def _clean_inline(value) -> str:
    """Like _clean_html but on a single line (titles, headers, units)."""
    return " ".join(_clean_html(value).split())


def _parse_links(values) -> list[dict]:
    links = []
    for value in values or []:
        match = _LINK_RE.search(str(value))
        if match:
            links.append({"title": _clean_html(match.group(2)), "url": match.group(1)})
    return links


def _portal_url(indicator_id) -> str:
    return f"{BASE_URL}/indikatorenportal/{indicator_id}"


def _parse_indicator_id(value) -> int:
    """The id must be a positive integer (the portal does not validate it itself)."""
    text = str(value).strip() if not isinstance(value, bool) and value is not None else ""
    if not text.isascii() or not text.isdigit() or int(text) < 1 or len(text) > 9:
        raise ToolError(
            f"indicator_id must be a positive integer such as 5813, got {value!r}. "
            "Find ids with search_indicators (use the 'id' field of a result)."
        )
    return int(text)


_UNIT_ALIASES = {"prozent": "%"}


def _unit_from_subtitle(subtitle) -> str | None:
    """The portal has no unit field; it is the first part of the subtitle, e.g.
    "in %, Basel-Stadt" -> "%", "Anzahl Personen" -> "Personen". None if unclear."""
    first = _clean_inline(subtitle).split(",")[0].strip()
    lowered = first.casefold()
    unit = None
    if lowered.startswith("in ") and len(first) > 3:
        unit = first[3:].strip()
    elif lowered.startswith("anzahl"):
        unit = first[len("anzahl") :].strip() or "Anzahl"
    elif lowered.startswith(("pro ", "indexiert", "einwohner pro ")):
        unit = first
    if not unit:
        return None
    return _UNIT_ALIASES.get(unit.casefold(), unit)


def _notes(data: dict, limit: int = NOTES_MAX_CHARS) -> str | None:
    """Short methodology text (erlaeuterungen first, then lesehilfe) so that breaks
    in the series and definitions reach the model."""
    parts = [
        " ".join(_clean_html(data.get(key)).split()) for key in ("erlaeuterungen", "lesehilfe")
    ]
    text = " ".join(part for part in parts if part)
    if len(text) <= limit:
        return text or None
    return text[: limit - 1].rsplit(" ", 1)[0].rstrip(",;:") + "…"


def _simplify_indicator(data: dict) -> dict:
    return {
        "id": data.get("id"),
        "title": _clean_inline(data.get("title")),
        "subtitle": _clean_inline(data.get("subtitle")),
        "unit": _unit_from_subtitle(data.get("subtitle")),
        "thema": data.get("thema"),
        "unterthema": data.get("unterthema"),
        "kennzahlenset": data.get("kennzahlenset"),
        "raeumliche_gliederung": data.get("raeumlicheGliederung") or [],
        "darstellungsart": data.get("darstellungsart"),
        "aktualisierungsdatum": data.get("aktualisierungsdatum"),
        "portal_url": _portal_url(data.get("id")),
    }


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

_UMLAUT_DIGRAPHS = str.maketrans({"ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss"})
_STEM_SUFFIXES = ("en", "er", "es", "e", "n", "s")


def _normalize(value) -> str:
    """Casefolded text with umlauts written as ae/oe/ue (the "oe" spelling)."""
    return " ".join(str(value or "").casefold().translate(_UMLAUT_DIGRAPHS).split())


def _fold(value) -> str:
    """Casefolded text with umlauts reduced to the bare vowel ("Wohnbevolkerung")."""
    text = unicodedata.normalize("NFKD", str(value or "").casefold().replace("ß", "ss"))
    return " ".join("".join(c for c in text if not unicodedata.combining(c)).split())


def _stems(term: str) -> list[str]:
    """The term plus one trimmed inflection ("leerwohnungen" -> "leerwohnung"), so a
    plural or case ending does not hide a compound ("Leerwohnungsquote")."""
    stems = [term]
    for suffix in _STEM_SUFFIXES:
        if term.endswith(suffix) and len(term) - len(suffix) >= 5:
            stems.append(term[: -len(suffix)])
            break
    return stems


def _search_texts(indicator: dict) -> list[tuple[str, str, float]]:
    return [
        (_normalize(indicator.get(name)), _fold(indicator.get(name)), weight)
        for name, weight in _SEARCH_FIELDS
    ]


def _score(texts: list[tuple[str, str, float]], terms: list[str]) -> tuple[int, float, list[str]]:
    """(matched term count, weighted score, matched terms). Substring matching keeps
    German compounds findable, e.g. "arbeitslos" hits "Arbeitslosenquote"; umlauts
    match as "ae" or as the bare vowel."""
    matched, score, hits = 0, 0.0, []
    for term in terms:
        variants = [(stem, _fold(stem)) for stem in _stems(term)]
        best = max(
            (
                weight
                for norm, folded, weight in texts
                if any(stem in norm or bare in folded for stem, bare in variants)
            ),
            default=0.0,
        )
        if best:
            matched += 1
            score += best
            hits.append(term)
    return matched, score, hits


def _matches_filter(value, wanted: str | None) -> bool:
    if not wanted:
        return True
    wanted_norm = _normalize(wanted)
    values = value if isinstance(value, list) else [value]
    return any(wanted_norm in _normalize(v) for v in values)


@mcp.tool(
    title="Search Indicators",
    description=(
        "Search statistical indicators of Basel-Stadt by German keywords (e.g. "
        '"Leerwohnungsquote", not English). Returns hits with id, title, unit, '
        "aktualisierungsdatum and portal_url; pass the id to get_indicator_data. "
        "Covers the ~1,000 indicators shown in the portal, not every indicator id. "
        "Prefer one or two distinctive words over a sentence."
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
    Args:
        search: German keywords; umlauts may be written ae/oe/ue. Hits match all
            words; otherwise the best partial matches are returned (see "match").
        thema: Topic filter, substring (see get_facets).
        unterthema: Subtopic filter, substring.
        kennzahlenset: Indicator set filter, substring.
        raeumliche_gliederung: Spatial unit filter, e.g. "Wohnviertel", "Gemeinde", "Kanton".
        limit: Hits to return (default 10, max 100).
        offset: Hits to skip (default 0).
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
    matched_terms: dict[int, list[str]] = {}
    hint = None
    if terms:
        scored = [(ind, *_score(_search_texts(ind), terms)) for ind in candidates]
        hits = [(ind, s) for ind, m, s, _ in scored if m == len(terms)]
        if not hits:
            match = "partial"
            hits = [(ind, m * 10 + s) for ind, m, s, _ in scored if m]
            matched_terms = {id(ind): found for ind, _, _, found in scored}
            hint = (
                "No indicator matches all search terms; these match only some of them "
                "(see matched_terms). Try fewer or different German words."
            )
            if not hits:
                match = "none"
                hint = (
                    "No indicator matches. Try a shorter or different German term, or drop filters."
                )
        # Ties (common: many indicators repeat a title per district) go to an exact
        # title match, then the shorter title, then the more recently updated one.
        query = " ".join(terms)
        hits.sort(
            key=lambda hit: (
                hit[1],
                _normalize(hit[0].get("title")) == query,
                -len(str(hit[0].get("title") or "")),
                str(hit[0].get("aktualisierungsdatum") or ""),
            ),
            reverse=True,
        )
        candidates = [ind for ind, _ in hits]
    else:
        candidates.sort(key=lambda ind: str(ind.get("aktualisierungsdatum") or ""), reverse=True)

    limit = max(1, min(limit, 100))
    offset = max(0, offset)
    results = []
    for ind in candidates[offset : offset + limit]:
        hit = _simplify_indicator(ind)
        if match == "partial":
            hit["matched_terms"] = matched_terms.get(id(ind), [])
        results.append(hit)
    response = {"total_count": len(candidates), "match": match, "results": results}
    if hint:
        response["hint"] = hint
    return response


@mcp.tool(
    title="Get Indicator Metadata",
    description=(
        "Get the definition of one indicator by id: title, unit, reading aid "
        "(lesehilfe), explanations (erlaeuterungen), source (quellenangabe) and "
        "last update. Read it before interpreting values from get_indicator_data."
    ),
)
async def get_indicator(indicator_id: int | str) -> dict:
    """
    Args:
        indicator_id: Positive integer id from search_indicators (e.g. 5813).
    """
    indicator_id = _parse_indicator_id(indicator_id)
    data = await fetch(f"/api/indikator-meta/{indicator_id}")
    return {
        **_simplify_indicator(data),
        "lesehilfe": _clean_html(data.get("lesehilfe")),
        "erlaeuterungen": _clean_html(data.get("erlaeuterungen")),
        "quellenangabe": [_clean_inline(q) for q in data.get("quellenangabe") or []],
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


def _clean_cell(value):
    if isinstance(value, str) and ("<" in value or "&" in value):
        value = _clean_inline(value)
    return _to_number(value) if isinstance(value, str) else value


_PERIOD_HEADER_RE = re.compile(
    r"jahr|year|datum|periode|stichtag|zeit|monat|quartal", re.IGNORECASE
)


def _period_column(headers: list, rows: list[list]) -> int | None:
    """Index of the column holding the year/date if the table looks like a time series."""
    for idx, header in enumerate(headers):
        if _PERIOD_HEADER_RE.search(str(header)) and rows and all(len(r) > idx for r in rows):
            return idx
    if rows and all(r and isinstance(r[0], int) and 1800 <= r[0] <= 2200 for r in rows):
        return 0
    return None


def _period_key(value):
    """Sort key for periods like 2025, "2025", "2025-06-01", "2025 Q2" (None if unusable)."""
    return (
        (0, value)
        if isinstance(value, int)
        else (1, str(value))
        if value not in ("", None)
        else None
    )


def _latest(headers: list, rows: list[list], max_rows: int = 20) -> dict | None:
    """The most recent period that has at least one value, as {period, values}."""
    col = _period_column(headers, rows)
    if col is None:
        return None
    # A row counts when it holds a number besides the period (category labels
    # alone are not a value); tables without any number fall back to any text.
    numeric = any(isinstance(c, int | float) for r in rows for i, c in enumerate(r) if i != col)

    def has_value(row: list) -> bool:
        cells = [c for i, c in enumerate(row) if i != col]
        return (
            any(isinstance(c, int | float) for c in cells)
            if numeric
            else any(c != "" for c in cells)
        )

    candidates = [r for r in rows if _period_key(r[col]) is not None and has_value(r)]
    if not candidates:
        return None
    best = max(_period_key(r[col]) for r in candidates)
    latest_rows = [r for r in candidates if _period_key(r[col]) == best]
    values = [
        {str(h): c for i, (h, c) in enumerate(zip(headers, r)) if i != col and c != ""}
        for r in latest_rows
    ]
    return {
        "period_label": str(headers[col]),
        "period": latest_rows[0][col],
        "values": values[0] if len(values) == 1 else values[:max_rows],
    }


@mcp.tool(
    title="Get Indicator Data",
    description=(
        "Get the values of one indicator by id: headers, rows (newest rows kept), unit, source and, for time series, latest = newest period "
        "with values. Cite portal_url, source and aktualisierungsdatum; check notes "
        "for series breaks. Output is capped; use max_rows for more history."
    ),
)
async def get_indicator_data(indicator_id: int | str, max_rows: int = 200) -> dict:
    """
    Args:
        indicator_id: Positive integer id from search_indicators (e.g. 5813).
        max_rows: Rows to return, newest last (default 200, max 5000). Output is
            additionally capped at about 12,000 characters.
    """
    indicator_id = _parse_indicator_id(indicator_id)
    # Several indicators share one data file (data-id differs from id), so
    # resolve it from the metadata instead of assuming data file == id.
    meta = await fetch(f"/api/indikator-meta/{indicator_id}")
    data_id = meta.get("data-id") or meta.get("id") or indicator_id
    data = await fetch(f"/api/indikator-data/{_parse_indicator_id(data_id)}")

    headers = [_clean_inline(h) for h in data.get("headers", [])]
    rows = [[_clean_cell(cell) for cell in row] for row in data.get("rows", [])]
    max_rows = max(1, min(max_rows, 5000))
    result = {
        "indicator_id": indicator_id,
        "title": _clean_inline(meta.get("title")),
        "subtitle": _clean_inline(meta.get("subtitle")),
        "unit": _unit_from_subtitle(meta.get("subtitle")),
        "aktualisierungsdatum": meta.get("aktualisierungsdatum"),
        "source": [_clean_inline(q) for q in meta.get("quellenangabe") or []],
        "portal_url": _portal_url(indicator_id),
        "notes": _notes(meta),
        "headers": headers,
        "total_rows": len(rows),
    }
    latest = _latest(headers, rows)
    if latest:
        result["latest"] = latest
    if meta.get("filter"):
        # The portal chart shows only a subset of this shared data file.
        result["note"] = (
            "The portal chart shows a filtered subset of this table "
            f"(filter: {_clean_inline(meta['filter'])}). Rows for other categories are included."
        )

    # Keep the newest rows that fit the character budget.
    budget = DATA_MAX_CHARS - len(json.dumps(result, ensure_ascii=False)) - 200
    kept: list[list] = []
    for row in reversed(rows[-max_rows:]):
        budget -= len(json.dumps(row, ensure_ascii=False)) + 2
        if budget < 0 and kept:
            break
        kept.append(row)
    kept.reverse()
    result["rows"] = kept
    result["truncated"] = len(kept) < len(rows)
    if result["truncated"]:
        result["hint"] = (
            f"Showing the newest {len(kept)} of {len(rows)} rows. "
            "Raise max_rows for more history (output stays capped at about 12,000 characters)."
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
        "List the valid filter values with counts for search_indicators: thema, "
        "unterthema, kennzahlenset, raeumliche_gliederung or darstellungsart. Use "
        "it to browse topics, not to find data."
    ),
)
async def get_facets(facet: str = "thema") -> dict:
    """
    Args:
        facet: thema, unterthema, kennzahlenset, raeumliche_gliederung or darstellungsart.
    """
    if facet not in _FACETS:
        raise ToolError(f"Unknown facet {facet!r}. Use one of: {', '.join(_FACETS)}")
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
        "title": _clean_inline(hit.get("bezeichnung")),
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
            f"Full-text search over all content on {DOMAIN} (articles, reports, tables, "
            "maps, OGD datasets). Use German terms; for a single time series prefer "
            "search_indicators."
        ),
    )(search_portal)


# --- HTTP app ---------------------------------------------------------------


def _healthz(request) -> JSONResponse:
    return JSONResponse({"status": "ok"})


def _index(request) -> JSONResponse:
    return JSONResponse(
        {
            "server": DOMAIN,
            "status": "ok",
            "transport": "mcp-streamable-http",
            "endpoints": {
                "mcp": "/mcp",
                "health": "/healthz",
                "openapi": "/openapi.json",
                "docs": "/docs",
            },
        }
    )


def _openapi(request) -> JSONResponse:
    return JSONResponse(api_docs.build_openapi(DOMAIN, mcp._tool_manager.list_tools()))


def _docs(request) -> HTMLResponse:
    return HTMLResponse(api_docs.docs_page(DOMAIN))


def create_http_app():
    """Build a fresh ASGI app. Call once per process; tests create one per case
    because the underlying session manager may run only once per instance."""
    app = mcp.streamable_http_app(**MCP_HTTP_SETTINGS)
    app.add_middleware(RelaxedAcceptHeaderMiddleware)
    app.router.routes.insert(0, Route("/", _index))
    app.router.routes.insert(0, Route("/healthz", _healthz))
    app.router.routes.insert(0, Route("/openapi.json", _openapi))
    app.router.routes.insert(0, Route("/docs", _docs))
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
