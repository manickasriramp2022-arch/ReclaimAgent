"""Component 4: the compliance layer.

Every refusal path gets a test, and each one asserts the refusal names the
constant in config/compliance.yaml that produced it. A refusal that cannot say
which rule it came from is not auditable.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from reclaim.compliance import (
    CaseComplianceState,
    ComplianceEngine,
    Gate,
    in_quiet_hours,
    next_permitted_contact_instant,
)
from reclaim.config import AppConfig
from reclaim.models import Channel, FailedTransaction, MandateStatus, RootCause

# 12:00 UTC is 17:30 IST: inside business hours, outside quiet hours.
MIDDAY = datetime(2026, 3, 2, 6, 0, tzinfo=UTC)
NIGHT = datetime(2026, 3, 2, 18, 0, tzinfo=UTC)  # 23:30 IST


def card(**overrides: object) -> FailedTransaction:
    base: dict[str, object] = {
        "transaction_id": "t1",
        "case_id": "c1",
        "customer_id": "cust1",
        "merchant_id": "m1",
        "amount_paise": 250000,
        "payment_method": "card",
        "attempt_ts": MIDDAY - timedelta(hours=3),
        "decline_code": "NF_INSUFFICIENT_FUNDS",
        "decline_description": "Insufficient funds",
        "contact_channels": ["email", "sms"],
        "contact_consent": True,
        "prior_attempt_count": 0,
    }
    base.update(overrides)
    return FailedTransaction.model_validate(base)


def mandate(**overrides: object) -> FailedTransaction:
    base: dict[str, object] = {
        "payment_method": "e_mandate",
        "mandate_id": "mdt_1",
        "mandate_status": "active",
        "pre_debit_notice_sent_ts": MIDDAY - timedelta(hours=48),
    }
    base.update(overrides)
    return card(**base)


@pytest.fixture
def engine(config: AppConfig) -> ComplianceEngine:
    return ComplianceEngine(config.compliance)


def charge(
    engine: ComplianceEngine,
    txn: FailedTransaction,
    instant: datetime = MIDDAY,
    state: CaseComplianceState | None = None,
    category: RootCause = RootCause.INSUFFICIENT_FUNDS,
):  # type: ignore[no-untyped-def]
    return engine.evaluate(
        txn, Channel.RETRY_CHARGE, instant, state or CaseComplianceState(), category
    )


def contact(
    engine: ComplianceEngine,
    txn: FailedTransaction,
    instant: datetime = MIDDAY,
    state: CaseComplianceState | None = None,
    channel: Channel = Channel.DUNNING_EMAIL,
):  # type: ignore[no-untyped-def]
    return engine.evaluate(
        txn, channel, instant, state or CaseComplianceState(), RootCause.INSUFFICIENT_FUNDS
    )


# ---------------------------------------------------------------------------
# Admission
# ---------------------------------------------------------------------------
def test_only_test_mode_records_are_admitted(engine: ComplianceEngine) -> None:
    assert engine.admit_record(card()).allowed


def test_a_clean_charge_is_allowed(engine: ComplianceEngine) -> None:
    assert charge(engine, card()).gate is Gate.ALLOW


# ---------------------------------------------------------------------------
# Card network constraints
# ---------------------------------------------------------------------------
def test_never_retry_reason_codes_are_refused_outright(engine: ComplianceEngine) -> None:
    decision = charge(engine, card(decline_code="HD_STOLEN_CARD"))
    assert decision.gate is Gate.REFUSE
    assert decision.terminal
    assert decision.rule == "card_network.no_retry_reason_codes"


def test_network_retry_ceiling_counts_attempts_made_before_the_batch(
    engine: ComplianceEngine, config: AppConfig
) -> None:
    ceiling = config.compliance.network_max_retries
    decision = charge(
        engine,
        card(prior_attempt_count=ceiling),
        state=CaseComplianceState(prior_attempt_count=ceiling),
    )
    assert decision.gate is Gate.REFUSE
    assert decision.rule == "card_network.max_retries_per_declined_authorisation"
    assert decision.terminal


def test_retries_are_spaced_by_the_network_minimum(
    engine: ComplianceEngine, config: AppConfig
) -> None:
    gap = config.compliance.network_min_hours_between_retries
    last = MIDDAY - timedelta(hours=gap / 2)
    decision = charge(engine, card(), state=CaseComplianceState(last_charge_at=last))
    assert decision.gate is Gate.DEFER
    assert decision.rule == "card_network.min_hours_between_retries"
    assert decision.defer_until == last + timedelta(hours=gap)


def test_technical_errors_are_exempt_from_decline_retry_spacing(
    engine: ComplianceEngine,
) -> None:
    """A gateway timeout returned no issuer decision, so a rule written for
    declined authorisations does not apply to it."""
    decision = charge(
        engine,
        card(decline_code="TE_GATEWAY_TIMEOUT"),
        state=CaseComplianceState(last_charge_at=MIDDAY - timedelta(minutes=20)),
        category=RootCause.TECHNICAL_ERROR,
    )
    assert decision.gate is Gate.ALLOW


# ---------------------------------------------------------------------------
# E-mandate constraints
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "status", [MandateStatus.REVOKED, MandateStatus.PAUSED, MandateStatus.EXPIRED]
)
def test_a_debit_needs_an_active_mandate(engine: ComplianceEngine, status: MandateStatus) -> None:
    decision = charge(engine, mandate(mandate_status=status.value))
    assert decision.gate is Gate.REFUSE
    assert decision.terminal
    assert decision.rule == "emandate.mandate_must_be_active"
    assert "unauthorised debit" in decision.reason


def test_amount_above_the_afa_threshold_is_refused(
    engine: ComplianceEngine, config: AppConfig
) -> None:
    over = config.compliance.afa_threshold_paise + 1
    decision = charge(engine, mandate(amount_paise=over))
    assert decision.gate is Gate.REFUSE
    assert decision.rule == "emandate.afa_exemption_threshold_paise"


def test_amount_at_the_afa_threshold_is_allowed(
    engine: ComplianceEngine, config: AppConfig
) -> None:
    at = config.compliance.afa_threshold_paise
    assert charge(engine, mandate(amount_paise=at)).gate is Gate.ALLOW


def test_missing_pre_debit_notification_is_refused(engine: ComplianceEngine) -> None:
    decision = charge(engine, mandate(pre_debit_notice_sent_ts=None))
    assert decision.gate is Gate.REFUSE
    assert decision.terminal
    assert decision.rule == "emandate.pre_debit_notification_lead_hours"


def test_a_too_recent_pre_debit_notification_defers_rather_than_refusing(
    engine: ComplianceEngine, config: AppConfig
) -> None:
    """The window will elapse on its own, so the correct answer is 'not yet',
    not 'never'."""
    lead = config.compliance.pre_debit_lead_hours
    sent = MIDDAY - timedelta(hours=lead / 2)
    decision = charge(engine, mandate(pre_debit_notice_sent_ts=sent))
    assert decision.gate is Gate.DEFER
    assert decision.defer_until == sent + timedelta(hours=lead)


def test_one_debit_per_mandate_per_day(engine: ComplianceEngine) -> None:
    decision = charge(
        engine,
        mandate(),
        state=CaseComplianceState(last_mandate_debit_at=MIDDAY - timedelta(hours=3)),
    )
    assert decision.gate is Gate.DEFER
    assert decision.rule == "emandate.max_debits_per_mandate_per_day"


# ---------------------------------------------------------------------------
# Consent and contact
# ---------------------------------------------------------------------------
def test_contact_without_consent_is_refused(engine: ComplianceEngine) -> None:
    decision = contact(engine, card(contact_consent=False))
    assert decision.gate is Gate.REFUSE
    assert decision.terminal
    assert decision.rule == "consent.required_before_contact"


def test_contact_needs_a_reachable_channel(engine: ComplianceEngine) -> None:
    decision = contact(engine, card(contact_channels=["email"]), channel=Channel.SMS)
    assert decision.gate is Gate.REFUSE
    assert "no reachable sms channel" in decision.reason


def test_consent_gates_contact_but_not_the_charge_rail(engine: ComplianceEngine) -> None:
    """Re-presenting an existing authorisation is a different permission from
    messaging a customer. Conflating the two strands recoverable cases."""
    no_consent = card(contact_consent=False)
    assert contact(engine, no_consent).gate is Gate.REFUSE
    assert charge(engine, no_consent).gate is Gate.ALLOW


def test_daily_contact_cap(engine: ComplianceEngine, config: AppConfig) -> None:
    cap = config.compliance.max_contacts_per_day
    decision = contact(engine, card(), state=CaseComplianceState(contacts_last_24h=cap))
    assert decision.gate is Gate.REFUSE
    assert decision.rule == "contact.max_contacts_per_customer_per_day"
    assert not decision.terminal, "a frequency cap resets; it is not a permanent bar"


def test_weekly_contact_cap(engine: ComplianceEngine, config: AppConfig) -> None:
    cap = config.compliance.max_contacts_per_week
    decision = contact(engine, card(), state=CaseComplianceState(contacts_last_7d=cap))
    assert decision.gate is Gate.REFUSE
    assert decision.rule == "contact.max_contacts_per_customer_per_week"


# ---------------------------------------------------------------------------
# Quiet hours
# ---------------------------------------------------------------------------
def test_quiet_hours_window_wraps_midnight(config: AppConfig) -> None:
    assert in_quiet_hours(NIGHT, config.compliance)
    assert not in_quiet_hours(MIDDAY, config.compliance)


def test_contact_inside_quiet_hours_is_deferred_not_refused(engine: ComplianceEngine) -> None:
    decision = contact(engine, card(), instant=NIGHT)
    assert decision.gate is Gate.DEFER
    assert decision.rule == "contact.quiet_hours_local"


def test_deferral_lands_outside_quiet_hours(config: AppConfig) -> None:
    nxt = next_permitted_contact_instant(NIGHT, config.compliance)
    assert nxt > NIGHT
    assert not in_quiet_hours(nxt, config.compliance)


def test_quiet_hours_do_not_block_a_charge(engine: ComplianceEngine) -> None:
    """No message is sent by a re-presentment, so the commercial-communication
    time band does not apply to it."""
    assert charge(engine, card(), instant=NIGHT).gate is Gate.ALLOW


def test_a_non_contact_channel_cannot_be_evaluated_as_contact(
    engine: ComplianceEngine,
) -> None:
    with pytest.raises(ValueError, match="not a customer-contact channel"):
        engine._evaluate_contact(card(), Channel.RETRY_CHARGE, MIDDAY, CaseComplianceState())


# ---------------------------------------------------------------------------
# Provenance
# ---------------------------------------------------------------------------
def test_every_unverified_constant_carries_a_source(config: AppConfig) -> None:
    entries = config.compliance.unverified_entries()
    assert entries, "the compliance file should be honest about what is unconfirmed"
    for name, const in entries:
        assert const.source.strip(), f"{name} is marked unverified but names no source"
        assert "CONFIRM" in const.source, f"{name} does not say what must be confirmed"
