import httpx
import pytest
from mcp.server.mcpserver.exceptions import ToolError

ENDPOINT = "/api/webtabelle-data/t01-1-02.xlsx"

SHEETS = [
    {"index": 0, "name": "Steckbrief", "kind": "steckbrief"},
    {"index": 1, "name": "Kanton", "kind": "table"},
    {"index": 2, "name": "Riehen", "kind": "table"},
]

BASE = {"file": "t01-1-02.xlsx", "sheets": SHEETS, "code": "t01.1.02"}

# Shapes as returned by the portal's /api/webtabelle-data: the Steckbrief as
# Merkmal/Wert pairs, table sheets already flat (joined headers, Gruppe column).
STECKBRIEF = {
    **BASE,
    "sheet": "Steckbrief",
    "title": "Wohnbevölkerung",
    "headers": ["Merkmal", "Wert"],
    "rows": [
        ["Datenquelle", "Einwohnerregister"],
        ["Letzte Aktualisierung", "Oktober 2026"],
        ["Weitere Auskünfte", "Irma Rodiqi, irma.rodiqi@bs.ch; Max Muster"],
    ],
    "notes": [],
}

KANTON = {
    **BASE,
    "sheet": "Kanton",
    "title": "Wohnbevölkerung seit 1900¹",
    "headers": ["Gruppe", "Jahr", "Schweiz – Männlich", "Schweiz – Weiblich", "Spalte 4"],
    "rows": [
        ["Basel", 1900, 32135, 37311, 0.12300000000000001],
        ["Basel", "1920²", "…", 40000.0, ""],
        ["Riehen", 1900, 1000.0, 1100, 560.387096774194],
    ],
    "notes": ["¹Volkszählungen."],
}

RIEHEN = {**KANTON, "sheet": "Riehen", "rows": KANTON["rows"][2:]}

BY_INDEX = {0: STECKBRIEF, 1: KANTON, 2: RIEHEN}


def _api(params):
    return BY_INDEX[int(params.get("sheet", 1))]


@pytest.mark.anyio
async def test_get_table_returns_flat_sheet(mock_fetch):
    import main

    calls = mock_fetch({ENDPOINT: _api})

    result = await main.get_table("t01-1-02.xlsx")

    assert result["sheet"] == "Kanton"
    assert result["code"] == "t01.1.02"
    assert result["title"] == "Wohnbevölkerung seit 1900¹"
    assert result["columns"] == KANTON["headers"]
    # float noise is rounded to 6 significant digits, integral floats become ints
    assert result["rows"] == [
        ["Basel", 1900, 32135, 37311, 0.123],
        ["Basel", "1920²", "…", 40000, ""],
        ["Riehen", 1900, 1000, 1100, 560.387],
    ]
    assert isinstance(result["rows"][2][2], int)
    assert result["notes"] == ["¹Volkszählungen."]
    assert result["download_url"].endswith("/files/webtabellen/t01-1-02.xlsx")
    assert result["csv_url"].endswith("/api/webtabelle/t01-1-02.xlsx?sheet=1&format=csv")
    assert [s["name"] for s in result["sheets"]] == ["Steckbrief", "Kanton", "Riehen"]
    # default sheet plus the Steckbrief
    assert [c["params"] for c in calls] == [{}, {"sheet": 0}]


@pytest.mark.anyio
async def test_get_table_includes_steckbrief(mock_fetch):
    import main

    mock_fetch({ENDPOINT: _api})

    result = await main.get_table("t01-1-02.xlsx")

    assert result["steckbrief"] == {
        "Datenquelle": "Einwohnerregister",
        "Letzte Aktualisierung": "Oktober 2026",
        "Weitere Auskünfte": "Irma Rodiqi, irma.rodiqi@bs.ch; Max Muster",
    }
    # citation fields, as for indicators
    assert result["source"] == "Einwohnerregister"
    assert result["aktualisierungsdatum"] == "Oktober 2026"


@pytest.mark.anyio
async def test_get_table_selects_sheet_by_name(mock_fetch):
    import main

    calls = mock_fetch({ENDPOINT: _api})

    result = await main.get_table("t01-1-02.xlsx", sheet="riehen")

    assert result["sheet"] == "Riehen"
    assert result["rows"] == [["Riehen", 1900, 1000, 1100, 560.387]]
    assert result["csv_url"].endswith("?sheet=2&format=csv")
    assert [c["params"] for c in calls] == [{}, {"sheet": 2}, {"sheet": 0}]


