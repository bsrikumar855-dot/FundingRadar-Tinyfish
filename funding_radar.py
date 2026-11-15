#!/usr/bin/env python3
"""
funding_radar - describe a startup in one line, get a ranked list of investors to approach,
every match backed by a live link that proves the investment. Built on the TinyFish APIs.

  TinySet  (POST /v1/datasets)            web-wide table of recently funded startups similar to the description
  Search   (GET  api.search)              templated news queries that add deals TinySet missed
  Fetch    (POST api.fetch, markdown)     re-opens every deal's source page; a deal counts only if the page names
                                          the company AND at least one investor (verified / dropped)
  Agent    (POST /v1/automation/run-async) visits each top investor's own site: cheque size, stage focus, contact route

Deterministic core: parsing, verification, the 18-month window, similarity, ranking and the "why" text are plain
code, no LLM. The web-facing layers only supply candidate facts; nothing reaches the output without a live page.
Stdlib only.
"""
import argparse, csv, email.utils, json, os, re, sys, threading, time
import urllib.error, urllib.parse, urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, timedelta
from html import unescape

FETCH_URL = os.environ.get("TINYFISH_FETCH_URL", "https://api.fetch.tinyfish.ai").rstrip("/")
SEARCH_URL = os.environ.get("TINYFISH_SEARCH_URL", "https://api.search.tinyfish.ai").rstrip("/")
AGENT_URL = os.environ.get("TINYFISH_AGENT_URL", "https://agent.tinyfish.ai").rstrip("/")
UA_PURPOSE = "Funding Radar: verify that a startup funding round and its investors are named on this page"
ABORT = threading.Event()
WINDOW_DAYS = 548          # 18 months
TS_COST_PER_ROW = 0.10
TS_DESC_MAX = 1900         # TinySet: 2049-char description -> 400 INVALID_INPUT
TS_ROWS_CAP = 60


class Fatal(Exception):
    pass


def say(*a):
    """Print to stderr without crashing on a cp1252 Windows console."""
    msg = " ".join(str(x) for x in a)
    enc = getattr(sys.stderr, "encoding", None) or "utf-8"
    sys.stderr.write(msg.encode(enc, "replace").decode(enc, "replace") + "\n")
    sys.stderr.flush()


# ----------------------------------------------------------------------------- http / rate limits (from zombiecheck)
class Limiter:
    def __init__(self, per_min):
        self.rate, self.next, self.lock = max(per_min, 1) / 60.0, 0.0, threading.Lock()

    def wait(self, n=1):
        with self.lock:
            now = time.monotonic()
            start = max(now, self.next)
            self.next = start + n / self.rate
        if start > now:
            time.sleep(start - now)


def http(method, url, key, body=None, params=None, timeout=150, retries=5):
    if params:
        url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
    data = json.dumps(body).encode() if body is not None else None
    headers = {"X-API-Key": key, "Content-Type": "application/json", "Accept": "application/json"}
    for attempt in range(retries):
        if ABORT.is_set():
            raise Fatal("aborted")
        try:
            req = urllib.request.Request(url, data=data, headers=headers, method=method)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read()
                return r.status, (json.loads(raw) if raw else {})
        except urllib.error.HTTPError as e:
            raw = e.read()
            try:
                payload = json.loads(raw)
            except Exception:
                payload = {"raw": raw[:300].decode("utf8", "replace")}
            if e.code in (429, 500, 502, 503, 504) and attempt < retries - 1:
                ra = e.headers.get("Retry-After", "")
                time.sleep(float(ra) if re.fullmatch(r"\d+(\.\d+)?", ra or "") else min(2 ** attempt * 2, 60))
                continue
            return e.code, payload
        except (urllib.error.URLError, TimeoutError, ConnectionError, json.JSONDecodeError) as e:
            if attempt < retries - 1:
                time.sleep(min(2 ** attempt * 2, 30))
                continue
            return 0, {"error": str(e)}
    return 0, {"error": "retries exhausted"}


class TF:
    """Thin TinyFish client (same shape as zombiecheck's). Fetch <=10 URLs per call, Search 30/min."""

    def __init__(self, key, fetch_per_min=150, search_per_min=30):
        self.key = key
        self.fl, self.sl = Limiter(fetch_per_min), Limiter(search_per_min)
        self.stats = {"fetch_urls": 0, "fetch_calls": 0, "search": 0, "agent_runs": 0, "agent_completed": 0,
                      "agent_403": 0, "tinyset_datasets": 0, "tinyset_rows": 0}
        self._lock = threading.Lock()
        self._cache = {}

    def _fatal(self, st, js, what):
        if st == 401:
            raise Fatal(f"{what}: 401 invalid TINYFISH_API_KEY")
        if st == 402:
            raise Fatal(f"{what}: 402 free allowance used and wallet empty - top up, then re-run")

    def fetch(self, urls, fmt="markdown"):
        """urls<=10 per API call (chunked here). Returns {requested_url: result | {'error':..}}. Always live (ttl=0)."""
        urls = list(dict.fromkeys(u for u in urls if u))
        out = {}
        with self._lock:
            for u in urls:
                if (u, fmt) in self._cache:
                    out[u] = self._cache[(u, fmt)]
        want = [u for u in urls if u not in out]
        for i in range(0, len(want), 10):
            got = self._fetch_live(want[i:i + 10], fmt)
            with self._lock:
                for u, r in got.items():
                    if "error" not in r:
                        self._cache[(u, fmt)] = r
            out.update(got)
        return out

    def _fetch_live(self, urls, fmt):
        out = {}
        self.fl.wait(len(urls))
        st, js = http("POST", FETCH_URL, self.key, {"urls": urls, "format": fmt, "ttl": 0, "links": False,
                                                    "per_url_timeout_ms": 45000, "purpose": UA_PURPOSE})
        self._fatal(st, js, "fetch")
        if st == 403:
            raise Fatal("fetch: 403 forbidden")
        with self._lock:
            self.stats["fetch_urls"] += len(urls)
            self.stats["fetch_calls"] += 1
        if st != 200:
            return {u: {"error": f"http_{st}"} for u in urls}
        leftovers = []
        for r in js.get("results", []) or []:
            if r.get("url") in urls:
                out[r["url"]] = r
            else:
                leftovers.append(r)
        for e in js.get("errors", []) or []:
            out[e.get("url")] = {"error": e.get("error", "error"), "status": e.get("status")}
        for u in urls:
            if u not in out and leftovers:
                out[u] = leftovers.pop(0)
            out.setdefault(u, {"error": "no_result"})
        return out

    def search(self, query, **kw):
        self.sl.wait()
        st, js = http("GET", SEARCH_URL, self.key, params={"query": query, **kw}, timeout=30)
        self._fatal(st, js, "search")
        with self._lock:
            self.stats["search"] += 1
        return js.get("results", []) or [] if st == 200 else []

    def agent_run(self, url, goal, secs=240, poll=4.0):
        """POST /v1/automation/run-async, then poll GET /v1/runs/{id}. Returns (result, note).
        No max_steps (beta-only, 403). A 403 is reported, never fatal."""
        st, js = http("POST", AGENT_URL + "/v1/automation/run-async", self.key,
                      {"url": url, "goal": goal, "agent_config": {"max_duration_seconds": secs}}, timeout=60, retries=3)
        self._fatal(st, js, "agent")
        if st == 403:
            with self._lock:
                self.stats["agent_403"] += 1
            say(f"  agent: 403 forbidden for {url} - field(s) left unknown ({str(js)[:160]})")
            return None, "agent http 403"
        run_id = js.get("run_id") if st == 200 else None
        if not run_id:
            return None, f"agent create http {st}"
        with self._lock:
            self.stats["agent_runs"] += 1
        deadline = time.time() + secs + 120
        while time.time() < deadline and not ABORT.is_set():
            st, js = http("GET", AGENT_URL + f"/v1/runs/{run_id}", self.key, timeout=30, retries=3)
            self._fatal(st, js, "agent")
            status = js.get("status") if st == 200 else None
            if status == "COMPLETED":
                with self._lock:
                    self.stats["agent_completed"] += 1
                return js.get("result"), "completed"
            if status in ("FAILED", "CANCELLED"):
                return None, f"agent run {status.lower()}"
            time.sleep(poll)
        http("POST", AGENT_URL + f"/v1/runs/{run_id}/cancel", self.key, timeout=30, retries=1)
        return None, "agent run timed out"

    def ds(self, method, path, body=None, params=None):
        return http(method, AGENT_URL + path, self.key, body, params, timeout=90)


