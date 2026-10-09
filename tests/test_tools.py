import pytest
from mcp.server.mcpserver.exceptions import ToolError

INDEX = [
    {
        "id": 7510,
        "title": "Arbeitslosenquote nach Alter",
        "subtitle": "in %, Basel-Stadt",
        "thema": "03 Arbeit, Erwerb",
        "unterthema": "Arbeitslose",
        "kennzahlenset": "Arbeitsmarkt",
        "raeumlicheGliederung": ["Kanton"],
        "aktualisierungsdatum": "2026-09-07T00:00:00",
    },
    {
        "id": 4839,
        "title": "Arbeitslosenquote",
        "subtitle": "in %, Wohnviertel",
        "thema": "03 Arbeit, Erwerb",
        "unterthema": "Arbeitslose",
        "kennzahlenset": "Soziales",
        "raeumlicheGliederung": ["Wohnviertel"],
        "aktualisierungsdatum": "2026-03-12T00:00:00",
    },
    {
        "id": 100,
        "title": "Wohnbevölkerung",
        "subtitle": "Anzahl Personen",
        "thema": "01 Bevölkerung",
        "unterthema": "Bestand",
        "kennzahlenset": "Soziales",
        "raeumlicheGliederung": ["Kanton", "Gemeinde"],
        "aktualisierungsdatum": "2026-01-01T00:00:00",
    },
]


def _index(**extra):
    import main

    return {main.INDEX_PATH: INDEX, **extra}


@pytest.mark.anyio
async def test_search_ranks_title_hits_first(mock_fetch):
    import main

    mock_fetch(_index())

    result = await main.search_indicators(search="arbeitslosenquote")

    assert result["match"] == "all_terms"
    # equal title score: the shorter title wins the tie
    assert [r["id"] for r in result["results"]] == [4839, 7510]
    assert result["results"][0]["portal_url"].endswith("/indikatorenportal/4839")


@pytest.mark.anyio
async def test_search_normalizes_umlauts(mock_fetch):
    import main

    mock_fetch(_index())

    result = await main.search_indicators(search="Wohnbevoelkerung")

    assert [r["id"] for r in result["results"]] == [100]


@pytest.mark.anyio
async def test_search_falls_back_to_partial_match(mock_fetch):
    import main

    mock_fetch(_index())

    result = await main.search_indicators(search="arbeitslosenquote velofahrer")

    assert result["match"] == "partial"
    assert {r["id"] for r in result["results"]} == {7510, 4839}


@pytest.mark.anyio
async def test_search_filters_by_spatial_unit(mock_fetch):
    import main

    mock_fetch(_index())

    result = await main.search_indicators(raeumliche_gliederung="Gemeinde")

    assert [r["id"] for r in result["results"]] == [100]


@pytest.mark.anyio
async def test_index_is_cached(mock_fetch):
    import main

    calls = mock_fetch(_index())

    await main.search_indicators(search="arbeit")
    await main.get_facets("thema")

    assert len(calls) == 1


@pytest.mark.anyio
async def test_facets_count_list_values(mock_fetch):
    import main

    mock_fetch(_index())

    result = await main.get_facets("raeumliche_gliederung")

    assert result["values"] == [
        {"name": "Gemeinde", "count": 1},
        {"name": "Kanton", "count": 2},
        {"name": "Wohnviertel", "count": 1},
    ]


@pytest.mark.anyio
async def test_facets_rejects_unknown(mock_fetch):
    import main

    mock_fetch(_index())

    with pytest.raises(ToolError, match="Unknown facet"):
        await main.get_facets("nope")


@pytest.mark.anyio
async def test_get_indicator_cleans_html(mock_fetch):
    import main

    meta = {
        "id": 11165,
        "title": "Pflegeheime",
        "erlaeuterungen": "Zeile 1<br><br>Stand &amp; Anfang 2025",
        "quellenangabe": ["Gesundheitsdepartement"],
        "externalLinks": ["<a href = 'https://www.bs.ch/x' target = '_blank'>Liste</a>"],
    }
    mock_fetch({"/api/indikator-meta/11165": meta})

    result = await main.get_indicator(11165)

    assert result["erlaeuterungen"] == "Zeile 1\n\nStand & Anfang 2025"
    assert result["external_links"] == [{"title": "Liste", "url": "https://www.bs.ch/x"}]
    assert result["data_id"] == 11165


