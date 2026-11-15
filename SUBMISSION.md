# Funding Radar: TinyFish Student Bounty Drop 002

Repo: https://github.com/bsrikumar855-dot/FundingRadar-Tinyfish

> Status: demo 1 is done and rescored. **Demos 2 and 3 have not been run yet**; their sections below are placeholders.

## What it does

Give it a one-line startup description ("what it does, stage, country"). It returns a ranked list of investors to approach. Each investor row has:

- the similar startups it backed in the last 18 months, each with a link we re-opened that names the company **and** the investor next to a funding phrase
- typical cheque size, stage focus and contact route (application form / open intake / warm intro only), each with the page it came from, or `unknown`
- the lead partner where a page names one
- a plain-language reason for its rank, generated from the score parts

Output goes to `results.json` (full ranked list plus every candidate deal with verified/dropped status and reasons), `results.csv` and `report.md` (top 15). It works for any description: the parser, query templates and scoring are generic code with no per-demo hardcoding and no LLM in the core.

## The 4 TinyFish endpoints and the job of each

| Endpoint | Call | Job in the pipeline | Why it is needed |
|---|---|---|---|
| **TinySet** | `POST /v1/datasets`, poll `GET /v1/datasets/{id}`, `GET /rows` | One dataset per run: "a recently funded startup similar to <description>". 9 columns, `company` is the primary key. | Finds comparable funded startups across many news sites in one structured table. This is the web-wide discovery step. |
| **Search** | `GET api.search` with `recency_minutes` (18 months) and `exclude_domains` | Templated queries (sector × stage × country × "raises / led by") add deals TinySet missed. It also finds each top investor's own website. | Gives a second, independent source of candidates and locates the sites the Agent visits. |
| **Fetch** | `POST api.fetch`, markdown, `ttl=0`, ≤10 URLs per call | Re-opens every candidate's source page. A deal is verified only if the page names the company, names an investor within 200 characters of a funding phrase, and dates the round within 18 months. | This is the proof step: nothing reaches the output without a live page backing it. |
| **Agent** | `POST /v1/automation/run-async`, poll `GET /v1/runs/{id}` | A read-only browser run on each top investor's own site: cheque size, stage focus, contact route, portfolio partners, each with its page URL. | These facts sit on investors' own sites behind navigation, so plain fetches don't reach them. |

## Proof and quality rules

1. **Live page proof.** Company and investor are matched whole-word on the re-fetched page. The investor must appear within 200 characters of a funding phrase ("led by", "backed by", "investors, including", "participation from", "funded by", "raised $X from", plus Portuguese/Spanish/German/French equivalents). A name that appears only in a sidebar or a listicle table doesn't count.
2. **Self-published pages are not proof.** If the link is on the investor's own domain (agfundernews.com for AgFunder, touchstone.vc for Touchstone Partners), that match is removed unless a third-party page also proves it.
3. **18-month window.** The page date comes first, then the claimed date. A page date older than 18 months drops the deal even if TinySet claimed it was recent.
4. **Agent facts must belong to the right entity.** A fact's page must sit on a domain that clearly carries the investor's name (5+ letters), or the page must mention one of the investor's verified portfolio companies. Otherwise the fact is `unknown`. Example: PINC's facts came from pauliggroup.com and were rejected.
5. **No padding.** If fewer than 10 companies verify, the report says so. When few deals match the target country and stage, the report says that too, with a count of deals by country.

## Scoring weights and thresholds

**These were tuned on demo 1 only (Indian agritech). They are frozen from now on except for bug fixes.** Demos 2 and 3 run with exactly these values.

