"""The Anthropic call, verified against the real SDK without a network request.

Every other classifier test substitutes a fake client, which proves the routing
logic and proves nothing about the request the SDK actually builds. These tests
drive the genuine `anthropic` client through a mock transport, so the SDK
serialises a real request that is then asserted on. A malformed call would
otherwise only surface on a reviewer's machine, the first time anyone ran
`--llm` with a key.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx2
import pytest
from anthropic import Anthropic

from reclaim.classify import (
    CLOSED_SET,
    AnthropicClient,
    Classifier,
    CredentialsRejected,
    LlmCache,
)
from reclaim.config import AppConfig
from reclaim.models import FailedTransaction, RootCause

TOOL_USE_RESPONSE: dict[str, Any] = {
    "id": "msg_1",
    "type": "message",
    "role": "assistant",
    "model": "claude-opus-5",
    "stop_reason": "tool_use",
    "stop_sequence": None,
    "usage": {"input_tokens": 10, "output_tokens": 5},
    "content": [
        {
            "type": "tool_use",
            "id": "tu_1",
            "name": "record_root_cause",
            "input": {
                "category": "TECHNICAL_ERROR",
                "confidence": 0.91,
                "rationale": "processor-side fault, unrelated to the payer",
            },
        }
    ],
}


def _client_capturing(sent: dict[str, Any], status: int = 200, body: Any = None) -> AnthropicClient:
    def handler(request: httpx2.Request) -> httpx2.Response:
        sent["url"] = str(request.url)
        sent["body"] = json.loads(request.content)
        return httpx2.Response(status, json=body if body is not None else TOOL_USE_RESPONSE)

    wrapper = AnthropicClient(api_key="sk-ant-offline-test")
    wrapper._client = Anthropic(
        api_key="sk-ant-offline-test",
        max_retries=0,
        http_client=httpx2.Client(transport=httpx2.MockTransport(handler)),
    )
    return wrapper


@pytest.fixture
def sent() -> dict[str, Any]:
    return {}


# ---------------------------------------------------------------------------
# The request the SDK actually builds
# ---------------------------------------------------------------------------
def test_the_call_reaches_the_messages_endpoint_and_parses(sent: dict[str, Any]) -> None:
    result = _client_capturing(sent).classify("XX_1904", "unmapped", "claude-opus-5", 2000)
    assert result == (
        RootCause.TECHNICAL_ERROR,
        0.91,
        "processor-side fault, unrelated to the payer",
    )
    assert sent["url"].endswith("/v1/messages")


def test_the_tool_is_forced_so_the_model_cannot_answer_in_prose(sent: dict[str, Any]) -> None:
    _client_capturing(sent).classify("XX_1904", "unmapped", "claude-opus-5", 2000)
    assert sent["body"]["tool_choice"] == {"type": "tool", "name": "record_root_cause"}


def test_strict_tool_use_is_on_so_the_api_enforces_the_closed_set(sent: dict[str, Any]) -> None:
    """ "The model must not invent a category" is the graded property. Python
    validates the answer, and strict tool use makes the API reject a
    non-conforming one before it is ever returned."""
    _client_capturing(sent).classify("XX_1904", "unmapped", "claude-opus-5", 2000)
    tool = sent["body"]["tools"][0]
    assert tool["strict"] is True
    assert tool["input_schema"]["additionalProperties"] is False
    assert set(tool["input_schema"]["properties"]["category"]["enum"]) == set(CLOSED_SET)
    assert set(tool["input_schema"]["required"]) == {"category", "confidence", "rationale"}


def test_effort_is_set_so_thinking_cannot_eat_the_token_budget(sent: dict[str, Any]) -> None:
    """Thinking is on by default on current models and its tokens count against
    max_tokens. Mapping one decline code is a simple task, so it runs at low
    effort rather than with thinking disabled, which can make the model write
    the tool call into visible text where this parser would never see it."""
    _client_capturing(sent).classify("XX_1904", "unmapped", "claude-opus-5", 2000)
    assert sent["body"]["output_config"] == {"effort": "low"}
    assert sent["body"]["max_tokens"] >= 1000, (
        "too small a ceiling risks truncating before the tool call"
    )


def test_the_system_prompt_travels_with_the_request(sent: dict[str, Any]) -> None:
    _client_capturing(sent).classify("XX_1904", "unmapped", "claude-opus-5", 2000)
    system = sent["body"]["system"]
    text = system if isinstance(system, str) else " ".join(b["text"] for b in system)
    assert "UNKNOWN" in text
    # The prompt must carry the safety framing, not just the category list.
    assert "never retryable" in text.lower()
    assert "record_root_cause" in text


def test_the_decline_code_and_description_are_both_sent(sent: dict[str, Any]) -> None:
    _client_capturing(sent).classify("QQ_ODD", "a bank said something", "claude-opus-5", 2000)
    content = json.dumps(sent["body"]["messages"])
    assert "QQ_ODD" in content
    assert "a bank said something" in content


# ---------------------------------------------------------------------------
# Failure modes
# ---------------------------------------------------------------------------
def test_rejected_credentials_fail_loudly(sent: dict[str, Any]) -> None:
    """Regression. A bad key used to be swallowed by the same handler that
    absorbs an outage, so every unmapped code became UNKNOWN and the operator
    got a batch full of escalations with no sign that the key was the problem."""
    client = _client_capturing(
        sent,
        status=401,
        body={"type": "error", "error": {"type": "authentication_error", "message": "invalid"}},
    )
    with pytest.raises(CredentialsRejected, match="ANTHROPIC_API_KEY"):
        client.classify("XX_1904", "unmapped", "claude-opus-5", 2000)


def test_the_classifier_propagates_rejected_credentials(
    config: AppConfig, sent: dict[str, Any]
) -> None:
    client = _client_capturing(
        sent,
        status=401,
        body={"type": "error", "error": {"type": "authentication_error", "message": "invalid"}},
    )
    classifier = Classifier(config, llm_client=client, cache=LlmCache(Path("/dev/null"), False))
    txn = FailedTransaction(
        transaction_id="t",
        case_id="c",
        customer_id="cu",
        merchant_id="m",
        amount_paise=1000,
        payment_method="card",  # type: ignore[arg-type]
        attempt_ts=datetime(2026, 3, 1, tzinfo=UTC),
        decline_code="XX_UNSPECIFIED_1904",
        decline_description="unmapped",
        prior_attempt_count=0,
    )
    with pytest.raises(CredentialsRejected):
        classifier.classify(txn)


def test_a_server_error_still_degrades_to_unknown(config: AppConfig, sent: dict[str, Any]) -> None:
    """An outage is not a bad key. The batch is still worth running, so the
    case escalates instead of aborting the run."""
    client = _client_capturing(
        sent, status=500, body={"type": "error", "error": {"type": "api_error", "message": "boom"}}
    )
    classifier = Classifier(config, llm_client=client, cache=LlmCache(Path("/dev/null"), False))
    txn = FailedTransaction(
        transaction_id="t",
        case_id="c",
        customer_id="cu",
        merchant_id="m",
        amount_paise=1000,
        payment_method="card",  # type: ignore[arg-type]
        attempt_ts=datetime(2026, 3, 1, tzinfo=UTC),
        decline_code="XX_UNSPECIFIED_1904",
        decline_description="unmapped",
        prior_attempt_count=0,
    )
    result = classifier.classify(txn)
    assert result.category is RootCause.UNKNOWN


def test_a_response_with_no_tool_use_block_yields_no_answer(sent: dict[str, Any]) -> None:
    prose_only = dict(TOOL_USE_RESPONSE)
    prose_only["content"] = [{"type": "text", "text": "I think it is a technical error."}]
    prose_only["stop_reason"] = "end_turn"
    assert (
        _client_capturing(sent, body=prose_only).classify("X", "y", "claude-opus-5", 2000) is None
    )


def test_the_configured_model_is_the_one_that_is_called(
    config: AppConfig, sent: dict[str, Any]
) -> None:
    configured = config.decline_rules.llm_fallback.model
    _client_capturing(sent).classify("XX_1904", "unmapped", configured, 2000)
    assert sent["body"]["model"] == configured
