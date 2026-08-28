"""End-to-end behaviour: determinism, escalation, refusal paths, the breaker,
and the CLI surface the demo depends on.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
import yaml

from helpers import build_run
from reclaim.cli import main
from reclaim.config import AppConfig, load_config
from reclaim.escalation import read_escalations
from reclaim.generate import generate_batch
from reclaim.metrics import build_comparison, compute_metrics
from reclaim.models import Action, FailedTransaction, Outcome
from reclaim.replay import case_events, pick_demo_cases, render_replay
from reclaim.verify import verify_run


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------
def test_same_seed_produces_a_byte_identical_audit_log(
    config: AppConfig, batch: list[FailedTransaction], tmp_path: Path
) -> None:
    """The audit log carries no wall-clock time and no random ids, so two runs
    over the same batch must be indistinguishable on disk."""
    first = build_run(config, batch, tmp_path / "a", tmp_path).execute()
    second = build_run(config, batch, tmp_path / "b", tmp_path).execute()
    assert first.run_id == second.run_id
    assert first.audit_file.read_bytes() == second.audit_file.read_bytes()


def test_same_seed_produces_an_identical_escalation_queue(
    config: AppConfig, batch: list[FailedTransaction], tmp_path: Path
) -> None:
    first = build_run(config, batch, tmp_path / "a", tmp_path).execute()
    second = build_run(config, batch, tmp_path / "b", tmp_path).execute()
    assert first.escalation_file.read_bytes() == second.escalation_file.read_bytes()


def test_a_different_seed_produces_a_different_run(config: AppConfig, tmp_path: Path) -> None:
    a = build_run(config, generate_batch(7, 60, config), tmp_path / "a", tmp_path, seed=7)
    b = build_run(config, generate_batch(8, 60, config), tmp_path / "b", tmp_path, seed=8)
    assert a.execute().run_id != b.execute().run_id


def test_config_changes_change_the_run_id(
    config: AppConfig, batch: list[FailedTransaction], tmp_path: Path, config_copy: Path
) -> None:
    """The run id folds in a config fingerprint, so a policy edit cannot quietly
    overwrite the audit log of a run made under the old policy."""
    policies = config_copy / "policies.yaml"
    text = policies.read_text().replace("max_charge_attempts: 3", "max_charge_attempts: 2", 1)
    policies.write_text(text)
    changed = load_config(config_copy)
    baseline_run = build_run(config, batch, tmp_path / "a", tmp_path)
    changed_run = build_run(changed, batch, tmp_path / "b", tmp_path)
    assert baseline_run.run_id != changed_run.run_id


# ---------------------------------------------------------------------------
# Compliance refusal paths through the whole engine
# ---------------------------------------------------------------------------
def test_refusals_appear_in_the_log_with_the_rule_that_produced_them(
    config: AppConfig, batch: list[FailedTransaction], run_dir: Path, tmp_path: Path
) -> None:
    result = build_run(config, batch, run_dir, tmp_path).execute()
    refusals = [e for e in result.events if e.action is Action.COMPLIANCE_REFUSAL]
    assert refusals, "the fixture batch must exercise at least one refusal"
    for event in refusals:
        assert event.rule, "a refusal with no named rule is not auditable"
        assert "." in event.rule, "a refusal must name a constant in compliance.yaml"
        assert event.outcome is Outcome.REFUSED
        assert event.detail


def test_a_refusal_is_not_counted_as_a_failed_attempt(
    config: AppConfig, batch: list[FailedTransaction], run_dir: Path, tmp_path: Path
) -> None:
    result = build_run(config, batch, run_dir, tmp_path).execute()
    refused_cases = {
        e.case_id
        for e in result.events
        if e.action is Action.STOPPED and e.inputs.get("terminal_class") == "compliance_refusal"
    }
    attempted = {e.case_id for e in result.events if e.action is Action.CHARGE_ATTEMPT}
    metrics = compute_metrics(result.events, "x")
    assert metrics.compliance_refused_terminal_paise > 0
    # A refused case may have had earlier permitted attempts, but a case refused
    # on its very first step must never show one.
    first_step_refusals = refused_cases - attempted
    assert first_step_refusals


def test_consent_refusal_does_not_kill_the_charge_path(
    config: AppConfig, run_dir: Path, tmp_path: Path
) -> None:
    """A customer who cannot be messaged can still have an existing
    authorisation re-presented. Conflating the two strands recoverable money."""
    records = [
        r.model_copy(update={"contact_consent": False}) for r in generate_batch(21, 120, config)
    ]
    result = build_run(config, records, run_dir, tmp_path, seed=21).execute()
    refused = {
        e.case_id
        for e in result.events
        if e.action is Action.COMPLIANCE_REFUSAL and e.rule == "consent.required_before_contact"
    }
    attempted = {e.case_id for e in result.events if e.action is Action.CHARGE_ATTEMPT}
    assert refused, "this batch should refuse contact for every case"
    assert refused & attempted, "consent-refused cases must still reach the charge rail"


# ---------------------------------------------------------------------------
# Escalation queue
# ---------------------------------------------------------------------------
def test_escalations_carry_the_reasoning_a_human_needs(
    config: AppConfig, batch: list[FailedTransaction], run_dir: Path, tmp_path: Path
) -> None:
    result = build_run(config, batch, run_dir, tmp_path).execute()
    assert result.escalations
    for record in result.escalations:
        assert record.stopping_rule
        assert record.recommended_action
        assert record.decision_chain, "an escalation without its decision chain is useless"
        assert record.amount_at_risk_paise > 0
        assert record.priority_rank >= 1


def test_escalation_queue_is_ranked_by_recoverable_value(
    config: AppConfig, batch: list[FailedTransaction], run_dir: Path, tmp_path: Path
) -> None:
    result = build_run(config, batch, run_dir, tmp_path).execute()
    scores = [r.priority_score for r in result.escalations]
    assert scores == sorted(scores, reverse=True)
    assert [r.priority_rank for r in result.escalations] == list(range(1, len(scores) + 1))


def test_unknown_and_refused_and_exhausted_all_reach_the_queue(
    config: AppConfig, batch: list[FailedTransaction], run_dir: Path, tmp_path: Path
) -> None:
    result = build_run(config, batch, run_dir, tmp_path).execute()
    rules = {r.stopping_rule for r in result.escalations}
    assert "unknown_requires_human" in rules
    assert "max_attempts_per_case" in rules
    assert any("." in rule for rule in rules), "a compliance refusal should be queued too"


def test_queue_on_disk_matches_the_queue_in_memory(
    config: AppConfig, batch: list[FailedTransaction], run_dir: Path, tmp_path: Path
) -> None:
    result = build_run(config, batch, run_dir, tmp_path).execute()
    on_disk = read_escalations(result.escalation_file)
    assert [r.case_id for r in on_disk] == [r.case_id for r in result.escalations]


# ---------------------------------------------------------------------------
# Circuit breaker, end to end
# ---------------------------------------------------------------------------
def test_circuit_breaker_halts_the_batch_and_escalates_everything_open(
    config_copy: Path, tmp_path: Path, run_dir: Path
) -> None:
    """Force a total outage by zeroing every success rate in the simulator, then
    confirm the run halts rather than grinding through the whole plan."""
    sim = config_copy / "simulation.yaml"
    data = yaml.safe_load(sim.read_text())
    for block in data["categories"].values():
        block["base"] = 0.0
    sim.write_text(yaml.safe_dump(data))

    cfg = load_config(config_copy)
    records = generate_batch(31, 200, cfg)
    result = build_run(cfg, records, run_dir, tmp_path, seed=31).execute()

    tripped = [e for e in result.events if e.action is Action.CIRCUIT_BREAKER_TRIPPED]
    assert tripped, "a total outage must trip the breaker"
    assert tripped[0].rule == "batch_circuit_breaker"
    assert tripped[0].inputs["observed_successes"] == 0.0

    metrics = compute_metrics(result.events, "x")
    assert metrics.circuit_breaker_tripped
    assert metrics.stops_by_rule.get("batch_circuit_breaker", 0) > 0
    # Everything still open at the moment of the trip goes to a human.
    halted = {
        e.case_id
        for e in result.events
        if e.action is Action.STOPPED and e.rule == "batch_circuit_breaker"
    }
    escalated = {e.case_id for e in result.events if e.action is Action.ESCALATED}
    assert halted <= escalated


def test_no_breaker_trip_on_a_healthy_batch(
    config: AppConfig, batch: list[FailedTransaction], run_dir: Path, tmp_path: Path
) -> None:
    result = build_run(config, batch, run_dir, tmp_path).execute()
    assert not compute_metrics(result.events, "x").circuit_breaker_tripped


# ---------------------------------------------------------------------------
# Replay
# ---------------------------------------------------------------------------
def test_replay_walks_a_recovery_and_a_handled_failure(
    config: AppConfig, batch: list[FailedTransaction], run_dir: Path, tmp_path: Path
) -> None:
    result = build_run(config, batch, run_dir, tmp_path).execute()
    success, stopped = pick_demo_cases(result.run_id, run_dir)
    assert success and stopped

    success_text = render_replay(case_events(result.run_id, success, run_dir), success)
    assert "ROOT CAUSE" in success_text
    assert "RECOVERED" in success_text
    assert "value recovered" in success_text

    stopped_text = render_replay(case_events(result.run_id, stopped, run_dir), stopped)
    assert "Stopped by rule" in stopped_text
    assert "Charge attempts spent: 0" in stopped_text


def test_replay_of_an_unknown_case_says_so(
    config: AppConfig, batch: list[FailedTransaction], run_dir: Path, tmp_path: Path
) -> None:
    result = build_run(config, batch, run_dir, tmp_path).execute()
    assert "No audit events" in render_replay(
        case_events(result.run_id, "case_does_not_exist", run_dir), "case_does_not_exist"
    )


# ---------------------------------------------------------------------------
# Baseline comparison
# ---------------------------------------------------------------------------
def test_baseline_retries_what_the_policy_engine_refuses_to(
    config: AppConfig, batch: list[FailedTransaction], run_dir: Path, tmp_path: Path
) -> None:
    from reclaim.baseline import run_baseline
    from reclaim.classify import Classifier

    result = build_run(config, batch, run_dir, tmp_path).execute()
    _, _, baseline_events = run_baseline(
        config, batch, 7, Classifier(config, llm_client=None), result.run_id, run_dir
    )
    report = build_comparison(result.events, baseline_events)

    assert report.baseline_attempts_on_hard_stop_cases > 0
    assert report.baseline_attempts_on_unknown_cases > 0
    assert report.treatment.hard_stop_cases_with_zero_attempts == report.treatment.hard_stop_cases
    assert report.baseline.hard_stop_cases_with_zero_attempts == 0
    assert report.treatment_like_for_like.cases <= report.treatment.cases


def test_the_two_strategies_see_the_same_batch(
    config: AppConfig, batch: list[FailedTransaction], run_dir: Path, tmp_path: Path
) -> None:
    from reclaim.baseline import run_baseline
    from reclaim.classify import Classifier

    result = build_run(config, batch, run_dir, tmp_path).execute()
    _, _, baseline_events = run_baseline(
        config, batch, 7, Classifier(config, llm_client=None), result.run_id, run_dir
    )
    treatment_cases = {e.case_id for e in result.events if e.action is Action.CASE_INGESTED}
    baseline_cases = {e.case_id for e in baseline_events if e.action is Action.CASE_INGESTED}
    assert treatment_cases == baseline_cases


# ---------------------------------------------------------------------------
# The CLI, exactly as the demo runs it
# ---------------------------------------------------------------------------
def test_full_cli_pipeline(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    data_dir = tmp_path / "data"
    out_dir = tmp_path / "out"
    config_dir = Path(__file__).resolve().parents[1] / "config"
    common = ["--config-dir", str(config_dir), "--out-dir", str(out_dir)]

    assert (
        main([*common, "generate", "--seed", "5", "--size", "80", "--data-dir", str(data_dir)]) == 0
    )
    assert main([*common, "run", "--batch", str(data_dir / "batch_5.jsonl"), "--no-llm"]) == 0
    run_output = capsys.readouterr().out
    assert "SIMULATED" in run_output
    assert "Like-for-like" in run_output

    run_id = (out_dir / "latest_run.txt").read_text().strip()
    assert main([*common, "verify-audit"]) == 0
    assert main([*common, "report"]) == 0
    assert main([*common, "replay", "--case", "@success"]) == 0
    assert main([*common, "replay", "--case", "@stopped"]) == 0
    assert main([*common, "queue"]) == 0

    report = (out_dir / f"report_{run_id}.html").read_text()
    assert "Simulated outcomes, synthetic data" in report
    assert "Worked example" in report
    assert "&#8377;" in report, "the report should carry rupee figures"
    assert "http://" not in report and "https://" not in report, "the report must be self-contained"


def test_verify_audit_passes_on_a_real_run(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    out_dir = tmp_path / "out"
    config_dir = Path(__file__).resolve().parents[1] / "config"
    common = ["--config-dir", str(config_dir), "--out-dir", str(out_dir)]
    main([*common, "generate", "--seed", "5", "--size", "60", "--data-dir", str(data_dir)])
    main([*common, "run", "--batch", str(data_dir / "batch_5.jsonl"), "--no-llm"])
    run_id = (out_dir / "latest_run.txt").read_text().strip()

    result = verify_run(run_id, out_dir)
    assert result.ok, result.render()


def test_verify_audit_fails_when_the_reported_metrics_are_edited(tmp_path: Path) -> None:
    """The whole point of verify-audit: a number that does not come from the log
    must be caught."""
    data_dir = tmp_path / "data"
    out_dir = tmp_path / "out"
    config_dir = Path(__file__).resolve().parents[1] / "config"
    common = ["--config-dir", str(config_dir), "--out-dir", str(out_dir)]
    main([*common, "generate", "--seed", "5", "--size", "60", "--data-dir", str(data_dir)])
    main([*common, "run", "--batch", str(data_dir / "batch_5.jsonl"), "--no-llm"])
    run_id = (out_dir / "latest_run.txt").read_text().strip()

    metrics_file = out_dir / f"metrics_{run_id}.json"
    reported = json.loads(metrics_file.read_text())
    reported["recovered_paise"] += 100_000_00  # a rupee figure the log cannot justify
    metrics_file.write_text(json.dumps(reported))
    result = verify_run(run_id, out_dir)
    assert not result.ok
    assert any("recompute" in name for name, ok, _ in result.checks if not ok)


def test_cli_rejects_llm_mode_without_a_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    data_dir = tmp_path / "data"
    config_dir = Path(__file__).resolve().parents[1] / "config"
    common = ["--config-dir", str(config_dir), "--out-dir", str(tmp_path / "out")]
    main([*common, "generate", "--seed", "5", "--size", "60", "--data-dir", str(data_dir)])
    with pytest.raises(SystemExit, match="ANTHROPIC_API_KEY"):
        main([*common, "run", "--batch", str(data_dir / "batch_5.jsonl"), "--llm"])


def test_offline_run_needs_no_api_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The demo must not be able to fail live because a key is missing."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    data_dir = tmp_path / "data"
    config_dir = Path(__file__).resolve().parents[1] / "config"
    common = ["--config-dir", str(config_dir), "--out-dir", str(tmp_path / "out")]
    main([*common, "generate", "--seed", "5", "--size", "60", "--data-dir", str(data_dir)])
    assert main([*common, "run", "--batch", str(data_dir / "batch_5.jsonl"), "--no-llm"]) == 0


def test_repository_ships_no_secrets() -> None:
    repo = Path(__file__).resolve().parents[1]
    assert not (repo / ".env").exists(), ".env must never be committed"
    example = (repo / ".env.example").read_text()
    assert "ANTHROPIC_API_KEY" in example
    assert "sk-ant-REPLACE_ME" in example
    assert shutil.which  # keeps the import meaningful under lint
