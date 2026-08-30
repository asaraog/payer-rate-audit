"""X12 5010 835 remittance advice: the file a practice actually has today.

An 835 is what the clearinghouse drops in the practice's mailbox for every
payment, so it is the whole electronic book of business without an integration
project. This module reads service lines out of it and hands them to the shared
:mod:`utilization` layer, which reprices them exactly as it reprices FHIR
``ExplanationOfBenefit`` lines.

Scope is deliberately the service line. Claim-level ``CAS`` adjustments,
``PLB`` provider-level adjustments, corrections/COB logic, and any service line
whose procedure qualifier is not ``HC`` are out of scope -- and, like every
other skip in this tool, they are counted and reported rather than dropped in
silence.

PHI: one 835 holds many patients' claims. This parser never reads patient names
or identifiers (``NM1*QC``, ``CLP01``) into its output, so no identifier can
reach a frame, a CSV, a report, or an error message.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .metrics import JoinResult
from .utilization import (
    LineFormatError,
    LineSource,
    UtilizationAudit,
    audit_lines,
    empty_frame,
    input_files,
    to_frame,
)

KIND = "X12 835 remittance advice"

NON_HC_QUALIFIER = "835 service lines with a non-HCPCS procedure qualifier"
NO_CODE = "835 service lines with no procedure code"
REVERSALS = "835 reversal/negative service lines netted against charges"
NON_835_FILES = "input files that are not X12 835 remittances"

_ISA_LENGTH = 106
_CPT_QUALIFIERS = {"HC"}


class ERAFormatError(LineFormatError):
    """Raised when an input path holds no readable X12 835."""


@dataclass
class Delimiters:
    """The three delimiters an 835 declares in its own ISA segment."""

    element: str
    composite: str
    segment: str


@dataclass
class ERAParseResult(LineSource):
    """Parsed 835 service lines, in remittance vocabulary."""

    claims_read: int = 0
    reversal_lines: int = 0

    @property
    def transactions_read(self) -> int:
        return self.records_read


@dataclass
class _Claim:
    """Claim-level context a service line inherits. No identifiers here."""

    payer_name: str = ""
    status: str = ""
    service_date: str = ""
    lines: int = 0


@dataclass
class _Envelope:
    """One functional group's worth of state while walking segments."""

    payer_name: str = ""
    claim: _Claim = field(default_factory=_Claim)


def read_delimiters(text: str) -> Delimiters:
    """Read the element, composite, and segment delimiters from the ISA.

    ISA is the one fixed-width segment in X12: the element delimiter is the
    fourth character, and the last two characters of the 106-character segment
    are the composite and segment delimiters. Nothing here is hardcoded --
    files from different clearinghouses genuinely differ.
    """
    if not text.startswith("ISA"):
        raise ERAFormatError("not an X12 interchange: file does not start with ISA")
    if len(text) < _ISA_LENGTH:
        raise ERAFormatError("truncated ISA segment")
    element = text[3]
    composite = text[_ISA_LENGTH - 2]
    segment = text[_ISA_LENGTH - 1]
    return Delimiters(element=element, composite=composite, segment=segment)


def split_segments(text: str, delimiters: Delimiters) -> list[list[str]]:
    """Split an interchange into segments of elements, ignoring line breaks."""
    segments: list[list[str]] = []
    for raw in text.split(delimiters.segment):
        stripped = raw.strip("\r\n \t")
        if stripped:
            segments.append(stripped.split(delimiters.element))
    return segments


def _element(segment: list[str], index: int) -> str:
    return segment[index].strip() if index < len(segment) else ""


def _amount(value: str) -> float:
    try:
        return float(value)
    except ValueError:
        return 0.0


def _units(value: str) -> float:
    """SVC05 units, defaulting to 1 when absent -- never to 0."""
    if not value:
        return 1.0
    try:
        parsed = float(value)
    except ValueError:
        return 1.0
    return parsed if parsed else 1.0


def _service_date(value: str) -> str:
    """DTM dates are CCYYMMDD; render them ISO so both adapters agree."""
    digits = value.strip()
    if len(digits) == 8 and digits.isdigit():
        return f"{digits[0:4]}-{digits[4:6]}-{digits[6:8]}"
    return digits


def _code_type(code: str) -> str:
    """CPT is five digits; HCPCS Level II is a letter plus four digits."""
    if code.isdigit() and len(code) == 5:
        return "CPT"
    return "HCPCS"


def _svc_composite(value: str, delimiters: Delimiters) -> tuple[str, str, str]:
    """Split SVC01 (``HC:97110:GP``) into qualifier, code, and modifiers."""
    parts = value.split(delimiters.composite)
    qualifier = parts[0].strip().upper() if parts else ""
    code = parts[1].strip().upper() if len(parts) > 1 else ""
    modifiers = [part.strip().upper() for part in parts[2:6] if part.strip()]
    return qualifier, code, ",".join(modifiers)


