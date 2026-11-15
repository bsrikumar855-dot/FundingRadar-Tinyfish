# Funding Radar: TinyFish Student Bounty Drop 002

Repo: https://github.com/bsrikumar855-dot/FundingRadar-Tinyfish

> Status: all 3 demos are done. Demo 3's measured cost is pending the user's balance reading.

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

## Per-investor fields (plain code over opened pages, no LLM, never guessed)

| Field | How it is filled | Demo 1 main-list top 10 filled |
|---|---|---|
| Investor type | Name and domain keywords: fund / corporate / bank / foundation / farmer-company / unknown. `--funds-only` hides the rest in report/CSV | 6 known (fund), 4 unknown |
| Observed round sizes and stages | Labelled "round sizes of deals it joined (observed)". Median announced round size **per currency** (never converted) across its counted deals, plus the set of stages, with deal links. This is the whole round, not its own cheque | 9 (Rainmatter has only undisclosed amounts) |
| Lead partner | A person named on a verified source page as partner / principal / founder / managing director **at that investor**, with role, sentence and URL. If the source pages name nobody, a search pass runs `"<company> <investor> partner"` for the top 15 main-list investors (≤15 queries per run), fetches the top 3 results and keeps only pages naming both | 1 (Unicorn India Ventures → Anil Joshi, managing partner, via the search pass). 14 search-pass queries found nobody else |
| Reach route | Ladder, each step with its source URL: (a) pitch form / apply page, (b) intake email, (c) "no unsolicited pitches", all on the investor's own domain; else (d) warm intro via the founder of its most similar backed company. The domain is accepted only if a page on it names the investor **and** one of its verified portfolio companies | 10. Only 2 from the investor's own site (Unicorn India Ventures and Catalyst Fund application forms); 8 are warm intros. The ladder also reads `/portfolio`, `/companies`, `/investments`, `/contact` on accepted domains |
| Cheque size / stage focus (site) | Agent on the investor's own site (demo 1's original run only), entity-checked | Unicorn India Ventures only |

