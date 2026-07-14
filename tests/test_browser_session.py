import browser as br


def test_browser_session_properties_without_browser():
    session = br.BrowserSession()
    assert session.browser is None
    assert session.page is None
    assert session.served == 0


def test_current_session_returns_browser_session():
    session = br.current_session()
    assert isinstance(session, br.BrowserSession)


def test_module_aliases_match_session_api():
    assert br.get_browser is br._get_browser
    assert br.get_page is br._get_page
    assert callable(br.start_browser)
    assert callable(br.stop_browser)
    assert callable(br.prepare_browser_for_next_account)
