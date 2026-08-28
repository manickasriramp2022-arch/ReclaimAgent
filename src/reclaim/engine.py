"""The orchestrator: ingest -> classify -> policy -> compliance -> act -> log.

Time model
----------
A run executes against a *simulated clock*, not wall time. T0 is derived from
the batch (one hour after the latest failure in it) and the run advances an
event queue forward over a bounded horizon. That is what lets a policy with a
150-hour backoff finish in well under a second, and it is why the audit log
contains no wall-clock timestamps: identical inputs produce an identical log,
byte for byte. Wall-clock metadata lives in the run manifest instead.

Nothing in this module decides anything on its own. Classification comes from
`reclaim.classify`, what to do next comes from `reclaim.policy`, whether it is
permitted comes from `reclaim.compliance`, and what happens comes from
`reclaim.simulate`. This module's job is to sequence them and write down every
answer.
"""

from __future__ import annotations

import hashlib
import heapq
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from . import __version__
from .audit import AuditLog, audit_path
from .classify import Classifier
from .compliance import CaseComplianceState, ComplianceEngine, Gate
from .config import AppConfig
from .escalation import EscalationBuilder, write_escalations
from .models import (
    Action,
    Actor,
    AuditEvent,
    CaseStatus,
    CategoryPolicy,
    Channel,
    Classification,
    EscalationRecord,
    FailedTransaction,
    Outcome,
    RootCause,
    RunManifest,
)
from .policy import BatchRuntime, CaseRuntime, PolicyEngine, Verdict
from .simulate import OutcomeSimulator

BATCH_SCOPE = "__batch__"
DEFAULT_HORIZON_HOURS = 24 * 21
_SEED_IN_NAME = re.compile(r"batch_(-?\d+)\.jsonl$")


class RunResult(BaseModel):
    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    run_id: str
    audit_file: Path
    escalation_file: Path
    manifest: RunManifest
    escalations: list[EscalationRecord]
    events: list[AuditEvent]


def infer_seed(batch_path: Path, records: list[FailedTransaction]) -> int:
    """Recover the batch seed so the simulator's draws are reproducible."""
    match = _SEED_IN_NAME.search(batch_path.name)
    if match:
        return int(match.group(1))
    digest = hashlib.sha256("".join(r.transaction_id for r in records).encode()).digest()
    return int.from_bytes(digest[:4], "big")


def make_run_id(batch_seed: int, records: list[FailedTransaction], config: AppConfig) -> str:
    body = "".join(r.case_id for r in records) + config.fingerprint
    return f"{batch_seed}-{hashlib.sha256(body.encode()).hexdigest()[:8]}"


class _Case:
    """Per-case working set. Nothing here is written to the log directly."""

    __slots__ = ("txn", "classification", "policy", "runtime", "status", "stop_rule", "stop_reason")

    def __init__(self, txn: FailedTransaction) -> None:
        self.txn = txn
        self.classification: Classification | None = None
        self.policy: CategoryPolicy | None = None
        self.runtime: CaseRuntime | None = None
        self.status = CaseStatus.OPEN
        self.stop_rule = ""
        self.stop_reason = ""