# ----------------------------------------------------------------------------- dates (from zombiecheck)
MONTHS = {m: i for i, m in enumerate("jan feb mar apr may jun jul aug sep oct nov dec".split(), 1)}
MON = r"(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?"
ISO = re.compile(r"\b(20\d{2})-(\d{2})-(\d{2})(?:[T ]\d{2}:\d{2})?")
MDY = re.compile(MON + r"\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(20\d{2})\b", re.I)
DMY = re.compile(r"\b(\d{1,2})(?:st|nd|rd|th)?\s+" + MON + r",?\s+(20\d{2})\b", re.I)
YM = re.compile(r"^(20\d{2})-(\d{2})$")


def mkdate(y, m, d):
    try:
        return date(int(y), int(m), int(d))
    except (ValueError, TypeError):
        return None


def valid(d, today):
    return d if d and date(2010, 1, 1) <= d <= today + timedelta(days=1) else None


def find_dates(text, today):
    out = [mkdate(*m.groups()) for m in ISO.finditer(text)]
    out += [mkdate(m.group(3), MONTHS[m.group(1)[:3].lower()], m.group(2)) for m in MDY.finditer(text)]
    out += [mkdate(m.group(3), MONTHS[m.group(2)[:3].lower()], m.group(1)) for m in DMY.finditer(text)]
    return [d for d in (valid(x, today) for x in out) if d]


def parse_any(s, today):
    s = (str(s) if s is not None else "").strip()
    if not s:
        return None
    m = ISO.search(s)
    if m:
        return valid(mkdate(*m.groups()), today)
    m = YM.match(s)
    if m:
        return valid(mkdate(m.group(1), m.group(2), 1), today)
    try:
        return valid(email.utils.parsedate_to_datetime(s).date(), today)
    except Exception:
        ds = find_dates(s, today)
        return max(ds) if ds else None


def in_window(d, today, days=WINDOW_DAYS):
    """True if date d (date or str) is within the last `days` days (and not in the future)."""
    if isinstance(d, str):
        d = parse_any(d, today)
    return bool(d) and today - timedelta(days=days) <= d <= today + timedelta(days=1)


def page_date(r, today):
    """Publication date of a fetched page: structured fields first, then the first date in the page head."""
    for k in ("published_date", "published_time", "date", "datePublished", "published"):
        d = parse_any(r.get(k) or (r.get("metadata") or {}).get(k), today)
        if d:
            return d, k
    text = r.get("text") or r.get("content") or ""
    m = re.search(r"\"datePublished\"\s*:\s*\"([^\"]+)\"|article:published_time[\"'][^>]*content=[\"']([^\"']+)", text)
    if m:
        d = parse_any(m.group(1) or m.group(2), today)
        if d:
            return d, "structured"
    ds = find_dates(text[:3000], today)
    return (ds[0], "text") if ds else (None, None)


# ----------------------------------------------------------------------------- text helpers
def reg_domain(host):
    host = (host or "").lower().split(":")[0]
    host = host[4:] if host.startswith("www.") else host
    p = host.split(".")
    if len(p) >= 3 and p[-2] in ("co", "com", "org", "net", "gov", "ac", "edu") and len(p[-1]) == 2:
        return ".".join(p[-3:])
    return ".".join(p[-2:]) if len(p) >= 2 else host


def host_of(u):
    return reg_domain(urllib.parse.urlparse(u or "").netloc)


def norm_name(s):
    s = unescape(str(s or "")).lower().replace("&", " and ")
    s = re.sub(r"[^a-z0-9 ]", " ", s)
    s = re.sub(r"\b(inc|ltd|llc|llp|plc|corp|co|gmbh|limited|pvt|private|sa|sas|ag|bv|ab|the)\b", " ", s)
    return re.sub(r"\s+", " ", s).strip()


INV_SUFFIX = r"\b(ventures?|capital|partners|fund|funds|vc|investments?|management|group|holdings|advisors|equity|global|labs)\b"


def core_name(s):
    """Investor/company name without generic suffixes: 'Accel Partners' -> 'accel'."""
    c = re.sub(r"\s+", " ", re.sub(INV_SUFFIX, " ", norm_name(s))).strip()
    return c if len(c) >= 3 else norm_name(s)


def plain(text):
    """Markdown/HTML -> lowercase plain words for name matching."""
    t = re.sub(r"\]\([^)]*\)", "]", text or "")          # drop markdown link targets
    t = re.sub(r"(?s)<[^>]+>", " ", t)
    return " " + norm_name(t) + " "


def name_on_page(name, ptext):
    """Whole-word match of the normalised name (or its suffix-free core) in plain page text."""
    for n in {norm_name(name), core_name(name)}:
        if n and len(n) >= 2 and f" {n} " in ptext:
            return True
    return False


def slugify(s, n=48):
    return re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", (s or "").lower())).strip("-")[:n].strip("-") or "startup"


# ----------------------------------------------------------------------------- 1. parse (deterministic)
STOP = set("""a an the and or of for to in on with by from at as is are be this that it its into via our we your their
startup startups company companies platform app apps solution solutions based building builds build which who what
helps help using use new small large very early stage scale round funding raise raising seed series pre ai-powered""".split())

SECTORS = {  # label -> trigger words; the label's words are added to the keyword set with extra weight
    "agritech": "agri agritech agtech agriculture agricultural farm farmer farmers farming crop crops harvest soil irrigation smallholder livestock dairy",
    "legaltech": "legal legaltech law lawyer lawyers attorney attorneys firm firms contract contracts litigation paralegal compliance clm",
    "climate tech": "climate cleantech carbon decarbonization emissions renewable renewables solar wind sustainability recycling circular grid-scale",
    "battery": "battery batteries lithium cathode anode cell cells recycling recycle recycled storage grid-scale",
    "energy": "energy grid grid-scale power electricity utility utilities storage battery batteries",
    "healthtech": "health healthcare medical clinic clinics patient patients hospital diagnostics telehealth",
    "fintech": "fintech payments payment banking bank lending loans credit insurance insurtech wealth",
    "edtech": "edtech education learning students teachers school schools tutoring",
    "saas": "saas software b2b workflow automation dashboard",
    "ai": "ai ml llm genai machine-learning computer-vision vision assistant copilot agent agents detection",
    "hardware": "hardware robotics robot robots device devices manufacturing industrial",
    "logistics": "logistics supply-chain shipping freight delivery warehouse",
    "mobility": "ev evs electric mobility vehicle vehicles charging",
    "proptech": "proptech real-estate property construction",
    "biotech": "biotech biology drug therapeutics genomics",
    "cybersecurity": "security cybersecurity identity fraud",
    "ecommerce": "ecommerce commerce retail marketplace d2c",
}

COUNTRIES = {  # canonical -> (aliases, region)
    "India": ("india indian bharat bengaluru bangalore mumbai delhi pune hyderabad chennai", "Asia"),
    "USA": ("usa us u.s. united-states america american silicon-valley new-york san-francisco", "North America"),
    "Canada": ("canada canadian toronto", "North America"),
    "UK": ("uk u.k. britain british england london united-kingdom", "Europe"),
    "Germany": ("germany german berlin munich", "Europe"),
    "France": ("france french paris", "Europe"),
    "Netherlands": ("netherlands dutch amsterdam", "Europe"),
    "Sweden": ("sweden swedish stockholm", "Europe"),
    "Norway": ("norway norwegian oslo", "Europe"),
    "Finland": ("finland finnish helsinki", "Europe"),
    "Denmark": ("denmark danish copenhagen", "Europe"),
    "Spain": ("spain spanish madrid barcelona", "Europe"),
    "Italy": ("italy italian milan", "Europe"),
    "Switzerland": ("switzerland swiss zurich", "Europe"),
    "Belgium": ("belgium belgian brussels", "Europe"),
    "Austria": ("austria austrian vienna", "Europe"),
    "Ireland": ("ireland irish dublin", "Europe"),
    "Poland": ("poland polish warsaw", "Europe"),
    "Portugal": ("portugal portuguese lisbon", "Europe"),
    "Estonia": ("estonia estonian tallinn", "Europe"),
    "Singapore": ("singapore singaporean", "Asia"),
    "Indonesia": ("indonesia indonesian jakarta", "Asia"),
    "Japan": ("japan japanese tokyo", "Asia"),
    "China": ("china chinese beijing shanghai shenzhen", "Asia"),
    "UAE": ("uae emirates dubai abu-dhabi", "Middle East"),
    "Israel": ("israel israeli tel-aviv", "Middle East"),
    "Nigeria": ("nigeria nigerian lagos", "Africa"),
    "Kenya": ("kenya kenyan nairobi", "Africa"),
    "South Africa": ("south-africa cape-town johannesburg", "Africa"),
    "Brazil": ("brazil brazilian sao-paulo", "Latin America"),
    "Mexico": ("mexico mexican", "Latin America"),
    "Australia": ("australia australian sydney melbourne", "Oceania"),
}
REGIONS = {"europe": "Europe", "european": "Europe", "eu": "Europe", "asia": "Asia", "africa": "Africa",
           "latam": "Latin America", "latin-america": "Latin America", "north-america": "North America",
           "middle-east": "Middle East", "mena": "Middle East", "nordics": "Europe", "sea": "Asia"}
