"""Store receiving evaluation: when a store delivery comes up short or damaged, who gets the cause right?

Runs the simulated world with Corp C's stores for some weeks, with truck mishaps and store miscounts far more
common than in real life so there are enough to judge. Every receiving report that does not match its ship
notice is recorded with the evidence the tower had at that moment (the store's report, the ship notice, the
carrier's delivery notes, Corp A's nightly counts, the store's history), then labelled with what really
happened on the truck (the simulators know). Each arm then decides the same recorded cases:

  rules   the report alone: damaged units are the carrier's, anything else the warehouse's
  claude  Claude weighs all the evidence (what PilotConfig.receiving_review = claude runs)

The cost of a wrong call is concrete. Crediting a store's miscount gives money away. Blaming the warehouse
for what the carrier lost or damaged forgoes the freight claim that would have recovered it. A freight claim
on the warehouse's own mistake is declined and costs the claims desk time. A recount asked of a store that
really was shorted makes the client wait, and the case comes back to a person. Calls below the confidence bar
go to a person, which costs a person's time.

  python -m evals.receiving --days 14                      # rules only, no model needed
  python -m evals.receiving --days 56 --arms all           # needs Claude on Azure
"""
from __future__ import annotations

import argparse
import sys
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from tower.agents.receiving import ReceivingAgent, rules_call
from tower.core.ports import LLM

ARMS = ("rules", "claude")
PRICE_IN, PRICE_OUT = 4.00, 20.00     # Claude Opus 5.5 list prices, USD per million tokens
TRUTH = {"mis_pick": "pick_error", "short_pick": "pick_error", "carrier_damage": "carrier_damage",
         "carrier_loss": "carrier_loss", "store_miscount": "store_count_error"}
CARRIER = {"carrier_damage", "carrier_loss"}
# How often mishaps happen in the evaluation world (real life is about a tenth of this).
HOT_FATES = (("mis_pick", 0.11), ("short_pick", 0.11), ("carrier_damage", 0.1), ("carrier_loss", 0.1))
MISCOUNT_SCALE = 10
PERSON_REVIEW_USD = 15.0              # a person's time on one case
DECLINED_CLAIM_USD = 35.0             # the claims desk's time on a freight claim the carrier declines


@dataclass
class Case:
    claim_id: str
    truth: str                         # a ReceivingCause value
    fate: str                          # what the simulator did
    credit_usd: float
    problems: list[dict[str, Any]]
    evidence: dict[str, Any]


@dataclass
class Score:
    arm: str
    n: int = 0
    correct: int = 0
    to_person: int = 0
    wrong_acted: int = 0
    credits_given_away: float = 0.0    # credited a store's miscount
    recoveries_missed: float = 0.0     # the carrier's fault, no freight claim
    claims_declined: int = 0
    shorted_store_recounts: int = 0    # a real short sent back for a recount
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    seconds: float = 0.0
    confusion: Counter = field(default_factory=Counter)

    @property
    def model_cost(self) -> float:
        return (self.input_tokens * PRICE_IN + self.output_tokens * PRICE_OUT) / 1e6

    @property
    def total(self) -> float:
        return (self.credits_given_away + self.recoveries_missed + self.claims_declined * DECLINED_CLAIM_USD
                + (self.to_person + self.shorted_store_recounts) * PERSON_REVIEW_USD + self.model_cost)


def collect(*, days: int, catalog_size: int, seed: int) -> list[Case]:
    """Run the world (rules decide live, so nothing depends on Claude) and record every case."""
    from evals.world import build_world
    with build_world(catalog_size=catalog_size, orders_per_day=20, corpc=True, corpc_size=24, seed_c=seed,
                     truck_fates=HOT_FATES, miscount_scale=MISCOUNT_SCALE,
                     config_overrides={"receiving_review": "rules"}) as w:
        for _ in range(days):
            w.run(hours=24, step_min=20)
            w.system.run_job("reconciliation_daily")
        truth = {f"CORPC-{r['receipt_id']}": r["truth"] for r in w.corpc.store.query("receipt")}
        cases = []
        for c in w.system.ports.store.query("receiving_claims"):
            fate = truth.get(c["claim_id"])
            if fate in TRUTH:
                cases.append(Case(c["claim_id"], TRUTH[fate], fate, c["credit_usd"], c["problems"], c["evidence"]))
        return sorted(cases, key=lambda c: c.claim_id)


def decide(arm: str, case: Case, agent: ReceivingAgent | None) -> dict[str, Any] | None:
    if arm == "rules":
        return rules_call(case.problems)
    return agent.ask_claude(case.evidence) if agent else None