The entity check matters in practice. For example, Search returned `exim.com.my` (Malaysia's Exim Bank) for India's Exim Bank, and `pinc.co.in` for PINC. Both were rejected because no page on them named a backed company.

The latest demo 1 enrichment (re-fetch, reach ladder, partner pass) used **Fetch 216 URLs + Search 72 queries, 0 TinySet, 0 Agent**.

**Sections are a presentation rule, not a scoring change.** `report.md` and `results.csv` show funds and unknown types under "Investors to approach". Banks, corporates, foundations and farmer companies go under "Other backers (not seed funds)", in the same score order with the same fields. Scores and ranks are computed over all investors exactly as before, and `results.json` keeps one full list with a `section` field. `--all-in-one` restores the single list. In demo 1, 5 of the previous single-list top 15 moved to Other backers: Sahyadri Farms, Exim Bank, SIDBI, Indigram Labs Foundation and Corteva (11 of 101 investors overall). In live runs the Agent and the reach ladder now target main-list investors first.

## Ranking order on equal scores

The lead bonus is **already inside the score**: a deal the investor led counts 1.5× its similarity (`LEAD_BONUS = 0.5`). Scores are compared at 4 decimals. On an exact tie the order is:

1. led any counted deal first
2. more counted deals
3. most recent deal date
4. investor name A–Z

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
| Description | "AI crop-disease detection app for smallholder farmers, seed, India" | "AI assistant that drafts and reviews contracts for small law firms, seed/Series A, USA" | "grid-scale battery recycling startup, seed, Europe" |
| Flags | `--ts-rows 25 --agent-top 6` | `--ts-rows 25 --agent-top 6` | `--ts-rows 25 --agent-top 6` |
| TinySet rows returned | 24 of 25 | 25 of 25 | 25 of 25 |
| Candidates (TinySet + Search) | 40 (24 + 16) | 34 (25 + 9) | 51 (25 + 26) |
| Verified companies, original run | 29 (35 links) | 27 (28 links) | 32 (36 links) |
| Verified companies after the proof rules | **28** | **25** | **31** |
| Dropped | 6 | 8 | 15 |
| Matches removed: funding-phrase rule / self-published rule | 5 / 2 | 3 / 2 | 8 / 4 |
| Deals counted toward scores (≥ 0.30) | 25 | 24 | 17 |
| Investors ranked | 101 | 86 | 63 (58 main, 5 other) |
| Agent runs (completed) | 6 (5) | 6 (5) | 6 (6) |
| Main-list top 10 with ≥ 1 target-country deal | 8 | 10 | 10 (target is the region Europe) |
| Leads demoted (saved "led by" text names only some) | 11 names on 4 deals | 0 | 0 |
| Main-list top 10 filled: type / observed size / observed stages | 6 / 9 / 9 | 9 / 9 / 10 | 7 / 10 / 10 |
| Main-list top 10 filled: lead partner | 1 | 6 | 2 |
| Main-list top 10 filled: reach from own site / any route incl. warm intro | 2 / 10 | 1 / 10 | 3 / 10 |
| **Measured cost (wallet before → after)** | **$29.00 → $24.92 = $4.08** | **$24.92 → $20.26 = $4.66** | $20.26 → not measured yet |

Demo 1 notes:
- The original run used the earlier scoring. It was then rescored with the rules above, and its 35 verified links were re-fetched with Fetch only (no TinySet, no Agent) to apply the funding-phrase rule.
- Only 1 verified India deal is at seed stage. The report says so and lists the other matches by country.
- Of the 6 Agent runs: Unicorn India Ventures gave cheque size and an application form. Astanor gave a cheque size. Corteva gave an application form. PINC's facts were rejected (another company's domain). AgFunder's run completed but its site stated none of the fields. Catalyst Fund's run timed out.

Demo 2 notes:
- It ran end to end with the code of that time. Afterwards, three extraction bugs found in its output were fixed, and its saved results were rescored offline (no API calls). Ranking, reach routes and Agent fields were unchanged apart from these fixes:
  - **Headline mis-parse:** "White & Case announces strategic investment in … Clauze.AI" was read as company "Case" backed by "White". Headlines can now contain "&", the "X invests in Y" shape is parsed with X as the investor, and a deal whose company is only the tail of an "Investor & Company" name is dropped. Result: the bogus "Case" deal was dropped and "White" left the ranking. Clauze.AI's own proof was White & Case's press release, so the self-published rule also drops it.
  - **"&" in names:** "&" now joins a firm name only when both sides are single words (White & Case). Otherwise it separates a list (SIDBI & Exim Bank).
  - **Fund-name fragments as people:** "Capital Partners" (from Flybridge Capital Partners) and "Private Capital" (from Intact Private Capital) were extracted as partners. Person names made of fund-suffix words or parts of the investor's name are now rejected.
- Main-list top 10: all 10 have a US deal, and 6 have a named lead partner from a source page. Examples: Flybridge Capital Partners → Jesse Middleton (general partner), Scopus Ventures → Bahram Nour-Omid (managing partner).
- Of the 6 Agent runs, 5 completed: Flex Capital ($500K–$2M, application form), Scopus Ventures ($500k–$1M), Flybridge Capital Partners ($1M–$3M), Moxxie Ventures ($500k–$2M), Amino Capital (stage only). Y Combinator's run timed out.
- The reach ladder found 1 own-site route in the main-list top 10: Collide Capital's intake email (`investors@collidecap.com`). The other 9 fall back to a warm intro through a named portfolio company.
- Y Combinator is typed `unknown`: its name has no fund keyword, and the type label is keywords only.