STAGE_ORDER = ["pre-seed", "seed", "series a", "series b", "series c", "growth"]
STAGE_RE = re.compile(r"\b(pre[- ]?seed|seed|pre[- ]series[- ]a|series[- ]?([a-h])\b|growth|late[- ]stage|angel)", re.I)


def norm_stage(s):
    """Free text -> canonical stage or None. 'Series A extension' -> 'series a'; 'pre-Series A' -> 'seed'."""
    m = STAGE_RE.search(str(s or ""))
    if not m:
        return None
    t = m.group(1).lower().replace("-", " ")
    if t.startswith("pre") and "series" in t:
        return "seed"
    if t.startswith("pre"):
        return "pre-seed"
    if t == "angel":
        return "pre-seed"
    if m.group(2):
        x = m.group(2).lower()
        return f"series {x}" if x in "abc" else "growth"
    if t.startswith(("growth", "late")):
        return "growth"
    return "seed"


def stages_in(s):
    out = []
    for m in STAGE_RE.finditer(str(s or "")):
        st = norm_stage(m.group(0))
        if st and st not in out:
            out.append(st)
    return out


def _phr(text):
    """Lowercase text with multi-word place names hyphenated so they tokenise as one word."""
    t = " " + re.sub(r"[^a-z0-9.\- ]", " ", text.lower()) + " "
    for c, (al, _) in COUNTRIES.items():
        for a in al.split():
            if "-" in a:
                t = t.replace(" " + a.replace("-", " ") + " ", " " + a + " ")
    for r in REGIONS:
        if "-" in r:
            t = t.replace(" " + r.replace("-", " ") + " ", " " + r + " ")
    t = t.replace(" grid scale ", " grid-scale ").replace(" computer vision ", " computer-vision ")
    return t


def tokens(text):
    out = []
    for w in _phr(text).split():
        w = w.strip(".-")
        if not w:
            continue
        out.append(w)
        if "-" in w and not any(w in al.split() for al, _ in COUNTRIES.values()) and w not in REGIONS:
            out += [x for x in w.split("-") if len(x) > 1]
    return out


def find_country(text):
    """-> (country or None, region or None)."""
    toks = set(tokens(text))
    for c, (al, reg) in COUNTRIES.items():
        if toks & set(al.split()):
            return c, reg
    for r, reg in REGIONS.items():
        if r in toks:
            return None, reg
    return None, None


def parse_description(desc, stage=None, country=None, sector=None):
    """One-line startup description -> {keywords, weights, sectors, stages, country, region}. No LLM."""
    toks = tokens(desc)
    geo = set()
    for al, _ in COUNTRIES.values():
        geo |= set(al.split())
    geo |= set(REGIONS)
    stage_words = {"pre-seed", "seed", "series", "growth", "angel"}
    kw = []
    for w in toks:
        if w in STOP or w in geo or w in stage_words or len(w) < 2 or w.isdigit() or re.fullmatch(r"[a-h]", w):
            continue
        w = re.sub(r"(?<=[a-z]{4})s$", "", w) if w not in ("analytics", "logistics", "ags") else w
        if w not in kw:
            kw.append(w)
    sectors = []
    for label, trig in SECTORS.items():
        tset = set(trig.split())
        tset |= {re.sub(r"(?<=[a-z]{4})s$", "", t) for t in tset}
        if tset & set(kw):
            sectors.append(label)
    if sector:
        sectors = [s.strip().lower() for s in re.split(r"[,/]", sector) if s.strip()] + [s for s in sectors]
        sectors = list(dict.fromkeys(sectors))
    weights = {w: 1.0 for w in kw}
    for s in sectors:
        for w in s.split():
            if w not in STOP:
                weights[w] = max(weights.get(w, 0), 2.0)
    st = stages_in(stage) if stage else stages_in(desc)
    if country:
        c, reg = find_country(country)
        c = c or (country.strip() if not reg else None)
    else:
        c, reg = find_country(desc)
    return {"description": desc.strip(), "keywords": kw, "weights": weights, "sectors": sectors,
            "stages": st, "country": c, "region": reg}


# ----------------------------------------------------------------------------- 2. TinySet
TS_COLS = [
    ("company", "string", True, "Startup name exactly as written in the funding announcement.", None),
    ("what_it_does", "string", False, "One sentence: what the startup's product does.", None),
    ("round_stage", "string", False, "Round stage as announced.", "One of: Pre-seed | Seed | Series A | Series B | Series C | Growth | Other"),
    ("amount", "string", False, "Amount raised with currency as announced, e.g. $4.5M or INR 20 crore. Empty if undisclosed.", None),
    ("announced_date", "date", False, "Date the round was announced (article or press release date).", None),
    ("lead_investor", "string", False, "Investor(s) that led the round. Empty if no lead named.", None),
    ("other_investors", "string", False, "Other participating investors, comma separated.", None),
    ("hq_country", "string", False, "Country of the startup's headquarters.", None),
    ("source_url", "string", False, "URL of the news article or press release you opened that names the startup AND its investors. Not a database or social profile.", None),
]


def ts_description(p, today):
    since = (today - timedelta(days=WINDOW_DAYS)).isoformat()
    where = p["country"] or p["region"] or "any country"
    stage = "/".join(s.title() for s in p["stages"]) or "Pre-seed to Series A"
    d = (f"Entity: a recently funded startup similar to: \"{p['description'][:400]}\".\n"
         f"Similar means same problem or customer: {', '.join(p['sectors'][:5]) or 'same sector'}; "
         f"keywords {', '.join(p['keywords'][:12])}.\n"
         f"Prefer startups in {where} at stage {stage}, but similar startups elsewhere are fine.\n"
         f"Only funding rounds announced on or after {since}. One row per startup (its most recent round).\n"
         "Every row needs source_url: a news article or press release that names the startup and its investors. "
         "Do not use Crunchbase, PitchBook, Tracxn, LinkedIn or X as the source. Leave a cell empty rather than guess.")
    return d


def ts_spec(p, today):
    desc = ts_description(p, today)
    assert len(desc) <= TS_DESC_MAX, f"TinySet description too long ({len(desc)} > {TS_DESC_MAX})"
    cols = []
    for name, typ, pk, hint, fmt in TS_COLS:
        c = {"name": name, "label": name.replace("_", " ").title(), "type": typ, "is_primary_key": pk, "retrieval_hint": hint}
        if fmt:
            c["format"] = fmt
        cols.append(c)
    assert len(cols) <= 10 and sum(c["is_primary_key"] for c in cols) == 1
    return {"name": "funding radar: " + p["description"][:60], "description": desc, "columns": cols}


def run_tinyset(tf, p, today, rows, state_dir, poll=20, timeout=2400, dataset_id=None):
    """Create ONE dataset (or resume / reuse dataset_id), poll until ready, page through rows. Returns (rows, note)."""
    sp = os.path.join(state_dir, "tinyset.json")
    st_js = {}
    if os.path.exists(sp):
        st_js = json.load(open(sp, encoding="utf-8"))
        if st_js.get("status") == "collected" and not dataset_id:
            say(f"TinySet: reusing saved rows of dataset {st_js.get('id')} (no new spend)")
            return st_js.get("rows", []), "reused saved rows"
    ds_id = dataset_id or st_js.get("id")
    if not ds_id:
        st, js = tf.ds("POST", "/v1/datasets", {"spec": ts_spec(p, today), "target_rows": rows, "populate": True})
        if st == 403:
            say("TinySet: 403 forbidden - Datasets need a PERSONAL API key, not an organisation key. Continuing with Search only.")
            return [], "tinyset http 403 (personal key needed)"
        if st == 402:
            say("TinySet: 402 insufficient credits. Continuing with Search only.")
            return [], "tinyset http 402"
        if st not in (200, 201, 202) or not js.get("id"):
            say(f"TinySet: create failed http {st}: {str(js)[:200]}")
            return [], f"tinyset create http {st}"
        ds_id = js["id"]
        tf.stats["tinyset_datasets"] += 1
        json.dump({"id": ds_id, "status": "populating"}, open(sp, "w", encoding="utf-8"))
        say(f"TinySet: dataset {ds_id} populating ({rows} rows target)")
    deadline, final = time.time() + timeout, None
    while time.time() < deadline and not ABORT.is_set():
        st, js = tf.ds("GET", f"/v1/datasets/{ds_id}")
        final = js.get("status") if st == 200 else None
        if final in ("ready", "failed", "cancelled"):
            break
        time.sleep(poll)
    out, cursor = [], None
    while True:
        st, js = tf.ds("GET", f"/v1/datasets/{ds_id}/rows", params={"limit": 2000, **({"after": cursor} if cursor else {})})
        if st != 200:
            break
        out += [r.get("data", {}) for r in js.get("rows", [])]
        cursor = js.get("next_cursor")
        if not js.get("truncated") or not cursor:
            break
    tf.stats["tinyset_rows"] += len(out)
    status = "collected" if final in ("ready", "failed", "cancelled") else "partial"
    json.dump({"id": ds_id, "status": status, "final": final, "rows": out}, open(sp, "w", encoding="utf-8"), indent=1)
    return out, f"dataset {ds_id} {final}"


