"""The sensitivity sweep, and the cost accounting that makes the recovery
figure a net one rather than a gross one.

The sweep exists because a single seed's delta is an anecdote. These tests
assert the sweep is honest about that: it must report losses if there are any,
and it must never quietly touch the real out/ directory.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from helpers import build_run
from reclaim.audit import read_audit
from reclaim.benchmark import read_benchmark, render_benchmark, run_benchmark, write_benchmark
from reclaim.config import AppConfig
from reclaim.metrics import compute_metrics
from reclaim.models import Action, BenchmarkReport, BenchmarkRow, Channel, FailedTransaction

SWEEP_SEEDS = 4
SWEEP_SIZE = 60


@pytest.fixture(scope="module")
def sweep(config: AppConfig) -> BenchmarkReport:
    return run_benchmark(config, seeds=SWEEP_SEEDS, size=SWEEP_SIZE)


# ---------------------------------------------------------------------------
# Cost accounting
# ---------------------------------------------------------------------------
def test_every_billable_action_records_what_it_cost(
    config: AppConfig, batch: list[FailedTransaction], run_dir: Path, tmp_path: Path
) -> None:
    result = build_run(config, batch, run_dir, tmp_path).execute()
    billable = [
        e for e in result.events if e.action in {Action.CHARGE_ATTEMPT, Action.CONTACT_SENT}
    ]
    assert billable
    for event in billable:
        assert event.inputs.get("action_cost_paise", 0) > 0, (
            f"seq {event.seq} spent money without recording what it cost"
        )


def test_action_cost_matches_the_configured_price_of_the_channel(
    config: AppConfig, batch: list[FailedTransaction], run_dir: Path, tmp_path: Path
) -> None:
    prices = config.policies.stopping_rules.cost_floor
    result = build_run(config, batch, run_dir, tmp_path).execute()
    for event in result.events:
        if event.action in {Action.CHARGE_ATTEMPT, Action.CONTACT_SENT}:
            assert event.channel is not None
            assert event.inputs["action_cost_paise"] == prices.cost_of(event.channel)


def test_total_cost_recomputes_from_the_log(
    config: AppConfig, batch: list[FailedTransaction], run_dir: Path, tmp_path: Path
) -> None:
    result = build_run(config, batch, run_dir, tmp_path).execute()
    events = read_audit(result.audit_file)
    metrics = compute_metrics(events, "x")
    by_hand = sum(
        int(e.inputs.get("action_cost_paise", 0))
        for e in events
        if e.action in {Action.CHARGE_ATTEMPT, Action.CONTACT_SENT}
    )
    assert metrics.action_cost_paise == by_hand
    assert metrics.net_recovered_paise == metrics.recovered_paise - by_hand


def test_cost_of_acting_is_a_small_fraction_of_what_is_recovered(
    config: AppConfig, batch: list[FailedTransaction], run_dir: Path, tmp_path: Path
) -> None:
    """Not a tuning assertion, a sanity one: if acting ever costs more than it
    returns, the cost floor is not doing its job."""
    result = build_run(config, batch, run_dir, tmp_path).execute()
    metrics = compute_metrics(result.events, "x")
    assert metrics.net_recovered_paise > 0
    assert metrics.action_cost_paise < metrics.recovered_paise


def test_recovered_per_attempt_is_the_readable_form_of_attempts_per_rupee(
    config: AppConfig, batch: list[FailedTransaction], run_dir: Path, tmp_path: Path
) -> None:
    result = build_run(config, batch, run_dir, tmp_path).execute()
    metrics = compute_metrics(result.events, "x")
    assert metrics.recovered_paise_per_attempt == pytest.approx(
        metrics.recovered_paise / metrics.charge_attempts, rel=0.01
    )


def test_the_baseline_is_costed_on_the_same_basis(
    config: AppConfig, batch: list[FailedTransaction], run_dir: Path, tmp_path: Path
) -> None:
    """A cost comparison is meaningless if only one side is metered."""
    from reclaim.baseline import run_baseline
    from reclaim.classify import Classifier

    result = build_run(config, batch, run_dir, tmp_path).execute()
    _, _, baseline_events = run_baseline(
        config, batch, 7, Classifier(config, llm_client=None), result.run_id, run_dir
    )
    price = config.policies.stopping_rules.cost_floor.cost_of(Channel.RETRY_CHARGE)
    attempts = [e for e in baseline_events if e.action is Action.CHARGE_ATTEMPT]
    assert attempts
    assert all(e.inputs["action_cost_paise"] == price for e in attempts)

    baseline_metrics = compute_metrics(baseline_events, "naive")
    treatment_metrics = compute_metrics(result.events, "reclaim")
    assert baseline_metrics.action_cost_paise > treatment_metrics.action_cost_paise, (
        "the baseline makes far more attempts, so it must cost more to run"
    )


# ---------------------------------------------------------------------------
# The sweep
# ---------------------------------------------------------------------------
def test_sweep_runs_every_seed_it_was_asked_for(sweep: BenchmarkReport) -> None:
    assert sweep.seeds == SWEEP_SEEDS
    assert len(sweep.rows) == SWEEP_SEEDS
    assert [r.seed for r in sweep.rows] == list(range(1, SWEEP_SEEDS + 1))
    assert all(r.cases == SWEEP_SIZE for r in sweep.rows)


def test_sweep_never_writes_into_the_real_output_directory(
    config: AppConfig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A sweep runs dozens of pipelines. If it wrote audit logs to out/ it would
    clobber the artefacts of the run the report is built from."""
    monkeypatch.chdir(tmp_path)
    run_benchmark(config, seeds=1, size=SWEEP_SIZE)
    assert not (tmp_path / "out").exists()
    assert list(tmp_path.iterdir()) == []


