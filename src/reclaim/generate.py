"""Component 1: synthetic failed-transaction batch generator.

Every field distribution lives in config/generator.yaml. Nothing here contains
magic numbers, and nothing here produces a record that is not marked
environment=test. Given the same seed and the same config the output file is
byte-identical.
"""

from __future__ import annotations

import json
import random
from datetime import timedelta
from pathlib import Path

from .config import AppConfig, GeneratorConfig, load_config
from .models import FailedTransaction, MandateStatus, PaymentMethod

MIN_BATCH_SIZE = 50
DEFAULT_BATCH_SIZE = 250


def _weighted_choice(rng: random.Random, options: list[str], weights: list[float]) -> str:
    return rng.choices(options, weights=weights, k=1)[0]


def _pick_method(rng: random.Random, gen: GeneratorConfig, allowed: list[str]) -> PaymentMethod:
    """Pick a payment method consistent with both the global mix and the
    methods the chosen decline code can actually occur on."""
    names = [m for m in gen.payment_method_mix if m in allowed]
    if not names:
        names = list(allowed)
    weights = [gen.payment_method_mix.get(n, 0.01) for n in names]
    return PaymentMethod(_weighted_choice(rng, names, weights))


def _pick_amount(rng: random.Random, gen: GeneratorConfig) -> int:
    band = rng.choices(gen.amount_bands, weights=[b.weight for b in gen.amount_bands], k=1)[0]
    # Round to the nearest rupee: real subscription debits are not sub-rupee.
    return int(rng.randint(band.min_paise, band.max_paise) // 100 * 100)


def _pick_prior_attempts(rng: random.Random, gen: GeneratorConfig) -> int:
    keys = sorted(gen.prior_attempt_distribution)
    weights = [gen.prior_attempt_distribution[k] for k in keys]
    return int(rng.choices(keys, weights=weights, k=1)[0])


def _pick_mandate_status(rng: random.Random, gen: GeneratorConfig, code: str) -> MandateStatus:
    """Mandate state must be consistent with the decline code: a revoked-mandate
    decline cannot sit on an active mandate."""
    if code == "MR_MANDATE_REVOKED":
        return MandateStatus.REVOKED
    if code == "MR_MANDATE_EXPIRED":
        return MandateStatus.EXPIRED
    names = list(gen.mandate_status_mix)
    weights = [gen.mandate_status_mix[n] for n in names]
    return MandateStatus(_weighted_choice(rng, names, weights))


def generate_batch(
    seed: int,
    size: int = DEFAULT_BATCH_SIZE,
    config: AppConfig | None = None,
) -> list[FailedTransaction]:
    """Build a reproducible batch of failed transactions."""
    if size < MIN_BATCH_SIZE:
        raise ValueError(f"batch size must be at least {MIN_BATCH_SIZE} (got {size})")
    cfg = config or load_config()
    gen = cfg.generator
    rng = random.Random(seed)

    codes = [d.code for d in gen.decline_mix]
    weights = [d.weight for d in gen.decline_mix]
    by_code = {d.code: d for d in gen.decline_mix}

    anchor = gen.batch_anchor_utc
    records: list[FailedTransaction] = []

    for i in range(size):
        code = _weighted_choice(rng, codes, weights)
        spec = by_code[code]
        method = _pick_method(rng, gen, spec.methods)
        amount = _pick_amount(rng, gen)

        minutes_back = rng.randint(0, gen.attempt_window_hours * 60)
        attempt_ts = anchor - timedelta(minutes=minutes_back)

        channels = [
            name for name, prob in gen.contact_channel_availability.items() if rng.random() < prob
        ]
        consent = rng.random() < gen.contact_consent_probability

        mandate_id: str | None = None
        mandate_status: MandateStatus | None = None
        pre_debit_ts = None
        if method is PaymentMethod.E_MANDATE:
            mandate_id = f"mdt_{seed}_{i:05d}"
            mandate_status = _pick_mandate_status(rng, gen, code)
            if rng.random() < gen.pre_debit_notice_sent_probability:
                # Notification sent somewhere in the 24-72h before the debit.
                lead = rng.randint(24, 72)
                pre_debit_ts = attempt_ts - timedelta(hours=lead)

        records.append(
            FailedTransaction(
                transaction_id=f"txn_{seed}_{i:05d}",
                case_id=f"case_{seed}_{i:05d}",
                customer_id=f"cust_{seed}_{rng.randint(1, max(10, size // 3)):05d}",
                merchant_id=f"merch_{rng.randint(1, 6):03d}",
                amount_paise=amount,
                currency=gen.currency,
                payment_method=method,
                attempt_ts=attempt_ts,
                decline_code=code,
                decline_description=spec.description,
                mandate_id=mandate_id,
                mandate_status=mandate_status,
                contact_channels=channels,
                contact_consent=consent,
                prior_attempt_count=_pick_prior_attempts(rng, gen),
                pre_debit_notice_sent_ts=pre_debit_ts,
                environment=gen.environment,
            )
        )
    return records


def write_batch(records: list[FailedTransaction], path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for rec in records:
            fh.write(json.dumps(rec.model_dump(mode="json"), sort_keys=True) + "\n")
    return path


def read_batch(path: Path) -> list[FailedTransaction]:
    records: list[FailedTransaction] = []
    with path.open(encoding="utf-8") as fh:
        for line_no, line in enumerate(fh, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                records.append(FailedTransaction.model_validate(json.loads(line)))
            except Exception as exc:  # noqa: BLE001 - want the line number in the message
                raise ValueError(f"{path}:{line_no}: invalid batch record: {exc}") from exc
    if not records:
        raise ValueError(f"{path} contains no records")
    return records


def batch_path_for(seed: int, data_dir: Path = Path("data")) -> Path:
    return data_dir / f"batch_{seed}.jsonl"


def batch_mix_summary(records: list[FailedTransaction]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for rec in records:
        counts[rec.decline_code] = counts.get(rec.decline_code, 0) + 1
    return dict(sorted(counts.items(), key=lambda kv: -kv[1]))