@pytest.mark.anyio
async def test_data_uses_data_id_and_truncates(mock_fetch):
    import main

    meta = {"id": 12719, "data-id": 12611, "title": "Pyramide", "filter": ""}
    data = {"headers": ["Jahr", "Wert"], "rows": [["2020", "1.5"], ["2021", ""], ["2022", "3"]]}
    calls = mock_fetch({"/api/indikator-meta/12719": meta, "/api/indikator-data/12611": data})

    result = await main.get_indicator_data(12719, max_rows=2)

    assert calls[1]["endpoint"] == "/api/indikator-data/12611"
    assert result["rows"] == [[2021, ""], [2022, 3]]
    assert result["total_rows"] == 3
    assert result["truncated"] is True
    assert "note" not in result


# --- agent-citable results -------------------------------------------------


@pytest.mark.parametrize(
    ("subtitle", "unit"),
    [
        ("in %, Basel-Stadt", "%"),
        ("in Prozent, Basel-Stadt", "%"),
        ("Anzahl Personen", "Personen"),
        ("Anzahl", "Anzahl"),
        ("in Franken pro m<sup>2</sup>", "Franken pro m²"),
        ("pro 1000 Einwohner", "pro 1000 Einwohner"),
        ("nach Zimmerzahl, Basel-Stadt", None),
        ("Basel-Stadt, 2026", None),
        ("", None),
        (None, None),
    ],
)
def test_unit_from_subtitle(subtitle, unit):
    import main

    assert main._unit_from_subtitle(subtitle) == unit


@pytest.mark.parametrize("bad", ["abc", "", "-3", 0, -1, "12.5", "1; rm", None, True, 10**12])
@pytest.mark.anyio
async def test_invalid_indicator_id_gives_helpful_error(mock_fetch, bad):
    import main

    calls = mock_fetch({})

    for tool in (main.get_indicator, main.get_indicator_data):
        with pytest.raises(ToolError, match="positive integer.*search_indicators"):
            await tool(bad)
    assert calls == []


@pytest.mark.anyio
async def test_string_indicator_id_is_accepted(mock_fetch):
    import main

    calls = mock_fetch({"/api/indikator-meta/5813": {"id": 5813, "title": "Q"}})

    result = await main.get_indicator(" 5813 ")

    assert result["id"] == 5813
    assert calls[0]["endpoint"] == "/api/indikator-meta/5813"


@pytest.mark.anyio
async def test_search_hits_are_citable(mock_fetch):
    import main

    mock_fetch(_index())

    results = (await main.search_indicators(search="arbeitslosenquote"))["results"]
    hit = next(r for r in results if r["id"] == 7510)

    assert hit["title"] == "Arbeitslosenquote nach Alter"
    assert hit["unit"] == "%"
    assert hit["aktualisierungsdatum"] == "2026-09-07T00:00:00"
    assert hit["portal_url"].endswith("/indikatorenportal/7510")


@pytest.mark.anyio
async def test_search_tolerates_umlaut_spellings_and_inflection(mock_fetch):
    import main

    mock_fetch(_index())

    for query in ("Wohnbevölkerung", "Wohnbevoelkerung", "Wohnbevolkerung", "WOHNBEVÖLKERUNG"):
        result = await main.search_indicators(search=query)
        assert [r["id"] for r in result["results"]] == [100], query
    # plural / case ending vs. compound in the title
    result = await main.search_indicators(search="Arbeitslosenquoten")
    assert {r["id"] for r in result["results"]} == {7510, 4839}


@pytest.mark.anyio
async def test_search_exact_title_wins_ties(mock_fetch):
    import main

    index = [
        {
            "id": 1,
            "title": "Leerwohnungsquote nach Zimmerzahl",
            "aktualisierungsdatum": "2026-01-01",
        },
        {"id": 2, "title": "Leerwohnungsquote", "aktualisierungsdatum": "2020-01-01"},
    ]
    mock_fetch({main.INDEX_PATH: index})

    result = await main.search_indicators(search="Leerwohnungsquote")

    assert [r["id"] for r in result["results"]] == [2, 1]


@pytest.mark.anyio
async def test_search_partial_and_none_explain_themselves(mock_fetch):
    import main

    mock_fetch(_index())

    partial = await main.search_indicators(search="arbeitslosenquote velofahrer")
    assert partial["match"] == "partial"
    assert "only some" in partial["hint"]
    assert partial["results"][0]["matched_terms"] == ["arbeitslosenquote"]

    none = await main.search_indicators(search="velofahrer")
    assert none["match"] == "none"
    assert none["results"] == []
    assert none["hint"]


