import pytest

from app.agent.payguard import AMOUNT_MOVED_USD, PAY_ENABLED, PayBlockedError, attempt_pay


def test_pay_is_hard_disabled():
    assert PAY_ENABLED is False
    assert AMOUNT_MOVED_USD == 0.0


def test_any_payment_attempt_is_refused():
    with pytest.raises(PayBlockedError, match="pay is disabled"):
        attempt_pay(1.00, "someone")
    with pytest.raises(PayBlockedError, match="\\$0 moved"):
        attempt_pay(0.00001, "someone")
