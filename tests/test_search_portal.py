import httpx
import pytest
from mcp.server.mcpserver.exceptions import ToolError


@pytest.fixture
def meili(monkeypatch):
    """Route main.meili_search to a mock Meilisearch that answers with `handler`."""
    import main

    monkeypatch.setattr(main, "MEILISEARCH_URL", "http://meili.test")
    monkeypatch.setattr(main, "MEILISEARCH_KEY", "test-key")
    real_client = httpx.AsyncClient

    def _install(handler):
        requests = []

        def record(request):
            requests.append(request)
            return handler(request)

        transport = httpx.MockTransport(record)
        monkeypatch.setattr(
            main.httpx, "AsyncClient", lambda **kwargs: real_client(transport=transport, **kwargs)
        )
        return requests

    return _install


@pytest.mark.anyio
async def test_search_portal_returns_hits(meili):
    import main

    hit = {"bezeichnung": "Beispieltabelle", "suchprodukt": "Tabelle", "produkt": "Webtabelle"}
    requests = meili(lambda r: httpx.Response(200, json={"estimatedTotalHits": 1, "hits": [hit]}))

    result = await main.search_portal("Beispiel", product_type="Tabelle")

    assert result["results"][0]["title"] == "Beispieltabelle"
    assert requests[0].headers["Authorization"] == "Bearer test-key"


@pytest.mark.parametrize("status", [401, 403])
@pytest.mark.anyio
async def test_search_portal_invalid_key_names_fallback(meili, status):
    import main

    meili(lambda r: httpx.Response(status, json={"message": "The provided API key is invalid."}))

    with pytest.raises(ToolError) as error:
        await main.search_portal("Beispiel")

    message = str(error.value)
    assert f"Portal search unavailable (HTTP {status}" in message
    assert "search key is invalid" in message
    assert "search_indicators" in message


@pytest.mark.anyio
async def test_search_portal_server_error_names_fallback(meili):
    import main

    meili(lambda r: httpx.Response(503, text="Service Unavailable"))

    with pytest.raises(ToolError, match=r"HTTP 503\).*search_indicators"):
        await main.search_portal("Beispiel")


@pytest.mark.anyio
async def test_search_portal_unreachable_names_fallback(meili):
    import main

    def refuse(request):
        raise httpx.ConnectError("connection refused", request=request)

    meili(refuse)

    with pytest.raises(ToolError, match=r"unavailable \(ConnectError\).*search_indicators"):
        await main.search_portal("Beispiel")


@pytest.mark.anyio
async def test_search_portal_rejected_query_passes_reason(meili):
    import main

    reason = "Attribute `foo` is not filterable."
    meili(lambda r: httpx.Response(400, json={"message": reason}))

    with pytest.raises(ToolError, match="rejected the query: Attribute `foo` is not filterable"):
        await main.search_portal("Beispiel", product_type="Unbekannt")


@pytest.mark.anyio
async def test_search_portal_non_json_answer(meili):
    import main

    meili(lambda r: httpx.Response(200, text="<html>proxy login</html>"))

    with pytest.raises(ToolError, match="no JSON.*search_indicators"):
        await main.search_portal("Beispiel")
