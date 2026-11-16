"""Pay guard — hard-coded OFF. This is not a config toggle.

Crocbot X moves $0. Pay stays disabled until the founder gives a
written go, and that go is implemented by editing this file, not by
flipping an env var. Any code path that would move money must call
:func:`attempt_pay` and will be refused here.
"""
from __future__ import annotations

# Hard-coded. Do NOT make this an env var or a config flag.
PAY_ENABLED = False
AMOUNT_MOVED_USD = 0.0


class PayBlockedError(RuntimeError):
    """Raised for any attempted payment while the guard is on."""


def assert_pay_disabled() -> None:
    if PAY_ENABLED:  # pragma: no cover - unreachable by design
        raise RuntimeError("PAY_ENABLED must never be True in this build")


def attempt_pay(amount_usd: float, recipient: str) -> None:
    """Refuse any payment. Always raises while the guard is on."""
    raise PayBlockedError(
        f"pay is disabled (hard guard): refusing ${amount_usd:.2f} to {recipient!r}; "
        "$0 moved"
    )
