"""Console and HTML rendering.

The HTML file is self-contained: inline CSS, no external assets, no CDN, so it
survives being emailed to someone's compliance officer.
"""

from __future__ import annotations

import html
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

import pandas as pd

from .metrics import EXCLUSION_LABELS, AuditResult, header_facts
from .utilization import UtilizationAudit

_TOP_N = 25

_REPRICE_COLUMNS = {
    "payer_name": "Payer",
    "plan_name": "Plan",
    "codes_priced": "Codes priced",
    "codes_unpriced": "Codes unpriced",
    "units_priced": "Units",
    "actual_paid": "Actually paid $",
    "repriced": "Repriced $",
    "delta": "Delta $",
    "ratio": "x actual",
}
_REPRICE_FORMATS = {
    "codes_priced": ",d",
    "codes_unpriced": ",d",
    "units_priced": ",.0f",
    "actual_paid": ",.2f",
    "repriced": ",.2f",
    "delta": ",.2f",
    "ratio": ",.2f",
}


_VOLUME_COLUMNS = {
    "code": "Code",
    "code_type": "Type",
    "line_items": "Lines",
    "units": "Units",
    "actual_paid": "Actually paid $",
}
_VOLUME_FORMATS = {"line_items": ",d", "units": ",.0f", "actual_paid": ",.2f"}


def _record_word(kind: str) -> str:
    """What one record is called in the source format's own vocabulary."""
    return "resources" if "FHIR" in kind else "transactions"


def _reprice_columns(frame: pd.DataFrame) -> dict[str, str]:
    return {key: label for key, label in _REPRICE_COLUMNS.items() if key in frame.columns}


def _fmt(value: Any, spec: str = "") -> str:
    if value is None or (isinstance(value, float) and pd.isna(value)) or value is pd.NA:
        return "-"
    if spec:
        try:
            return format(value, spec)
        except (TypeError, ValueError):
            return str(value)
    return str(value)


