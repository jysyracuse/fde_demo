import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from eval_actions import ACTIONS, CHECK_ORDER, playbook  # noqa: E402


def test_playbook_sections_cover_every_check_and_action():
    _, checks, actions = playbook()
    assert list(checks) == [k for k, _ in CHECK_ORDER]
    assert set(actions) == set(ACTIONS)
    assert all(len(v) > 40 for v in [*checks.values(), *actions.values()])


def test_packet_rules_match_layer_one_on_the_full_set(tmp_path):
    import json

    from eval_actions import arm_rules, arm_rules_packet, cases
    from generate_data import AS_OF, SEED, Generator, _default
    g = Generator(SEED, AS_OF)
    g.run()
    for name, obj in g.outputs().items():
        (tmp_path / name).write_text(json.dumps(obj, default=_default))
    packets, _ = cases(tmp_path)
    layer1 = arm_rules(tmp_path)
    assert {p["customer_id"]: arm_rules_packet(p) for p in packets} == {
        p["customer_id"]: layer1[p["customer_id"]] for p in packets}
