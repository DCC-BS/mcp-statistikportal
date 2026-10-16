import pytest


@pytest.fixture
def mock_fetch(monkeypatch):
    """Replace main.fetch with a stub that serves canned bodies per endpoint and
    records the calls. A body may be a callable taking the query params. Also
    resets the in-memory index cache."""
    import main

    monkeypatch.setitem(main._index_cache, "data", None)
    monkeypatch.setitem(main._index_cache, "loaded_at", 0.0)

    def _install(bodies: dict):
        calls = []

        async def fake_fetch(endpoint, params=None, **kwargs):
            calls.append({"endpoint": endpoint, "params": params or {}})
            body = bodies[endpoint]
            return body(params or {}) if callable(body) else body

        monkeypatch.setattr("main.fetch", fake_fetch)
        return calls

    return _install
