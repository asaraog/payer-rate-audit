# payer-rate-audit

**Compare payers on rate alone, with case mix normalized out.**

A practice comparing two contracts cannot compare dollar totals: the two payers saw
different patients doing different things. Nor can it compare a handful of codes, because
a payer that pays well on 99213 may pay badly on everything else. The comparable number is
the **effective conversion factor** — the dollars a payer pays per unit of physician work,
practice expense and malpractice risk:

```
effective CF = sum(negotiated_dollar) / sum(total_RVU)
```

RVUs are the case-mix denominator Medicare already publishes. Divide by them and the
service mix cancels. What is left is the rate. Expressed as a multiple of the Medicare
conversion factor, one number tells you where a payer sits.

This tool computes that number for every payer in a hospital's CMS price transparency
machine-readable file (MRF), by joining the file to the CMS Physician Fee Schedule
relative value file (PPRRVU) on HCPCS/CPT.

## What these numbers are, and are not

**These are hospital MRFs, so the rates are hospital outpatient rates, not independent
physician office rates.** A hospital outpatient contract bundles facility overhead that an
office visit in a physician's own suite does not, which is why the multiples of Medicare
you will see here run high. **The methodology transfers; the specific dollars do not.**
Point the same code at a payer transparency file, an ASC file, or your own fee schedule
and the effective conversion factor means what you expect.

Two further limits, stated up front and repeated in the report header:

- RVUs are **national**. Actual Medicare payment applies GPCI locality adjustment; this
  tool does not. Comparisons between payers in the same market are unaffected — the
  locality factor is common to all of them and cancels in the ratio.
- Every aggregate is printed **with its denominator** — rows counted, rows excluded, total
  RVUs, and the join rate. A conversion factor computed over 40 rows of a 200,000-row file
  is not a contract summary, and the report will show you that it was 40.

## Install and run

Python 3.11+.

```bash
git clone <this repo> && cd payer-rate-audit
pip install -e ".[dev]"          # one command, clean clone
python scripts/fetch_rvu.py      # downloads the current PPRRVU file into data/
payer-rate-audit path/to/hospital_standardcharges.csv
```

`fetch_rvu.py` discovers the current quarterly release for the year in `config.toml` from
cms.gov, verifies the download is a valid ZIP, extracts the non-QPP PPRRVU CSV into
`data/`, and re-parses it to prove the layout still matches. It is idempotent: re-running
skips an existing good file unless you pass `--force`.

Useful flags:

```bash
payer-rate-audit MRF --html report.html      # one self-contained file, no CDN, no assets
payer-rate-audit MRF --group-by payer        # aggregate a payer's plans
payer-rate-audit MRF --billing-class professional
payer-rate-audit MRF --rvu-basis facility    # override the auto facility/non-facility pick
payer-rate-audit MRF --csv-out out/          # payer table, spread, cash flags, unmatched
```

Exit code `3` means the join rate fell below `[audit].min_join_rate` (default 0.60) — the
run produced output, but do not trust it before reading the exclusion counts.

## What it reads

**MRF, all three CMS shapes, detected automatically** — CSV tall (one row per item per
payer per plan), CSV wide (payer columns named
`standard_charge|<payer>|<plan>|negotiated_dollar`), and nested JSON
(`standard_charge_information[] -> code_information[] / standard_charges[] ->
payers_information[]`). Coded against the CMS v3.0 template and data dictionary in
[CMSgov/hospital-price-transparency](https://github.com/CMSgov/hospital-price-transparency),
not against a remembered column list. All three normalize to one table:

```
code, code_type, modifiers, description, billing_class, setting,
payer_name, plan_name, negotiated_dollar, gross_charge,
discounted_cash, min_negotiated, max_negotiated, methodology
```

**RVUs** — the current-year PPRRVU file from the CMS
[PFS relative value files](https://www.cms.gov/medicare/payment/fee-schedules/physician/pfs-relative-value-files)
page, parsed to `hcpcs, modifier, status_code, work_rvu, nonfac_pe_rvu, fac_pe_rvu,
mp_rvu, nonfac_total, fac_total, pctc_indicator, global_days`. The parser locates the
stacked CMS header by name and fails loudly if the layout changes rather than silently
producing empty columns.

**The Medicare conversion factor** is a required value in `config.toml`. It is published in
the annual PFS final rule, it changes every year, and from CY2026 there are two of them
(qualifying APM participant and not). It is not scraped and not hardcoded in source, so
you always know which number your ratios are against.

### Licensing note

The repo carries code, not licensed content. CPT **code numbers** are facts and appear in
the tiny synthetic test fixtures; CPT **long descriptors** are AMA-copyrighted. Neither the
PPRRVU file nor its descriptors are committed — `data/` is gitignored and populated by
`fetch_rvu.py` — and `rvu.py` drops descriptor columns on parse. Descriptions that appear
in output come from the hospital's own MRF.

## What it reports

1. **Effective conversion factor** per payer (and plan), sorted, over joined
   separately-payable rows only.
2. **Ratio to Medicare** — effective CF divided by the configured conversion factor.
3. **Cash beats contract** — every code where the hospital's discounted cash price is
   below what a payer negotiated.
4. **Spread** — per code, min and max negotiated dollar across payers, ranked by gap.

Plus the things that make those numbers checkable: source file name and detected shape,
hospital name, RVU file and year, join rate, and an exclusion table that accounts for
every row that did not reach the denominator — out-of-scope code types (DRG, APC, NDC,
ICD, revenue codes), CPT/HCPCS codes absent from the PPRRVU file, non-separately-payable
PFS status codes, zero-RVU rows, and charges stated as a percentage or algorithm instead
of a dollar amount. Unmatched codes are enumerated, never dropped in silence.

Verified end to end against real published MRFs in all three shapes as well as the CMS
example files; see `ASSUMPTIONS.md` for the domain calls made along the way, each of which
a reviewer can overturn in one pass.

## Tests

```bash
pytest        # synthetic fixtures, one per MRF shape, hand-authored and tiny
ruff check .
```

## Not in scope

No login, no database, no claim submission, no denial logic, no ML, no live payer API, no
PHI.

MIT licensed.
