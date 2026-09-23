import pytest


@pytest.fixture
def mock_fetch(monkeypatch):
    """Replace main.fetch with a stub that serves canned bodies per endpoint and
    records the calls. Also resets the in-memory index cache."""
    import main

    monkeypatch.setitem(main._index_cache, "data", None)
    monkeypatch.setitem(main._index_cache, "loaded_at", 0.0)

    def _install(bodies: dict):
        calls = []

        async def fake_fetch(endpoint, params=None):
            calls.append({"endpoint": endpoint, "params": params or {}})
            return bodies[endpoint]

        monkeypatch.setattr("main.fetch", fake_fetch)
        return calls

    return _install