# ----------------------------------------------------------------------------- 3. Search
EXCLUDE = "linkedin.com,x.com,twitter.com,facebook.com,instagram.com,youtube.com,crunchbase.com,pitchbook.com,tracxn.com,cbinsights.com,reddit.com"


GENERIC = set("ai ml app apps assistant platform tool tools software detection draft drafts review reviews small solution genai llm".split())


def query_terms(p, n=3):
    """Most specific keywords for search: drop generic words and hyphen compounds (their parts are kept)."""
    return [k for k in p["keywords"] if k not in GENERIC and "-" not in k][:n]


def search_queries(p, n=8):
    """Templated queries by stage / country / sector. Deterministic, no LLM."""
    where = p["country"] or p["region"] or ""
    stage = p["stages"][0] if p["stages"] else ""
    secs = [s for s in p["sectors"] if s not in ("ai", "saas")] or p["sectors"] or [""]
    sec = secs[0]
    topic = " ".join(re.split(r"[,;(]", p["description"])[0].split()[:8])
    k3, k2 = " ".join(query_terms(p, 3)), " ".join(query_terms(p, 2))
    qs = [
        f"{topic} startup raises funding",
        f"{k3} startup raises {stage} {where}",
        f"{sec} startup raises {stage} round {where}",
        f"{k2} startup funding round led by",
        f"{sec} {stage} funding {where} {date.today().year}",
        f"{k3} startup secures investment",
        f"{sec} startup series a {where}",
        f"{k2} {sec} startup raises million",
    ]
    out = []
    for q in qs:
        q = re.sub(r"\b(\w+)( \1\b)+", r"\1", re.sub(r"\s+", " ", q)).strip()
        if q and q not in out:
            out.append(q)
    return out[:n]


VERB = r"(raises|raised|secures|secured|bags|bagged|closes|closed|lands|landed|nabs|gets|got|picks up|snags|announces|attracts|grabs|receives)"
AMT = r"((?:US)?\$|€|£|₹|Rs\.?|INR|USD|EUR|GBP)\s?\d[\d,.]*\s?(?:k|m|mn|bn|b|million|billion|crore|cr|lakh)?|\d[\d,.]*\s?(?:million|crore|cr|mn|m)\s?(?:dollars|euros|usd|eur|rupees)?"
HEAD_RE = re.compile(r"(?P<co>[A-Z0-9][\w.&'\-]*(?:\s+[A-Z0-9][\w.&'\-]*){0,3})\s*(?:,[^,]{0,60},)?\s+" + VERB + r"\b(?P<rest>.*)", re.S)
LED_RE = re.compile(r"\b(?:led by|co-led by|from|backed by|with participation from|participation from|investors include|including)\s+(?P<inv>[^.;:\n|()]{3,200})", re.I)
DROP_PREFIX = re.compile(r"^(exclusive|breaking|funding|update|news|report|watch|agritech|legaltech|climate|cleantech|startup|fintech|healthtech|edtech|saas)\b[:\s-]*", re.I)
NOT_INVESTOR = re.compile(r"^(others?|existing investors?|angels?|angel investors?|undisclosed|a clutch|several|various|investors?|"
                          r"a group|family offices?|clients?|its|their|the round|the company|new and existing|high net|hnis?|\d|"
                          r"seed|series|pre|round|funding|investment)\b", re.I)


CUT_RE = re.compile(r"\s(?:to|which|who|that|said|says|for|as|in|on|at|is|are|was|has|have|had|now|will|through|via|straight|"
                    r"this|these|its|their|after|before|since|during|while|where|bringing|taking|valuing|among|amid|"
                    r"the funds?|alongside existing)\s", re.I)
MONTH_START = re.compile(r"^(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+\d", re.I)
NAME_CONNECT = {"of", "de", "da", "do", "du", "la", "le", "for", "and", "&", "von", "van", "der", "y", "e", "the"}


def plausible_name(x):
    """An investor name: starts with a capital letter, 1-6 words, mostly capitalised, no digits-first / dates / sentences."""
    if not (2 < len(x) <= 60) or MONTH_START.match(x) or not re.match(r"[A-Z]|\d+[A-Za-z]|\d+ [A-Z]", x) or re.match(r"\d+ (?:[A-Z][a-z]+ )?(?:farmers|users|customers|people|jobs)\b", x):
        return False
    if NOT_INVESTOR.match(x) and not re.match(r"\d+[A-Za-z]|\d+ [A-Z]", x):
        return False
    w = x.split()
    if len(w) > 6:
        return False
    low = [t for t in w[1:] if t[:1].islower() and t.lower() not in NAME_CONNECT]
    return not low


def split_investors(s):
    s = re.sub(r"\s+", " ", unescape(s or "")).strip(" ,.")
    s = CUT_RE.split(" " + s + " ", 1)[0].strip(" ,.")
    parts = re.split(r",|;|\band\b|&(?![a-z])|\balong with\b|\bas well as\b|\bwith\b", s)
    out = []
    for x in parts:
        x = re.sub(r"^(?:and|the|existing investors?|investors?|participation from|from|including)\s+", "", x.strip(" .-\"'"), flags=re.I).strip(" .")
        x = re.sub(r"\s*\((?:lead|led)\)\s*$", "", x, flags=re.I)
        x = re.sub(r"(?:'s|’s)$", "", x)
        if plausible_name(x):
            out.append(x)
    return list(dict.fromkeys(out))


def parse_headline(title, snippet=""):
    """'Fasal raises $12M Series A led by Peak XV' -> {company, amount, round_stage, investors}. None if no deal shape."""
    t = DROP_PREFIX.sub("", re.sub(r"\s+", " ", unescape(title or "")).strip())
    t = DROP_PREFIX.sub("", t)
    m = HEAD_RE.search(t)
    if not m:
        return None
    co = m.group("co").strip(" ,'\"")
    co = re.sub(r"^(?:[A-Z][a-z]+-based|[A-Z][\w]*tech|AI|Startup)\s+", "", co).strip()
    if not co or norm_name(co) in ("startup", "it", "company", "ai") or len(co) > 50:
        return None
    rest = m.group("rest") + " " + (snippet or "")
    am = re.search(AMT, rest, re.I)
    led = LED_RE.search(m.group("rest")) or LED_RE.search(snippet or "")
    invs = split_investors(led.group("inv")) if led else []
    lead = []
    lm = re.search(r"\b(?:co-)?led by\s+([^.;:\n|()]{3,120})", rest, re.I)
    if lm:
        lead = split_investors(lm.group(1))
    return {"company": co, "amount": am.group(0).strip() if am else "", "round_stage": norm_stage(rest) or "",
            "lead_investor": ", ".join(lead), "other_investors": ", ".join(i for i in invs if i not in lead)}


def run_search(tf, p, n_queries=8):
    """-> list of candidate deals from news search results (each with source_url and the query that found it)."""
    deals = []
    for q in search_queries(p, n_queries):
        try:
            res = tf.search(q, recency_minutes=WINDOW_DAYS * 1440, exclude_domains=EXCLUDE)
        except Fatal:
            raise
        except Exception as e:  # noqa
            say(f"  search failed for {q!r}: {e}")
            continue
        for r in res[:10]:
            h = parse_headline(r.get("title", ""), r.get("snippet", ""))
            if not h or not r.get("url"):
                continue
            h.update({"source_url": r["url"], "announced_date": r.get("date") or "", "what_it_does": (r.get("snippet") or "")[:240],
                      "hq_country": "", "origin": "search", "query": q})
            deals.append(h)
    return deals


