"""Smoke tests for protocol adapter boundary (no live network)."""
from __future__ import annotations

from pathlib import Path

import protocol_register as pr


def test_engine_root_exists():
    assert pr.ENGINE_ROOT.is_dir()
    assert (pr.ENGINE_ROOT / "xconsole_client").is_dir()


def test_boundary_doc_exists():
    assert (Path("protocol_engine") / "BOUNDARY.md").is_file()


def test_register_one_protocol_is_public():
    assert callable(pr.register_one_protocol)
    assert callable(pr.create_email_receiver)


def test_unsupported_provider_raises():
    try:
        pr.create_email_receiver({"protocol_email_provider": "not-a-provider"})
        assert False, "expected ValueError"
    except ValueError as exc:
        assert "unsupported" in str(exc)


def test_moemail_requires_key():
    try:
        pr.create_moemail({"protocol_moemail_api_key": ""})
        assert False, "expected ValueError"
    except ValueError as exc:
        assert "protocol_moemail_api_key" in str(exc)
