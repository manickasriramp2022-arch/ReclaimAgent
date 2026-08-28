"""Component 4: the compliance layer.

Every regulatory and network constraint is a named constant in
config/compliance.yaml. This module reads those constants and answers one
question per proposed action: allow, refuse, or defer.

A refusal is a distinct outcome. It is not a failed recovery attempt, it never
enters the recovery-rate denominator, and it always names the constant that
blocked it so the audit log says *which* rule refused and why.
"""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta, timezone
from enum import StrEnum
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict

from .config import ComplianceConfig
from .models import (
    CONTACT_CHANNELS,
    Channel,
    FailedTransaction,
    MandateStatus,
    PaymentMethod,
    RootCause,
)

# Used only if the host has no tz database; keeps CI and offline demos honest
# rather than silently skipping the quiet-hours rule.
_FIXED_OFFSETS: dict[str, timedelta] = {"Asia/Kolkata": timedelta(hours=5, minutes=30)}


class Gate(StrEnum):
    ALLOW = "allow"
    REFUSE = "refuse"
    DEFER = "defer"


class ComplianceDecision(BaseModel):
    """The compliance layer's verdict on one proposed action."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    gate: Gate
    rule: str | None = None
    reason: str = ""
    terminal: bool = False
    defer_until: datetime | None = None
    constant_value: str = ""

    @property
    def allowed(self) -> bool:
        return self.gate is Gate.ALLOW


ALLOWED = ComplianceDecision(gate=Gate.ALLOW, reason="all compliance preconditions met")


class CaseComplianceState(BaseModel):
    """The per-case facts the compliance layer needs. Passed explicitly so the
    layer holds no hidden state and is trivially testable."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    charge_attempts_made: int = 0
    prior_attempt_count: int = 0
    last_charge_at: datetime | None = None
    last_mandate_debit_at: datetime | None = None
    contacts_last_24h: int = 0
    contacts_last_7d: int = 0


def _tz(name: str) -> timezone | ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, KeyError):
        return timezone(_FIXED_OFFSETS.get(name, timedelta(0)), name)


def in_quiet_hours(instant: datetime, cfg: ComplianceConfig) -> bool:
    start, end = cfg.contact.quiet_hours_local.window()
    local = instant.astimezone(_tz(cfg.contact.quiet_hours_local.timezone)).time()
    if start <= end:
        return start <= local < end
    return local >= start or local < end  # window wraps midnight


def next_permitted_contact_instant(instant: datetime, cfg: ComplianceConfig) -> datetime:
    """First instant at or after `instant` that is outside quiet hours."""
    if not in_quiet_hours(instant, cfg):
        return instant
    tz = _tz(cfg.contact.quiet_hours_local.timezone)
    _, end = cfg.contact.quiet_hours_local.window()
    local = instant.astimezone(tz)
    candidate = local.replace(hour=end.hour, minute=end.minute, second=0, microsecond=0)
    if candidate <= local:
        candidate = candidate + timedelta(days=1)
    return candidate.astimezone(UTC)


