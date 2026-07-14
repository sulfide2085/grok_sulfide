import threading
from concurrent.futures import ThreadPoolExecutor

import store


def test_sqlite_mark_used_and_is_email_used(tmp_path, monkeypatch):
    db = tmp_path / "state.db"
    used = tmp_path / "emails_used.txt"
    err = tmp_path / "emails_error.txt"
    monkeypatch.setattr(store, "_DB_PATH", str(db))
    monkeypatch.setattr(store, "_EMAILS_USED_FILE", str(used))
    monkeypatch.setattr(store, "_EMAILS_ERROR_FILE", str(err))
    monkeypatch.setattr(store, "_db_initialized", False)
    monkeypatch.setattr(store, "DUAL_WRITE_TEXT", True)

    assert store.is_email_used("a@example.com") is False
    store.mark_used("A@example.com", "p----w")  # password may contain ----
    assert store.is_email_used("a@example.com") is True
    # dual-write still happened
    assert "A@example.com" in used.read_text(encoding="utf-8")


def test_sqlite_survives_password_with_delimiter(tmp_path, monkeypatch):
    db = tmp_path / "state.db"
    monkeypatch.setattr(store, "_DB_PATH", str(db))
    monkeypatch.setattr(store, "_EMAILS_USED_FILE", str(tmp_path / "u.txt"))
    monkeypatch.setattr(store, "_EMAILS_ERROR_FILE", str(tmp_path / "e.txt"))
    monkeypatch.setattr(store, "_db_initialized", False)
    monkeypatch.setattr(store, "DUAL_WRITE_TEXT", False)

    store.mark_used("x@y.com", "a----b----c")
    assert store.is_email_used("x@y.com") is True


def test_concurrent_mark_used(tmp_path, monkeypatch):
    db = tmp_path / "state.db"
    monkeypatch.setattr(store, "_DB_PATH", str(db))
    monkeypatch.setattr(store, "_EMAILS_USED_FILE", str(tmp_path / "u.txt"))
    monkeypatch.setattr(store, "_EMAILS_ERROR_FILE", str(tmp_path / "e.txt"))
    monkeypatch.setattr(store, "_db_initialized", False)
    monkeypatch.setattr(store, "DUAL_WRITE_TEXT", False)

    # Initialize schema once on the main thread before fan-out.
    assert store.is_email_used("init@ex.com") is False

    def work(i: int):
        store.mark_used(f"user{i}@ex.com", f"pw{i}")

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(work, range(20)))

    for i in range(20):
        assert store.is_email_used(f"user{i}@ex.com")


def test_record_account_upsert(tmp_path, monkeypatch):
    db = tmp_path / "state.db"
    monkeypatch.setattr(store, "_DB_PATH", str(db))
    monkeypatch.setattr(store, "_db_initialized", False)
    monkeypatch.setattr(store, "DUAL_WRITE_TEXT", False)

    store.record_account("a@x.com", "pw", "sso1")
    store.record_account("a@x.com", "pw2", "sso2")
    found = store.collect_local_consumed_emails({})
    assert "a@x.com" in found
    import sqlite3

    conn = sqlite3.connect(db)
    row = conn.execute("SELECT password, sso FROM accounts WHERE email=?", ("a@x.com",)).fetchone()
    conn.close()
    assert row == ("pw2", "sso2")


def test_migrate_text_into_sqlite(tmp_path, monkeypatch):
    used = tmp_path / "emails_used.txt"
    err = tmp_path / "emails_error.txt"
    accounts = tmp_path / "accounts_cli.txt"
    used.write_text("u@x.com----p----ok\n", encoding="utf-8")
    err.write_text("e@x.com----p----bad\n", encoding="utf-8")
    accounts.write_text("a@x.com----p----sso123\n", encoding="utf-8")
    db = tmp_path / "state.db"
    monkeypatch.setattr(store, "_DB_PATH", str(db))
    monkeypatch.setattr(store, "_EMAILS_USED_FILE", str(used))
    monkeypatch.setattr(store, "_EMAILS_ERROR_FILE", str(err))
    monkeypatch.setattr(store, "_ACCOUNTS_FILE", str(accounts))
    monkeypatch.setattr(store, "_db_initialized", False)

    stats = store.migrate_text_ledgers_into_sqlite()
    assert stats["used"] == 1 and stats["error"] == 1 and stats["accounts"] == 1
    assert store.is_email_used("u@x.com")
    assert store.is_email_used("e@x.com")