Demo 3 notes (reported as produced; nothing rescored or edited by hand):
- The target is a region (Europe), so the country factor applies to any European deal. All 10 of the main-list top 10 have one. Verified deals by country: Germany 8, USA 4, unknown 3, Finland 2, Netherlands 2, Sweden 2, Switzerland 2, UK 2, and 1 each for Australia, Belgium, Estonia, India, Italy, Luxembourg and Singapore.
- Only 17 of 32 verified deals cleared the 0.30 similarity threshold. Search brought in climate deals that are not battery recycling (Mondra, Brineworks, VARM, Furo), and they don't count toward any investor.
- All 6 Agent runs completed. Cheque sizes came back for 2C Ventures (€250K–1M), EIT Urban Mobility (up to €2.5M), Rise PropTech (€500K–3M), Volve Capital (€150K–500K) and Turbine Capital (€0.3–1.0M).
- **Headline mis-parses and name artifacts seen in this run. Fixed in code afterwards, and all three demos rescored offline (no API calls):**
  - Company "Düsseldorf-based Co-reactive" → "Co-reactive": location prefixes now strip with non-ASCII letters.
  - "Redwood" and "Redwood Materials" are merged: one name is a whole-word prefix of the other, and the longer name appears on the shorter one's source page. Verified companies 32 → 31. The merged deal (similarity 0.28) is still below the 0.30 threshold.
  - "Nvidia’s NVentures" → "NVentures", "Google-backed CapitalG" → "CapitalG", "Sheryl Sandberg’s Sandberg Bernthal Venture Partners" → "Sandberg Bernthal Venture Partners", "Germany’s Federal Ministry of Research" → "Federal Ministry of Research". Descriptor prefixes are stripped and merged with the plain name.
  - "CEO Timo Siukkola" removed (names starting with CEO / CTO / founder / partner / managing / director / chairman / president are dropped). It was #7 in the main list. Cherry Ventures (#11 → #10) entered the top 10, and Voima Ventures, CDP Venture Capital SGR and Norrsken Evolve each moved up one place.
  - "Group" removed (single generic words are not investor names; "Partners Group" is kept).
  - New type `public body`: Department of Energy, Swedish Energy Agency, Clean Energy Finance Corporation and European Innovation Council (EIC) are now `public body` and sit under Other backers. EIC moved there from the main list; the other three were already there as `corporate`.
  - EIT Urban Mobility's reach route is no longer an "application form": the master's-programme page is rejected (programme / course / admission / master's wording), and the route falls back to a warm intro via Rebaba. A pitch-form or intake page must now also mention pitching, startups, founders or funding.
  - After the rescore the main-list top 10 has 3 genuine own-site routes: Turbine Capital (intake email), Voima Ventures (pitch page) and Cherry Ventures (intake email).

## Cost

Measured as the change in the wallet balance of the account that owns the API key, read by the user before and after each run.

| Run | Before | After | Cost |
|---|---|---|---|
| Live check (`--ts-rows 15 --agent-top 4`) | not measured | not measured | not measured |
| Demo 1 | $29.00 | $24.92 | **$4.08**. Anything that posted late from the live check would also fall inside this window |
| Demo 1 re-fetches for rescoring and enrichment (Fetch + Search only, no TinySet, no Agent) | not measured | not measured | not measured (Fetch and Search are $0 on the current plan) |
| Demo 2 | $24.92 | $20.26 | **$4.66** |
| Demo 3 | $20.26 | not measured yet | not measured yet |

## Limits

- **Thin regional data.** Live sources hold few verified India seed rounds for a niche like crop-disease detection. The ranking falls back to the nearest verified matches elsewhere and says so; it never invents deals.
- **Lexical matching.** A company described in different words than the query scores low on what-it-does overlap.
- **Weights.** They were tuned on one demo. Other sectors may want different balances, but they are frozen to avoid overfitting the demos.
- **Investor names.** They are merged by a suffix-free core (`Accel Partners` = `Accel`). Rebranded funds stay separate.
- **Unreachable pages.** Pages that block bots or need JavaScript fail Fetch, and their deals are dropped.
- **Investor-site facts.** Many funds don't publish cheque sizes, so those fields often stay `unknown`. Agent runs can time out (240 s limit).
- **Co-investors.** One verified deal gives every named co-investor the same similarity, so ties are common for investors with a single deal. Ties are broken only by the fixed rules above (led a deal, number of deals, latest date, name). There is no other judgement about who matters more.
- **Investor type is a keyword label, not a classification.** Banks (Exim Bank, SIDBI), corporates (Corteva), foundations and farmer companies (Sahyadri Farms) appear in the ranking because they appear in funding news. Names with no keyword (Rainmatter, GVFL, JITO) are `unknown`. Use `--funds-only` to hide everything but funds.
- **Country is a label, not a market.** A deal's country is the HQ label from the source or TinySet. It can differ from where the company operates: OKO Finance is labelled Luxembourg but serves farmers in Mali and Côte d'Ivoire.
- **Observed round size is not the investor's cheque.** It is the announced size of rounds it joined. Amounts in different currencies are reported separately and never converted.
- **Lead partners are rare in news text.** Only 6 of 28 verified demo 1 pages name a partner at an investor, so most rows say `unknown`.
- **Reach routes often fall back to a warm intro.** Many investor sites have no fetchable pitch form or intake email, or their domain fails the entity check. Sites that need JavaScript would need the Agent, which is not run without a cost confirmation.
- **Live pages move scores slightly.** Re-fetching can return a different page version or fail on one link, which changes the excerpt used for what-it-does overlap. Between two demo 1 re-fetches, GVFL moved from 3rd to 4th.

### Name and parsing fixes made after the demos (code changes, then offline rescores)

Fixed:
- "&" in firm names (White & Case) vs lists (SIDBI & Exim Bank).
- "X invests in Y" headlines, and companies that are only the tail of an "Investor & Company" name.
- Fund-name fragments read as people ("Capital Partners").
- Names starting with a role word ("CEO Timo Siukkola").
- Descriptor prefixes ("Nvidia’s NVentures", "Google-backed CapitalG").
- Single generic words as investors ("Group").
- Unicode location prefixes ("Düsseldorf-based").
- Company names that are whole-word prefixes of each other on the same page ("Redwood" / "Redwood Materials").
- Government bodies now get their own `public body` type and sit under Other backers.
- Study-programme pages are rejected as pitch routes.

Effect on the committed results: demo 1 unchanged; demo 2 lost one investor outside the top 10 ("CEO Maged Helmy"); demo 3 as listed in its notes.

Remains:
- **Individual people** without a role word ("Priit Viru", "Basant Sharma", "Dr Norman Röchert", "Sheryl Sandberg") are kept as investors. Some are genuine angels, and nothing distinguishes them from funds except the type `unknown`.
- **Regional and public bodies without a keyword** ("NRW", "North Holland", "Enterprise Estonia", "Innosuisse", "Vinnova") stay `unknown` and appear in the main list.
- **Spelling and short-form variants** of the same company or fund ("HTGF" vs "High-Tech Gründerfonds") are only merged when one is a whole-word prefix of the other. Acronyms are not expanded.
- **Accelerator programmes** that take applications through a "programme" page are now rejected as pitch routes. Such investors fall back to a warm intro, even when the programme is the right way in.
- **Topic drift in Search:** in demo 3 it brought in climate deals outside battery recycling. They are verified but stay below the similarity threshold, so they don't count.

## Hand-checked precision (audit sample, seed 42)

`demos/agritech-india/audit.csv` holds 20 random investor–company matches from demo 1, each with its link and the sentence that names them. Precision is **not measured yet**: it is filled in by `audit_precision.py` once the `correct y/n` column has been checked by hand.

<!-- audit:start -->
<!-- audit:end -->
