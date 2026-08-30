# payer-rate-audit

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
```

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
