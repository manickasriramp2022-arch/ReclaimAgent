"""`reclaim verify-docs`: the README's numbers must come from a real run.

Documentation rot is the quiet failure mode of a project whose whole claim is
that its figures are measured. A reviewer who finds one stale number in the
README has a reason to distrust every other number in it, including the ones
that are correct.

So this walks the artefacts of an actual run, the seed sweep and the ablation,
formats each headline figure exactly as the README presents it, and checks the
document contains it. It runs in CI against a freshly generated run, which
means a change that moves a number and does not update the README fails the
build.

It deliberately checks presence rather than parsing the Markdown: the point is
that the figure a reader sees is a figure the pipeline produced, not that the
prose has a particular shape.
"""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from .ablation import FULL_SYSTEM, read_ablation
from .metrics import read_metrics
from .models import ComparisonReport


class FigureCheck(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    label: str
    rendered: str
    found: bool


class DocsCheckResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    document: str
    checks: list[FigureCheck]

    @property
    def ok(self) -> bool:
        return all(c.found for c in self.checks)

    @property
    def stale(self) -> list[FigureCheck]:
        return [c for c in self.checks if not c.found]

    def render(self) -> str:
        lines = [f"documentation figures: {self.document} ({len(self.checks)} checked)"]
        for check in self.checks:
            mark = "OK   " if check.found else "STALE"
            lines.append(f"  [{mark}] {check.label:<40} {check.rendered}")
        if self.stale:
            lines.append("")
            lines.append(
                f"  => {len(self.stale)} figure(s) in {self.document} do not appear in the "
                "current run's artefacts. Re-run the pipeline and update the document."
            )
        else:
            lines.append("  => OK, every figure traces to an actual run")
        return "\n".join(lines)


def _rupees(paise: int) -> str:
    return f"{paise / 100:,.0f}"


def _rupees_exact(paise: int) -> str:
    return f"{paise / 100:,.2f}"


def collect_figures(run_id: str, out_dir: Path) -> list[tuple[str, str]]:
    """Every headline figure the README quotes, formatted as the README shows it."""
    metrics = read_metrics(out_dir / f"metrics_{run_id}.json")
    comparison = ComparisonReport.model_validate(
        json.loads((out_dir / f"comparison_{run_id}.json").read_text(encoding="utf-8"))
    )
    figures: list[tuple[str, str]] = [
        ("run id", run_id),
        ("value at risk", _rupees(metrics.value_at_risk_paise)),
        ("recovered", _rupees(metrics.recovered_paise)),
        ("addressable value", _rupees(metrics.addressable_value_paise)),
        ("compliance-refused value", _rupees(metrics.compliance_refused_terminal_paise)),
        ("charge attempts", str(metrics.charge_attempts)),
        ("cost of acting", _rupees_exact(metrics.action_cost_paise)),
        ("net recovered", _rupees_exact(metrics.net_recovered_paise)),
        ("recovered per attempt", _rupees_exact(metrics.recovered_paise_per_attempt)),
        ("escalated cases", str(metrics.escalated_cases)),
        ("escalated value", _rupees(metrics.escalated_value_paise)),
        ("compliance refusals", str(metrics.compliance_refusals)),
        (
            "hard stops honoured",
            f"{metrics.hard_stop_cases_with_zero_attempts}/{metrics.hard_stop_cases}",
        ),
        ("like-for-like cases", str(comparison.treatment_like_for_like.cases)),
        ("like-for-like treatment", _rupees(comparison.treatment_like_for_like.recovered_paise)),
        ("like-for-like baseline", _rupees(comparison.baseline_like_for_like.recovered_paise)),
        ("like-for-like delta", _rupees(comparison.like_for_like_delta_paise)),
        ("baseline gross recovery", _rupees(comparison.baseline.recovered_paise)),
        ("value taken from refused cases", _rupees(comparison.baseline_value_from_refused_paise)),
        ("baseline attempts on hard stops", str(comparison.baseline_attempts_on_hard_stop_cases)),
        ("baseline attempts on unknown", str(comparison.baseline_attempts_on_unknown_cases)),
    ]

    for category in metrics.per_category:
        if category.cases:
            figures.append((f"recovered, {category.category}", _rupees(category.recovered_paise)))

    from .benchmark import read_benchmark

    sweep = read_benchmark(out_dir / "benchmark.json")
    if sweep is not None:
        figures += [
            ("sweep win rate", f"{sweep.wins} / {sweep.seeds}"),
            ("sweep median delta", f"{sweep.median_delta_pct:+.1%}"),
            ("sweep worst delta", f"{sweep.worst_delta_pct:+.1%}"),
            ("sweep best delta", f"{sweep.best_delta_pct:+.1%}"),
        ]

    ablation = read_ablation(out_dir / "ablation.json")
    if ablation is not None:
        for row in ablation.rows:
            if row.variant == FULL_SYSTEM:
                figures.append(("ablation, full system", _rupees(row.recovered_paise)))
            else:
                figures.append((f"ablation, {row.variant}", f"{abs(row.recovery_vs_full_pct):.1%}"))
    return figures


def verify_docs(run_id: str, out_dir: Path, document: Path) -> DocsCheckResult:
    text = document.read_text(encoding="utf-8")
    checks = [
        FigureCheck(label=label, rendered=rendered, found=rendered in text)
        for label, rendered in collect_figures(run_id, out_dir)
    ]
    return DocsCheckResult(document=str(document), checks=checks)
