import store


def _isolate(tmp_path, monkeypatch, *, dual_write=True):
    monkeypatch.setattr(store, "_DB_PATH", str(tmp_path / "state.db"))
    monkeypatch.setattr(store, "_EMAILS_USED_FILE", str(tmp_path / "emails_used.txt"))
    monkeypatch.setattr(store, "_EMAILS_ERROR_FILE", str(tmp_path / "emails_error.txt"))
    monkeypatch.setattr(store, "_ACCOUNTS_FILE", str(tmp_path / "accounts_cli.txt"))
    monkeypatch.setattr(store, "_ROOT", str(tmp_path))
    monkeypatch.setattr(store, "_db_initialized", False)
    monkeypatch.setattr(store, "DUAL_WRITE_TEXT", dual_write)


def test_mark_used_and_is_email_used(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    used = tmp_path / "emails_used.txt"

    assert store.is_email_used("a@example.com") is False
    store.mark_used("A@example.com", "pw1")
    assert used.read_text(encoding="utf-8").strip() == "A@example.com----pw1----ok"
    assert store.is_email_used("a@example.com") is True


def test_mark_error_requires_at_and_truncates_reason(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    err = tmp_path / "emails_error.txt"

    store.mark_error("not-an-email", reason="x")
    assert not err.exists() or err.read_text(encoding="utf-8") == ""

    long_reason = "r" * 500 + "\nnewline"
    store.mark_error("b@example.com", password="p", reason=long_reason)
    line = err.read_text(encoding="utf-8").strip()
    parts = line.split("----")
    assert parts[0] == "b@example.com"
    assert parts[1] == "p"
    assert "\n" not in parts[2]
    assert len(parts[2]) <= 200
    assert store.is_email_used("B@example.com") is True


def test_collect_local_consumed_emails(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    (tmp_path / "emails_used.txt").write_text("u@x.com----p----ok\n", encoding="utf-8")
    (tmp_path / "emails_error.txt").write_text("e@x.com----p----bad\n", encoding="utf-8")
    # seed sqlite via migrate so collect sees them even if text dual-read misses
    store.migrate_text_ledgers_into_sqlite()
    found = store.collect_local_consumed_emails({})
    assert "u@x.com" in found
    assert "e@x.com" in found
