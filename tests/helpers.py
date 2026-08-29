"""Helpers shared across test modules."""

from __future__ import annotations

from pathlib import Path

from reclaim.classify import Classifier, LlmClient
from reclaim.config import AppConfig
from reclaim.engine import RecoveryRun
from reclaim.generate import write_batch
from reclaim.models import FailedTransaction

TEST_SEED = 7


def build_run(
    config: AppConfig,
    records: list[FailedTransaction],
    out_dir: Path,
    tmp_path: Path,
    seed: int = TEST_SEED,
    llm_client: LlmClient | None = None,
) -> RecoveryRun:
    batch_path = write_batch(records, tmp_path / f"batch_{seed}.jsonl")
    return RecoveryRun(
        config,
        records,
        seed,
        Classifier(config, llm_client=llm_client, cache=_NoCache()),
        batch_path,
        out_dir=out_dir,
        llm_enabled=llm_client is not None,
    )


class _NoCache:
    """Disk-free LLM cache stand-in, so tests never write out/llm_cache.json."""

    enabled = False
    path = Path("/dev/null")

    def get(self, code: str) -> None:
        return None

    def put(self, *args: object, **kwargs: object) -> None:
        return None

    def flush(self) -> None:
        return None
