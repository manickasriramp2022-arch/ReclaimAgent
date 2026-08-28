"""Component 6: the human escalation queue.

Exhausted, refused and UNKNOWN cases leave the automated pipeline here. Each
one arrives as a structured record carrying the money at risk, the category,
the full decision chain that led to the stop, the named rule that fired, a
recommended human action and a priority score, so the human opening the queue
never has to reconstruct what the agent was thinking.

Priority is value weighted by how much a human can realistically still do:
a large hopeless case should not outrank a mid-sized fixable one.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from .config import EscalationConfig
from .models import Action, AuditEvent, EscalationRecord, FailedTransaction, RootCause


def render_chain(events: list[AuditEvent]) -> list[str]:
    """Flatten a case's audit events into lines a human can read aloud."""
    lines: list[str] = []
    for event in events:
        stamp = event.ts.strftime("%Y-%m-%d %H:%M")
        bits = [f"[{stamp}] {event.actor}/{event.action} -> {event.outcome}"]
        if event.rule:
            bits.append(f"rule={event.rule}")
        if event.channel:
            bits.append(f"channel={event.channel}")
        if event.attempt_no is not None:
            bits.append(f"attempt={event.attempt_no}")
        head = " ".join(bits)
        lines.append(f"{head}: {event.detail}" if event.detail else head)
    return lines


class EscalationBuilder:
    def __init__(self, cfg: EscalationConfig) -> None:
        self.cfg = cfg

    def build(
        self,
        *,
        run_id: str,
        txn: FailedTransaction,
        category: RootCause,
        stopping_rule: str,
        reason: str,
        charge_attempts: int,
        contacts: int,
        events: list[AuditEvent],
        escalated_at: datetime,
    ) -> EscalationRecord:
        weight = self.cfg.weight(category)
        return EscalationRecord(
            run_id=run_id,
            case_id=txn.case_id,
            transaction_id=txn.transaction_id,
            customer_id=txn.customer_id,
            amount_at_risk_paise=txn.amount_paise,
            currency=txn.currency,
            category=category,
            decline_code=txn.decline_code,
            stopping_rule=stopping_rule,
            reason=reason,
            recommended_action=self.cfg.action_for(stopping_rule, txn.decline_code),
            priority_rank=0,
            priority_score=round(txn.amount_paise * weight / 100.0, 2),
            charge_attempts_spent=charge_attempts,
            contacts_sent=contacts,
            decision_chain=render_chain(events),
            escalated_at=escalated_at,
        )

    def rank(self, records: list[EscalationRecord]) -> list[EscalationRecord]:
        """Highest recoverable value first; ties broken by case id for determinism."""
        ordered = sorted(records, key=lambda r: (-r.priority_score, r.case_id))
        return [r.model_copy(update={"priority_rank": i}) for i, r in enumerate(ordered, start=1)]


def write_escalations(records: list[EscalationRecord], path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for rec in records:
            fh.write(json.dumps(rec.model_dump(mode="json"), sort_keys=True) + "\n")
    return path


def read_escalations(path: Path) -> list[EscalationRecord]:
    if not path.is_file():
        return []
    out: list[EscalationRecord] = []
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                out.append(EscalationRecord.model_validate(json.loads(line)))
    return out


def chain_from_events(events: list[AuditEvent], case_id: str) -> list[AuditEvent]:
    return [e for e in events if e.case_id == case_id]


def terminal_event(events: list[AuditEvent]) -> AuditEvent | None:
    for event in reversed(events):
        if event.action in {Action.RECOVERED, Action.STOPPED}:
            return event
    return None
