"""CMS Physician Fee Schedule relative value (PPRRVU) file parsing.

The PPRRVU CSV carries five stacked header rows whose cells concatenate into one
logical column name (``NON-FACILITY`` + ``TOTAL``), a copyright banner above
them, and long CPT descriptors that are AMA-copyrighted. Columns are located by
matching those concatenated names, not by position, and the parser raises rather
than returning empty columns when a name it needs is gone.

Descriptors are dropped on read: this repository carries code, not licensed
content.
"""

from __future__ import annotations

import csv
import re
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

RVU_COLUMNS = [
    "hcpcs",
    "modifier",
    "status_code",
    "work_rvu",
    "nonfac_pe_rvu",
    "fac_pe_rvu",
    "mp_rvu",
    "nonfac_total",
    "fac_total",
    "pctc_indicator",
    "global_days",
]

# Logical column name (concatenation of the stacked header rows) for each field.
_HEADER_NAMES = {
    "hcpcs": "HCPCS",
    "modifier": "MOD",
    "status_code": "STATUS CODE",
    "work_rvu": "WORK RVU",
    "nonfac_pe_rvu": "NON-FAC PE RVU",
    "fac_pe_rvu": "FACILITY PE RVU",
    "mp_rvu": "MP RVU",
    "nonfac_total": "NON-FACILITY TOTAL",
    "fac_total": "FACILITY TOTAL",
    "pctc_indicator": "PCTC IND",
    "global_days": "GLOB DAYS",
}

_NUMERIC_FIELDS = ("work_rvu", "nonfac_pe_rvu", "fac_pe_rvu", "mp_rvu",
                   "nonfac_total", "fac_total")

# Modifiers that select a distinct priced row in the PPRRVU file. Every other
# modifier (50, 59, LT, ...) is a payment-adjustment modifier with no separate
# RVU row. See ASSUMPTIONS.md, decision 2.
PRICING_MODIFIERS = ("26", "TC", "53")

_HEADER_SEARCH_ROWS = 40
_HEADER_STACK_DEPTH = 6


class RVUFormatError(ValueError):
    """Raised when the PPRRVU file does not match the expected record layout."""


@dataclass
class RVUTable:
    frame: pd.DataFrame
    source_file: str
    year: int | None = None

    @property
    def row_count(self) -> int:
        return len(self.frame)


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", str(text).replace("\ufeff", "").strip()).upper()


def _locate_columns(rows: list[list[str]], header_index: int) -> dict[str, int]:
    """Match each required field to a column index using the stacked header rows."""
    stack = rows[max(0, header_index - _HEADER_STACK_DEPTH):header_index + 1]
    width = max(len(row) for row in stack)
    composites: dict[int, str] = {}
    for column in range(width):
        parts = [
            rows[row][column]
            for row in range(max(0, header_index - _HEADER_STACK_DEPTH), header_index + 1)
            if column < len(rows[row]) and rows[row][column].strip()
        ]
        composites[column] = _normalize(" ".join(parts))

    positions: dict[str, int] = {}
    missing: list[str] = []
    for field_name, expected in _HEADER_NAMES.items():
        wanted = _normalize(expected)
        match = next((col for col, name in composites.items() if name == wanted), None)
        if match is None:
            missing.append(f"{field_name} (expected header {expected!r})")
        else:
            positions[field_name] = match
    if missing:
        raise RVUFormatError(
            "PPRRVU column layout has changed; could not locate: "
            + "; ".join(missing)
            + ". Headers seen: "
            + ", ".join(sorted({name for name in composites.values() if name})[:40])
        )
    return positions


def parse_pprrvu(path: str | Path, year: int | None = None) -> RVUTable:
    """Parse a PPRRVU CSV into :data:`RVU_COLUMNS`, dropping CPT descriptors."""
    path = Path(path)
    if not path.is_file():
        raise RVUFormatError(
            f"RVU file not found: {path}. Run 'python scripts/fetch_rvu.py' to download it."
        )
    with path.open("r", encoding="latin-1", newline="") as handle:
        rows = list(csv.reader(handle))

    header_index = next(
        (
            index
            for index, row in enumerate(rows[:_HEADER_SEARCH_ROWS])
            if row and _normalize(row[0]) == "HCPCS"
        ),
        None,
    )
    if header_index is None:
        raise RVUFormatError(
            f"{path}: no PPRRVU header row found in the first {_HEADER_SEARCH_ROWS} rows "
            "(expected a row whose first cell is 'HCPCS')."
        )
    positions = _locate_columns(rows, header_index)

    records = []
    for row in rows[header_index + 1:]:
        if not row or not row[0].strip():
            continue
        hcpcs = row[positions["hcpcs"]].strip().upper() if positions["hcpcs"] < len(row) else ""
        # HCPCS codes are 5 characters; anything else is a trailing note row.
        if not re.fullmatch(r"[A-Z0-9]{5}", hcpcs):
            continue

        def cell(field_name: str, current_row: list[str] = row) -> str:
            position = positions[field_name]
            return current_row[position].strip() if position < len(current_row) else ""

        records.append(
            {
                "hcpcs": hcpcs,
                "modifier": cell("modifier").upper(),
                "status_code": cell("status_code").upper(),
                "work_rvu": cell("work_rvu"),
                "nonfac_pe_rvu": cell("nonfac_pe_rvu"),
                "fac_pe_rvu": cell("fac_pe_rvu"),
                "mp_rvu": cell("mp_rvu"),
                "nonfac_total": cell("nonfac_total"),
                "fac_total": cell("fac_total"),
                "pctc_indicator": cell("pctc_indicator"),
                "global_days": cell("global_days").upper(),
            }
        )

    if not records:
        raise RVUFormatError(f"{path}: header row parsed but no HCPCS data rows were found.")

    frame = pd.DataFrame(records, columns=RVU_COLUMNS)
    for column in _NUMERIC_FIELDS:
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    if frame[list(_NUMERIC_FIELDS)].notna().sum().sum() == 0:
        raise RVUFormatError(
            f"{path}: every RVU column parsed as empty. The file layout has changed."
        )
    frame = frame.drop_duplicates(subset=["hcpcs", "modifier"], keep="first")
    return RVUTable(frame=frame.reset_index(drop=True), source_file=path.name, year=year)


def find_rvu_file(data_dir: str | Path = "data", year: int | None = None) -> Path:
    """Find a downloaded PPRRVU CSV in ``data_dir``, newest first."""
    data_dir = Path(data_dir)
    pattern = f"PPRRVU{year}*.csv" if year else "PPRRVU*.csv"
    candidates = sorted(data_dir.glob(pattern), key=lambda p: p.stat().st_mtime, reverse=True)
    if not candidates:
        raise RVUFormatError(
            f"No {pattern} found in {data_dir}/. Run 'python scripts/fetch_rvu.py' first "
            "(the RVU file is not committed: it ships with AMA-copyrighted descriptors)."
        )
    return candidates[0]


def load_rvu_table(data_dir: str | Path = "data", year: int | None = None) -> RVUTable:
    path = find_rvu_file(data_dir, year)
    return parse_pprrvu(path, year=year)
