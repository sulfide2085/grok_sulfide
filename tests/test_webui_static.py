import webui_service as svc


def test_resolve_static_module_assets():
    app = svc.resolve_static_path("/assets/js/app.js")
    api = svc.resolve_static_path("/assets/js/api.js")
    css = svc.resolve_static_path("/assets/styles.css")
    index = svc.resolve_static_path("/")
    assert app is not None and app.name == "app.js"
    assert api is not None and api.name == "api.js"
    assert css is not None and css.name == "styles.css"
    assert index is not None and index.name == "index.html"


def test_resolve_static_rejects_escape_and_missing():
    assert svc.resolve_static_path("/assets/../config.json") is None
    assert svc.resolve_static_path("/assets/js/does-not-exist.js") is None
    assert svc.resolve_static_path("/api/status") is None


def test_index_loads_es_module_entry():
    html = (svc.WEB_ROOT / "index.html").read_text(encoding="utf-8")
    assert 'type="module"' in html
    assert "/assets/js/app.js" in html
    assert 'id="rememberDraftButton"' in html
    assert 'id="clearDraftButton"' in html
    assert 'value="tempmail"' in html
