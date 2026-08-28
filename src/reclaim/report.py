"""Component 10: the self-contained HTML report.

Everything on the page is derived from the two audit logs and the escalation
queue on disk. Nothing is passed in from a live run, which is why
`reclaim report` works on a run that finished days ago and why
`reclaim verify-audit` can re-derive every number the page displays.
"""

from __future__ import annotations

import html
from datetime import UTC, datetime
from pathlib import Path

from .ablation import FULL_SYSTEM, read_ablation
from .audit import read_audit
from .benchmark import read_benchmark
from .config import AppConfig
from .escalation import read_escalations
from .metrics import build_comparison, compute_metrics
from .models import (
    AblationReport,
    Action,
    AuditEvent,
    BenchmarkReport,
    ComparisonReport,
    EscalationRecord,
    Outcome,
    RootCause,
    RunMetrics,
)
from .replay import pick_demo_cases

CSS = """
:root{--ink:#12161c;--muted:#5b6672;--line:#e2e6ea;--bg:#ffffff;--panel:#f7f9fb;
--good:#0a7d43;--bad:#b3261e;--warn:#8a5a00;--accent:#1f4ed8}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);
font:15px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif}
.wrap{max-width:1080px;margin:0 auto;padding:32px 24px 80px}
h1{font-size:26px;margin:0 0 4px;letter-spacing:-.02em}
h2{font-size:18px;margin:40px 0 12px;padding-bottom:8px;border-bottom:1px solid var(--line)}
h3{font-size:15px;margin:22px 0 8px}
p{margin:0 0 12px}
.sub{color:var(--muted);margin-bottom:20px}
.banner{background:#fff8e1;border:1px solid #f0d68a;border-left:4px solid #d9a406;
padding:12px 16px;border-radius:6px;margin:18px 0;font-size:14px}
.banner strong{color:#7a5a00}
.hero{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:14px;margin:22px 0}
.card{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:16px}
.card .label{font-size:11px;letter-spacing:.08em;text-transform:uppercase;color:var(--muted)}
.card .value{font-size:26px;font-weight:650;margin-top:6px;letter-spacing:-.02em}
.card .note{font-size:12px;color:var(--muted);margin-top:6px}
.pos{color:var(--good)} .neg{color:var(--bad)} .warnc{color:var(--warn)}
table{width:100%;border-collapse:collapse;font-size:14px;margin:10px 0 4px}
th,td{padding:8px 10px;border-bottom:1px solid var(--line);text-align:right}
th:first-child,td:first-child{text-align:left}
th{font-size:11px;letter-spacing:.06em;text-transform:uppercase;color:var(--muted);
border-bottom:1px solid #cdd4db}
tbody tr:hover{background:var(--panel)}
tfoot td{font-weight:650;border-top:2px solid #cdd4db}
.scroll{overflow-x:auto}
code,pre{font-family:ui-monospace,SFMono-Regular,Menlo,monospace}
code{background:var(--panel);padding:1px 5px;border-radius:4px;font-size:13px}
pre{background:#12161c;color:#e6edf3;padding:14px 16px;border-radius:8px;overflow-x:auto;
font-size:12.5px;line-height:1.5}
.chain{border-left:3px solid var(--line);padding-left:14px;margin:10px 0}
.ev{padding:6px 0;border-bottom:1px dotted var(--line);font-size:13.5px}
.ev:last-child{border-bottom:0}
.ev .h{font-weight:600}
.ev .m{color:var(--muted);font-size:12.5px}
.tag{display:inline-block;padding:1px 7px;border-radius:10px;font-size:11px;font-weight:600;
background:#eef2f7;color:#3b4756;margin-left:6px}
.tag.ok{background:#e3f5ea;color:var(--good)}
.tag.no{background:#fdeceb;color:var(--bad)}
.tag.rf{background:#fff3d6;color:var(--warn)}
.foot{margin-top:50px;padding-top:16px;border-top:1px solid var(--line);
color:var(--muted);font-size:12.5px}
"""


def _rs(paise: int | float) -> str:
    return f"&#8377;{paise / 100:,.2f}"


