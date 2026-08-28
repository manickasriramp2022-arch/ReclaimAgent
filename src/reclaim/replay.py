"""`reclaim replay --case <case_id>`: the whole decision chain for one case.

This is the demo command. Its output is meant to be read aloud, so it is
plain prose with the numbers lined up, not a JSON dump. Everything it prints
comes from the audit log; nothing is recomputed from live state.
"""

from __future__ import annotations

from pathlib import Path

from .audit import read_audit
from .models import Action, AuditEvent, Outcome

_ACTION_PROSE: dict[Action, str] = {
    Action.CASE_INGESTED: "Ingested",
    Action.CLASSIFIED: "Classified",
    Action.POLICY_SELECTED: "Policy selected",
    Action.STEP_SCHEDULED: "Scheduled",
    Action.STEP_DEFERRED: "Deferred",
    Action.COMPLIANCE_REFUSAL: "Compliance refused",
    Action.CHARGE_ATTEMPT: "Charge attempt",
    Action.CONTACT_SENT: "Contacted customer",
    Action.CONTACT_RESPONSE: "Customer responded",
    Action.RECOVERED: "RECOVERED",
    Action.STOPPED: "STOPPED",
    Action.ESCALATED: "Escalated to human",
    Action.CIRCUIT_BREAKER_TRIPPED: "Circuit breaker tripped",
}


def case_events(run_id: str, case_id: str, out_dir: Path = Path("out")) -> list[AuditEvent]:
    path = out_dir / f"audit_{run_id}.jsonl"
    return [e for e in read_audit(path) if e.case_id == case_id]


def render_replay(events: list[AuditEvent], case_id: str) -> str:
    if not events:
        return f"No audit events found for case {case_id}."

    ingest = next((e for e in events if e.action is Action.CASE_INGESTED), None)
    classified = next((e for e in events if e.action is Action.CLASSIFIED), None)
    terminal = next(
        (e for e in reversed(events) if e.action in {Action.RECOVERED, Action.STOPPED}), None
    )
    attempts = [e for e in events if e.action is Action.CHARGE_ATTEMPT]
    contacts = [e for e in events if e.action is Action.CONTACT_SENT]
    refusals = [e for e in events if e.action is Action.COMPLIANCE_REFUSAL]
    amount = ingest.value_paise if ingest else 0

    out: list[str] = []
    out.append("=" * 78)
    out.append(f"CASE {case_id}   run {events[0].run_id}")
    out.append("=" * 78)
    if ingest:
        out.append(
            f"  Rs {amount / 100:,.2f} at risk. "
            f"{ingest.inputs.get('payment_method')} payment declined with "
            f"{ingest.inputs.get('decline_code')}."
        )
        out.append(f"  {ingest.detail}")
        out.append(
            f"  Prior attempts before this batch: {ingest.inputs.get('prior_attempt_count')}. "
            f"Contact consent: {ingest.inputs.get('contact_consent')}. "
            f"Environment: {ingest.inputs.get('environment')}."
        )
    if classified:
        layer = classified.inputs.get("layer")
        out.append("")
        out.append(
            f"  ROOT CAUSE: {classified.category}  "
            f"(decided by the {layer} layer, confidence {classified.inputs.get('confidence')})"
        )
        if classified.rule:
            out.append(f"  Deciding rule: {classified.rule}")
        out.append(f"  Reasoning: {classified.detail}")

    out.append("")
    out.append(f"DECISION CHAIN ({len(events)} audit events, seq {events[0].seq}-{events[-1].seq})")
    out.append("-" * 78)
    for event in events:
        label = _ACTION_PROSE.get(event.action, str(event.action))
        stamp = event.ts.strftime("%d %b %H:%M")
        head = f"  seq {event.seq:>5}  {stamp}  {label}"
        if event.channel:
            head += f" [{event.channel}]"
        if event.attempt_no is not None:
            head += f" #{event.attempt_no}"
        if event.action is Action.CHARGE_ATTEMPT:
            head += f" -> {'AUTHORISED' if event.outcome is Outcome.SUCCESS else 'declined'}"
        out.append(head)
        if event.rule:
            out.append(f"{'':>16}rule fired: {event.rule}")
        if event.detail:
            out.append(f"{'':>16}{event.detail}")
        if event.action is Action.RECOVERED:
            out.append(f"{'':>16}value recovered: Rs {event.value_paise / 100:,.2f}")

    out.append("-" * 78)
    out.append("OUTCOME")
    if terminal and terminal.action is Action.RECOVERED:
        out.append(
            f"  Recovered Rs {terminal.value_paise / 100:,.2f} on charge attempt "
            f"{terminal.attempt_no} after {len(contacts)} customer contact(s)."
        )
    elif terminal:
        out.append(f"  Stopped by rule: {terminal.rule}")
        out.append(f"  Because: {terminal.detail}")
        out.append(
            f"  Charge attempts spent: {len(attempts)}. Contacts sent: {len(contacts)}. "
            f"Compliance refusals: {len(refusals)}."
        )
        out.append(f"  Value still at risk: Rs {amount / 100:,.2f}")
    escalated = next((e for e in events if e.action is Action.ESCALATED), None)
    if escalated:
        out.append("")
        out.append("  Handed to the human queue.")
        out.append(f"  Recommended action: {escalated.inputs.get('recommended_action')}")
    out.append("=" * 78)
    return "\n".join(out)


def pick_demo_cases(run_id: str, out_dir: Path = Path("out")) -> tuple[str | None, str | None]:
    """Find the highest-value successful recovery and the highest-value
    correctly-handled hard stop, for the demo and the report."""
    events = read_audit(out_dir / f"audit_{run_id}.jsonl")
    best_success: tuple[int, str] | None = None
    best_stop: tuple[int, str] | None = None
    for event in events:
        if event.action is Action.RECOVERED and event.outcome is Outcome.SUCCESS:
            cand = (event.value_paise, event.case_id)
            if best_success is None or cand > best_success:
                best_success = cand
        elif event.action is Action.STOPPED and event.rule in {
            "hard_stop_category",
            "unknown_requires_human",
        }:
            cand = (event.value_paise, event.case_id)
            if best_stop is None or cand > best_stop:
                best_stop = cand
    return (
        best_success[1] if best_success else None,
        best_stop[1] if best_stop else None,
    )