def test_hard_stops_are_honoured_on_every_seed(sweep: BenchmarkReport) -> None:
    """The one invariant that must not depend on the batch."""
    assert sweep.hard_stops_always_honoured
    for row in sweep.rows:
        assert row.correctly_stopped_rate == 1.0
        assert row.hard_stop_cases > 0


def test_summary_statistics_agree_with_the_rows(sweep: BenchmarkReport) -> None:
    pcts = [r.delta_pct for r in sweep.rows]
    assert sweep.wins == sum(1 for p in pcts if p > 0)
    assert sweep.losses == sum(1 for r in sweep.rows if r.delta_paise < 0)
    assert sweep.wins + sweep.losses <= sweep.seeds
    assert sweep.worst_delta_pct == min(pcts)
    assert sweep.best_delta_pct == max(pcts)
    assert min(pcts) <= sweep.median_delta_pct <= max(pcts)
    assert min(pcts) <= sweep.mean_delta_pct <= max(pcts)
    assert sweep.total_delta_paise == sum(r.delta_paise for r in sweep.rows)


def test_sweep_reports_a_loss_honestly() -> None:
    """Constructed, not run: the summary must not launder a losing seed."""
    rows = [
        BenchmarkRow(
            seed=1,
            cases=60,
            addressable_cases=50,
            treatment_recovered_paise=100,
            baseline_recovered_paise=200,
            treatment_recovery_rate=0.20,
            delta_paise=-100,
            delta_pct=-0.5,
            treatment_attempts=10,
            baseline_attempts=30,
            attempt_delta=-20,
            attempt_delta_pct=-0.667,
            hard_stop_cases=3,
            correctly_stopped_rate=1.0,
            circuit_breaker_tripped=False,
        ),
        BenchmarkRow(
            seed=2,
            cases=60,
            addressable_cases=50,
            treatment_recovered_paise=300,
            baseline_recovered_paise=200,
            treatment_recovery_rate=0.60,
            delta_paise=100,
            delta_pct=0.5,
            treatment_attempts=10,
            baseline_attempts=30,
            attempt_delta=-20,
            attempt_delta_pct=-0.667,
            hard_stop_cases=3,
            correctly_stopped_rate=1.0,
            circuit_breaker_tripped=False,
        ),
    ]
    report = BenchmarkReport(seeds=2, batch_size=60, config_fingerprint="x", rows=rows)
    assert report.wins == 1
    assert report.losses == 1
    assert report.worst_delta_pct == -0.5
    assert "1/2" in render_benchmark(report)
    assert "-50.0%" in render_benchmark(report)


def test_the_rate_distribution_gets_the_same_treatment_as_the_delta(
    sweep: BenchmarkReport,
) -> None:
    """Reporting the spread only for the figure where the headline seed looks
    conservative, and not for the one where it looks flattering, would be
    selective honesty. Both distributions must be available."""
    rates = [r.treatment_recovery_rate for r in sweep.rows]
    assert all(0.0 <= rate <= 1.0 for rate in rates)
    assert sweep.worst_recovery_rate == min(rates)
    assert sweep.best_recovery_rate == max(rates)
    assert min(rates) <= sweep.median_recovery_rate <= max(rates)
    assert min(rates) <= sweep.mean_recovery_rate <= max(rates)
    assert sweep.seeds_below_rate(max(rates) + 0.01) == len(rates)
    assert sweep.seeds_below_rate(min(rates)) == 0

    text = render_benchmark(sweep)
    assert "recovery rate, mean / median" in text
    assert "recovery rate, worst / best seed" in text


def test_sweep_is_deterministic(config: AppConfig) -> None:
    a = run_benchmark(config, seeds=2, size=SWEEP_SIZE)
    b = run_benchmark(config, seeds=2, size=SWEEP_SIZE)
    assert a.model_dump() == b.model_dump()


def test_sweep_roundtrips_through_disk(sweep: BenchmarkReport, tmp_path: Path) -> None:
    path = write_benchmark(sweep, tmp_path / "benchmark.json")
    assert read_benchmark(path) is not None
    assert read_benchmark(path).model_dump() == sweep.model_dump()  # type: ignore[union-attr]
    assert read_benchmark(tmp_path / "absent.json") is None


def test_rendered_sweep_names_the_worst_seed_not_just_the_mean(
    sweep: BenchmarkReport,
) -> None:
    text = render_benchmark(sweep)
    assert "worst / best seed" in text
    assert "simulated" in text.lower()


