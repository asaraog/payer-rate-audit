"""Join normalized MRF rows to the PFS relative value file and compute rates.

Nothing is dropped silently. Every row that leaves the denominator is counted
and named in an exclusion bucket, and every aggregate states the row count it
was computed from.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pandas as pd

from .config import Config
from .mrf import ParseResult
from .rvu import PRICING_MODIFIERS, RVUTable

# Only these code types describe a physician-fee-schedule service. DRG, APC,
# NDC, ICD and revenue codes are out of scope for an RVU join by construction.
JOINABLE_CODE_TYPES = frozenset({"CPT", "HCPCS"})

EXCLUSION_LABELS = {
    "not_joinable_code_type": "code type cannot join to the PFS (DRG/APC/NDC/ICD/revenue/local)",
    "billing_class_filtered": "filtered out by billing_class filter",
    "unmatched_code": "CPT/HCPCS code absent from the PPRRVU file",
    "not_separately_payable": "PFS status code is not separately payable",
    "zero_rvu": "matched and payable but total RVU is zero",
}


@dataclass
class JoinResult:
    """MRF rows annotated with RVU data plus the audit trail of what left."""

    frame: pd.DataFrame
    rvu_basis: str
    in_scope_rows: int = 0
    matched_rows: int = 0
    counted_rows: int = 0
    exclusions: dict[str, int] = field(default_factory=dict)
    out_of_scope_by_code_type: dict[str, int] = field(default_factory=dict)
    unmatched_codes: pd.DataFrame = field(default_factory=pd.DataFrame)

    @property
    def join_rate(self) -> float | None:
        if not self.in_scope_rows:
            return None
        return self.matched_rows / self.in_scope_rows

    def join_rate_is_low(self, threshold: float) -> bool:
        return self.join_rate is not None and self.join_rate < threshold


@dataclass
class AuditResult:
    parse: ParseResult
    join: JoinResult
    config: Config
    rvu_source_file: str
    rvu_year: int | None
    payer_table: pd.DataFrame
    cash_beats_contract: pd.DataFrame
    spread: pd.DataFrame
    warnings: list[str] = field(default_factory=list)


def _pricing_modifier(modifiers: str) -> str:
    """Return the modifier that selects a distinct PPRRVU row, if any."""
    if not modifiers:
        return ""
    present = {part.strip().upper() for part in modifiers.split(",") if part.strip()}
    for modifier in PRICING_MODIFIERS:
        if modifier in present:
            return modifier
    return ""


def _rvu_basis_column(frame: pd.DataFrame, basis: str) -> pd.Series:
    """Per row, the PPRRVU total-RVU column that prices it."""
    if basis == "facility":
        return pd.Series("fac_total", index=frame.index)
    if basis == "nonfacility":
        return pd.Series("nonfac_total", index=frame.index)
    # auto: a professional-class charge is the physician's own fee for a service
    # rendered in a hospital, which is what the facility total prices. Anything
    # else is (or includes) the hospital's technical charge, whose closest PFS
    # analogue is the global, non-facility total. See ASSUMPTIONS.md, decision 1.
    professional = frame["billing_class"].fillna("").str.lower().eq("professional")
    return pd.Series("nonfac_total", index=frame.index).where(~professional, "fac_total")


def join_rvu(parse: ParseResult, rvu: RVUTable, config: Config) -> JoinResult:
    frame = parse.frame.copy()
    exclusions: dict[str, int] = {key: 0 for key in EXCLUSION_LABELS}

    frame["joinable_code_type"] = frame["code_type"].str.upper().isin(JOINABLE_CODE_TYPES)
    out_of_scope = frame.loc[~frame["joinable_code_type"], "code_type"]
    out_of_scope_counts = (
        out_of_scope.replace("", "(blank)").value_counts().to_dict() if len(out_of_scope) else {}
    )
    exclusions["not_joinable_code_type"] = int((~frame["joinable_code_type"]).sum())

    if config.billing_class_filter:
        wanted = config.billing_class_filter
        keep = frame["billing_class"].str.lower().isin({wanted, "both"})
        exclusions["billing_class_filtered"] = int((~keep & frame["joinable_code_type"]).sum())
        frame["in_scope"] = frame["joinable_code_type"] & keep
    else:
        frame["in_scope"] = frame["joinable_code_type"]

    frame["pricing_modifier"] = frame["modifiers"].map(_pricing_modifier)

    rvu_frame = rvu.frame
    keyed = rvu_frame.set_index(["hcpcs", "modifier"])
    base = rvu_frame[rvu_frame["modifier"] == ""].set_index("hcpcs")
    rvu_fields = ["status_code", "work_rvu", "nonfac_pe_rvu", "fac_pe_rvu", "mp_rvu",
                  "nonfac_total", "fac_total", "pctc_indicator", "global_days"]

    with_modifier = frame.join(
        keyed[rvu_fields],
        on=["code", "pricing_modifier"],
        how="left",
    )
    fallback = frame.join(base[rvu_fields].add_suffix("__base"), on="code", how="left")
    # A pricing modifier that has no dedicated PPRRVU row falls back to the base
    # row for the code, and the fallback is recorded.
    missing = with_modifier["status_code"].isna()
    for column in rvu_fields:
        with_modifier.loc[missing, column] = fallback.loc[missing, f"{column}__base"]
    frame = with_modifier
    frame["rvu_modifier_used"] = frame["pricing_modifier"].where(~missing, "")
    frame["modifier_fallback"] = missing & (frame["pricing_modifier"] != "")

    frame["matched"] = frame["in_scope"] & frame["status_code"].notna()
    exclusions["unmatched_code"] = int((frame["in_scope"] & ~frame["matched"]).sum())

    payable = frame["status_code"].fillna("").str.upper().isin(config.payable_status_codes)
    frame["separately_payable"] = frame["matched"] & payable
    exclusions["not_separately_payable"] = int(
        (frame["matched"] & ~frame["separately_payable"]).sum()
    )

    basis_column = _rvu_basis_column(frame, config.rvu_basis)
    frame["rvu_column_used"] = basis_column
    frame["total_rvu"] = pd.to_numeric(
        frame["fac_total"].where(basis_column.eq("fac_total"), frame["nonfac_total"]),
        errors="coerce",
    )

    frame["counted"] = frame["separately_payable"] & frame["total_rvu"].gt(0)
    exclusions["zero_rvu"] = int((frame["separately_payable"] & ~frame["counted"]).sum())

    unmatched = (
        frame.loc[frame["in_scope"] & ~frame["matched"], ["code", "code_type", "description"]]
        .assign(rows=1)
        .groupby(["code", "code_type"], as_index=False)
        .agg(rows=("rows", "sum"), description=("description", "first"))
        .sort_values("rows", ascending=False)
        .reset_index(drop=True)
    )

    return JoinResult(
        frame=frame,
        rvu_basis=config.rvu_basis,
        in_scope_rows=int(frame["in_scope"].sum()),
        matched_rows=int(frame["matched"].sum()),
        counted_rows=int(frame["counted"].sum()),
        exclusions=exclusions,
        out_of_scope_by_code_type={str(k): int(v) for k, v in out_of_scope_counts.items()},
        unmatched_codes=unmatched,
    )


def _group_keys(config: Config) -> list[str]:
    return ["payer_name", "plan_name"] if config.group_by == "plan" else ["payer_name"]


def payer_table(join: JoinResult, config: Config) -> pd.DataFrame:
    """Effective conversion factor and ratio to Medicare, per payer (and plan)."""
    keys = _group_keys(config)
    frame = join.frame
    counted = frame[frame["counted"]]
    if counted.empty:
        return pd.DataFrame(
            columns=[*keys, "rows_counted", "rows_seen", "rows_excluded", "negotiated_dollars",
                     "total_rvus", "effective_cf", "ratio_to_medicare"]
        )

    aggregated = (
        counted.groupby(keys, dropna=False)
        .agg(
            rows_counted=("negotiated_dollar", "size"),
            negotiated_dollars=("negotiated_dollar", "sum"),
            total_rvus=("total_rvu", "sum"),
        )
        .reset_index()
    )
    seen = frame.groupby(keys, dropna=False).size().rename("rows_seen").reset_index()
    aggregated = aggregated.merge(seen, on=keys, how="left")
    aggregated["rows_excluded"] = aggregated["rows_seen"] - aggregated["rows_counted"]
    aggregated["effective_cf"] = aggregated["negotiated_dollars"] / aggregated["total_rvus"]
    aggregated["ratio_to_medicare"] = aggregated["effective_cf"] / config.conversion_factor
    return aggregated.sort_values("effective_cf", ascending=False).reset_index(drop=True)


def cash_beats_contract(join: JoinResult) -> pd.DataFrame:
    """Rows where the hospital's discounted cash price undercuts the contract."""
    frame = join.frame
    mask = (
        frame["discounted_cash"].notna()
        & frame["negotiated_dollar"].notna()
        & (frame["discounted_cash"] < frame["negotiated_dollar"])
    )
    columns = ["payer_name", "plan_name", "code", "code_type", "description",
               "discounted_cash", "negotiated_dollar"]
    result = frame.loc[mask, columns].copy()
    if result.empty:
        return result.assign(gap=pd.Series(dtype=float), cash_share=pd.Series(dtype=float))
    result["gap"] = result["negotiated_dollar"] - result["discounted_cash"]
    result["cash_share"] = result["discounted_cash"] / result["negotiated_dollar"]
    return result.sort_values("gap", ascending=False).reset_index(drop=True)


