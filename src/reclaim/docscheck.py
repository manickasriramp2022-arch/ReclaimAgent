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
    """One documented figure, and the anchor that makes finding it meaningful.

    A bare substring search is close to worthless for short figures: three of
    the per-category recovery values render as "0", which matches any document
    containing a zero. So a figure may carry a `context` string that must appear
    on the SAME LINE, which for a Markdown table means the same row and for
    prose means the same sentence. A check with a context is a real check; one
    without it is only used where the figure is long enough to be distinctive
    on its own.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    label: str
    rendered: str
    context: str = ""
    found: bool

    @property
    def anchored(self) -> bool:
        """A check is only meaningful if it cannot match a whole document by luck."""
        return bool(self.context) or len(self.rendered) > 6


class DocsCheckResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    document: str
    checks: list[FigureCheck]

    @property
    def ok(self) -> bool:
        return all(c.found for c in self.checks) and not self.unanchored

    @property
    def stale(self) -> list[FigureCheck]:
        return [c for c in self.checks if not c.found]

    @property
    def unanchored(self) -> list[FigureCheck]:
        """Checks weak enough to pass by coincidence. Should always be empty."""
        return [c for c in self.checks if not c.anchored]

    def render(self) -> str:
        lines = [f"documentation figures: {self.document} ({len(self.checks)} checked)"]
        for check in self.checks:
            mark = "OK   " if check.found else "STALE"
            where = f"  (on a line with {check.context!r})" if check.context else ""
            lines.append(f"  [{mark}] {check.label:<40} {check.rendered}{where}")
        if self.stale:
            lines.append("")
            lines.append(
                f"  => {len(self.stale)} figure(s) in {self.document} do not appear in the "
                "current run's artefacts. Re-run the pipeline and update the document."
            )
        elif self.unanchored:
            lines.append("")
            lines.append(
                f"  => {len(self.unanchored)} check(s) are short enough to match by "
                "coincidence and carry no context anchor. Give them one."
            )
        else:
            lines.append("  => OK, every figure traces to an actual run")
        return "\n".join(lines)


def _rupees(paise: int) -> str:
    return f"{paise / 100:,.0f}"


def _rupees_exact(paise: int) -> str:
    return f"{paise / 100:,.2f}"


def collect_figures(run_id: str, out_dir: Path) -> list[tuple[str, str, str]]:
    """Every headline figure the README quotes, as (label, rendered, context).

    `context` must appear on the same line as the figure. It exists because a
    bare substring search is meaningless for a figure like "0": every short
    value carries an anchor so the check cannot pass by coincidence.
    """
    metrics = read_metrics(out_dir / f"metrics_{run_id}.json")
    comparison = ComparisonReport.model_validate(
        json.loads((out_dir / f"comparison_{run_id}.json").read_text(encoding="utf-8"))
    )
    figures: list[tuple[str, str, str]] = [
        ("run id", run_id, ""),
        ("value at risk", _rupees(metrics.value_at_risk_paise), "value at risk"),
        ("recovered", _rupees(metrics.recovered_paise), "RECOVERED"),
        ("addressable value", _rupees(metrics.addressable_value_paise), "addressable"),
        (
            "compliance-refused value",
            _rupees(metrics.compliance_refused_terminal_paise),
            "compliance-refused",
        ),
        ("charge attempts", str(metrics.charge_attempts), "charge attempts"),
        ("cost of acting", _rupees_exact(metrics.action_cost_paise), "cost of acting"),
        ("net recovered", _rupees_exact(metrics.net_recovered_paise), "net"),
        (
            "recovered per attempt",
            _rupees_exact(metrics.recovered_paise_per_attempt),
            "recovered per attempt",
        ),
        ("escalated cases", str(metrics.escalated_cases), "escalated"),
        ("escalated value", _rupees(metrics.escalated_value_paise), "escalated"),
        ("compliance refusals", str(metrics.compliance_refusals), "compliance refusals"),
        (
            "hard stops honoured",
            f"{metrics.hard_stop_cases_with_zero_attempts}/{metrics.hard_stop_cases}",
            "hard stops honoured",
        ),
        (
            "like-for-like cases",
            str(comparison.treatment_like_for_like.cases),
            "Like-for-like on the",
        ),
        (
            "like-for-like treatment",
            _rupees(comparison.treatment_like_for_like.recovered_paise),
            "ReclaimAgent",
        ),
        (
            "like-for-like baseline",
            _rupees(comparison.baseline_like_for_like.recovered_paise),
            "naive 3x",
        ),
        ("like-for-like delta", _rupees(comparison.like_for_like_delta_paise), "DELTA"),
        ("baseline gross recovery", _rupees(comparison.baseline.recovered_paise), "baseline"),
        (
            "value taken from refused cases",
            _rupees(comparison.baseline_value_from_refused_paise),
            "debited anyway",
        ),
        (
            "baseline attempts on hard stops",
            str(comparison.baseline_attempts_on_hard_stop_cases),
            "hard-decline",
        ),
        (
            "baseline attempts on unknown",
            str(comparison.baseline_attempts_on_unknown_cases),
            "unclassified",
        ),
    ]

    for category in metrics.per_category:
        # Categories that recovered nothing are deliberately NOT checked. Their
        # value renders as "0", and a table row for a hard-stop category is full
        # of legitimate zeroes, so a substring search there would pass no matter
        # what the number became. Claiming it as a verified figure would inflate
        # the count with a check that cannot fail. That those categories recover
        # nothing is guaranteed by the hard-stop invariant instead, which is
        # asserted by verify-audit, by three tests, and by a CI gate across every
        # seed of the sweep and every variant of the ablation.
        if category.cases and category.recovered_paise:
            figures.append(
                (
                    f"recovered, {category.category}",
                    _rupees(category.recovered_paise),
                    str(category.category),
                )
            )

    from .benchmark import read_benchmark

    sweep = read_benchmark(out_dir / "benchmark.json")
    if sweep is not None:
        figures += [
            ("sweep win rate", f"{sweep.wins} / {sweep.seeds}", "recovers more"),
            ("sweep median delta", f"{sweep.median_delta_pct:+.1%}", "median"),
            ("sweep worst delta", f"{sweep.worst_delta_pct:+.1%}", "worst"),
            ("sweep best delta", f"{sweep.best_delta_pct:+.1%}", "best"),
        ]

    ablation = read_ablation(out_dir / "ablation.json")
    if ablation is not None:
        for row in ablation.rows:
            if row.variant == FULL_SYSTEM:
                figures.append(("ablation, full system", _rupees(row.recovered_paise), row.variant))
            else:
                figures.append(
                    (
                        f"ablation, {row.variant}",
                        f"{abs(row.recovery_vs_full_pct):.1%}",
                        row.variant,
                    )
                )
    return figures


def segment(markdown: str) -> list[str]:
    """Split a Markdown document into the units a figure and its anchor share.

    Matching on raw lines is wrong: prose wraps, so a figure and the words that
    give it meaning routinely land on different lines. Matching on the whole
    document is also wrong, because then any anchor matches anything.

    The right unit is the row or the paragraph:

    - a table row (a line starting with `|`) is its own segment, so a per-category
      figure is pinned to that category's row and nothing else;
    - a line inside a fenced code block is its own segment, because CLI output is
      line-oriented;
    - everything else accumulates into paragraphs separated by blank lines, so a
      wrapped sentence stays one unit and can be reflowed freely.
    """
    segments: list[str] = []
    paragraph: list[str] = []
    in_code = False

    def flush() -> None:
        if paragraph:
            segments.append(" ".join(paragraph))
            paragraph.clear()

    for line in markdown.splitlines():
        stripped = line.strip()
        if stripped.startswith("```"):
            flush()
            in_code = not in_code
            continue
        if in_code or stripped.startswith("|"):
            flush()
            segments.append(line)
            continue
        if not stripped:
            flush()
            continue
        paragraph.append(stripped)
    flush()
    return segments


def _present(segments: list[str], rendered: str, context: str) -> bool:
    if not context:
        return any(rendered in seg for seg in segments)
    return any(rendered in seg and context in seg for seg in segments)


def verify_docs(run_id: str, out_dir: Path, document: Path) -> DocsCheckResult:
    segments = segment(document.read_text(encoding="utf-8"))
    checks = [
        FigureCheck(
            label=label,
            rendered=rendered,
            context=context,
            found=_present(segments, rendered, context),
        )
        for label, rendered, context in collect_figures(run_id, out_dir)
    ]
    return DocsCheckResult(document=str(document), checks=checks)
