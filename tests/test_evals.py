"""The exception evaluation runs end to end on a short simulated window, with a stand-in for Claude."""
import json
import threading

from evals.exceptions import ARMS, NONE, collect, report, score
from tower.core.models import Classification, ExceptionCategory


class FakeClaude:
    """Always says delivery delay with high confidence, and counts like FoundryClaude does."""

    model = "fake"

    def __init__(self):
        self.calls = self.input_tokens = self.output_tokens = 0
        self._lock = threading.Lock()

    def structured(self, *, system, prompt, schema):
        with self._lock:
            self.calls += 1
            self.input_tokens += len(prompt) // 4
            self.output_tokens += 50
        return Classification(category=ExceptionCategory.DELIVERY_DELAY, confidence=0.95, rationale="fake")


def test_evaluation_scores_every_arm():
    samples = collect(days=2, orders_per_day=120, catalog_size=30, seed=21)
    assert samples, "the simulated window should raise some exception signals"
    assert all(s.truth for s in samples)
    assert all(s.order_snapshot and "transitions" in s.order_snapshot for s in samples)

    llm = FakeClaude()
    scores = [score(samples, arm, llm if arm != "rules" else None, threshold=0.8) for arm in ARMS]
    by_arm = {sc.arm: sc for sc in scores}
    assert all(sc.n == len(samples) for sc in scores)
    assert by_arm["rules"].calls == 0
    assert by_arm["llm"].calls == by_arm["llm_context"].calls == len(samples)
    # The order's record makes the context arm's prompts longer.
    assert by_arm["llm_context"].input_tokens > by_arm["llm"].input_tokens
    # A model that always says "delay" is wrong, and acts, on everything that was not a delay.
    not_delay = sum(s.truth != ExceptionCategory.DELIVERY_DELAY.value for s in samples)
    assert by_arm["llm"].wrong_acted == not_delay
    assert by_arm["llm"].correct == len(samples) - not_delay

    text = report(samples, scores, days=2, orders_per_day=120, model="fake")
    assert "| llm_context |" in text and "Acted on wrongly" in text
    assert NONE in text or not any(s.truth == NONE for s in samples)


class FakePlanner:
    """Plans with recent orders and approves every proposal; counts like the real client does."""

    model = "fake"

    def __init__(self):
        self.calls = self.input_tokens = self.output_tokens = 0
        self._lock = threading.Lock()

    def structured(self, *, system, prompt, schema):
        import json
        with self._lock:
            self.calls += 1
            self.input_tokens += len(prompt) // 4
            self.output_tokens += 50
        ev = json.loads(prompt)
        if schema.__name__ == "DemandCall":
            n = 14
            return schema(rates=[{"region": r, "daily_units": d["orders"] / n} for r, d in ev["drift"].items()],
                          basis="orders", confidence=0.7, rationale="fake")
        if schema.__name__ == "RestockReview":
            return schema(decision="approve", confidence=0.7, rationale="fake")
        return None


def test_planning_evaluation_runs_every_arm_on_a_short_window():
    from evals.planning import report, run_arm, scenario
    from evals.world import START

    llm = FakePlanner()
    results = [run_arm(arm, days=3, warmup_days=15, orders_per_day=150, catalog_size=30, seed=31,
                       llm=llm if arm == "claude" else None) for arm in ("rules", "sensing", "claude")]
    by_arm = {r.arm: r for r in results}
    assert all(r.ordered > 0 for r in results)
    assert by_arm["rules"].calls == by_arm["sensing"].calls == 0 and by_arm["rules"].demand_calls == 0
    assert by_arm["sensing"].demand_calls > 0 and by_arm["claude"].calls > 0
    text = report(results, scenario(30, START, 15), days=3, orders_per_day=150, model="fake")
    assert "| claude |" in text and "take-off" in text


def test_receiving_evaluation_scores_rules_and_claude_on_the_same_cases():
    from evals import receiving as ev
    from tower.adapters.memory import ScriptedLLM
    from tower.core.config import PilotConfig
    cases = ev.collect(days=12, catalog_size=12, seed=7)
    assert cases and {c.truth for c in cases} <= set(ev.TRUTH.values())
    config = PilotConfig(receiving_review="claude")
    llm = ScriptedLLM(structured_outputs={"ReceivingCall": {"cause": "store_count_error", "confidence": 0.9,
                                                            "rationale": "clean POD"}})
    rules = ev.score("rules", cases, None, config.receiving_confidence)
    claude = ev.score("claude", cases, ev._offline_agent(config, llm), config.receiving_confidence)
    assert rules.n == claude.n == len(cases) and len(llm.seen) == len(cases)
    really_short = sum(c.truth != "store_count_error" for c in cases)
    assert claude.shorted_store_recounts == really_short and claude.credits_given_away == 0
    text = ev.report([rules, claude], cases, days=12, model=None, threshold=config.receiving_confidence)
    assert "| rules |" in text and "| claude |" in text