def spread(join: JoinResult) -> pd.DataFrame:
    """Per code, the min and max negotiated dollar across payers, ranked by gap.

    Codes priced by a single payer have no spread to report, so they are left
    out rather than listed with a zero gap.
    """
    frame = join.frame[join.frame["negotiated_dollar"].notna()]
    multi_payer = frame.groupby(["code", "code_type"])["payer_name"].transform("nunique") > 1
    frame = frame[multi_payer]
    if frame.empty:
        return pd.DataFrame(
            columns=["code", "code_type", "description", "payers", "min_dollar", "min_payer",
                     "max_dollar", "max_payer", "gap", "ratio"]
        )
    rows = []
    for (code, code_type), group in frame.groupby(["code", "code_type"], dropna=False):
        low = group.loc[group["negotiated_dollar"].idxmin()]
        high = group.loc[group["negotiated_dollar"].idxmax()]
        rows.append(
            {
                "code": code,
                "code_type": code_type,
                "description": low["description"],
                "payers": group["payer_name"].nunique(),
                "min_dollar": float(low["negotiated_dollar"]),
                "min_payer": low["payer_name"],
                "max_dollar": float(high["negotiated_dollar"]),
                "max_payer": high["payer_name"],
            }
        )
    result = pd.DataFrame(rows)
    result["gap"] = result["max_dollar"] - result["min_dollar"]
    result["ratio"] = result["max_dollar"] / result["min_dollar"].replace(0, pd.NA)
    return result.sort_values("gap", ascending=False).reset_index(drop=True)


