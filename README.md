# Funding Radar

Describe a startup in one line. Get back a ranked list of investors to approach. Every match is backed by a live link that proves the investment.

```
python funding_radar.py "AI crop-disease detection app for smallholder farmers, seed, India"
```

Built on four TinyFish endpoints: **TinySet, Search, Fetch and Agent**. Python stdlib only, so there is nothing to install.

## What you get

`out/<slug>/results.json`, `results.csv` and `report.md`. Each investor row has:

| field | meaning |
|---|---|
| `rank`, `score` | plain-code score (see Ranking) |
| `why` | plain-language reason, generated from the score parts |
| `backed_companies` | `[{name, round, date, amount, link, led}]`, where every `link` is a page we re-fetched that names the company **and** the investor |
| `cheque_size`, `stage_focus` | from the investor's own site (Agent), each with its page URL, else `unknown` |
| `lead_partner` | partner named on the deal article, or on the investor's portfolio page, with source, else `unknown` |
| `reach_route`, `reach_url` | `application form` / `open intake` / `warm intro only` / `unknown`, with the page that shows it |
| `sources` | every URL behind the row |

`results.json` also lists every candidate deal with `status` = `verified` / `dropped` / `duplicate` and a `drop_reason`.

## Usage

```
export TINYFISH_API_KEY=...        # personal key (TinySet returns 403 for org keys)
export PYTHONUTF8=1                # Windows cp1252 consoles
python funding_radar.py "<what it does, stage, country>" [flags]
```

| flag | default | |
|---|---|---|
| `--stage`, `--country`, `--sector` | parsed from the text | override the parser (`--stage "seed/Series A"`, `--country Europe`) |
| `--ts-rows N` | 40 (cap 60) | TinySet target rows, about $0.10 per row. The estimate is printed before any TinySet call |
| `--yes` | off | skip the spend confirmation |
| `--ts-dataset ID` | | reuse an existing dataset, with no new spend |
| `--no-tinyset`, `--no-search` | | turn off a source |
| `--queries N` | 8 | Search queries (rate-limited to 30/min) |
| `--agent-top N` | 8 | Agent runs on the top N investors (0 = off) |
| `--agent-concurrency` | 2 | |
| `--out`, `--slug`, `--state` | `out`, from the text, `state` | output and state folders (both gitignored) |
| `--print-spec` | | print the parse, the TinySet spec and the queries, then exit with no API calls |
| `--rescore PATH` | | re-apply the current proof and ranking rules to a saved `results.json` and rewrite its outputs, with no API calls |
| `--report-top N` | 15 | investors shown in `report.md` and `results.csv`; `results.json` keeps the full list |
| `--refetch` | | with `--rescore`: re-fetch the verified links (Fetch only, free), apply the funding-phrase rule, extract lead partners, run the reach ladder |
| `--reach-top N` | 15 | run the reach-route ladder (Search + Fetch, free) for the top N investors |
| `--funds-only` | off | hide corporates, banks, foundations, farmer companies and unknown types in `report.md` / `results.csv` |
| `--all-in-one` | off | one investor list in `report.md` / `results.csv`, instead of the main ranking plus "Other backers (not seed funds)" |
| `--audit-sample N` | 0 | write `audit.csv` with N random (seed 42) investor–company matches for hand-checking |

### HTTP mode

```
python funding_radar.py serve --port 8080
curl -X POST localhost:8080/rank -d '{"description": "AI contract review for small law firms, seed, USA"}'
```

With no extra fields this runs Search + Fetch only. TinySet runs only with `"ts_rows": N, "confirm_cost": true`; `ts_rows` without `confirm_cost` is refused with 402 and the estimated cost. The request and response schema is in [docs/schema.md](docs/schema.md).

### Hand audit

```
python funding_radar.py --rescore demos/agritech-india/results.json --audit-sample 20
# fill the "correct y/n" column of demos/agritech-india/audit.csv, then:
python audit_precision.py demos/agritech-india/audit.csv --label "demo 1"
```

## Per-investor fields beyond the score

All of these come from plain code over pages we opened. None are guessed.

- **Investor type**: `fund` / `corporate` / `bank` / `foundation` / `farmer-company` / `unknown`, from name and domain keywords.
- **Observed round sizes and stages**, labelled "round sizes of deals it joined (observed)". This is the median announced round size per currency across the investor's counted deals, with the deal links. It is the whole round, not the investor's own cheque. Mixed currencies stay in their own currency and are never converted.
- **Sections (presentation only)**: `report.md` and `results.csv` list funds and unknown types under "Investors to approach". Banks, corporates, foundations and farmer companies go under "Other backers (not seed funds)", in the same score order with the same fields. `results.json` keeps one full ranked list with a `section` field. Scores and ranks are not changed.
- **Lead partner**: a person named on a verified source page as partner / principal / founder / managing director **at that investor** ("said Jane Smith, Partner at Alpha Ventures"). The name, role, sentence and URL are stored; otherwise `unknown`.
  - When the source pages name nobody, a search pass runs for the top 15 main-list investors (at most 15 queries per run). It takes the investor's most similar counted deal, runs one Search for `"<company> <investor> partner"`, and fetches the top 3 results.
  - The same extraction then runs, on pages that name both the company and the investor.