| Name | Value | Meaning |
|---|---|---|
| `W.what` | 0.40 | weight of what-it-does keyword overlap (description keywords vs the deal's `what_it_does` + source-page excerpt) |
| `W.sector` | 0.05 | weight of a broad sector-label match (agritech, legaltech, …) |
| `W.stage` | 0.10 | weight of stage match; an adjacent stage gets half |
| `W.country` | 0.35 | weight of country match |
| `W.recency` | 0.10 | weight of recency, linear from 1 (today) to 0 (18 months ago) |
| `REGION_SHARE` | 0.30 | same region but another country gets 30% of the country weight. If the target is only a region (e.g. Europe), a deal in that region gets the full country weight |
| generic-word weight | 0.5 | `ai, ml, app, detection, platform, tool, software, assistant, data, analytics, digital, smart` count half; other description keywords count 1.0 |
| `WHAT_FULL` | 3.0 | matched keyword weight that counts as a full what-it-does match |
| `SECTOR_ONLY` | ×0.25 | a deal with matched keyword weight < 1.0 (no specific word) keeps 25% of its similarity |
| `MIN_SIM` | 0.30 | deals below this similarity do not count toward any investor |
| `LEAD_BONUS` | +50% | a deal the investor led adds 1.5× its similarity |
| `COUNTRY_FACTOR` | ×1.5 | investor score × 1.5 if it backed at least one counted deal in the target country (or the target region) |
| `NEAR_CHARS` | 200 | max distance between investor name and funding phrase |
| `WINDOW_DAYS` | 548 | 18-month window for rounds |
| date contradiction | 120 days | page date and claimed date that disagree by more than this (with the page date outside the window) drop the deal |
| domain-match minimum | 5 letters | an investor name shorter than this never matches a domain on the name alone (for the Agent-fact rule) |
| country note | < 5 | "few verified <country> <stage> deals" is printed when fewer than 5 verified deals match country and stage |
| report size | top 15 | investors in `report.md` / `results.csv`; `results.json` keeps all |
| TinySet rows | default 40, cap 60 | `--ts-rows`; demos use 25 |
| TinySet description | ≤ 1,900 chars | asserted; 2,049 returned 400 INVALID_INPUT |

## Demo results

All numbers are copied from each demo's `results.json`.

| | Demo 1: Indian agritech | Demo 2: US contract-review SaaS | Demo 3: EU battery recycling |
|---|---|---|---|
| Description | "AI crop-disease detection app for smallholder farmers, seed, India" | "AI assistant that drafts and reviews contracts for small law firms, seed/Series A, USA" | "grid-scale battery recycling startup, Europe" |
| Flags | `--ts-rows 25 --agent-top 6` | `--ts-rows 25 --agent-top 6` | `--ts-rows 25 --agent-top 6` |
| TinySet rows returned | 24 of 25 | not run yet | not run yet |
| Candidates (TinySet + Search) | 40 (24 + 16) | not run yet | not run yet |
| Verified companies, original run | 29 (35 links) | not run yet | not run yet |
| Verified companies after the proof rules | **28** | not run yet | not run yet |
| Dropped | 6 | not run yet | not run yet |
| Matches removed: funding-phrase rule / self-published rule | 5 / 2 | not run yet | not run yet |
| Deals counted toward scores (≥ 0.30) | 25 | not run yet | not run yet |
| Investors ranked | 101 | not run yet | not run yet |
| Agent runs (completed) | 6 (5) | not run yet | not run yet |
| Top 10 with ≥ 1 target-country deal | 9 | not run yet | not run yet |
| **Measured cost (wallet before → after)** | **$29.00 → $24.92 = $4.08** | not measured | not measured |

Demo 1 notes:
- The original run used the earlier scoring. It was then rescored with the rules above, and its 35 verified links were re-fetched with Fetch only (no TinySet, no Agent) to apply the funding-phrase rule.
- Only 1 verified India deal is at seed stage. The report says so and lists the other matches by country.
- Of the 6 Agent runs: Unicorn India Ventures gave cheque size and an application form. Astanor gave a cheque size. Corteva gave an application form. PINC's facts were rejected (another company's domain). AgFunder's run completed but its site stated none of the fields. Catalyst Fund's run timed out.

## Cost

Measured as the change in the wallet balance of the account that owns the API key, read by the user before and after each run.

| Run | Before | After | Cost |
|---|---|---|---|
| Live check (`--ts-rows 15 --agent-top 4`) | not measured | not measured | not measured |
| Demo 1 | $29.00 | $24.92 | **$4.08**. Anything that posted late from the live check would also fall inside this window |
| Demo 1 re-fetch for rescoring (Fetch only, 35 URLs) | not measured | not measured | not measured (Fetch is $0/URL on the current plan) |
| Demo 2 | not measured | not measured | not measured |
| Demo 3 | not measured | not measured | not measured |

## Limits

- **Thin regional data.** Live sources hold few verified India seed rounds for a niche like crop-disease detection. The ranking falls back to the nearest verified matches elsewhere and says so; it never invents deals.
- **Lexical matching.** A company described in different words than the query scores low on what-it-does overlap.
- **Weights.** They were tuned on one demo. Other sectors may want different balances, but they are frozen to avoid overfitting the demos.
- **Investor names.** They are merged by a suffix-free core (`Accel Partners` = `Accel`). Rebranded funds stay separate.
- **Unreachable pages.** Pages that block bots or need JavaScript fail Fetch, and their deals are dropped.
- **Investor-site facts.** Many funds don't publish cheque sizes, so those fields often stay `unknown`. Agent runs can time out (240 s limit).
- **Co-investors.** One verified deal gives every named co-investor the same similarity, so ties are common for investors with a single deal.