def _text_table(frame: pd.DataFrame, columns: dict[str, str], formats: dict[str, str]) -> str:
    if frame.empty:
        return "  (none)"
    headers = list(columns.values())
    rows = [
        [_fmt(row[key], formats.get(key, "")) for key in columns] for _, row in frame.iterrows()
    ]
    widths = [
        max(len(headers[i]), *(len(row[i]) for row in rows)) if rows else len(headers[i])
        for i in range(len(headers))
    ]
    line = "  ".join(header.ljust(widths[i]) for i, header in enumerate(headers))
    separator = "  ".join("-" * width for width in widths)
    body = "\n".join("  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)) for row in rows)
    return f"{line}\n{separator}\n{body}"


def _payer_columns(group_by: str) -> tuple[dict[str, str], dict[str, str]]:
    columns = {"payer_name": "Payer"}
    if group_by == "plan":
        columns["plan_name"] = "Plan"
    columns.update(
        {
            "effective_cf": "Effective CF",
            "ratio_to_medicare": "x Medicare",
            "negotiated_dollars": "Negotiated $",
            "total_rvus": "Total RVUs",
            "rows_counted": "Rows counted",
            "rows_excluded": "Rows excluded",
        }
    )
    formats = {
        "effective_cf": ",.2f",
        "ratio_to_medicare": ",.2f",
        "negotiated_dollars": ",.0f",
        "total_rvus": ",.1f",
        "rows_counted": ",d",
        "rows_excluded": ",d",
    }
    return columns, formats


def render_console(
    result: AuditResult,
    top_n: int = _TOP_N,
    sources: Sequence[UtilizationAudit] = (),
) -> str:
    facts = header_facts(result)
    join = result.join
    lines: list[str] = []
    add = lines.append

    add("=" * 78)
    add("PAYER RATE AUDIT")
    add("=" * 78)
    add(f"MRF file            : {facts['mrf_file']} ({facts['mrf_shape']})")
    add(f"Hospital            : {facts['hospital']}")
    add(
        f"MRF last updated    : {facts['mrf_last_updated']}  "
        f"(template v{facts['mrf_template_version']})"
    )
    add(f"RVU file            : {facts['rvu_file']} (year {facts['rvu_year']})")
    add(
        f"Medicare CF         : {facts['medicare_conversion_factor']} "
        f"[{facts['conversion_factor_source']}]"
    )
    add(
        f"RVU basis           : {facts['rvu_basis']}   "
        f"billing_class filter: {facts['billing_class_filter']}"
    )
    add(
        f"Rows                : {facts['normalized_rows']:,} normalized -> "
        f"{facts['in_scope_rows']:,} CPT/HCPCS in scope -> {facts['matched_rows']:,} matched -> "
        f"{facts['counted_rows']:,} in the denominator"
    )
    join_rate = facts["join_rate"]
    add(
        f"Join rate           : {join_rate:.1%}"
        if join_rate is not None
        else "Join rate           : n/a (no CPT/HCPCS rows in scope)"
    )
    add("Geography           : national RVUs, no GPCI locality adjustment applied")

    if result.warnings:
        add("")
        add("WARNINGS")
        for warning in result.warnings:
            add(f"  ! {warning}")

    add("")
    add("EXCLUSIONS (rows removed from the denominator, by reason)")
    for key, count in join.exclusions.items():
        if count:
            add(f"  {count:>8,}  {EXCLUSION_LABELS[key]}")
    if result.parse.excluded_no_dollar:
        add(
            f"  {result.parse.excluded_no_dollar:>8,}  charge stated as percentage or algorithm, "
            "not a dollar amount"
        )
    if result.parse.excluded_no_code:
        add(f"  {result.parse.excluded_no_code:>8,}  charge with no usable billing code")
    if join.out_of_scope_by_code_type:
        detail = ", ".join(
            f"{code_type}={count:,}"
            for code_type, count in sorted(
                join.out_of_scope_by_code_type.items(), key=lambda item: -item[1]
            )
        )
        add(f"  non-joinable code types: {detail}")

    add("")
    add(
        "EFFECTIVE CONVERSION FACTOR BY PAYER"
        + (" AND PLAN" if result.config.group_by == "plan" else " (aggregated across plans)")
    )
    columns, formats = _payer_columns(result.config.group_by)
    add(_text_table(result.payer_table, columns, formats))

    add("")
    add(
        f"UNMATCHED CPT/HCPCS CODES ({len(join.unmatched_codes):,} distinct; "
        f"top {min(top_n, len(join.unmatched_codes))} by row count)"
    )
    add(
        _text_table(
            join.unmatched_codes.head(top_n),
            {"code": "Code", "code_type": "Type", "rows": "Rows", "description": "MRF description"},
            {"rows": ",d"},
        )
    )

    add("")
    add(
        f"CASH BEATS CONTRACT ({len(result.cash_beats_contract):,} rows where the discounted "
        f"cash price is below the negotiated rate; top {top_n} by gap)"
    )
    add(
        _text_table(
            result.cash_beats_contract.head(top_n),
            {
                "payer_name": "Payer",
                "plan_name": "Plan",
                "code": "Code",
                "discounted_cash": "Cash $",
                "negotiated_dollar": "Negotiated $",
                "gap": "Gap $",
            },
            {"discounted_cash": ",.2f", "negotiated_dollar": ",.2f", "gap": ",.2f"},
        )
    )

    add("")
    add(f"SPREAD BY CODE ({len(result.spread):,} codes; top {top_n} by absolute gap)")
    add(
        _text_table(
            result.spread.head(top_n),
            {
                "code": "Code",
                "code_type": "Type",
                "payers": "Payers",
                "min_dollar": "Min $",
                "min_payer": "Min payer",
                "max_dollar": "Max $",
                "max_payer": "Max payer",
                "gap": "Gap $",
            },
            {"min_dollar": ",.2f", "max_dollar": ",.2f", "gap": ",.2f", "payers": ",d"},
        )
    )

    for source in sources:
        parse = source.parse
        add("")
        add(f"OBSERVED UTILIZATION ({parse.kind})")
        add(f"  source              : {parse.source}")
        add(
            f"  read                : {parse.files_read:,} files, "
            f"{parse.records_read:,} {_record_word(parse.kind)}, "
            f"{parse.line_items_read:,} line items -> "
            f"{parse.row_count:,} coded lines"
        )
        add(
            f"  no usable HCPCS/CPT : {parse.line_items_without_code:,} line items "
            "(reported, not dropped)"
        )
        for reason, count in parse.exclusions.items():
            add(f"  excluded            : {count:,} {reason}")
        if parse.unreadable_files:
            add(f"  unreadable files    : {len(parse.unreadable_files):,}")
        add("")
        add(f"OBSERVED MIX REPRICED AT EACH PAYER'S CONTRACTED RATES ({parse.kind})")
        add(_text_table(source.repriced, _reprice_columns(source.repriced), _REPRICE_FORMATS))
        add("")
        add(f"TOP CODES BY VOLUME (top {top_n}, {parse.kind})")
        add(_text_table(source.utilization.head(top_n), _VOLUME_COLUMNS, _VOLUME_FORMATS))
    add("")
    return "\n".join(lines)


def _html_table(frame: pd.DataFrame, columns: dict[str, str], formats: dict[str, str]) -> str:
    if frame.empty:
        return "<p class='empty'>(none)</p>"
    head = "".join(f"<th>{html.escape(label)}</th>" for label in columns.values())
    body = "".join(
        "<tr>"
        + "".join(
            f"<td>{html.escape(_fmt(row[key], formats.get(key, '')))}</td>" for key in columns
        )
        + "</tr>"
        for _, row in frame.iterrows()
    )
    return f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"


_CSS = """
:root { color-scheme: light; }
body { font: 15px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, sans-serif;
       margin: 0 auto; max-width: 1100px; padding: 2rem 1.5rem 4rem; color: #16202a; }
h1 { font-size: 1.6rem; margin-bottom: 0.2rem; }
h2 { font-size: 1.1rem; margin-top: 2.2rem; border-bottom: 2px solid #16202a;
     padding-bottom: .3rem; }
p.sub { color: #5b6b7a; margin-top: 0; }
dl.facts { display: grid; grid-template-columns: max-content 1fr; gap: .25rem 1.2rem;
           margin: 1rem 0; }
dl.facts dt { color: #5b6b7a; }
dl.facts dd { margin: 0; font-variant-numeric: tabular-nums; }
table { border-collapse: collapse; width: 100%; margin-top: .6rem; font-size: 14px; }
th, td { text-align: left; padding: .35rem .6rem; border-bottom: 1px solid #e2e8ee; }
th { background: #f4f7fa; font-weight: 600; }
td { font-variant-numeric: tabular-nums; }
tr:hover td { background: #fbfcfe; }
.warn { background: #fff4e5; border-left: 4px solid #d97706; padding: .8rem 1rem; margin: .6rem 0; }
.empty { color: #7b8794; font-style: italic; }
ul.exclusions { padding-left: 1.2rem; }
footer { margin-top: 3rem; color: #7b8794; font-size: 13px; }
"""


def _source_html(source: UtilizationAudit, top_n: int) -> str:
    parse = source.parse
    kind = html.escape(parse.kind)
    facts = {
        "Source": parse.source,
        "Read": (
            f"{parse.files_read:,} files / {parse.records_read:,} {_record_word(parse.kind)} / "
            f"{parse.line_items_read:,} line items / {parse.row_count:,} coded lines"
        ),
        "Line items with no usable HCPCS/CPT": f"{parse.line_items_without_code:,}",
        **{reason.capitalize(): f"{count:,}" for reason, count in parse.exclusions.items()},
        "Unreadable files": f"{len(parse.unreadable_files):,}",
    }
    facts_html = "".join(
        f"<dt>{html.escape(key)}</dt><dd>{html.escape(value)}</dd>" for key, value in facts.items()
    )
    return f"""
<h2>Observed utilization ({kind})</h2>
<dl class="facts">{facts_html}</dl>
<h2>Observed mix repriced at each payer's contracted rates ({kind})</h2>
{_html_table(source.repriced, _reprice_columns(source.repriced), _REPRICE_FORMATS)}
<h2>Top codes by volume ({kind})</h2>
{_html_table(source.utilization.head(top_n), _VOLUME_COLUMNS, _VOLUME_FORMATS)}
"""


def render_html(
    result: AuditResult,
    top_n: int = _TOP_N,
    sources: Sequence[UtilizationAudit] = (),
) -> str:
    facts = header_facts(result)
    join = result.join
    join_rate = facts["join_rate"]
    columns, formats = _payer_columns(result.config.group_by)

    fact_rows = {
        "MRF file": f"{facts['mrf_file']} ({facts['mrf_shape']})",
        "Hospital": facts["hospital"],
        "MRF last updated": (
            f"{facts['mrf_last_updated']} (template v{facts['mrf_template_version']})"
        ),
        "RVU file": f"{facts['rvu_file']} (year {facts['rvu_year']})",
        "Medicare conversion factor": (
            f"{facts['medicare_conversion_factor']} [{facts['conversion_factor_source']}]"
        ),
        "RVU basis": facts["rvu_basis"],
        "Grouping": facts["group_by"],
        "billing_class filter": facts["billing_class_filter"],
        "Rows": (
            f"{facts['normalized_rows']:,} normalized / {facts['in_scope_rows']:,} CPT-HCPCS in "
            f"scope / {facts['matched_rows']:,} matched / {facts['counted_rows']:,} in denominator"
        ),
        "Join rate": f"{join_rate:.1%}" if join_rate is not None else "n/a",
        "Geographic adjustment": "none - national RVUs, no GPCI locality factors",
        "Generated": datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC"),
    }
    facts_html = "".join(
        f"<dt>{html.escape(key)}</dt><dd>{html.escape(str(value))}</dd>"
        for key, value in fact_rows.items()
    )
    warnings_html = "".join(
        f"<div class='warn'>{html.escape(warning)}</div>" for warning in result.warnings
    )

    exclusion_items = [
        f"<li><strong>{count:,}</strong> {html.escape(EXCLUSION_LABELS[key])}</li>"
        for key, count in join.exclusions.items()
        if count
    ]
    if result.parse.excluded_no_dollar:
        exclusion_items.append(
            f"<li><strong>{result.parse.excluded_no_dollar:,}</strong> charges stated as a "
            "percentage or algorithm rather than a dollar amount</li>"
        )
    if result.parse.excluded_no_code:
        exclusion_items.append(
            f"<li><strong>{result.parse.excluded_no_code:,}</strong> charges with no usable "
            "billing code</li>"
        )
    if join.out_of_scope_by_code_type:
        detail = ", ".join(
            f"{html.escape(code_type)}={count:,}"
            for code_type, count in sorted(
                join.out_of_scope_by_code_type.items(), key=lambda item: -item[1]
            )
        )
        exclusion_items.append(f"<li>non-joinable code types: {detail}</li>")

    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Payer rate audit - {html.escape(facts["mrf_file"])}</title>
<style>{_CSS}</style></head><body>
<h1>Payer rate audit</h1>
<p class="sub">Negotiated dollars per RVU, by payer. Hospital outpatient rates: the methodology
transfers to physician-office economics, the dollars do not.</p>
<dl class="facts">{facts_html}</dl>
{warnings_html}
<h2>Exclusions</h2>
<ul class="exclusions">{"".join(exclusion_items) or "<li>none</li>"}</ul>
<h2>Effective conversion factor by payer</h2>
{_html_table(result.payer_table, columns, formats)}
<h2>Unmatched CPT/HCPCS codes ({len(join.unmatched_codes):,} distinct)</h2>
{
        _html_table(
            join.unmatched_codes.head(top_n),
            {"code": "Code", "code_type": "Type", "rows": "Rows", "description": "MRF description"},
            {"rows": ",d"},
        )
    }
<h2>Cash beats contract ({len(result.cash_beats_contract):,} rows)</h2>
{
        _html_table(
            result.cash_beats_contract.head(top_n),
            {
                "payer_name": "Payer",
                "plan_name": "Plan",
                "code": "Code",
                "discounted_cash": "Cash $",
                "negotiated_dollar": "Negotiated $",
                "gap": "Gap $",
            },
            {"discounted_cash": ",.2f", "negotiated_dollar": ",.2f", "gap": ",.2f"},
        )
    }
<h2>Spread by code ({len(result.spread):,} codes)</h2>
{
        _html_table(
            result.spread.head(top_n),
            {
                "code": "Code",
                "code_type": "Type",
                "payers": "Payers",
                "min_dollar": "Min $",
                "min_payer": "Min payer",
                "max_dollar": "Max $",
                "max_payer": "Max payer",
                "gap": "Gap $",
            },
            {"min_dollar": ",.2f", "max_dollar": ",.2f", "gap": ",.2f", "payers": ",d"},
        )
    }
{"".join(_source_html(source, top_n) for source in sources)}
<footer>Generated by payer-rate-audit. CPT descriptors are reproduced from the source MRF only;
no AMA-licensed descriptor content is distributed with this tool.</footer>
</body></html>
"""