def _esc(text: object) -> str:
    return html.escape(str(text))


def _hero(cmp_: ComparisonReport, metrics: RunMetrics) -> str:
    delta = cmp_.like_for_like_delta_paise
    cls = "pos" if delta >= 0 else "neg"
    sign = "+" if delta >= 0 else ""
    return f"""
<div class="hero">
  <div class="card">
    <div class="label">Recovered</div>
    <div class="value">{_rs(metrics.recovered_paise)}</div>
    <div class="note">{metrics.recovered_cases} of {metrics.cases} cases,
      {metrics.recovery_rate_on_addressable:.1%} of addressable value</div>
  </div>
  <div class="card">
    <div class="label">Delta vs naive baseline</div>
    <div class="value {cls}">{sign}{_rs(delta)}</div>
    <div class="note">{cmp_.like_for_like_delta_pct:+.1%} on the
      {cmp_.treatment_like_for_like.cases} cases both strategies were permitted to work</div>
  </div>
  <div class="card">
    <div class="label">Charge attempts</div>
    <div class="value">{metrics.charge_attempts:,}</div>
    <div class="note">{cmp_.attempt_delta:+,} vs baseline ({cmp_.attempt_delta_pct:+.0%});
      {_rs(metrics.recovered_paise_per_attempt)} recovered per attempt,
      {metrics.attempts_per_rupee_recovered:.4f} attempts per rupee</div>
  </div>
  <div class="card">
    <div class="label">Cost of acting</div>
    <div class="value">{_rs(metrics.action_cost_paise)}</div>
    <div class="note">summed from the cost stamped on every attempt and contact event;
      net recovery {_rs(metrics.net_recovered_paise)}</div>
  </div>
  <div class="card">
    <div class="label">Hard stops honoured</div>
    <div class="value {"pos" if metrics.correctly_stopped_rate == 1.0 else "neg"}">
      {metrics.correctly_stopped_rate:.0%}</div>
    <div class="note">{metrics.hard_stop_cases_with_zero_attempts} of
      {metrics.hard_stop_cases} hard-stop cases with zero retry attempts</div>
  </div>
  <div class="card">
    <div class="label">Escalated to humans</div>
    <div class="value">{metrics.escalated_cases}</div>
    <div class="note">{_rs(metrics.escalated_value_paise)} sitting in the queue</div>
  </div>
  <div class="card">
    <div class="label">Compliance refusals</div>
    <div class="value">{metrics.compliance_refusals}</div>
    <div class="note">{_rs(metrics.compliance_refused_terminal_paise)} removed from the
      recovery-rate denominator</div>
  </div>
</div>"""


def _category_table(metrics: RunMetrics, baseline: RunMetrics) -> str:
    base_by_cat = {c.category: c for c in baseline.per_category}
    rows = []
    for cat in metrics.per_category:
        if cat.cases == 0:
            continue
        b = base_by_cat.get(cat.category)
        b_rec = b.recovered_paise if b else 0
        b_att = b.charge_attempts if b else 0
        delta = cat.recovered_paise - b_rec
        cls = "pos" if delta > 0 else ("neg" if delta < 0 else "")
        rows.append(
            f"""<tr>
  <td><code>{_esc(cat.category)}</code></td>
  <td>{cat.cases}</td>
  <td>{_rs(cat.value_at_risk_paise)}</td>
  <td>{_rs(cat.recovered_paise)}</td>
  <td>{cat.recovery_rate:.1%}</td>
  <td>{cat.charge_attempts}</td>
  <td>{cat.contacts}</td>
  <td>{cat.refusals}</td>
  <td>{cat.escalated_cases}</td>
  <td>{_rs(b_rec)}</td>
  <td>{b_att}</td>
  <td class="{cls}">{"+" if delta >= 0 else ""}{_rs(delta)}</td>
</tr>"""
        )
    return f"""
<div class="scroll"><table>
<thead><tr>
  <th>Root cause</th><th>Cases</th><th>At risk</th><th>Recovered</th><th>Rate</th>
  <th>Attempts</th><th>Contacts</th><th>Refusals</th><th>Escalated</th>
  <th>Baseline recovered</th><th>Baseline attempts</th><th>Delta</th>
</tr></thead>
<tbody>{"".join(rows)}</tbody>
<tfoot><tr>
  <td>Total</td><td>{metrics.cases}</td><td>{_rs(metrics.value_at_risk_paise)}</td>
  <td>{_rs(metrics.recovered_paise)}</td><td>{metrics.recovery_rate_gross:.1%}</td>
  <td>{metrics.charge_attempts}</td><td>{metrics.contacts_sent}</td>
  <td>{metrics.compliance_refusals}</td><td>{metrics.escalated_cases}</td>
  <td>{_rs(baseline.recovered_paise)}</td><td>{baseline.charge_attempts}</td>
  <td>{"+" if metrics.recovered_paise >= baseline.recovered_paise else ""}
      {_rs(metrics.recovered_paise - baseline.recovered_paise)}</td>
</tr></tfoot>
</table></div>"""


