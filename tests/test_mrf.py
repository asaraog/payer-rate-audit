from __future__ import annotations

import pandas as pd
import pytest

from conftest import SHAPE_FIXTURES
from payer_rate_audit.mrf import (
    NORMALIZED_COLUMNS,
    MRFFormatError,
    MRFShape,
    detect_shape,
    parse_mrf,
)

SORT_KEYS = ["payer_name", "plan_name", "code", "code_type"]


@pytest.mark.parametrize("expected,path", [(shape, path) for shape, path in SHAPE_FIXTURES.items()])
def test_detect_shape(expected, path):
    assert detect_shape(path) is MRFShape(expected)


@pytest.mark.parametrize("path", SHAPE_FIXTURES.values(), ids=list(SHAPE_FIXTURES))
def test_normalized_schema_is_identical_across_shapes(path):
    result = parse_mrf(path)
    assert list(result.frame.columns) == NORMALIZED_COLUMNS


def test_all_three_shapes_normalize_to_the_same_rows():
    frames = {}
    for shape, path in SHAPE_FIXTURES.items():
        frame = parse_mrf(path).frame.sort_values(SORT_KEYS).reset_index(drop=True)
        frames[shape] = frame
    reference = frames["csv_tall"]
    for shape, frame in frames.items():
        pd.testing.assert_frame_equal(frame, reference, obj=shape)


@pytest.mark.parametrize("path", SHAPE_FIXTURES.values(), ids=list(SHAPE_FIXTURES))
def test_row_counts_and_exclusions_are_reported(path):
    result = parse_mrf(path)
    # 13 payer-item charge records; two of them are percentage/algorithm only.
    assert result.total_charge_records == 13
    assert result.excluded_no_dollar == 2
    assert result.excluded_no_code == 0
    assert len(result.frame) == 11
    assert result.metadata["hospital_name"] == "Fixture General Hospital"


@pytest.mark.parametrize("path", SHAPE_FIXTURES.values(), ids=list(SHAPE_FIXTURES))
def test_cpt_wins_over_a_revenue_code_on_the_same_item(path):
    frame = parse_mrf(path).frame
    office_visits = frame[frame["description"].str.startswith("Office visit")]
    assert set(office_visits["code"]) == {"99213"}
    assert set(office_visits["code_type"]) == {"CPT"}
    # One row per payer, not one per code pair: the negotiated dollar must not
    # be duplicated across the revenue code and the CPT code.
    assert len(office_visits) == 2


@pytest.mark.parametrize("path", SHAPE_FIXTURES.values(), ids=list(SHAPE_FIXTURES))
def test_modifiers_and_non_cpt_codes_survive_normalization(path):
    frame = parse_mrf(path).frame
    mri = frame[frame["code"] == "70551"]
    assert set(mri["modifiers"]) == {"26"}
    drg = frame[frame["code_type"] == "MS-DRG"]
    assert len(drg) == 1
    assert drg.iloc[0]["code"] == "470"


@pytest.mark.parametrize("path", SHAPE_FIXTURES.values(), ids=list(SHAPE_FIXTURES))
def test_dollar_and_context_fields_are_carried_through(path):
    frame = parse_mrf(path).frame
    row = frame[(frame["code"] == "99213") & (frame["payer_name"] == "Alpha Health")].iloc[0]
    assert row["plan_name"] == "PPO"
    assert row["negotiated_dollar"] == 120.0
    assert row["gross_charge"] == 210.0
    assert row["discounted_cash"] == 150.0
    assert row["min_negotiated"] == 80.0
    assert row["max_negotiated"] == 120.0
    assert row["billing_class"] == "professional"
    assert row["setting"] == "outpatient"
    assert row["methodology"] == "fee schedule"


def test_percentage_and_algorithm_rows_are_excluded_not_dropped_silently():
    for path in SHAPE_FIXTURES.values():
        result = parse_mrf(path)
        assert "Gamma Care" not in set(result.frame["payer_name"])
        assert result.excluded_no_dollar == 2


def test_unknown_file_shape_raises(tmp_path):
    path = tmp_path / "mystery.csv"
    path.write_text("alpha,beta\n1,2\n", encoding="utf-8")
    with pytest.raises(MRFFormatError):
        detect_shape(path)
