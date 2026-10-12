"""Which Claude the Azure runtime uses: an Anthropic API key in Key Vault wins, else Foundry, else none."""
import pytest

pytest.importorskip("anthropic")
pytest.importorskip("azure.identity")

from tower.adapters.azure import AnthropicClaude, FoundryClaude, _claude  # noqa: E402


class Secrets:
    def __init__(self, **values):
        self.values = values

    def get_secret(self, name):
        if name not in self.values:
            raise KeyError(name)  # Key Vault raises ResourceNotFoundError
        return self.values[name]


class Cred:
    def get_token(self, *scopes, **kw):
        raise AssertionError("no token is fetched at construction")


def test_anthropic_key_wins_over_foundry():
    llm = _claude({"MCF_FOUNDRY_RESOURCE": "res"}, Secrets(**{"anthropic-api-key": "sk-test"}), Cred())
    assert isinstance(llm, AnthropicClaude) and llm.model == "claude-opus-5-5"


def test_foundry_without_key_and_none_without_either():
    assert type(_claude({"MCF_FOUNDRY_RESOURCE": "res"}, Secrets(), Cred())) is FoundryClaude
    assert _claude({}, Secrets(), Cred()) is None


def test_a_throttled_email_never_waits_long():
    pytest.importorskip("azure.core")
    from azure.core.pipeline import PipelineResponse
    from azure.core.rest import HttpRequest

    from tower.adapters.azure import _short_retry

    class Throttled:
        status_code, headers = 429, {"Retry-After": "3600"}     # ACS's answer to its hourly email limit

    policy = _short_retry()
    assert policy.get_retry_after(PipelineResponse(HttpRequest("POST", "https://acs"), Throttled(), None)) == 10
    assert policy.total_retries == 2