# ----------------------------------------------------------------------------- 4. Fetch verification
def investors_of(d):
    lead = split_investors(d.get("lead_investor", "")) if d.get("lead_investor") else []
    oth = split_investors(d.get("other_investors", "")) if d.get("other_investors") else []
    return lead, [o for o in oth if norm_name(o) not in {norm_name(x) for x in lead}]


SENT_RE = re.compile(r"[^.!?\n]{0,400}")


def page_investors(text, company):
    """Extra investor names from 'led by X' / 'participation from Y' sentences on the source page."""
    out_lead, out_all = [], []
    text = text or ""
    cn = core_name(company)
    # only look near mentions of the company, so newsletter boxes and sidebars don't leak names in
    spans = [(max(0, m.start() - 300), m.end() + 700) for m in re.finditer(re.escape(company), text, re.I)][:12]
    for a, b in spans:
        chunk = text[a:b]
        for m in re.finditer(r"\b(?:co-)?led by\s+([^.;:\n|()]{3,160})", chunk, re.I):
            out_lead += split_investors(m.group(1))
        for m in PAGE_LED_RE.finditer(chunk):
            out_all += split_investors(m.group("inv"))
    keep = lambda xs: [x for x in dict.fromkeys(xs) if cn not in norm_name(x)]
    return keep(out_lead)[:4], keep(out_all)[:10]


PAGE_LED_RE = re.compile(r"\b(?:backed by|with participation from|participation from|investors include|investors including|"
                         r"(?:raised|raises|secured|secures|funding|investment|round|capital)[^.;\n]{0,80}?\bfrom)\s+(?P<inv>[^.;:\n|()]{3,200})", re.I)


PARTNER_RE = [
    re.compile(r"(?P<p>[A-Z][a-z]+(?: [A-Z][a-z'\-]+){1,2}),? (?:a |the )?(?:founding |managing |general |venture |senior )?(?:partner|principal|investment director|director)(?: and [\w ]+)? (?:at|of|with) (?P<f>[A-Z][\w&.\- ]{2,40})"),
    re.compile(r"(?:partner|principal|managing director|investment director) (?:at|of) (?P<f>[A-Z][\w&.\- ]{2,40}),? (?P<p>[A-Z][a-z]+(?: [A-Z][a-z'\-]+){1,2})"),
    re.compile(r"(?P<f>[A-Z][\w&.\-]+(?: [A-Z][\w&.\-]+){0,3})(?:'s|’s) (?:founding |managing |general )?(?:partner|principal) (?P<p>[A-Z][a-z]+(?: [A-Z][a-z'\-]+){1,2})"),
]


def find_partners(text):
    """-> [(partner, fund)] named on the page, e.g. 'Jane Doe, partner at Peak XV'."""
    flat = re.sub(r"[*_\[\]#>]", "", text or "")
    out = []
    for rx in PARTNER_RE:
        for m in rx.finditer(flat):
            pn, f = m.group("p").strip(), m.group("f").strip(" .,")
            f = re.split(r"\b(?:said|says|who|which|and|in|on|for)\b", f)[0].strip(" .,")
            if f and pn and (pn, f) not in out:
                out.append((pn, f))
    return out


def verify_deal(d, page, today):
    """Apply the proof rules to one candidate deal and its live-fetched source page. Mutates and returns d.
    verified = page fetched + company named + >=1 investor named + date known and within 18 months."""
    d.setdefault("lead_partner", "")
    url = d.get("source_url") or ""
    if not re.match(r"^https?://", url):
        d.update(status="dropped", drop_reason="no source_url")
        return d
    if not page or page.get("error"):
        d.update(status="dropped", drop_reason=f"source page not fetched ({(page or {}).get('error', 'no result')})")
        return d
    text = page.get("text") or page.get("content") or ""
    pt = plain(text)
    if not name_on_page(d["company"], pt):
        d.update(status="dropped", drop_reason="company name not on source page")
        return d
    lead, oth = investors_of(d)
    pl, pa = page_investors(text, d["company"])
    lead = list(dict.fromkeys(lead + [x for x in pl if norm_name(x) not in map(norm_name, lead)]))
    named = [i for i in lead + oth + pa if name_on_page(i, pt) and not name_on_page(i, " " + norm_name(d["company"]) + " ")]
    named = list(dict.fromkeys(named))
    # 'High' next to 'High-Tech Gruenderfonds', 'MOV' next to 'MOV Investimentos': keep the full name only
    named = [x for x in named if not (len(x.split()) == 1 and any(y != x and re.match(re.escape(x) + r"[\s\-]", y) for y in named))]
    if not named:
        d.update(status="dropped", drop_reason="no investor name on source page")
        return d
    pdate, basis = page_date(page, today)
    claimed = parse_any(d.get("announced_date"), today)
    use = pdate or claimed
    if not use:
        d.update(status="dropped", drop_reason="no date on page or in claim")
        return d
    if not in_window(use, today):
        d.update(status="dropped", drop_reason=f"older than 18 months ({use.isoformat()})")
        return d
    if pdate and claimed and abs((pdate - claimed).days) > 120 and not in_window(pdate, today):
        d.update(status="dropped", drop_reason=f"page date {pdate} contradicts claimed date {claimed}")
        return d
    nl = {norm_name(x) for x in named}
    d["investors_verified"] = named
    d["lead_verified"] = [x for x in lead if norm_name(x) in nl]
    d["date"] = use.isoformat()
    d["date_basis"] = f"page ({basis})" if pdate else "claimed (page has no date)"
    if not d.get("round_stage"):
        d["round_stage"] = norm_stage(text[:4000]) or ""
    if not d.get("amount"):
        am = re.search(AMT, text[:6000], re.I)
        d["amount"] = am.group(0).strip() if am else ""
    partners = []
    for pn, fund in find_partners(text):
        for inv in named:
            if core_name(inv) and (core_name(inv) in norm_name(fund) or norm_name(fund) in norm_name(inv)):
                partners.append({"investor": inv, "partner": pn})
    d["partners"] = partners
    d.update(status="verified", drop_reason="", final_url=page.get("final_url") or url)
    return d


def dedupe(deals):
    """One candidate per (company, source_url); TinySet rows before Search rows."""
    seen, out = set(), []
    for d in deals:
        k = (company_key(d.get("company")), d.get("source_url"))
        if not k[0] or k in seen:
            continue
        seen.add(k)
        out.append(d)
    return out


def company_key(name):
    """'GROWiT India' and 'GROWiT' are the same company: drop trailing country / generic words."""
    geo = {w for al, _ in COUNTRIES.values() for w in al.split()}
    w = norm_name(name).split()
    while len(w) > 1 and (w[-1] in geo or w[-1] in ("ai", "tech", "technologies", "labs", "app", "hq")):
        w.pop()
    return " ".join(w)


def merge_verified(deals):
    """Same company verified from several pages -> keep the richest record, union its investors."""
    by = {}
    for d in deals:
        if d["status"] != "verified":
            continue
        k = company_key(d["company"])
        if k not in by:
            by[k] = d
            continue
        b = by[k]
        b.setdefault("extra_links", []).append(d["source_url"])
        for f in ("investors_verified", "lead_verified"):
            b[f] = list(dict.fromkeys(b[f] + [x for x in d[f] if norm_name(x) not in map(norm_name, b[f])]))
        b["partners"] = b.get("partners", []) + [x for x in d.get("partners", []) if x not in b.get("partners", [])]
        for f in ("amount", "round_stage", "what_it_does", "hq_country"):
            if not b.get(f) and d.get(f):
                b[f] = d[f]
    return list(by.values())


# ----------------------------------------------------------------------------- 5. rank (plain code)
W_SECTOR, W_STAGE, W_COUNTRY, W_RECENCY = 0.45, 0.20, 0.20, 0.15
LEAD_BONUS = 0.5   # a led deal counts 1.5x


