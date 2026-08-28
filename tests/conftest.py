from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from payer_rate_audit.config import load_config
from payer_rate_audit.rvu import parse_pprrvu

FIXTURES = Path(__file__).parent / "fixtures"
SHAPE_FIXTURES = {
    "csv_tall": FIXTURES / "tall.csv",
    "csv_wide": FIXTURES / "wide.csv",
    "json": FIXTURES / "nested.json",
}


@pytest.fixture(scope="session")
def fixtures_dir() -> Path:
    return FIXTURES


@pytest.fixture(scope="session")
def config():
    return load_config(Path(__file__).resolve().parents[1] / "config.toml")


@pytest.fixture(scope="session")
def rvu():
    return parse_pprrvu(FIXTURES / "pprrvu_sample.csv", year=2026)


@pytest.fixture
def payer_config(config):
    return dataclasses.replace(config, group_by="payer")
