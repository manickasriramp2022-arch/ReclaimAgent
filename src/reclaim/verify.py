"""`reclaim verify-audit`: prove the report from the log.

Two things are checked. First, the audit log's own integrity: sequence
continuity, hash chain, ordering, and the safety invariants (no attempt ever
lands on a hard-stop or unclassified case). Second, and more important, that
every published number can be recomputed from the log alone. The run writes
out/metrics_<run_id>.json at the end of the run; this command throws that file
away, recomputes every field from the JSONL, and diffs. If a number in the
report cannot be re-derived from the audit trail, this fails.

This runs in CI on a fixture run, so a change that makes the report and the log
disagree cannot merge.
"""

from __future__ import annotations

from pathlib import Path

from .audit import VerificationResult, read_audit, verify_structure
from .escalation import read_run_escalations
from .metrics import build_comparison, compute_metrics, diff_metrics, read_metrics
from .models import Action, ComparisonReport


def metrics_path(run_id: str, out_dir: Path) -> Path:
    return out_dir / f"metrics_{run_id}.json"


def comparison_path(run_id: str, out_dir: Path) -> Path:
    return out_dir / f"comparison_{run_id}.json"


def verify_run(run_id: str, out_dir: Path = Path("out")) -> VerificationResult:
    treatment_log = out_dir / f"audit_{run_id}.jsonl"
    baseline_log = out_dir / f"audit_{run_id}-baseline.jsonl"

    result = verify_structure(treatment_log)

    if baseline_log.is_file():
        baseline_result = verify_structure(baseline_log)
        # The baseline is deliberately non-compliant, so its two safety
        # invariants are expected to fail. Every structural check must still
        # pass: an unusable baseline log would make the comparison worthless.
        structural = [
            (n, ok, d)
            for n, ok, d in baseline_result.checks
            if "hard-stop" not in n and "UNKNOWN" not in n
        ]
        result.check(
            "baseline log is structurally sound",
            all(ok for _, ok, _ in structural),
            "; ".join(f"{n}: {d}" for n, ok, d in structural if not ok),
        )
        safety_failures = sum(1 for _, ok, _ in baseline_result.checks if not ok)
        result.check(
            "baseline log demonstrates the failures the policy engine prevents",
            safety_failures > 0,
            ""
            if safety_failures
            else "baseline unexpectedly honoured hard stops; the comparison would be vacuous",
        )

    reported_path = metrics_path(run_id, out_dir)
    if not reported_path.is_file():
        result.check("reported metrics file exists", False, f"missing {reported_path}")
        return result

    events = read_audit(treatment_log)
    reported = read_metrics(reported_path)
    recomputed = compute_metrics(events, reported.strategy)
    problems = diff_metrics(reported, recomputed)
    result.check(
        "every reported metric recomputes from the log alone",
        not problems,
        "; ".join(problems[:4]),
    )

    if baseline_log.is_file():
        cmp_path = comparison_path(run_id, out_dir)
        if cmp_path.is_file():
            reported_cmp = ComparisonReport.model_validate_json(
                cmp_path.read_text(encoding="utf-8")
            )
            recomputed_cmp = build_comparison(events, read_audit(baseline_log))
            same = reported_cmp.model_dump(mode="json") == recomputed_cmp.model_dump(mode="json")
            result.check(
                "the baseline comparison recomputes from the two logs alone",
                same,
                "" if same else "reported comparison differs from the recomputed one",
            )

    mine = read_run_escalations(out_dir, run_id)
    logged = {e.case_id: e.value_paise for e in events if e.action is Action.ESCALATED}
    result.check(
        "escalation queue matches the ESCALATED events in the log",
        {e.case_id for e in mine} == set(logged),
        f"queue has {len(mine)} records, log has {len(logged)} ESCALATED events",
    )
    mismatched = [e.case_id for e in mine if logged.get(e.case_id) != e.amount_at_risk_paise]
    result.check(
        "escalated value at risk matches the log",
        not mismatched,
        "" if not mismatched else f"{len(mismatched)} mismatches, first {mismatched[0]}",
    )
    return result
