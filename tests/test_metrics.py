from __future__ import annotations

import dataclasses

import pytest

from conftest import SHAPE_FIXTURES
from payer_rate_audit.metrics import audit, join_rvu
from payer_rate_audit.mrf import parse_mrf

# Denominator for both payers, given the fixtures:
#   99213  professional -> facility total   1.92
#   70551  modifier 26, facility class -> non-facility total of the 26 row  2.01
#   45378  professional -> facility total   5.33
# 29881 is absent from the RVU fixture (unmatched), 0042T is status I and
# A4550 is status E, so neither reaches the denominator.
EXPECTED_RVUS = 1.92 + 2.01 + 5.33
ALPHA_DOLLARS = 120 + 900 + 1200
BETA_DOLLARS = 80 + 600 + 700


@pytest.fixture(params=list(SHAPE_FIXTURES), ids=list(SHAPE_FIXTURES))
def parsed(request):
    return parse_mrf(SHAPE_FIXTURES[request.param])


def test_join_scopes_flags_and_counts(parsed, rvu, config):
    join = join_rvu(parsed, rvu, config)
    assert join.in_scope_rows == 10  # 11 normalized rows minus the MS-DRG row
    assert join.out_of_scope_by_code_type == {"MS-DRG": 1}
    assert join.exclusions["not_joinable_code_type"] == 1
    assert join.exclusions["unmatched_code"] == 2  # 29881, both payers
    assert join.exclusions["not_separately_payable"] == 2  # 0042T status I, A4550 status E
    assert join.matched_rows == 8
    assert join.counted_rows == 6
    assert join.join_rate == pytest.approx(8 / 10)


def test_unmatched_codes_are_enumerated_not_dropped(parsed, rvu, config):
    join = join_rvu(parsed, rvu, config)
    assert list(join.unmatched_codes["code"]) == ["29881"]
    assert int(join.unmatched_codes.iloc[0]["rows"]) == 2
    assert join.unmatched_codes.iloc[0]["code_type"] == "CPT"


def test_effective_conversion_factor_and_ratio(parsed, rvu, config):
    result = audit(parsed, rvu, config)
    table = result.payer_table.set_index("payer_name")
    assert list(table.index) == ["Alpha Health", "Beta Mutual"]  # sorted by CF, descending

    alpha = table.loc["Alpha Health"]
    assert alpha["negotiated_dollars"] == pytest.approx(ALPHA_DOLLARS)
    assert alpha["total_rvus"] == pytest.approx(EXPECTED_RVUS)
    assert alpha["effective_cf"] == pytest.approx(ALPHA_DOLLARS / EXPECTED_RVUS)
    assert alpha["ratio_to_medicare"] == pytest.approx(
        (ALPHA_DOLLARS / EXPECTED_RVUS) / config.conversion_factor
    )
    assert alpha["rows_counted"] == 3

    beta = table.loc["Beta Mutual"]
    assert beta["effective_cf"] == pytest.approx(BETA_DOLLARS / EXPECTED_RVUS)
    assert beta["effective_cf"] < alpha["effective_cf"]


def test_every_aggregate_states_its_denominator(parsed, rvu, config):
    table = audit(parsed, rvu, config).payer_table
    for column in (
        "rows_counted",
        "rows_seen",
        "rows_excluded",
        "negotiated_dollars",
        "total_rvus",
    ):
        assert column in table.columns
        assert table[column].notna().all()
    assert (table["rows_seen"] == table["rows_counted"] + table["rows_excluded"]).all()


def test_grouping_by_payer_collapses_plans(parsed, rvu, payer_config):
    table = audit(parsed, rvu, payer_config).payer_table
    assert "plan_name" not in table.columns
    assert len(table) == 2


def test_cash_beats_contract(parsed, rvu, config):
    flags = audit(parsed, rvu, config).cash_beats_contract
    # The colonoscopy only, and only for Alpha: the $900 cash price undercuts
    # Alpha's $1,200 contract but not Beta's $700.
    assert list(flags["code"]) == ["45378"]
    row = flags.iloc[0]
    assert row["payer_name"] == "Alpha Health"
    assert row["discounted_cash"] == pytest.approx(900)
    assert row["negotiated_dollar"] == pytest.approx(1200)
    assert row["gap"] == pytest.approx(300)


def test_spread_is_ranked_by_absolute_gap(parsed, rvu, config):
    spread = audit(parsed, rvu, config).spread
    # Single-payer codes (the DRG, 0042T, A4550) have no spread to report.
    assert list(spread["code"]) == ["29881", "45378", "70551", "99213"]
    top = spread.iloc[0]
    assert top["gap"] == pytest.approx(4100 - 3300)
    assert top["max_payer"] == "Alpha Health"
    assert top["min_payer"] == "Beta Mutual"
    assert top["payers"] == 2


def test_facility_basis_override_changes_the_denominator(parsed, rvu, config):
    facility = audit(parsed, rvu, dataclasses.replace(config, rvu_basis="facility"))
    nonfacility = audit(parsed, rvu, dataclasses.replace(config, rvu_basis="nonfacility"))
    alpha_fac = facility.payer_table.set_index("payer_name").loc["Alpha Health"]
    alpha_non = nonfacility.payer_table.set_index("payer_name").loc["Alpha Health"]
    assert alpha_fac["total_rvus"] == pytest.approx(1.92 + 2.01 + 5.33)
    assert alpha_non["total_rvus"] == pytest.approx(2.53 + 2.01 + 7.20)
    assert alpha_fac["effective_cf"] > alpha_non["effective_cf"]


def test_billing_class_filter_excludes_and_counts(parsed, rvu, config):
    filtered = audit(parsed, rvu, dataclasses.replace(config, billing_class_filter="professional"))
    assert filtered.join.exclusions["billing_class_filtered"] == 3
    alpha = filtered.payer_table.set_index("payer_name").loc["Alpha Health"]
    assert alpha["total_rvus"] == pytest.approx(1.92 + 5.33)  # the MRI row is institutional
    assert alpha["negotiated_dollars"] == pytest.approx(120 + 1200)


def test_percentage_exclusions_and_low_join_rate_are_warned_about(parsed, rvu, config):
    result = audit(parsed, rvu, dataclasses.replace(config, min_join_rate=0.9))
    joined = " ".join(result.warnings)
    assert "join rate" in joined.lower()
    assert "percentage" in joined.lower() or "algorithm" in joined.lower()
