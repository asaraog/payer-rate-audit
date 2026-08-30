# payer-rate-audit

[![CI](https://github.com/asaraog/payer-rate-audit/actions/workflows/ci.yml/badge.svg)](https://github.com/asaraog/payer-rate-audit/actions/workflows/ci.yml)

**Which payer actually pays best?**

Dollar totals can't answer that: different payers cover different patients getting
different services. This tool divides what each payer pays by the RVUs of the work
(relative value units — Medicare's public per-code work weights), so every payer becomes one comparable
number — **dollars per unit of work, as a multiple of Medicare.**

## Real-life example

Community Hospital (Fairfax, MO), from their
[price transparency page](https://fairfaxmed.com/price-transparency/):

```bash
curl -LO https://fairfaxmed.com/wp-content/uploads/2026/03/STANDARD-CHARGES-DOWNLOAD-2026-01-01.csv
payer-rate-audit STANDARD-CHARGES-DOWNLOAD-2026-01-01.csv --group-by payer
```

```
Payer                      $ per RVU   x Medicare
UNITED HEALTHCARE PPO      142.31      4.26
CIGNA COMMERCIAL PPO       129.78      3.89
AETNA COMMERCIAL PPO       121.26      3.63
BCBS COMMERCIAL PPO        104.85      3.14
AMBETTER PPO               64.21       1.92
```

Same hospital, same procedures: a 2.2x gap between the best and worst contract.

## Install

Python 3.11+.

```bash
git clone https://github.com/asaraog/payer-rate-audit && cd payer-rate-audit
pip install -e .
python scripts/fetch_rvu.py      # downloads the current Medicare RVU file
payer-rate-audit your_hospital_file.csv
```

Reads any hospital's CMS price transparency file — all three official shapes
(CSV tall, CSV wide, JSON), detected automatically.

## It also reports

- **Cash beats contract** — codes where the cash price undercuts a payer's rate
- **Spread** — biggest price gaps per code across payers
- **Reprice your own mix** — `--era dir/` reads X12 835 remittance files (what a
  practice's clearinghouse already delivers) and shows what your actual services
  would have paid at each payer's posted rates; `--eob dir/` does the same from
  FHIR ExplanationOfBenefit files
- Every number comes with its denominator: rows counted, rows excluded and why,
  join rate. Nothing is dropped silently.

## Limits

- Hospital files carry hospital outpatient rates — higher than physician-office
  rates.
- National RVUs, no locality adjustment (but useful when comparing payers at the
  same facility).
- Repricing is arithmetic on posted rates — not an adjudication model.
- A code's RVUs depend on who pays the overhead, so professional and
  institutional rows use different RVU totals. Two payers can post the same
  dollar and score differently because they cost different amounts of RVUs.
- Rows priced as a percentage or algorithm are excluded and counted, never
  converted to invented dollars.

No PHI anywhere: inputs are public files and synthetic fixtures. CPT descriptors
(AMA-copyrighted) are never committed. Tests: `pytest` (95). BSD 3-Clause license.