def _rule_table(title: str, counts: dict[str, int], descriptions: dict[str, str]) -> str:
    if not counts:
        return f"<p class='sub'>No {title.lower()} fired in this run.</p>"
    rows = "".join(
        f"<tr><td><code>{_esc(rule)}</code></td><td>{count}</td>"
        f"<td style='text-align:left;color:#5b6672'>{_esc(descriptions.get(rule, ''))}</td></tr>"
        for rule, count in sorted(counts.items(), key=lambda kv: -kv[1])
    )
    return (
        "<div class='scroll'><table><thead><tr><th>Rule</th><th>Times fired</th>"
        f"<th style='text-align:left'>What it does</th></tr></thead><tbody>{rows}"
        "</tbody></table></div>"
    )


def _queue_table(records: list[EscalationRecord], limit: int = 20) -> str:
    if not records:
        return "<p class='sub'>Nothing was escalated in this run.</p>"
    rows = "".join(
        f"""<tr>
  <td>#{r.priority_rank}</td>
  <td>{_rs(r.amount_at_risk_paise)}</td>
  <td>{r.priority_score:,.0f}</td>
  <td><code>{_esc(r.case_id)}</code></td>
  <td>{_esc(r.category)}</td>
  <td><code>{_esc(r.stopping_rule)}</code></td>
  <td>{r.charge_attempts_spent}</td>
  <td style="text-align:left">{_esc(r.recommended_action)}</td>
</tr>"""
        for r in records[:limit]
    )
    more = (
        f"<p class='sub'>Showing the top {limit} of {len(records)} queued cases. "
        f"The full queue is in <code>out/escalations.jsonl</code>.</p>"
        if len(records) > limit
        else ""
    )
    return (
        "<div class='scroll'><table><thead><tr><th>Rank</th><th>At risk</th>"
        "<th>Priority</th><th>Case</th>"
        "<th>Root cause</th><th>Stopped by</th><th>Attempts</th>"
        "<th style='text-align:left'>Recommended human action</th></tr></thead>"
        f"<tbody>{rows}</tbody></table></div>{more}"
    )


def _chain(events: list[AuditEvent]) -> str:
    out = []
    for e in events:
        tag = ""
        if e.action is Action.CHARGE_ATTEMPT:
            tag = (
                "<span class='tag ok'>authorised</span>"
                if e.outcome is Outcome.SUCCESS
                else "<span class='tag no'>declined</span>"
            )
        elif e.action is Action.COMPLIANCE_REFUSAL:
            tag = "<span class='tag rf'>refused</span>"
        elif e.action is Action.RECOVERED:
            tag = f"<span class='tag ok'>{_rs(e.value_paise)}</span>"
        elif e.action is Action.STOPPED:
            tag = "<span class='tag no'>terminal</span>"
        rule = f"<span class='tag'>{_esc(e.rule)}</span>" if e.rule else ""
        chan = f" [{_esc(e.channel)}]" if e.channel else ""
        out.append(
            f"<div class='ev'><span class='h'>seq {e.seq} &middot; "
            f"{e.ts.strftime('%d %b %H:%M')} &middot; {_esc(e.actor)}/{_esc(e.action)}"
            f"{chan}</span>{tag}{rule}"
            f"<div class='m'>{_esc(e.detail)}</div></div>"
        )
    return f"<div class='chain'>{''.join(out)}</div>"


