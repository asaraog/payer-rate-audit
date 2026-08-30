"""CARIN Blue Button FHIR R4 ExplanationOfBenefit parsing, and repricing.

Milestone 7. The rate audit answers "what does each payer pay per RVU". This
module answers the follow-on question a practice actually asks: "given what we
actually did last year, what would each payer have paid, and how does that
compare to what we were paid?"

Input is either a directory of FHIR bundles / EOB resources (``*.json``,
``*.ndjson``) or a single NDJSON file. Line items whose ``productOrService``
carries no usable HCPCS or CPT coding are counted and reported, never dropped
in silence.

Line items normalize to the shared table in :mod:`utilization`, which is what
actually reprices them; this module only knows FHIR.

Amount extraction follows the CARIN Blue Button adjudication slices and the
Blue Button 2.0 variable code systems, because production files use both:
``submitted`` / ``eligible`` / ``benefit`` from the HL7 adjudication code
system, plus the CMS ``line_sbmtd_chrg_amt`` / ``line_alowd_chrg_amt`` /
``line_prvdr_pmt_amt`` variables.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .metrics import JoinResult
from .utilization import (
    LINE_COLUMNS,
    LineFormatError,
    LineSource,
    UtilizationAudit,
    audit_lines,
    empty_frame,
    input_files,
    reprice,
    to_frame,
    utilization,
)

__all__ = [
    "EOB_COLUMNS",
    "EOBAudit",
    "EOBFormatError",
    "EOBParseResult",
    "eob_audit",
    "parse_eob",
    "reprice",
    "utilization",
]

EOB_COLUMNS = LINE_COLUMNS
KIND = "FHIR ExplanationOfBenefit"
NON_EOB_RESOURCES = "non-EOB FHIR resources skipped"
_SUFFIXES = {".json", ".ndjson"}

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


class EOBFormatError(LineFormatError):
    """Raised when a file contains no FHIR resource this module can read."""


@dataclass
class EOBParseResult(LineSource):
    """Parsed EOB line items, in FHIR's vocabulary."""

    @property
    def resources_read(self) -> int:
        return self.records_read

    @property
    def non_eob_resources(self) -> int:
        return self.exclusions.get(NON_EOB_RESOURCES, 0)


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


def parse_eob(path: str | Path) -> EOBParseResult:
    """Parse a directory of EOB bundles, or a single JSON/NDJSON file."""
    root = Path(path)
    records: list[dict[str, Any]] = []
    result = EOBParseResult(frame=empty_frame(), source=str(root), kind=KIND)

    try:
        files = input_files(root, _SUFFIXES)
    except LineFormatError as error:
        raise EOBFormatError(str(error)) from error

    for file_path in files:
        try:
            payloads = list(_iter_payloads(file_path))
        except (json.JSONDecodeError, UnicodeDecodeError):
            result.unreadable_files.append(str(file_path))
            continue
        result.files_read += 1
        for payload in payloads:
            for resource in _iter_resources(payload):
                result.records_read += 1
                if resource.get("resourceType") != "ExplanationOfBenefit":
                    result.exclude(NON_EOB_RESOURCES)
                    continue
                payer = _payer_name(resource)
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
                            "source_file": file_path.name,
                            "payer_name": payer,
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

    result.frame = to_frame(records)
    return result


EOBAudit = UtilizationAudit


def eob_audit(path: str | Path, join: JoinResult, group_by: str = "plan") -> UtilizationAudit:
    """Parse EOBs and reprice the observed mix at each payer's contracted rates."""
    return audit_lines(parse_eob(path), join, group_by=group_by)