def score(arm: str, cases: list[Case], agent: ReceivingAgent | None, threshold: float) -> Score:
    s, llm = Score(arm), getattr(agent, "llm", None)
    usage = lambda: (getattr(llm, "calls", 0), getattr(llm, "input_tokens", 0), getattr(llm, "output_tokens", 0))  # noqa: E731
    before, t0 = usage(), time.time()
    for case in cases:
        call = decide(arm, case, agent) or rules_call(case.problems)
        cause, sure = call["cause"], call["confidence"] >= threshold and call["cause"] != "unclear"
        s.n += 1
        s.confusion[(case.truth, cause)] += 1
        s.correct += cause == case.truth
        if not sure:
            s.to_person += 1
            continue
        s.wrong_acted += cause != case.truth
        if cause == "store_count_error":
            s.shorted_store_recounts += case.truth != "store_count_error"
            continue
        if case.truth == "store_count_error":
            s.credits_given_away += case.credit_usd
        if case.truth in CARRIER and cause not in CARRIER:
            s.recoveries_missed += case.credit_usd
        if cause in CARRIER and case.truth not in CARRIER:
            s.claims_declined += 1
    s.seconds = time.time() - t0
    s.calls, s.input_tokens, s.output_tokens = (a - b for a, b in zip(usage(), before))
    return s


def report(scores: list[Score], cases: list[Case], *, days: int, model: str | None, threshold: float) -> str:
    pct = lambda a, b: f"{100 * a / b:.0f}%" if b else "n/a"  # noqa: E731
    kinds = Counter(c.truth for c in cases)
    lines = [
        "# Store receiving evaluation", "",
        f"Run {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC: {days} days of Corp C's 12 stores, {len(cases)} "
        f"deliveries that did not match their ship notice. Model: {model or 'none'}.", "",
        "What really happened: " + ", ".join(f"{k} {v}" for k, v in sorted(kinds.items())) + ".", "",
        "| Arm | Right cause | Wrong and acted on | To a person | Miscounts credited | Carrier recoveries "
        "missed | Freight claims declined | Shorted stores asked to recount | Claude calls | Claude cost | "
        "Total cost |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for s in scores:
        lines.append(f"| {s.arm} | {s.correct}/{s.n} ({pct(s.correct, s.n)}) | {s.wrong_acted} | {s.to_person} | "
                     f"${s.credits_given_away:,.0f} | ${s.recoveries_missed:,.0f} | {s.claims_declined} | "
                     f"{s.shorted_store_recounts} | {s.calls} | ${s.model_cost:.2f} | ${s.total:,.0f} |")
    lines += ["", f"Total cost = miscounts credited + carrier recoveries missed + ${DECLINED_CLAIM_USD:.0f} per declined "
                  f"freight claim + ${PERSON_REVIEW_USD:.0f} per case a person handles (sent to one below "
                  f"{threshold:.0%} confidence, or a shorted store's recount coming back) + Claude.", "",
              "## Calls by what really happened", ""]
    causes = sorted({c for s in scores for _, c in s.confusion})
    lines += ["| Arm | Really | " + " | ".join(causes) + " |", "|---|---|" + "---|" * len(causes)]
    for s in scores:
        for truth in sorted(kinds):
            lines.append(f"| {s.arm} | {truth} | " + " | ".join(str(s.confusion[(truth, c)]) for c in causes) + " |")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> str:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--days", type=int, default=56)
    ap.add_argument("--catalog-size", type=int, default=12)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--arms", default="rules", help=f"comma list of {', '.join(ARMS)}, or 'all'")
    args = ap.parse_args(argv)
    arms = list(ARMS) if args.arms == "all" else args.arms.split(",")

    from tower.core.config import PilotConfig
    config = PilotConfig(receiving_review="claude")
    llm: LLM | None = None
    ports = None
    if "claude" in arms:
        from tower.runtime import from_env
        ports = from_env()
        llm = ports.llm
        if llm is None:
            raise SystemExit("the claude arm needs Claude: put anthropic-api-key in Key Vault or deploy it in Foundry")
    cases = collect(days=args.days, catalog_size=args.catalog_size, seed=args.seed)
    agent = _offline_agent(config, llm) if llm else None
    scores = [score(a, cases, agent, config.receiving_confidence) for a in arms]
    text = report(scores, cases, days=args.days, model=getattr(llm, "model", None),
                  threshold=config.receiving_confidence)
    print(text)
    if ports is not None:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        ports.blobs.write("reports", f"evals/receiving-{stamp}.md", text.encode(), content_type="text/markdown")
        ops = "ops-team"
        ports.notifier.send(ports.directory.contact(ops) or ops, "Store receiving evaluation", text)
    return text


def _offline_agent(config, llm: LLM) -> ReceivingAgent:
    """The agent's own Claude call, without a bus or a store: the evaluation only asks it for calls."""
    agent = ReceivingAgent.__new__(ReceivingAgent)
    agent.config, agent.llm = config, llm
    return agent


if __name__ == "__main__":
    main(sys.argv[1:])
