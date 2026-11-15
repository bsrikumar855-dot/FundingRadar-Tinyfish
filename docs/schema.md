# Funding Radar: JSON schema

Two things share this schema: the `serve` HTTP API and the `results.json` file written by every run.

```
python funding_radar.py serve --port 8080          # binds 127.0.0.1 by default; --host to change
```

One run at a time. A second request while a run is in progress gets `429 busy`.

## `GET /health`

```json
{"ok": true, "key_set": true}
```

`key_set` says whether `TINYFISH_API_KEY` is set in the server's environment. The key itself is never returned.

## `POST /rank`

Request body (JSON):

| field | type | required | default | notes |
|---|---|---|---|---|
| `description` | string | yes | | one line: what it does, stage, country |
| `stage` | string | no | parsed | e.g. `"seed"`, `"seed/Series A"` |
| `country` | string | no | parsed | country or region, e.g. `"India"`, `"Europe"` |
| `sector` | string | no | parsed | comma-separated sector labels |
| `ts_rows` | integer 1–60 | no | none | TinySet target rows (~$0.10/row). **Requires `confirm_cost: true`.** |
| `confirm_cost` | boolean | no | `false` | must be the JSON literal `true` to allow any metered call (TinySet, Agent) |
| `agent_top` | integer 0–10 | no | `0` | Agent runs on the top N investors (metered). `> 0` requires `confirm_cost: true` |
| `report_top` | integer | no | `15` | investors in report.md / results.csv (the JSON always has all) |
| `funds_only` | boolean | no | `false` | hide non-fund investor types in report.md / results.csv |

Spend rules, enforced before anything runs:

- No `ts_rows` and no `confirm_cost` → Search + Fetch only (free). TinySet and Agent are never called.
- `ts_rows` without `confirm_cost: true` → **402** `confirm_cost_required`, with `estimated_tinyset_usd`.
- `confirm_cost: true` without `ts_rows` → **400** `ts_rows_required` (an explicit cap is mandatory).
- `agent_top > 0` without `confirm_cost: true` → **402** `confirm_cost_required`.
- Saved TinySet rows for the same description (`state/serve/<slug>/tinyset.json`) are reused, so a repeated confirmed request does not pay twice.

Examples:

```json
{"description": "AI assistant that drafts and reviews contracts for small law firms, seed/Series A, USA"}
{"description": "grid-scale battery recycling startup, Europe", "ts_rows": 25, "confirm_cost": true, "agent_top": 6}
```

Responses:

| status | body |
|---|---|
| 200 | the run's `results.json` object (below) |
| 400 | `{"error": "description_required" \| "invalid_json" \| "ts_rows_invalid" \| "ts_rows_required" \| "agent_top_invalid", ...}` |
| 402 | `{"error": "confirm_cost_required", "estimated_tinyset_usd": 2.5, "hint": "..."}` |
| 404 | `{"error": "not_found"}` |
| 429 | `{"error": "busy"}` |
| 500 / 502 | `{"error": "run_failed" \| "tinyfish_error", "detail": "..."}` |

## `results.json`

```text
{
  "input":  {description, keywords[], sectors[], stages[], country|null, region|null},
  "meta":   {run_date, window_start, candidates, from_tinyset, from_search, verified_companies, verified_links,
             verified_companies_after_rules, counted_deals, dropped, investors_ranked, self_published_dropped,
             proximity_dropped, tinyset_rows, tinyset_note, tinyset_cost_est, stats{...}, seconds, ...},
  "investors": [Investor, ...],      // full ranked list, rank 1 first
  "deals":     [Deal, ...]           // every candidate, with status and reasons
}
```

### Investor

| field | type | meaning |
|---|---|---|
| `rank` | int | 1 = best |
| `investor` | string | display name |
| `investor_type` | `fund` \| `corporate` \| `bank` \| `foundation` \| `farmer-company` \| `unknown` | from name and domain keywords |
| `score` | number | `base_score × country_factor` |
| `base_score` | number | sum of similarities of its counted deals, +50% for each deal it led |
| `country_factor` | 1.0 \| 1.5 | 1.5 if at least one counted deal is in the target country (or region) |
| `has_target_country_deal` | bool | |
| `why` | string | plain-language reason built from the score parts |
| `score_parts` | object | `{what, sector, stage, country, recency, lead}` summed over its deals |
| `backed_companies` | `[{name, round, date, amount, link, led}]` | each `link` is a verified page |
| `observed` | object | `{label: "round sizes of deals it joined (observed)", by_currency: [{currency, median, median_text, n, deals: [{name, amount, link}]}], undisclosed: [name], stages: [stage], text, stages_text}`. Each currency is kept separately and never converted |
| `lead_partner` | `"unknown"` or `[{investor, name, role, sentence, url, company}]` | a person named on a source page as partner / principal / founder / managing director **at this investor** |
| `reach_route` | string | `application form` \| `open intake (email)` \| `no unsolicited pitches` \| `warm intro via founder` \| `unknown` |
| `reach_url` | string | form / mailto / page URL, or the verified deal link for a warm intro |
| `reach` | object | `{route, url, source_url, evidence, company?}` |
| `warm_intro` | object\|null | `{route, company, url, source_url, evidence}`, always offered as a fallback |
| `own_domain` | string | the investor's domain, accepted only if a page on it names the investor **and** one of its verified portfolio companies |
| `own_domain_evidence` | object | `{url, company}` or `{note}` |
| `js_only_site` | bool | site pages had almost no text (Agent fallback candidate) |
| `cheque_size`, `stage_focus` | string | from the investor's site via Agent, or `unknown` |
| `cheque_size_source`, `stage_focus_source` | string | page URL of that fact |
| `reach_route_agent`, `reach_url_agent` | string | the Agent's own reading of the contact route |
| `lead_partner_agent` | list | portfolio partners the Agent reported, with URLs |
| `agent_note` | string | `completed`, `agent run timed out`, `agent http 403`, `not enriched …`, or rejection notes |
| `sources` | string[] | every URL behind the row |

### Deal

| field | meaning |
|---|---|
| `company`, `what_it_does`, `round_stage`, `amount`, `date`, `hq_country`, `source_url`, `extra_links` | the deal |
| `origin` | `tinyset` or `search` |
| `status` | `verified` \| `dropped` \| `duplicate` |
| `drop_reason` | why it was dropped |
| `investors_verified`, `lead_verified` | investors named near a funding phrase on the page |
| `self_published_dropped` | investors removed because the only proof was their own site |
| `not_near_funding_phrase` | names on the page that were not within 200 characters of a funding phrase |
| `proof_snippets` | `{investor: text window that proves it}` |
| `partner_mentions` | `[{investor, name, role, sentence, url}]` |
| `page_excerpt` | ~1,500 characters of the page around the company's first mention |
| `similarity`, `sim_parts`, `keyword_hits`, `counted`, `country_norm` | scoring; `counted` = similarity ≥ 0.30 |

## `audit.csv`

Written with `--audit-sample N`: N random investor–company matches (seed 42) from counted deals.

`investor, company, link, sentence, correct y/n`. Fill in the last column by hand, then run:

```
python audit_precision.py <audit.csv> --label "demo 1"
```

This writes `correct / (correct + wrong)` into SUBMISSION.md.
