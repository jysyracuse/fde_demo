import json
import sys
from pathlib import Path

import pytest

from arfollow import rules, store

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from generate_data import AS_OF, SEED, Generator, _default  # noqa: E402
from load_db import load  # noqa: E402


@pytest.fixture
def stores(tmp_path):
    """Full generated set in tmp: app conn, billing conn, today's saved follow-ups."""
    g = Generator(SEED, AS_OF)
    g.run()
    for name, obj in g.outputs().items():
        (tmp_path / name).write_text(json.dumps(obj, default=_default))
    load(tmp_path, tmp_path / "billing.db")
    conn, billing = store.app(tmp_path / "app.db"), store.billing(tmp_path / "billing.db")
    fus = rules.build_followups(conn, billing)
    rules.save_followups(conn, fus)
    return conn, billing, fus