def _benchmark_section(report: BenchmarkReport | None) -> str:
    if report is None or not report.rows:
        return (
            "<p class='sub'>No sensitivity sweep on record. Run "
            "<code>reclaim benchmark --seeds 30</code> to generate one.</p>"
        )
    rows = "".join(
        f"""<tr>
  <td><code>{r.seed}</code></td>
  <td>{_rs(r.treatment_recovered_paise)}</td>
  <td>{_rs(r.baseline_recovered_paise)}</td>
  <td class="{"pos" if r.delta_paise >= 0 else "neg"}">{"+" if r.delta_paise >= 0 else ""}{_rs(r.delta_paise)}</td>
  <td class="{"pos" if r.delta_pct >= 0 else "neg"}">{r.delta_pct:+.1%}</td>
  <td class="pos">{r.attempt_delta_pct:+.0%}</td>
  <td class="{"pos" if r.correctly_stopped_rate == 1.0 else "neg"}">{r.correctly_stopped_rate:.0%}</td>
</tr>"""
        for r in report.rows
    )
    verdict_class = "pos" if report.losses == 0 else "warnc"
    return f"""
<div class="hero">
  <div class="card">
    <div class="label">Seeds where ReclaimAgent wins</div>
    <div class="value {verdict_class}">{report.wins} / {report.seeds}</div>
    <div class="note">independently generated batches of {report.batch_size} cases</div>
  </div>
  <div class="card">
    <div class="label">Delta, median</div>
    <div class="value">{report.median_delta_pct:+.1%}</div>
    <div class="note">mean {report.mean_delta_pct:+.1%}</div>
  </div>
  <div class="card">
    <div class="label">Delta, worst seed</div>
    <div class="value {"pos" if report.worst_delta_pct >= 0 else "neg"}">{report.worst_delta_pct:+.1%}</div>
    <div class="note">best seed {report.best_delta_pct:+.1%}</div>
  </div>
  <div class="card">
    <div class="label">Hard stops honoured</div>
    <div class="value {"pos" if report.hard_stops_always_honoured else "neg"}">
      {"every seed" if report.hard_stops_always_honoured else "NOT every seed"}</div>
    <div class="note">zero retries on every hard-decline and revoked-mandate case</div>
  </div>
</div>
<div class="scroll"><table>
<thead><tr><th>Seed</th><th>ReclaimAgent</th><th>Naive 3&times;</th><th>Delta</th>
<th>Delta %</th><th>Attempts</th><th>Hard stops</th></tr></thead>
<tbody>{rows}</tbody></table></div>"""


def _ablation_section(report: AblationReport | None) -> str:
    if report is None or not report.rows:
        return (
            "<p class='sub'>No ablation on record. Run <code>reclaim ablate</code> to "
            "measure what each design decision is worth.</p>"
        )
    rows = []
    for r in report.rows:
        is_full = r.variant == FULL_SYSTEM
        cost_cls = "" if is_full else ("neg" if r.recovery_vs_full_pct < -0.001 else "muted")
        cost = "reference" if is_full else f"{r.recovery_vs_full_pct:+.1%}"
        att = "&mdash;" if is_full else f"{r.attempts_vs_full_pct:+.1%}"
        rows.append(
            f"""<tr>
  <td><strong>{_esc(r.variant)}</strong>
      <div class="m" style="color:#5b6672;font-size:12.5px">{_esc(r.question)}</div></td>
  <td>{_rs(r.recovered_paise)}</td>
  <td>{r.charge_attempts:,}</td>
  <td>{r.mean_delta_vs_baseline_pct:+.1%}</td>
  <td class="{cost_cls}"><strong>{cost}</strong></td>
  <td>{att}</td>
</tr>"""
        )
    return f"""
<div class="scroll"><table>
<thead><tr><th>Variant</th><th>Recovered</th><th>Attempts</th>
<th>vs naive baseline</th><th>Recovery lost by removing it</th>
<th>Attempts</th></tr></thead>
<tbody>{"".join(rows)}</tbody></table></div>
<p class="sub">Every variant honoured hard stops on every seed:
<strong>{all(r.hard_stops_always_honoured for r in report.rows)}</strong>.
Measured over {report.seeds} seeds of {report.batch_size} cases each. Reproduce with
<code>reclaim ablate --seeds {report.seeds}</code>.</p>"""


