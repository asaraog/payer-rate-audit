# Assumptions

Every judgment call this tool makes about clinical-finance semantics, with the reasoning,
where it lives in the code, and how to overturn it. Nothing here is settled science; each
one is a lever a reviewer can pull in one pass.

---

## 1. Facility vs non-facility PE RVU

**Choice: `rvu_basis = "auto"`.** Rows whose MRF `billing_class` is `professional` are
priced on the **facility** total RVU (`fac_total`); everything else — `institutional`,
`both`, or blank — is priced on the **non-facility** total (`nonfac_total`).

**Why.** The practice-expense RVU answers "who owns the overhead". A professional-component
charge billed alongside a hospital's facility charge is, by definition, care delivered in a
facility whose overhead the hospital bills separately, which is exactly the facility PE
scenario. An institutional charge covers that overhead itself, so the non-facility total —
the global, all-in figure — is the closer analogue. Blank defaults to non-facility because
it is the larger denominator, which yields the *conservative* (lower) conversion factor
rather than a flattering one.

**Caveat.** This is the single most consequential assumption in the tool. Non-facility
totals exceed facility totals substantially for office-based procedures, so a payer whose
rows are mostly institutional gets a systematically lower effective CF than one whose rows
are professional. Compare payers within a file; be careful comparing across files with
different billing-class mixes.

**Override.** `[audit].rvu_basis = "facility" | "nonfacility"`, or `--rvu-basis`. Both
force one column for every row, which makes cross-payer comparison strictly apples to
apples at the cost of being wrong about overhead in one direction.

*Code: `_rvu_basis_column` in `src/payer_rate_audit/metrics.py`.*

---

## 2. Modifier 26 / TC

**Choice: modifiers `26`, `TC` and `53` select the matching PPRRVU row for the code; every
other modifier is ignored for RVU lookup. If a pricing modifier has no dedicated PPRRVU
row, the row falls back to the code's base row and the fallback is recorded on the row
(`modifier_fallback`).**

**Why.** The PPRRVU file itself carries separate priced rows for `26` and `TC`, with real
RVU splits — pricing a professional-component charge at global RVUs would inflate the
denominator by the technical component and understate the payer's rate by a wide margin,
so those must be honored. `53` (discontinued procedure) likewise appears as a distinct
priced row for some codes. Everything else in the modifier field — `50`, `59`, `LT`, `RT`,
`GA`, and the rest — is a payment-adjustment or informational modifier with no separate RVU
row; applying an adjustment percentage to the denominator would be inventing data.

**Caveat.** Bilateral (`50`) and multiple-procedure reductions genuinely change what a payer
pays, and the numerator here includes that adjusted dollar while the denominator uses
unadjusted RVUs. Files that carry many `50` rows will show a slightly depressed effective
CF.

**Override.** Edit `PRICING_MODIFIERS` in `src/payer_rate_audit/rvu.py`.

---

## 3. `billing_class`

**Choice: no filter by default; `--billing-class professional` is a flag.** When the filter
is set, rows whose `billing_class` is `both` are kept alongside the requested class, and
filtered-out rows are counted in the exclusion table rather than dropped.

**Why.** Filtering to `professional` gets closest to physician-office economics and is the
right call for a practice benchmarking its own contracts — but many hospital MRFs populate
`billing_class` sparsely, inconsistently, or not at all, and a default filter would
silently reduce a 200,000-row file to a few hundred rows and report a confident number over
a denominator nobody looked at. The default therefore uses everything and prints what it
used; the analyst who knows the file opts in.

**Override.** `[audit].billing_class_filter = "professional"` or `--billing-class`.

---

## 4. Multiple plans per payer

**Choice: report per plan (`group_by = "plan"`), with `--group-by payer` to aggregate.**

**Why.** A payer's Medicaid, Medicare Advantage, exchange and commercial plans are separate
contracts at genuinely different rates — collapsing them produces an average that describes
no contract that exists, and the spread between a payer's own plans is often the most
actionable thing in the file. Aggregating is offered because some files put a single plan
name on everything, or leave it blank, in which case per-plan and per-payer coincide.