def test_autonomy_eval_replays_requests_through_earned_autonomy():
    from evals.autonomy import replay, report
    from evals.exceptions import Sample
    from tower.core.config import PilotConfig
    from tower.core.models import UnhappyPathSignal

    class Judge:
        """Suggests what the order record shows, except where told to get it wrong."""
        def __init__(self, wrong=()):
            self.wrong, self.n = set(wrong), 0

        def structured(self, *, system, prompt, schema):
            self.n += 1
            lost = '"state": "lost"' in prompt
            right = "approve" if lost else "reject"
            flip = {"approve": "reject", "reject": "approve"}
            return schema.model_validate({
                "summary": "s", "if_approved": "a", "if_rejected": "r", "key_numbers": [], "check_first": [],
                "suggestion": flip[right] if self.n in self.wrong else right, "suggestion_reason": "the record"})

    def asks(n):
        return [Sample(UnhappyPathSignal(signal_type="carrier_lost", source="t", order_id=f"o{i}"),
                       {"state": "lost" if i % 3 else "late"}, "lost_shipment" if i % 3 else "delivery_delay")
                for i in range(n)]

    r = replay(asks(40), Judge())
    assert r.first_auto == 21 and r.auto + r.spot_checks == 13 and r.spot_checks == 1 and r.auto_wrong == 0
    assert r.auto + r.to_person == 40 and r.claude_agreed == r.claude_clear == 40
    r = replay(asks(40), Judge(wrong={25}))       # Claude says approve on a late parcel after autonomy started
    assert r.auto_wrong == 1 and r.wrong[0]["really"] == "delivery_delay"
    assert "Wrongly approved on its own" in report(r, days=1, model="m", config=PilotConfig())


def test_autonomy_eval_asks_only_what_the_agent_would_ask():
    from evals.autonomy import agent_requests
    from evals.exceptions import Sample
    from tower.core.models import UnhappyPathSignal

    class Agent:
        def structured(self, *, system, prompt, schema):
            lost = "gone" in prompt
            return schema.model_validate({"category": "lost_shipment" if lost else "delivery_delay",
                                          "confidence": 0.9 if "sure" in prompt else 0.5,
                                          "candidates": {}, "rationale": "record"})

    def sample(i, state):
        return Sample(UnhappyPathSignal(signal_type="carrier_lost", source="t", order_id=f"o{i}"), {"state": state},
                      "lost_shipment")
    asks = agent_requests([sample(1, "gone sure"), sample(2, "gone"), sample(3, "late sure"),
                           sample(1, "gone sure")], Agent(), threshold=0.7)
    assert [s.signal.order_id for s in asks] == ["o1"]          # sure and lost, once per order
    assert asks[0].finding == {"category": "lost_shipment", "confidence": 0.9, "rationale": "record"}


def test_autonomy_eval_briefs_see_the_agents_finding():
    from evals.autonomy import replay
    from evals.exceptions import Sample
    from tower.core.models import UnhappyPathSignal

    class Judge:
        def __init__(self):
            self.prompts = []

        def structured(self, *, system, prompt, schema):
            self.prompts.append(json.loads(prompt))
            return schema.model_validate({
                "summary": "s", "if_approved": "a", "if_rejected": "r", "key_numbers": [], "check_first": [],
                "suggestion": "look_closer", "suggestion_reason": "r"})

    finding = {"category": "lost_shipment", "confidence": 0.92, "rationale": "no scan for 9 days"}
    judge = Judge()
    replay([Sample(UnhappyPathSignal(signal_type="carrier_lost", source="t", order_id="o1"), {"state": "x"},
                   "lost_shipment", finding),
            Sample(UnhappyPathSignal(signal_type="carrier_lost", source="t", order_id="o2"), {"state": "x"},
                   "lost_shipment")], judge)
    first, second = (p["request"] for p in judge.prompts)
    assert first["content"]["agent_finding"] == finding and first["rationale"] == "no scan for 9 days"
    assert "agent_finding" not in second["content"]


def test_autonomy_eval_records_its_replay_for_the_live_lane():
    from evals.autonomy import LANE, Result, record_replay
    from tower.core.config import PilotConfig
    from tower.runtime import memory_ports

    ports = memory_ports()
    cert = record_replay(ports, PilotConfig(), Result(requests=64, claude_clear=62, claude_agreed=62), report="r.md")
    assert cert["passed"] and ports.store.get("autonomy_lanes", LANE)["replay"]["report"] == "r.md"
    cert = record_replay(ports, PilotConfig(), Result(requests=64, claude_clear=62, claude_agreed=61,
                                                      claude_wrong_approve=1))
    assert not cert["passed"]