@pytest.mark.anyio
async def test_get_table_rejects_unknown_sheet(mock_fetch):
    import main

    mock_fetch({ENDPOINT: _api})

    with pytest.raises(ToolError, match="Available: Steckbrief, Kanton, Riehen"):
        await main.get_table("t01-1-02.xlsx", sheet="Bettingen")


@pytest.mark.parametrize(
    ("kwargs", "years", "first_row"),
    [
        ({"max_rows": 2}, ["Basel", "Basel"], 0),
        ({"max_rows": 2, "offset": 1}, ["Basel", "Riehen"], 1),
        ({"max_rows": 1, "from_end": True}, ["Riehen"], 2),
        ({"max_rows": 2, "offset": 1, "from_end": True}, ["Basel", "Basel"], 0),
        ({"max_rows": 2, "offset": 10}, [], 3),
    ],
)
@pytest.mark.anyio
async def test_get_table_row_window(mock_fetch, kwargs, years, first_row):
    import main

    mock_fetch({ENDPOINT: _api})

    result = await main.get_table("t01-1-02.xlsx", **kwargs)

    assert [row[0] for row in result["rows"]] == years
    assert result["first_row"] == first_row
    assert result["total_rows"] == 3
    assert result["truncated"] is True


@pytest.mark.anyio
async def test_get_table_not_truncated(mock_fetch):
    import main

    mock_fetch({ENDPOINT: _api})

    result = await main.get_table("t01-1-02.xlsx", from_end=True)

    assert len(result["rows"]) == 3
    assert result["truncated"] is False


@pytest.mark.anyio
async def test_get_table_returns_steckbrief_sheet(mock_fetch):
    import main

    calls = mock_fetch({ENDPOINT: _api})

    result = await main.get_table("t01-1-02.xlsx", sheet="Steckbrief")

    assert result["sheet"] == "Steckbrief"
    assert result["steckbrief"]["Datenquelle"] == "Einwohnerregister"
    assert "rows" not in result
    assert "csv_url" not in result
    assert len(calls) == 2


@pytest.mark.parametrize(
    "table",
    [
        "t01-1-02.xlsx",
        "t01-1-02",
        "t01.1.02",
        "T01-1-02.XLSX",
        "https://statistik.bs.ch/files/webtabellen/t01-1-02.xlsx",
        "https://statistik.bs.ch/files/webtabellen/t01-1-02.xlsx?download=1",
    ],
)
def test_webtabelle_file_accepts_variants(table):
    import main

    assert main._webtabelle_file(table) == "t01-1-02.xlsx"


@pytest.mark.parametrize("table", ["../", "t01 1 02", "a;b.xlsx", ""])
def test_webtabelle_file_rejects_invalid(table):
    import main

    with pytest.raises(ToolError, match="t06-1-05.xlsx"):
        main._webtabelle_file(table)


@pytest.mark.anyio
async def test_get_table_not_found(monkeypatch):
    import main

    real_client = httpx.AsyncClient
    transport = httpx.MockTransport(lambda request: httpx.Response(404))
    monkeypatch.setattr(
        main.httpx, "AsyncClient", lambda **kwargs: real_client(transport=transport, **kwargs)
    )

    # the 404 points to search_portal, not to the indicator search
    with pytest.raises(ToolError, match="no such web table.*search_portal"):
        await main.get_table("t99-9-99.xlsx")


@pytest.mark.parametrize(("from_end", "first", "last"), [(False, 0, None), (True, None, 599)])
@pytest.mark.anyio
async def test_get_table_caps_output(mock_fetch, monkeypatch, from_end, first, last):
    import main

    long_sheet = {**KANTON, "rows": [["Basel", year, 1, 2, 3] for year in range(600)]}
    mock_fetch({ENDPOINT: lambda p: STECKBRIEF if p.get("sheet") == 0 else long_sheet})
    monkeypatch.setattr(main, "DATA_MAX_CHARS", 3000)

    result = await main.get_table("t01-1-02.xlsx", max_rows=5000, from_end=from_end)

    assert 0 < len(result["rows"]) < 600
    assert result["truncated"] is True
    assert "offset" in result["hint"]
    # reading from the start keeps the first rows, from the end the newest ones
    if first is not None:
        assert result["rows"][0][1] == first
    if last is not None:
        assert result["rows"][-1][1] == last
    assert result["first_row"] == result["rows"][0][1]
