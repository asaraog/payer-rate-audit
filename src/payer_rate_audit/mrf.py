"""Hospital price transparency machine-readable file (MRF) parsing.

Coded against the CMS Hospital Price Transparency template v3.0 data dictionary
(CY2026 OPPS/ASC final rule, effective 2026-01-01, enforcement 2026-04-01),
published at https://github.com/CMSgov/hospital-price-transparency.

CMS permits three shapes and hospitals pick one without announcing it, so the
shape is detected from the file rather than declared by the caller:

* CSV "tall" - one row per item per payer per plan; identified by a ``payer_name``
  column on the header row.
* CSV "wide" - one row per item; payer-specific columns are named
  ``standard_charge|<payer>|<plan>|negotiated_dollar``.
* JSON - ``standard_charge_information[]``, each entry carrying ``code_information[]``
  and ``standard_charges[]`` with nested ``payers_information[]``.

All three normalize to :data:`NORMALIZED_COLUMNS`.
"""

from __future__ import annotations

import csv
import json
import re
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

import pandas as pd

NORMALIZED_COLUMNS = [
    "code",
    "code_type",
    "modifiers",
    "description",
    "billing_class",
    "setting",
    "payer_name",
    "plan_name",
    "negotiated_dollar",
    "gross_charge",
    "discounted_cash",
    "min_negotiated",
    "max_negotiated",
    "methodology",
]

# Valid "Code Type(s)" values, v3.0 data dictionary. Only CPT and HCPCS can join
# to the PFS relative value file; the rest are carried through and counted.
VALID_CODE_TYPES = frozenset(
    {
        "CPT",
        "HCPCS",
        "NDC",
        "RC",
        "ICD",
        "DRG",
        "MS-DRG",
        "R-DRG",
        "S-DRG",
        "APS-DRG",
        "AP-DRG",
        "APR-DRG",
        "TRIS-DRG",
        "APC",
        "LOCAL",
        "EAPG",
        "HIPPS",
        "CDT",
        "CDM",
        "CMG",
        "MS-LTC-DRG",
    }
)

# When an item carries several codes (a revenue code plus a CPT, say), one
# normalized row per code would multiply the same negotiated dollars across
# codes and corrupt every sum. One code is chosen per item, most joinable first.
# See ASSUMPTIONS.md, decision 7.
CODE_TYPE_PREFERENCE = ("CPT", "HCPCS", "APC", "MS-DRG", "DRG", "EAPG", "RC", "NDC")

_CSV_SNIFF_ROWS = 20


class MRFShape(StrEnum):
    CSV_TALL = "csv_tall"
    CSV_WIDE = "csv_wide"
    JSON = "json"


class MRFFormatError(ValueError):
    """Raised when a file does not look like any CMS-permitted MRF shape."""


@dataclass
class ParseResult:
    """A normalized MRF plus the counts needed to state a denominator."""

    shape: MRFShape
    frame: pd.DataFrame
    source_file: str
    metadata: dict[str, str] = field(default_factory=dict)
    total_charge_records: int = 0
    excluded_no_dollar: int = 0
    excluded_no_code: int = 0
    skipped_modifier_only_items: int = 0

    @property
    def row_count(self) -> int:
        return len(self.frame)

    def summary(self) -> dict[str, Any]:
        return {
            "shape": self.shape.value,
            "source_file": self.source_file,
            "normalized_rows": self.row_count,
            "charge_records_read": self.total_charge_records,
            "excluded_percentage_or_algorithm_only": self.excluded_no_dollar,
            "excluded_no_usable_code": self.excluded_no_code,
            "skipped_modifier_only_items": self.skipped_modifier_only_items,
        }


def normalize_header(header: str) -> str:
    """Lower-case a header and strip whitespace around pipe separators.

    The data dictionary states that inadvertent spaces around pipes and mixed
    case are both valid, so ``standard_charge | Aetna | PPO | negotiated_dollar``
    and ``standard_charge|aetna|ppo|negotiated_dollar`` are the same column.
    """
    return re.sub(r"\s*\|\s*", "|", str(header).replace("\ufeff", "").strip()).lower()


