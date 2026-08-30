"""Command line entry point."""

from __future__ import annotations

import argparse
import dataclasses
import sys
from pathlib import Path

from .config import GROUP_BY_CHOICES, RVU_BASIS_CHOICES, ConfigError, load_config
from .eob import eob_audit
from .metrics import audit
from .mrf import MRFFormatError, parse_mrf
from .report import render_console, render_html
from .rvu import RVUFormatError, load_rvu_table, parse_pprrvu
from .utilization import LineFormatError, UtilizationAudit
from .x12_835 import era_audit


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="payer-rate-audit",
        description=(
            "Compute each payer's effective conversion factor (negotiated dollars per RVU) "
            "from a hospital price transparency machine-readable file."
        ),
    )
    parser.add_argument("mrf", help="Path to a CMS hospital MRF (CSV tall, CSV wide, or JSON)")
    parser.add_argument("--config", default="config.toml", help="Path to config.toml")
    parser.add_argument("--rvu-file", help="Path to a PPRRVU CSV (default: newest in --data-dir)")
    parser.add_argument("--data-dir", default="data", help="Where fetch_rvu.py put the RVU file")
    parser.add_argument("--rvu-basis", choices=RVU_BASIS_CHOICES, help="Override [audit].rvu_basis")
    parser.add_argument("--group-by", choices=GROUP_BY_CHOICES, help="Override [audit].group_by")
    parser.add_argument(
        "--billing-class",
        help="Only count rows with this billing_class "
        "(e.g. professional); 'both' rows always count",
    )
    parser.add_argument(
        "--eob",
        metavar="PATH",
        help="Directory of CARIN Blue Button FHIR R4 ExplanationOfBenefit "
        "bundles, or a single JSON/NDJSON file; adds the observed service "
        "mix repriced at each payer's rates",
    )
    parser.add_argument(
        "--era",
        metavar="PATH",
        help="Directory (searched recursively) of X12 835 remittance files, or "
        "a single 835 file; adds the observed service mix repriced at each "
        "payer's rates. Can be combined with --eob",
    )
    parser.add_argument("--html", metavar="PATH", help="Write a self-contained HTML report")
    parser.add_argument(
        "--csv-out",
        metavar="DIR",
        help="Also write payer_table.csv, spread.csv, cash_beats_contract.csv "
        "and unmatched_codes.csv to DIR",
    )
    parser.add_argument("--top", type=int, default=25, help="Rows per detail table (default 25)")
    parser.add_argument("--quiet", action="store_true", help="Suppress the console report")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    try:
        config = load_config(args.config)
    except ConfigError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2

    overrides = {}
    if args.rvu_basis:
        overrides["rvu_basis"] = args.rvu_basis
    if args.group_by:
        overrides["group_by"] = args.group_by
    if args.billing_class:
        overrides["billing_class_filter"] = args.billing_class.strip().lower()
    if overrides:
        config = dataclasses.replace(config, **overrides)

    try:
        parse_result = parse_mrf(args.mrf)
        rvu_table = (
            parse_pprrvu(args.rvu_file, year=config.rvu_year)
            if args.rvu_file
            else load_rvu_table(args.data_dir, config.rvu_year)
        )
    except (MRFFormatError, RVUFormatError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1

    result = audit(parse_result, rvu_table, config)

    sources: list[UtilizationAudit] = []
    eob: UtilizationAudit | None = None
    era: UtilizationAudit | None = None
    try:
        if args.eob:
            eob = eob_audit(args.eob, result.join, group_by=config.group_by)
            sources.append(eob)
        if args.era:
            era = era_audit(args.era, result.join, group_by=config.group_by)
            sources.append(era)
    except LineFormatError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1

    if not args.quiet:
        print(render_console(result, top_n=args.top, sources=sources))

    if args.html:
        Path(args.html).write_text(
            render_html(result, top_n=args.top, sources=sources), encoding="utf-8"
        )
        print(f"wrote {args.html}")

    if args.csv_out:
        out_dir = Path(args.csv_out)
        out_dir.mkdir(parents=True, exist_ok=True)
        result.payer_table.to_csv(out_dir / "payer_table.csv", index=False)
        result.spread.to_csv(out_dir / "spread.csv", index=False)
        result.cash_beats_contract.to_csv(out_dir / "cash_beats_contract.csv", index=False)
        result.join.unmatched_codes.to_csv(out_dir / "unmatched_codes.csv", index=False)
        written = 4
        if eob is not None:
            eob.repriced.to_csv(out_dir / "eob_repriced.csv", index=False)
            eob.utilization.to_csv(out_dir / "eob_utilization.csv", index=False)
            written += 2
        if era is not None:
            era.repriced.to_csv(out_dir / "era_repriced.csv", index=False)
            era.utilization.to_csv(out_dir / "era_utilization.csv", index=False)
            written += 2
        print(f"wrote {written} CSVs to {out_dir}")

    # A join rate below the configured floor is a result the caller should be
    # able to act on, not just read.
    return 3 if result.join.join_rate_is_low(config.min_join_rate) else 0


if __name__ == "__main__":
    raise SystemExit(main())
