"""Component 2: the root-cause classifier, rule layer and LLM fallback."""

from __future__ import annotations

from datetime import UTC
from pathlib import Path

import pytest

from reclaim.classify import CLOSED_SET, Classifier, LlmCache, _parse_model_payload
from reclaim.config import AppConfig
from reclaim.models import Actor, FailedTransaction, RootCause

CODE_TO_CATEGORY = {
    "NF_INSUFFICIENT_FUNDS": RootCause.INSUFFICIENT_FUNDS,
    "NF_DAILY_LIMIT_EXCEEDED": RootCause.INSUFFICIENT_FUNDS,
    "SD_ISSUER_UNAVAILABLE": RootCause.ISSUER_SOFT_DECLINE,
    "SD_DO_NOT_HONOUR": RootCause.ISSUER_SOFT_DECLINE,
    "SD_RISK_HOLD": RootCause.ISSUER_SOFT_DECLINE,
    "TE_GATEWAY_TIMEOUT": RootCause.TECHNICAL_ERROR,
    "TE_UPSTREAM_5XX": RootCause.TECHNICAL_ERROR,
    "EC_CARD_EXPIRED": RootCause.EXPIRED_CARD,
    "EC_CARD_REISSUED": RootCause.EXPIRED_CARD,
    "HD_STOLEN_CARD": RootCause.HARD_DECLINE,
    "HD_ACCOUNT_CLOSED": RootCause.HARD_DECLINE,
    "HD_FRAUD_SUSPECTED": RootCause.HARD_DECLINE,
    "MR_MANDATE_REVOKED": RootCause.MANDATE_REVOKED,
    "MR_MANDATE_EXPIRED": RootCause.MANDATE_REVOKED,
}


class FakeLlm:
    """Stand-in for the Anthropic client. Records what it was asked."""

    def __init__(self, answer: tuple[RootCause, float, str] | None, boom: bool = False) -> None:
        self.answer = answer
        self.boom = boom
        self.calls: list[str] = []

    def classify(
        self, decline_code: str, description: str, model: str, max_tokens: int
    ) -> tuple[RootCause, float, str] | None:
        self.calls.append(decline_code)
        if self.boom:
            raise RuntimeError("simulated model outage")
        return self.answer


def txn(code: str, description: str = "some decline") -> FailedTransaction:
    from datetime import datetime

    return FailedTransaction(
        transaction_id="t1",
        case_id="c1",
        customer_id="cust1",
        merchant_id="m1",
        amount_paise=50000,
        payment_method="card",  # type: ignore[arg-type]
        attempt_ts=datetime(2026, 3, 1, tzinfo=UTC),
        decline_code=code,
        decline_description=description,
        prior_attempt_count=0,
    )


@pytest.mark.parametrize(("code", "expected"), sorted(CODE_TO_CATEGORY.items()))
def test_rule_layer_covers_every_mapped_code(
    config: AppConfig, code: str, expected: RootCause
) -> None:
    result = Classifier(config, llm_client=None).classify(txn(code))
    assert result.category is expected
    assert result.decided_by is Actor.RULE
    assert result.rule_id, "a rule-layer decision must name the rule that decided it"


def test_every_category_is_reachable(config: AppConfig) -> None:
    """All seven categories, including UNKNOWN, must be produced by the
    classifier; a category no input can reach is dead policy."""
    classifier = Classifier(config, llm_client=None)
    seen = {classifier.classify(txn(code)).category for code in CODE_TO_CATEGORY}
    seen.add(classifier.classify(txn("ZZ_NO_SUCH_CODE", "opaque")).category)
    assert seen == set(RootCause)


def test_unmapped_code_without_llm_becomes_unknown(config: AppConfig) -> None:
    result = Classifier(config, llm_client=None).classify(txn("XX_UNSPECIFIED_1904", "who knows"))
    assert result.category is RootCause.UNKNOWN
    assert "escalating" in result.rationale


def test_pattern_layer_catches_unmapped_prefixes(config: AppConfig) -> None:
    result = Classifier(config, llm_client=None).classify(txn("HD_SOMETHING_NEW", "n/a"))
    assert result.category is RootCause.HARD_DECLINE
    assert result.rule_id == "pattern.hd_prefix"


def test_keyword_layer_is_the_last_deterministic_resort(config: AppConfig) -> None:
    result = Classifier(config, llm_client=None).classify(
        txn("QQ_OPAQUE", "Customer had insufficient funds available")
    )
    assert result.category is RootCause.INSUFFICIENT_FUNDS
    assert result.rule_id == "keyword.insufficient"


def test_llm_is_only_consulted_for_unmapped_codes(config: AppConfig) -> None:
    fake = FakeLlm((RootCause.TECHNICAL_ERROR, 0.9, "processor fault"))
    classifier = Classifier(config, llm_client=fake, cache=LlmCache(Path("/dev/null"), False))
    classifier.classify(txn("NF_INSUFFICIENT_FUNDS"))
    assert fake.calls == [], "the rule layer already answered; the model must not be called"
    classifier.classify(txn("XX_BANK_MESSAGE", "bank said something"))
    assert fake.calls == ["XX_BANK_MESSAGE"]


def test_low_confidence_model_answer_is_routed_to_unknown(config: AppConfig) -> None:
    floor = config.decline_rules.llm_fallback.min_confidence
    fake = FakeLlm((RootCause.INSUFFICIENT_FUNDS, floor - 0.2, "probably balance"))
    classifier = Classifier(config, llm_client=fake, cache=LlmCache(Path("/dev/null"), False))
    result = classifier.classify(txn("XX_BANK_MESSAGE"))
    assert result.category is RootCause.UNKNOWN
    assert "below the" in result.rationale


def test_model_cannot_invent_a_category(config: AppConfig) -> None:
    assert _parse_model_payload({"category": "PLEASE_RETRY_FOREVER", "confidence": 1.0}) is None
    assert _parse_model_payload({"category": "HARD_DECLINE", "confidence": 0.9, "rationale": "x"})


def test_model_outage_degrades_to_unknown_not_a_crash(config: AppConfig) -> None:
    classifier = Classifier(
        config, llm_client=FakeLlm(None, boom=True), cache=LlmCache(Path("/dev/null"), False)
    )
    result = classifier.classify(txn("XX_BANK_MESSAGE"))
    assert result.category is RootCause.UNKNOWN
    assert "unavailable" in result.rationale


def test_llm_answers_are_cached_by_decline_code(config: AppConfig, tmp_path: Path) -> None:
    fake = FakeLlm((RootCause.TECHNICAL_ERROR, 0.95, "processor fault"))
    cache = LlmCache(tmp_path / "cache.json", True)
    classifier = Classifier(config, llm_client=fake, cache=cache)
    for _ in range(5):
        classifier.classify(txn("XX_UNSPECIFIED_1904"))
    assert fake.calls == ["XX_UNSPECIFIED_1904"], "one call per distinct decline code"
    classifier.flush()

    reloaded = Classifier(config, llm_client=fake, cache=LlmCache(tmp_path / "cache.json", True))
    result = reloaded.classify(txn("XX_UNSPECIFIED_1904"))
    assert result.cache_hit and result.category is RootCause.TECHNICAL_ERROR
    assert len(fake.calls) == 1, "a warm cache must not re-call the model"


def test_tool_schema_pins_the_closed_set(config: AppConfig) -> None:
    from reclaim.classify import CLASSIFIER_TOOL

    enum = CLASSIFIER_TOOL["input_schema"]["properties"]["category"]["enum"]
    assert set(enum) == set(CLOSED_SET) == {str(c) for c in RootCause}
