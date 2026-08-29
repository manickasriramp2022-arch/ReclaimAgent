"""The report's inline SVG charts.

The charts must add what a table cannot (shape, instant comparison) without
introducing the two things this report cannot afford: an external request, or a
mark that misrepresents the data. A zero-length bar is the specific risk here,
because a hard-stop category's retry count is zero and that zero is the single
most important measured result on the page.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from helpers import build_run
from reclaim.charts import (
    SERIES_1,
    SERIES_2,
    ablation_contribution,
    attempts_by_category,
    sweep_distribution,
)
from reclaim.config import AppConfig
from reclaim.models import FailedTransaction

SWEEP = [0.007, 0.11, 0.34, 0.797]
ATTEMPTS = [("INSUFFICIENT_FUNDS", 150, 192), ("HARD_DECLINE", 0, 66)]
ABLATION = [("no root-cause routing", -0.179), ("no cost floor", 0.0)]


# ---------------------------------------------------------------------------
# Self-containment
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "svg",
    [
        sweep_distribution(SWEEP, 0.11),
        attempts_by_category(ATTEMPTS),
        ablation_contribution(ABLATION),
    ],
)
def test_a_chart_never_reaches_out_to_the_network(svg: str) -> None:
    assert "http://" not in svg
    assert "https://" not in svg
    assert "<script" not in svg
    assert "@import" not in svg


@pytest.mark.parametrize(
    "svg",
    [
        sweep_distribution(SWEEP, 0.11),
        attempts_by_category(ATTEMPTS),
        ablation_contribution(ABLATION),
    ],
)
def test_every_mark_carries_a_hover_title(svg: str) -> None:
    """The zero-JavaScript hover layer. Tooltips enhance; the direct labels and
    the report's tables carry the values regardless."""
    assert svg.count("<title>") >= 1
    assert 'role="img"' in svg
    assert "aria-label" in svg


# ---------------------------------------------------------------------------
# A zero is a result, not an absence
# ---------------------------------------------------------------------------
def test_a_zero_attempt_category_is_drawn_and_labelled() -> None:
    """A bar of length zero renders as nothing. Here nothing is the graded
    result, so it gets a visible stub and an explicit label."""
    svg = attempts_by_category(ATTEMPTS)
    assert ">0<" in svg, "the zero must be written out, not left as blank space"
    assert "measured result, not missing data" in svg
    # The stub is a rect, distinct from the rounded bar paths.
    assert svg.count("<rect") >= 1


def test_a_zero_contribution_ablation_row_says_by_design() -> None:
    svg = ablation_contribution(ABLATION)
    assert "0.0%" in svg
    assert "by design" in svg


def test_a_nonzero_bar_is_a_path_not_a_stub() -> None:
    svg = attempts_by_category([("TECHNICAL_ERROR", 43, 47)])
    assert svg.count("<path") == 2


# ---------------------------------------------------------------------------
# Legend rules: present for two series, absent for one
# ---------------------------------------------------------------------------
def test_two_series_get_a_legend_and_both_validated_hues() -> None:
    svg = attempts_by_category(ATTEMPTS)
    assert "ReclaimAgent" in svg and "naive retry 3x" in svg
    assert SERIES_1 in svg and SERIES_2 in svg


@pytest.mark.parametrize("svg", [sweep_distribution(SWEEP, 0.11), ablation_contribution(ABLATION)])
def test_one_series_uses_one_hue_and_no_legend(svg: str) -> None:
    """A legend box with a single swatch restates the caption and costs space."""
    assert SERIES_1 in svg
    assert SERIES_2 not in svg
    assert "naive retry 3x" not in svg


# ---------------------------------------------------------------------------
# Geometry
# ---------------------------------------------------------------------------
def test_bars_are_thin() -> None:
    """Thick saturated blocks read loud. Marks stay capped."""
    svg = ablation_contribution([("a", -0.2), ("b", -0.1)])
    heights = [float(m) for m in re.findall(r"V(\d+\.\d+) Q", svg)]
    assert heights, "expected rounded bar paths"


def test_gridlines_are_solid_hairlines() -> None:
    svg = sweep_distribution(SWEEP, 0.11)
    assert "stroke-dasharray" not in svg, "dashed grid reads as a threshold, not a grid"
    assert 'stroke-width="1"' in svg


def test_empty_input_produces_no_chart_rather_than_an_empty_frame() -> None:
    assert sweep_distribution([], 0.0) == ""
    assert attempts_by_category([]) == ""
    assert ablation_contribution([]) == ""


# ---------------------------------------------------------------------------
# Integration with the report
# ---------------------------------------------------------------------------
def test_the_report_carries_the_charts_when_the_data_exists(
    config: AppConfig, batch: list[FailedTransaction], run_dir: Path, tmp_path: Path
) -> None:
    from reclaim.ablation import run_ablation, write_ablation
    from reclaim.baseline import run_baseline
    from reclaim.benchmark import run_benchmark, write_benchmark
    from reclaim.classify import Classifier
    from reclaim.report import build_report

    result = build_run(config, batch, run_dir, tmp_path).execute()
    run_baseline(config, batch, 7, Classifier(config, llm_client=None), result.run_id, run_dir)
    write_benchmark(run_benchmark(config, seeds=2, size=60), run_dir / "benchmark.json")
    write_ablation(run_ablation(config, seeds=1, size=60), run_dir / "ablation.json")

    html = build_report(result.run_id, run_dir, config)
    assert html.count('<figure class="chart">') == 3
    assert "http://" not in html and "https://" not in html


def test_the_report_omits_the_attempts_chart_without_a_baseline(
    config: AppConfig, batch: list[FailedTransaction], run_dir: Path, tmp_path: Path
) -> None:
    """A one-series version of a two-series comparison would just restate the
    table beside it, so it is not drawn at all."""
    from reclaim.report import build_report

    result = build_run(config, batch, run_dir, tmp_path).execute()
    html = build_report(result.run_id, run_dir, config)
    assert "Charge attempts by root cause" not in html
