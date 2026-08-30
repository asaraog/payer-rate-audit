from __future__ import annotations

from pathlib import Path

import pytest

from conftest import SHAPE_FIXTURES
from payer_rate_audit.cli import main

CONFIG = str(Path(__file__).resolve().parents[1] / "config.toml")


def _run(capsys, *args) -> tuple[int, str]:
    code = main(list(args))
    return code, capsys.readouterr().out


@pytest.mark.parametrize("path", SHAPE_FIXTURES.values(), ids=list(SHAPE_FIXTURES))
def test_console_report_states_its_denominators(capsys, fixtures_dir, path):
    code, out = _run(
        capsys,
        str(path),
        "--config",
        CONFIG,
        "--rvu-file",
        str(fixtures_dir / "pprrvu_sample.csv"),
    )
    assert code == 0
    assert "EFFECTIVE CONVERSION FACTOR BY PAYER" in out
    assert "Alpha Health" in out and "Beta Mutual" in out
    assert "Join rate           : 80.0%" in out
    assert "33.4009" in out  # Medicare CF, from config.toml
    assert "pprrvu_sample.csv" in out
    assert Path(path).name in out
    # Excluded things are named, not silently gone.
    assert "29881" in out  # unmatched code enumerated
    assert "percentage or algorithm" in out
    assert "no GPCI" in out


def test_payer_table_is_sorted_by_effective_conversion_factor(capsys, fixtures_dir):
    _, out = _run(
        capsys,
        str(SHAPE_FIXTURES["csv_tall"]),
        "--config",
        CONFIG,
        "--rvu-file",
        str(fixtures_dir / "pprrvu_sample.csv"),
    )
    assert out.index("Alpha Health") < out.index("Beta Mutual")


def test_html_is_self_contained(tmp_path, capsys, fixtures_dir):
    target = tmp_path / "report.html"
    code, _ = _run(
        capsys,
        str(SHAPE_FIXTURES["json"]),
        "--config",
        CONFIG,
        "--rvu-file",
        str(fixtures_dir / "pprrvu_sample.csv"),
        "--html",
        str(target),
        "--quiet",
    )
    assert code == 0
    html = target.read_text(encoding="utf-8")
    assert html.startswith("<!DOCTYPE html>")
    assert "Alpha Health" in html
    for external in ("http://", "https://", "<script", "cdn.", "src="):
        assert external not in html


def test_low_join_rate_is_reported_and_exits_nonzero(capsys, fixtures_dir, tmp_path):
    config = tmp_path / "config.toml"
    config.write_text(
        "[medicare]\nconversion_factor = 33.4009\n[rvu]\nyear = 2026\n"
        "[audit]\nmin_join_rate = 0.95\n",
        encoding="utf-8",
    )
    code, out = _run(
        capsys,
        str(SHAPE_FIXTURES["csv_tall"]),
        "--config",
        str(config),
        "--rvu-file",
        str(fixtures_dir / "pprrvu_sample.csv"),
    )
    assert code == 3
    assert "Join rate is 80.0%" in out


def test_missing_config_is_an_error(capsys, tmp_path, fixtures_dir):
    code = main([str(SHAPE_FIXTURES["csv_tall"]), "--config", str(tmp_path / "absent.toml")])
    assert code == 2


def test_csv_outputs(tmp_path, capsys, fixtures_dir):
    out_dir = tmp_path / "out"
    code, _ = _run(
        capsys,
        str(SHAPE_FIXTURES["csv_wide"]),
        "--config",
        CONFIG,
        "--rvu-file",
        str(fixtures_dir / "pprrvu_sample.csv"),
        "--csv-out",
        str(out_dir),
        "--quiet",
    )
    assert code == 0
    written = sorted(path.name for path in out_dir.iterdir())
    assert written == [
        "cash_beats_contract.csv",
        "payer_table.csv",
        "spread.csv",
        "unmatched_codes.csv",
    ]


def test_eob_adds_utilization_repricing_and_csvs(tmp_path, capsys, fixtures_dir):
    out_dir = tmp_path / "out"
    html = tmp_path / "report.html"
    code, out = _run(
        capsys,
        str(SHAPE_FIXTURES["csv_tall"]),
        "--config",
        CONFIG,
        "--rvu-file",
        str(fixtures_dir / "pprrvu_sample.csv"),
        "--eob",
        str(fixtures_dir / "eob"),
        "--csv-out",
        str(out_dir),
        "--html",
        str(html),
    )
    assert code == 0
    assert "OBSERVED UTILIZATION" in out
    assert "no usable HCPCS/CPT : 2 line items" in out
    assert "OBSERVED MIX REPRICED" in out
    assert {"eob_repriced.csv", "eob_utilization.csv"} <= {p.name for p in out_dir.iterdir()}
    report = html.read_text(encoding="utf-8")
    assert "Observed mix repriced" in report
    for external in ("http://", "https://", "<script", "cdn.", "src="):
        assert external not in report


def test_unreadable_eob_path_is_an_error(capsys, fixtures_dir, tmp_path):
    code, _ = _run(
        capsys,
        str(SHAPE_FIXTURES["csv_tall"]),
        "--config",
        CONFIG,
        "--rvu-file",
        str(fixtures_dir / "pprrvu_sample.csv"),
        "--eob",
        str(tmp_path / "absent"),
        "--quiet",
    )
    assert code == 1


def test_era_adds_the_repriced_table_and_csvs(tmp_path, capsys, fixtures_dir):
    out_dir = tmp_path / "out"
    code, out = _run(
        capsys,
        str(SHAPE_FIXTURES["csv_tall"]),
        "--config",
        CONFIG,
        "--rvu-file",
        str(fixtures_dir / "pprrvu_sample.csv"),
        "--era",
        str(fixtures_dir / "era"),
        "--csv-out",
        str(out_dir),
    )
    assert code == 0
    assert "OBSERVED UTILIZATION (X12 835 remittance advice)" in out
    assert "OBSERVED MIX REPRICED AT EACH PAYER'S CONTRACTED RATES (X12 835" in out
    assert "reversal/negative service lines" in out
    assert "non-HCPCS procedure qualifier" in out
    assert {"era_repriced.csv", "era_utilization.csv"} <= {p.name for p in out_dir.iterdir()}
    written = "\n".join(p.read_text(encoding="utf-8") for p in out_dir.iterdir())
    for identifier in ("ACCT-10001", "DOEPATIENT", "SYNTHMEMBER1"):
        assert identifier not in written and identifier not in out


def test_era_and_eob_sources_are_reported_separately(capsys, fixtures_dir):
    code, out = _run(
        capsys,
        str(SHAPE_FIXTURES["csv_tall"]),
        "--config",
        CONFIG,
        "--rvu-file",
        str(fixtures_dir / "pprrvu_sample.csv"),
        "--eob",
        str(fixtures_dir / "eob"),
        "--era",
        str(fixtures_dir / "era"),
    )
    assert code == 0
    assert "OBSERVED UTILIZATION (FHIR ExplanationOfBenefit)" in out
    assert "OBSERVED UTILIZATION (X12 835 remittance advice)" in out


def test_unreadable_era_path_is_an_error(capsys, fixtures_dir, tmp_path):
    code, _ = _run(
        capsys,
        str(SHAPE_FIXTURES["csv_tall"]),
        "--config",
        CONFIG,
        "--rvu-file",
        str(fixtures_dir / "pprrvu_sample.csv"),
        "--era",
        str(tmp_path / "absent"),
        "--quiet",
    )
    assert code == 1
