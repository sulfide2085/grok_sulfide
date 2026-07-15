"""Unit tests for Tempmail provider (mocked HTTP, no live network)."""
from __future__ import annotations

from types import SimpleNamespace

import providers
import providers.tempmail as tm


class _Resp:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text or str(payload)

    def json(self):
        return self._payload


def test_create_inbox_success(monkeypatch):
    calls = {}

    def fake_post(url, **kwargs):
        calls["url"] = url
        calls["headers"] = kwargs.get("headers")
        calls["json"] = kwargs.get("json")
        return _Resp(201, {"address": "abc@tempmail.lol", "token": "tok123"})

    monkeypatch.setattr(tm.runtime, "http_post", fake_post)
    monkeypatch.setattr(tm.runtime, "config", lambda: {"tempmail_api_key": "k", "tempmail_prefix": "grok"})
    addr, token = tm.create_inbox()
    assert addr == "abc@tempmail.lol"
    assert token == "tok123"
    assert calls["url"].endswith("/v2/inbox/create")
    assert calls["headers"]["Authorization"] == "Bearer k"
    assert calls["json"]["prefix"] == "grok"


def test_provider_get_email_and_code(monkeypatch):
    monkeypatch.setattr(tm, "create_inbox", lambda api_key=None: ("u@tempmail.lol", "t1"))
    monkeypatch.setattr(
        tm,
        "fetch_emails",
        lambda token, api_key=None: [
            {
                "from": "noreply@x.ai",
                "subject": "ABC-123 xAI",
                "body": "Your code is ABC-123",
                "date": 1,
            }
        ],
    )
    monkeypatch.setattr(tm.runtime, "extract_verification_code", lambda text, subject="": "ABC-123")
    monkeypatch.setattr(tm.runtime, "raise_if_cancelled", lambda cb=None: None)
    monkeypatch.setattr(tm.runtime, "sleep_with_cancel", lambda s, cb=None: None)

    p = tm.TempmailProvider()
    email, token = p.get_email_and_token()
    assert email.endswith("@tempmail.lol")
    code = p.get_oai_code(token, email, timeout=5, poll_interval=0.1)
    assert code == "ABC-123"


def test_registry_returns_tempmail():
    assert isinstance(providers.get_provider("tempmail"), tm.TempmailProvider)
