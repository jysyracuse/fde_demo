import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from generate_data import AS_OF, SEED, SETS, Generator, _default  # noqa: E402


def build(name):
    g = Generator(SEED, AS_OF, SETS[name])
    g.run()
    out = json.loads(json.dumps(g.outputs(), default=_default))
    b = out["billing.json"]
    return {"customers": b["customers"], "invoices": b["invoices"], "lines": b["invoice_lines"],
            "payments": b["payments"], "charges": b["charge_attempts"],
            "credit_notes": b["credit_notes"], "inbox": out["inbox.json"],
            "accounts": out["salesforce.json"]["accounts"]}


def test_each_set_is_a_subset_of_the_next():
    names = list(SETS)
    sets = {n: build(n) for n in names}
    for small, big in zip(names, names[1:]):
        for key, rows in sets[small].items():
            bigger = [json.dumps(r, sort_keys=True) for r in sets[big][key]]
            missing = [r for r in rows if json.dumps(r, sort_keys=True) not in bigger]
            assert not missing, f"{small} -> {big}: {key} differs, e.g. {missing[0]}"
    ids = [i["id"] for i in sets["full"]["invoices"]]
    assert len(ids) == len(set(ids))
