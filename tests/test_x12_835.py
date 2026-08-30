from __future__ import annotations

from pathlib import Path

import pytest

from conftest import SHAPE_FIXTURES
from payer_rate_audit.metrics import audit
from payer_rate_audit.mrf import parse_mrf
from payer_rate_audit.x12_835 import (
    NON_835_FILES,
    NON_HC_QUALIFIER,
    REVERSALS,
    ERAFormatError,
    era_audit,
    parse_era,
    read_delimiters,
)

# Identifiers planted in the synthetic fixtures. None may reach any output.
PLANTED_IDENTIFIERS = [
    "ACCT-10001",
    "ACCT-20001",
    "ACCT-30001",
    "ACCT-40001",
    "ACCT-50001",
    "DOEPATIENT",
    "JOHNPATIENT",
    "ROEPATIENT",
    "ZOEPATIENT",
    "SYNTHMEMBER1",
]


@pytest.fixture(scope="module")
def era_dir(request) -> Path:
    return Path(request.config.rootdir) / "tests" / "fixtures" / "era"


@pytest.fixture
def join(config, rvu):
    return audit(parse_mrf(SHAPE_FIXTURES["csv_tall"]), rvu, config).join


def test_delimiters_are_read_from_the_isa_not_hardcoded(era_dir):
    standard = read_delimiters((era_dir / "clean_single_claim.835").read_text())
    assert (standard.element, standard.composite, standard.segment) == ("*", ":", "~")

    alternate = read_delimiters((era_dir / "alt_delimiters.edi").read_text())
    assert (alternate.element, alternate.composite, alternate.segment) == ("|", ">", "\n")


def test_alternate_delimiters_parse_to_the_same_shape(era_dir):
    parse = parse_era(era_dir / "alt_delimiters.edi")
    row = parse.frame.iloc[0]
    assert row["code"] == "99213"
    assert row["payer_name"] == "Alpha Health"
    assert row["units"] == 3


def test_service_lines_are_extracted(era_dir):
    parse = parse_era(era_dir / "clean_single_claim.835")
    assert parse.files_read == 1
    assert parse.transactions_read == 1
    assert parse.claims_read == 1
    assert parse.row_count == 2

    row = parse.frame[parse.frame["code"] == "99213"].iloc[0]
    assert row["code_type"] == "CPT"
    assert row["payer_name"] == "Alpha Health"
    assert row["paid_amount"] == 95.0
    assert row["submitted_amount"] == 120.0
    assert row["units"] == 1
    assert row["serviced_date"] == "2026-01-12"


def test_modifiers_come_off_the_svc_composite(era_dir):
    frame = parse_era(era_dir / "multi_claim_modifiers.835").frame
    assert frame.loc[frame["code"] == "70551", "modifiers"].iloc[0] == "26"
    assert frame.loc[frame["code"] == "99213", "modifiers"].iloc[0] == "GP"


def test_absent_svc05_units_default_to_one_never_zero(era_dir):
    frame = parse_era(era_dir / "multi_claim_modifiers.835").frame
    assert frame.loc[frame["code"] == "99213", "units"].iloc[0] == 1
    assert frame.loc[frame["code"] == "45378", "units"].iloc[0] == 2


def test_claim_level_date_is_the_fallback_when_a_line_has_no_dtm(era_dir, tmp_path):
    source = (era_dir / "clean_single_claim.835").read_text()
    stripped = "\n".join(line for line in source.splitlines() if not line.startswith("DTM*472"))
    path = tmp_path / "claim_date_only.835"
    path.write_text(stripped, encoding="utf-8")
    assert set(parse_era(path).frame["serviced_date"]) == {"2026-01-12"}


def test_non_hc_qualifier_lines_are_counted_out_of_scope(era_dir):
    parse = parse_era(era_dir / "non_hc_qualifier.835")
    assert parse.line_items_read == 2
    assert parse.row_count == 1
    assert parse.exclusions[NON_HC_QUALIFIER] == 1
    assert "0450" not in set(parse.frame["code"])


def test_reversals_net_out_and_are_counted(era_dir):
    parse = parse_era(era_dir / "reversal.835")
    assert parse.reversal_lines == 1
    assert parse.exclusions[REVERSALS] == 1
    assert parse.frame["paid_amount"].sum() == 0.0
    assert parse.frame["units"].sum() == 0.0


def test_directory_is_read_recursively_and_non_835_files_are_counted(era_dir, tmp_path):
    nested = tmp_path / "mailbox" / "2026-01"
    nested.mkdir(parents=True)
    (nested / "copy.835").write_text(
        (era_dir / "clean_single_claim.835").read_text(), encoding="utf-8"
    )
    (tmp_path / "mailbox" / "notes.txt").write_text("not a remittance", encoding="utf-8")

    parse = parse_era(tmp_path / "mailbox")
    assert parse.files_read == 1
    assert parse.exclusions[NON_835_FILES] == 1
    assert parse.row_count == 2


def test_a_path_with_no_835_is_an_error(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    (empty / "notes.txt").write_text("nothing here", encoding="utf-8")
    with pytest.raises(ERAFormatError):
        parse_era(empty)
    with pytest.raises(ERAFormatError):
        parse_era(tmp_path / "absent")


def test_repricing_uses_the_same_engine_as_the_fhir_path(era_dir, join):
    result = era_audit(era_dir, join)
    assert not result.repriced.empty
    assert set(result.repriced["payer_name"]) <= {"Alpha Health", "Beta Mutual"}
    assert (result.repriced["codes_priced"] > 0).all()
    assert set(result.utilization.columns) >= {"code", "units", "actual_paid"}


def test_no_patient_identifier_reaches_any_output(era_dir, join):
    result = era_audit(era_dir, join)
    rendered = "\n".join(
        [
            result.parse.frame.to_csv(index=False),
            result.utilization.to_csv(index=False),
            result.repriced.to_csv(index=False),
            str(result.parse.exclusions),
            result.parse.source,
        ]
    )
    fixtures = "\n".join(path.read_text() for path in sorted(era_dir.iterdir()))
    for identifier in PLANTED_IDENTIFIERS:
        assert identifier in fixtures, "fixture no longer plants this identifier"
        assert identifier not in rendered
