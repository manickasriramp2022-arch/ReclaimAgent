"""Ablation study: which design decision is actually earning the money?

`reclaim benchmark` answers "does this beat the baseline, or did I pick a good
seed?". This module answers the next question a reviewer asks: *why* does it
win. Each variant disables exactly one feature of the system, re-runs the whole
pipeline over the identical seeds, and reports what removing it cost.

A feature that costs nothing to remove is a feature that was not earning its
place, and this is designed to say so. The cost floor is the honest example: it
returns almost no extra recovery, and that is the correct result, because it is
a spend-control rule rather than a recovery rule. It shows up in the attempts
column instead.

Variants mutate a copy of config/policies.yaml on disk and reload it through
the normal loader, so an ablation exercises the same configuration path the
real system uses rather than a special-cased in-memory shortcut.
"""

from __future__ import annotations

import copy
import json
import shutil
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

import yaml

from .baseline import run_baseline
from .classify import Classifier, LlmCache
from .config import AppConfig, load_config
from .engine import RecoveryRun
from .generate import generate_batch, write_batch
from .metrics import build_comparison, compute_metrics
from .models import AblationReport, AblationRow

DEFAULT_ABLATION_SEEDS = 12

PolicyMutator = Callable[[dict[str, Any]], None]

FULL_SYSTEM = "full ReclaimAgent"


def _strip_contact_steps(policies: dict[str, Any]) -> None:
    """Remove every customer nudge, leaving the re-presentment schedule intact."""
    for policy in policies["policies"].values():
        policy["plan"] = [s for s in policy["plan"] if s["channel"] == "retry_charge"]


def _naive_timing(policies: dict[str, Any]) -> None:
    """Keep root-cause routing and nudges, but re-present on the baseline's flat
    24/48/72-hour schedule instead of on each cause's recovery curve."""
    for policy in policies["policies"].values():
        charges = [s for s in policy["plan"] if s["channel"] == "retry_charge"]
        others = [s for s in policy["plan"] if s["channel"] != "retry_charge"]
        for index, step in enumerate(charges):
            step["after_hours"] = 24 * (index + 1)
        policy["plan"] = sorted(others + charges, key=lambda s: float(s["after_hours"]))


def _disable_cost_floor(policies: dict[str, Any]) -> None:
    policies["stopping_rules"]["cost_floor"]["enabled"] = False


def _single_policy_for_everything(policies: dict[str, Any]) -> None:
    """Classify as normal, then ignore the answer: give every recoverable
    category the same plan. Hard stops are deliberately left intact, because
    removing them would test recklessness rather than routing."""
    template = copy.deepcopy(policies["policies"]["ISSUER_SOFT_DECLINE"])
    for name in ("INSUFFICIENT_FUNDS", "EXPIRED_CARD", "TECHNICAL_ERROR"):
        keep_rationale = policies["policies"][name]["rationale"]
        replacement = copy.deepcopy(template)
        replacement["rationale"] = keep_rationale
        policies["policies"][name] = replacement


VARIANTS: list[tuple[str, str, PolicyMutator | None]] = [
    (
        FULL_SYSTEM,
        "Every feature on. The reference the others are measured against.",
        None,
    ),
    (
        "no root-cause routing",
        "What is classification worth? Every recoverable cause gets the same "
        "plan. Hard stops stay, so this measures routing rather than recklessness.",
        _single_policy_for_everything,
    ),
    (
        "naive 24/48/72h timing",
        "What is scheduling on each cause's recovery curve worth? Same routing, "
        "same nudges, but re-presentments move to the baseline's flat interval.",
        _naive_timing,
    ),
    (
        "no customer nudges",
        "What are the dunning email, SMS and update-payment-method link worth? "
        "Charge schedule unchanged, every contact step removed.",
        _strip_contact_steps,
    ),
    (
        "no cost floor",
        "What does the spend-control rule cost in recovery, and save in "
        "attempts? Expect roughly no recovery change; that is the point of it.",
        _disable_cost_floor,
    ),
]


def _variant_config(base_dir: Path, mutate: PolicyMutator | None, workspace: Path) -> AppConfig:
    target = workspace / "config"
    shutil.copytree(base_dir, target)
    if mutate is not None:
        path = target / "policies.yaml"
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        mutate(data)
        path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return load_config(target)


