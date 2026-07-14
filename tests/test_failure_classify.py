from failure_classify import FailureClass, FailureStats, classify_failure


def test_classify_rate_limit():
    assert classify_failure("xAI 验证码发送过多") is FailureClass.RATE_LIMIT


def test_classify_already_registered():
    assert classify_failure("EmailAlreadyRegisteredError: account already exists") is FailureClass.ALREADY_REGISTERED


def test_classify_captcha():
    assert classify_failure("turnstile stuck") is FailureClass.CAPTCHA


def test_classify_mail_timeout():
    assert classify_failure("Cloudflare 在 150s 内未收到验证码邮件") is FailureClass.MAIL_TIMEOUT


def test_classify_dom():
    assert classify_failure("未进入验证码页（仍在邮箱表单）") is FailureClass.DOM_CHANGED


def test_classify_oauth():
    assert classify_failure("oauth2 refresh 失败 invalid_grant") is FailureClass.OAUTH_DEAD


def test_classify_network():
    assert classify_failure("Could not connect to server proxy 127.0.0.1") is FailureClass.NETWORK


def test_stats_summary():
    stats = FailureStats()
    stats.record("turnstile")
    stats.record("too many codes")
    stats.record("turnstile again")
    text = stats.summary()
    assert "CAPTCHA=2" in text
    assert "RATE_LIMIT=1" in text