def similarity(p, d, today):
    """Weighted keyword overlap + stage + country + recency, each part kept for the 'why' text. Range 0..1."""
    dt = set(tokens(" ".join(str(d.get(k, "")) for k in ("company", "what_it_does", "round_stage"))))
    dt |= {re.sub(r"(?<=[a-z]{4})s$", "", w) for w in dt}
    for label, trig in SECTORS.items():
        if set(trig.split()) & dt:
            dt |= set(label.split())
    w = p["weights"]
    hit = [k for k in w if k in dt]
    total = sum(sorted(w.values(), reverse=True)[:6]) or 1.0
    sector = min(1.0, sum(w[k] for k in hit) / total)
    ds = norm_stage(d.get("round_stage"))
    if not p["stages"] or not ds:
        stage = 0.5 if not p["stages"] else 0.0
    elif ds in p["stages"]:
        stage = 1.0
    else:
        gap = min(abs(STAGE_ORDER.index(ds) - STAGE_ORDER.index(s)) for s in p["stages"])
        stage = 0.5 if gap == 1 else 0.0
    c, reg = find_country(str(d.get("hq_country") or "") or d.get("what_it_does", ""))
    if p["country"] and c == p["country"]:
        country = 1.0
    elif p["region"] and reg == p["region"]:
        country = 0.5 if p["country"] else 1.0
    else:
        country = 0.0
    dd = parse_any(d.get("date") or d.get("announced_date"), today)
    rec = max(0.0, 1 - (today - dd).days / WINDOW_DAYS) if dd else 0.0
    parts = {"sector": round(W_SECTOR * sector, 4), "stage": round(W_STAGE * stage, 4),
             "country": round(W_COUNTRY * country, 4), "recency": round(W_RECENCY * rec, 4)}
    return round(sum(parts.values()), 4), parts, hit, ds, c


def inv_key(name):
    return core_name(name)


def rank_investors(p, deals, today):
    """Investor score = sum of similarities of the verified deals they backed (+50% for deals they led)."""
    inv = {}
    for d in deals:
        if d["status"] != "verified":
            continue
        sim, parts, hit, ds, c = similarity(p, d, today)
        d.update(similarity=sim, sim_parts=parts, keyword_hits=hit, stage_norm=ds or "", country_norm=c or "")
        leads = {inv_key(x) for x in d.get("lead_verified", [])}
        for name in d.get("investors_verified", []):
            k = inv_key(name)
            if not k:
                continue
            e = inv.setdefault(k, {"names": [], "deals": [], "score": 0.0, "lead_n": 0, "parts": {"sector": 0, "stage": 0, "country": 0, "recency": 0, "lead": 0}})
            if any(x["name"] == d["company"] for x in e["deals"]):
                continue
            e["names"].append(name)
            led = k in leads
            bonus = LEAD_BONUS * sim if led else 0.0
            e["score"] += sim + bonus
            e["lead_n"] += led
            for pk, pv in parts.items():
                e["parts"][pk] += pv
            e["parts"]["lead"] += bonus
            partner = next((x["partner"] for x in d.get("partners", []) if inv_key(x["investor"]) == k), "")
            e["deals"].append({"name": d["company"], "round": d.get("round_stage") or "", "date": d.get("date", ""),
                               "amount": d.get("amount") or "", "link": d["source_url"], "led": led, "similarity": sim,
                               "partner": partner})
    out = []
    for k, e in inv.items():
        disp = max(e["names"], key=lambda n: (e["names"].count(n), len(n)))
        e["deals"].sort(key=lambda x: (-x["similarity"], x["date"]), reverse=False)
        out.append({"investor": disp, "key": k, "score": round(e["score"], 4), "lead_n": e["lead_n"],
                    "parts": {a: round(b, 4) for a, b in e["parts"].items()}, "backed": e["deals"]})
    out.sort(key=lambda x: (-x["score"], -x["lead_n"], -len(x["backed"]), x["investor"].lower()))
    for i, r in enumerate(out, 1):
        r["rank"] = i
        r["why"] = why_text(p, r, deals)
    return out


def why_text(p, r, deals):
    """Plain-language reason, built only from the score parts."""
    b = r["backed"]
    n = len(b)
    names = ", ".join(x["name"] for x in b[:3]) + (f" +{n - 3} more" if n > 3 else "")
    bits = [f"Rank {r['rank']}, score {r['score']:.2f}: backed {n} verified similar startup{'s' if n != 1 else ''} ({names})"]
    if r["lead_n"]:
        bits.append(f"led {r['lead_n']} of {'them' if n > 1 else 'it'} (+{r['parts']['lead']:.2f} lead bonus)")
    by = {d["company"]: d for d in deals if d.get("status") == "verified"}
    kws = []
    for x in b:
        for k in by.get(x["name"], {}).get("keyword_hits", []):
            if k not in kws:
                kws.append(k)
    if kws:
        bits.append("sector overlap on " + ", ".join(f"'{k}'" for k in kws[:4]))
    else:
        bits.append("weak sector overlap")
    if p["stages"]:
        m = sum(1 for x in b if norm_stage(x["round"]) in p["stages"])
        bits.append(f"{m}/{n} at your stage ({'/'.join(p['stages'])})")
    want = p["country"] or p["region"]
    if want:
        m = sum(1 for x in b if (by.get(x["name"], {}).get("sim_parts") or {}).get("country", 0) > 0)
        bits.append(f"{m}/{n} in {want}")
    dates = sorted(x["date"] for x in b if x["date"])
    if dates:
        bits.append(f"latest deal {dates[-1]}")
    return "; ".join(bits) + "."


# ----------------------------------------------------------------------------- 6. Agent (investor's own site)
NOT_SITE = ("crunchbase.com", "linkedin.com", "pitchbook.com", "tracxn.com", "wikipedia.org", "x.com", "twitter.com", "techcrunch.com",
            "yourstory.com", "inc42.com", "entrackr.com", "economictimes.indiatimes.com", "bloomberg.com", "reuters.com", "forbes.com",
            "facebook.com", "youtube.com", "medium.com", "cbinsights.com", "signal.nfx.com", "dealroom.co", "vcsheet.com", "openvc.com",
            "sifted.eu", "eu-startups.com", "businesswire.com", "prnewswire.com", "globenewswire.com", "instagram.com", "github.com")


def investor_site(tf, name):
    """Find the investor's own website via Search: first result whose domain carries a token of its name."""
    toks = [t for t in core_name(name).split() if len(t) >= 3] or norm_name(name).split()
    for r in tf.search(f"{name} venture capital firm official website", exclude_domains=EXCLUDE)[:10]:
        u = r.get("url", "")
        h = host_of(u)
        if not h or any(h == x or h.endswith("." + x) for x in NOT_SITE):
            continue
        hb = re.sub(r"[^a-z0-9]", "", h.split(".")[0])
        if any(t in hb for t in toks) or hb in "".join(toks):
            pu = urllib.parse.urlparse(u)
            return f"{pu.scheme}://{pu.netloc}/"
    return None


AGENT_GOAL = """You are on the website of the investor "{name}". Browse it like a normal visitor (home, about, focus/thesis, FAQ,
founders/apply/contact pages). Do NOT sign in, do NOT submit any form, do NOT type into any field.
Find, using ONLY what this website states:
1. cheque_size: typical first cheque / ticket size (e.g. "$500k-$2M"). Empty if the site does not state it.
2. stage_focus: stages they invest in (e.g. "Pre-seed, Seed"). Empty if not stated.
3. reach_route: exactly one of "application form" (a pitch/apply form exists), "open intake" (they publish an email or say founders can pitch directly), "warm intro only" (site says they only take introductions), or "unknown".
4. reach_url: URL of the form, page or email address that supports reach_route.
5. partners: for any of these portfolio companies {companies}, the partner the site lists for that company, if shown.
For every fact give the URL of the exact page where you saw it. Never guess; leave a field empty if not found.
Return JSON only: {{"cheque_size":"","cheque_size_url":"","stage_focus":"","stage_focus_url":"","reach_route":"","reach_url":"","reach_source_url":"","partners":[{{"company":"","partner":"","url":""}}]}}"""

ROUTES = ("application form", "open intake", "warm intro only")


def parse_agent(res, site):
    """Agent result (dict or JSON text) -> facts that come with a page URL; everything else 'unknown'."""
    if isinstance(res, str):
        m = re.search(r"\{.*\}", res, re.S)
        try:
            res = json.loads(m.group(0)) if m else {}
        except Exception:
            res = {}
    if isinstance(res, dict) and len(res) == 1 and isinstance(next(iter(res.values())), (dict, str)) and "cheque_size" not in res:
        inner = next(iter(res.values()))
        return parse_agent(inner, site)
    res = res if isinstance(res, dict) else {}
    out = {"cheque_size": "unknown", "cheque_size_url": "", "stage_focus": "unknown", "stage_focus_url": "",
           "reach_route": "unknown", "reach_url": "", "agent_partners": []}

    def ok_url(u):
        return isinstance(u, str) and re.match(r"^(https?://|mailto:)", u.strip())
    cs, csu = str(res.get("cheque_size") or "").strip(), res.get("cheque_size_url") or ""
    if cs and ok_url(csu) and not re.search(r"\b(unknown|not (stated|found|specified)|n/?a)\b", cs, re.I):
        out["cheque_size"], out["cheque_size_url"] = cs[:120], csu.strip()
    sf, sfu = str(res.get("stage_focus") or "").strip(), res.get("stage_focus_url") or ""
    if sf and ok_url(sfu) and not re.search(r"\b(unknown|not (stated|found|specified)|n/?a)\b", sf, re.I):
        out["stage_focus"], out["stage_focus_url"] = sf[:120], sfu.strip()
    rr = str(res.get("reach_route") or "").strip().lower()
    ru = res.get("reach_url") or res.get("reach_source_url") or ""
    route = next((r for r in ROUTES if r.split()[0] in rr and (r != "open intake" or "intake" in rr or "open" in rr)), None)
    if route and ok_url(ru):
        out["reach_route"], out["reach_url"] = route, ru.strip()
        out["reach_source_url"] = (res.get("reach_source_url") or ru).strip() if ok_url(res.get("reach_source_url") or ru) else ru.strip()
    for x in res.get("partners") or []:
        if isinstance(x, dict) and x.get("partner") and x.get("company") and ok_url(x.get("url")):
            out["agent_partners"].append({"company": str(x["company"]), "partner": str(x["partner"])[:80], "url": x["url"]})
    return out


