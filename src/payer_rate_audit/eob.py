"""CARIN Blue Button FHIR R4 ExplanationOfBenefit parsing, and repricing.

Milestone 7. The rate audit answers "what does each payer pay per RVU". This
module answers the follow-on question a practice actually asks: "given what we
actually did last year, what would each payer have paid, and how does that
compare to what we were paid?"

Input is either a directory of FHIR bundles / EOB resources (``*.json``,
``*.ndjson``) or a single NDJSON file. Line items whose ``productOrService``
carries no usable HCPCS or CPT coding are counted and reported, never dropped
in silence.

Amount extraction follows the CARIN Blue Button adjudication slices and the
Blue Button 2.0 variable code systems, because production files use both:
``submitted`` / ``eligible`` / ``benefit`` from the HL7 adjudication code
system, plus the CMS ``line_sbmtd_chrg_amt`` / ``line_alowd_chrg_amt`` /
``line_prvdr_pmt_amt`` variables.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pandas as pd

from .metrics import JoinResult

EOB_COLUMNS = [
    "eob_id",
    "source_file",
    "payer_name",
    "patient_id",
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

# productOrService coding systems, mapped to the code_type this tool joins on.
_CPT_SYSTEMS = ("ama-assn.org/go/cpt", "codesystem/cpt", "/sid/cpt")
_HCPCS_SYSTEMS = (
    "hcpcs",
    "codesystem/hcpcs",
    "hcpcsreleasecodesets",
    "bluebutton.cms.gov/resources/codesystem/hcpcs",
)

# adjudication.category codes, by the amount they carry. CARIN uses the HL7
# adjudication code system; Blue Button 2.0 uses CMS claim-variable codes.
_AMOUNT_CATEGORIES = {
    "submitted_amount": {
        "submitted",
        "submittedamount",
        "line_sbmtd_chrg_amt",
        "line_submitted_charge_amount",
    },
    "allowed_amount": {
        "eligible",
        "allowed",
        "allowedamount",
        "line_alowd_chrg_amt",
        "line_allowed_charge_amount",
    },
    "paid_amount": {
        "benefit",
        "paid",
        "paidtoprovider",
        "line_prvdr_pmt_amt",
        "line_provider_payment_amount",
    },
}


class EOBFormatError(ValueError):
    """Raised when a file contains no FHIR resource this module can read."""


@dataclass
class EOBParseResult:
    """Parsed EOB line items plus the counts needed to state a denominator."""

    frame: pd.DataFrame
    source: str
    files_read: int = 0
    resources_read: int = 0
    line_items_read: int = 0
    line_items_without_code: int = 0
    non_eob_resources: int = 0
    unreadable_files: list[str] = field(default_factory=list)

    @property
    def row_count(self) -> int:
        return len(self.frame)


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _classify_coding(coding: dict[str, Any]) -> tuple[str, str] | None:
    """Return (code, code_type) if this coding is a CPT or HCPCS code."""
    code = _text(coding.get("code")).upper()
    if not code:
        return None
    system = _text(coding.get("system")).lower()
    if any(marker in system for marker in _CPT_SYSTEMS):
        return code, "CPT"
    if any(marker in system for marker in _HCPCS_SYSTEMS):
        return code, "HCPCS"
    # No recognizable system: fall back to the shape of the code itself. CPT is
    # five digits; HCPCS Level II is a letter plus four digits.
    if not system:
        if code.isdigit() and len(code) == 5:
            return code, "CPT"
        if len(code) == 5 and code[0].isalpha() and code[1:].isdigit():
            return code, "HCPCS"
    return None


def _pick_code(concept: dict[str, Any] | None) -> tuple[str, str] | None:
    for coding in (concept or {}).get("coding", []) or []:
        picked = _classify_coding(coding)
        if picked and picked[1] == "CPT":
            return picked
    for coding in (concept or {}).get("coding", []) or []:
        picked = _classify_coding(coding)
        if picked:
            return picked
    return None


def _modifiers(item: dict[str, Any]) -> str:
    codes: list[str] = []
    for concept in item.get("modifier", []) or []:
        for coding in concept.get("coding", []) or []:
            code = _text(coding.get("code")).upper()
            if code:
                codes.append(code)
    return ",".join(codes)


def _serviced_date(item: dict[str, Any], resource: dict[str, Any]) -> str:
    if item.get("servicedDate"):
        return _text(item["servicedDate"])
    period = item.get("servicedPeriod") or {}
    if period.get("start"):
        return _text(period["start"])
    billable = resource.get("billablePeriod") or {}
    return _text(billable.get("start"))


def _amounts(item: dict[str, Any]) -> dict[str, float | None]:
    found: dict[str, float | None] = {key: None for key in _AMOUNT_CATEGORIES}
    for entry in item.get("adjudication", []) or []:
        amount = (entry.get("amount") or {}).get("value")
        if amount is None:
            continue
        codes = {
            _text(coding.get("code")).lower()
            for coding in (entry.get("category") or {}).get("coding", []) or []
        }
        for key, wanted in _AMOUNT_CATEGORIES.items():
            if found[key] is None and codes & wanted:
                found[key] = float(amount)
    return found


def _payer_name(resource: dict[str, Any]) -> str:
    insurer = resource.get("insurer") or {}
    name = _text(insurer.get("display"))
    if name:
        return name
    for coverage in resource.get("insurance", []) or []:
        name = _text((coverage.get("coverage") or {}).get("display"))
        if name:
            return name
    return _text(insurer.get("reference"))


def _iter_resources(payload: Any) -> Iterator[dict[str, Any]]:
    """Yield resources from a bundle, a bare resource, or a list of either."""
    if isinstance(payload, list):
        for entry in payload:
            yield from _iter_resources(entry)
        return
    if not isinstance(payload, dict):
        return
    if payload.get("resourceType") == "Bundle":
        for entry in payload.get("entry", []) or []:
            yield from _iter_resources(entry.get("resource"))
        return
    if payload.get("resourceType"):
        yield payload


def _iter_payloads(path: Path) -> Iterator[Any]:
    if path.suffix.lower() == ".ndjson":
        with path.open("r", encoding="utf-8-sig") as handle:
            for line in handle:
                line = line.strip()
                if line:
                    yield json.loads(line)
        return
    with path.open("r", encoding="utf-8-sig") as handle:
        yield json.load(handle)


def _input_files(path: Path) -> list[Path]:
    if path.is_file():
        return [path]
    files = sorted(
        candidate
        for candidate in path.rglob("*")
        if candidate.is_file() and candidate.suffix.lower() in {".json", ".ndjson"}
    )
    if not files:
        raise EOBFormatError(f"{path}: no .json or .ndjson files found")
    return files


def parse_eob(path: str | Path) -> EOBParseResult:
    """Parse a directory of EOB bundles, or a single JSON/NDJSON file."""
    root = Path(path)
    if not root.exists():
        raise EOBFormatError(f"{root}: no such file or directory")

    records: list[dict[str, Any]] = []
    result = EOBParseResult(frame=pd.DataFrame(columns=EOB_COLUMNS), source=str(root))

    for file_path in _input_files(root):
        try:
            payloads = list(_iter_payloads(file_path))
        except (json.JSONDecodeError, UnicodeDecodeError):
            result.unreadable_files.append(str(file_path))
            continue
        result.files_read += 1
        for payload in payloads:
            for resource in _iter_resources(payload):
                result.resources_read += 1
                if resource.get("resourceType") != "ExplanationOfBenefit":
                    result.non_eob_resources += 1
                    continue
                payer = _payer_name(resource)
                patient = _text((resource.get("patient") or {}).get("reference"))
                for item in resource.get("item", []) or []:
                    result.line_items_read += 1
                    picked = _pick_code(item.get("productOrService"))
                    if picked is None:
                        result.line_items_without_code += 1
                        continue
                    code, code_type = picked
                    amounts = _amounts(item)
                    quantity = (item.get("quantity") or {}).get("value")
                    records.append(
                        {
                            "eob_id": _text(resource.get("id")),
                            "source_file": file_path.name,
                            "payer_name": payer,
                            "patient_id": patient,
                            "line": item.get("sequence"),
                            "code": code,
                            "code_type": code_type,
                            "modifiers": _modifiers(item),
                            "serviced_date": _serviced_date(item, resource),
                            "units": float(quantity) if quantity is not None else 1.0,
                            **amounts,
                        }
                    )

    if result.files_read == 0:
        raise EOBFormatError(f"{root}: no readable JSON found ({len(result.unreadable_files)} bad)")

    frame = pd.DataFrame(records, columns=EOB_COLUMNS)
    for column in ("units", "submitted_amount", "allowed_amount", "paid_amount"):
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    result.frame = frame
    return result


@dataclass
class EOBAudit:
    """Everything the report needs about the utilization layer."""

    parse: EOBParseResult
    utilization: pd.DataFrame
    repriced: pd.DataFrame


def utilization(parse: EOBParseResult) -> pd.DataFrame:
    """Observed service mix: units and actual dollars per code."""
    frame = parse.frame
    if frame.empty:
        return pd.DataFrame(columns=["code", "code_type", "line_items", "units", "actual_paid"])
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


def reprice(parse: EOBParseResult, join: JoinResult, group_by: str = "plan") -> pd.DataFrame:
    """Reprice the observed service mix at each payer's contracted rate.

    The rate used for a code is the mean negotiated dollar across that payer's
    rows for the code, so a payer with several rows for one code (different
    settings, modifiers) does not get counted several times. Codes the payer
    does not price are excluded and counted per payer, because a payer cannot
    be charged with a rate it never published.
    """
    keys = ["payer_name", "plan_name"] if group_by == "plan" else ["payer_name"]
    volume = utilization(parse)
    empty = pd.DataFrame(
        columns=[
            *keys,
            "codes_priced",
            "codes_unpriced",
            "units_priced",
            "actual_paid",
            "repriced",
            "delta",
            "ratio",
        ]
    )
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
    merged["matched"] = merged["rate"].notna()
    matched = merged[merged["matched"]].copy()
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
    columns = [
        *keys,
        "codes_priced",
        "codes_unpriced",
        "units_priced",
        "actual_paid",
        "repriced",
        "delta",
        "ratio",
    ]
    return table[columns].sort_values("repriced", ascending=False).reset_index(drop=True)


def eob_audit(path: str | Path, join: JoinResult, group_by: str = "plan") -> EOBAudit:
    """Parse EOBs and reprice the observed mix at each payer's contracted rates."""
    parse = parse_eob(path)
    return EOBAudit(
        parse=parse,
        utilization=utilization(parse),
        repriced=reprice(parse, join, group_by=group_by),
    )
