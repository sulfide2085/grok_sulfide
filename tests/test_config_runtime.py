import config_runtime as cfg


def test_normalize_defaults_and_outlook_alias():
    out = cfg.normalize_runtime_config({"email_provider": "outlook", "hotmail_alias_mode": "main"})
    assert out["email_provider"] == "hotmail"
    assert out["hotmail_alias_mode"] == "primary"
    assert out["hotmail_max_aliases_per_account"] == 1


def test_cpa_proxy_falls_back_to_proxy():
    out = cfg.normalize_runtime_config({"proxy": "http://127.0.0.1:1", "cpa_proxy": ""})
    assert out["cpa_proxy"] == "http://127.0.0.1:1"


def test_invalid_registration_method_strict():
    try:
        cfg.validate_config({"registration_method": "telepathy"}, strict=True)
        assert False, "expected ConfigError"
    except cfg.ConfigError as e:
        assert "registration_method" in str(e)


def test_invalid_registration_method_non_strict_defaults():
    out = cfg.validate_config({"registration_method": "telepathy"}, strict=False)
    assert out["registration_method"] == "browser"


def test_threads_coerced_positive():
    out = cfg.validate_config({"register_threads": 0, "mail_timeout": -5}, strict=False)
    assert out["register_threads"] >= 1
    assert out["mail_timeout"] >= 1
