"""Shared fixtures. Every test runs offline: no fixture touches the network."""

from __future__ import annotations

import shutil
from collections.abc import Iterator
from pathlib import Path

import pytest

from reclaim.config import AppConfig, load_config
from reclaim.generate import generate_batch
from reclaim.models import FailedTransaction

REPO_ROOT = Path(__file__).resolve().parents[1]
CONFIG_DIR = REPO_ROOT / "config"

TEST_SEED = 7
TEST_SIZE = 60


@pytest.fixture(scope="session")
def config() -> AppConfig:
    return load_config(CONFIG_DIR)


@pytest.fixture
def config_copy(tmp_path: Path) -> Path:
    """A writable copy of config/, for tests that need to bend a constant."""
    target = tmp_path / "config"
    shutil.copytree(CONFIG_DIR, target)
    return target


@pytest.fixture(scope="session")
def batch(config: AppConfig) -> list[FailedTransaction]:
    return generate_batch(TEST_SEED, TEST_SIZE, config)


@pytest.fixture
def run_dir(tmp_path: Path) -> Iterator[Path]:
    out = tmp_path / "out"
    out.mkdir()
    yield out
