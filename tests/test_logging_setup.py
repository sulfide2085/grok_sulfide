import logging_setup as ls


def test_redact_tokens_and_passwords():
    raw = "sso=abc123xyz password: secret99 refresh_token: RRR"
    out = ls.redact_log_text(raw)
    assert "abc123xyz" not in out
    assert "secret99" not in out
    assert "RRR" not in out
    assert "***" in out


def test_init_idempotent(tmp_path):
    log1 = ls.init(log_dir=tmp_path)
    log2 = ls.init(log_dir=tmp_path)
    assert log1 is log2
    log1.info("sso=should-be-redacted")
    content = (tmp_path / "grok_sulfide.log").read_text(encoding="utf-8")
    assert "should-be-redacted" not in content
    assert "sso" in content.lower()
