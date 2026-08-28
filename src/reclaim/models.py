"""Pydantic models for every record and event that crosses a module boundary.

No bare dicts are passed between modules. The only mapping types that survive
here are `AuditEvent.inputs` (a deliberately open, JSON-serialisable payload
recording what a decision consulted) and configuration payloads, which are
validated on load in `reclaim.config`.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


class Frozen(BaseModel):
    """Base for immutable records. Audit integrity depends on immutability."""

    model_config = ConfigDict(frozen=True, extra="forbid")


class PaymentMethod(StrEnum):
    CARD = "card"
    UPI = "upi"
    NETBANKING = "netbanking"
    E_MANDATE = "e_mandate"


class MandateStatus(StrEnum):
    ACTIVE = "active"
    PAUSED = "paused"
    REVOKED = "revoked"
    EXPIRED = "expired"


class RootCause(StrEnum):
    """The closed set of root-cause categories.

    The LLM fallback is only ever permitted to return a member of this set;
    anything else is coerced to UNKNOWN, which escalates.
    """

    INSUFFICIENT_FUNDS = "INSUFFICIENT_FUNDS"
    EXPIRED_CARD = "EXPIRED_CARD"
    ISSUER_SOFT_DECLINE = "ISSUER_SOFT_DECLINE"
    HARD_DECLINE = "HARD_DECLINE"
    MANDATE_REVOKED = "MANDATE_REVOKED"
    TECHNICAL_ERROR = "TECHNICAL_ERROR"
    UNKNOWN = "UNKNOWN"


HARD_STOP_CATEGORIES: frozenset[RootCause] = frozenset(
    {RootCause.HARD_DECLINE, RootCause.MANDATE_REVOKED}
)
NEVER_RETRY_CATEGORIES: frozenset[RootCause] = HARD_STOP_CATEGORIES | {RootCause.UNKNOWN}


class Channel(StrEnum):
    RETRY_CHARGE = "retry_charge"
    DUNNING_EMAIL = "dunning_email"
    SMS = "sms"
    UPDATE_PAYMENT_METHOD_LINK = "update_payment_method_link"


CONTACT_CHANNELS: frozenset[Channel] = frozenset(
    {Channel.DUNNING_EMAIL, Channel.SMS, Channel.UPDATE_PAYMENT_METHOD_LINK}
)


class Actor(StrEnum):
    """Who made the decision recorded in an audit event.

    `rule`, `model`, `policy` and `human_queue` are the four required actors.
    `compliance` and `simulator` are additions: attributing a refusal to the
    compliance layer rather than to the policy engine is what makes the
    refusal counts auditable, and attributing an outcome to the simulator
    keeps simulated results visibly separate from decisions.
    """

    RULE = "rule"
    MODEL = "model"
    POLICY = "policy"
    HUMAN_QUEUE = "human_queue"
    COMPLIANCE = "compliance"
    SIMULATOR = "simulator"


class Action(StrEnum):
    RUN_STARTED = "RUN_STARTED"
    CASE_INGESTED = "CASE_INGESTED"
    CLASSIFIED = "CLASSIFIED"
    POLICY_SELECTED = "POLICY_SELECTED"
    STEP_SCHEDULED = "STEP_SCHEDULED"
    STEP_DEFERRED = "STEP_DEFERRED"
    COMPLIANCE_REFUSAL = "COMPLIANCE_REFUSAL"
    CHARGE_ATTEMPT = "CHARGE_ATTEMPT"
    CONTACT_SENT = "CONTACT_SENT"
    CONTACT_RESPONSE = "CONTACT_RESPONSE"
    RECOVERED = "RECOVERED"
    STOPPED = "STOPPED"
    ESCALATED = "ESCALATED"
    CIRCUIT_BREAKER_TRIPPED = "CIRCUIT_BREAKER_TRIPPED"
    RUN_COMPLETED = "RUN_COMPLETED"


class Outcome(StrEnum):
    SUCCESS = "success"
    FAILURE = "failure"
    REFUSED = "refused"
    DEFERRED = "deferred"
    STOPPED = "stopped"
    ESCALATED = "escalated"
    RECORDED = "recorded"


class CaseStatus(StrEnum):
    OPEN = "open"
    RECOVERED = "recovered"
    STOPPED = "stopped"
    ESCALATED = "escalated"
    REFUSED = "refused"


class FailedTransaction(Frozen):
    """One failed payment as it arrives from the batch file."""

    transaction_id: str
    case_id: str
    customer_id: str
    merchant_id: str
    amount_paise: int = Field(gt=0)
    currency: str = "INR"
    payment_method: PaymentMethod
    attempt_ts: datetime
    decline_code: str
    decline_description: str
    mandate_id: str | None = None
    mandate_status: MandateStatus | None = None
    contact_channels: list[str] = Field(default_factory=list)
    contact_consent: bool = False
    prior_attempt_count: int = Field(ge=0)
    pre_debit_notice_sent_ts: datetime | None = None
    environment: str = "test"

    @field_validator("environment")
    @classmethod
    def _must_be_test(cls, v: str) -> str:
        if v != "test":
            raise ValueError(
                f"refusing to construct a non-test transaction (environment={v!r}); "
                "ReclaimAgent only ever processes synthetic test-mode data"
            )
        return v

    @property
    def amount_rupees(self) -> float:
        return self.amount_paise / 100.0


class Classification(Frozen):
    """The outcome of the root-cause classifier for one case."""

    case_id: str
    decline_code: str
    category: RootCause
    decided_by: Actor  # RULE or MODEL
    confidence: float = Field(ge=0.0, le=1.0)
    rationale: str
    rule_id: str | None = None
    model_name: str | None = None
    cache_hit: bool = False


class AuditEvent(Frozen):
    """One immutable decision record.

    `event_hash` chains each event to its predecessor, so a deleted or
    rewritten event breaks the chain and `verify-audit` fails.
    """

    run_id: str
    seq: int = Field(ge=1)
    ts: datetime
    case_id: str
    actor: Actor
    action: Action
    outcome: Outcome
    value_paise: int = 0
    category: RootCause | None = None
    rule: str | None = None
    channel: Channel | None = None
    attempt_no: int | None = None
    inputs: dict[str, Any] = Field(default_factory=dict)
    detail: str = ""
    prev_hash: str
    event_hash: str = ""

    def payload_for_hash(self) -> str:
        body = self.model_dump(mode="json", exclude={"event_hash"})
        return json.dumps(body, sort_keys=True, separators=(",", ":"))

    def with_hash(self) -> AuditEvent:
        digest = hashlib.sha256(self.payload_for_hash().encode("utf-8")).hexdigest()
        return self.model_copy(update={"event_hash": digest})


class EscalationRecord(Frozen):
    """A case handed to a human, with the reasoning attached."""

    run_id: str
    case_id: str
    transaction_id: str
    customer_id: str
    amount_at_risk_paise: int
    currency: str
    category: RootCause
    decline_code: str
    stopping_rule: str
    reason: str
    recommended_action: str
    priority_rank: int
    priority_score: float
    charge_attempts_spent: int
    contacts_sent: int
    decision_chain: list[str]
    escalated_at: datetime


class StepPlan(Frozen):
    after_hours: float = Field(ge=0)
    channel: Channel


class CategoryPolicy(Frozen):
    category: RootCause
    recoverable: bool
    immediate_terminal: bool
    terminal_rule: str | None = None
    max_charge_attempts: int = Field(ge=0)
    backoff_hours: list[float]
    allowed_channels: list[Channel]
    quiet_hours_apply: bool
    plan: list[StepPlan]
    terminal_conditions: list[str]
    always_escalate: bool = False
    rationale: str = ""


class CategoryMetric(Frozen):
    category: RootCause
    cases: int
    value_at_risk_paise: int
    recovered_cases: int
    recovered_paise: int
    charge_attempts: int
    contacts: int
    refusals: int
    escalated_cases: int
    stopped_cases: int

    @property
    def recovery_rate(self) -> float:
        return (
            0.0 if not self.value_at_risk_paise else self.recovered_paise / self.value_at_risk_paise
        )


class RunMetrics(Frozen):
    """Every headline number, recomputed from the audit log alone."""

    run_id: str
    strategy: str
    cases: int
    value_at_risk_paise: int
    compliance_refused_terminal_paise: int
    addressable_value_paise: int
    recovered_cases: int
    recovered_paise: int
    recovery_rate_on_addressable: float
    recovery_rate_gross: float
    charge_attempts: int
    contacts_sent: int
    attempts_per_rupee_recovered: float
    # The same efficiency, the way a human reads it. attempts_per_rupee is what
    # the brief asks for by name, but on a batch of this size it renders as
    # 0.0008, which tells a reviewer nothing.
    recovered_paise_per_attempt: int
    # What the recovery cost to run, summed from the action costs stamped on
    # each attempt and contact event, so it is derived from the log like
    # everything else here rather than recomputed from config at report time.
    action_cost_paise: int
    net_recovered_paise: int
    hard_stop_cases: int
    hard_stop_cases_with_zero_attempts: int
    correctly_stopped_rate: float
    escalated_cases: int
    escalated_value_paise: int
    compliance_refusals: int
    refusals_by_rule: dict[str, int]
    stops_by_rule: dict[str, int]
    per_category: list[CategoryMetric]
    circuit_breaker_tripped: bool

    @property
    def recovered_rupees(self) -> float:
        return self.recovered_paise / 100.0


class BaselineComparison(Frozen):
    treatment: RunMetrics
    baseline: RunMetrics

    @property
    def delta_paise(self) -> int:
        return self.treatment.recovered_paise - self.baseline.recovered_paise

    @property
    def delta_attempts(self) -> int:
        return self.treatment.charge_attempts - self.baseline.charge_attempts


class ComparisonReport(Frozen):
    """Treatment vs naive baseline over the identical batch and seed.

    Two comparisons are reported, because only one of them is fair and the
    other one is the compliance story.

    `*_like_for_like` covers only the cases the compliance layer permitted
    ReclaimAgent to work at all. That is the apples-to-apples number.

    The full-batch figures include the cases ReclaimAgent terminally refused.
    The baseline debits those anyway, so its extra recovery there is money
    obtained through actions the compliance layer had already refused. It is
    reported separately rather than netted off, so nobody has to take the
    framing on trust.
    """

    treatment: RunMetrics
    baseline: RunMetrics
    treatment_like_for_like: RunMetrics
    baseline_like_for_like: RunMetrics
    refused_case_count: int
    baseline_value_from_refused_paise: int
    baseline_attempts_on_refused_cases: int
    baseline_attempts_on_hard_stop_cases: int
    baseline_attempts_on_unknown_cases: int

    @property
    def like_for_like_delta_paise(self) -> int:
        return (
            self.treatment_like_for_like.recovered_paise
            - self.baseline_like_for_like.recovered_paise
        )

    @property
    def like_for_like_delta_pct(self) -> float:
        base = self.baseline_like_for_like.recovered_paise
        return 0.0 if not base else self.like_for_like_delta_paise / base

    @property
    def attempt_delta(self) -> int:
        return (
            self.treatment_like_for_like.charge_attempts
            - self.baseline_like_for_like.charge_attempts
        )

    @property
    def attempt_delta_pct(self) -> float:
        base = self.baseline_like_for_like.charge_attempts
        return 0.0 if not base else self.attempt_delta / base

    @property
    def baseline_non_compliant_attempts(self) -> int:
        return (
            self.baseline_attempts_on_refused_cases
            + self.baseline_attempts_on_hard_stop_cases
            + self.baseline_attempts_on_unknown_cases
        )


class BenchmarkRow(Frozen):
    """One seed's like-for-like result in a sensitivity sweep."""

    seed: int
    cases: int
    addressable_cases: int
    treatment_recovered_paise: int
    baseline_recovered_paise: int
    delta_paise: int
    delta_pct: float
    treatment_attempts: int
    baseline_attempts: int
    attempt_delta: int
    attempt_delta_pct: float
    hard_stop_cases: int
    correctly_stopped_rate: float
    circuit_breaker_tripped: bool


