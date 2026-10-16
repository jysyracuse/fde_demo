import pytest

from arfollow.agent import calculate

SEEN = '{"amount": "AUD 149.00"} {"amount": "AUD 80.00"} {"amount": "AUD 69.00"} ' \
       '{"users_billed": 14, "usual": 4, "unit_price": "AUD 10.00"}'


def test_exact_cents_arithmetic_on_retrieved_values():
    assert calculate("AUD 149.00 - AUD 80.00 + AUD 69.00", SEEN)["result"] == "AUD 138.00"
    assert calculate("(14 - 4) * AUD 10.00", SEEN)["result"] == "AUD 100.00"
    assert calculate("14 - 4", SEEN)["result"] == 10


@pytest.mark.parametrize("expr, why", [
    ("AUD 149.00 - AUD 90.00", "not in retrieved"),      # operand never retrieved
    ("15 * AUD 10.00", "not in retrieved"),              # count never retrieved
    ("AUD 149.00 - NZD 80.00", "mixed currencies"),
    ("AUD 149.00 * AUD 80.00", "money by money"),
    ("AUD 149.00 + 4", "count to money"),
    ("14 ** 4", "unsupported"),
    ("__import__('os')", "unsupported"),
])
def test_rejects_untraced_operands_and_unsafe_or_meaningless_expressions(expr, why):
    with pytest.raises(ValueError, match=why):
        calculate(expr, SEEN)
