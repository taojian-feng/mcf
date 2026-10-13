"""Earned autonomy evaluation: if people had decided every reship request by what really happened, which
requests would the tower have approved on its own, and would any of those have been wrong?

Runs the simulated world, records every signal that can make the exception agent ask for a reship (the
carrier says the parcel is lost, or it can no longer project a delivery), and labels each with what really
happened to the parcel. Each request then goes through the approval workflow in the order it arrived:
Claude writes the brief from the order's record, earned autonomy approves it on its own when the lane
qualifies (services/autonomy.py), and otherwise a simulated person decides it correctly (approve the reship
only if the parcel really was lost). Spot checks go to that person too.

The requests are deliberately harder than live ones: they come from the signal alone, so many are parcels
that were only late. What matters is "approved on its own, wrongly": a reship nobody needed.

  python -m evals.autonomy --days 14            # rules briefs only: nothing is ever automatic
  python -m evals.autonomy --claude             # on Azure, 42 days, with Claude writing the briefs
"""
from __future__ import annotations

import argparse
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from evals.exceptions import Metered, Sample, collect
from tower.core.config import PilotConfig
from tower.core.models import ApprovalCategory, ApprovalStatus, ExceptionCategory, OwningParty
from tower.core.ports import LLM
from tower.services.autonomy import AUTONOMY

LOST = ExceptionCategory.LOST_SHIPMENT.value
ASKS_FOR_RESHIP = ("carrier_lost", "projected_delivery_unavailable")
PERSON = "alice@corpb"
PERSON_MINUTES = 4          # a person reading a brief and the order before deciding
PRICE_IN, PRICE_OUT = 4.00, 20.00


@dataclass
class Result:
    requests: int = 0
    really_lost: int = 0
    claude_agreed: int = 0          # Claude's suggestion matched the right decision (clear suggestions only)
    claude_clear: int = 0
    auto: int = 0
    auto_wrong: int = 0
    spot_checks: int = 0
    to_person: int = 0
    lost_autonomy: int = 0
    first_auto: int | None = None   # which request was the first approved on its own
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    seconds: float = 0
    wrong: list[dict[str, Any]] = field(default_factory=list)

    @property
    def cost(self) -> float:
        return (self.input_tokens * PRICE_IN + self.output_tokens * PRICE_OUT) / 1e6


def requests(samples: list[Sample]) -> list[Sample]:
    """One request per order: its first signal that asks for a reship, in the order they arrived."""
    asks = [s for s in samples if s.signal.signal_type in ASKS_FOR_RESHIP and s.order_snapshot]
    asks = sorted(asks, key=lambda s: s.signal.received_at or datetime.min.replace(tzinfo=timezone.utc))
    seen: set[str] = set()
    return [s for s in asks if not (s.signal.order_id in seen or seen.add(s.signal.order_id))]


def replay(asks: list[Sample], llm: LLM | None, config: PilotConfig | None = None) -> Result:
    from tower.adapters.memory import StaticDirectory
    from tower.runtime import build, memory_ports

    directory = StaticDirectory(roles={PERSON: {"approver:exception_resolution:corp_b"}})
    ports = memory_ports(directory=directory, llm=llm)
    system = build(ports, config or PilotConfig(), sleep=lambda s: None)
    approvals, res = system.approvals, Result(requests=len(asks))
    meter = Metered(llm) if llm is not None else None
    before, started = (meter.usage() if meter else (0, 0, 0)), time.monotonic()
    for n, s in enumerate(asks, 1):
        ports.bus.advance(timedelta(hours=2))
        ports.store.put("orders", s.signal.order_id, {**s.order_snapshot, "order_id": s.signal.order_id})
        right = s.truth == LOST
        res.really_lost += right
        item = approvals.submit(
            category=ApprovalCategory.EXCEPTION_RESOLUTION, owning_party=OwningParty.CORP_B, agent="exception_agent",
            subject_id=f"case-{n}", tenant_id="corp_b",
            content={"order_id": s.signal.order_id, "playbook": "PB-LOST", "version": "1.0",
                     "steps": ["request_reship", "notify_oms_reship"], "signal": s.signal.model_dump(mode="json")},
            rationale=f"signal {s.signal.signal_type}: the parcel looks lost; reship it")
        said = (item.brief or {}).get("suggestion")
        if said in ("approve", "reject") and (item.brief or {}).get("source") == "claude":
            res.claude_clear += 1
            res.claude_agreed += (said == "approve") == right
        if item.decided_by == AUTONOMY:
            res.auto += 1
            res.first_auto = res.first_auto or n
            if not right:
                res.auto_wrong += 1
                res.wrong.append({"order": s.signal.order_id, "signal": s.signal.signal_type, "really": s.truth,
                                  "why": (item.brief or {}).get("suggestion_reason", "")[:160]})
            continue
        res.to_person += 1
        res.spot_checks += bool(item.auto and item.auto.get("spot_check"))
        ports.bus.advance(timedelta(minutes=30))
        approvals.decide(PERSON, item.approval_id, approve=right, reason="" if right else "the parcel was only late")
        res.lost_autonomy += bool(item.auto) and not right
    res.seconds = time.monotonic() - started
    if meter:
        res.calls, res.input_tokens, res.output_tokens = (a - b for a, b in zip(meter.usage(), before))
    assert all(d["status"] != ApprovalStatus.PENDING.value for d in ports.store.query("approvals"))
    return res


