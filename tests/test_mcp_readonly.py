import asyncio
import sqlite3

import pytest
from mcp import Client

from arfollow import config, mcp_server, store

READ_TOOLS = {"get_customer", "list_invoices", "get_invoice_lines", "compare_seats",
              "list_payments", "list_credit_notes", "list_crm_cases", "list_crm_activities",
              "list_replies"}


def test_server_exposes_only_the_read_tools():
    async def names():
        async with Client(mcp_server.server) as c:
            return {t.name for t in (await c.list_tools()).tools}
    assert asyncio.run(names()) == READ_TOOLS


@pytest.mark.skipif(not config.APP_DB.exists() or not config.BILLING_DB.exists(),
                    reason="needs data/app.db and billing.db")
@pytest.mark.parametrize("conn", [mcp_server._app, store.billing])
def test_server_connections_reject_writes(conn):
    with pytest.raises(sqlite3.OperationalError, match="readonly"):
        conn().execute("CREATE TABLE x (a)")