def enrich_investors(tf, ranked, top, concurrency=2, secs=240):
    """Agent runs on the top investors' own sites. Failure -> fields stay 'unknown' with a note, never a guess."""
    todo = ranked[:max(0, top)]
    blank = {"cheque_size": "unknown", "cheque_size_url": "", "stage_focus": "unknown", "stage_focus_url": "",
             "reach_route": "unknown", "reach_url": "", "agent_partners": []}
    for r in ranked:
        r.update(blank, site="", agent_note="not enriched (below --agent-top)")

    def job(r):
        site = investor_site(tf, r["investor"])
        if not site:
            r["agent_note"] = "own website not found via Search"
            return r
        r["site"] = site
        goal = AGENT_GOAL.format(name=r["investor"], companies=", ".join(x["name"] for x in r["backed"][:5]))
        res, note = tf.agent_run(site, goal, secs=secs)
        r["agent_note"] = note
        if res is not None:
            r.update(parse_agent(res, site))
        return r
    if not todo:
        return
    with ThreadPoolExecutor(max_workers=max(1, concurrency)) as ex:
        futs = [ex.submit(job, r) for r in todo]
        for f in as_completed(futs):
            try:
                r = f.result()
                say(f"  agent {r['investor']}: {r['agent_note']} | cheque={r['cheque_size']} | stage={r['stage_focus']} | route={r['reach_route']}")
            except Fatal:
                raise
            except Exception as e:  # noqa
                say(f"  agent error: {type(e).__name__}: {e}")


def lead_partner_of(r):
    """Partner who led deals for this investor: from source articles first, then from the investor's own site (Agent)."""
    found = []
    for x in r["backed"]:
        if x.get("partner"):
            found.append(f"{x['partner']} ({x['name']}, source: {x['link']})")
    for x in r.get("agent_partners", []):
        found.append(f"{x['partner']} ({x['company']}, source: {x['url']})")
    return "; ".join(dict.fromkeys(found)) or "unknown"


# ----------------------------------------------------------------------------- 7. output
CSV_COLS = ["rank", "investor", "score", "why", "backed_companies", "cheque_size", "stage_focus", "lead_partner",
            "reach_route", "reach_url", "sources"]


def investor_row(r):
    src = [x["link"] for x in r["backed"]]
    for k in ("cheque_size_url", "stage_focus_url", "reach_url"):
        if r.get(k):
            src.append(r[k])
    src += [x["url"] for x in r.get("agent_partners", [])]
    return {"rank": r["rank"], "investor": r["investor"], "score": r["score"], "why": r["why"],
            "backed_companies": [{k: x[k] for k in ("name", "round", "date", "amount", "link")} | {"led": x["led"]} for x in r["backed"]],
            "cheque_size": r.get("cheque_size", "unknown"), "cheque_size_source": r.get("cheque_size_url", ""),
            "stage_focus": r.get("stage_focus", "unknown"), "stage_focus_source": r.get("stage_focus_url", ""),
            "lead_partner": lead_partner_of(r), "reach_route": r.get("reach_route", "unknown"), "reach_url": r.get("reach_url", ""),
            "investor_site": r.get("site", ""), "agent_note": r.get("agent_note", ""),
            "score_parts": r["parts"], "sources": list(dict.fromkeys(src))}