def _data_meta(**extra):
    return {
        "id": 5813,
        "title": "Leerwohnungsquote",
        "subtitle": "in Prozent, Basel-Stadt",
        "aktualisierungsdatum": "2026-08-21T08:59:31",
        "quellenangabe": ["Statistisches Amt <b>Basel-Stadt</b>, Leerstandserhebung"],
        "erlaeuterungen": "Stichtag 1. Juni.<br>Bruch 2020 &amp; neu.",
        "lesehilfe": "Die Quote liegt 2026 bei 0,78%.",
        **extra,
    }


@pytest.mark.anyio
async def test_data_carries_unit_source_notes_and_latest(mock_fetch):
    import main

    data = {
        "headers": ["Jahr", "Quote in %"],
        "rows": [["2024", "0.7722"], ["2026", "0.7764"], ["2025", "0.9232"]],
    }
    mock_fetch({"/api/indikator-meta/5813": _data_meta(), "/api/indikator-data/5813": data})

    result = await main.get_indicator_data(5813)

    assert result["unit"] == "%"
    assert result["subtitle"] == "in Prozent, Basel-Stadt"
    assert result["aktualisierungsdatum"] == "2026-08-21T08:59:31"
    assert result["source"] == ["Statistisches Amt Basel-Stadt, Leerstandserhebung"]
    assert result["portal_url"] == main._portal_url(5813)
    assert result["title"] == "Leerwohnungsquote"
    assert result["notes"] == "Stichtag 1. Juni. Bruch 2020 & neu. Die Quote liegt 2026 bei 0,78%."
    assert result["latest"] == {
        "period_label": "Jahr",
        "period": 2026,
        "values": {"Quote in %": 0.7764},
    }


@pytest.mark.anyio
async def test_latest_skips_empty_trailing_period_and_lists_categories(mock_fetch):
    import main

    data = {
        "headers": ["Jahr", "Geschlecht", "Anzahl"],
        "rows": [["2024", "m", "5"], ["2024", "w", "6"], ["2025", "m", ""], ["2025", "w", ""]],
    }
    mock_fetch({"/api/indikator-meta/5813": _data_meta(), "/api/indikator-data/5813": data})

    result = await main.get_indicator_data(5813)

    assert result["latest"]["period"] == 2024
    assert result["latest"]["values"] == [
        {"Geschlecht": "m", "Anzahl": 5},
        {"Geschlecht": "w", "Anzahl": 6},
    ]


@pytest.mark.anyio
async def test_no_latest_without_period_column(mock_fetch):
    import main

    data = {"headers": ["Kategorie", "Wert"], "rows": [["a", "1"], ["b", "2"]]}
    mock_fetch({"/api/indikator-meta/5813": _data_meta(), "/api/indikator-data/5813": data})

    result = await main.get_indicator_data(5813)

    assert "latest" not in result
    assert result["truncated"] is False
    assert "hint" not in result


def test_notes_are_capped_on_a_word_boundary():
    import main

    notes = main._notes({"erlaeuterungen": "wort " * 400})

    assert len(notes) <= 600
    assert notes.endswith("wort…")
    assert main._notes({"erlaeuterungen": "", "lesehilfe": None}) is None


@pytest.mark.anyio
async def test_data_output_is_capped_newest_rows_first(mock_fetch):
    import json

    import main

    rows = [[str(1900 + i), f"{i}.123456", "x" * 40] for i in range(2000)]
    data = {"headers": ["Jahr", "Wert", "Text"], "rows": rows}
    mock_fetch({"/api/indikator-meta/5813": _data_meta(), "/api/indikator-data/5813": data})

    result = await main.get_indicator_data(5813, max_rows=5000)

    assert len(json.dumps(result, ensure_ascii=False)) <= main.DATA_MAX_CHARS
    assert result["truncated"] is True
    assert result["total_rows"] == 2000
    assert result["rows"][-1][0] == 3899  # newest row is kept
    assert "max_rows" in result["hint"]
    assert result["latest"]["period"] == 3899


@pytest.mark.anyio
async def test_html_is_cleaned_in_titles_headers_and_cells(mock_fetch):
    import main

    meta = _data_meta(title="Fl&auml;che in m<sup>2</sup>", filter="Art=<i>A</i>")
    data = {"headers": ["Jahr", "Wert<br>in m<sup>2</sup>"], "rows": [["2025", "<b>5</b>"]]}
    mock_fetch({"/api/indikator-meta/5813": meta, "/api/indikator-data/5813": data})

    result = await main.get_indicator_data(5813)

    assert result["title"] == "Fläche in m²"
    assert result["headers"] == ["Jahr", "Wert in m²"]
    assert result["rows"] == [[2025, 5]]
    assert "<" not in result["note"]
