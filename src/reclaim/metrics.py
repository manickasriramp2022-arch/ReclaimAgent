"""Component 8: metrics, computed from the audit log and nothing else.

Every function here takes a list of `AuditEvent` read back off disk. None of
them can see the engine's in-memory state, which is the point: if a number
appears in the report, it was derived from the log, so a reviewer can re-derive
it themselves with `reclaim verify-audit`.

Denominator note: a compliance refusal is not a failed recovery. Cases the
compliance layer terminally refused are subtracted from the value at risk to
give `addressable_value_paise`, and the headline recovery rate is measured
against that. The gross rate is reported alongside it so nothing is hidden.
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from collections.abc import Callable
from pathlib import Path

from .audit import read_audit
from .models import (
    HARD_STOP_CATEGORIES,
    Action,
    AuditEvent,
    BaselineComparison,
    CategoryMetric,
    ComparisonReport,
    Outcome,
    RootCause,
    RunMetrics,
)

BATCH_SCOPE = "__batch__"


def _safe_div(num: float, den: float) -> float:
    return 0.0 if den == 0 else num / den


def compute_metrics(events: list[AuditEvent], strategy: str) -> RunMetrics:
    if not events:
        raise ValueError("cannot compute metrics from an empty audit log")
    run_id = events[0].run_id

    ingested: dict[str, int] = {}
    category_of: dict[str, RootCause] = {}
    recovered: dict[str, int] = {}
    attempts_by_case: Counter[str] = Counter()
    contacts_by_case: Counter[str] = Counter()
    refusals_by_case: Counter[str] = Counter()
    refusals_by_rule: Counter[str] = Counter()
    stops_by_rule: Counter[str] = Counter()
    stopped_cases: dict[str, str] = {}
    refused_terminal: dict[str, int] = {}
    escalated: dict[str, int] = {}
    breaker_tripped = False

    for event in events:
        if event.case_id == BATCH_SCOPE:
            if event.action is Action.CIRCUIT_BREAKER_TRIPPED:
                breaker_tripped = True
            continue
        if event.action is Action.CASE_INGESTED:
            ingested[event.case_id] = event.value_paise
        elif event.action is Action.CLASSIFIED and event.category is not None:
            category_of[event.case_id] = event.category
        elif event.action is Action.CHARGE_ATTEMPT:
            attempts_by_case[event.case_id] += 1
        elif event.action is Action.CONTACT_SENT:
            contacts_by_case[event.case_id] += 1
        elif event.action is Action.COMPLIANCE_REFUSAL:
            refusals_by_case[event.case_id] += 1
            refusals_by_rule[event.rule or "unspecified"] += 1
        elif event.action is Action.RECOVERED and event.outcome is Outcome.SUCCESS:
            recovered[event.case_id] = event.value_paise
        elif event.action is Action.STOPPED:
            stops_by_rule[event.rule or "unspecified"] += 1
            stopped_cases[event.case_id] = event.rule or "unspecified"
            if event.inputs.get("terminal_class") == "compliance_refusal":
                refused_terminal[event.case_id] = event.value_paise
        elif event.action is Action.ESCALATED:
            escalated[event.case_id] = event.value_paise

    value_at_risk = sum(ingested.values())
    refused_value = sum(refused_terminal.values())
    addressable = value_at_risk - refused_value
    recovered_value = sum(recovered.values())
    charge_attempts = sum(attempts_by_case.values())
    contacts = sum(contacts_by_case.values())

    hard_stop_cases = [c for c, cat in category_of.items() if cat in HARD_STOP_CATEGORIES]
    hard_stop_clean = [c for c in hard_stop_cases if attempts_by_case[c] == 0]

    per_category: list[CategoryMetric] = []
    grouped: dict[RootCause, list[str]] = defaultdict(list)
    for case_id in ingested:
        grouped[category_of.get(case_id, RootCause.UNKNOWN)].append(case_id)
    for category in RootCause:
        cases = grouped.get(category, [])
        per_category.append(
            CategoryMetric(
                category=category,
                cases=len(cases),
                value_at_risk_paise=sum(ingested[c] for c in cases),
                recovered_cases=sum(1 for c in cases if c in recovered),
                recovered_paise=sum(recovered.get(c, 0) for c in cases),
                charge_attempts=sum(attempts_by_case[c] for c in cases),
                contacts=sum(contacts_by_case[c] for c in cases),
                refusals=sum(refusals_by_case[c] for c in cases),
                escalated_cases=sum(1 for c in cases if c in escalated),
                stopped_cases=sum(1 for c in cases if c in stopped_cases),
            )
        )

    return RunMetrics(
        run_id=run_id,
        strategy=strategy,
        cases=len(ingested),
        value_at_risk_paise=value_at_risk,
        compliance_refused_terminal_paise=refused_value,
        addressable_value_paise=addressable,
        recovered_cases=len(recovered),
        recovered_paise=recovered_value,
        recovery_rate_on_addressable=round(_safe_div(recovered_value, addressable), 6),
        recovery_rate_gross=round(_safe_div(recovered_value, value_at_risk), 6),
        charge_attempts=charge_attempts,
        contacts_sent=contacts,
        attempts_per_rupee_recovered=round(_safe_div(charge_attempts, recovered_value / 100.0), 6),
        hard_stop_cases=len(hard_stop_cases),
        hard_stop_cases_with_zero_attempts=len(hard_stop_clean),
        correctly_stopped_rate=round(_safe_div(len(hard_stop_clean), len(hard_stop_cases)), 6)
        if hard_stop_cases
        else 1.0,
        escalated_cases=len(escalated),
        escalated_value_paise=sum(escalated.values()),
        compliance_refusals=sum(refusals_by_case.values()),
        refusals_by_rule=dict(sorted(refusals_by_rule.items())),
        stops_by_rule=dict(sorted(stops_by_rule.items())),
        per_category=per_category,
        circuit_breaker_tripped=breaker_tripped,
    )


def metrics_from_file(path: Path, strategy: str) -> RunMetrics:
    return compute_metrics(read_audit(path), strategy)


def write_metrics(metrics: RunMetrics, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(metrics.model_dump_json(indent=2) + "\n", encoding="utf-8")
    return path


def read_metrics(path: Path) -> RunMetrics:
    return RunMetrics.model_validate(json.loads(path.read_text(encoding="utf-8")))


def compare(treatment: RunMetrics, baseline: RunMetrics) -> BaselineComparison:
    return BaselineComparison(treatment=treatment, baseline=baseline)


def terminally_refused_cases(events: list[AuditEvent]) -> set[str]:
    """Cases the compliance layer refused outright, read back off the log."""
    return {
        e.case_id
        for e in events
        if e.action is Action.STOPPED and e.inputs.get("terminal_class") == "compliance_refusal"
    }


def _restrict(events: list[AuditEvent], exclude: set[str]) -> list[AuditEvent]:
    return [e for e in events if e.case_id not in exclude]


def build_comparison(
    treatment_events: list[AuditEvent],
    baseline_events: list[AuditEvent],
    treatment_name: str = "reclaimagent",
    baseline_name: str = "naive_retry_3x",
) -> ComparisonReport:
    """Everything the report needs to state a delta, derived from two logs."""
    refused = terminally_refused_cases(treatment_events)

    category_of = {e.case_id: e.category for e in baseline_events if e.action is Action.CLASSIFIED}

    def attempts_on(predicate: Callable[[str], bool]) -> int:
        return sum(
            1 for e in baseline_events if e.action is Action.CHARGE_ATTEMPT and predicate(e.case_id)
        )

    baseline_on_refused = [e for e in baseline_events if e.case_id in refused]
    recovered_from_refused = sum(
        e.value_paise
        for e in baseline_on_refused
        if e.action is Action.RECOVERED and e.outcome is Outcome.SUCCESS
    )

    return ComparisonReport(
        treatment=compute_metrics(treatment_events, treatment_name),
        baseline=compute_metrics(baseline_events, baseline_name),
        treatment_like_for_like=compute_metrics(
            _restrict(treatment_events, refused), f"{treatment_name}/addressable"
        ),
        baseline_like_for_like=compute_metrics(
            _restrict(baseline_events, refused), f"{baseline_name}/addressable"
        ),
        refused_case_count=len(refused),
        baseline_value_from_refused_paise=recovered_from_refused,
        baseline_attempts_on_refused_cases=attempts_on(lambda c: c in refused),
        baseline_attempts_on_hard_stop_cases=attempts_on(
            lambda c: category_of.get(c) in HARD_STOP_CATEGORIES
        ),
        baseline_attempts_on_unknown_cases=attempts_on(
            lambda c: category_of.get(c) is RootCause.UNKNOWN
        ),
    )


def diff_metrics(reported: RunMetrics, recomputed: RunMetrics) -> list[str]:
    """Field-by-field comparison used by `verify-audit`.

    Reports every headline field that does not match what the log alone
    produces. An empty list means every published number is reproducible.
    """
    problems: list[str] = []
    a = reported.model_dump(mode="json")
    b = recomputed.model_dump(mode="json")
    for key in sorted(set(a) | set(b)):
        if a.get(key) != b.get(key):
            problems.append(f"{key}: reported={a.get(key)!r} recomputed={b.get(key)!r}")
    return problems


def headline(metrics: RunMetrics, baseline: RunMetrics | None = None) -> list[str]:
    """The numbers a reviewer should hear first, as printable lines."""
    lines = [
        f"value at risk        : Rs {metrics.value_at_risk_paise / 100:>14,.2f}  ({metrics.cases} cases)",
        f"compliance-refused   : Rs {metrics.compliance_refused_terminal_paise / 100:>14,.2f}  (excluded from the denominator)",
        f"addressable value    : Rs {metrics.addressable_value_paise / 100:>14,.2f}",
        f"RECOVERED            : Rs {metrics.recovered_paise / 100:>14,.2f}  ({metrics.recovery_rate_on_addressable:.2%} of addressable, {metrics.recovered_cases} cases)",
        f"charge attempts      : {metrics.charge_attempts:>17,}  ({metrics.attempts_per_rupee_recovered:.4f} attempts per rupee recovered)",
        f"hard stops honoured  : {metrics.hard_stop_cases_with_zero_attempts}/{metrics.hard_stop_cases} with zero retries ({metrics.correctly_stopped_rate:.0%})",
        f"escalated            : {metrics.escalated_cases} cases, Rs {metrics.escalated_value_paise / 100:,.2f} at risk",
        f"compliance refusals  : {metrics.compliance_refusals}",
    ]
    if baseline is not None:
        delta = metrics.recovered_paise - baseline.recovered_paise
        att = metrics.charge_attempts - baseline.charge_attempts
        lines += [
            "",
            f"baseline ({baseline.strategy}): Rs {baseline.recovered_paise / 100:,.2f} recovered on {baseline.charge_attempts:,} attempts",
            f"DELTA                : Rs {delta / 100:+,.2f} recovered, {att:+,} charge attempts",
        ]
    return lines


def comparison_headline(report: ComparisonReport) -> list[str]:
    """The delta a reviewer should hear first, with the caveat attached."""
    t, b = report.treatment_like_for_like, report.baseline_like_for_like
    return [
        f"Like-for-like on the {t.cases} cases both strategies were permitted to work:",
        f"  ReclaimAgent : Rs {t.recovered_paise / 100:>12,.2f} recovered on {t.charge_attempts:>4,} charge attempts",
        f"  naive 3x     : Rs {b.recovered_paise / 100:>12,.2f} recovered on {b.charge_attempts:>4,} charge attempts",
        f"  DELTA        : Rs {report.like_for_like_delta_paise / 100:>+12,.2f} "
        f"({report.like_for_like_delta_pct:+.1%}) on {report.attempt_delta:+,} attempts "
        f"({report.attempt_delta_pct:+.1%})",
        "",
        f"On the {report.refused_case_count} cases ReclaimAgent terminally refused, the baseline "
        f"debited anyway: Rs {report.baseline_value_from_refused_paise / 100:,.2f} taken across "
        f"{report.baseline_attempts_on_refused_cases} attempts the compliance layer had refused.",
        f"The baseline also spent {report.baseline_attempts_on_hard_stop_cases} attempts on "
        f"hard-decline and revoked-mandate cases and "
        f"{report.baseline_attempts_on_unknown_cases} on unclassified ones, recovering nothing.",
    ]