- **Reach route ladder**, each step with its source URL:
  - The investor's own domain is found with Search. It is accepted only if a page on it names the investor **and** one of its verified portfolio companies.
  - On that domain (home, `/contact`, `/apply`, `/pitch`, `/founders`, `/portfolio`, `/companies`, `/investments`, plus site search results), in order: (a) a pitch form or "submit your pitch / apply" page, (b) an intake email (pitch@, deals@, founders@ …, or an address on the investor's domain next to pitch/deck/founder wording), (c) "does not accept unsolicited pitches".
  - Otherwise (d): a warm intro via the founder of its most similar backed company, linking that verified deal.
  - Sites with almost no fetchable text are flagged `js_only_site`, as Agent fallback candidates. The Agent is not run for this without a cost confirmation.
- **Cheque size / stage focus (site)**: from the Agent run on the investor's own site, when one was made.

Re-running the same description reuses the saved TinySet rows in `state/<slug>/tinyset.json`, so you never pay for them twice.

## Pipeline, and why each endpoint matters

1. **Parse** (plain code). Sector keywords (with a sector lexicon), stage and country/region come from the text. There is no LLM, so the same input always gives the same parse.
2. **TinySet** (`POST /v1/datasets`, poll, `GET /rows`). One dataset per run. The entity is "a recently funded startup similar to <description>", with 9 columns: `company` (primary key), `what_it_does`, `round_stage`, `amount`, `announced_date`, `lead_investor`, `other_investors`, `hq_country`, `source_url`. TinySet does the slow, web-wide part: it finds comparable funded startups across many news sites in one structured table. The spec description is asserted to stay under 1,900 characters (2,049 fails with 400).
3. **Search** (`GET api.search`). Templated queries (stage × country × sector × "raises / led by") with `recency_minutes` = 18 months and `exclude_domains` for social sites and paywalled databases. These add deals TinySet missed. Headlines are parsed with regexes (`X raises $3M seed led by Y`). Search also finds each top investor's own website.
4. **Fetch** (`POST api.fetch`, markdown, `ttl=0`, at most 10 URLs per call). This is the proof step. Every candidate's `source_url` is re-opened live. A deal is **verified** only if the page names the company and at least one investor (whole-word match), with that investor's name **within 200 characters of a funding phrase** ("led by", "backed by", "investors include", "participation from", "funded by", "raised … from", …). The date (page date first, else claimed) must also be within 18 months. Otherwise the deal is **dropped** with a reason. Investors and lead partners named on the page ("led by X", "Jane Doe, partner at X") are picked up near the company's mentions only, so newsletter boxes and sidebars don't leak names in.
   **Self-published proof does not count.** If the link is on the investor's own domain (agfundernews.com for AgFunder, calculuscapital.com for Calculus Capital), that investor–company match is removed unless a third-party page also proves it. A deal without a verified link never reaches the output.
5. **Rank** (plain code). Deal similarity = 0.40 × **what-it-does overlap** + 0.05 × broad sector + 0.10 × stage (half for an adjacent stage) + 0.35 × country (30% of that for the same region) + 0.10 × recency. What-it-does overlap compares the description's own keywords (stopword-filtered; `crop`, `disease`, `smallholder`, `farmer`, with generic words like `ai`/`app`/`detection` at half weight) with the deal's `what_it_does` and its source-page excerpt.
   - A deal that matches only the broad sector (no specific keyword) keeps 25% of its score.
   - A deal below similarity 0.30 does not count toward any investor.
   - Investor score = sum of the similarities of the counted verified deals it backed, +50% for each deal it led.

   Each deal's similarity components are in `results.json` (`sim_parts`), and the `why` text is generated from them. `report.md` lists verified deals by country and says plainly when few deals match your country and stage. It doesn't pad.
6. **Agent** (`POST /v1/automation/run-async`, poll `GET /v1/runs/{id}`). This step visits the top investors' own sites with a read-only goal (no sign-in, no form submits) to extract cheque size, stage focus, contact route and portfolio partners. A fact counts only if the agent returns the page URL where it saw it, otherwise it is `unknown`. That page must also be tied to the right entity: its domain must clearly carry the investor's name (5+ letters, e.g. astanor.com), or the page (re-fetched) or the agent's portfolio findings on that domain must mention one of the investor's verified portfolio companies. Otherwise the fact becomes `unknown`. Example: facts for "PINC" came from pauliggroup.com, which was rejected. `max_steps` is not sent (it is beta-only and returns 403). A 403 or a failed run is printed and the field stays `unknown`; the run keeps going.

## Tests

```
python -m unittest discover -s tests -v
```

These are offline tests against `tests/mock_tinyfish.py`, a local fake of all four endpoints plus a small fake web of funding news. They cover the parser, similarity, ranking, the 18-month filter, verified/dropped rules, headline parsing, agent-fact validation, the TinySet spec limit, and an end-to-end run. The end-to-end run also checks API-usage rules: at most 10 Fetch URLs per call, no `max_steps`, Search filters, one dataset per run, and that the API key never lands in a file.

## Limits

- Matching is lexical. A company described in different words than the query can score low on sector overlap.
- Investor names are merged by a suffix-free core (`Accel Partners` = `Accel`). Rebrands (e.g. a fund that renamed) stay separate.
- Pages that block bots or need JavaScript fail Fetch, and their deals are dropped, never assumed.
- Agent facts depend on what the investor publishes. Many funds do not state a cheque size; those fields say `unknown`.