def test_report_includes_the_sweep_when_one_is_on_disk(
    config: AppConfig,
    batch: list[FailedTransaction],
    run_dir: Path,
    tmp_path: Path,
    sweep: BenchmarkReport,
) -> None:
    from reclaim.baseline import run_baseline
    from reclaim.classify import Classifier
    from reclaim.report import build_report

    result = build_run(config, batch, run_dir, tmp_path).execute()
    run_baseline(config, batch, 7, Classifier(config, llm_client=None), result.run_id, run_dir)
    write_benchmark(sweep, run_dir / "benchmark.json")

    html = build_report(result.run_id, run_dir, config)
    assert "lucky batch" in html
    assert f"{sweep.wins} / {sweep.seeds}" in html
    assert "Delta, worst seed" in html


def test_report_is_still_valid_without_a_sweep(
    config: AppConfig, batch: list[FailedTransaction], run_dir: Path, tmp_path: Path
) -> None:
    from reclaim.baseline import run_baseline
    from reclaim.classify import Classifier
    from reclaim.report import build_report

    result = build_run(config, batch, run_dir, tmp_path).execute()
    run_baseline(config, batch, 7, Classifier(config, llm_client=None), result.run_id, run_dir)
    html = build_report(result.run_id, run_dir, config)
    assert "No sensitivity sweep on record" in html
    assert "reclaim benchmark" in html


# ---------------------------------------------------------------------------
# An unrecorded cost is unknown, not zero
# ---------------------------------------------------------------------------
def _strip_cost_stamps(events: list) -> list:  # type: ignore[type-arg]
    """What an audit log written before cost tracking existed looks like."""
    from reclaim.models import Action as A

    out = []
    for e in events:
        if e.action in {A.CHARGE_ATTEMPT, A.CONTACT_SENT}:
            e = e.model_copy(
                update={"inputs": {k: v for k, v in e.inputs.items() if k != "action_cost_paise"}}
            )
        out.append(e)
    return out


def test_a_log_without_cost_stamps_reports_unknown_not_zero(
    config: AppConfig, batch: list[FailedTransaction], run_dir: Path, tmp_path: Path
) -> None:
    """Regression. Summing a missing field to 0 published "cost of acting
    Rs 0.00" and a net figure equal to the gross one, which reads as a
    measurement of a cheap run rather than an absence of data."""
    result = build_run(config, batch, run_dir, tmp_path).execute()

    current = compute_metrics(result.events, "x")
    assert current.action_cost_recorded
    assert current.action_cost_paise > 0

    legacy = compute_metrics(_strip_cost_stamps(result.events), "x")
    assert not legacy.action_cost_recorded, (
        "a log whose billable actions carry no cost stamp must not claim a cost"
    )


def test_the_headline_refuses_to_state_a_net_figure_it_cannot_support(
    config: AppConfig, batch: list[FailedTransaction], run_dir: Path, tmp_path: Path
) -> None:
    from reclaim.metrics import headline

    result = build_run(config, batch, run_dir, tmp_path).execute()
    legacy = compute_metrics(_strip_cost_stamps(result.events), "x")
    line = next(line for line in headline(legacy) if "cost of acting" in line)
    assert "not recorded" in line
    assert "0.00" not in line, "a cost of 0.00 here would be a fabricated measurement"


def test_the_report_says_not_recorded_rather_than_zero(
    config: AppConfig, batch: list[FailedTransaction], run_dir: Path, tmp_path: Path
) -> None:
    from reclaim.audit import AuditLog
    from reclaim.report import build_report

    result = build_run(config, batch, run_dir, tmp_path).execute()
    stripped = _strip_cost_stamps(result.events)

    legacy_id = f"{result.run_id}-legacy"
    with AuditLog(legacy_id, run_dir / f"audit_{legacy_id}.jsonl") as log:
        for e in stripped:
            log.append(
                case_id=e.case_id,
                actor=e.actor,
                action=e.action,
                outcome=e.outcome,
                ts=e.ts,
                value_paise=e.value_paise,
                category=e.category,
                rule=e.rule,
                channel=e.channel,
                attempt_no=e.attempt_no,
                inputs=e.inputs,
                detail=e.detail,
            )

    html = build_report(legacy_id, run_dir, config)
    assert "not recorded" in html
    assert "predates per-action cost tracking" in html


# ---------------------------------------------------------------------------
# Config contract the sweep for this defect class exposed
# ---------------------------------------------------------------------------
def test_a_recoverable_policy_must_actually_have_a_plan(config: AppConfig) -> None:
    """`plan` defaults to an empty list when the key is missing, so a typo in
    policies.yaml would produce a recoverable category that silently does
    nothing and terminates as plan_exhausted."""
    from reclaim.models import RootCause

    for category in RootCause:
        policy = config.policies.for_category(category)
        if policy.recoverable:
            assert policy.plan, f"{category} is recoverable but plans no steps"
            assert policy.max_charge_attempts > 0
            assert policy.allowed_channels
        else:
            assert not policy.plan, f"{category} is non-recoverable but plans steps"
            assert policy.max_charge_attempts == 0
