from __future__ import annotations

import csv

import pytest

from payer_rate_audit.rvu import RVU_COLUMNS, RVUFormatError, find_rvu_file, parse_pprrvu


def test_parses_the_documented_columns(rvu):
    assert list(rvu.frame.columns) == RVU_COLUMNS
    assert rvu.year == 2026
    assert len(rvu.frame) == 10


def test_descriptors_are_not_retained():
    # CPT long descriptors are AMA-licensed; the parser must not carry them.
    assert "description" not in RVU_COLUMNS


def test_numeric_columns_are_numeric(rvu):
    row = rvu.frame[(rvu.frame["hcpcs"] == "99213") & (rvu.frame["modifier"] == "")].iloc[0]
    assert row["work_rvu"] == pytest.approx(1.30)
    assert row["nonfac_pe_rvu"] == pytest.approx(1.14)
    assert row["fac_pe_rvu"] == pytest.approx(0.53)
    assert row["mp_rvu"] == pytest.approx(0.09)
    assert row["nonfac_total"] == pytest.approx(2.53)
    assert row["fac_total"] == pytest.approx(1.92)
    assert row["status_code"] == "A"
    assert row["global_days"] == "XXX"


def test_pc_tc_rows_are_kept_separately(rvu):
    mri = rvu.frame[rvu.frame["hcpcs"] == "70551"].set_index("modifier")
    assert set(mri.index) == {"", "26", "TC"}
    assert mri.loc["26", "nonfac_total"] == pytest.approx(2.01)
    assert mri.loc["TC", "nonfac_total"] == pytest.approx(7.73)
    assert (mri["pctc_indicator"] == "1").all()


def test_schema_change_fails_loudly(tmp_path, fixtures_dir):
    rows = list(csv.reader((fixtures_dir / "pprrvu_sample.csv").open()))
    for row in rows:
        for index, cell in enumerate(row):
            if cell.strip() == "FACILITY":
                row[index] = "RENAMED"
    broken = tmp_path / "broken.csv"
    with broken.open("w", newline="") as handle:
        csv.writer(handle).writerows(rows)
    with pytest.raises(RVUFormatError, match="fac_total|column"):
        parse_pprrvu(broken)


def test_a_file_without_a_header_fails_loudly(tmp_path):
    path = tmp_path / "nothing.csv"
    path.write_text("a,b,c\n1,2,3\n", encoding="utf-8")
    with pytest.raises(RVUFormatError):
        parse_pprrvu(path)


def test_find_rvu_file_reports_where_it_looked(tmp_path):
    with pytest.raises(RVUFormatError, match="fetch_rvu"):
        find_rvu_file(tmp_path, 2026)
