"""The ablation study: what each design decision is actually worth.

The value of an ablation is entirely in its honesty. These tests assert the
machinery cannot flatter the design: every variant must genuinely differ from
the full system, the reference row must be the full system, and a variant that
costs nothing must be reported as costing nothing.
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

import pytest
import yaml

from reclaim.ablation import (
    FULL_SYSTEM,
    VARIANTS,
    read_ablation,
    render_ablation,
    run_ablation,
    write_ablation,
)
from reclaim.config import AppConfig, load_config
from reclaim.models import AblationReport, Channel, RootCause

ABLATION_SEEDS = 3
ABLATION_SIZE = 60


@pytest.fixture(scope="module")
def ablation(config: AppConfig) -> AblationReport:
    return run_ablation(config, seeds=ABLATION_SEEDS, size=ABLATION_SIZE)


# ---------------------------------------------------------------------------
# The mutators actually mutate
# ---------------------------------------------------------------------------
def _mutated(config: AppConfig, name: str, tmp_path: Path) -> AppConfig:
    mutate = next(m for n, _, m in VARIANTS if n == name)
    target = tmp_path / name.replace(" ", "_") / "config"
    shutil.copytree(config.config_dir, target)
    assert mutate is not None
    path = target / "policies.yaml"
    data = yaml.safe_load(path.read_text())
    mutate(data)
    path.write_text(yaml.safe_dump(data, sort_keys=False))
    return load_config(target)


def test_no_customer_nudges_removes_every_contact_step(config: AppConfig, tmp_path: Path) -> None:
    variant = _mutated(config, "no customer nudges", tmp_path)
    for category in RootCause:
        plan = variant.policies.for_category(category).plan
        assert all(step.channel is Channel.RETRY_CHARGE for step in plan)
    # And the full system does have nudges, or the ablation would be vacuous.
    full_contacts = sum(
        1
        for category in RootCause
        for step in config.policies.for_category(category).plan
        if step.channel is not Channel.RETRY_CHARGE
    )
    assert full_contacts > 0


def test_naive_timing_moves_charges_onto_a_flat_schedule(config: AppConfig, tmp_path: Path) -> None:
    variant = _mutated(config, "naive 24/48/72h timing", tmp_path)
    for category in RootCause:
        charges = [
            s
            for s in variant.policies.for_category(category).plan
            if s.channel is Channel.RETRY_CHARGE
        ]
        assert [s.after_hours for s in charges] == [24.0 * (i + 1) for i in range(len(charges))]


def test_naive_timing_keeps_the_nudges(config: AppConfig, tmp_path: Path) -> None:
    """This variant isolates timing. If it also dropped contacts it would be
    measuring two things at once."""
    variant = _mutated(config, "naive 24/48/72h timing", tmp_path)
    contacts = sum(
        1
        for category in RootCause
        for step in variant.policies.for_category(category).plan
        if step.channel is not Channel.RETRY_CHARGE
    )
    assert contacts > 0


def test_no_routing_gives_every_recoverable_cause_the_same_plan(
    config: AppConfig, tmp_path: Path
) -> None:
    variant = _mutated(config, "no root-cause routing", tmp_path)
    plans = {
        category: [(s.after_hours, s.channel) for s in variant.policies.for_category(category).plan]
        for category in (
            RootCause.INSUFFICIENT_FUNDS,
            RootCause.EXPIRED_CARD,
            RootCause.TECHNICAL_ERROR,
            RootCause.ISSUER_SOFT_DECLINE,
        )
    }
    assert len({tuple(p) for p in plans.values()}) == 1, "all four should share one plan"


def test_no_routing_leaves_hard_stops_alone(config: AppConfig, tmp_path: Path) -> None:
    """Removing hard stops would test recklessness, not routing. The ablation
    must not quietly conflate the two."""
    variant = _mutated(config, "no root-cause routing", tmp_path)
    for category in (RootCause.HARD_DECLINE, RootCause.MANDATE_REVOKED, RootCause.UNKNOWN):
        policy = variant.policies.for_category(category)
        assert policy.immediate_terminal
        assert policy.max_charge_attempts == 0
        assert policy.plan == []


def test_no_cost_floor_disables_only_that_rule(config: AppConfig, tmp_path: Path) -> None:
    variant = _mutated(config, "no cost floor", tmp_path)
    assert not variant.policies.stopping_rules.cost_floor.enabled
    assert variant.policies.stopping_rules.batch_circuit_breaker.enabled
    assert variant.policies.stopping_rules.rolling_window_attempt_cap.enabled


# ---------------------------------------------------------------------------
# The report
# ---------------------------------------------------------------------------
def test_every_declared_variant_is_measured(ablation: AblationReport) -> None:
    assert [r.variant for r in ablation.rows] == [name for name, _, _ in VARIANTS]
    assert ablation.seeds == ABLATION_SEEDS


def test_the_full_system_is_the_reference_row(ablation: AblationReport) -> None:
    full = ablation.full
    assert full is not None
    assert full.variant == FULL_SYSTEM
    assert full.recovery_vs_full_pct == 0.0
    assert full.attempts_vs_full_pct == 0.0


def test_every_variant_carries_the_question_it_answers(ablation: AblationReport) -> None:
    for row in ablation.rows:
        assert row.question.strip(), f"{row.variant} does not say what it is testing"


def test_hard_stops_survive_every_ablation(ablation: AblationReport) -> None:
    """No amount of feature removal may produce a retry on a stolen card."""
    assert all(r.hard_stops_always_honoured for r in ablation.rows)


def test_removing_routing_or_timing_costs_recovery(ablation: AblationReport) -> None:
    """The core claim of the whole project: routing by root cause, and timing
    against each cause's recovery curve, are what earn the money."""
    by_name = {r.variant: r for r in ablation.rows}
    assert by_name["no root-cause routing"].recovery_vs_full_pct < 0
    assert by_name["naive 24/48/72h timing"].recovery_vs_full_pct < 0
    assert by_name["no customer nudges"].recovery_vs_full_pct < 0


