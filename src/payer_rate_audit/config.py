"""Configuration loading.

The Medicare conversion factor is deliberately a required config value: it is
published in the annual PFS final rule, it changes every year, and a tool that
hardcodes it silently reports last year's answer.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

RVU_BASIS_CHOICES = ("auto", "facility", "nonfacility")
GROUP_BY_CHOICES = ("plan", "payer")


class ConfigError(ValueError):
    """Raised when config.toml is missing, malformed, or missing a required value."""


@dataclass(frozen=True)
class Config:
    conversion_factor: float
    conversion_factor_source: str
    rvu_year: int
    payable_status_codes: frozenset[str]
    rvu_basis: str
    group_by: str
    billing_class_filter: str | None
    min_join_rate: float
    path: Path | None = field(default=None, compare=False)


def load_config(path: str | Path = "config.toml") -> Config:
    path = Path(path)
    if not path.is_file():
        raise ConfigError(
            f"config file not found: {path}. Copy the config.toml shipped with the "
            "repository and set [medicare].conversion_factor for the year you are auditing."
        )
    with path.open("rb") as handle:
        raw = tomllib.load(handle)

    medicare = raw.get("medicare", {})
    if "conversion_factor" not in medicare:
        raise ConfigError(
            f"{path}: [medicare].conversion_factor is required. It is published in the "
            "annual Medicare Physician Fee Schedule final rule and changes yearly."
        )
    conversion_factor = float(medicare["conversion_factor"])
    if conversion_factor <= 0:
        raise ConfigError(f"{path}: [medicare].conversion_factor must be positive.")

    rvu = raw.get("rvu", {})
    year = int(rvu.get("year", 0))
    if year < 2000:
        raise ConfigError(f"{path}: [rvu].year is required and must be a four-digit year.")
    status_codes = rvu.get("payable_status_codes", ["A", "R", "T"])
    if not status_codes:
        raise ConfigError(f"{path}: [rvu].payable_status_codes must not be empty.")

    audit = raw.get("audit", {})
    rvu_basis = str(audit.get("rvu_basis", "auto")).lower()
    if rvu_basis not in RVU_BASIS_CHOICES:
        raise ConfigError(
            f"{path}: [audit].rvu_basis must be one of {', '.join(RVU_BASIS_CHOICES)}."
        )
    group_by = str(audit.get("group_by", "plan")).lower()
    if group_by not in GROUP_BY_CHOICES:
        raise ConfigError(f"{path}: [audit].group_by must be one of {', '.join(GROUP_BY_CHOICES)}.")
    billing_class_filter = str(audit.get("billing_class_filter", "")).strip().lower() or None

    return Config(
        conversion_factor=conversion_factor,
        conversion_factor_source=str(
            medicare.get("conversion_factor_source", "unspecified (see config.toml)")
        ),
        rvu_year=year,
        payable_status_codes=frozenset(str(code).strip().upper() for code in status_codes),
        rvu_basis=rvu_basis,
        group_by=group_by,
        billing_class_filter=billing_class_filter,
        min_join_rate=float(audit.get("min_join_rate", 0.60)),
        path=path,
    )
