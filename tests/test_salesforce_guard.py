"""Salesforce read-only guard: a default client must refuse writes before any HTTP call."""
import pytest
import requests

from arfollow.salesforce import SalesforceClient


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("SF_DOMAIN", "example-dev-ed.develop.my.salesforce.com")
    monkeypatch.setenv("SF_CLIENT_ID", "id")
    monkeypatch.setenv("SF_CLIENT_SECRET", "secret")

    def no_http(*a, **kw):
        raise AssertionError("HTTP call made")
    monkeypatch.setattr(requests, "post", no_http)
    monkeypatch.setattr(requests.Session, "request", no_http)


def test_default_client_refuses_writes(env):
    sf = SalesforceClient()
    with pytest.raises(PermissionError):
        sf.create_many("Account", [{"Name": "x"}])
    with pytest.raises(PermissionError):
        sf.delete_many(["001000000000001"])


@pytest.mark.parametrize("domain", ["someone@example.com", "login.salesforce.com"])
def test_rejects_non_my_domain(env, monkeypatch, domain):
    monkeypatch.setenv("SF_DOMAIN", domain)
    with pytest.raises(SystemExit):
        SalesforceClient()
