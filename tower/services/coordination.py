"""The coordination tax: how long a client's decisions wait on people, and how much the tower decides itself.

Supply chains rarely lack data; they lose time routing decisions through people. This measures that for one
client over a period, from the approvals the tower already keeps: requests decided by a person and how long
they waited, requests approved on their own (earned autonomy), spot checks people rejected, and person
decisions per 100 orders. The kind of request that cost the most waiting is the next one worth automating.
The weekly review carries it (agents/reporting.py).
"""
from __future__ import annotations

from datetime import date, datetime
from statistics import median
from typing import Any

from tower.core.models import ApprovalCategory
from tower.core.ports import DocumentStore
from tower.core.tenants import TENANT_ONE

from .autonomy import AUTONOMY


def _at(v: Any) -> datetime | None:
    return datetime.fromisoformat(v) if isinstance(v, str) else v


def _of(doc: dict[str, Any], tenant_id: str) -> bool:
    return (doc.get("tenant_id") or TENANT_ONE) == tenant_id


def _in(v: Any, start: date, end: date) -> bool:
    t = _at(v)
    return t is not None and start <= t.date() <= end


def _hours(a: datetime, b: datetime) -> float:
    return max((b - a).total_seconds(), 0) / 3600


def _p90(xs: list[float]) -> float:
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(0.9 * len(xs)))]


def coordination(store: DocumentStore, tenant_id: str, start: date, end: date, now: datetime) -> dict[str, Any]:
    reqs = [a for a in store.query("approvals") if _of(a, tenant_id) and _in(a.get("created_at"), start, end)]
    auto = [a for a in reqs if a.get("decided_by") == AUTONOMY]
    person = [a for a in reqs if a.get("decided_at") and a.get("decided_by") not in (None, AUTONOMY)]
    pending = [a for a in reqs if a["status"] == "pending"]
    waits = [_hours(_at(a["created_at"]), _at(a["decided_at"])) for a in person]
    orders = [o for o in store.query("orders") if _of(o, tenant_id) and _in(o.get("placed_at"), start, end)]

    def waited(a: dict[str, Any]) -> float:
        done = _at(a["decided_at"]) if a in person else now if a in pending else None
        return _hours(_at(a["created_at"]), done) if done else 0.0

    kinds = []
    for cat in sorted({a["category"] for a in reqs}):
        mine = [a for a in reqs if a["category"] == cat]
        mine_waits = [waited(a) for a in mine if a in person]
        kinds.append({"kind": ApprovalCategory(cat).value.replace("_", " "), "requests": len(mine),
                      "automatic": sum(a in auto for a in mine), "by_person": len(mine_waits),
                      "median_wait_hours": round(median(mine_waits), 1) if mine_waits else None,
                      "hours_waiting": round(sum(waited(a) for a in mine), 1)})
    kinds.sort(key=lambda k: -k["hours_waiting"])
    decided = len(auto) + len(person)
    return {
        "requests": len(reqs), "automatic": len(auto), "by_person": len(person), "still_waiting": len(pending),
        "automatic_share": round(len(auto) / decided, 3) if decided else None,
        "median_wait_hours": round(median(waits), 1) if waits else None,
        "p90_wait_hours": round(_p90(waits), 1) if waits else None,
        "hours_waiting": round(sum(waited(a) for a in reqs), 1),
        "spot_checks_rejected": sum(bool((a.get("auto") or {}).get("spot_check")) and a["status"] == "rejected"
                                    for a in reqs),
        "orders": len(orders),
        "person_decisions_per_100_orders": round(100 * len(person) / len(orders), 1) if orders else None,
        "by_kind": kinds,
        "next_to_automate": next((k["kind"] for k in kinds if k["by_person"]), None),
    }


def render(c: dict[str, Any]) -> list[str]:
    """The weekly review's 'Coordination tax' section, as markdown lines."""
    if not c["requests"]:
        return ["- No decisions needed this period."]
    share = "n/a" if c["automatic_share"] is None else f"{c['automatic_share']:.0%}"
    lines = [f"- {c['requests']} decisions: {c['automatic']} made by the tower on its own ({share}), "
             f"{c['by_person']} by a person, {c['still_waiting']} still waiting.",
             f"- People's decisions waited {c['median_wait_hours'] if c['median_wait_hours'] is not None else 'n/a'} "
             f"hours (median), {c['p90_wait_hours'] if c['p90_wait_hours'] is not None else 'n/a'} at the 90th "
             f"percentile; {c['hours_waiting']} hours of waiting in all.",
             f"- Person decisions per 100 orders: "
             f"{c['person_decisions_per_100_orders'] if c['person_decisions_per_100_orders'] is not None else 'n/a'}"
             f" ({c['orders']} orders).",
             f"- Spot checks people rejected: {c['spot_checks_rejected']}."]
    if c["next_to_automate"]:
        lines.append(f"- Most waiting: {c['next_to_automate']}. It is the next kind worth automating.")
    lines += ["", "| Kind | Requests | On its own | By a person | Median wait (h) | Hours waiting |",
              "|---|---|---|---|---|---|"]
    lines += [f"| {k['kind']} | {k['requests']} | {k['automatic']} | {k['by_person']} | "
              f"{'n/a' if k['median_wait_hours'] is None else k['median_wait_hours']} | {k['hours_waiting']} |"
              for k in c["by_kind"]]
    return lines