class ComplianceEngine:
    """Stateless evaluator over config/compliance.yaml."""

    def __init__(self, cfg: ComplianceConfig) -> None:
        self.cfg = cfg

    # -- batch admission -------------------------------------------------
    def admit_record(self, txn: FailedTransaction) -> ComplianceDecision:
        if self.cfg.environment_must_be_test and txn.environment != "test":
            return ComplianceDecision(
                gate=Gate.REFUSE,
                rule="data.environment_must_be_test",
                reason="record is not marked environment=test; ReclaimAgent never processes live data",
                terminal=True,
                constant_value="true",
            )
        return ALLOWED

    # -- per-action evaluation -------------------------------------------
    def evaluate(
        self,
        txn: FailedTransaction,
        channel: Channel,
        instant: datetime,
        state: CaseComplianceState,
        category: RootCause,
    ) -> ComplianceDecision:
        if channel is Channel.RETRY_CHARGE:
            return self._evaluate_charge(txn, instant, state, category)
        return self._evaluate_contact(txn, channel, instant, state)

    # -- charge path ------------------------------------------------------
    def _evaluate_charge(
        self,
        txn: FailedTransaction,
        instant: datetime,
        state: CaseComplianceState,
        category: RootCause,
    ) -> ComplianceDecision:
        if txn.decline_code in self.cfg.no_retry_reason_codes:
            return ComplianceDecision(
                gate=Gate.REFUSE,
                rule="card_network.no_retry_reason_codes",
                reason=f"decline code {txn.decline_code} is on the never-retry list",
                terminal=True,
                constant_value=", ".join(sorted(self.cfg.no_retry_reason_codes)),
            )

        total_attempts = state.charge_attempts_made + txn.prior_attempt_count
        if total_attempts >= self.cfg.network_max_retries:
            return ComplianceDecision(
                gate=Gate.REFUSE,
                rule="card_network.max_retries_per_declined_authorisation",
                reason=(
                    f"{total_attempts} attempts already made against this declined "
                    f"authorisation; network ceiling is {self.cfg.network_max_retries}"
                ),
                terminal=True,
                constant_value=str(self.cfg.network_max_retries),
            )

        spacing_exempt = str(category) in self.cfg.retry_spacing_exempt_categories
        if state.last_charge_at is not None and not spacing_exempt:
            gap_hours = (instant - state.last_charge_at).total_seconds() / 3600.0
            if gap_hours < self.cfg.network_min_hours_between_retries:
                return ComplianceDecision(
                    gate=Gate.DEFER,
                    rule="card_network.min_hours_between_retries",
                    reason=(
                        f"only {gap_hours:.1f}h since the last attempt; network minimum "
                        f"spacing is {self.cfg.network_min_hours_between_retries:.0f}h"
                    ),
                    defer_until=state.last_charge_at
                    + timedelta(hours=self.cfg.network_min_hours_between_retries),
                    constant_value=str(self.cfg.network_min_hours_between_retries),
                )

        if txn.payment_method is PaymentMethod.E_MANDATE:
            return self._evaluate_mandate(txn, instant, state)
        return ALLOWED

    def _evaluate_mandate(
        self, txn: FailedTransaction, instant: datetime, state: CaseComplianceState
    ) -> ComplianceDecision:
        if self.cfg.mandate_must_be_active and txn.mandate_status is not MandateStatus.ACTIVE:
            return ComplianceDecision(
                gate=Gate.REFUSE,
                rule="emandate.mandate_must_be_active",
                reason=(
                    f"mandate {txn.mandate_id} is {txn.mandate_status}; debiting without an "
                    "active mandate would be an unauthorised debit"
                ),
                terminal=True,
                constant_value="true",
            )

        if txn.amount_paise > self.cfg.afa_threshold_paise:
            return ComplianceDecision(
                gate=Gate.REFUSE,
                rule="emandate.afa_exemption_threshold_paise",
                reason=(
                    f"amount {txn.amount_paise / 100:.2f} INR exceeds the AFA exemption "
                    f"threshold of {self.cfg.afa_threshold_paise / 100:.2f} INR; this debit "
                    "requires customer authentication and cannot be retried unattended"
                ),
                terminal=True,
                constant_value=str(self.cfg.afa_threshold_paise),
            )

        notice = txn.pre_debit_notice_sent_ts
        if notice is None:
            return ComplianceDecision(
                gate=Gate.REFUSE,
                rule="emandate.pre_debit_notification_lead_hours",
                reason=(
                    "no pre-debit notification on record for this mandate; a recurring debit "
                    "may not be presented without one"
                ),
                terminal=True,
                constant_value=str(self.cfg.pre_debit_lead_hours),
            )
        lead_hours = (instant - notice).total_seconds() / 3600.0
        if lead_hours < self.cfg.pre_debit_lead_hours:
            return ComplianceDecision(
                gate=Gate.DEFER,
                rule="emandate.pre_debit_notification_lead_hours",
                reason=(
                    f"pre-debit notification sent only {lead_hours:.1f}h ago; the configured "
                    f"lead time is {self.cfg.pre_debit_lead_hours:.0f}h"
                ),
                defer_until=notice + timedelta(hours=self.cfg.pre_debit_lead_hours),
                constant_value=str(self.cfg.pre_debit_lead_hours),
            )

        if state.last_mandate_debit_at is not None:
            gap = (instant - state.last_mandate_debit_at).total_seconds() / 3600.0
            if gap < 24.0 and self.cfg.max_debits_per_mandate_per_day <= 1:
                return ComplianceDecision(
                    gate=Gate.DEFER,
                    rule="emandate.max_debits_per_mandate_per_day",
                    reason=f"already debited this mandate {gap:.1f}h ago; cap is one per 24h",
                    defer_until=state.last_mandate_debit_at + timedelta(hours=24),
                    constant_value=str(self.cfg.max_debits_per_mandate_per_day),
                )
        return ALLOWED

    # -- contact path -----------------------------------------------------
    def _evaluate_contact(
        self,
        txn: FailedTransaction,
        channel: Channel,
        instant: datetime,
        state: CaseComplianceState,
    ) -> ComplianceDecision:
        if channel not in CONTACT_CHANNELS:
            raise ValueError(f"{channel} is not a customer-contact channel")

        if self.cfg.consent_required and not txn.contact_consent:
            return ComplianceDecision(
                gate=Gate.REFUSE,
                rule="consent.required_before_contact",
                reason="customer has no recorded consent for outbound dunning contact",
                terminal=True,
                constant_value="true",
            )

        required = "email" if channel is not Channel.SMS else "sms"
        if required not in txn.contact_channels:
            return ComplianceDecision(
                gate=Gate.REFUSE,
                rule="consent.required_before_contact",
                reason=f"no reachable {required} channel on file for this customer",
                terminal=True,
                constant_value="true",
            )

        if state.contacts_last_24h >= self.cfg.max_contacts_per_day:
            return ComplianceDecision(
                gate=Gate.REFUSE,
                rule="contact.max_contacts_per_customer_per_day",
                reason=(
                    f"customer already received {state.contacts_last_24h} messages in the last "
                    f"24h; cap is {self.cfg.max_contacts_per_day}"
                ),
                constant_value=str(self.cfg.max_contacts_per_day),
            )

        if state.contacts_last_7d >= self.cfg.max_contacts_per_week:
            return ComplianceDecision(
                gate=Gate.REFUSE,
                rule="contact.max_contacts_per_customer_per_week",
                reason=(
                    f"customer already received {state.contacts_last_7d} messages in the last "
                    f"7 days; cap is {self.cfg.max_contacts_per_week}"
                ),
                constant_value=str(self.cfg.max_contacts_per_week),
            )

        if in_quiet_hours(instant, self.cfg):
            nxt = next_permitted_contact_instant(instant, self.cfg)
            qh = self.cfg.contact.quiet_hours_local
            return ComplianceDecision(
                gate=Gate.DEFER,
                rule="contact.quiet_hours_local",
                reason=(
                    f"{instant.isoformat()} falls inside quiet hours "
                    f"{qh.start}-{qh.end} {qh.timezone}"
                ),
                defer_until=nxt,
                constant_value=f"{qh.start}-{qh.end} {qh.timezone}",
            )

        return ALLOWED


def quiet_hours_probe(cfg: ComplianceConfig) -> tuple[time, time, str]:
    start, end = cfg.contact.quiet_hours_local.window()
    return start, end, cfg.contact.quiet_hours_local.timezone