def _measure(
    config: AppConfig, seeds: list[int], size: int
) -> tuple[int, int, int, int, float, bool]:
    recovered = attempts = contacts = cost = 0
    deltas: list[float] = []
    honoured = True
    with tempfile.TemporaryDirectory(prefix="reclaim-ablation-run-") as scratch_str:
        scratch = Path(scratch_str)
        for seed in seeds:
            records = generate_batch(seed, size, config)
            batch_path = write_batch(records, scratch / f"batch_{seed}.jsonl")

            def classifier() -> Classifier:
                return Classifier(
                    config, llm_client=None, cache=LlmCache(Path("/dev/null"), enabled=False)
                )

            result = RecoveryRun(
                config,
                records,
                seed,
                classifier(),
                batch_path,
                out_dir=scratch,
                llm_enabled=False,
            ).execute()
            _, _, baseline_events = run_baseline(
                config, records, seed, classifier(), result.run_id, scratch
            )
            comparison = build_comparison(result.events, baseline_events)
            treatment = comparison.treatment_like_for_like

            recovered += treatment.recovered_paise
            attempts += treatment.charge_attempts
            contacts += treatment.contacts_sent
            cost += compute_metrics(result.events, "x").action_cost_paise
            deltas.append(comparison.like_for_like_delta_pct)
            honoured &= comparison.treatment.correctly_stopped_rate == 1.0

    mean_delta = sum(deltas) / len(deltas) if deltas else 0.0
    return recovered, attempts, contacts, cost, mean_delta, honoured


def run_ablation(
    config: AppConfig,
    seeds: int = DEFAULT_ABLATION_SEEDS,
    size: int = 250,
    first_seed: int = 1,
) -> AblationReport:
    seed_list = list(range(first_seed, first_seed + seeds))
    measured: list[tuple[str, str, tuple[int, int, int, int, float, bool]]] = []

    for name, question, mutate in VARIANTS:
        with tempfile.TemporaryDirectory(prefix="reclaim-ablation-cfg-") as workspace:
            variant_config = _variant_config(config.config_dir, mutate, Path(workspace))
            measured.append((name, question, _measure(variant_config, seed_list, size)))

    full = next(m for m in measured if m[0] == FULL_SYSTEM)[2]
    full_recovered, full_attempts = full[0], full[1]

    rows = [
        AblationRow(
            variant=name,
            question=question,
            recovered_paise=m[0],
            charge_attempts=m[1],
            contacts_sent=m[2],
            action_cost_paise=m[3],
            mean_delta_vs_baseline_pct=round(m[4], 6),
            recovery_vs_full_pct=round(
                (m[0] - full_recovered) / full_recovered if full_recovered else 0.0, 6
            ),
            attempts_vs_full_pct=round(
                (m[1] - full_attempts) / full_attempts if full_attempts else 0.0, 6
            ),
            hard_stops_always_honoured=m[5],
        )
        for name, question, m in measured
    ]
    return AblationReport(
        seeds=len(seed_list),
        batch_size=size,
        config_fingerprint=config.fingerprint,
        rows=rows,
    )


def write_ablation(report: AblationReport, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(report.model_dump_json(indent=2) + "\n", encoding="utf-8")
    return path


def read_ablation(path: Path) -> AblationReport | None:
    if not path.is_file():
        return None
    return AblationReport.model_validate(json.loads(path.read_text(encoding="utf-8")))


def render_ablation(report: AblationReport) -> str:
    lines = [
        f"ablation: each feature disabled in turn, over the same {report.seeds} seeds "
        f"of {report.batch_size} cases",
        "",
        f"{'variant':<26} {'recovered':>13} {'attempts':>9} {'vs baseline':>12} "
        f"{'recovery cost':>14} {'attempts':>9}",
    ]
    for row in report.rows:
        vs_full = "reference" if row.variant == FULL_SYSTEM else f"{row.recovery_vs_full_pct:+.1%}"
        att = "" if row.variant == FULL_SYSTEM else f"{row.attempts_vs_full_pct:+.1%}"
        lines.append(
            f"{row.variant:<26} {row.recovered_paise / 100:>13,.0f} {row.charge_attempts:>9} "
            f"{row.mean_delta_vs_baseline_pct:>+11.1%} {vs_full:>14} {att:>9}"
        )
    lines.append("")
    ranked = report.ranked
    if ranked:
        lines.append("What each decision is worth, most valuable first:")
        for row in ranked:
            lines.append(
                f"  {row.variant:<26} removing it changes recovery by "
                f"{row.recovery_vs_full_pct:+.1%} and attempts by {row.attempts_vs_full_pct:+.1%}"
            )
    lines += [
        "",
        f"hard stops honoured in every variant : "
        f"{all(r.hard_stops_always_honoured for r in report.rows)}",
        "",
        "Outcomes are simulated. A variant that costs nothing to remove is a feature that "
        "was not earning its place, and this table is built to say so.",
    ]
    return "\n".join(lines)
