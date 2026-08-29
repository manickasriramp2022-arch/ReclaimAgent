"""Component 3: the recovery policy engine and its stopping rules.

There is no per-category branching in this file. The engine reads
config/policies.yaml, selects the policy block for the classified category and
executes the plan it finds there. Adding a category is a config change.

Stopping rules are the point of this module. Each rule has a name; when it
fires, the engine stops the case and writes that name into the audit log, so
"why did this case stop?" is always answerable from the log alone.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from .config import PolicyConfig
from .models import (
    NEVER_RETRY_CATEGORIES,
    CategoryPolicy,
    Channel,
    FailedTransaction,
    RootCause,
)


class Verdict(StrEnum):
    PROCEED = "proceed"
    STOP = "stop"
    DEFER = "defer"


class PolicyDecision(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    verdict: Verdict
    rule: str | None = None
    reason: str = ""
    defer_until: datetime | None = None
    escalate: bool = False
    # Numbers the decision consulted, recorded verbatim in the audit event so a
    # reviewer can re-derive the arithmetic without rerunning anything.
    evidence: dict[str, float] = Field(default_factory=dict)


PROCEED = PolicyDecision(verdict=Verdict.PROCEED, reason="no stopping rule fired")


class CaseRuntime(BaseModel):
    """Mutable per-case bookkeeping. Never written to the audit log directly;
    the log is built from the decisions taken over this state."""

    model_config = ConfigDict(extra="forbid")

    case_id: str
    category: RootCause
    charge_attempts_made: int = 0
    contacts_sent: int = 0
    deferrals_this_step: int = 0
    step_index: int = 0
    customer_acted: bool = False
    last_charge_at: datetime | None = None
    charge_times: list[datetime] = Field(default_factory=list)
    closed: bool = False


class BatchRuntime(BaseModel):
    """Batch-level state, needed only by the circuit breaker."""

    model_config = ConfigDict(extra="forbid")

    recent_attempt_results: list[bool] = Field(default_factory=list)
    # The engine's own prior for each attempt in the window, so the breaker can
    # ask "is this worse than we predicted?" rather than merely "is this bad?".
    recent_attempt_priors: list[float] = Field(default_factory=list)
    total_charge_attempts: int = 0
    tripped: bool = False

    def record_attempt(self, success: bool, prior: float, window: int) -> None:
        self.total_charge_attempts += 1
        self.recent_attempt_results.append(success)
        self.recent_attempt_priors.append(prior)
        overflow = len(self.recent_attempt_results) - window
        if overflow > 0:
            del self.recent_attempt_results[:overflow]
            del self.recent_attempt_priors[:overflow]


class PolicyEngine:
    def __init__(self, cfg: PolicyConfig) -> None:
        self.cfg = cfg
        self.rules = cfg.stopping_rules

    # -- policy selection -------------------------------------------------
    def select(self, category: RootCause) -> CategoryPolicy:
        return self.cfg.for_category(category)

    def immediate_terminal_decision(self, policy: CategoryPolicy) -> PolicyDecision | None:
        """Hard stops are decided before any action is even scheduled.

        This is the invariant the whole system is graded on: a HARD_DECLINE or
        MANDATE_REVOKED case never reaches the scheduler, so it cannot possibly
        accumulate an attempt.
        """
        if not policy.immediate_terminal:
            return None
        rule = policy.terminal_rule or "hard_stop_category"
        escalate = policy.always_escalate
        return PolicyDecision(
            verdict=Verdict.STOP,
            rule=rule,
            reason=(
                f"{policy.category} is configured non-recoverable: zero retries, "
                "immediate terminal state"
            ),
            escalate=escalate,
            evidence={"max_charge_attempts": float(policy.max_charge_attempts)},
        )

    # -- pre-action stopping rules ---------------------------------------
    def check_before_step(
        self,
        txn: FailedTransaction,
        policy: CategoryPolicy,
        runtime: CaseRuntime,
        channel: Channel,
        instant: datetime,
        expected_value_paise: int,
    ) -> PolicyDecision:
        """Run every stopping rule that applies to the proposed step.

        Order matters: the cheapest and most absolute rules run first so a case
        that should never have got here is stopped before anything else is
        computed.
        """
        if runtime.category in NEVER_RETRY_CATEGORIES:
            return PolicyDecision(
                verdict=Verdict.STOP,
                rule=policy.terminal_rule or "hard_stop_category",
                reason=f"{runtime.category} may never be actioned",
                escalate=policy.always_escalate,
            )

        if channel not in policy.allowed_channels:
            return PolicyDecision(
                verdict=Verdict.STOP,
                rule="channel_not_allowed",
                reason=f"{channel} is not in the allowed channels for {policy.category}",
            )

        if channel is Channel.RETRY_CHARGE:
            decision = self._check_attempt_cap(txn, policy, runtime)
            if decision is not None:
                return decision
            decision = self._check_rolling_window(runtime, instant)
            if decision is not None:
                return decision

        decision = self._check_cost_floor(policy, runtime, channel, expected_value_paise)
        if decision is not None:
            return decision

        return PROCEED

    def _check_attempt_cap(
        self, txn: FailedTransaction, policy: CategoryPolicy, runtime: CaseRuntime
    ) -> PolicyDecision | None:
        rule = self.rules.max_attempts_per_case
        if not bool(getattr(rule, "enabled", True)):
            return None
        count_prior = bool(getattr(rule, "count_prior_attempts", True))
        spent = runtime.charge_attempts_made + (txn.prior_attempt_count if count_prior else 0)
        if spent >= policy.max_charge_attempts:
            return PolicyDecision(
                verdict=Verdict.STOP,
                rule="max_attempts_per_case",
                reason=(
                    f"{spent} charge attempts spent "
                    f"({runtime.charge_attempts_made} this run + {txn.prior_attempt_count} prior); "
                    f"policy cap for {policy.category} is {policy.max_charge_attempts}"
                ),
                escalate=self.cfg.defaults.escalate_on_exhaustion,
                evidence={
                    "attempts_this_run": float(runtime.charge_attempts_made),
                    "prior_attempts": float(txn.prior_attempt_count),
                    "cap": float(policy.max_charge_attempts),
                },
            )
        return None

    def _check_rolling_window(
        self, runtime: CaseRuntime, instant: datetime
    ) -> PolicyDecision | None:
        rule = self.rules.rolling_window_attempt_cap
        if not rule.enabled or str(runtime.category) in rule.exempt_categories:
            return None
        cutoff = instant - timedelta(hours=rule.window_hours)
        in_window = [t for t in runtime.charge_times if t > cutoff]
        if len(in_window) >= rule.max_charge_attempts:
            nxt = min(in_window) + timedelta(hours=rule.window_hours)
            return PolicyDecision(
                verdict=Verdict.DEFER,
                rule="rolling_window_attempt_cap",
                reason=(
                    f"{len(in_window)} charge attempts in the last {rule.window_hours:.0f}h; "
                    f"window cap is {rule.max_charge_attempts}"
                ),
                defer_until=nxt,
                evidence={
                    "attempts_in_window": float(len(in_window)),
                    "window_hours": float(rule.window_hours),
                    "cap": float(rule.max_charge_attempts),
                },
            )
        return None

    def _check_cost_floor(
        self,
        policy: CategoryPolicy,
        runtime: CaseRuntime,
        channel: Channel,
        expected_value_paise: int,
    ) -> PolicyDecision | None:
        rule = self.rules.cost_floor
        if not rule.enabled:
            return None
        attempt_index = runtime.charge_attempts_made + 1
        prior = rule.prior(policy.category, attempt_index)
        cost = rule.cost_of(channel)
        if channel is not Channel.RETRY_CHARGE:
            # A nudge is only worth sending if the charge it unblocks clears the
            # floor, so value the nudge at the charge it makes possible.
            prior = rule.prior(policy.category, max(1, attempt_index))
        expected_gain = expected_value_paise * prior * rule.recovered_value_margin
        threshold = cost * rule.min_expected_value_multiple
        if expected_gain < threshold:
            return PolicyDecision(
                verdict=Verdict.STOP,
                rule="cost_floor",
                reason=(
                    f"expected gain {expected_gain / 100:.2f} INR from the next {channel} "
                    f"is below the floor of {threshold / 100:.2f} INR "
                    f"(cost {cost / 100:.2f} INR x {rule.min_expected_value_multiple:g})"
                ),
                escalate=False,
                evidence={
                    "expected_gain_paise": round(expected_gain, 2),
                    "threshold_paise": round(threshold, 2),
                    "prior_success": prior,
                    "attempt_index": float(attempt_index),
                    "cost_paise": float(cost),
                },
            )
        return None

    # -- deferral bookkeeping --------------------------------------------
    def deferral_budget_exceeded(self, runtime: CaseRuntime) -> bool:
        rule = self.rules.quiet_hours
        return rule.enabled and runtime.deferrals_this_step >= rule.max_deferrals_per_step

    # -- circuit breaker --------------------------------------------------
    def check_circuit_breaker(self, batch: BatchRuntime) -> PolicyDecision | None:
        rule = self.rules.batch_circuit_breaker
        if not rule.enabled or batch.tripped:
            return None
        if batch.total_charge_attempts < rule.min_attempts_before_arming:
            return None
        window = batch.recent_attempt_results
        if not window:
            return None
        failure_rate = sum(1 for ok in window if not ok) / len(window)
        if failure_rate < rule.failure_rate_threshold:
            return None

        observed = float(sum(1 for ok in window if ok))
        expected = float(sum(batch.recent_attempt_priors))
        # A hard cohort fails a lot, but roughly as predicted, and the tail of a
        # decay curve can produce a long failing streak on its own. Requiring a
        # meaningful forecast that then collapses is what distinguishes an
        # outage from a batch of difficult cases.
        if expected < rule.min_expected_successes_in_window:
            return None
        if observed >= expected * rule.expected_shortfall_ratio:
            return None

        return PolicyDecision(
            verdict=Verdict.STOP,
            rule="batch_circuit_breaker",
            reason=(
                f"retry failure rate {failure_rate:.1%} over the last {len(window)} attempts "
                f"is at or above the {rule.failure_rate_threshold:.0%} halt threshold, and "
                f"{observed:.0f} successes came in against {expected:.1f} predicted; "
                "halting the batch and escalating every open case"
            ),
            escalate=True,
            evidence={
                "failure_rate": round(failure_rate, 4),
                "window_attempts": float(len(window)),
                "threshold": rule.failure_rate_threshold,
                "observed_successes": observed,
                "expected_successes": round(expected, 3),
                "expected_shortfall_ratio": rule.expected_shortfall_ratio,
                "total_attempts": float(batch.total_charge_attempts),
            },
        )

    def circuit_breaker_window(self) -> int:
        return self.rules.batch_circuit_breaker.window_attempts