def build_warnings(parse: ParseResult, join: JoinResult, config: Config) -> list[str]:
    warnings: list[str] = []
    rate = join.join_rate
    if rate is None:
        warnings.append(
            "No CPT or HCPCS rows were in scope, so no conversion factor could be computed. "
            "This MRF may be entirely DRG/revenue-code priced."
        )
    elif rate < config.min_join_rate:
        warnings.append(
            f"Join rate is {rate:.1%}, below the {config.min_join_rate:.0%} threshold. "
            f"Only {join.matched_rows:,} of {join.in_scope_rows:,} CPT/HCPCS rows matched the "
            f"PPRRVU file. Something is wrong with the code column, the RVU year, or the file "
            f"itself; treat the conversion factors below as unreliable."
        )
    if parse.excluded_no_dollar:
        warnings.append(
            f"{parse.excluded_no_dollar:,} payer-specific charges were expressed as a percentage "
            "or an algorithm rather than a dollar amount and are excluded from every aggregate."
        )
    if join.counted_rows == 0 and rate is not None:
        warnings.append("No rows survived to the denominator; every aggregate below is empty.")
    return warnings


def audit(parse: ParseResult, rvu: RVUTable, config: Config) -> AuditResult:
    join = join_rvu(parse, rvu, config)
    return AuditResult(
        parse=parse,
        join=join,
        config=config,
        rvu_source_file=rvu.source_file,
        rvu_year=rvu.year,
        payer_table=payer_table(join, config),
        cash_beats_contract=cash_beats_contract(join),
        spread=spread(join),
        warnings=build_warnings(parse, join, config),
    )


def header_facts(result: AuditResult) -> dict[str, Any]:
    """The denominators every report must state."""
    join = result.join
    return {
        "mrf_file": result.parse.source_file,
        "mrf_shape": result.parse.shape.value,
        "hospital": result.parse.metadata.get("hospital_name", "(not stated in file)"),
        "mrf_last_updated": result.parse.metadata.get("last_updated_on", "(not stated in file)"),
        "mrf_template_version": result.parse.metadata.get("version", "(not stated in file)"),
        "rvu_file": result.rvu_source_file,
        "rvu_year": result.rvu_year,
        "medicare_conversion_factor": result.config.conversion_factor,
        "conversion_factor_source": result.config.conversion_factor_source,
        "rvu_basis": result.config.rvu_basis,
        "group_by": result.config.group_by,
        "billing_class_filter": result.config.billing_class_filter or "(none)",
        "normalized_rows": result.parse.row_count,
        "in_scope_rows": join.in_scope_rows,
        "matched_rows": join.matched_rows,
        "counted_rows": join.counted_rows,
        "join_rate": join.join_rate,
    }
