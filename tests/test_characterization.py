from grok_register_ttk import extract_verification_code, generate_username


def test_code_from_subject_hyphenated():
    assert extract_verification_code("", "ABC-123 xAI Verification") == "ABC-123"


def test_code_from_body_hyphenated():
    assert extract_verification_code("Your code is XY9-2K7, thanks") == "XY9-2K7"


def test_code_numeric_fallback():
    assert extract_verification_code("verification code: 482913") == "482913"


def test_code_absent_returns_none():
    assert extract_verification_code("no code in this text") is None


def test_generate_username_length_and_charset():
    u = generate_username(10)
    assert len(u) == 10 and u.isalnum()
