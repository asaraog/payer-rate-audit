# Assumptions

The judgment calls this tool makes, each overturnable via config or one small edit.

## The six big ones

| # | Decision | Choice | Why (one line) | Override |
|---|---|---|---|---|
| 1 | Facility vs non-facility RVU | `auto`: professional rows use facility total, everything else non-facility | Professional-alongside-facility care is the facility-PE scenario; blank rows get the larger denominator, which is the conservative CF | `[audit].rvu_basis` / `--rvu-basis` |
| 2 | Modifiers | `26`/`TC`/`53` select their own PPRRVU row; all others ignored for lookup, with recorded fallback | Those three have real RVU splits in the file; inventing adjustment percentages for the rest would be making up data | `PRICING_MODIFIERS` in `rvu.py` |
| 3 | billing_class filter | Off by default | Show the whole file; filtering is one flag | `--billing-class professional` |
| 4 | Plans | Report per plan | Payers price plans differently; aggregation hides it | `--group-by payer` |
| 5 | Percentage/algorithm charges | Excluded, counted, warned | Converting "120% of gross" to dollars would trust the hospital's own base; not our number to invent | none — deliberate |
| 6 | GPCI locality | Ignored; national RVUs | The locality factor is common to every payer in one file and cancels in the comparison | none — deliberate |

**#1 is the most consequential.** Non-facility totals run well above facility totals for
office procedures, so payers with mostly-institutional rows score systematically lower.
Compare payers within one file; be careful across files.

## Smaller calls

- **Status codes**: only `A`/`R`/`T` (the payable ones) enter denominators. Config: `[rvu].payable_status_codes`.
- **Multi-coded items**: prefer CPT, then HCPCS; one row per payer charge, never double-counted.
- **Zero-RVU rows**: excluded and counted (they'd silently inflate the CF).
- **Spread**: only codes priced by 2+ payers.
- **Repricing rate**: mean of a payer's rows for the code (no setting info exists in a claim line to pick better); unpublished codes go to `codes unpriced`, never guessed.
- **EOB codes without a system**: bare 5 digits = CPT, letter+4 = HCPCS; anything else counted as uncoded.
- **835 reversals**: negative-dollar lines get negated units so they net out; counted separately.
- **835 scope**: service lines (`SVC` with `HC` qualifier) only. Claim-level CAS, PLB, COB not applied — "actually paid" is the sum of lines, not the check amount. Allowed amount left empty rather than inferred.
- **Join rate**: matched / in-scope rows; below `[audit].min_join_rate` (0.60) the run warns loudly and exits 3. Out-of-scope code types (DRG etc.) don't count against it.

Everything excluded for any reason above appears in the report's exclusion table with its own count.
