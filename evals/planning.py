"""Planning evaluation: who should make the planning calls, fixed rules or Claude?

Runs the whole simulated world (Corp B, the tower, Corp A) once per arm with the same seeds. After two warm-up
weeks some items stop following Corp B's forecast, as real items do, and the forecast does not catch up:

  take-off   three items sell 3x from a few days in
  fade       three items drop to a fifth
  shift      two items move west (2.5x west, 0.4x east)
  bulk       two slow items each get one large order (three weeks of their demand in one line), then go back
             to normal: a blip, not a trend
  steady     everything else follows the forecast

Each arm plans the same world differently (PilotConfig.plan_review):

  rules    the forecast and fixed rules (what runs today)
  sensing  rules, plus a fixed blend of recent orders into the plan when they drift from the forecast
  claude   Claude decides the demand for drifting items and reviews every restock and rebalancing plan

A simulated planner approves whatever reaches them, so the arms differ only in what they propose. The
scorecard counts what Corp A's network actually did: units on orders that could not ship for lack of stock,
units moved between nodes and the freight for them, stock left at the fulfillment centres beyond six weeks of
real demand, approvals people were asked for, and what Claude cost. Money weights are placeholders, shown.

  python -m evals.planning --arms rules,sensing          # no model needed
  python -m evals.planning --arms all                    # needs Claude (anthropic-api-key in Key Vault, or Foundry)
"""
from __future__ import annotations

import argparse
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from sim import catalog
from tower.core.models import ApprovalStatus
from tower.core.ports import LLM

ARMS = ("rules", "sensing", "claude")
PRICE_IN, PRICE_OUT = 4.00, 20.00     # Claude Opus 5.5 list prices, USD per million tokens
# Placeholder money weights, per unit
LOST_SALE_SHARE = 0.30                # margin lost on a unit that could not ship, as a share of unit cost
FREIGHT_IN_REGION, FREIGHT_CROSS = 0.50, 1.50
EXCESS_SHARE = 0.20                   # carrying cost and markdown risk of excess stock, as a share of its value
EXCESS_WEEKS = 6


@dataclass
class Scenario:
    groups: dict[str, list[str]]
    shifts: list[dict[str, Any]]
    bulk: list[dict[str, Any]]
    true_weekly: dict[str, dict[str, float]]    # after the shifts, per region


