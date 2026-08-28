"""The naive baseline: retry everything three times, regardless of cause.

This exists so the headline is a delta rather than an absolute. It runs over
the identical batch with the identical seed and the identical simulator, so
every case sees the same random draw it would have seen in the treatment run;
only the strategy differs.

The baseline still classifies each case and writes the CLASSIFIED event, purely
so per-category metrics line up between the two runs. It then ignores the
answer entirely, which is the whole point: it re-presents stolen cards and
revoked mandates, it ignores quiet hours, consent, AFA thresholds and pre-debit
notification windows, and it has no stopping rules. Its audit log is written in
exactly the same vocabulary, so the same metrics code reads both.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

from .audit import AuditLog, audit_path
from .classify import Classifier
from .config import AppConfig
from .engine import BATCH_SCOPE
from .models import Action, Actor, AuditEvent, Channel, FailedTransaction, Outcome
from .simulate import OutcomeSimulator


def run_baseline(
    config: AppConfig,
    records: list[FailedTransaction],
    batch_seed: int,
    classifier: Classifier,
    run_id: str,
    out_dir: Path = Path("out"),
) -> tuple[str, Path, list[AuditEvent]]:
    cfg = config.policies.baseline
    baseline_run_id = f"{run_id}-baseline"
    path = audit_path(baseline_run_id, out_dir)
    simulator = OutcomeSimulator(config.simulation, batch_seed)
    t0 = max(r.attempt_ts for r in records).replace(minute=0, second=0, microsecond=0) + timedelta(
        hours=1
    )

    with AuditLog(baseline_run_id, path) as log:
        log.append(
            case_id=BATCH_SCOPE,
            actor=Actor.POLICY,
            action=Action.RUN_STARTED,
            outcome=Outcome.RECORDED,
            ts=t0,
            inputs={
                "strategy": cfg.name,
                "attempts": cfg.attempts,
                "interval_hours": cfg.interval_hours,
                "ignores_compliance": cfg.ignores_compliance,
                "ignores_hard_stops": cfg.ignores_hard_stops,
                "batch_seed": batch_seed,
                "cases": len(records),
                "simulated_outcomes": True,
            },
            detail=cfg.description,
        )

        # Ingest and classify every case first, then run the retry rounds in
        # time order across the whole batch. That keeps the baseline log's
        # timestamps monotonic in sequence order, so the same structural
        # verifier passes on both logs.
        classifications = {}
        for txn in records:
            log.append(
                case_id=txn.case_id,
                actor=Actor.POLICY,
                action=Action.CASE_INGESTED,
                outcome=Outcome.RECORDED,
                ts=t0,
                value_paise=txn.amount_paise,
                inputs={"decline_code": txn.decline_code, "amount_paise": txn.amount_paise},
                detail=f"{txn.decline_description} ({txn.decline_code})",
            )
            classification = classifier.classify(txn)
            classifications[txn.case_id] = classification
            log.append(
                case_id=txn.case_id,
                actor=classification.decided_by,
                action=Action.CLASSIFIED,
                outcome=Outcome.RECORDED,
                ts=t0,
                category=classification.category,
                rule=classification.rule_id,
                value_paise=txn.amount_paise,
                inputs={
                    "decline_code": txn.decline_code,
                    "layer": "rule" if classification.decided_by is Actor.RULE else "model",
                    "confidence": classification.confidence,
                    "baseline_uses_classification": False,
                },
                detail=(
                    f"{classification.rationale} "
                    "(baseline records the root cause and then ignores it)"
                ),
            )

        open_cases = list(records)
        when: datetime = t0
        for attempt in range(1, cfg.attempts + 1):
            when = t0 + timedelta(hours=cfg.interval_hours * attempt)
            still_open: list[FailedTransaction] = []
            for txn in open_cases:
                classification = classifications[txn.case_id]
                hours_since = (when - txn.attempt_ts).total_seconds() / 3600.0
                outcome = simulator.attempt(
                    txn.case_id, classification.category, attempt, hours_since, when, False
                )
                log.append(
                    case_id=txn.case_id,
                    actor=Actor.SIMULATOR,
                    action=Action.CHARGE_ATTEMPT,
                    outcome=Outcome.SUCCESS if outcome.success else Outcome.FAILURE,
                    ts=when,
                    category=classification.category,
                    channel=Channel.RETRY_CHARGE,
                    attempt_no=attempt,
                    value_paise=txn.amount_paise if outcome.success else 0,
                    inputs={
                        "simulated": True,
                        "success_probability": outcome.probability,
                        "roll": outcome.roll,
                        "hours_since_original_failure": outcome.hours_since_original,
                        "compliance_checked": False,
                    },
                    detail=(
                        f"naive re-presentment #{attempt} at p={outcome.probability:.3f}: "
                        f"{'authorised' if outcome.success else 'declined'}"
                    ),
                )
                if outcome.success:
                    log.append(
                        case_id=txn.case_id,
                        actor=Actor.POLICY,
                        action=Action.RECOVERED,
                        outcome=Outcome.SUCCESS,
                        ts=when,
                        category=classification.category,
                        attempt_no=attempt,
                        value_paise=txn.amount_paise,
                        inputs={"charge_attempts": attempt},
                        detail=f"recovered {txn.amount_paise / 100:.2f} INR",
                    )
                else:
                    still_open.append(txn)
            open_cases = still_open

        for txn in open_cases:
            log.append(
                case_id=txn.case_id,
                actor=Actor.POLICY,
                action=Action.STOPPED,
                outcome=Outcome.STOPPED,
                ts=when,
                category=classifications[txn.case_id].category,
                rule="naive_attempt_budget",
                value_paise=txn.amount_paise,
                inputs={
                    "terminal_class": "policy_stop",
                    "charge_attempts_made": cfg.attempts,
                },
                detail=f"{cfg.attempts} blind retries spent with no recovery and no escalation",
            )

        log.append(
            case_id=BATCH_SCOPE,
            actor=Actor.POLICY,
            action=Action.RUN_COMPLETED,
            outcome=Outcome.RECORDED,
            ts=when,
            inputs={"cases": len(records), "strategy": cfg.name},
            detail="baseline run complete",
        )
        events = log.events
    return baseline_run_id, path, events
