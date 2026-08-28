"""Component 1: the synthetic batch generator."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from reclaim.config import AppConfig
from reclaim.generate import (
    MIN_BATCH_SIZE,
    batch_mix_summary,
    generate_batch,
    read_batch,
    write_batch,
)
from reclaim.models import MandateStatus, PaymentMethod


def test_same_seed_gives_an_identical_batch(config: AppConfig) -> None:
    a = generate_batch(11, 80, config)
    b = generate_batch(11, 80, config)
    assert [r.model_dump() for r in a] == [r.model_dump() for r in b]


def test_different_seeds_give_different_batches(config: AppConfig) -> None:
    a = generate_batch(11, 80, config)
    b = generate_batch(12, 80, config)
    assert [r.transaction_id for r in a] != [r.decline_code for r in b]


def test_batch_size_floor_is_enforced(config: AppConfig) -> None:
    with pytest.raises(ValueError, match="at least 50"):
        generate_batch(1, MIN_BATCH_SIZE - 1, config)


def test_every_record_is_marked_test_mode(config: AppConfig) -> None:
    assert all(r.environment == "test" for r in generate_batch(3, 60, config))


def test_a_non_test_record_cannot_be_constructed(config: AppConfig) -> None:
    payload = generate_batch(3, 60, config)[0].model_dump(mode="json")
    payload["environment"] = "live"
    from reclaim.models import FailedTransaction

    with pytest.raises(ValueError, match="refusing to construct a non-test transaction"):
        FailedTransaction.model_validate(payload)


def test_distribution_is_skewed_not_uniform(config: AppConfig) -> None:
    """Insufficient funds and soft declines must dominate; hard declines and
    revoked mandates must stay a minority. A uniform mix would make the whole
    recovery story meaningless."""
    records = generate_batch(42, 400, config)
    mix = batch_mix_summary(records)
    total = len(records)
    soft_and_nsf = sum(v for k, v in mix.items() if k.startswith(("NF_", "SD_"))) / total
    hard_and_revoked = sum(v for k, v in mix.items() if k.startswith(("HD_", "MR_"))) / total
    assert soft_and_nsf > 0.55, f"soft+NSF share was only {soft_and_nsf:.1%}"
    assert hard_and_revoked < 0.20, f"hard+revoked share was {hard_and_revoked:.1%}"
    assert mix["NF_INSUFFICIENT_FUNDS"] == max(mix.values())


def test_mandate_state_is_consistent_with_the_decline_code(config: AppConfig) -> None:
    for record in generate_batch(5, 400, config):
        if record.decline_code == "MR_MANDATE_REVOKED":
            assert record.mandate_status is MandateStatus.REVOKED
        if record.decline_code == "MR_MANDATE_EXPIRED":
            assert record.mandate_status is MandateStatus.EXPIRED
        if record.payment_method is not PaymentMethod.E_MANDATE:
            assert record.mandate_id is None


def test_roundtrip_through_jsonl(config: AppConfig, tmp_path: Path) -> None:
    records = generate_batch(9, 60, config)
    path = write_batch(records, tmp_path / "batch_9.jsonl")
    assert [r.model_dump() for r in read_batch(path)] == [r.model_dump() for r in records]
    first = json.loads(path.read_text().splitlines()[0])
    assert first["environment"] == "test"
    assert first["currency"] == "INR"


def test_reading_an_empty_batch_fails_loudly(tmp_path: Path) -> None:
    path = tmp_path / "empty.jsonl"
    path.write_text("")
    with pytest.raises(ValueError, match="no records"):
        read_batch(path)