def test_ranking_puts_the_most_valuable_decision_first(ablation: AblationReport) -> None:
    ranked = ablation.ranked
    assert FULL_SYSTEM not in [r.variant for r in ranked]
    costs = [r.recovery_vs_full_pct for r in ranked]
    assert costs == sorted(costs)


def test_a_variant_that_costs_nothing_is_reported_as_costing_nothing(
    ablation: AblationReport,
) -> None:
    """The cost floor is a spend-control rule. It should barely move recovery,
    and the table must say so rather than dress it up."""
    row = next(r for r in ablation.rows if r.variant == "no cost floor")
    assert abs(row.recovery_vs_full_pct) < 0.02
    assert row.attempts_vs_full_pct > 0, "disabling the floor should spend more attempts"


def test_ablation_is_deterministic(config: AppConfig) -> None:
    a = run_ablation(config, seeds=2, size=ABLATION_SIZE)
    b = run_ablation(config, seeds=2, size=ABLATION_SIZE)
    assert a.model_dump() == b.model_dump()


def test_ablation_never_writes_into_the_working_directory(
    config: AppConfig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    run_ablation(config, seeds=1, size=ABLATION_SIZE)
    assert list(tmp_path.iterdir()) == []


def test_ablation_roundtrips_through_disk(ablation: AblationReport, tmp_path: Path) -> None:
    path = write_ablation(ablation, tmp_path / "ablation.json")
    loaded = read_ablation(path)
    assert loaded is not None
    assert loaded.model_dump() == ablation.model_dump()
    assert read_ablation(tmp_path / "absent.json") is None


def test_rendered_ablation_names_what_each_decision_is_worth(
    ablation: AblationReport,
) -> None:
    text = render_ablation(ablation)
    assert "most valuable first" in text
    assert "no root-cause routing" in text
    assert "simulated" in text.lower()


def test_report_renders_the_ablation_when_present(
    config: AppConfig, batch: list, run_dir: Path, tmp_path: Path, ablation: AblationReport
) -> None:
    from helpers import build_run
    from reclaim.baseline import run_baseline
    from reclaim.classify import Classifier
    from reclaim.report import build_report

    result = build_run(config, batch, run_dir, tmp_path).execute()
    run_baseline(config, batch, 7, Classifier(config, llm_client=None), result.run_id, run_dir)
    write_ablation(ablation, run_dir / "ablation.json")

    html = build_report(result.run_id, run_dir, config)
    assert "earns the money" in html
    assert "no root-cause routing" in html
    assert "spend-control rule" in html


def test_report_is_valid_without_an_ablation(
    config: AppConfig, batch: list, run_dir: Path, tmp_path: Path
) -> None:
    from helpers import build_run
    from reclaim.baseline import run_baseline
    from reclaim.classify import Classifier
    from reclaim.report import build_report

    result = build_run(config, batch, run_dir, tmp_path).execute()
    run_baseline(config, batch, 7, Classifier(config, llm_client=None), result.run_id, run_dir)
    html = build_report(result.run_id, run_dir, config)
    assert "No ablation on record" in html
    assert tempfile  # keeps the import meaningful under lint


# ---------------------------------------------------------------------------
# The report must not manufacture a comparison it does not have
# ---------------------------------------------------------------------------
def test_report_states_plainly_when_there_is_no_baseline(
    config: AppConfig, batch: list, run_dir: Path, tmp_path: Path
) -> None:
    """Regression. With the baseline log absent the report compared the run
    against itself and rendered a +0.00 delta, which reads as a measured result
    and is not one."""
    from helpers import build_run
    from reclaim.report import build_report

    result = build_run(config, batch, run_dir, tmp_path).execute()
    assert not (run_dir / f"audit_{result.run_id}-baseline.jsonl").exists()

    html = build_report(result.run_id, run_dir, config)
    assert "not measured" in html
    assert "no claim" in html
    assert "+&#8377;0.00" not in html, "a zero delta must never be manufactured from a missing file"
    assert "The headline, and its caveat" not in html
    assert "Baseline recovered" not in html, "baseline columns are a self-comparison here"
    # Everything derived from the treatment log alone must still be present.
    assert "Recovery by root cause" in html
    assert "Stopping rules that fired" in html
    assert "Human escalation queue" in html


def test_report_makes_the_comparison_when_the_baseline_exists(
    config: AppConfig, batch: list, run_dir: Path, tmp_path: Path
) -> None:
    from helpers import build_run
    from reclaim.baseline import run_baseline
    from reclaim.classify import Classifier
    from reclaim.report import build_report

    result = build_run(config, batch, run_dir, tmp_path).execute()
    run_baseline(config, batch, 7, Classifier(config, llm_client=None), result.run_id, run_dir)

    html = build_report(result.run_id, run_dir, config)
    assert "The headline, and its caveat" in html
    assert "Baseline recovered" in html
    assert "not measured" not in html
