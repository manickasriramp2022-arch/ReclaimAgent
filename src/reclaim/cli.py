"""Command line interface.

reclaim generate --seed 42 --size 250
reclaim run --batch data/batch_42.jsonl
reclaim report --run <run_id>
reclaim replay --case <case_id>
reclaim verify-audit --run <run_id>
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from . import __version__
from .audit import read_audit
from .baseline import run_baseline
from .classify import AnthropicClient, Classifier, LlmClient
from .config import AppConfig, load_config
from .engine import RecoveryRun, infer_seed
from .escalation import read_escalations
from .generate import (
    DEFAULT_BATCH_SIZE,
    MIN_BATCH_SIZE,
    batch_mix_summary,
    batch_path_for,
    generate_batch,
    read_batch,
    write_batch,
)
from .metrics import (
    build_comparison,
    comparison_headline,
    compute_metrics,
    headline,
    write_metrics,
)
from .replay import case_events, pick_demo_cases, render_replay
from .report import write_report
from .verify import comparison_path, metrics_path, verify_run

LATEST = "latest_run.txt"

SIMULATION_NOTICE = (
    "NOTE: every payment outcome below is SIMULATED from config/simulation.yaml. "
    "No gateway was contacted and no live payment data was used."
)


def _remember_run(run_id: str, out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / LATEST).write_text(run_id + "\n", encoding="utf-8")


def _resolve_run(run_id: str | None, out_dir: Path) -> str:
    if run_id:
        return run_id
    marker = out_dir / LATEST
    if marker.is_file():
        return marker.read_text(encoding="utf-8").strip()
    raise SystemExit("no run id given and no previous run found in out/. Run `reclaim run` first.")


def _build_llm_client(mode: str) -> tuple[LlmClient | None, bool, str]:
    """Returns (client, enabled, notice)."""
    if mode == "off":
        return None, False, "LLM fallback disabled (--no-llm): rule layer only."
    has_key = bool(os.environ.get("ANTHROPIC_API_KEY"))
    if mode == "on":
        if not has_key:
            raise SystemExit(
                "--llm was requested but ANTHROPIC_API_KEY is not set. "
                "Set it, or run with --no-llm."
            )
        return AnthropicClient(), True, "LLM fallback enabled for unmapped decline codes."
    if has_key:
        return AnthropicClient(), True, "LLM fallback enabled (ANTHROPIC_API_KEY found)."
    return (
        None,
        False,
        "ANTHROPIC_API_KEY not set, so the LLM fallback is off and unmapped "
        "decline codes will be escalated as UNKNOWN. This is the offline demo path.",
    )


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------
def cmd_generate(args: argparse.Namespace) -> int:
    config = load_config(args.config_dir)
    records = generate_batch(args.seed, args.size, config)
    path = write_batch(records, batch_path_for(args.seed, args.data_dir))
    total = sum(r.amount_paise for r in records)
    print(f"wrote {len(records)} synthetic test-mode records to {path}")
    print(f"value at risk: Rs {total / 100:,.2f}")
    print("\ndecline mix:")
    for code, count in batch_mix_summary(records).items():
        print(f"  {code:<26} {count:>4}  {count / len(records):>6.1%}")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    config: AppConfig = load_config(args.config_dir)
    batch_path = Path(args.batch)
    records = read_batch(batch_path)
    seed = args.seed if args.seed is not None else infer_seed(batch_path, records)

    client, llm_enabled, notice = _build_llm_client(args.llm_mode)
    classifier = Classifier(config, llm_client=client)

    print(f"ReclaimAgent {__version__}")
    print(SIMULATION_NOTICE)
    print(notice)
    print(f"batch {batch_path} ({len(records)} cases, seed {seed})\n")

    run = RecoveryRun(
        config,
        records,
        seed,
        classifier,
        batch_path,
        out_dir=args.out_dir,
        horizon_hours=args.horizon_hours,
        llm_enabled=llm_enabled,
    )
    result = run.execute()

    baseline_classifier = Classifier(config, llm_client=client, cache=classifier.cache)
    _, baseline_file, baseline_events = run_baseline(
        config, records, seed, baseline_classifier, result.run_id, args.out_dir
    )

    metrics = compute_metrics(result.events, "reclaimagent")
    comparison = build_comparison(result.events, baseline_events)
    write_metrics(metrics, metrics_path(result.run_id, args.out_dir))
    comparison_path(result.run_id, args.out_dir).write_text(
        comparison.model_dump_json(indent=2) + "\n", encoding="utf-8"
    )
    _remember_run(result.run_id, args.out_dir)

    print(f"run id: {result.run_id}")
    print(f"audit log: {result.audit_file}  ({len(result.events)} events)")
    print(f"baseline log: {baseline_file}")
    print(f"escalations: {result.escalation_file}  ({len(result.escalations)} cases)\n")
    for line in headline(metrics):
        print(line)
    print()
    for line in comparison_headline(comparison):
        print(line)
    if classifier.llm_calls_made:
        print(f"\nLLM fallback calls made: {classifier.llm_calls_made} (cached by decline code)")
    print(f"\nNext: reclaim report --run {result.run_id}")
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    run_id = _resolve_run(args.run, args.out_dir)
    path = write_report(run_id, args.out_dir, load_config(args.config_dir))
    print(f"wrote {path}")
    if args.open:
        import webbrowser

        webbrowser.open(path.resolve().as_uri())
    return 0


def cmd_replay(args: argparse.Namespace) -> int:
    run_id = _resolve_run(args.run, args.out_dir)
    case_id = args.case
    if case_id in {"@success", "@stopped"}:
        success, stopped = pick_demo_cases(run_id, args.out_dir)
        picked = success if case_id == "@success" else stopped
        if picked is None:
            print(f"no case matching {case_id} in run {run_id}")
            return 1
        case_id = picked
    print(SIMULATION_NOTICE)
    print()
    print(render_replay(case_events(run_id, case_id, args.out_dir), case_id))
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    run_id = _resolve_run(args.run, args.out_dir)
    result = verify_run(run_id, args.out_dir)
    print(result.render())
    return 0 if result.ok else 1


def cmd_queue(args: argparse.Namespace) -> int:
    run_id = _resolve_run(args.run, args.out_dir)
    records = [
        e for e in read_escalations(args.out_dir / "escalations.jsonl") if e.run_id == run_id
    ]
    print(f"escalation queue for run {run_id}: {len(records)} cases")
    total = sum(r.amount_at_risk_paise for r in records)
    print(f"value sitting in the queue: Rs {total / 100:,.2f}")
    print("ranked by amount at risk weighted by how much a human can still do about it\n")
    for rec in records[: args.limit]:
        print(
            f"  #{rec.priority_rank:<3} Rs {rec.amount_at_risk_paise / 100:>10,.2f} at risk  "
            f"(priority {rec.priority_score:>9,.0f})  {rec.category:<20} {rec.case_id}"
        )
        print(f"       stopped by: {rec.stopping_rule}")
        print(f"       do next   : {rec.recommended_action}")
    return 0


def cmd_events(args: argparse.Namespace) -> int:
    run_id = _resolve_run(args.run, args.out_dir)
    events = read_audit(args.out_dir / f"audit_{run_id}.jsonl")
    for event in events[: args.limit]:
        print(event.model_dump_json())
    return 0


# --------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="reclaim",
        description=(
            "ReclaimAgent: failed-payment root-cause and recovery agent. "
            "Test-mode synthetic data and simulated outcomes only."
        ),
    )
    parser.add_argument("--version", action="version", version=f"reclaim {__version__}")
    parser.add_argument(
        "--config-dir", type=Path, default=None, help="override the config/ directory"
    )
    parser.add_argument("--out-dir", type=Path, default=Path("out"), help="output directory")
    sub = parser.add_subparsers(dest="command", required=True)

    gen = sub.add_parser("generate", help="generate a synthetic failed-payment batch")
    gen.add_argument("--seed", type=int, default=42)
    gen.add_argument(
        "--size", type=int, default=DEFAULT_BATCH_SIZE, help=f"batch size (min {MIN_BATCH_SIZE})"
    )
    gen.add_argument("--data-dir", type=Path, default=Path("data"))
    gen.set_defaults(func=cmd_generate)

    run = sub.add_parser("run", help="run the recovery pipeline over a batch")
    run.add_argument("--batch", required=True)
    run.add_argument("--seed", type=int, default=None, help="override the inferred batch seed")
    run.add_argument(
        "--horizon-hours", type=int, default=24 * 21, help="simulated recovery horizon"
    )
    llm = run.add_mutually_exclusive_group()
    llm.add_argument(
        "--no-llm",
        dest="llm_mode",
        action="store_const",
        const="off",
        help="rule layer only; fully offline, unmapped codes escalate as UNKNOWN",
    )
    llm.add_argument(
        "--llm",
        dest="llm_mode",
        action="store_const",
        const="on",
        help="require the LLM fallback (fails if ANTHROPIC_API_KEY is unset)",
    )
    run.set_defaults(func=cmd_run, llm_mode="auto")

    rep = sub.add_parser("report", help="render the self-contained HTML report")
    rep.add_argument("--run", default=None)
    rep.add_argument("--open", action="store_true", help="open the report in a browser")
    rep.set_defaults(func=cmd_report)

    rep2 = sub.add_parser("replay", help="print the full decision chain for one case")
    rep2.add_argument(
        "--case",
        required=True,
        help="case id, or @success / @stopped to pick the headline demo cases",
    )
    rep2.add_argument("--run", default=None)
    rep2.set_defaults(func=cmd_replay)

    ver = sub.add_parser(
        "verify-audit", help="check log integrity and recompute every metric from the log"
    )
    ver.add_argument("--run", default=None)
    ver.set_defaults(func=cmd_verify)

    que = sub.add_parser("queue", help="print the human escalation queue")
    que.add_argument("--run", default=None)
    que.add_argument("--limit", type=int, default=15)
    que.set_defaults(func=cmd_queue)

    evt = sub.add_parser("events", help="dump raw audit events as JSONL")
    evt.add_argument("--run", default=None)
    evt.add_argument("--limit", type=int, default=20)
    evt.set_defaults(func=cmd_events)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        result: int = args.func(args)
    except (FileNotFoundError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return result


if __name__ == "__main__":
    raise SystemExit(main())
