"""Component 7: the append-only audit trail.

Every decision the system makes is one line of JSONL in out/audit_<run_id>.jsonl.
Events are never mutated and never deleted. Each event carries a monotonic
sequence number and a SHA-256 hash over its own body plus its predecessor's
hash, so removing or editing an event breaks the chain and `verify-audit`
fails on the file alone, without needing any other state.

The audit log deliberately contains no wall-clock time and no random ids. Its
timestamps are the run's simulated clock. That is what makes a run with the
same seed byte-identical, which in turn is what makes the determinism test
meaningful. Wall-clock metadata lives in the run manifest instead.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from typing import Any, TextIO

from .models import Action, Actor, AuditEvent, Channel, Outcome, RootCause

GENESIS_HASH = "0" * 64


class AuditLog:
    """Append-only writer. Open it, append events, close it. Nothing else."""

    def __init__(self, run_id: str, path: Path) -> None:
        self.run_id = run_id
        self.path = path
        self._seq = 0
        self._prev_hash = GENESIS_HASH
        self._fh: TextIO | None = None
        self._events: list[AuditEvent] = []

    def __enter__(self) -> AuditLog:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # "w" is correct here: a run owns its own file and creates it once.
        # Within the run's lifetime the handle is append-only; there is no
        # code path that seeks, truncates or rewrites a line.
        self._fh = self.path.open("w", encoding="utf-8")
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        if self._fh is not None:
            self._fh.close()
            self._fh = None

    @property
    def events(self) -> list[AuditEvent]:
        return list(self._events)

    def append(
        self,
        *,
        case_id: str,
        actor: Actor,
        action: Action,
        outcome: Outcome,
        ts: datetime,
        value_paise: int = 0,
        category: RootCause | None = None,
        rule: str | None = None,
        channel: Channel | None = None,
        attempt_no: int | None = None,
        inputs: dict[str, Any] | None = None,
        detail: str = "",
    ) -> AuditEvent:
        if self._fh is None:
            raise RuntimeError("audit log is not open; use it as a context manager")
        self._seq += 1
        event = AuditEvent(
            run_id=self.run_id,
            seq=self._seq,
            ts=ts,
            case_id=case_id,
            actor=actor,
            action=action,
            outcome=outcome,
            value_paise=value_paise,
            category=category,
            rule=rule,
            channel=channel,
            attempt_no=attempt_no,
            inputs=inputs or {},
            detail=detail,
            prev_hash=self._prev_hash,
        ).with_hash()
        self._prev_hash = event.event_hash
        self._events.append(event)
        self._fh.write(json.dumps(event.model_dump(mode="json"), sort_keys=True) + "\n")
        return event


def audit_path(run_id: str, out_dir: Path = Path("out")) -> Path:
    return out_dir / f"audit_{run_id}.jsonl"


def read_audit(path: Path) -> list[AuditEvent]:
    return list(iter_audit(path))


def iter_audit(path: Path) -> Iterator[AuditEvent]:
    if not path.is_file():
        raise FileNotFoundError(f"no audit log at {path}")
    with path.open(encoding="utf-8") as fh:
        for line_no, line in enumerate(fh, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                yield AuditEvent.model_validate(json.loads(line))
            except Exception as exc:  # noqa: BLE001 - line number matters more than type
                raise ValueError(f"{path}:{line_no}: malformed audit event: {exc}") from exc


class VerificationResult:
    """Result of verifying one audit log."""

    def __init__(self, path: Path, run_id: str, events: int) -> None:
        self.path = path
        self.run_id = run_id
        self.events = events
        self.checks: list[tuple[str, bool, str]] = []

    def check(self, name: str, passed: bool, detail: str = "") -> None:
        self.checks.append((name, passed, detail))

    @property
    def ok(self) -> bool:
        return all(passed for _, passed, _ in self.checks)

    @property
    def failures(self) -> list[tuple[str, bool, str]]:
        return [c for c in self.checks if not c[1]]

    def render(self) -> str:
        lines = [f"audit verification: {self.path} ({self.events} events)"]
        for name, passed, detail in self.checks:
            mark = "PASS" if passed else "FAIL"
            lines.append(f"  [{mark}] {name}" + (f" - {detail}" if detail else ""))
        lines.append(f"  => {'OK' if self.ok else 'FAILED'}")
        return "\n".join(lines)


def verify_structure(path: Path) -> VerificationResult:
    """Structural integrity: sequence continuity, hash chain, ordering, shape.

    Metric recomputation is checked separately in `reclaim.metrics` so this
    function stays dependency-free and usable on any audit log.
    """
    events = read_audit(path)
    run_id = events[0].run_id if events else ""
    result = VerificationResult(path, run_id, len(events))

    result.check("log is non-empty", bool(events))
    if not events:
        return result

    result.check(
        "single run_id throughout",
        all(e.run_id == run_id for e in events),
        f"run_id={run_id}",
    )

    expected = list(range(1, len(events) + 1))
    actual = [e.seq for e in events]
    gaps = [(x, y) for x, y in zip(expected, actual, strict=True) if x != y]
    result.check(
        "sequence numbers are 1..N with no gaps or duplicates",
        not gaps,
        "" if not gaps else f"first divergence expected {gaps[0][0]} got {gaps[0][1]}",
    )

    prev = GENESIS_HASH
    chain_break: str = ""
    for event in events:
        if event.prev_hash != prev:
            chain_break = f"seq {event.seq}: prev_hash does not match seq {event.seq - 1}"
            break
        recomputed = event.model_copy(update={"event_hash": ""}).with_hash().event_hash
        if recomputed != event.event_hash:
            chain_break = f"seq {event.seq}: body does not match its own hash (event was edited)"
            break
        prev = event.event_hash
    result.check("hash chain is intact (append-only, untampered)", not chain_break, chain_break)

    out_of_order = [e.seq for a, e in zip(events, events[1:], strict=False) if e.ts < a.ts]
    result.check(
        "timestamps are non-decreasing in sequence order",
        not out_of_order,
        ""
        if not out_of_order
        else f"{len(out_of_order)} out-of-order events, first at seq {out_of_order[0]}",
    )

    ingested = {e.case_id for e in events if e.action is Action.CASE_INGESTED}
    run_scope = {"", "__batch__"}
    orphans = sorted({e.case_id for e in events} - ingested - run_scope)
    result.check(
        "every case referenced has a CASE_INGESTED event",
        not orphans,
        "" if not orphans else f"{len(orphans)} orphan case ids, first {orphans[0]}",
    )

    dup_ingest = len([e for e in events if e.action is Action.CASE_INGESTED]) != len(ingested)
    result.check("no case is ingested twice", not dup_ingest)

    started = [e for e in events if e.action is Action.RUN_STARTED]
    completed = [e for e in events if e.action is Action.RUN_COMPLETED]
    result.check(
        "run is bracketed by RUN_STARTED and RUN_COMPLETED",
        len(started) == 1
        and len(completed) == 1
        and started[0].seq == 1
        and completed[0].seq == len(events),
    )

    # The headline safety invariant: hard-stop categories never see an attempt.
    hard_stop_cases = {
        e.case_id
        for e in events
        if e.action is Action.CLASSIFIED
        and e.category in {RootCause.HARD_DECLINE, RootCause.MANDATE_REVOKED}
    }
    violating = sorted(
        {e.case_id for e in events if e.action is Action.CHARGE_ATTEMPT} & hard_stop_cases
    )
    result.check(
        "zero charge attempts on hard-stop categories",
        not violating,
        "" if not violating else f"{len(violating)} violations, first {violating[0]}",
    )

    unknown_cases = {
        e.case_id
        for e in events
        if e.action is Action.CLASSIFIED and e.category is RootCause.UNKNOWN
    }
    unknown_retried = sorted(
        {e.case_id for e in events if e.action is Action.CHARGE_ATTEMPT} & unknown_cases
    )
    result.check(
        "zero charge attempts on UNKNOWN cases",
        not unknown_retried,
        "" if not unknown_retried else f"{len(unknown_retried)} violations",
    )

    unattributed = [
        e.seq
        for e in events
        if e.action in {Action.STOPPED, Action.COMPLIANCE_REFUSAL} and not e.rule
    ]
    result.check(
        "every stop and refusal names the rule that fired",
        not unattributed,
        ""
        if not unattributed
        else f"{len(unattributed)} unattributed, first seq {unattributed[0]}",
    )

    return result
