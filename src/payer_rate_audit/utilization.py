"""The adapter-neutral utilization layer: claim lines in, repriced mix out.

Every remittance format this tool reads — FHIR ``ExplanationOfBenefit`` today,
X12 835 remittance advice as well — normalizes to one line-item table and is
repriced by the same code. An adapter's only job is to fill :class:`LineSource`
and account for whatever it could not use, in the same counted-exclusion style
the MRF path uses.

No patient identifiers live in this table. Remittance files are PHI in the real
world; this tool reports aggregates, so the identifiers are never read into the
frame in the first place.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

from .metrics import JoinResult

LINE_COLUMNS = [
    "source_file",
    "payer_name",
    "line",
    "code",
    "code_type",
    "modifiers",
    "serviced_date",
    "units",
    "submitted_amount",
    "allowed_amount",
    "paid_amount",
]

_REPRICE_COLUMNS = [
    "codes_priced",
    "codes_unpriced",
    "units_priced",
    "actual_paid",
    "repriced",
    "delta",
    "ratio",
]


class LineFormatError(ValueError):
    """Raised when an input path holds nothing this tool can read."""


@dataclass
class LineSource:
    """Parsed claim lines plus the counts needed to state a denominator.

    ``exclusions`` maps a human-readable reason to the number of records it
    accounts for, so an adapter can report format-specific skips (a non-EOB
    FHIR resource, an 835 service line with a non-HCPCS qualifier) without the
    report knowing anything about the format.
    """

    frame: pd.DataFrame
    source: str
    kind: str
    files_read: int = 0
    records_read: int = 0
    line_items_read: int = 0
    line_items_without_code: int = 0
    unreadable_files: list[str] = field(default_factory=list)
    exclusions: dict[str, int] = field(default_factory=dict)

    @property
    def row_count(self) -> int:
        return len(self.frame)

    def exclude(self, reason: str, count: int = 1) -> None:
        self.exclusions[reason] = self.exclusions.get(reason, 0) + count


@dataclass
class UtilizationAudit:
    """Everything the report needs about one remittance source."""

    parse: LineSource
    utilization: pd.DataFrame
    repriced: pd.DataFrame


def empty_frame() -> pd.DataFrame:
    return pd.DataFrame(columns=LINE_COLUMNS)


def to_frame(records: list[dict[str, object]]) -> pd.DataFrame:
    frame = pd.DataFrame(records, columns=LINE_COLUMNS)
    for column in ("units", "submitted_amount", "allowed_amount", "paid_amount"):
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    return frame


def input_files(path: str | Path, suffixes: set[str] | None = None) -> list[Path]:
    """Every candidate file under ``path``, or ``path`` itself if it is a file."""
    root = Path(path)
    if not root.exists():
        raise LineFormatError(f"{root}: no such file or directory")
    if root.is_file():
        return [root]
    files = sorted(
        candidate
        for candidate in root.rglob("*")
        if candidate.is_file() and (suffixes is None or candidate.suffix.lower() in suffixes)
    )
    if not files:
        raise LineFormatError(f"{root}: no readable files found")
    return files


def utilization(parse: LineSource) -> pd.DataFrame:
    """Observed service mix: units and actual dollars per code.

    Negative lines (835 reversals and take-backs) net against the positive ones
    for the same code rather than being dropped or double-counted.
    """
    frame = parse.frame
    columns = ["code", "code_type", "line_items", "units", "actual_paid", "actual_allowed"]
    if frame.empty:
        return pd.DataFrame(columns=columns)
    return (
        frame.groupby(["code", "code_type"], as_index=False)
        .agg(
            line_items=("code", "size"),
            units=("units", "sum"),
            actual_paid=("paid_amount", "sum"),
            actual_allowed=("allowed_amount", "sum"),
        )
        .sort_values("units", ascending=False)
        .reset_index(drop=True)
    )


def reprice(parse: LineSource, join: JoinResult, group_by: str = "plan") -> pd.DataFrame:
    """Reprice the observed service mix at each payer's contracted rate.

    The rate used for a code is the mean negotiated dollar across that payer's
    MRF rows for the code, so a payer with several rows for one code (different
    settings, modifiers) does not get counted several times. Codes the payer
    does not price are excluded and counted per payer, because a payer cannot
    be charged with a rate it never published.
    """
    keys = ["payer_name", "plan_name"] if group_by == "plan" else ["payer_name"]
    volume = utilization(parse)
    empty = pd.DataFrame(columns=[*keys, *_REPRICE_COLUMNS])
    if volume.empty:
        return empty

    priced = join.frame[join.frame["negotiated_dollar"].notna()]
    if priced.empty:
        return empty
    rates = (
        priced.groupby([*keys, "code", "code_type"], as_index=False)["negotiated_dollar"]
        .mean()
        .rename(columns={"negotiated_dollar": "rate"})
    )

    merged = rates.merge(volume, on=["code", "code_type"], how="right")
    matched = merged[merged["rate"].notna()].copy()
    if matched.empty:
        return empty
    matched["repriced"] = matched["rate"] * matched["units"]

    codes_in_volume = len(volume)
    table = matched.groupby(keys, as_index=False).agg(
        codes_priced=("code", "nunique"),
        units_priced=("units", "sum"),
        actual_paid=("actual_paid", "sum"),
        repriced=("repriced", "sum"),
    )
    table["codes_unpriced"] = codes_in_volume - table["codes_priced"]
    table["delta"] = table["repriced"] - table["actual_paid"]
    table["ratio"] = table["repriced"] / table["actual_paid"].replace(0, pd.NA)
    return (
        table[[*keys, *_REPRICE_COLUMNS]]
        .sort_values("repriced", ascending=False)
        .reset_index(drop=True)
    )


def audit_lines(parse: LineSource, join: JoinResult, group_by: str = "plan") -> UtilizationAudit:
    return UtilizationAudit(
        parse=parse,
        utilization=utilization(parse),
        repriced=reprice(parse, join, group_by=group_by),
    )