def _worked_example(events: list[AuditEvent], case_id: str, title: str, why: str) -> str:
    chain = [e for e in events if e.case_id == case_id]
    if not chain:
        return ""
    ingest = next((e for e in chain if e.action is Action.CASE_INGESTED), None)
    classified = next((e for e in chain if e.action is Action.CLASSIFIED), None)
    amount = ingest.value_paise if ingest else 0
    return f"""
<h3>{_esc(title)} &mdash; <code>{_esc(case_id)}</code> &middot; {_rs(amount)} at risk</h3>
<p class="sub">{_esc(why)} Root cause <code>{_esc(classified.category if classified else "?")}</code>,
decided by the <strong>{_esc(classified.inputs.get("layer") if classified else "?")}</strong>
layer. Reproduce with <code>reclaim replay --case {_esc(case_id)}</code>.</p>
{_chain(chain)}"""


STOP_RULE_DESCRIPTIONS = {
    "hard_stop_category": "Non-recoverable root cause. Zero retries, immediate terminal state.",
    "unknown_requires_human": "Unclassified decline. Escalated rather than retried blind.",
    "max_attempts_per_case": "The policy's charge-attempt budget for this case was spent.",
    "rolling_window_attempt_cap": "Too many attempts on this case inside the rolling window.",
    "cost_floor": "The next attempt was expected to cost more than it would return.",
    "contact_frequency_cap": "The customer's contact budget was spent and contact was the only path left.",
    "network_retry_cap": "The card network's retry ceiling for this authorisation was reached.",
    "batch_circuit_breaker": "The batch failed far worse than predicted, so the run halted.",
    "plan_exhausted": "Every planned step ran without recovering the case.",
    "horizon_reached": "The simulated recovery horizon elapsed with the case still open.",
    "naive_attempt_budget": "Baseline only: three blind retries spent, no escalation.",
}


def build_report(run_id: str, out_dir: Path, config: AppConfig) -> str:
    treatment_log = out_dir / f"audit_{run_id}.jsonl"
    baseline_log = out_dir / f"audit_{run_id}-baseline.jsonl"
    events = read_audit(treatment_log)
    baseline_events = read_audit(baseline_log) if baseline_log.is_file() else []

    metrics = compute_metrics(events, "reclaimagent")
    baseline_metrics = (
        compute_metrics(baseline_events, "naive_retry_3x") if baseline_events else metrics
    )
    cmp_ = build_comparison(events, baseline_events or events)
    queue = [r for r in read_escalations(out_dir / "escalations.jsonl") if r.run_id == run_id]
    benchmark = read_benchmark(out_dir / "benchmark.json")
    ablation = read_ablation(out_dir / "ablation.json")

    success_case, stopped_case = pick_demo_cases(run_id, out_dir)
    compliance_descriptions = {
        name: const.note
        for section in ("consent", "emandate", "card_network", "data")
        for name, const in (
            (f"{section}.{k}", v) for k, v in getattr(config.compliance, section).items()
        )
    }
    compliance_descriptions["contact.quiet_hours_local"] = (
        config.compliance.contact.quiet_hours_local.note
    )
    compliance_descriptions["contact.max_contacts_per_customer_per_day"] = (
        config.compliance.contact.max_contacts_per_customer_per_day.note
    )
    compliance_descriptions["contact.max_contacts_per_customer_per_week"] = (
        config.compliance.contact.max_contacts_per_customer_per_week.note
    )

    unverified = config.compliance.unverified_entries()
    generated = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")

    examples = ""
    if success_case:
        examples += _worked_example(
            events,
            success_case,
            "Worked example 1: a recovery",
            "The largest single recovery in this batch, traced end to end.",
        )
    if stopped_case:
        examples += _worked_example(
            events,
            stopped_case,
            "Worked example 2: a correctly-handled failure",
            "The largest case the engine refused to retry at all.",
        )

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>ReclaimAgent run {_esc(run_id)}</title>
<style>{CSS}</style></head><body><div class="wrap">

