"""In-memory 'core banking' data + deterministic scenario triggers.

Each member_id below is a fixed, reproducible scenario so replay demos are
repeatable: valid lookups, a not-found, a validation error, a permission
denial, a first-request-slow record, a first-request-interstitial record,
and a record that deterministically triggers a server error (hard failure).
"""
from __future__ import annotations

MEMBERS: dict[str, dict] = {
    "10001": {"name": "Jordan Rivera", "savings_balance": "4230.55", "checking_balance": "1875.20"},
    "10002": {"name": "Alicia Chen", "savings_balance": "812.10", "checking_balance": "3020.00"},
    "50000": {"name": "Marcus Webb", "savings_balance": "0.00", "checking_balance": "0.00", "restricted": True},
    "60000": {"name": "Priya Nair", "savings_balance": "15920.44", "checking_balance": "410.75"},
    "70000": {"name": "Sam Okafor", "savings_balance": "2200.00", "checking_balance": "990.10"},
}

# Members whose first detail-page view is deliberately slow (simulating a
# transient backend slowdown). Cleared after first successful serve.
_SLOW_ONCE: set[str] = {"60000"}
_slow_served: set[str] = set()

# Members whose first detail-page view shows an interstitial (e.g. a policy
# notice) that must be dismissed before the page content is usable.
_INTERSTITIAL_ONCE: set[str] = {"70000"}
_interstitial_served: set[str] = set()

HARD_FAILURE_MEMBER_ID = "99999999"  # deterministically triggers a simulated server error


def get_member(member_id: str) -> dict | None:
    return MEMBERS.get(member_id)


def is_restricted(member_id: str) -> bool:
    m = MEMBERS.get(member_id, {})
    return bool(m.get("restricted"))


def should_be_slow(member_id: str) -> bool:
    if member_id in _SLOW_ONCE and member_id not in _slow_served:
        _slow_served.add(member_id)
        return True
    return False


def should_show_interstitial(member_id: str) -> bool:
    return member_id in _INTERSTITIAL_ONCE and member_id not in _interstitial_served


def mark_interstitial_dismissed(member_id: str) -> None:
    _interstitial_served.add(member_id)


def reset_scenarios() -> None:
    _slow_served.clear()
    _interstitial_served.clear()