def _to_float(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value) if pd.notna(value) else None
    text = str(value).strip().replace("$", "").replace(",", "")
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _clean(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and pd.isna(value):
        return ""
    return str(value).strip()


def _normalize_modifiers(value: Any) -> str:
    """Normalize a modifier field to a comma-separated upper-case string.

    Hospitals separate multiple modifiers with commas, pipes, semicolons or
    spaces; JSON files use an array.
    """
    if isinstance(value, (list, tuple)):
        parts = [_clean(part) for part in value]
    else:
        parts = re.split(r"[,;|\s]+", _clean(value))
    return ",".join(part.upper() for part in parts if part)


def _pick_code(codes: list[tuple[str, str]]) -> tuple[str, str] | None:
    """Choose one (code, code_type) pair for an item. See CODE_TYPE_PREFERENCE."""
    usable = [(code, code_type) for code, code_type in codes if code and code_type]
    if not usable:
        return None
    for preferred in CODE_TYPE_PREFERENCE:
        for code, code_type in usable:
            if code_type == preferred:
                return code, code_type
    return usable[0]


def detect_shape(path: str | Path) -> MRFShape:
    """Detect which of the three CMS-permitted shapes a file uses."""
    path = Path(path)
    with path.open("r", encoding="utf-8-sig", errors="replace") as handle:
        head = handle.read(4096).lstrip()
    if head.startswith("{") or head.startswith("["):
        return MRFShape.JSON

    header_row, _ = _find_csv_header(path)
    headers = {normalize_header(cell) for cell in header_row}
    wide = any(
        re.fullmatch(r"standard_charge\|.+\|negotiated_(dollar|percentage|algorithm)", header)
        and header.count("|") >= 3
        for header in headers
    )
    if wide:
        return MRFShape.CSV_WIDE
    if "payer_name" in headers:
        return MRFShape.CSV_TALL
    raise MRFFormatError(
        f"{path}: CSV has neither a 'payer_name' column (tall shape) nor any "
        "'standard_charge|<payer>|<plan>|negotiated_dollar' column (wide shape). "
        f"Header row seen: {sorted(headers)[:12]}"
    )


def _find_csv_header(path: Path) -> tuple[list[str], int]:
    """Return the item-level header row and its zero-based index.

    CMS puts hospital-level metadata on rows 1-2 and the item header on row 3,
    but files in the wild sometimes omit the metadata rows, so the header is
    located by content: the row carrying ``description`` and a ``code|<i>`` column.
    """
    with path.open("r", encoding="utf-8-sig", errors="replace", newline="") as handle:
        reader = csv.reader(handle)
        for index, row in enumerate(reader):
            if index >= _CSV_SNIFF_ROWS:
                break
            headers = {normalize_header(cell) for cell in row}
            if "description" in headers and any(
                re.fullmatch(r"code\|\d+", header) for header in headers
            ):
                return row, index
    raise MRFFormatError(
        f"{path}: no MRF item header row found in the first {_CSV_SNIFF_ROWS} rows. "
        "Expected a row containing 'description' and 'code | 1'."
    )


def _read_csv_metadata(path: Path, header_index: int) -> dict[str, str]:
    """Read the hospital-level metadata that CMS places above the item header."""
    if header_index < 2:
        return {}
    with path.open("r", encoding="utf-8-sig", errors="replace", newline="") as handle:
        rows = list(csv.reader(handle))[:header_index]
    if len(rows) < 2:
        return {}
    keys = [normalize_header(cell) for cell in rows[0]]
    values = rows[1]
    wanted = {"hospital_name", "last_updated_on", "version", "location_name"}
    return {
        key: values[i].strip()
        for i, key in enumerate(keys)
        if key in wanted and i < len(values) and values[i].strip()
    }


def _csv_code_columns(headers: list[str]) -> list[tuple[int, int]]:
    """Return (code column index, code type column index) pairs, ordered by ``i``."""
    codes: dict[str, int] = {}
    types: dict[str, int] = {}
    for index, header in enumerate(headers):
        code_match = re.fullmatch(r"code\|(\d+)", header)
        type_match = re.fullmatch(r"code\|(\d+)\|type", header)
        if code_match:
            codes[code_match.group(1)] = index
        elif type_match:
            types[type_match.group(1)] = index
    return [
        (codes[key], types[key])
        for key in sorted(codes, key=lambda value: int(value))
        if key in types
    ]


def _base_record(row: list[str], headers: list[str], code_columns: list[tuple[int, int]]):
    """Extract the item-level fields shared by the tall and wide CSV shapes."""
    index_of = {header: i for i, header in enumerate(headers)}

    def cell(header: str) -> str:
        position = index_of.get(header)
        return _clean(row[position]) if position is not None and position < len(row) else ""

    codes = [
        (
            _clean(row[code_i]).upper() if code_i < len(row) else "",
            _clean(row[type_i]).upper() if type_i < len(row) else "",
        )
        for code_i, type_i in code_columns
    ]
    picked = _pick_code(codes)
    return picked, {
        "description": cell("description"),
        "modifiers": _normalize_modifiers(cell("modifiers")),
        "billing_class": cell("billing_class").lower(),
        "setting": cell("setting").lower(),
        "gross_charge": _to_float(cell("standard_charge|gross")),
        "discounted_cash": _to_float(cell("standard_charge|discounted_cash")),
        "min_negotiated": _to_float(cell("standard_charge|min")),
        "max_negotiated": _to_float(cell("standard_charge|max")),
    }


def parse_csv_tall(path: str | Path) -> ParseResult:
    path = Path(path)
    header_row, header_index = _find_csv_header(path)
    headers = [normalize_header(cell) for cell in header_row]
    code_columns = _csv_code_columns(headers)
    if not code_columns:
        raise MRFFormatError(f"{path}: no 'code | <i>' / 'code | <i> | type' column pairs found.")
    index_of = {header: i for i, header in enumerate(headers)}

    records: list[dict[str, Any]] = []
    total = excluded_no_dollar = excluded_no_code = 0
    with path.open("r", encoding="utf-8-sig", errors="replace", newline="") as handle:
        reader = csv.reader(handle)
        for _ in range(header_index + 1):
            next(reader, None)
        for row in reader:
            if not any(_clean(cell) for cell in row):
                continue
            total += 1

            def cell(header: str, current_row: list[str] = row) -> str:
                position = index_of.get(header)
                if position is None or position >= len(current_row):
                    return ""
                return _clean(current_row[position])

            dollar = _to_float(cell("standard_charge|negotiated_dollar"))
            if dollar is None:
                if cell("standard_charge|negotiated_percentage") or cell(
                    "standard_charge|negotiated_algorithm"
                ):
                    excluded_no_dollar += 1
                continue
            picked, base = _base_record(row, headers, code_columns)
            if picked is None:
                excluded_no_code += 1
                continue
            code, code_type = picked
            records.append(
                {
                    **base,
                    "code": code,
                    "code_type": code_type,
                    "payer_name": cell("payer_name"),
                    "plan_name": cell("plan_name"),
                    "negotiated_dollar": dollar,
                    "methodology": cell("standard_charge|methodology").lower(),
                }
            )

    return ParseResult(
        shape=MRFShape.CSV_TALL,
        frame=_to_frame(records),
        source_file=path.name,
        metadata=_read_csv_metadata(path, header_index),
        total_charge_records=total,
        excluded_no_dollar=excluded_no_dollar,
        excluded_no_code=excluded_no_code,
    )


def _wide_payer_columns(
    headers: list[str], raw_headers: list[str]
) -> dict[tuple[str, str], dict[str, int]]:
    """Map each (payer, plan) pair to its column indices in a wide CSV.

    Payer and plan names sit between fixed prefix and suffix tokens. A name
    containing a pipe would be ambiguous, so the first segment is taken as the
    payer and the remainder as the plan. Matching is done on the normalized
    header but the names themselves are read back out of the raw header, so
    payers keep the capitalization the hospital published.
    """
    columns: dict[tuple[str, str], dict[str, int]] = {}
    patterns = {
        "negotiated_dollar": r"standard_charge\|(.+)\|negotiated_dollar",
        "negotiated_percentage": r"standard_charge\|(.+)\|negotiated_percentage",
        "negotiated_algorithm": r"standard_charge\|(.+)\|negotiated_algorithm",
        "methodology": r"standard_charge\|(.+)\|methodology",
    }
    for index, header in enumerate(headers):
        for field_name, pattern in patterns.items():
            match = re.fullmatch(pattern, header)
            if not match:
                continue
            if "|" not in match.group(1):
                continue  # tall-shape column such as standard_charge|negotiated_dollar
            parts = [part.strip() for part in raw_headers[index].split("|")]
            payer, plan = parts[1], "|".join(parts[2:-1])
            columns.setdefault((payer, plan), {})[field_name] = index
    return columns


def parse_csv_wide(path: str | Path) -> ParseResult:
    path = Path(path)
    header_row, header_index = _find_csv_header(path)
    headers = [normalize_header(cell) for cell in header_row]
    code_columns = _csv_code_columns(headers)
    if not code_columns:
        raise MRFFormatError(f"{path}: no 'code | <i>' / 'code | <i> | type' column pairs found.")
    payer_columns = _wide_payer_columns(headers, header_row)
    if not payer_columns:
        raise MRFFormatError(
            f"{path}: wide shape detected but no payer-specific columns could be parsed."
        )

    records: list[dict[str, Any]] = []
    total = excluded_no_dollar = excluded_no_code = 0
    with path.open("r", encoding="utf-8-sig", errors="replace", newline="") as handle:
        reader = csv.reader(handle)
        for _ in range(header_index + 1):
            next(reader, None)
        for row in reader:
            if not any(_clean(cell) for cell in row):
                continue
            picked, base = _base_record(row, headers, code_columns)
            for (payer, plan), positions in payer_columns.items():

                def value(
                    field_name: str,
                    current_row: list[str] = row,
                    current_positions: dict[str, int] = positions,
                ) -> str:
                    position = current_positions.get(field_name)
                    if position is None or position >= len(current_row):
                        return ""
                    return _clean(current_row[position])

                dollar_text = value("negotiated_dollar")
                percentage = value("negotiated_percentage")
                algorithm = value("negotiated_algorithm")
                if not (dollar_text or percentage or algorithm):
                    continue  # this payer simply has no charge for this item
                total += 1
                dollar = _to_float(dollar_text)
                if dollar is None:
                    excluded_no_dollar += 1
                    continue
                if picked is None:
                    excluded_no_code += 1
                    continue
                code, code_type = picked
                records.append(
                    {
                        **base,
                        "code": code,
                        "code_type": code_type,
                        "payer_name": payer,
                        "plan_name": plan,
                        "negotiated_dollar": dollar,
                        "methodology": value("methodology").lower(),
                    }
                )

    return ParseResult(
        shape=MRFShape.CSV_WIDE,
        frame=_to_frame(records),
        source_file=path.name,
        metadata=_read_csv_metadata(path, header_index),
        total_charge_records=total,
        excluded_no_dollar=excluded_no_dollar,
        excluded_no_code=excluded_no_code,
    )


def parse_json(path: str | Path) -> ParseResult:
    path = Path(path)
    with path.open("r", encoding="utf-8-sig", errors="replace") as handle:
        document = json.load(handle)
    if not isinstance(document, dict) or "standard_charge_information" not in document:
        raise MRFFormatError(
            f"{path}: JSON MRF must be an object with a 'standard_charge_information' array."
        )

    records: list[dict[str, Any]] = []
    total = excluded_no_dollar = excluded_no_code = 0
    for item in document.get("standard_charge_information") or []:
        codes = [
            (_clean(entry.get("code")).upper(), _clean(entry.get("type")).upper())
            for entry in item.get("code_information") or []
        ]
        picked = _pick_code(codes)
        description = _clean(item.get("description"))
        for charge in item.get("standard_charges") or []:
            shared = {
                "description": description,
                "modifiers": _normalize_modifiers(charge.get("modifier_code")),
                "billing_class": _clean(charge.get("billing_class")).lower(),
                "setting": _clean(charge.get("setting")).lower(),
                "gross_charge": _to_float(charge.get("gross_charge")),
                "discounted_cash": _to_float(charge.get("discounted_cash")),
                "min_negotiated": _to_float(charge.get("minimum")),
                "max_negotiated": _to_float(charge.get("maximum")),
            }
            for payer in charge.get("payers_information") or []:
                total += 1
                dollar = _to_float(payer.get("standard_charge_dollar"))
                if dollar is None:
                    if (
                        payer.get("standard_charge_percentage") is not None
                        or payer.get("standard_charge_algorithm") is not None
                    ):
                        excluded_no_dollar += 1
                    continue
                if picked is None:
                    excluded_no_code += 1
                    continue
                code, code_type = picked
                records.append(
                    {
                        **shared,
                        "code": code,
                        "code_type": code_type,
                        "payer_name": _clean(payer.get("payer_name")),
                        "plan_name": _clean(payer.get("plan_name")),
                        "negotiated_dollar": dollar,
                        "methodology": _clean(payer.get("methodology")).lower(),
                    }
                )

    metadata = {
        key: _clean(document.get(key))
        for key in ("hospital_name", "last_updated_on", "version")
        if document.get(key)
    }
    # modifier_information entries describe modifier pricing rules without an
    # item or a dollar amount; they carry no negotiated rate to aggregate.
    modifier_only = len(document.get("modifier_information") or [])
    return ParseResult(
        shape=MRFShape.JSON,
        frame=_to_frame(records),
        source_file=path.name,
        metadata=metadata,
        total_charge_records=total,
        excluded_no_dollar=excluded_no_dollar,
        excluded_no_code=excluded_no_code,
        skipped_modifier_only_items=modifier_only,
    )


def _to_frame(records: list[dict[str, Any]]) -> pd.DataFrame:
    frame = pd.DataFrame(records, columns=NORMALIZED_COLUMNS)
    for column in ("negotiated_dollar", "gross_charge", "discounted_cash",
                   "min_negotiated", "max_negotiated"):
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    for column in ("code", "code_type", "modifiers", "description", "billing_class",
                   "setting", "payer_name", "plan_name", "methodology"):
        frame[column] = frame[column].fillna("").astype(str)
    return frame


_PARSERS = {
    MRFShape.CSV_TALL: parse_csv_tall,
    MRFShape.CSV_WIDE: parse_csv_wide,
    MRFShape.JSON: parse_json,
}


def parse_mrf(path: str | Path) -> ParseResult:
    """Detect the shape of an MRF and parse it into the normalized schema."""
    path = Path(path)
    if not path.is_file():
        raise MRFFormatError(f"MRF not found: {path}")
    return _PARSERS[detect_shape(path)](path)
