"""`reclaim verify-docs`: the README's numbers must trace to a real run.

Documentation rot is the quiet failure mode of a project whose entire claim is
that its figures are measured. One stale number gives a reviewer a reason to
distrust every other number, including the correct ones.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from helpers import build_run
from reclaim.baseline import run_baseline
from reclaim.benchmark import run_benchmark, write_benchmark
from reclaim.classify import Classifier
from reclaim.config import AppConfig
from reclaim.docscheck import collect_figures, verify_docs
from reclaim.metrics import build_comparison, compute_metrics, write_metrics
from reclaim.models import FailedTransaction
from reclaim.verify import comparison_path, metrics_path


@pytest.fixture
def prepared_run(
    config: AppConfig, batch: list[FailedTransaction], run_dir: Path, tmp_path: Path
) -> str:
    result = build_run(config, batch, run_dir, tmp_path).execute()
    _, _, baseline_events = run_baseline(
        config, batch, 7, Classifier(config, llm_client=None), result.run_id, run_dir
    )
    metrics = compute_metrics(result.events, "reclaimagent")
    write_metrics(metrics, metrics_path(result.run_id, run_dir))
    comparison = build_comparison(result.events, baseline_events)
    comparison_path(result.run_id, run_dir).write_text(
        comparison.model_dump_json(indent=2) + "\n", encoding="utf-8"
    )
    return result.run_id


def _document_containing_everything(run_id: str, run_dir: Path, tmp_path: Path) -> Path:
    body = "\n".join(f"{label}: {rendered}" for label, rendered in collect_figures(run_id, run_dir))
    path = tmp_path / "perfect.md"
    path.write_text(body, encoding="utf-8")
    return path


def test_a_document_quoting_the_run_passes(
    prepared_run: str, run_dir: Path, tmp_path: Path
) -> None:
    doc = _document_containing_everything(prepared_run, run_dir, tmp_path)
    result = verify_docs(prepared_run, run_dir, doc)
    assert result.ok, result.render()
    assert result.stale == []


def test_a_single_stale_figure_fails(prepared_run: str, run_dir: Path, tmp_path: Path) -> None:
    doc = _document_containing_everything(prepared_run, run_dir, tmp_path)
    metrics = json.loads(metrics_path(prepared_run, run_dir).read_text())
    real = f"{metrics['recovered_paise'] / 100:,.0f}"
    doc.write_text(doc.read_text().replace(real, "999,999,999"), encoding="utf-8")

    result = verify_docs(prepared_run, run_dir, doc)
    assert not result.ok
    assert any("recovered" in c.label for c in result.stale)
    assert "STALE" in result.render()


def test_an_empty_document_fails_every_check(
    prepared_run: str, run_dir: Path, tmp_path: Path
) -> None:
    empty = tmp_path / "empty.md"
    empty.write_text("nothing here", encoding="utf-8")
    result = verify_docs(prepared_run, run_dir, empty)
    assert not result.ok
    assert len(result.stale) == len(result.checks)


def test_the_headline_figures_are_all_covered(prepared_run: str, run_dir: Path) -> None:
    """If a headline number is not in this list, the README can drift on it
    silently, which defeats the purpose."""
    labels = {label for label, _ in collect_figures(prepared_run, run_dir)}
    for required in (
        "recovered",
        "value at risk",
        "addressable value",
        "compliance-refused value",
        "charge attempts",
        "cost of acting",
        "net recovered",
        "hard stops honoured",
        "like-for-like delta",
        "baseline gross recovery",
        "escalated value",
        "compliance refusals",
    ):
        assert required in labels, f"{required} is not checked against the docs"


def test_sweep_and_ablation_figures_join_the_check_when_present(
    prepared_run: str, run_dir: Path, config: AppConfig
) -> None:
    before = {label for label, _ in collect_figures(prepared_run, run_dir)}
    assert not any("sweep" in label for label in before)

    write_benchmark(run_benchmark(config, seeds=2, size=60), run_dir / "benchmark.json")
    after = {label for label, _ in collect_figures(prepared_run, run_dir)}
    assert "sweep win rate" in after
    assert "sweep worst delta" in after


def test_check_is_tolerant_of_missing_sweep_and_ablation(
    prepared_run: str, run_dir: Path, tmp_path: Path
) -> None:
    """A plain run with no sweep on disk must still be checkable."""
    assert not (run_dir / "benchmark.json").exists()
    assert not (run_dir / "ablation.json").exists()
    doc = _document_containing_everything(prepared_run, run_dir, tmp_path)
    assert verify_docs(prepared_run, run_dir, doc).ok


def test_the_repository_readme_matches_its_own_artifacts_if_present() -> None:
    """Not a hard requirement in a fresh checkout: out/ is git-ignored, so this
    only asserts when a real run's artefacts are actually on disk. CI generates
    them explicitly and runs the same check as a gate."""
    repo = Path(__file__).resolve().parents[1]
    out = repo / "out"
    marker = out / "latest_run.txt"
    if not marker.is_file():
        pytest.skip("no run artefacts on disk; CI generates them explicitly")
    run_id = marker.read_text().strip()
    if not (out / f"metrics_{run_id}.json").is_file():
        pytest.skip("run artefacts incomplete")
    result = verify_docs(run_id, out, repo / "README.md")
    assert result.ok, result.render()