**Caveat.** Per-plan rows are computed over smaller denominators. The row count and total
RVUs are printed on every row precisely so a thin plan is visible as thin.

**Override.** `[audit].group_by = "payer"` or `--group-by payer`.

---

## 5. `negotiated_percentage` / `negotiated_algorithm` rows

**Choice: excluded from every dollar aggregate, and counted and warned about explicitly.**

**Why.** CMS requires a dollar amount where one can be expressed, but real files still
carry percentage-of-charge and algorithm rows. Resolving "45% of gross charge" into a
dollar figure would require asserting the hospital's own gross charge is the correct base
and that no carve-outs apply — an inference presented as data. The tool refuses and instead
tells you how many rows it declined, because that count is itself a finding: a payer whose
rates are mostly formulas cannot be compared on rate, and the report says so rather than
quietly reporting the conversion factor of the minority of its rows that happened to carry
a dollar.

**Where to see it.** The exclusion table's "charge stated as percentage or algorithm" line
and a warning in every output format. Percentage and algorithm text is preserved in the
normalized `methodology` column.

---

## 6. Geographic adjustment (GPCI)

**Choice: ignored. National RVUs, no locality adjustment.** Stated in the header of every
report.

**Why.** PPRRVU RVUs are national; Medicare payment multiplies each of the three RVU
components by its locality GPCI before applying the conversion factor, so a locality-true
figure would need the GPCI file and an assumption about which locality the hospital sits
in. For the question this tool answers — how do payers compare to each other, and to
Medicare, at one hospital — the locality factor is common to every payer in the file and
cancels out of the comparison.

**Caveat.** The **ratio to Medicare** is therefore a national-rate ratio. In a high-GPCI
market (San Francisco, New York) true local Medicare payment is above the national figure,
so the ratios printed here overstate the multiple; in a low-GPCI market they understate it.
Relative ordering of payers is unaffected.

---

## Smaller calls, for completeness

- **Payable status codes.** Only PFS status codes `A`, `R` and `T` enter the denominator,
  per the PPRRVU record layout's statement that these are the codes used for payment.
  Configurable at `[rvu].payable_status_codes`. Rows excluded for status are counted.
- **Multiple code types on one item.** JSON items and CSV rows can carry several
  code/type pairs (a CDM number, a revenue code, a HCPCS code). The parser prefers CPT,
  then HCPCS, and emits **one** row per payer charge, so a negotiated dollar is never
  double-counted across an item's codes. Items whose only codes are modifiers are counted
  as skipped rather than dropped silently.
- **Zero-RVU rows.** A code that matches, is separately payable, and carries a total RVU of
  zero contributes nothing to a denominator and would silently inflate the CF, so it is
  excluded and counted on its own exclusion line.
- **Spread.** Computed only over codes priced by more than one payer; a single-payer code
  has a spread of zero by construction and would crowd the ranking.
- **EOB repricing rate.** When a payer has several MRF rows for one observed code (different
  settings, modifiers, plans), repricing uses the **mean** negotiated dollar for that payer
  and code rather than min, max, or the first row: no observed-setting information exists in
  an EOB line to pick among them, and a mean does not systematically flatter or punish a
  payer. Codes a payer does not publish are excluded from its repriced total and counted on
  its own `codes unpriced` column — a payer cannot be charged with a rate it never posted.
- **EOB coding fallback.** `item.productOrService` codings are read by system (CPT, HCPCS);
  where a coding carries no system at all, a bare five-digit numeric is treated as CPT and a
  letter-plus-four-digits as HCPCS Level II. Anything else counts as an uncoded line item and
  is reported.
- **Join rate.** Matched / in-scope CPT-HCPCS rows. Below `[audit].min_join_rate` (0.60)
  the tool warns loudly and exits `3`. Out-of-scope code types are excluded from the
  denominator of this rate — a DRG-priced file is not a broken join.
