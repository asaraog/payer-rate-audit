from __future__ import annotations

import json
from pathlib import Path

import pytest

from conftest import SHAPE_FIXTURES
from payer_rate_audit.eob import EOBFormatError, eob_audit, parse_eob, reprice, utilization
from payer_rate_audit.metrics import audit
from payer_rate_audit.mrf import parse_mrf


@pytest.fixture(scope="module")
def eob_dir(request) -> Path:
    return Path(request.config.rootdir) / "tests" / "fixtures" / "eob"


@pytest.fixture
def join(config, rvu):
    return audit(parse_mrf(SHAPE_FIXTURES["csv_tall"]), rvu, config).join


def test_directory_reads_bundles_and_ndjson(eob_dir):
    parse = parse_eob(eob_dir)
    assert parse.files_read == 2
    assert parse.resources_read == 5  # 4 EOBs plus one Patient
    assert parse.non_eob_resources == 1
    assert parse.unreadable_files == []
    assert set(parse.frame["payer_name"]) == {"Alpha Health", "Beta Mutual"}


def test_line_items_without_coding_are_counted_not_dropped(eob_dir):
    parse = parse_eob(eob_dir)
    assert parse.line_items_read == parse.row_count + parse.line_items_without_code
    assert parse.line_items_without_code == 2


def test_single_ndjson_file_is_accepted(eob_dir):
    parse = parse_eob(eob_dir / "eobs.ndjson")
    assert parse.files_read == 1
    assert set(parse.frame["payer_name"]) == {"Beta Mutual"}


def test_line_item_fields_are_extracted(eob_dir):
    frame = parse_eob(eob_dir).frame
    row = frame[(frame["code"] == "99213") & (frame["payer_name"] == "Beta Mutual")].iloc[0]
    assert row["code_type"] == "CPT"
    assert row["serviced_date"] == "2026-04-04"
    assert row["units"] == 2
    assert row["submitted_amount"] == 420
    assert row["allowed_amount"] == 170
    assert row["paid_amount"] == 150


def test_modifiers_and_hcpcs_level_two_are_recognized(eob_dir):
    frame = parse_eob(eob_dir).frame
    assert frame.loc[frame["code"] == "70551", "modifiers"].iloc[0] == "26"
    assert frame.loc[frame["code"] == "A4550", "code_type"].iloc[0] == "HCPCS"


def test_bare_five_digit_code_without_a_system_falls_back_to_cpt(eob_dir):
    frame = parse_eob(eob_dir / "eobs.ndjson").frame
    assert frame.loc[frame["code"] == "29881", "code_type"].iloc[0] == "CPT"


def test_utilization_aggregates_the_observed_mix(eob_dir):
    table = utilization(parse_eob(eob_dir))
    row = table[table["code"] == "99213"].iloc[0]
    assert row["line_items"] == 2
    assert row["units"] == 5  # 3 from the bundle plus 2 from the ndjson
    assert table["units"].is_monotonic_decreasing


def test_repricing_states_priced_and_unpriced_codes_per_payer(eob_dir, join):
    parse = parse_eob(eob_dir)
    table = reprice(parse, join, group_by="plan")
    codes_observed = utilization(parse)["code"].nunique()
    for _, row in table.iterrows():
        assert row["codes_priced"] + row["codes_unpriced"] == codes_observed
        assert row["delta"] == pytest.approx(row["repriced"] - row["actual_paid"])
    # Beta Mutual does not publish a rate for the HCPCS supply code in the mix.
    beta = table[table["payer_name"] == "Beta Mutual"].iloc[0]
    assert beta["codes_unpriced"] == 1


def test_repricing_can_group_by_payer(eob_dir, join):
    table = reprice(parse_eob(eob_dir), join, group_by="payer")
    assert "plan_name" not in table.columns
    assert len(table) == 2


def test_missing_path_and_empty_directory_fail_loudly(tmp_path, eob_dir):
    with pytest.raises(EOBFormatError):
        parse_eob(tmp_path / "nope")
    (tmp_path / "empty").mkdir()
    with pytest.raises(EOBFormatError):
        parse_eob(tmp_path / "empty")


def test_unreadable_files_are_recorded_rather_than_raising(tmp_path, eob_dir):
    (tmp_path / "broken.json").write_text("{not json", encoding="utf-8")
    (tmp_path / "ok.json").write_text(
        json.dumps(json.loads((eob_dir / "bundle.json").read_text())), encoding="utf-8"
    )
    parse = parse_eob(tmp_path)
    assert parse.unreadable_files == [str(tmp_path / "broken.json")]
    assert parse.files_read == 1


def test_eob_audit_bundles_parse_utilization_and_repricing(eob_dir, join):
    result = eob_audit(eob_dir, join, group_by="plan")
    assert result.parse.row_count == len(result.parse.frame)
    assert not result.utilization.empty
    assert not result.repriced.empty