class RecoveryRun:
    """One execution of the recovery pipeline over one batch."""

    def __init__(
        self,
        config: AppConfig,
        records: list[FailedTransaction],
        batch_seed: int,
        classifier: Classifier,
        batch_path: Path,
        out_dir: Path = Path("out"),
        horizon_hours: int = DEFAULT_HORIZON_HOURS,
        llm_enabled: bool = False,
    ) -> None:
        self.config = config
        self.records = records
        self.batch_seed = batch_seed
        self.classifier = classifier
        self.batch_path = batch_path
        self.out_dir = out_dir
        self.horizon_hours = horizon_hours
        self.llm_enabled = llm_enabled

        self.policy_engine = PolicyEngine(config.policies)
        self.compliance = ComplianceEngine(config.compliance)
        self.simulator = OutcomeSimulator(config.simulation, batch_seed)
        self.escalation_builder = EscalationBuilder(config.policies.escalation)

        self.run_id = make_run_id(batch_seed, records, config)
        self.t0 = max(r.attempt_ts for r in records).replace(
            minute=0, second=0, microsecond=0
        ) + timedelta(hours=1)
        self.deadline = self.t0 + timedelta(hours=horizon_hours)

        self.cases: dict[str, _Case] = {}
        self.batch_runtime = BatchRuntime()
        self.contact_log: dict[str, list[datetime]] = {}
        self.mandate_debits: dict[str, datetime] = {}
        self.case_events: dict[str, list[AuditEvent]] = {}
        self.escalations: list[EscalationRecord] = []
        self._log: AuditLog | None = None
        self._now = self.t0

    # -- audit helper ------------------------------------------------------
    def _emit(self, **kwargs: object) -> AuditEvent:
        assert self._log is not None
        event = self._log.append(**kwargs)  # type: ignore[arg-type]
        self.case_events.setdefault(event.case_id, []).append(event)
        return event

    # -- main --------------------------------------------------------------
    def execute(self) -> RunResult:
        wall_start = datetime.now(UTC)
        path = audit_path(self.run_id, self.out_dir)
        with AuditLog(self.run_id, path) as log:
            self._log = log
            self._emit(
                case_id=BATCH_SCOPE,
                actor=Actor.POLICY,
                action=Action.RUN_STARTED,
                outcome=Outcome.RECORDED,
                ts=self.t0,
                inputs={
                    "batch": self.batch_path.name,
                    "batch_seed": self.batch_seed,
                    "cases": len(self.records),
                    "config_fingerprint": self.config.fingerprint,
                    "llm_fallback_enabled": self.llm_enabled,
                    "horizon_hours": self.horizon_hours,
                    "simulated_outcomes": True,
                },
                detail=(
                    "ReclaimAgent run start. All payment outcomes in this log are simulated; "
                    "no gateway was contacted."
                ),
            )

            queue = self._ingest_and_plan()
            self._drain(queue)
            self._close_remaining()

            recovered = sum(1 for c in self.cases.values() if c.status is CaseStatus.RECOVERED)
            self._emit(
                case_id=BATCH_SCOPE,
                actor=Actor.POLICY,
                action=Action.RUN_COMPLETED,
                outcome=Outcome.RECORDED,
                ts=self._now,
                inputs={
                    "cases": len(self.cases),
                    "recovered_cases": recovered,
                    "escalated_cases": len(self.escalations),
                    "charge_attempts": self.batch_runtime.total_charge_attempts,
                    "circuit_breaker_tripped": self.batch_runtime.tripped,
                },
                detail="run complete",
            )
            events = log.events
        self.classifier.flush()

        self.escalations = self.escalation_builder.rank(self.escalations)
        escalation_file = write_escalations(self.escalations, self.out_dir / "escalations.jsonl")
        manifest = RunManifest(
            run_id=self.run_id,
            baseline_run_id=f"{self.run_id}-baseline",
            batch_path=str(self.batch_path),
            batch_seed=self.batch_seed,
            cases=len(self.records),
            llm_enabled=self.llm_enabled,
            wall_started_at=wall_start,
            wall_finished_at=datetime.now(UTC),
            config_fingerprint=self.config.fingerprint,
            reclaim_version=__version__,
        )
        (self.out_dir / f"manifest_{self.run_id}.json").write_text(
            manifest.model_dump_json(indent=2) + "\n", encoding="utf-8"
        )
        return RunResult(
            run_id=self.run_id,
            audit_file=path,
            escalation_file=escalation_file,
            manifest=manifest,
            escalations=self.escalations,
            events=events,
        )

    # -- phase 1: ingest, classify, select policy -------------------------
    def _ingest_and_plan(self) -> list[tuple[datetime, int, int]]:
        queue: list[tuple[datetime, int, int]] = []
        for order, txn in enumerate(self.records):
            case = _Case(txn)
            self.cases[txn.case_id] = case

            admission = self.compliance.admit_record(txn)
            if not admission.allowed:
                self._emit(
                    case_id=txn.case_id,
                    actor=Actor.COMPLIANCE,
                    action=Action.COMPLIANCE_REFUSAL,
                    outcome=Outcome.REFUSED,
                    ts=self.t0,
                    rule=admission.rule,
                    detail=admission.reason,
                )
                case.status = CaseStatus.REFUSED
                continue

            self._emit(
                case_id=txn.case_id,
                actor=Actor.POLICY,
                action=Action.CASE_INGESTED,
                outcome=Outcome.RECORDED,
                ts=self.t0,
                value_paise=txn.amount_paise,
                inputs={
                    "decline_code": txn.decline_code,
                    "payment_method": str(txn.payment_method),
                    "amount_paise": txn.amount_paise,
                    "prior_attempt_count": txn.prior_attempt_count,
                    "mandate_status": str(txn.mandate_status) if txn.mandate_status else None,
                    "contact_consent": txn.contact_consent,
                    "environment": txn.environment,
                },
                detail=f"{txn.decline_description} ({txn.decline_code})",
            )

            classification = self.classifier.classify(txn)
            case.classification = classification
            self._emit(
                case_id=txn.case_id,
                actor=classification.decided_by,
                action=Action.CLASSIFIED,
                outcome=Outcome.RECORDED,
                ts=self.t0,
                category=classification.category,
                rule=classification.rule_id,
                value_paise=txn.amount_paise,
                inputs={
                    "decline_code": txn.decline_code,
                    "decline_description": txn.decline_description,
                    "layer": "rule" if classification.decided_by is Actor.RULE else "model",
                    "confidence": classification.confidence,
                    "model": classification.model_name,
                    "cache_hit": classification.cache_hit,
                },
                detail=classification.rationale,
            )

            policy = self.policy_engine.select(classification.category)
            case.policy = policy
            case.runtime = CaseRuntime(case_id=txn.case_id, category=classification.category)
            self._emit(
                case_id=txn.case_id,
                actor=Actor.POLICY,
                action=Action.POLICY_SELECTED,
                outcome=Outcome.RECORDED,
                ts=self.t0,
                category=classification.category,
                inputs={
                    "max_charge_attempts": policy.max_charge_attempts,
                    "backoff_hours": policy.backoff_hours,
                    "allowed_channels": [str(c) for c in policy.allowed_channels],
                    "quiet_hours_apply": policy.quiet_hours_apply,
                    "plan_steps": len(policy.plan),
                    "terminal_conditions": policy.terminal_conditions,
                },
                detail=policy.rationale,
            )

            terminal = self.policy_engine.immediate_terminal_decision(policy)
            if terminal is not None:
                self._stop_case(
                    case,
                    terminal.rule or "hard_stop_category",
                    terminal.reason,
                    self.t0,
                    escalate=terminal.escalate,
                )
                continue

            if policy.plan:
                first = policy.plan[0]
                heapq.heappush(queue, (self.t0 + timedelta(hours=first.after_hours), order, 0))
            else:
                self._stop_case(
                    case,
                    "plan_exhausted",
                    "policy defines no plan steps",
                    self.t0,
                    escalate=self.config.policies.defaults.escalate_on_exhaustion,
                )
        return queue

    # -- phase 2: run the schedule ----------------------------------------
    def _drain(self, queue: list[tuple[datetime, int, int]]) -> None:
        while queue:
            when, order, step_index = heapq.heappop(queue)
            if when > self.deadline:
                continue
            self._now = max(self._now, when)
            txn = self.records[order]
            case = self.cases[txn.case_id]
            if case.status is not CaseStatus.OPEN or self.batch_runtime.tripped:
                continue
            self._run_step(case, order, step_index, when, queue)

    def _run_step(
        self,
        case: _Case,
        order: int,
        step_index: int,
        when: datetime,
        queue: list[tuple[datetime, int, int]],
    ) -> None:
        policy = case.policy
        runtime = case.runtime
        assert policy is not None and runtime is not None
        if step_index >= len(policy.plan):
            self._exhaust(case, when)
            return

        step = policy.plan[step_index]
        channel = step.channel
        txn = case.txn

        if runtime.step_index != step_index:
            runtime.step_index = step_index
            runtime.deferrals_this_step = 0

        decision = self.policy_engine.check_before_step(
            txn, policy, runtime, channel, when, txn.amount_paise
        )
        if decision.verdict is Verdict.STOP:
            self._stop_case(
                case,
                decision.rule or "unspecified",
                decision.reason,
                when,
                escalate=decision.escalate,
                evidence=decision.evidence,
            )
            return
        if decision.verdict is Verdict.DEFER:
            self._defer(
                case,
                order,
                step_index,
                when,
                decision.defer_until,
                decision.rule or "",
                decision.reason,
                queue,
                Actor.POLICY,
            )
            return

        verdict = self.compliance.evaluate(
            txn, channel, when, self._compliance_state(case, when), runtime.category
        )
        if verdict.gate is Gate.REFUSE:
            self._emit(
                case_id=txn.case_id,
                actor=Actor.COMPLIANCE,
                action=Action.COMPLIANCE_REFUSAL,
                outcome=Outcome.REFUSED,
                ts=when,
                category=runtime.category,
                rule=verdict.rule,
                channel=channel,
                inputs={
                    "constant": verdict.rule,
                    "constant_value": verdict.constant_value,
                    "terminal": verdict.terminal,
                    "amount_paise": txn.amount_paise,
                },
                detail=verdict.reason,
            )
            # A refusal on the charge rail means no automated path remains, so
            # the case ends and its value leaves the recovery-rate denominator.
            # A refusal on a contact channel only removes that nudge: the
            # customer may not be messaged, but re-presenting an existing
            # authorisation is a different permission, so the plan continues.
            if verdict.terminal and channel is Channel.RETRY_CHARGE:
                self._stop_case(
                    case,
                    verdict.rule or "compliance",
                    verdict.reason,
                    when,
                    escalate=self.config.policies.defaults.escalate_on_terminal_refusal,
                    terminal_class="compliance_refusal",
                    status=CaseStatus.REFUSED,
                )
                return
            contact_is_last_hope = "contact_cap" in policy.terminal_conditions and not any(
                step.channel is Channel.RETRY_CHARGE for step in policy.plan[step_index + 1 :]
            )
            if contact_is_last_hope:
                self._stop_case(
                    case,
                    (verdict.rule or "contact_frequency_cap")
                    if verdict.terminal
                    else "contact_frequency_cap",
                    f"{verdict.reason}; customer contact was the only remaining recovery path",
                    when,
                    escalate=self.config.policies.defaults.escalate_on_exhaustion,
                )
                return
            self._schedule_next(case, order, step_index, when, queue)
            return

        if verdict.gate is Gate.DEFER:
            self._defer(
                case,
                order,
                step_index,
                when,
                verdict.defer_until,
                verdict.rule or "",
                verdict.reason,
                queue,
                Actor.COMPLIANCE,
            )
            return

        if channel is Channel.RETRY_CHARGE:
            self._do_charge(case, order, step_index, when, queue)
        else:
            self._do_contact(case, order, step_index, when, channel, queue)

    # -- actions -----------------------------------------------------------
    def _do_charge(
        self,
        case: _Case,
        order: int,
        step_index: int,
        when: datetime,
        queue: list[tuple[datetime, int, int]],
    ) -> None:
        txn, runtime = case.txn, case.runtime
        assert runtime is not None
        attempt_index = runtime.charge_attempts_made + 1
        hours_since = (when - txn.attempt_ts).total_seconds() / 3600.0
        outcome = self.simulator.attempt(
            txn.case_id, runtime.category, attempt_index, hours_since, when, runtime.customer_acted
        )
        runtime.charge_attempts_made = attempt_index
        runtime.last_charge_at = when
        runtime.charge_times.append(when)
        if txn.mandate_id:
            self.mandate_debits[txn.mandate_id] = when
        prior = self.config.policies.stopping_rules.cost_floor.prior(
            runtime.category, attempt_index
        )
        self.batch_runtime.record_attempt(
            outcome.success, prior, self.policy_engine.circuit_breaker_window()
        )

        self._emit(
            case_id=txn.case_id,
            actor=Actor.SIMULATOR,
            action=Action.CHARGE_ATTEMPT,
            outcome=Outcome.SUCCESS if outcome.success else Outcome.FAILURE,
            ts=when,
            category=runtime.category,
            channel=Channel.RETRY_CHARGE,
            attempt_no=attempt_index,
            value_paise=txn.amount_paise if outcome.success else 0,
            inputs={
                "simulated": True,
                "success_probability": outcome.probability,
                "roll": outcome.roll,
                "hours_since_original_failure": outcome.hours_since_original,
                "contact_uplift_applied": outcome.contact_uplift_applied,
            },
            detail=(
                f"simulated re-presentment #{attempt_index} at p={outcome.probability:.3f}: "
                f"{'authorised' if outcome.success else 'declined'}"
            ),
        )

        if outcome.success:
            self._emit(
                case_id=txn.case_id,
                actor=Actor.POLICY,
                action=Action.RECOVERED,
                outcome=Outcome.SUCCESS,
                ts=when,
                category=runtime.category,
                attempt_no=attempt_index,
                value_paise=txn.amount_paise,
                inputs={
                    "charge_attempts": attempt_index,
                    "contacts_sent": runtime.contacts_sent,
                    "hours_to_recovery": round(hours_since, 2),
                },
                detail=f"recovered {txn.amount_paise / 100:.2f} INR",
            )
            case.status = CaseStatus.RECOVERED
            runtime.closed = True
            return

        breaker = self.policy_engine.check_circuit_breaker(self.batch_runtime)
        if breaker is not None:
            self._trip_breaker(breaker.reason, breaker.evidence, when)
            return

        self._schedule_next(case, order, step_index, when, queue)

    def _do_contact(
        self,
        case: _Case,
        order: int,
        step_index: int,
        when: datetime,
        channel: Channel,
        queue: list[tuple[datetime, int, int]],
    ) -> None:
        txn, runtime = case.txn, case.runtime
        assert runtime is not None
        result = self.simulator.contact(txn.case_id, channel, step_index)
        runtime.contacts_sent += 1
        self.contact_log.setdefault(txn.customer_id, []).append(when)

        self._emit(
            case_id=txn.case_id,
            actor=Actor.POLICY,
            action=Action.CONTACT_SENT,
            outcome=Outcome.RECORDED,
            ts=when,
            category=runtime.category,
            channel=channel,
            inputs={
                "simulated": True,
                "response_probability": result.probability,
                "roll": result.roll,
                "contacts_sent_for_case": runtime.contacts_sent,
            },
            detail=f"simulated {channel} dispatched to customer {txn.customer_id}",
        )
        if result.customer_acted:
            runtime.customer_acted = True
            self._emit(
                case_id=txn.case_id,
                actor=Actor.SIMULATOR,
                action=Action.CONTACT_RESPONSE,
                outcome=Outcome.SUCCESS,
                ts=when,
                category=runtime.category,
                channel=channel,
                inputs={"simulated": True, "uplift_now_applies": True},
                detail="simulated customer acted on the nudge; later attempts carry the uplift",
            )
        self._schedule_next(case, order, step_index, when, queue)

    # -- scheduling helpers -------------------------------------------------
    def _schedule_next(
        self,
        case: _Case,
        order: int,
        step_index: int,
        when: datetime,
        queue: list[tuple[datetime, int, int]],
    ) -> None:
        policy = case.policy
        assert policy is not None
        nxt = step_index + 1
        if nxt >= len(policy.plan):
            self._exhaust(case, when)
            return
        at = self.t0 + timedelta(hours=policy.plan[nxt].after_hours)
        if at <= when:
            at = when + timedelta(minutes=1)
        if at > self.deadline:
            self._exhaust(case, when)
            return
        self._emit(
            case_id=case.txn.case_id,
            actor=Actor.POLICY,
            action=Action.STEP_SCHEDULED,
            outcome=Outcome.RECORDED,
            ts=when,
            category=case.runtime.category if case.runtime else None,
            channel=policy.plan[nxt].channel,
            inputs={
                "step_index": nxt,
                "scheduled_for": at.isoformat(),
                "after_hours": policy.plan[nxt].after_hours,
            },
            detail=f"next step {policy.plan[nxt].channel} scheduled",
        )
        heapq.heappush(queue, (at, order, nxt))

    def _defer(
        self,
        case: _Case,
        order: int,
        step_index: int,
        when: datetime,
        until: datetime | None,
        rule: str,
        reason: str,
        queue: list[tuple[datetime, int, int]],
        actor: Actor,
    ) -> None:
        runtime = case.runtime
        assert runtime is not None
        runtime.deferrals_this_step += 1
        target = until or (when + timedelta(hours=1))
        if target <= when:
            target = when + timedelta(minutes=15)

        if self.policy_engine.deferral_budget_exceeded(runtime) or target > self.deadline:
            self._emit(
                case_id=case.txn.case_id,
                actor=actor,
                action=Action.STEP_DEFERRED,
                outcome=Outcome.DEFERRED,
                ts=when,
                category=runtime.category,
                rule=rule,
                inputs={"deferrals_this_step": runtime.deferrals_this_step},
                detail=f"{reason} (deferral budget exhausted; stopping instead)",
            )
            self._stop_case(
                case,
                rule or "quiet_hours",
                f"{reason}; deferral budget exhausted",
                when,
                escalate=self.config.policies.defaults.escalate_on_exhaustion,
            )
            return

        self._emit(
            case_id=case.txn.case_id,
            actor=actor,
            action=Action.STEP_DEFERRED,
            outcome=Outcome.DEFERRED,
            ts=when,
            category=runtime.category,
            rule=rule,
            channel=case.policy.plan[step_index].channel if case.policy else None,
            inputs={
                "deferred_until": target.isoformat(),
                "deferrals_this_step": runtime.deferrals_this_step,
            },
            detail=reason,
        )
        heapq.heappush(queue, (target, order, step_index))

    def _exhaust(self, case: _Case, when: datetime) -> None:
        runtime = case.runtime
        policy = case.policy
        assert runtime is not None and policy is not None
        spent = runtime.charge_attempts_made + case.txn.prior_attempt_count
        if spent >= policy.max_charge_attempts and policy.max_charge_attempts > 0:
            rule, reason = (
                "max_attempts_per_case",
                f"attempt budget spent ({spent}/{policy.max_charge_attempts}) with no recovery",
            )
        else:
            rule, reason = (
                "plan_exhausted",
                f"all {len(policy.plan)} planned steps executed with no recovery",
            )
        self._stop_case(
            case,
            rule,
            reason,
            when,
            escalate=self.config.policies.defaults.escalate_on_exhaustion,
        )

    # -- terminal states ----------------------------------------------------
    def _stop_case(
        self,
        case: _Case,
        rule: str,
        reason: str,
        when: datetime,
        escalate: bool,
        evidence: dict[str, float] | None = None,
        terminal_class: str = "policy_stop",
        status: CaseStatus = CaseStatus.STOPPED,
    ) -> None:
        runtime = case.runtime
        category = runtime.category if runtime else RootCause.UNKNOWN
        inputs: dict[str, object] = {
            "terminal_class": terminal_class,
            "charge_attempts_made": runtime.charge_attempts_made if runtime else 0,
            "contacts_sent": runtime.contacts_sent if runtime else 0,
            "prior_attempt_count": case.txn.prior_attempt_count,
        }
        if evidence:
            inputs["evidence"] = evidence
        self._emit(
            case_id=case.txn.case_id,
            actor=Actor.POLICY,
            action=Action.STOPPED,
            outcome=Outcome.STOPPED,
            ts=when,
            category=category,
            rule=rule,
            value_paise=case.txn.amount_paise,
            inputs=inputs,
            detail=reason,
        )
        case.status = status
        case.stop_rule = rule
        case.stop_reason = reason
        if runtime is not None:
            runtime.closed = True

        should_escalate = escalate or (
            case.txn.amount_paise >= self.config.policies.defaults.escalate_terminal_above_paise
        )
        if should_escalate:
            self._escalate(case, rule, reason, when)

    def _escalate(self, case: _Case, rule: str, reason: str, when: datetime) -> None:
        runtime = case.runtime
        record = self.escalation_builder.build(
            run_id=self.run_id,
            txn=case.txn,
            category=runtime.category if runtime else RootCause.UNKNOWN,
            stopping_rule=rule,
            reason=reason,
            charge_attempts=runtime.charge_attempts_made if runtime else 0,
            contacts=runtime.contacts_sent if runtime else 0,
            events=self.case_events.get(case.txn.case_id, []),
            escalated_at=when,
        )
        self.escalations.append(record)
        case.status = CaseStatus.ESCALATED if case.status is CaseStatus.STOPPED else case.status
        self._emit(
            case_id=case.txn.case_id,
            actor=Actor.HUMAN_QUEUE,
            action=Action.ESCALATED,
            outcome=Outcome.ESCALATED,
            ts=when,
            category=record.category,
            rule=rule,
            value_paise=case.txn.amount_paise,
            inputs={
                "priority_score": record.priority_score,
                "recommended_action": record.recommended_action,
                "decision_chain_length": len(record.decision_chain),
            },
            detail=f"queued for human review: {reason}",
        )

    def _trip_breaker(self, reason: str, evidence: dict[str, float], when: datetime) -> None:
        self.batch_runtime.tripped = True
        self._emit(
            case_id=BATCH_SCOPE,
            actor=Actor.POLICY,
            action=Action.CIRCUIT_BREAKER_TRIPPED,
            outcome=Outcome.STOPPED,
            ts=when,
            rule="batch_circuit_breaker",
            inputs=dict(evidence),
            detail=reason,
        )
        for case in self.cases.values():
            if case.status is CaseStatus.OPEN:
                self._stop_case(case, "batch_circuit_breaker", reason, when, escalate=True)

    def _close_remaining(self) -> None:
        if any(c.status is CaseStatus.OPEN for c in self.cases.values()):
            self._now = max(self._now, self.deadline)
        for case in self.cases.values():
            if case.status is CaseStatus.OPEN:
                self._stop_case(
                    case,
                    "horizon_reached",
                    f"recovery horizon of {self.horizon_hours}h elapsed with the case still open",
                    self.deadline,
                    escalate=self.config.policies.defaults.escalate_on_exhaustion,
                )

    # -- state assembly -----------------------------------------------------
    def _compliance_state(self, case: _Case, when: datetime) -> CaseComplianceState:
        runtime = case.runtime
        assert runtime is not None
        contacts = self.contact_log.get(case.txn.customer_id, [])
        return CaseComplianceState(
            charge_attempts_made=runtime.charge_attempts_made,
            prior_attempt_count=case.txn.prior_attempt_count,
            last_charge_at=runtime.last_charge_at,
            last_mandate_debit_at=(
                self.mandate_debits.get(case.txn.mandate_id) if case.txn.mandate_id else None
            ),
            contacts_last_24h=sum(1 for t in contacts if (when - t) <= timedelta(hours=24)),
            contacts_last_7d=sum(1 for t in contacts if (when - t) <= timedelta(days=7)),
        )

    # -- ordering for the escalation queue -----------------------------------
    def ranked_escalations(self) -> list[EscalationRecord]:
        return self.escalation_builder.rank(self.escalations)