def is_era(path: Path) -> bool:
    """Sniff for an ISA header rather than trusting the file extension.

    Clearinghouses hand out ``.835``, ``.txt``, ``.edi``, ``.rmt``, ``.dat``
    and extensionless files interchangeably, so the extension is not evidence.
    """
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            return handle.read(3) == "ISA"
    except OSError:
        return False


def parse_era(path: str | Path) -> ERAParseResult:
    """Parse a directory of 835 files (recursively), or a single 835 file."""
    root = Path(path)
    records: list[dict[str, object]] = []
    result = ERAParseResult(frame=empty_frame(), source=str(root), kind=KIND)

    try:
        candidates = input_files(root)
    except LineFormatError as error:
        raise ERAFormatError(str(error)) from error

    for file_path in candidates:
        if not is_era(file_path):
            result.exclude(NON_835_FILES)
            continue
        try:
            text = file_path.read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            result.unreadable_files.append(str(file_path))
            continue
        try:
            delimiters = read_delimiters(text)
        except ERAFormatError:
            result.unreadable_files.append(str(file_path))
            continue
        result.files_read += 1
        records.extend(_read_interchange(text, delimiters, file_path.name, result))

    if result.files_read == 0:
        raise ERAFormatError(f"{root}: no X12 835 files found")

    result.frame = to_frame(records)
    result.reversal_lines = result.exclusions.get(REVERSALS, 0)
    return result


def _read_interchange(
    text: str,
    delimiters: Delimiters,
    source_file: str,
    result: ERAParseResult,
) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    envelope = _Envelope()
    line_number = 0
    current: dict[str, object] | None = None

    for segment in split_segments(text, delimiters):
        tag = segment[0].strip().upper()

        if tag == "ST":
            result.records_read += 1
            envelope = _Envelope(payer_name=envelope.payer_name)
        elif tag == "N1" and _element(segment, 1).upper() == "PR":
            envelope.payer_name = _element(segment, 2)
        elif tag == "CLP":
            # CLP01 is the patient account number: PHI, so it is never read.
            result.claims_read += 1
            current = None
            envelope.claim = _Claim(
                payer_name=envelope.payer_name,
                status=_element(segment, 2),
            )
        elif tag == "DTM" and _element(segment, 1) in {"232", "472"}:
            date = _service_date(_element(segment, 2))
            # A DTM*472 after a service line dates that line; anything earlier
            # is the claim-level date every line falls back to.
            if current is not None and _element(segment, 1) == "472":
                current["serviced_date"] = date
            else:
                envelope.claim.service_date = date
        elif tag == "SVC":
            line_number += 1
            envelope.claim.lines += 1
            current = _read_service_line(segment, delimiters, envelope, result)
            if current is None:
                continue
            current["source_file"] = source_file
            current["line"] = line_number
            records.append(current)

    return records


def _read_service_line(
    segment: list[str],
    delimiters: Delimiters,
    envelope: _Envelope,
    result: ERAParseResult,
) -> dict[str, object] | None:
    result.line_items_read += 1
    qualifier, code, modifiers = _svc_composite(_element(segment, 1), delimiters)
    if not code:
        result.line_items_without_code += 1
        result.exclude(NO_CODE)
        return None
    if qualifier not in _CPT_QUALIFIERS:
        # NU (revenue), RB, and friends do not join to RVUs, the same way DRG
        # rows do not on the MRF side.
        result.exclude(NON_HC_QUALIFIER)
        return None

    paid = _amount(_element(segment, 2))
    submitted = _amount(_element(segment, 3))
    units = _units(_element(segment, 5))
    if paid < 0 or submitted < 0:
        # Reversals and take-backs carry negative dollars, and their units are
        # positive: negate the units so the reversal cancels the original line
        # in the netted sums instead of inflating volume.
        units = -abs(units)
        result.exclude(REVERSALS)

    return {
        "payer_name": envelope.claim.payer_name or envelope.payer_name,
        "code": code,
        "code_type": _code_type(code),
        "modifiers": modifiers,
        "serviced_date": envelope.claim.service_date,
        "units": units,
        "submitted_amount": submitted,
        "allowed_amount": None,
        "paid_amount": paid,
    }


def era_audit(path: str | Path, join: JoinResult, group_by: str = "plan") -> UtilizationAudit:
    """Parse 835s and reprice the observed mix at each payer's contracted rates."""
    return audit_lines(parse_era(path), join, group_by=group_by)