class BenchmarkReport(Frozen):
    """A sensitivity sweep across many seeds.

    A single seed's delta is an anecdote. This is what turns it into a
    measurement: the same comparison repeated over independent batches, with
    the worst case reported as prominently as the mean.
    """

    seeds: int
    batch_size: int
    config_fingerprint: str
    rows: list[BenchmarkRow]

    @property
    def wins(self) -> int:
        return sum(1 for r in self.rows if r.delta_paise > 0)

    @property
    def losses(self) -> int:
        return sum(1 for r in self.rows if r.delta_paise < 0)

    def _pcts(self) -> list[float]:
        return sorted(r.delta_pct for r in self.rows)

    @property
    def mean_delta_pct(self) -> float:
        pcts = self._pcts()
        return sum(pcts) / len(pcts) if pcts else 0.0

    @property
    def median_delta_pct(self) -> float:
        pcts = self._pcts()
        if not pcts:
            return 0.0
        mid = len(pcts) // 2
        return pcts[mid] if len(pcts) % 2 else (pcts[mid - 1] + pcts[mid]) / 2

    @property
    def worst_delta_pct(self) -> float:
        return min(self._pcts()) if self.rows else 0.0

    @property
    def best_delta_pct(self) -> float:
        return max(self._pcts()) if self.rows else 0.0

    @property
    def mean_attempt_delta_pct(self) -> float:
        if not self.rows:
            return 0.0
        return sum(r.attempt_delta_pct for r in self.rows) / len(self.rows)

    @property
    def total_delta_paise(self) -> int:
        return sum(r.delta_paise for r in self.rows)

    @property
    def hard_stops_always_honoured(self) -> bool:
        return all(r.correctly_stopped_rate == 1.0 for r in self.rows)


