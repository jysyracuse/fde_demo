"""Minimal Salesforce REST client using the OAuth 2.0 client credentials flow.

Read-only by default. Write methods exist only for the demo data loader and refuse to run
unless the client was constructed with ``allow_write=True``; nothing in the runtime system
(layers 1-3) constructs a writable client.
"""
from __future__ import annotations

from typing import Any, Iterator

import requests

from . import config


class SalesforceError(RuntimeError):
    pass


class SalesforceClient:
    def __init__(self, *, allow_write: bool = False, timeout: float = 30.0):
        self.domain = config.require("SF_DOMAIN").removeprefix("https://").rstrip("/")
        if "@" in self.domain:
            raise SystemExit("SF_DOMAIN looks like a username; use the My Domain host from "
                             "Setup → My Domain, e.g. yourname-dev-ed.develop.my.salesforce.com")
        if self.domain in ("login.salesforce.com", "test.salesforce.com"):
            raise SystemExit("SF_DOMAIN must be your My Domain host; client credentials "
                             "does not work on login/test.salesforce.com")
        self.client_id = config.require("SF_CLIENT_ID")
        self.client_secret = config.require("SF_CLIENT_SECRET")
        self.allow_write = allow_write
        self.timeout = timeout
        self.session = requests.Session()
        self.instance_url: str | None = None
        self.api_version: str | None = config.optional("SF_API_VERSION") or None

    # --- auth -------------------------------------------------------------
    def authenticate(self) -> None:
        resp = requests.post(
            f"https://{self.domain}/services/oauth2/token",
            data={
                "grant_type": "client_credentials",
                "client_id": self.client_id,
                "client_secret": self.client_secret,
            },
            timeout=self.timeout,
        )
        if resp.status_code != 200:
            raise SalesforceError(f"Token request failed ({resp.status_code}): {resp.text}")
        body = resp.json()
        self.instance_url = body["instance_url"]
        self.session.headers["Authorization"] = f"Bearer {body['access_token']}"
        if not self.api_version:
            versions = self._raw("GET", f"{self.instance_url}/services/data/").json()
            self.api_version = versions[-1]["version"]

    def _ensure_auth(self) -> None:
        if self.instance_url is None:
            self.authenticate()

    def _raw(self, method: str, url: str, **kw) -> requests.Response:
        resp = self.session.request(method, url, timeout=self.timeout, **kw)
        if resp.status_code == 401 and "Authorization" in self.session.headers:
            self.authenticate()  # token expired: client credentials has no refresh token
            resp = self.session.request(method, url, timeout=self.timeout, **kw)
        if resp.status_code >= 400:
            raise SalesforceError(f"{method} {url} -> {resp.status_code}: {resp.text[:500]}")
        return resp

    def _url(self, path: str) -> str:
        self._ensure_auth()
        return f"{self.instance_url}/services/data/v{self.api_version}/{path.lstrip('/')}"

    # --- read -------------------------------------------------------------
    def get(self, path: str, **params) -> dict[str, Any]:
        return self._raw("GET", self._url(path), params=params).json()

    def query(self, soql: str) -> Iterator[dict[str, Any]]:
        """Yield all records for a SOQL query, following pagination."""
        body = self.get("query", q=soql)
        while True:
            for rec in body["records"]:
                rec.pop("attributes", None)
                yield rec
            if body.get("done", True):
                return
            body = self._raw("GET", f"{self.instance_url}{body['nextRecordsUrl']}").json()

    def userinfo(self) -> dict[str, Any]:
        self._ensure_auth()
        return self._raw("GET", f"{self.instance_url}/services/oauth2/userinfo").json()

    # --- write (data loader only) ----------------------------------------
    def _check_write(self) -> None:
        if not self.allow_write:
            raise PermissionError("This Salesforce client is read-only")

    def create_many(self, sobject: str, records: list[dict[str, Any]]) -> list[str]:
        """Insert records in chunks of 200 (sObject Collections). Returns new IDs in order."""
        self._check_write()
        ids: list[str] = []
        for i in range(0, len(records), 200):
            chunk = [{"attributes": {"type": sobject}, **r} for r in records[i:i + 200]]
            results = self._raw("POST", self._url("composite/sobjects"),
                                json={"allOrNone": True, "records": chunk}).json()
            for r in results:
                if not r.get("success"):
                    raise SalesforceError(f"Insert {sobject} failed: {r.get('errors')}")
                ids.append(r["id"])
        return ids

    def update_many(self, sobject: str, records: list[dict[str, Any]]) -> int:
        """Update fields on existing records (each has "Id") in chunks of 200."""
        self._check_write()
        updated = 0
        for i in range(0, len(records), 200):
            chunk = [{"attributes": {"type": sobject}, **r} for r in records[i:i + 200]]
            results = self._raw("PATCH", self._url("composite/sobjects"),
                                json={"allOrNone": True, "records": chunk}).json()
            for r in results:
                if not r.get("success"):
                    raise SalesforceError(f"Update {sobject} failed: {r.get('errors')}")
            updated += len(results)
        return updated

    def delete_many(self, ids: list[str]) -> int:
        self._check_write()
        deleted = 0
        for i in range(0, len(ids), 200):
            chunk = ids[i:i + 200]
            results = self._raw("DELETE", self._url("composite/sobjects"),
                                params={"ids": ",".join(chunk), "allOrNone": "false"}).json()
            deleted += sum(1 for r in results if r.get("success"))
        return deleted
