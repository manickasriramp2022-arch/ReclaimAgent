"""Sensitivity sweep: does the headline delta survive a change of seed?

One seed's result is an anecdote. This module runs the identical comparison
over many independently generated batches and reports the distribution, with
the worst case given the same prominence as the mean. If the policy engine only
beats the baseline on the seed that happened to be in the README, this is what
says so.

Nothing here writes an audit log to disk. Each run is executed in a temporary
directory and only its recomputed metrics are kept, so a sweep cannot overwrite
the artefacts of a real run.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from .baseline import run_baseline
from .classify import Classifier, LlmCache
from .config import AppConfig
from .engine import RecoveryRun
from .generate import generate_batch, write_batch
from .metrics import build_comparison
from .models import BenchmarkReport, BenchmarkRow

DEFAULT_SEEDS = 30


def _classifier(config: AppConfig) -> Classifier:
    # Rule layer only, and no cache on disk: a sweep must not touch out/.
    return Classifier(config, llm_client=None, cache=LlmCache(Path("/dev/null"), enabled=False))


def run_benchmark(
    config: AppConfig,
    seeds: int = DEFAULT_SEEDS,
    size: int = 250,
    first_seed: int = 1,
) -> BenchmarkReport:
    rows: list[BenchmarkRow] = []
    with tempfile.TemporaryDirectory(prefix="reclaim-benchmark-") as tmp:
        scratch = Path(tmp)
        for seed in range(first_seed, first_seed + seeds):
            records = generate_batch(seed, size, config)
            batch_path = write_batch(records, scratch / f"batch_{seed}.jsonl")

            run = RecoveryRun(
                config,
                records,
                seed,
                _classifier(config),
                batch_path,
                out_dir=scratch,
                llm_enabled=False,
            )
            result = run.execute()
            _, _, baseline_events = run_baseline(
                config, records, seed, _classifier(config), result.run_id, scratch
            )
            comparison = build_comparison(result.events, baseline_events)
            t = comparison.treatment_like_for_like
            b = comparison.baseline_like_for_like

            rows.append(
                BenchmarkRow(
                    seed=seed,
                    cases=comparison.treatment.cases,
                    addressable_cases=t.cases,
                    treatment_recovered_paise=t.recovered_paise,
                    baseline_recovered_paise=b.recovered_paise,
                    delta_paise=comparison.like_for_like_delta_paise,
                    delta_pct=round(comparison.like_for_like_delta_pct, 6),
                    treatment_attempts=t.charge_attempts,
                    baseline_attempts=b.charge_attempts,
                    attempt_delta=comparison.attempt_delta,
                    attempt_delta_pct=round(comparison.attempt_delta_pct, 6),
                    hard_stop_cases=comparison.treatment.hard_stop_cases,
                    correctly_stopped_rate=comparison.treatment.correctly_stopped_rate,
                    circuit_breaker_tripped=comparison.treatment.circuit_breaker_tripped,
                )
            )

    return BenchmarkReport(
        seeds=len(rows),
        batch_size=size,
        config_fingerprint=config.fingerprint,
        rows=rows,
    )


def write_benchmark(report: BenchmarkReport, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(report.model_dump_json(indent=2) + "\n", encoding="utf-8")
    return path


def read_benchmark(path: Path) -> BenchmarkReport | None:
    if not path.is_file():
        return None
    return BenchmarkReport.model_validate(json.loads(path.read_text(encoding="utf-8")))


def render_benchmark(report: BenchmarkReport) -> str:
    lines = [
        f"sensitivity sweep: {report.seeds} independently generated batches of "
        f"{report.batch_size} cases",
        "",
        f"{'seed':>5} {'ReclaimAgent':>14} {'naive 3x':>13} {'delta':>13} {'delta %':>9} "
        f"{'attempts':>9} {'hard stop':>10}",
    ]
    for row in report.rows:
        lines.append(
            f"{row.seed:>5} {row.treatment_recovered_paise / 100:>14,.0f} "
            f"{row.baseline_recovered_paise / 100:>13,.0f} {row.delta_paise / 100:>13,.0f} "
            f"{row.delta_pct:>+8.1%} {row.attempt_delta_pct:>+8.0%} "
            f"{row.correctly_stopped_rate:>10.0%}"
        )
    lines += [
        "",
        f"seeds where ReclaimAgent recovers more : {report.wins}/{report.seeds}",
        f"seeds where it recovers less           : {report.losses}/{report.seeds}",
        f"delta, mean / median                   : {report.mean_delta_pct:+.1%} / "
        f"{report.median_delta_pct:+.1%}",
        f"delta, worst / best seed               : {report.worst_delta_pct:+.1%} / "
        f"{report.best_delta_pct:+.1%}",
        f"charge attempts, mean change           : {report.mean_attempt_delta_pct:+.1%}",
        f"hard stops honoured on every seed      : {report.hard_stops_always_honoured}",
        "",
        "Outcomes are simulated. What this sweep measures is whether the policy engine's "
        "advantage over the baseline is a property of the strategy or of one lucky batch.",
    ]
    return "\n".join(lines)
