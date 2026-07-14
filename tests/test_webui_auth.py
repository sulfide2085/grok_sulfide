import webui_server as ws


class _FakeHandler:
    def __init__(self, host: str, path: str = "/api/status", token: str = ""):
        self.headers = {"Host": host}
        if token:
            self.headers["X-Grok-WebUI-Token"] = token
        self.path = path


def test_loopback_host_allowed_without_token_when_disabled(monkeypatch):
    monkeypatch.setattr(ws, "WEBUI_TOKEN", "")
    monkeypatch.setattr(ws, "WEBUI_HOST", "127.0.0.1")
    ok, reason = ws._authorize(_FakeHandler("127.0.0.1:8765"))
    assert ok and reason == ""


def test_bad_host_rejected(monkeypatch):
    monkeypatch.setattr(ws, "WEBUI_TOKEN", "")
    monkeypatch.setattr(ws, "WEBUI_HOST", "127.0.0.1")
    ok, reason = ws._authorize(_FakeHandler("evil.example"))
    assert not ok
    assert "Host" in reason


def test_api_requires_token_when_enabled(monkeypatch):
    monkeypatch.setattr(ws, "WEBUI_TOKEN", "secret-token")
    monkeypatch.setattr(ws, "WEBUI_HOST", "127.0.0.1")
    ok, _ = ws._authorize(_FakeHandler("127.0.0.1:8765", path="/api/status"))
    assert not ok
    ok2, _ = ws._authorize(
        _FakeHandler("127.0.0.1:8765", path="/api/status", token="secret-token")
    )
    assert ok2


def test_static_allowed_with_valid_host_even_without_token(monkeypatch):
    monkeypatch.setattr(ws, "WEBUI_TOKEN", "secret-token")
    monkeypatch.setattr(ws, "WEBUI_HOST", "127.0.0.1")
    ok, reason = ws._authorize(_FakeHandler("127.0.0.1:8765", path="/"))
    assert ok and reason == ""
