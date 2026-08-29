"""`reclaim verify-docs`: the README's numbers must trace to a real run.

Documentation rot is the quiet failure mode of a project whose entire claim is
that its figures are measured. One stale number gives a reviewer a reason to
distrust every other number, including the correct ones.
"""

from __future__ import annotations

import json
import os
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
    # One figure per line, each on a line that also carries its context anchor,
    # which is what the checker requires.
    body = "\n".join(
        f"{label}: {rendered} {context}".rstrip()
        for label, rendered, context in collect_figures(run_id, run_dir)
    )
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
    labels = {label for label, _, _ in collect_figures(prepared_run, run_dir)}
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
    before = {label for label, _, _ in collect_figures(prepared_run, run_dir)}
    assert not any("sweep" in label for label in before)

    write_benchmark(run_benchmark(config, seeds=2, size=60), run_dir / "benchmark.json")
    after = {label for label, _, _ in collect_figures(prepared_run, run_dir)}
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
    """out/ is git-ignored, so this only asserts when a real run's artefacts are
    on disk. CI generates them and sets RECLAIM_REQUIRE_ARTIFACTS so it cannot
    quietly skip there."""
    found = _repo_run()
    if found is None:
        pytest.skip("no run artefacts on disk; CI generates them explicitly")
    run_id, out, readme = found
    result = verify_docs(run_id, out, readme)
    assert result.ok, result.render()


# ---------------------------------------------------------------------------
# Mutation testing: is the gate actually strong, or does it just look strong?
# ---------------------------------------------------------------------------
REQUIRE_ARTIFACTS = "RECLAIM_REQUIRE_ARTIFACTS"


def _repo_run() -> tuple[str, Path, Path] | None:
    """Locate the repository's own run artefacts, or None.

    Tests that need these skip when they are absent, which is right for a fresh
    checkout and wrong for CI: the mutation test is the only evidence the docs
    gate is real, and a silent skip there would leave that claim unverified in
    the one place it matters. CI sets RECLAIM_REQUIRE_ARTIFACTS after generating
    a run, which turns the skip into a failure.
    """
    repo = Path(__file__).resolve().parents[1]
    out, readme = repo / "out", repo / "README.md"
    marker = out / "latest_run.txt"
    missing = ""
    if not marker.is_file():
        missing = f"{marker} does not exist"
    else:
        run_id = marker.read_text().strip()
        if not (out / f"metrics_{run_id}.json").is_file():
            missing = f"metrics for run {run_id} are not on disk"
        else:
            return run_id, out, readme
    if os.environ.get(REQUIRE_ARTIFACTS):
        raise AssertionError(
            f"{REQUIRE_ARTIFACTS} is set but {missing}. These checks must not skip here."
        )
    return None


def test_changing_any_documented_figure_fails_the_gate(tmp_path: Path) -> None:
    """The claim the README makes about this gate, tested by mutation.

    For every checked figure, rewrite it throughout the document and assert the
    check fails. A gate that cannot fail is not a gate.
    """
    found = _repo_run()
    if found is None:
        pytest.skip("no run artefacts on disk; CI generates them explicitly")
    run_id, out, readme = found
    original = readme.read_text(encoding="utf-8")

    figures = collect_figures(run_id, out)
    assert len(figures) > 20, "the gate should cover a substantial set of figures"

    survived: list[str] = []
    for label, rendered, _context in figures:
        mutated = tmp_path / "mutated.md"
        mutated.write_text(original.replace(rendered, "9" * len(rendered)), encoding="utf-8")
        if verify_docs(run_id, out, mutated).ok:
            survived.append(f"{label}={rendered!r}")
    assert not survived, f"the gate did not notice these figures changing: {survived}"


def test_every_check_is_anchored_enough_to_mean_something(tmp_path: Path) -> None:
    """A short figure like "0" matches any document containing a zero. Every
    check must therefore either be long enough to be distinctive or carry a
    same-segment context anchor."""
    found = _repo_run()
    if found is None:
        pytest.skip("no run artefacts on disk")
    run_id, out, readme = found
    result = verify_docs(run_id, out, readme)
    assert result.unanchored == [], (
        f"these checks could pass by coincidence: {[c.label for c in result.unanchored]}"
    )


def test_figures_that_cannot_be_verified_are_not_claimed(tmp_path: Path) -> None:
    """Categories that recovered nothing render as "0" inside a table row full
    of legitimate zeroes, so no substring check on them can fail. They are
    excluded rather than counted, because a check that cannot fail inflates the
    number without adding assurance. The hard-stop invariant covers them."""
    found = _repo_run()
    if found is None:
        pytest.skip("no run artefacts on disk")
    run_id, out, _readme = found
    labels = {label for label, _, _ in collect_figures(run_id, out)}
    for zero_category in ("HARD_DECLINE", "MANDATE_REVOKED", "UNKNOWN"):
        assert f"recovered, {zero_category}" not in labels


def test_segmentation_keeps_table_rows_apart_and_paragraphs_together() -> None:
    """Matching on raw lines breaks on wrapped prose; matching on the whole
    document makes every anchor meaningless. The unit is the row or paragraph."""
    from reclaim.docscheck import segment

    doc = (
        "A sentence that wraps across\ntwo source lines.\n\n"
        "| `HARD_DECLINE` | 12 | 0 |\n| `EXPIRED_CARD` | 20 | 14,565 |\n\n"
        "```\ncharge attempts : 385\ncost of acting  : 1,497.70\n```\n"
    )
    segments = segment(doc)
    assert "A sentence that wraps across two source lines." in segments
    assert any("HARD_DECLINE" in s and "EXPIRED_CARD" not in s for s in segments)
    assert any("charge attempts : 385" in s and "cost of acting" not in s for s in segments)