class AblationRow(Frozen):
    """One variant of the system, measured over the same seeds as the others."""

    variant: str
    question: str
    recovered_paise: int
    charge_attempts: int
    contacts_sent: int
    action_cost_paise: int
    mean_delta_vs_baseline_pct: float
    recovery_vs_full_pct: float
    attempts_vs_full_pct: float
    hard_stops_always_honoured: bool


class AblationReport(Frozen):
    """What each design decision is actually worth.

    The seed sweep answers "does it win?". This answers "which part of it is
    doing the work?" by disabling one feature at a time and re-measuring over
    the identical seeds. A feature that costs nothing to remove is a feature
    that was not earning its place.
    """

    seeds: int
    batch_size: int
    config_fingerprint: str
    rows: list[AblationRow]

    @property
    def full(self) -> AblationRow | None:
        return next((r for r in self.rows if r.recovery_vs_full_pct == 0.0), None)

    @property
    def ranked(self) -> list[AblationRow]:
        """Ablations ordered by how much recovery they cost, worst first."""
        return sorted(
            (r for r in self.rows if r.recovery_vs_full_pct != 0.0),
            key=lambda r: r.recovery_vs_full_pct,
        )


class RunManifest(Frozen):
    """Non-deterministic run metadata, kept OUT of the audit log so that the
    log itself is byte-identical across runs with the same seed."""

    run_id: str
    baseline_run_id: str
    batch_path: str
    batch_seed: int
    cases: int
    llm_enabled: bool
    wall_started_at: datetime
    wall_finished_at: datetime
    config_fingerprint: str
    reclaim_version: str
