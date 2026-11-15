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

Re-running the same description reuses the saved TinySet rows in `state/<slug>/tinyset.json`, so you never pay for them twice.

## Pipeline, and why each endpoint matters

1. **Parse** (plain code). Sector keywords (with a sector lexicon), stage and country/region come from the text. There is no LLM, so the same input always gives the same parse.
2. **TinySet** (`POST /v1/datasets`, poll, `GET /rows`). One dataset per run. The entity is "a recently funded startup similar to <description>", with 9 columns: `company` (primary key), `what_it_does`, `round_stage`, `amount`, `announced_date`, `lead_investor`, `other_investors`, `hq_country`, `source_url`. TinySet does the slow, web-wide part: it finds comparable funded startups across many news sites in one structured table. The spec description is asserted to stay under 1,900 characters (2,049 fails with 400).
3. **Search** (`GET api.search`). Templated queries (stage × country × sector × "raises / led by") with `recency_minutes` = 18 months and `exclude_domains` for social sites and paywalled databases. These add deals TinySet missed. Headlines are parsed with regexes (`X raises $3M seed led by Y`). Search also finds each top investor's own website.
4. **Fetch** (`POST api.fetch`, markdown, `ttl=0`, at most 10 URLs per call). This is the proof step. Every candidate's `source_url` is re-opened live. A deal is **verified** only if the page names the company and at least one investor (whole-word match), and the date (page date first, else claimed) is within 18 months. Otherwise it is **dropped** with a reason. Investors and lead partners named on the page ("led by X", "Jane Doe, partner at X") are picked up here. A deal without a verified link never reaches the output.
5. **Rank** (plain code). Deal similarity = 0.45 × weighted sector-keyword overlap + 0.20 × stage match (half for an adjacent stage) + 0.20 × country match (half for the same region) + 0.15 × recency. Investor score = sum of the similarities of the verified deals it backed, +50% for each deal it led. The `why` text is generated from these parts.
6. **Agent** (`POST /v1/automation/run-async`, poll `GET /v1/runs/{id}`). This step visits the top investors' own sites with a read-only goal (no sign-in, no form submits) to extract cheque size, stage focus, contact route and portfolio partners. A fact counts only if the agent returns the page URL where it saw it, otherwise it is `unknown`. `max_steps` is not sent (it is beta-only and returns 403). A 403 or a failed run is printed and the field stays `unknown`; the run keeps going.

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
