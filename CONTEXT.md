# Context for agents working on this repo

Not shipped documentation — orientation for whoever (or whatever) picks this up next.
Written 2026-08-31.

## Why this exists

A medical practice wants to know which insurance contract actually pays best. Dollar
totals can't answer it: payers cover different patients getting different services. So
divide what each payer pays by the **RVUs** of the work (Medicare's public per-code work
weights) and every payer collapses to one comparable number — dollars per unit of work,
as a multiple of the Medicare conversion factor.

The author runs revenue cycle at a hand therapy clinic (claims, denials, appeals, payer
portals, CBCS-certified) and builds LLM systems professionally. This repo is the
intersection: the domain calls are his, the scaffolding was agent-built.

## How it was built, and what that means for you

Devin wrote the scaffold; it was then reviewed and corrected against billing reality.
**Every correction so far has been domain judgment, not code quality.** Assume the same
of your changes: the Python will be fine and the healthcare will be where you're wrong.

Corrections that have already happened, as a calibration set:
- Devin's first claims reader took **FHIR ExplanationOfBenefit** only. Standards-correct,
  but no independent practice can produce FHIR. Their data is **X12 835** remittances
  sitting in a clearinghouse mailbox. That became issue #4 and the `--era` adapter.
- Facility vs non-facility PE RVU is not a coin flip; it depends on who bills the
  overhead. See ASSUMPTIONS.md #1.
- Modifiers `26`/`TC`/`53` have their own PPRRVU rows and must be honored; every other
  modifier does not, and applying an invented adjustment percentage would be fabricating
  data. ASSUMPTIONS.md #2.

## The two halves

**Rate side (core).** One public input: a hospital's CMS-mandated price transparency
machine-readable file (MRF), joined to the CMS PPRRVU file on HCPCS. Needs nothing else —
no PHI, no credentials, no integration. This is what the README example runs.

**Claims side (optional).** Reprice the practice's *own* service mix at each payer's posted
rates. Needs a ledger of what was actually done: code, units, paid. Three possible readers,
same five-column internal table, engine untouched:

| Reader | Flag | Who can actually supply it |
|---|---|---|
| X12 835 | `--era` | Any practice with a clearinghouse. **The realistic one.** |
| FHIR EOB | `--eob` | Institutions now; practices partially after the 2027 CMS Provider Access rule, and never for workers' comp / auto / most commercial |
| PM-system export | not built | Everything including paper-remit payers, but every vendor's CSV differs — needs a column-mapping config. See issue #5 |

## Hard rules

1. **No PHI, ever.** Inputs are public files and hand-authored synthetic fixtures. No real
   835 belongs in this repo — every real one is PHI. The 835 parser must never read patient
   identifiers (`NM1*QC`, `CLP01`) into output; there is a test enforcing this.
2. **Never invent a dollar.** Percentage/algorithm-priced rows are excluded and counted,
   not converted. Same for modifier adjustments and allowed amounts that would require
   claim-level CAS logic.
3. **Nothing is dropped silently.** Every skipped row lands in a counted, printed exclusion
   bucket with a reason. Every aggregate prints its denominator.
4. **CPT descriptors are AMA-copyrighted.** Code numbers are facts and fine. The PPRRVU
   file is fetched at runtime into gitignored `data/`, never committed, and descriptor
   columns are dropped on parse.
5. **PRs only.** `main` is protected; CI runs ruff + pytest on 3.11/3.12/3.13. Merging is
   the human's click, not yours.

## Where things live

- `src/payer_rate_audit/mrf.py` — the three CMS MRF shapes (CSV tall, CSV wide, JSON), auto-detected
- `rvu.py` — PPRRVU parse, pricing-modifier logic
- `metrics.py` — the join and the four outputs
- `x12_835.py` / `eob.py` — the two claims readers
- `report.py` — CLI table + self-contained HTML
- `config.toml` — Medicare conversion factor (changes yearly, never hardcoded in source) and RVU basis
- `ASSUMPTIONS.md` — **read this before changing any calculation.** Six big domain calls in
  a table, each with its override.

## Verification status

Run end to end against a real published MRF (Community Hospital, Fairfax MO): 3,789 rows,
97.7% join rate, a 2.2x spread between best and worst payer contract, one excision code
ranging $318 to $4,977 across payers. 95 tests green.

## Open work

- Issue #5 — PM-system export adapter (paper-remit payers: workers' comp, auto, TPAs)
- Locality adjustment (GPCI) is deliberately not applied; the data is available in the same
  CMS zip and the parser already splits work/PE/MP components, so it is a small feature.
  Would make the "x Medicare" anchor local rather than national.