def report(r: Result, *, days: float, model: str | None, config: PilotConfig) -> str:
    pct = lambda a, b: f"{a / b:.0%}" if b else "n/a"  # noqa: E731
    saved = r.auto * PERSON_MINUTES
    lines = [
        "# Earned autonomy evaluation", "",
        f"Run {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC: {days:g} days, {r.requests} reship requests "
        f"({r.really_lost} parcels really lost, {r.requests - r.really_lost} only late). "
        f"Briefs by {model or 'rules (no Claude)'}.", "",
        f"Rule: approve on its own after {config.autonomy_min_decisions} decisions with at least "
        f"{config.autonomy_agreement:.0%} agreement and no rejected approval suggestion; one in "
        f"{config.autonomy_spot_check_every} still to a person.", "",
        "| Requests | Claude agreed with the right call | Approved on its own | Wrongly approved on its own "
        "| To a person (spot checks) | Autonomy lost | First automatic | Person time saved | Claude cost |",
        "|---|---|---|---|---|---|---|---|---|",
        f"| {r.requests} | {r.claude_agreed}/{r.claude_clear} ({pct(r.claude_agreed, r.claude_clear)}) | {r.auto} "
        f"({pct(r.auto, r.requests)}) | {r.auto_wrong} | {r.to_person} ({r.spot_checks}) | {r.lost_autonomy} | "
        f"{'#' + str(r.first_auto) if r.first_auto else 'never'} | {saved // 60}h {saved % 60}m | ${r.cost:.2f} |", "",
        f"Person time saved assumes {PERSON_MINUTES} minutes per decision. Claude: {r.calls} calls, "
        f"{r.seconds / 60:.0f} minutes.",
    ]
    if r.wrong:
        lines += ["", "## Approved on its own, wrongly", ""]
        lines += [f"- {w['order']} ({w['signal']}): really {w['really']}. Claude: {w['why']}" for w in r.wrong]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> str:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--days", type=float, default=42)
    ap.add_argument("--orders-per-day", type=int, default=300)
    ap.add_argument("--catalog-size", type=int, default=60)
    ap.add_argument("--seed", type=int, default=31)
    ap.add_argument("--claude", action="store_true", help="Claude writes the briefs (needs Azure)")
    args = ap.parse_args(argv)
    llm, ports = None, None
    if args.claude:
        from tower.runtime import from_env
        ports = from_env()
        llm = ports.llm
        if llm is None:
            raise SystemExit("--claude needs Claude: deploy it in Foundry or put anthropic-api-key in Key Vault")
    config = PilotConfig()
    asks = requests(collect(args.days, args.orders_per_day, args.catalog_size, args.seed))
    text = report(replay(asks, llm, config), days=args.days, model=getattr(llm, "model", None), config=config)
    print(text)
    if ports is not None:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        ports.blobs.write("reports", f"evals/autonomy-{stamp}.md", text.encode(), content_type="text/markdown")
        ops = "ops-team"
        ports.notifier.send(ports.directory.contact(ops) or ops, "Earned autonomy evaluation", text)
    return text


if __name__ == "__main__":
    main(sys.argv[1:])