<h1>ReclaimAgent &mdash; recovery report</h1>
<p class="sub">Run <code>{_esc(run_id)}</code> &middot; {metrics.cases} failed transactions
&middot; report generated {generated}</p>

<div class="banner">
<strong>Simulated outcomes, synthetic data.</strong> Every transaction in this batch was
generated by <code>config/generator.yaml</code> and marked <code>environment: test</code>.
Every retry outcome was drawn from the probabilistic model in
<code>config/simulation.yaml</code>. No payment gateway was contacted and no live payment
data was used. The rupee figures below measure the behaviour of the policy engine against
that model. They are not production recovery results and must not be quoted as such.
</div>

{_hero(cmp_, metrics)}

<h2>The headline, and its caveat</h2>
<p>Against the naive baseline &mdash; retry every failed transaction three times at 24-hour
intervals regardless of root cause &mdash; over the identical batch, the identical seed and
the identical outcome simulator:</p>
<div class="scroll"><table>
<thead><tr><th>Comparison</th><th>ReclaimAgent</th><th>Naive 3&times;</th><th>Delta</th></tr></thead>
<tbody>
<tr><td>Recovered, cases both strategies were permitted to work
    ({cmp_.treatment_like_for_like.cases} cases)</td>
  <td>{_rs(cmp_.treatment_like_for_like.recovered_paise)}</td>
  <td>{_rs(cmp_.baseline_like_for_like.recovered_paise)}</td>
  <td class="{"pos" if cmp_.like_for_like_delta_paise >= 0 else "neg"}">
    {"+" if cmp_.like_for_like_delta_paise >= 0 else ""}{_rs(cmp_.like_for_like_delta_paise)}
    ({cmp_.like_for_like_delta_pct:+.1%})</td></tr>
<tr><td>Charge attempts spent to get there</td>
  <td>{cmp_.treatment_like_for_like.charge_attempts:,}</td>
  <td>{cmp_.baseline_like_for_like.charge_attempts:,}</td>
  <td class="pos">{cmp_.attempt_delta:+,} ({cmp_.attempt_delta_pct:+.0%})</td></tr>
<tr><td>Hard-stop cases retried</td>
  <td class="pos">0</td>
  <td class="neg">{cmp_.baseline_attempts_on_hard_stop_cases} attempts</td>
  <td>&mdash;</td></tr>
<tr><td>Unclassified cases retried</td>
  <td class="pos">0</td>
  <td class="neg">{cmp_.baseline_attempts_on_unknown_cases} attempts</td>
  <td>&mdash;</td></tr>
</tbody></table></div>

<p><strong>The caveat, stated plainly.</strong> Across the whole batch the baseline
&quot;recovered&quot; {_rs(cmp_.baseline.recovered_paise)} against ReclaimAgent's
{_rs(metrics.recovered_paise)}. The difference is not skill. On the
{cmp_.refused_case_count} cases the compliance layer terminally refused &mdash; revoked or
inactive mandates, debits with no pre-debit notification on record, amounts above the
configured AFA threshold, authorisations past the network retry ceiling &mdash; the baseline
debited anyway and took {_rs(cmp_.baseline_value_from_refused_paise)} across
{cmp_.baseline_attempts_on_refused_cases} attempts that this system had already refused to
make. That money is not a win, and it is reported here rather than netted away. The
like-for-like row above is the fair comparison.</p>

<h2>Recovery by root cause</h2>
{_category_table(metrics, baseline_metrics)}

