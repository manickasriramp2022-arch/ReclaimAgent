"""Component 3: the policy engine and every one of its stopping rules.

This is the file the brief asks for the most coverage in. Each stopping rule
gets a test that fires it, and where the rule has a boundary, a test that sits
just below the boundary and confirms it does *not* fire.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from reclaim.config import AppConfig
from reclaim.models import (
    HARD_STOP_CATEGORIES,
    CategoryPolicy,
    Channel,
    FailedTransaction,
    RootCause,
)
from reclaim.policy import BatchRuntime, CaseRuntime, PolicyEngine, Verdict

T0 = datetime(2026, 3, 2, 12, 0, tzinfo=UTC)


def txn(amount_paise: int = 500000, prior: int = 0) -> FailedTransaction:
    return FailedTransaction(
        transaction_id="t1",
        case_id="c1",
        customer_id="cust1",
        merchant_id="m1",
        amount_paise=amount_paise,
        payment_method="card",  # type: ignore[arg-type]
        attempt_ts=T0 - timedelta(hours=2),
        decline_code="NF_INSUFFICIENT_FUNDS",
        decline_description="Insufficient funds",
        prior_attempt_count=prior,
    )


def runtime(category: RootCause, **kwargs: object) -> CaseRuntime:
    return CaseRuntime(case_id="c1", category=category, **kwargs)  # type: ignore[arg-type]


@pytest.fixture
def engine(config: AppConfig) -> PolicyEngine:
    return PolicyEngine(config.policies)


# ---------------------------------------------------------------------------
# Policy selection
# ---------------------------------------------------------------------------
def test_every_category_has_a_policy(engine: PolicyEngine) -> None:
    for category in RootCause:
        assert isinstance(engine.select(category), CategoryPolicy)


@pytest.mark.parametrize("category", sorted(HARD_STOP_CATEGORIES))
def test_hard_stop_categories_are_terminal_before_anything_is_scheduled(
    engine: PolicyEngine, category: RootCause
) -> None:
    policy = engine.select(category)
    assert policy.max_charge_attempts == 0
    assert policy.plan == []
    decision = engine.immediate_terminal_decision(policy)
    assert decision is not None
    assert decision.verdict is Verdict.STOP
    assert decision.rule == "hard_stop_category"


def test_unknown_is_terminal_and_always_escalates(engine: PolicyEngine) -> None:
    policy = engine.select(RootCause.UNKNOWN)
    decision = engine.immediate_terminal_decision(policy)
    assert decision is not None
    assert decision.rule == "unknown_requires_human"
    assert decision.escalate is True


def test_recoverable_categories_are_not_immediately_terminal(engine: PolicyEngine) -> None:
    for category in (
        RootCause.INSUFFICIENT_FUNDS,
        RootCause.ISSUER_SOFT_DECLINE,
        RootCause.TECHNICAL_ERROR,
        RootCause.EXPIRED_CARD,
    ):
        assert engine.immediate_terminal_decision(engine.select(category)) is None


# ---------------------------------------------------------------------------
# hard_stop_category, as a second line of defence inside check_before_step
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "category", [RootCause.HARD_DECLINE, RootCause.MANDATE_REVOKED, RootCause.UNKNOWN]
)
def test_never_retry_categories_are_stopped_even_if_a_step_reaches_the_engine(
    engine: PolicyEngine, category: RootCause
) -> None:
    """The scheduler should never hand these to check_before_step at all. If a
    future refactor ever does, the rule still fires."""
    decision = engine.check_before_step(
        txn(), engine.select(category), runtime(category), Channel.RETRY_CHARGE, T0, 500000
    )
    assert decision.verdict is Verdict.STOP
    assert decision.rule in {"hard_stop_category", "unknown_requires_human"}


# ---------------------------------------------------------------------------
# max_attempts_per_case
# ---------------------------------------------------------------------------
def test_attempt_cap_fires_at_the_policy_ceiling(engine: PolicyEngine) -> None:
    policy = engine.select(RootCause.INSUFFICIENT_FUNDS)
    state = runtime(RootCause.INSUFFICIENT_FUNDS, charge_attempts_made=policy.max_charge_attempts)
    decision = engine.check_before_step(txn(), policy, state, Channel.RETRY_CHARGE, T0, 500000)
    assert decision.verdict is Verdict.STOP
    assert decision.rule == "max_attempts_per_case"
    assert decision.evidence["cap"] == policy.max_charge_attempts


def test_attempt_cap_does_not_fire_one_below_the_ceiling(engine: PolicyEngine) -> None:
    policy = engine.select(RootCause.INSUFFICIENT_FUNDS)
    state = runtime(
        RootCause.INSUFFICIENT_FUNDS, charge_attempts_made=policy.max_charge_attempts - 1
    )
    decision = engine.check_before_step(txn(), policy, state, Channel.RETRY_CHARGE, T0, 500000)
    assert decision.verdict is Verdict.PROCEED


# ---------------------------------------------------------------------------
# rolling_window_attempt_cap
# ---------------------------------------------------------------------------
def test_rolling_window_defers_a_second_attempt_inside_the_window(engine: PolicyEngine) -> None:
    rule = engine.rules.rolling_window_attempt_cap
    recent = T0 - timedelta(hours=rule.window_hours / 2)
    state = runtime(RootCause.INSUFFICIENT_FUNDS, charge_times=[recent])
    decision = engine.check_before_step(
        txn(), engine.select(RootCause.INSUFFICIENT_FUNDS), state, Channel.RETRY_CHARGE, T0, 500000
    )
    assert decision.verdict is Verdict.DEFER
    assert decision.rule == "rolling_window_attempt_cap"
    assert decision.defer_until == recent + timedelta(hours=rule.window_hours)


def test_rolling_window_allows_an_attempt_once_the_window_has_passed(
    engine: PolicyEngine,
) -> None:
    rule = engine.rules.rolling_window_attempt_cap
    old = T0 - timedelta(hours=rule.window_hours + 1)
    state = runtime(RootCause.INSUFFICIENT_FUNDS, charge_times=[old])
    decision = engine.check_before_step(
        txn(), engine.select(RootCause.INSUFFICIENT_FUNDS), state, Channel.RETRY_CHARGE, T0, 500000
    )
    assert decision.verdict is Verdict.PROCEED


def test_exempt_categories_bypass_the_rolling_window(engine: PolicyEngine) -> None:
    """A gateway timeout never produced an issuer decision, so throttling it
    like a decline just delays a cheap, likely retry."""
    exempt = engine.rules.rolling_window_attempt_cap.exempt_categories
    assert "TECHNICAL_ERROR" in exempt
    state = runtime(RootCause.TECHNICAL_ERROR, charge_times=[T0 - timedelta(minutes=30)])
    decision = engine.check_before_step(
        txn(), engine.select(RootCause.TECHNICAL_ERROR), state, Channel.RETRY_CHARGE, T0, 500000
    )
    assert decision.verdict is Verdict.PROCEED


# ---------------------------------------------------------------------------
# cost_floor
# ---------------------------------------------------------------------------
def test_cost_floor_stops_an_attempt_that_cannot_pay_for_itself(engine: PolicyEngine) -> None:
    """A tiny balance-failure case: the expected recovery is worth less than the
    cost of presenting it."""
    decision = engine.check_before_step(
        txn(amount_paise=100),
        engine.select(RootCause.INSUFFICIENT_FUNDS),
        runtime(RootCause.INSUFFICIENT_FUNDS),
        Channel.RETRY_CHARGE,
        T0,
        100,
    )
    assert decision.verdict is Verdict.STOP
    assert decision.rule == "cost_floor"
    assert decision.evidence["expected_gain_paise"] < decision.evidence["threshold_paise"]


def test_cost_floor_lets_a_worthwhile_attempt_through(engine: PolicyEngine) -> None:
    decision = engine.check_before_step(
        txn(amount_paise=2_000_00),
        engine.select(RootCause.INSUFFICIENT_FUNDS),
        runtime(RootCause.INSUFFICIENT_FUNDS),
        Channel.RETRY_CHARGE,
        T0,
        2_000_00,
    )
    assert decision.verdict is Verdict.PROCEED


def test_cost_floor_arithmetic_is_reproducible_from_the_evidence(engine: PolicyEngine) -> None:
    rule = engine.rules.cost_floor
    amount = 100
    decision = engine.check_before_step(
        txn(amount_paise=amount),
        engine.select(RootCause.INSUFFICIENT_FUNDS),
        runtime(RootCause.INSUFFICIENT_FUNDS),
        Channel.RETRY_CHARGE,
        T0,
        amount,
    )
    prior = rule.prior(RootCause.INSUFFICIENT_FUNDS, 1)
    expected = amount * prior * rule.recovered_value_margin
    threshold = rule.cost_of(Channel.RETRY_CHARGE) * rule.min_expected_value_multiple
    assert decision.evidence["expected_gain_paise"] == pytest.approx(expected)
    assert decision.evidence["threshold_paise"] == pytest.approx(threshold)


def test_cost_floor_is_zero_for_categories_that_can_never_recover(engine: PolicyEngine) -> None:
    rule = engine.rules.cost_floor
    for category in (RootCause.HARD_DECLINE, RootCause.MANDATE_REVOKED, RootCause.UNKNOWN):
        assert rule.prior(category, 1) == 0.0


# ---------------------------------------------------------------------------
# channel guard
# ---------------------------------------------------------------------------
def test_a_channel_outside_the_policy_is_refused(engine: PolicyEngine) -> None:
    decision = engine.check_before_step(
        txn(),
        engine.select(RootCause.TECHNICAL_ERROR),  # retry_charge only
        runtime(RootCause.TECHNICAL_ERROR),
        Channel.SMS,
        T0,
        500000,
    )
    assert decision.verdict is Verdict.STOP
    assert decision.rule == "channel_not_allowed"


# ---------------------------------------------------------------------------
# quiet-hours deferral budget
# ---------------------------------------------------------------------------
def test_deferral_budget_is_bounded(engine: PolicyEngine) -> None:
    budget = engine.rules.quiet_hours.max_deferrals_per_step
    state = runtime(RootCause.INSUFFICIENT_FUNDS, deferrals_this_step=budget - 1)
    assert not engine.deferral_budget_exceeded(state)
    state.deferrals_this_step = budget
    assert engine.deferral_budget_exceeded(state)


# ---------------------------------------------------------------------------
# batch_circuit_breaker
# ---------------------------------------------------------------------------
def test_circuit_breaker_stays_shut_before_it_is_armed(engine: PolicyEngine) -> None:
    rule = engine.rules.batch_circuit_breaker
    batch = BatchRuntime()
    for _ in range(rule.min_attempts_before_arming - 1):
        batch.record_attempt(False, 0.3, rule.window_attempts)
    assert engine.check_circuit_breaker(batch) is None


def test_circuit_breaker_trips_when_the_batch_collapses(engine: PolicyEngine) -> None:
    rule = engine.rules.batch_circuit_breaker
    batch = BatchRuntime()
    for _ in range(rule.min_attempts_before_arming + rule.window_attempts):
        batch.record_attempt(False, 0.3, rule.window_attempts)
    decision = engine.check_circuit_breaker(batch)
    assert decision is not None
    assert decision.rule == "batch_circuit_breaker"
    assert decision.escalate is True
    assert decision.evidence["observed_successes"] == 0.0


def test_circuit_breaker_ignores_a_hard_cohort_that_fails_as_predicted(
    engine: PolicyEngine,
) -> None:
    """The tail of a decay curve fails constantly. That is not an outage, and
    halting the batch there would strand recoverable cases."""
    rule = engine.rules.batch_circuit_breaker
    batch = BatchRuntime()
    for _ in range(rule.min_attempts_before_arming + rule.window_attempts):
        batch.record_attempt(False, 0.02, rule.window_attempts)
    assert engine.check_circuit_breaker(batch) is None


def test_circuit_breaker_does_not_trip_while_successes_arrive(engine: PolicyEngine) -> None:
    rule = engine.rules.batch_circuit_breaker
    batch = BatchRuntime()
    for i in range(rule.min_attempts_before_arming + rule.window_attempts):
        batch.record_attempt(i % 4 == 0, 0.3, rule.window_attempts)
    assert engine.check_circuit_breaker(batch) is None


def test_breaker_window_does_not_grow_without_bound(engine: PolicyEngine) -> None:
    rule = engine.rules.batch_circuit_breaker
    batch = BatchRuntime()
    for _ in range(rule.window_attempts * 4):
        batch.record_attempt(True, 0.3, rule.window_attempts)
    assert len(batch.recent_attempt_results) == rule.window_attempts
    assert len(batch.recent_attempt_priors) == rule.window_attempts


# ---------------------------------------------------------------------------
# Config-as-contract
# ---------------------------------------------------------------------------
def test_policy_plans_only_use_channels_the_policy_allows(engine: PolicyEngine) -> None:
    for category in RootCause:
        policy = engine.select(category)
        for step in policy.plan:
            assert step.channel in policy.allowed_channels, (
                f"{category} plans a {step.channel} step that its allowed_channels forbids"
            )


def test_plans_never_schedule_more_charges_than_the_attempt_cap(engine: PolicyEngine) -> None:
    for category in RootCause:
        policy = engine.select(category)
        charges = sum(1 for s in policy.plan if s.channel is Channel.RETRY_CHARGE)
        assert charges <= policy.max_charge_attempts, (
            f"{category} plans {charges} charges against a cap of {policy.max_charge_attempts}"
        )


def test_plan_steps_are_in_ascending_time_order(engine: PolicyEngine) -> None:
    for category in RootCause:
        hours = [s.after_hours for s in engine.select(category).plan]
        assert hours == sorted(hours), f"{category} plan steps are out of order"


# ---------------------------------------------------------------------------
# A typo in policies.yaml must not silently change behaviour
# ---------------------------------------------------------------------------
def test_an_unrecognised_policy_key_is_rejected(config_copy: Path) -> None:
    """Regression. Writing `plann` instead of `plan` left INSUFFICIENT_FUNDS,
    the largest recoverable category, with no steps at all. The run still exited
    0 and reported a confident result while recovering nothing from it."""
    from reclaim.config import load_config

    path = config_copy / "policies.yaml"
    path.write_text(
        path.read_text().replace(
            "    plan:\n      - {after_hours: 4,   channel: dunning_email}",
            "    plann:\n      - {after_hours: 4,   channel: dunning_email}",
            1,
        )
    )
    with pytest.raises(ValueError, match="unrecognised key"):
        load_config(config_copy)


def test_the_rejection_suggests_what_was_meant(config_copy: Path) -> None:
    from reclaim.config import load_config

    path = config_copy / "policies.yaml"
    path.write_text(
        path.read_text().replace("    max_charge_attempts: 3", "    max_charge_attemps: 3", 1)
    )
    with pytest.raises(ValueError, match="did you mean 'max_charge_attempts'"):
        load_config(config_copy)


def test_a_missing_required_policy_key_is_rejected(config_copy: Path) -> None:
    from reclaim.config import load_config

    path = config_copy / "policies.yaml"
    path.write_text(path.read_text().replace("    quiet_hours_apply: true\n", "", 1))
    with pytest.raises(ValueError, match="missing required key"):
        load_config(config_copy)


def test_the_shipped_config_loads_cleanly(config_copy: Path) -> None:
    """The guard must not reject the configuration this project ships."""
    from reclaim.config import load_config

    assert load_config(config_copy).policies.policies
