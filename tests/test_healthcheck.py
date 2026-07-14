import healthcheck as hc


def test_mail_config_hotmail_missing_file(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    try:
        hc.check_mail_config(
            {
                "registration_method": "browser",
                "email_provider": "hotmail",
                "hotmail_accounts_file": "missing.txt",
            }
        )
        assert False, "expected HealthCheckError"
    except hc.HealthCheckError as e:
        assert "missing" in str(e)


def test_mail_config_protocol_moemail_requires_key():
    try:
        hc.check_mail_config(
            {"registration_method": "protocol", "protocol_email_provider": "moemail"}
        )
        assert False
    except hc.HealthCheckError as e:
        assert "protocol_moemail_api_key" in str(e)


def test_signup_reachable_with_mock():
    class Resp:
        status_code = 200
        text = "<html>accounts.x.ai sign-up email grok</html>"

    out = hc.check_signup_reachable(http_get=lambda url, **k: Resp())
    assert out["ok"] is True


def test_signup_unreachable_raises():
    def boom(url, **k):
        raise OSError("connection refused")

    try:
        hc.check_signup_reachable(http_get=boom)
        assert False
    except hc.HealthCheckError as e:
        assert "unreachable" in str(e)


def test_run_preflight_skip_network(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    mail = tmp_path / "mail_credentials.txt"
    mail.write_text("a@b.com----x----cid----rt\n", encoding="utf-8")
    results = hc.run_preflight(
        {
            "registration_method": "browser",
            "email_provider": "hotmail",
            "hotmail_accounts_file": "mail_credentials.txt",
        },
        skip_network=True,
    )
    assert results and results[0]["ok"] is True