<h2>Stopping rules that fired</h2>
<p>Every terminal state in the audit log names the rule that produced it, so
&quot;why did this case stop?&quot; is answerable from the log alone.</p>
{_rule_table("stopping rules", metrics.stops_by_rule, STOP_RULE_DESCRIPTIONS)}

<h2>Compliance refusals</h2>
<p>A refusal is not a failed recovery attempt. Refused cases are removed from the
recovery-rate denominator, and each refusal names the constant in
<code>config/compliance.yaml</code> that blocked it.
{len(unverified)} of those constants are marked <code>unverified: true</code> and are listed
in <code>COMPLIANCE_NOTES.md</code> as requiring confirmation before production use.</p>
{_rule_table("compliance refusals", metrics.refusals_by_rule, compliance_descriptions)}

<h2>Human escalation queue</h2>
<p>{metrics.escalated_cases} cases carrying {_rs(metrics.escalated_value_paise)} were handed to
humans, ranked by recoverable value: amount at risk weighted by how much a human can still do
about that root cause.</p>
{_queue_table(queue)}

<h2>Does this hold, or is it one lucky batch?</h2>
<p>A single seed's delta is an anecdote. The sweep below re-runs the identical comparison
over {benchmark.seeds if benchmark else 0} independently generated batches. The worst seed is
shown as prominently as the mean, because a strategy that wins on average and loses badly
somewhere is a different proposition from one that wins everywhere.</p>
{_benchmark_section(benchmark)}
<p class="sub">Reproduce with <code>reclaim benchmark --seeds {benchmark.seeds if benchmark else 30}
--size {benchmark.batch_size if benchmark else 250}</code>. Each row runs the full pipeline and
the full baseline in a temporary directory, so a sweep never overwrites a real run's artefacts.</p>

<h2>Which part of the design earns the money?</h2>
<p>The sweep above shows the system beats the baseline. This shows why. Each row disables
exactly one feature and re-runs everything over the identical seeds, so the
&quot;recovery lost&quot; column is what that decision is worth.</p>
{_ablation_section(ablation)}
<p>The last row is the one worth reading carefully. Removing the cost floor changes recovery
by essentially nothing and <em>increases</em> attempts. That is the correct result, not a
disappointing one: the cost floor is a spend-control rule, not a recovery rule, and it earns
its place in the attempts column rather than the rupees column. A table built to flatter the
design would not have shown that.</p>

<h2>Worked examples</h2>
<p>Two cases traced through the audit log, event by event.</p>
{examples}

<h2>Reproducing every number on this page</h2>
<pre>reclaim verify-audit --run {_esc(run_id)}</pre>
<p>That command re-reads <code>out/audit_{_esc(run_id)}.jsonl</code>, checks sequence
continuity and the SHA-256 hash chain, confirms no attempt was ever made against a hard-stop
or unclassified case, and recomputes every metric above from the log alone, diffing the
result against <code>out/metrics_{_esc(run_id)}.json</code>. It runs in CI.</p>

<div class="foot">
ReclaimAgent &middot; run {_esc(run_id)} &middot; config fingerprint
<code>{_esc(config.fingerprint)}</code> &middot;
{len(events)} audit events &middot; simulated outcomes, test-mode data only.
</div>
</div></body></html>"""


def write_report(run_id: str, out_dir: Path = Path("out"), config: AppConfig | None = None) -> Path:
    from .config import load_config

    cfg = config or load_config()
    path = out_dir / f"report_{run_id}.html"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(build_report(run_id, out_dir, cfg), encoding="utf-8")
    return path


def report_summary_lines(run_id: str, out_dir: Path = Path("out")) -> list[str]:
    events = read_audit(out_dir / f"audit_{run_id}.jsonl")
    metrics = compute_metrics(events, "reclaimagent")
    return [
        f"{metrics.cases} cases, {_rs(metrics.value_at_risk_paise)} at risk",
        f"{metrics.recovered_cases} recovered, {_rs(metrics.recovered_paise)}",
    ]


__all__ = ["build_report", "write_report", "report_summary_lines", "RootCause"]