def write_outputs(out_dir, p, deals, ranked, meta, top=15):
    os.makedirs(out_dir, exist_ok=True)
    rows = [investor_row(r) for r in ranked]
    ver = [d for d in deals if d["status"] == "verified"]
    keep = ("company", "what_it_does", "round_stage", "amount", "date", "announced_date", "date_basis", "lead_investor", "other_investors",
            "investors_verified", "lead_verified", "partners", "hq_country", "source_url", "final_url", "extra_links", "origin", "status",
            "drop_reason", "similarity", "sim_parts", "keyword_hits", "query")
    jd = {"input": {k: p[k] for k in ("description", "keywords", "sectors", "stages", "country", "region")}, "meta": meta,
          "investors": rows, "deals": [{k: d[k] for k in keep if k in d} for d in deals]}
    with open(os.path.join(out_dir, "results.json"), "w", encoding="utf-8") as f:
        json.dump(jd, f, indent=1, ensure_ascii=False)
    with open(os.path.join(out_dir, "results.csv"), "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(CSV_COLS)
        for r in rows[:top]:
            w.writerow([r["rank"], r["investor"], r["score"], r["why"],
                        " | ".join(f"{x['name']} ({x['round'] or '?'}, {x['date']}, {x['amount'] or 'undisclosed'}) {x['link']}" for x in r["backed_companies"]),
                        r["cheque_size"], r["stage_focus"], r["lead_partner"], r["reach_route"], r["reach_url"], " ".join(r["sources"])])
    with open(os.path.join(out_dir, "report.md"), "w", encoding="utf-8") as f:
        f.write(report_md(p, deals, rows, meta, top))
    return rows


def md(s):
    return str(s or "").replace("|", "/").replace("\n", " ")


def report_md(p, deals, rows, meta, top=15):
    ver = [d for d in deals if d["status"] == "verified"]
    drop = [d for d in deals if d["status"] == "dropped"]
    L = [f"# Funding Radar: {md(p['description'])}", "",
         f"Run {meta['run_date']} | sectors: {', '.join(p['sectors']) or '-'} | stage: {', '.join(p['stages']) or 'any'} | "
         f"country: {p['country'] or '-'} | region: {p['region'] or '-'}", "",
         f"**{len(ver)} verified similar companies** (from {meta['candidates']} candidates; {len(drop)} dropped), "
         f"**{len(rows)} investors ranked**. Window: rounds announced since {meta['window_start']}.", "",
         f"Showing the top {min(top, len(rows))}; the full ranked list is in results.json.", ""]
    if len(ver) < 10:
        L += [f"> Note: fewer than 10 verified similar companies were found for this description ({len(ver)}). Not padded.", ""]
    L += ["## Investors to approach", "", "| # | Investor | Score | Backed (verified link) | Cheque | Stage focus | Lead partner | Reach |", "|---|---|---|---|---|---|---|---|"]
    for r in rows[:top]:
        backed = "<br>".join(f"[{md(x['name'])}]({x['link']}) {md(x['round'])} {x['date']}{' (led)' if x['led'] else ''}" for x in r["backed_companies"])
        cs = f"[{md(r['cheque_size'])}]({r['cheque_size_source']})" if r["cheque_size_source"] else md(r["cheque_size"])
        sf = f"[{md(r['stage_focus'])}]({r['stage_focus_source']})" if r["stage_focus_source"] else md(r["stage_focus"])
        reach = f"[{r['reach_route']}]({r['reach_url']})" if r["reach_url"] else r["reach_route"]
        lp = md(re.sub(r", source: \S+\)", ")", r["lead_partner"]))
        L.append(f"| {r['rank']} | **{md(r['investor'])}** | {r['score']:.2f} | {backed} | {cs} | {sf} | {lp} | {reach} |")
    L += ["", "## Why each investor is ranked where it is", ""]
    for r in rows[:top]:
        L.append(f"{r['rank']}. **{md(r['investor'])}**: {md(r['why'])}")
    L += ["", "## Verified similar companies", "", "| Company | Round | Amount | Date | Investors named on page | Similarity | Proof |", "|---|---|---|---|---|---|---|"]
    for d in sorted(ver, key=lambda d: -d.get("similarity", 0)):
        L.append(f"| {md(d['company'])} | {md(d.get('round_stage'))} | {md(d.get('amount')) or 'undisclosed'} | {d.get('date')} | "
                 f"{md(', '.join(d.get('investors_verified', [])))} | {d.get('similarity', 0):.2f} | [link]({d['source_url']}) |")
    L += ["", f"## Dropped candidates ({len(drop)})", "", "| Company | Origin | Reason | Link |", "|---|---|---|---|"]
    for d in drop:
        L.append(f"| {md(d.get('company'))} | {d.get('origin')} | {md(d.get('drop_reason'))} | {d.get('source_url') or ''} |")
    s = meta["stats"]
    L += ["", "## TinyFish usage", "",
          f"- TinySet: {s['tinyset_datasets']} dataset created, {meta['tinyset_rows']} rows returned ({meta['tinyset_note']})",
          f"- Search: {s['search']} queries", f"- Fetch: {s['fetch_urls']} URLs in {s['fetch_calls']} calls",
          f"- Agent: {s['agent_runs']} runs, {s['agent_completed']} completed, {s['agent_403']} x 403",
          f"- Estimated TinySet cost: ${meta['tinyset_cost_est']:.2f} (Fetch/Search free; Agent metered, see wallet)", "",
          "Scores are computed by plain code: similarity = 0.45 sector-keyword overlap + 0.20 stage + 0.20 country + 0.15 recency; "
          "investor score = sum of similarities of the verified deals they backed, +50% for a deal they led. "
          "Facts marked unknown were not stated on a page we could open."]
    return "\n".join(L) + "\n"


# ----------------------------------------------------------------------------- main
def main(argv=None):
    ap = argparse.ArgumentParser(description="Funding Radar: ranked investors for a startup, every match backed by a live link.")
    ap.add_argument("description", help='e.g. "AI crop-disease detection app for smallholder farmers, seed, India"')
    ap.add_argument("--stage", help="override stage, e.g. seed or 'seed/Series A'")
    ap.add_argument("--country", help="override country or region, e.g. India, USA, Europe")
    ap.add_argument("--sector", help="override sector label(s), comma separated")
    ap.add_argument("--ts-rows", type=int, default=40, help=f"TinySet target rows (cap {TS_ROWS_CAP}; ~${TS_COST_PER_ROW:.2f}/row)")
    ap.add_argument("--no-tinyset", action="store_true")
    ap.add_argument("--ts-dataset", help="reuse an existing TinySet dataset id (no new spend)")
    ap.add_argument("--ts-poll", type=int, default=20), ap.add_argument("--ts-timeout", type=int, default=2400)
    ap.add_argument("--queries", type=int, default=8, help="Search queries to run (30/min limit)")
    ap.add_argument("--no-search", action="store_true")
    ap.add_argument("--search-per-min", type=int, default=30, help=argparse.SUPPRESS)
    ap.add_argument("--agent-top", type=int, default=8, help="Agent runs on the top N investors' own sites (0 = off)")
    ap.add_argument("--agent-concurrency", type=int, default=2)
    ap.add_argument("--agent-secs", type=int, default=240)
    ap.add_argument("--report-top", type=int, default=15, help="investors shown in report.md and results.csv (JSON keeps all)")
    ap.add_argument("--out", default="out", help="output root; results go to <out>/<slug>/")
    ap.add_argument("--slug", help="output folder name (default from description)")
    ap.add_argument("--state", default="state", help="state root (TinySet dataset id + rows, so re-runs never re-buy)")
    ap.add_argument("--yes", action="store_true", help="do not ask before TinySet spend")
    ap.add_argument("--print-spec", action="store_true", help="print parse + TinySet spec and exit (no API calls)")
    ap.add_argument("--today", help=argparse.SUPPRESS)
    a = ap.parse_args(argv)

    today = date.fromisoformat(a.today) if a.today else date.today()
    p = parse_description(a.description, a.stage, a.country, a.sector)
    slug = a.slug or slugify(a.description)
    rows_n = max(1, min(a.ts_rows, TS_ROWS_CAP))
    if a.ts_rows > TS_ROWS_CAP:
        say(f"--ts-rows capped at {TS_ROWS_CAP}")
    say(f"parsed: sectors={p['sectors']} stages={p['stages']} country={p['country']} region={p['region']} keywords={p['keywords'][:10]}")
    if a.print_spec:
        print(json.dumps({"parse": p, "tinyset_spec": ts_spec(p, today), "target_rows": rows_n, "queries": search_queries(p, a.queries)}, indent=1))
        return 0
    key = os.environ.get("TINYFISH_API_KEY", "")
    if not key:
        sys.exit("Set TINYFISH_API_KEY (personal key from https://agent.tinyfish.ai/api-keys)")
    tf = TF(key, search_per_min=a.search_per_min)
    state_dir = os.path.join(a.state, slug)
    os.makedirs(state_dir, exist_ok=True)
    t0 = time.time()

    ts_rows, ts_note = [], "skipped (--no-tinyset)"
    if not a.no_tinyset:
        saved = os.path.exists(os.path.join(state_dir, "tinyset.json"))
        if not saved and not a.ts_dataset:
            say(f"TinySet: 1 dataset x {rows_n} rows x ${TS_COST_PER_ROW:.2f} = estimated ${rows_n * TS_COST_PER_ROW:.2f}")
            if not a.yes:
                if not sys.stdin.isatty():
                    sys.exit("TinySet spend needs confirmation: re-run with --yes (or --no-tinyset).")
                say("Proceed? [y/N] ")
                if sys.stdin.readline().strip().lower() not in ("y", "yes"):
                    sys.exit("aborted")
        ts_rows, ts_note = run_tinyset(tf, p, today, rows_n, state_dir, a.ts_poll, a.ts_timeout, a.ts_dataset)
        say(f"TinySet: {len(ts_rows)} rows ({ts_note})")
    cands = []
    for r in ts_rows:
        if r.get("company"):
            cands.append({k: (str(r.get(k)) if r.get(k) is not None else "") for k, *_ in TS_COLS} | {"origin": "tinyset"})
    n_ts = len(cands)
    if not a.no_search:
        sd = run_search(tf, p, a.queries)
        known = {company_key(c["company"]) for c in cands}
        extra = [d for d in sd if company_key(d["company"]) not in known]
        say(f"Search: {tf.stats['search']} queries -> {len(sd)} deal-shaped headlines, {len(extra)} not already from TinySet")
        cands += extra
    cands = dedupe(cands)
    say(f"Fetch: verifying {len(cands)} candidate deals (company + investor must appear on the live page)")
    pages = tf.fetch([c["source_url"] for c in cands if re.match(r"^https?://", c.get("source_url", ""))], "markdown")
    for c in cands:
        verify_deal(c, pages.get(c.get("source_url")), today)
    ver_raw = sum(1 for c in cands if c["status"] == "verified")
    merged = merge_verified(cands)
    mk = {id(m) for m in merged}
    for c in cands:
        if c["status"] == "verified" and id(c) not in mk:
            c.update(status="duplicate", drop_reason="same company verified from another link")
    deals = cands
    ranked = rank_investors(p, deals, today)
    say(f"verified {len(merged)} companies ({ver_raw} links), dropped {sum(c['status'] == 'dropped' for c in deals)}, investors ranked {len(ranked)}")
    if a.agent_top and ranked:
        say(f"Agent: enriching top {min(a.agent_top, len(ranked))} investors from their own sites (concurrency {a.agent_concurrency})")
        enrich_investors(tf, ranked, a.agent_top, a.agent_concurrency, a.agent_secs)
    else:
        enrich_investors(tf, ranked, 0)
    meta = {"run_date": today.isoformat(), "window_start": (today - timedelta(days=WINDOW_DAYS)).isoformat(),
            "candidates": len(cands), "from_tinyset": n_ts, "from_search": len(cands) - n_ts,
            "verified_companies": len(merged), "verified_links": ver_raw,
            "dropped": sum(c["status"] == "dropped" for c in deals), "investors_ranked": len(ranked),
            "tinyset_rows": len(ts_rows), "tinyset_note": ts_note,
            "tinyset_cost_est": round(rows_n * TS_COST_PER_ROW, 2) if tf.stats["tinyset_datasets"] else 0.0,
            "stats": dict(tf.stats), "seconds": round(time.time() - t0, 1)}
    out_dir = os.path.join(a.out, slug)
    write_outputs(out_dir, p, deals, ranked, meta, a.report_top)
    say(f"done in {meta['seconds']}s -> {out_dir}/results.json, results.csv, report.md")
    print(json.dumps({k: meta[k] for k in ("tinyset_rows", "candidates", "verified_companies", "verified_links", "dropped", "investors_ranked", "tinyset_cost_est")} | {"stats": meta["stats"]}))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Fatal as e:
        say("fatal:", e)
        sys.exit(2)
    except KeyboardInterrupt:
        ABORT.set()
        sys.exit(130)