def scenario(catalog_size: int, start: datetime, warmup_days: int) -> Scenario:
    items = sorted(catalog.items(2026, catalog_size), key=lambda it: -sum(it.weekly_units.values()))
    pick = lambda ranks: [items[i] for i in ranks if i < len(items)]  # noqa: E731
    go = start + timedelta(days=warmup_days)
    groups = {"take-off": pick([2, 5, 8]), "fade": pick([0, 3, 6]), "shift": pick([1, 4]),
              "bulk": pick([len(items) // 2 + 3, len(items) // 2 + 7])}
    shifts, bulk = [], []
    for i, it in enumerate(groups["take-off"]):
        shifts.append({"upc": it.upc, "factor": 3.0, "from": go + timedelta(days=2 + 2 * i)})
    for i, it in enumerate(groups["fade"]):
        shifts.append({"upc": it.upc, "factor": 0.2, "from": go + timedelta(days=1 + 2 * i)})
    for it in groups["shift"]:
        shifts.append({"upc": it.upc, "factor": 2.5, "from": go + timedelta(days=3), "region": "west"})
        shifts.append({"upc": it.upc, "factor": 0.4, "from": go + timedelta(days=3), "region": "east"})
    for i, it in enumerate(groups["bulk"]):
        bulk.append({"at": go + timedelta(days=5 + 4 * i, hours=3), "upc": it.upc, "state": "CA",
                     "qty": max(6, round(sum(it.weekly_units.values()) * 3))})
    true_weekly = {}
    for it in items:
        true_weekly[it.upc] = {}
        for r in catalog.REGIONS:
            f = 1.0
            for s in shifts:
                if s["upc"] == it.upc and s.get("region") in (None, r):
                    f *= s["factor"]
            true_weekly[it.upc][r] = it.weekly_units[r] * f
    return Scenario({k: [it.upc for it in v] for k, v in groups.items()}, shifts, bulk, true_weekly)


@dataclass
class Result:
    arm: str
    ordered: int = 0
    lost: Counter = field(default_factory=Counter)        # by group
    ordered_by: Counter = field(default_factory=Counter)
    moved_in_region: int = 0
    moved_cross: int = 0
    excess_units: int = 0
    excess_value: float = 0.0
    lost_value: float = 0.0
    approvals: int = 0
    demand_calls: int = 0
    holds: int = 0
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    seconds: float = 0.0
    notes: list[str] = field(default_factory=list)

    @property
    def model_cost(self) -> float:
        return (self.input_tokens * PRICE_IN + self.output_tokens * PRICE_OUT) / 1e6

    @property
    def freight(self) -> float:
        return self.moved_in_region * FREIGHT_IN_REGION + self.moved_cross * FREIGHT_CROSS

    @property
    def total(self) -> float:
        return self.lost_value * LOST_SALE_SHARE + self.freight + self.excess_value * EXCESS_SHARE + self.model_cost


def run_arm(arm: str, *, days: int, warmup_days: int, orders_per_day: int, catalog_size: int, seed: int,
            llm: LLM | None) -> Result:
    from evals.world import START, build_world

    sc = scenario(catalog_size, START, warmup_days)
    res = Result(arm)
    started = time.monotonic()
    usage = lambda: (getattr(llm, "calls", 0), getattr(llm, "input_tokens", 0), getattr(llm, "output_tokens", 0))  # noqa: E731
    before = usage()
    with build_world(catalog_size=catalog_size, orders_per_day=orders_per_day, seed_a=seed, seed_b=seed + 1,
                     bad_record_rate=0.0, llm=llm if arm == "claude" else None, demand_shifts=sc.shifts,
                     bulk_orders=sc.bulk, config_overrides={"plan_review": arm}) as world:
        system, ran = world.system, set()
        measure_from = START + timedelta(days=warmup_days)
        for _ in range((warmup_days + days) * 24):
            world.run(hours=1)
            now = world.clock.now()
            if now.hour == 7 and now.date() not in ran:
                ran.add(now.date())
                system.run_job("planning_daily")
            for item in system.ports.store.query("approvals", status=ApprovalStatus.PENDING.value):
                if now >= measure_from:
                    res.approvals += 1
                system.approvals.decide("alice@corpb", item["approval_id"], approve=True)
        _score(world, sc, res, measure_from)
    res.seconds = time.monotonic() - started
    res.calls, res.input_tokens, res.output_tokens = (a - b for a, b in zip(usage(), before))
    return res


def _score(world, sc: Scenario, res: Result, since: datetime) -> None:
    group_of = {u: g for g, us in sc.groups.items() for u in us}
    cost = {it.upc: it.unit_cost for it in catalog.items(2026, len(sc.true_weekly))}
    for o in world.corpa.store.query("order"):
        if o.get("b2b") or o["received_at"] < since.isoformat():
            continue
        for it in o["request"]["items"]:
            g = group_of.get(it["sellerSku"], "steady")
            res.ordered += it["quantity"]
            res.ordered_by[g] += it["quantity"]
            if o["status"] == "UNFULFILLABLE":
                res.lost[g] += it["quantity"]
                res.lost_value += it["quantity"] * cost.get(it["sellerSku"], 0)
    from sim import geo
    region = {**{fc: r for r, fc in geo.REGION_FC.items()}, **{isc: r for r, isc in geo.REGION_ISC.items()}}
    for t in world.corpa.store.query("transfer"):
        if t["created_at"] < since.isoformat() or t["status"] == "cancelled":
            continue
        a, b = region.get(t["from"]), region.get(t["to"])
        if a is None or b is None or a == b:     # the cross-dock sits between the regions
            res.moved_in_region += t["qty"]
        else:
            res.moved_cross += t["qty"]
    for upc, weekly in sc.true_weekly.items():
        for r, fc in geo.REGION_FC.items():
            have = world.corpa._inv(upc, fc)["on_hand"]
            extra = max(0, have - weekly[r] * EXCESS_WEEKS)
            res.excess_units += round(extra)
            res.excess_value += extra * cost.get(upc, 0)
    store = world.ports.store
    res.demand_calls = sum(1 for r in world.system.audit.query(actor="planning_agent") if r.action == "demand_call")
    res.holds = len(store.query("replenishment_recs", status="held"))


def report(results: list[Result], sc: Scenario, *, days: int, orders_per_day: int, model: str | None) -> str:
    pct = lambda a, b: f"{100 * a / b:.1f}%" if b else "n/a"  # noqa: E731
    lines = [
        "# Planning evaluation",
        "",
        f"Run {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC: {days} measured days after two warm-up weeks, about "
        f"{orders_per_day} orders a day, {len(sc.true_weekly)} items. Model: {model or 'none'}.",
        "",
        "Items that stop following the forecast: " + "; ".join(f"{g} {len(us)}" for g, us in sc.groups.items()) + ".",
        "",
        "| Arm | Units that could not ship | Units moved (cross-region) | Freight | Excess stock at FCs | "
        "Approvals asked | Claude calls | Claude cost | Total cost score |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for r in results:
        lost = sum(r.lost.values())
        lines.append(f"| {r.arm} | {lost} ({pct(lost, r.ordered)}) | {r.moved_in_region + r.moved_cross} "
                     f"({r.moved_cross}) | ${r.freight:,.0f} | {r.excess_units} units, ${r.excess_value:,.0f} | "
                     f"{r.approvals} | {r.calls} | ${r.model_cost:.2f} | ${r.total:,.0f} |")
    lines += ["", f"Total cost score = {LOST_SALE_SHARE:.0%} of the value of units that could not ship + freight "
                  f"(${FREIGHT_IN_REGION:.2f} a unit in region, ${FREIGHT_CROSS:.2f} across) + {EXCESS_SHARE:.0%} of "
                  f"the value of stock beyond {EXCESS_WEEKS} weeks of real demand at the FCs + Claude. The weights "
                  "are placeholders; the raw numbers are next to them.", "",
              "## Units that could not ship, by kind of item", "",
              "| Arm | " + " | ".join(list(sc.groups) + ["steady"]) + " |",
              "|---|" + "---|" * (len(sc.groups) + 1)]
    for r in results:
        lines.append(f"| {r.arm} | " + " | ".join(f"{r.lost[g]} of {r.ordered_by[g]}" for g in list(sc.groups) + ["steady"]) + " |")
    lines += ["", "## How each arm planned", ""]
    for r in results:
        lines.append(f"- {r.arm}: {r.demand_calls} demand calls, {r.holds} restocks held, "
                     f"{r.seconds / 60:.0f} minutes to run.")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> str:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--days", type=int, default=28)
    ap.add_argument("--warmup-days", type=int, default=14)
    ap.add_argument("--orders-per-day", type=int, default=150)
    ap.add_argument("--catalog-size", type=int, default=30)
    ap.add_argument("--seed", type=int, default=31)
    ap.add_argument("--arms", default="rules,sensing", help=f"comma list of {', '.join(ARMS)}, or 'all'")
    args = ap.parse_args(argv)
    arms = list(ARMS) if args.arms == "all" else args.arms.split(",")

    llm, ports = None, None
    if "claude" in arms:
        from tower.runtime import from_env
        ports = from_env()
        llm = ports.llm
        if llm is None:
            raise SystemExit("the claude arm needs Claude: put anthropic-api-key in Key Vault or deploy it in Foundry")
    from evals.world import START
    sc = scenario(args.catalog_size, START, args.warmup_days)
    # Arms share nothing but the model client, so they run side by side (Claude's arm mostly waits on the model).
    with ThreadPoolExecutor(max_workers=len(arms)) as pool:
        results = list(pool.map(lambda a: run_arm(a, days=args.days, warmup_days=args.warmup_days,
                                                  orders_per_day=args.orders_per_day, catalog_size=args.catalog_size,
                                                  seed=args.seed, llm=llm), arms))
    text = report(results, sc, days=args.days, orders_per_day=args.orders_per_day, model=getattr(llm, "model", None))
    print(text)
    if ports is not None:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        ports.blobs.write("reports", f"evals/planning-{stamp}.md", text.encode(), content_type="text/markdown")
        ops = "ops-team"
        ports.notifier.send(ports.directory.contact(ops) or ops, "Planning evaluation", text)
    return text


if __name__ == "__main__":
    main(sys.argv[1:])
