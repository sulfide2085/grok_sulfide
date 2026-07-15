import providers
from providers.cloudflare import CloudflareProvider
from providers.cloudmail import CloudMailProvider
from providers.duckmail import DuckMailProvider
from providers.hotmail import HotmailProvider
from providers.tempmail import TempmailProvider
from providers.yyds import YydsProvider


def test_registry_aliases():
    assert isinstance(providers.get_provider("hotmail"), HotmailProvider)
    assert isinstance(providers.get_provider("outlook"), HotmailProvider)
    assert isinstance(providers.get_provider("microsoft"), HotmailProvider)
    assert isinstance(providers.get_provider("duckmail"), DuckMailProvider)
    assert isinstance(providers.get_provider("cloudflare"), CloudflareProvider)
    assert isinstance(providers.get_provider("yyds"), YydsProvider)
    assert isinstance(providers.get_provider("cloudmail"), CloudMailProvider)
    assert isinstance(providers.get_provider("tempmail"), TempmailProvider)
    assert isinstance(providers.get_provider("tempmail.lol"), TempmailProvider)


def test_unknown_falls_back_to_duckmail():
    assert isinstance(providers.get_provider("not-a-real-provider"), DuckMailProvider)


def test_provider_names():
    assert HotmailProvider.name == "hotmail"
    assert DuckMailProvider.name == "duckmail"
    assert TempmailProvider.name == "tempmail"
