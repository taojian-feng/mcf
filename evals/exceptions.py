"""Exception classification evaluation: how well do rules and Claude name what actually went wrong?

Runs the simulated world (Corp B, our system, Corp A) for some days, records every exception signal
together with what our own records showed at that moment, then labels each one with what the
simulator knows really happened to the order (lost, delayed, failed delivery, or nothing at all).
Each arm then classifies the same recorded signals:

  rules        deterministic signal-type rules only (no model)
  hybrid       the earlier production setup: rules, and Claude only for signals the rules find ambiguous
  llm          Claude on every signal, seeing the signal alone
  llm_context  Claude on every signal, also seeing the order's timeline from our records at that moment
  agent        what the exception agent runs now: it reads the order's record and asks Claude with it,
               falling back to rules if the call fails

What matters most is "wrong and acted on": a confident wrong answer starts the wrong playbook without a
person looking. Then accuracy, and how much goes to a human.

  python -m evals.exceptions --days 7                    # rules only, no model needed
  python -m evals.exceptions --days 14 --arms all        # needs Claude on Azure (Foundry, or anthropic-api-key in Key Vault)
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from tower.agents.exception import INVESTIGATE_PROMPT, SYSTEM_PROMPT, Classifier, order_evidence
from tower.core import topics
from tower.core.models import Classification, ExceptionCategory, UnhappyPathSignal
from tower.core.ports import LLM, Event

C = ExceptionCategory
NONE = "no_real_problem"
ARMS = ("rules", "hybrid", "llm", "llm_context", "agent")
# Claude Opus 5.5 list prices, USD per million tokens (Foundry bills at the same rates).
PRICE_IN, PRICE_OUT = 4.00, 20.00

CONTEXT_PROMPT = INVESTIGATE_PROMPT


@dataclass
class Sample:
    signal: UnhappyPathSignal
    order_snapshot: dict[str, Any] | None
    truth: str | None = None  # an ExceptionCategory value, NONE, or None when unknown


@dataclass
class Score:
    arm: str
    n: int = 0
    correct: int = 0
    acted: int = 0
    wrong_acted: int = 0
    to_human: int = 0
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    seconds: float = 0.0
    by_type: dict[str, Counter] = field(default_factory=lambda: defaultdict(Counter))
    misses: list[dict[str, Any]] = field(default_factory=list)

    @property
    def cost(self) -> float:
        return (self.input_tokens * PRICE_IN + self.output_tokens * PRICE_OUT) / 1e6


class Metered:
    """Counts calls and tokens of an LLM port that keeps usage counters (FoundryClaude does)."""

    def __init__(self, llm: LLM):
        self.llm = llm

    def usage(self) -> tuple[int, int, int]:
        return (getattr(self.llm, "calls", 0), getattr(self.llm, "input_tokens", 0),
                getattr(self.llm, "output_tokens", 0))


# ---------------------------------------------------------------- collect

def collect(days: float, orders_per_day: int, catalog_size: int, seed: int) -> list[Sample]:
    from evals.world import build_world

    samples: list[Sample] = []
    with build_world(catalog_size=catalog_size, orders_per_day=orders_per_day, seed_a=seed, seed_b=seed + 1,
                     bad_record_rate=0.02) as world:
        store = world.ports.store

        def record(event: Event) -> None:
            sig = UnhappyPathSignal.model_validate(event.payload)
            snap = store.get("orders", sig.order_id) if sig.order_id else None
            samples.append(Sample(sig, order_evidence(snap)))

        world.ports.bus.subscribe(topics.EXCEPTION_SIGNALS, record, subscription="eval-recorder")
        world.run(hours=days * 24)
        world.run(hours=24 * 4)  # let open orders finish so every signal can be labelled
        for s in samples:
            s.truth = _truth(world, s.signal)
    return [s for s in samples if s.truth is not None]


def _truth(world, sig: UnhappyPathSignal) -> str | None:
    if not sig.order_id:
        return None
    o = world.corpa.store.get(f"order:{sig.order_id}")
    ours = world.ports.store.get("orders", sig.order_id)
    if o is None or ours is None:
        return None
    fate = o.get("assigned_fate", "normal")
    if fate == "lost":
        return C.LOST_SHIPMENT.value
    if fate in ("undeliverable", "attempt"):
        return C.FAILED_DELIVERY.value
    if ours.get("on_time") is False or fate == "delay":
        return C.DELIVERY_DELAY.value
    if ours.get("on_time") is True:
        return NONE
    return None  # still in flight at the end of the run


# ---------------------------------------------------------------- score

def score(samples: list[Sample], arm: str, llm: LLM | None, threshold: float, workers: int = 8) -> Score:
    sc = Score(arm)
    meter = Metered(llm) if llm is not None else None
    before = meter.usage() if meter else (0, 0, 0)
    started = time.monotonic()
    rules = Classifier(None)
    hybrid = Classifier(llm)

    def classify(s: Sample) -> Classification:
        if arm == "rules":
            return rules.classify(s.signal)
        if arm == "hybrid":
            return hybrid.classify(s.signal)
        if arm == "agent":
            return hybrid.classify(s.signal, s.order_snapshot)
        prompt = {"signal": s.signal.model_dump(mode="json")}
        system = SYSTEM_PROMPT
        if arm == "llm_context":
            prompt["order_record"] = s.order_snapshot
            system = CONTEXT_PROMPT
        out = llm.structured(system=system, prompt=json.dumps(prompt, default=str), schema=Classification)
        return out or rules.classify(s.signal)  # a failed call falls back, as in production

    # Model calls take seconds each, so run them side by side; results keep the samples' order.
    with ThreadPoolExecutor(max_workers=1 if arm == "rules" else workers) as pool:
        for s, out in zip(samples, pool.map(classify, samples)):
            _tally(sc, s, out, threshold)
    sc.seconds = time.monotonic() - started
    if meter:
        after = meter.usage()
        sc.calls, sc.input_tokens, sc.output_tokens = (a - b for a, b in zip(after, before))
    return sc


def _tally(sc: Score, s: Sample, out: Classification, threshold: float) -> None:
    said = out.category.value if out.category else NONE
    ok = said == s.truth
    acted = out.category is not None and out.confidence >= threshold
    sc.n += 1
    sc.correct += ok
    sc.acted += acted
    sc.wrong_acted += acted and not ok
    sc.to_human += not acted
    t = sc.by_type[s.signal.signal_type]
    t["n"] += 1
    t["correct"] += ok
    t["wrong_acted"] += acted and not ok
    if not ok and len(sc.misses) < 8:
        sc.misses.append({"signal": s.signal.signal_type, "truth": s.truth, "said": said,
                          "confidence": round(out.confidence, 2), "why": out.rationale[:160]})


# ---------------------------------------------------------------- report

def report(samples: list[Sample], scores: list[Score], *, days: float, orders_per_day: int, model: str | None) -> str:
    truths = Counter(s.truth for s in samples)
    pct = lambda a, b: f"{100 * a / b:.0f}%" if b else "n/a"  # noqa: E731
    lines = [
        "# Exception classification evaluation",
        "",
        f"Run {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC on {days:g} simulated days at about "
        f"{orders_per_day} orders a day. {len(samples)} exception signals were labelled from what actually "
        f"happened to the order. Model: {model or 'none (rules only)'}.",
        "",
        "What really happened: " + ", ".join(f"{k} {v}" for k, v in truths.most_common()) + ".",
        "",
        "| Arm | Correct | Acted on wrongly | Sent to a person | Model calls | Cost (USD) |",
        "|---|---|---|---|---|---|",
    ]
    for sc in scores:
        lines.append(f"| {sc.arm} | {pct(sc.correct, sc.n)} ({sc.correct}/{sc.n}) | {sc.wrong_acted} | "
                     f"{pct(sc.to_human, sc.n)} | {sc.calls} | {sc.cost:.2f} |")
    lines += ["", "\"Acted on wrongly\" means a confident wrong answer that would start the wrong playbook "
                  "without a person looking. It should be zero.", ""]
    for sc in scores:
        lines += [f"## {sc.arm}", "", "| Signal | Seen | Correct | Acted on wrongly |", "|---|---|---|---|"]
        for t, c in sorted(sc.by_type.items(), key=lambda kv: -kv[1]["n"]):
            lines.append(f"| {t} | {c['n']} | {pct(c['correct'], c['n'])} | {c['wrong_acted']} |")
        if sc.misses:
            lines += ["", "Examples it got wrong:", ""]
            lines += [f"- {m['signal']}: was {m['truth']}, said {m['said']} at {m['confidence']} ({m['why']})"
                      for m in sc.misses]
        lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> str:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--days", type=float, default=7)
    ap.add_argument("--orders-per-day", type=int, default=140)
    ap.add_argument("--catalog-size", type=int, default=60)
    ap.add_argument("--seed", type=int, default=21)
    ap.add_argument("--arms", default="rules", help=f"comma list of {', '.join(ARMS)}, or 'all'")
    args = ap.parse_args(argv)
    arms = list(ARMS) if args.arms == "all" else args.arms.split(",")

    llm, ports = None, None
    if any(a != "rules" for a in arms):
        from tower.runtime import from_env
        ports = from_env()
        llm = ports.llm
        if llm is None:
            raise SystemExit("arms other than 'rules' need Claude: deploy it in Foundry or put anthropic-api-key in Key Vault (MCF_CLOUD=azure)")

    from tower.core.config import PilotConfig
    threshold = PilotConfig().classification_confidence_threshold
    samples = collect(args.days, args.orders_per_day, args.catalog_size, args.seed)
    scores = [score(samples, a, llm, threshold) for a in arms]
    text = report(samples, scores, days=args.days, orders_per_day=args.orders_per_day,
                  model=getattr(llm, "model", None))
    print(text)
    if ports is not None:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        ports.blobs.write("reports", f"evals/exceptions-{stamp}.md", text.encode(), content_type="text/markdown")
        ops = "ops-team"
        ports.notifier.send(ports.directory.contact(ops) or ops, "Exception classification evaluation", text)
    return text


if __name__ == "__main__":
    main(sys.argv[1:])
