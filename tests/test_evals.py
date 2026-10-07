"""The exception evaluation runs end to end on a short simulated window, with a stand-in for Claude."""
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
