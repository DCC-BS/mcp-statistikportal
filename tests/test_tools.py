import pytest

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
    assert [r["id"] for r in result["results"]] == [7510, 4839]
    assert result["results"][0]["portal_url"].endswith("/indikatorenportal/7510")


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

    with pytest.raises(ValueError):
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
