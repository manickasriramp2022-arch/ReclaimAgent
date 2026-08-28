"""Component 2: root-cause classifier.

Two layers, in strict order:

1. A deterministic rule layer over decline codes (config/decline_rules.yaml).
   Exact code match, then anchored regex on the code, then a conservative
   keyword match on the decline description.
2. An LLM fallback, consulted only for codes layer 1 could not match.

The model is constrained to the closed `RootCause` set by a tool schema, and
its answer is discarded unless confidence clears the configured floor. A
discarded or malformed answer becomes UNKNOWN, which escalates to a human. The
model can never introduce a category, and nothing is ever silently retried on
the strength of a low-confidence guess.

LLM answers are cached by decline code, so a 250-case batch costs at most one
call per distinct unmapped code.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Protocol

from .config import AppConfig, DeclineRuleConfig
from .models import Actor, Classification, FailedTransaction, RootCause

CLOSED_SET: tuple[str, ...] = tuple(str(c) for c in RootCause)

CLASSIFIER_TOOL: dict[str, Any] = {
    "name": "record_root_cause",
    "description": (
        "Record the single root-cause category for a payment decline code. "
        "You must choose from the provided enum. If the code does not clearly "
        "fit any category, choose UNKNOWN with a low confidence rather than "
        "guessing."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "category": {
                "type": "string",
                "enum": list(CLOSED_SET),
                "description": "The single best-fitting root-cause category.",
            },
            "confidence": {
                "type": "number",
                "minimum": 0.0,
                "maximum": 1.0,
                "description": "Calibrated confidence in the chosen category.",
            },
            "rationale": {
                "type": "string",
                "description": "One line, at most 25 words, explaining the choice.",
            },
        },
        "required": ["category", "confidence", "rationale"],
    },
}

SYSTEM_PROMPT = (
    "You classify payment gateway decline codes into exactly one root-cause "
    "category for a payment-recovery system. Categories:\n"
    "- INSUFFICIENT_FUNDS: the payer lacks balance or hit a spending limit. Recoverable by waiting.\n"
    "- EXPIRED_CARD: the instrument is expired or reissued. Recoverable only if the customer updates it.\n"
    "- ISSUER_SOFT_DECLINE: the issuer declined without a permanent reason, or was unreachable. Retryable.\n"
    "- HARD_DECLINE: stolen/lost card, closed account, confirmed fraud. Never retryable.\n"
    "- MANDATE_REVOKED: recurring debit authority withdrawn or expired. Never retryable.\n"
    "- TECHNICAL_ERROR: gateway, acquirer or network fault unrelated to the payer. Retryable after a short backoff.\n"
    "- UNKNOWN: you cannot tell. Choosing this routes the case to a human, which is the safe outcome.\n\n"
    "Retrying a HARD_DECLINE or MANDATE_REVOKED causes real harm. When in "
    "doubt between a retryable category and a non-retryable one, or when the "
    "code is opaque, answer UNKNOWN with low confidence. Always call the "
    "record_root_cause tool."
)


class LlmClient(Protocol):
    """Minimal surface the classifier needs, so tests can substitute a fake."""

    def classify(
        self, decline_code: str, description: str, model: str, max_tokens: int
    ) -> tuple[RootCause, float, str] | None: ...


class AnthropicClient:
    """Thin wrapper over the Anthropic SDK. Key comes from the environment only."""

    def __init__(self, api_key: str | None = None) -> None:
        key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        if not key:
            raise RuntimeError(
                "ANTHROPIC_API_KEY is not set. Run with --no-llm for the "
                "offline rule-layer-only pipeline."
            )
        from anthropic import Anthropic

        self._client = Anthropic(api_key=key)

    def classify(
        self, decline_code: str, description: str, model: str, max_tokens: int
    ) -> tuple[RootCause, float, str] | None:
        # The SDK's create() is overloaded across streaming and tool shapes; the
        # request is assembled here as one mapping so the cast is confined to a
        # single line rather than spread across five keyword arguments.
        params: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "system": SYSTEM_PROMPT,
            "tools": [CLASSIFIER_TOOL],
            "tool_choice": {"type": "tool", "name": "record_root_cause"},
            "messages": [
                {
                    "role": "user",
                    "content": (
                        f"Decline code: {decline_code}\n"
                        f"Gateway description: {description}\n\n"
                        "Classify this decline."
                    ),
                }
            ],
        }
        message = self._client.messages.create(**params)
        for block in message.content:
            if getattr(block, "type", None) == "tool_use":
                payload = getattr(block, "input", None)
                if isinstance(payload, dict):
                    return _parse_model_payload(payload)
        return None


def _parse_model_payload(payload: dict[str, Any]) -> tuple[RootCause, float, str] | None:
    """Coerce a model answer into the closed set, or reject it entirely."""
    raw_category = str(payload.get("category", "")).strip().upper()
    if raw_category not in CLOSED_SET:
        return None
    try:
        confidence = float(payload.get("confidence", 0.0))
    except (TypeError, ValueError):
        return None
    confidence = min(max(confidence, 0.0), 1.0)
    rationale = " ".join(str(payload.get("rationale", "")).split())[:240]
    return RootCause(raw_category), confidence, rationale or "no rationale supplied"


class LlmCache:
    """Disk-backed cache keyed by decline code."""

    def __init__(self, path: Path, enabled: bool = True) -> None:
        self.path = path
        self.enabled = enabled
        self._data: dict[str, dict[str, Any]] = {}
        if enabled and path.is_file():
            try:
                loaded = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    self._data = loaded
            except (OSError, json.JSONDecodeError):
                self._data = {}

    def get(self, code: str) -> tuple[RootCause, float, str] | None:
        if not self.enabled:
            return None
        entry = self._data.get(code)
        if not entry:
            return None
        return _parse_model_payload(entry)

    def put(self, code: str, category: RootCause, confidence: float, rationale: str) -> None:
        if not self.enabled:
            return
        self._data[code] = {
            "category": str(category),
            "confidence": confidence,
            "rationale": rationale,
        }

    def flush(self) -> None:
        if not self.enabled:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(self._data, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )


class Classifier:
    """Rule layer first, model second, UNKNOWN as the safe floor."""

    def __init__(
        self,
        config: AppConfig,
        llm_client: LlmClient | None = None,
        cache: LlmCache | None = None,
    ) -> None:
        self.config = config
        self.rules: DeclineRuleConfig = config.decline_rules
        self.llm_client = llm_client
        fb = self.rules.llm_fallback
        self.cache = cache if cache is not None else LlmCache(Path(fb.cache_path), fb.cache_enabled)
        self._compiled = [(re.compile(p.regex), p) for p in self.rules.patterns]
        self.llm_calls_made = 0

    # -- layer 1 ---------------------------------------------------------
    def classify_by_rule(self, code: str, description: str) -> Classification | None:
        exact = self.rules.exact.get(code)
        if exact is not None:
            return Classification(
                case_id="",
                decline_code=code,
                category=exact.category,
                decided_by=Actor.RULE,
                confidence=1.0,
                rationale=f"exact decline-code match on {code}",
                rule_id=exact.rule_id,
            )
        for compiled, rule in self._compiled:
            if compiled.search(code):
                return Classification(
                    case_id="",
                    decline_code=code,
                    category=rule.category,
                    decided_by=Actor.RULE,
                    confidence=0.95,
                    rationale=f"decline code matched pattern {rule.regex}",
                    rule_id=rule.rule_id,
                )
        haystack = description.lower()
        for kw in self.rules.keywords:
            hit = next((w for w in kw.any_of if w in haystack), None)
            if hit is not None:
                return Classification(
                    case_id="",
                    decline_code=code,
                    category=kw.category,
                    decided_by=Actor.RULE,
                    confidence=0.85,
                    rationale=f"description contained keyword {hit!r}",
                    rule_id=kw.rule_id,
                )
        return None

    # -- layer 2 ---------------------------------------------------------
    def classify_by_model(self, code: str, description: str) -> Classification:
        fb = self.rules.llm_fallback
        cached = self.cache.get(code)
        answer = cached
        cache_hit = cached is not None

        if answer is None and self.llm_client is not None:
            try:
                answer = self.llm_client.classify(code, description, fb.model, fb.max_tokens)
                self.llm_calls_made += 1
            except Exception as exc:  # noqa: BLE001 - a model outage must degrade, not crash
                return Classification(
                    case_id="",
                    decline_code=code,
                    category=RootCause.UNKNOWN,
                    decided_by=Actor.MODEL,
                    confidence=0.0,
                    rationale=f"model fallback unavailable ({type(exc).__name__}); escalating",
                    model_name=fb.model,
                )
            if answer is not None:
                self.cache.put(code, answer[0], answer[1], answer[2])

        if answer is None:
            reason = (
                "no rule matched and the LLM fallback is disabled (--no-llm)"
                if self.llm_client is None
                else "model returned no usable answer"
            )
            return Classification(
                case_id="",
                decline_code=code,
                category=RootCause.UNKNOWN,
                decided_by=Actor.MODEL if self.llm_client else Actor.RULE,
                confidence=0.0,
                rationale=f"{reason}; escalating rather than retrying blind",
                model_name=fb.model if self.llm_client else None,
                cache_hit=cache_hit,
            )

        category, confidence, rationale = answer
        if confidence < fb.min_confidence:
            return Classification(
                case_id="",
                decline_code=code,
                category=RootCause.UNKNOWN,
                decided_by=Actor.MODEL,
                confidence=confidence,
                rationale=(
                    f"model proposed {category} at confidence {confidence:.2f}, below the "
                    f"{fb.min_confidence:.2f} floor; routed to UNKNOWN for human review"
                ),
                model_name=fb.model,
                cache_hit=cache_hit,
            )
        return Classification(
            case_id="",
            decline_code=code,
            category=category,
            decided_by=Actor.MODEL,
            confidence=confidence,
            rationale=rationale,
            model_name=fb.model,
            cache_hit=cache_hit,
        )

    # -- entry point -----------------------------------------------------
    def classify(self, txn: FailedTransaction) -> Classification:
        by_rule = self.classify_by_rule(txn.decline_code, txn.decline_description)
        result = by_rule or self.classify_by_model(txn.decline_code, txn.decline_description)
        return result.model_copy(update={"case_id": txn.case_id})

    def flush(self) -> None:
        self.cache.flush()


def build_classifier(config: AppConfig, use_llm: bool) -> Classifier:
    """Construct a classifier, degrading to rule-only if no key is available."""
    client: LlmClient | None = None
    if use_llm:
        client = AnthropicClient()
    return Classifier(config, llm_client=client)
