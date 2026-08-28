"""Components 7 and 8: audit continuity, and metric recomputation from the log.

The point of these tests is that the report cannot drift from the log. If a
number can be produced any way other than by reading the JSONL back off disk,
these tests should fail.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from helpers import build_run
from reclaim.audit import GENESIS_HASH, AuditLog, read_audit, verify_structure
from reclaim.config import AppConfig
from reclaim.metrics import compute_metrics, diff_metrics, read_metrics, write_metrics
from reclaim.models import (
    HARD_STOP_CATEGORIES,
    Action,
    Actor,
    FailedTransaction,
    Outcome,
    RootCause,
)

T0 = datetime(2026, 3, 2, tzinfo=UTC)


def make_log(path: Path, n: int = 5) -> None:
    with AuditLog("r1", path) as log:
        log.append(
            case_id="__batch__",
            actor=Actor.POLICY,
            action=Action.RUN_STARTED,
            outcome=Outcome.RECORDED,
            ts=T0,
        )
        for i in range(n):
            log.append(
                case_id=f"c{i}",
                actor=Actor.POLICY,
                action=Action.CASE_INGESTED,
                outcome=Outcome.RECORDED,
                ts=T0 + timedelta(minutes=i),
                value_paise=1000,
            )
        log.append(
            case_id="__batch__",
            actor=Actor.POLICY,
            action=Action.RUN_COMPLETED,
            outcome=Outcome.RECORDED,
            ts=T0 + timedelta(hours=1),
        )


# ---------------------------------------------------------------------------
# Audit integrity
# ---------------------------------------------------------------------------
def test_sequence_starts_at_one_and_is_contiguous(tmp_path: Path) -> None:
    make_log(tmp_path / "a.jsonl")
    events = read_audit(tmp_path / "a.jsonl")
    assert [e.seq for e in events] == list(range(1, len(events) + 1))


def test_hash_chain_starts_at_genesis_and_links_forward(tmp_path: Path) -> None:
    make_log(tmp_path / "a.jsonl")
    events = read_audit(tmp_path / "a.jsonl")
    assert events[0].prev_hash == GENESIS_HASH
    for previous, current in zip(events, events[1:], strict=False):
        assert current.prev_hash == previous.event_hash
        assert previous.event_hash


def test_a_clean_log_verifies(tmp_path: Path) -> None:
    make_log(tmp_path / "a.jsonl")
    assert verify_structure(tmp_path / "a.jsonl").ok


def test_editing_an_event_breaks_the_chain(tmp_path: Path) -> None:
    path = tmp_path / "a.jsonl"
    make_log(path)
    lines = path.read_text().splitlines()
    tampered = json.loads(lines[3])
    tampered["value_paise"] = 999_999_99
    lines[3] = json.dumps(tampered, sort_keys=True)
    path.write_text("\n".join(lines) + "\n")

    result = verify_structure(path)
    assert not result.ok
    assert any("hash chain" in name for name, ok, _ in result.checks if not ok)


def test_deleting_an_event_breaks_continuity(tmp_path: Path) -> None:
    path = tmp_path / "a.jsonl"
    make_log(path)
    lines = path.read_text().splitlines()
    del lines[3]
    path.write_text("\n".join(lines) + "\n")

    result = verify_structure(path)
    assert not result.ok
    names = {name for name, ok, _ in result.checks if not ok}
    assert any("sequence" in n or "hash chain" in n for n in names)


def test_reordering_events_is_detected(tmp_path: Path) -> None:
    path = tmp_path / "a.jsonl"
    make_log(path)
    lines = path.read_text().splitlines()
    lines[2], lines[3] = lines[3], lines[2]
    path.write_text("\n".join(lines) + "\n")
    assert not verify_structure(path).ok


def test_audit_events_are_immutable(tmp_path: Path) -> None:
    make_log(tmp_path / "a.jsonl")
    event = read_audit(tmp_path / "a.jsonl")[0]
    with pytest.raises(ValueError):
        event.value_paise = 1  # type: ignore[misc]


def test_writing_to_a_closed_log_fails(tmp_path: Path) -> None:
    log = AuditLog("r1", tmp_path / "b.jsonl")
    with pytest.raises(RuntimeError, match="not open"):
        log.append(
            case_id="c",
            actor=Actor.POLICY,
            action=Action.CASE_INGESTED,
            outcome=Outcome.RECORDED,
            ts=T0,
        )


# ---------------------------------------------------------------------------
# Metrics recomputation
# ---------------------------------------------------------------------------
def test_every_metric_recomputes_from_the_log_alone(
    config: AppConfig, batch: list[FailedTransaction], run_dir: Path, tmp_path: Path
) -> None:
    result = build_run(config, batch, run_dir, tmp_path).execute()
    in_memory = compute_metrics(result.events, "reclaimagent")
    write_metrics(in_memory, run_dir / "m.json")

    from_disk = compute_metrics(read_audit(result.audit_file), "reclaimagent")
    assert diff_metrics(read_metrics(run_dir / "m.json"), from_disk) == []


def test_value_at_risk_equals_the_batch(
    config: AppConfig, batch: list[FailedTransaction], run_dir: Path, tmp_path: Path
) -> None:
    result = build_run(config, batch, run_dir, tmp_path).execute()
    metrics = compute_metrics(read_audit(result.audit_file), "reclaimagent")
    assert metrics.value_at_risk_paise == sum(r.amount_paise for r in batch)
    assert metrics.cases == len(batch)


def test_recovered_value_equals_the_sum_of_recovered_events(
    config: AppConfig, batch: list[FailedTransaction], run_dir: Path, tmp_path: Path
) -> None:
    result = build_run(config, batch, run_dir, tmp_path).execute()
    events = read_audit(result.audit_file)
    metrics = compute_metrics(events, "reclaimagent")
    by_hand = sum(
        e.value_paise
        for e in events
        if e.action is Action.RECOVERED and e.outcome is Outcome.SUCCESS
    )
    assert metrics.recovered_paise == by_hand


def test_refusals_are_excluded_from_the_recovery_denominator(
    config: AppConfig, batch: list[FailedTransaction], run_dir: Path, tmp_path: Path
) -> None:
    result = build_run(config, batch, run_dir, tmp_path).execute()
    metrics = compute_metrics(read_audit(result.audit_file), "reclaimagent")
    assert metrics.addressable_value_paise == (
        metrics.value_at_risk_paise - metrics.compliance_refused_terminal_paise
    )
    if metrics.compliance_refused_terminal_paise:
        assert metrics.recovery_rate_on_addressable > metrics.recovery_rate_gross


def test_every_case_reaches_exactly_one_terminal_state(
    config: AppConfig, batch: list[FailedTransaction], run_dir: Path, tmp_path: Path
) -> None:
    result = build_run(config, batch, run_dir, tmp_path).execute()
    events = read_audit(result.audit_file)
    terminal: dict[str, int] = {}
    for event in events:
        if event.action in {Action.RECOVERED, Action.STOPPED}:
            terminal[event.case_id] = terminal.get(event.case_id, 0) + 1
    ingested = {e.case_id for e in events if e.action is Action.CASE_INGESTED}
    assert set(terminal) == ingested
    assert all(count == 1 for count in terminal.values())


def test_stops_by_rule_accounts_for_every_stopped_case(
    config: AppConfig, batch: list[FailedTransaction], run_dir: Path, tmp_path: Path
) -> None:
    result = build_run(config, batch, run_dir, tmp_path).execute()
    events = read_audit(result.audit_file)
    metrics = compute_metrics(events, "reclaimagent")
    assert sum(metrics.stops_by_rule.values()) == sum(
        1 for e in events if e.action is Action.STOPPED
    )


def test_metrics_reject_an_empty_log() -> None:
    with pytest.raises(ValueError, match="empty audit log"):
        compute_metrics([], "x")


# ---------------------------------------------------------------------------
# The headline safety invariant
# ---------------------------------------------------------------------------
def test_hard_stop_cases_never_accumulate_an_attempt(
    config: AppConfig, batch: list[FailedTransaction], run_dir: Path, tmp_path: Path
) -> None:
    result = build_run(config, batch, run_dir, tmp_path).execute()
    events = read_audit(result.audit_file)
    hard = {
        e.case_id
        for e in events
        if e.action is Action.CLASSIFIED and e.category in HARD_STOP_CATEGORIES
    }
    assert hard, "the fixture batch must contain hard-stop cases for this test to mean anything"
    attempted = {e.case_id for e in events if e.action is Action.CHARGE_ATTEMPT}
    assert hard & attempted == set()

    metrics = compute_metrics(events, "reclaimagent")
    assert metrics.hard_stop_cases == len(hard)
    assert metrics.hard_stop_cases_with_zero_attempts == len(hard)
    assert metrics.correctly_stopped_rate == 1.0


def test_unknown_cases_are_escalated_never_retried(
    config: AppConfig, batch: list[FailedTransaction], run_dir: Path, tmp_path: Path
) -> None:
    result = build_run(config, batch, run_dir, tmp_path).execute()
    events = read_audit(result.audit_file)
    unknown = {
        e.case_id
        for e in events
        if e.action is Action.CLASSIFIED and e.category is RootCause.UNKNOWN
    }
    assert unknown
    attempted = {e.case_id for e in events if e.action is Action.CHARGE_ATTEMPT}
    escalated = {e.case_id for e in events if e.action is Action.ESCALATED}
    assert unknown & attempted == set()
    assert unknown <= escalated


def test_hard_stop_cases_stop_at_the_moment_of_classification(
    config: AppConfig, batch: list[FailedTransaction], run_dir: Path, tmp_path: Path
) -> None:
    """Not merely 'no attempt happened' but 'no attempt was ever schedulable':
    the stop must land immediately after policy selection."""
    result = build_run(config, batch, run_dir, tmp_path).execute()
    events = read_audit(result.audit_file)
    by_case: dict[str, list[Action]] = {}
    for event in events:
        by_case.setdefault(event.case_id, []).append(event.action)
    for event in events:
        if event.action is Action.CLASSIFIED and event.category in HARD_STOP_CATEGORIES:
            actions = by_case[event.case_id]
            assert actions[:4] == [
                Action.CASE_INGESTED,
                Action.CLASSIFIED,
                Action.POLICY_SELECTED,
                Action.STOPPED,
            ]
