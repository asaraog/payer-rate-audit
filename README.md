# payer-rate-audit

[![CI](https://github.com/asaraog/payer-rate-audit/actions/workflows/ci.yml/badge.svg)](https://github.com/asaraog/payer-rate-audit/actions/workflows/ci.yml)

**Compare payers on rate alone, with case mix normalized out.**

A practice comparing two contracts cannot compare dollar totals: the two payers saw
different patients doing different things. The comparable number is the **effective
conversion factor** — the dollars a payer pays per RVU, the unit of physician work
Medicare already publishes:

```
effective CF = sum(negotiated_dollar) / sum(total_RVU)
```

Divide by RVUs and the service mix cancels. What is left is the rate. Expressed as a
multiple of the Medicare conversion factor, one number tells you where a payer sits.

This tool computes that number for every payer in a hospital's CMS price transparency
machine-readable file (MRF), joined to the CMS Physician Fee Schedule relative value
file on HCPCS/CPT.

## Install and run

Python 3.11+.

```bash
git clone https://github.com/asaraog/payer-rate-audit && cd payer-rate-audit
pip install -e .
python scripts/fetch_rvu.py      # downloads the current PPRRVU file into data/
payer-rate-audit path/to/hospital_standardcharges.csv
```

All three CMS MRF shapes (CSV tall, CSV wide, JSON) are detected automatically.

Useful flags:

```bash
payer-rate-audit MRF --html report.html      # one self-contained file
payer-rate-audit MRF --group-by payer        # aggregate a payer's plans
payer-rate-audit MRF --csv-out out/          # payer table, spread, cash flags, unmatched
payer-rate-audit MRF --eob eobs/             # reprice your observed mix (below)
payer-rate-audit MRF --era era/              # same, from your 835 remittances
```

## Example: a real hospital

Run against Community Hospital (Fairfax, MO)'s published standard charges file,
from their [price transparency page](https://fairfaxmed.com/price-transparency/)
(3,789 rows, CSV tall, v2.0.0 template — 97.7% join rate):

```
curl -LO https://fairfaxmed.com/wp-content/uploads/2026/03/STANDARD-CHARGES-DOWNLOAD-2026-01-01.csv
payer-rate-audit STANDARD-CHARGES-DOWNLOAD-2026-01-01.csv --group-by payer
```

```
Payer                      Effective CF  x Medicare
UNITED HEALTHCARE PPO      142.31        4.26
CIGNA COMMERCIAL PPO       129.78        3.89
AETNA COMMERCIAL PPO       121.26        3.63
BCBS COMMERCIAL PPO        104.85        3.14
AMBETTER PPO               64.21         1.92
```

Same hospital, same procedures: a 2.2x gap between the best and worst contract.
One excision code (11426) spans $318 to $4,977 across payers. The multiples run
high because these are hospital outpatient rates (see Honest limits) — even the
Medicare Advantage plan in the file sits at 3.19x, where physician-office MA
would hug 1.0x.

## What it reports

1. **Effective conversion factor** per payer and plan, sorted.
2. **Ratio to Medicare** — effective CF over the conversion factor in `config.toml`.
3. **Cash beats contract** — codes where the discounted cash price is below a
   payer's negotiated rate.
4. **Spread** — per code, min and max negotiated dollar across payers.

Every aggregate prints its denominator: rows counted, rows excluded and why, and the
join rate. Unmatched codes are enumerated, never dropped in silence. Exit code `3`
means the join rate fell below the configured floor — read the exclusions before
trusting the run.

### Optional: reprice your observed service mix

`--eob PATH` reads CARIN Blue Button-conformant FHIR R4 `ExplanationOfBenefit` files
and answers: *given what we actually did, what would each payer have paid, and how
does that compare to what we were paid?* Repricing is arithmetic on published rates,
not an adjudication model — no coverage rules, bundling, or modifier pricing.

`--era PATH` does the same from X12 835 remittance advice, and is the input a practice
actually has: the clearinghouse mailbox already holds the whole electronic book of
business, today, with no integration project. FHIR EOB remains for institutions and the
2027 Provider Access path; both flags can be given at once and are reported separately.
Scope is the service line — claim-level `CAS`, `PLB`, reversals beyond netting, and COB
are out, and every line skipped for any reason is counted in the exclusions like
everything else here. 835 files are PHI: no patient identifier (`NM1*QC`, `CLP01`) is
ever read into the output, and no real 835 belongs in this repo.

## Honest limits

- **These are hospital MRFs**, so rates are hospital outpatient, not independent
  physician office. The methodology transfers; the specific dollars do not.
- RVUs are **national** — no GPCI locality adjustment. Same-market payer
  comparisons are unaffected; the factor cancels in the ratio.
- The Medicare conversion factor changes yearly and lives in `config.toml`, never
  hardcoded.

Domain decisions (facility vs non-facility RVUs, plan aggregation, billing-class
filtering) are recorded in [ASSUMPTIONS.md](ASSUMPTIONS.md), each overturnable in
one pass.

## Licensing note

The repo carries code, not licensed content. CPT long descriptors are
AMA-copyrighted: neither the PPRRVU file nor its descriptors are committed —
`data/` is gitignored and populated by `fetch_rvu.py`.

## Tests

```bash
pytest        # 80 tests, tiny hand-authored fixtures
```

## Not in scope

No login, no database, no claim submission, no denial logic, no ML, no live payer
API, no PHI. MIT licensed.
