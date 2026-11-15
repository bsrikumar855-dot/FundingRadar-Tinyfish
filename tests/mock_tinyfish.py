#!/usr/bin/env python3
"""Local stand-in for TinyFish Fetch / Search / Agent / Datasets with a small fake web of funding news.
Dates are relative to TODAY so the 18-month window logic is exercised. Records what clients sent (LOG)."""
import json, re, sys, uuid
from datetime import date, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

TODAY = date(2026, 11, 15)
DS, RUNS = {}, {}
LOG = {"fetch_batches": [], "search_params": [], "agent_bodies": [], "dataset_specs": []}


def d(days):
    return (TODAY - timedelta(days=days)).isoformat()


PAGES = {
    "https://news.example/krishi-ai-seed": f"# Krishi AI raises $3M seed round led by Alpha Ventures\n\nPublished {d(70)}\n\n"
        "Krishi AI, a Bengaluru-based startup that detects crop disease from phone photos for smallholder farmers, "
        "has raised $3M in a seed round led by Alpha Ventures, with participation from Beta Capital. "
        "\"Farmers lose a third of their crop,\" said Jane Smith, partner at Alpha Ventures.",
    "https://news.example/farmeye-seed": f"# FarmEye bags seed funding\n\n{d(160)}\n\nFarmEye, an Indian crop disease detection app for farmers, "
        "raised INR 10 crore from Alpha Ventures and Gamma Partners.",
    "https://news.example/oldfarm": "# OldFarm raises seed\n\nPublished 2023-01-05\n\nOldFarm, a crop app, raised money from Alpha Ventures.",
    "https://news.example/noinvestor": f"# NoInv Agro launches app\n\n{d(30)}\n\nNoInv Agro, a farmer app, launched today in Pune.",
    "https://news.example/soilsense": f"# SoilSense raises Rs 20 crore Series A led by Gamma Partners\n\n{d(40)}\n\n"
        "SoilSense, an Indian soil and crop analytics startup for farmers, raised Rs 20 crore led by Gamma Partners.",
    # investor sites for the reach ladder
    "https://alphaventures.example/": "<html><body><h1>Alpha Ventures</h1><p>Seed investors in agritech. Portfolio: Krishi AI, FarmEye.</p>"
        "<a href='/apply'>Apply</a></body></html>",
    "https://alphaventures.example/apply": "<html><body><h2>Submit your pitch</h2><form action='/send'><input name='deck'></form></body></html>",
    "https://gammapartners.example/": "<html><body><h1>Gamma Partners</h1><p>We backed SoilSense and FarmEye.</p></body></html>",
    "https://gammapartners.example/contact": "<html><body>Gamma Partners does not accept unsolicited pitches. Reach us through a portfolio founder.</body></html>",
    "https://betacapital.example/": "<html><body><h1>Beta Capital</h1><p>A different Beta Capital, real-estate lender.</p></body></html>",
}
SITES = {"alpha ventures": "alphaventures.example", "gamma partners": "gammapartners.example", "beta capital": "betacapital.example"}


def page(url):
    if url in PAGES:
        return {"url": url, "final_url": url, "title": PAGES[url].split("\n")[0].strip("# "), "text": PAGES[url]}
    return None


TS_ROWS = [
    {"company": "Krishi AI", "what_it_does": "Crop disease detection for smallholder farmers", "round_stage": "Seed", "amount": "$3M",
     "announced_date": d(70), "lead_investor": "Alpha Ventures", "other_investors": "Beta Capital", "hq_country": "India",
     "source_url": "https://news.example/krishi-ai-seed"},
    {"company": "FarmEye", "what_it_does": "Crop disease app for farmers", "round_stage": "Seed", "amount": "INR 10 crore",
     "announced_date": d(160), "lead_investor": "", "other_investors": "Alpha Ventures, Gamma Partners", "hq_country": "India",
     "source_url": "https://news.example/farmeye-seed"},
    {"company": "OldFarm", "what_it_does": "Crop app", "round_stage": "Seed", "amount": "", "announced_date": d(20),  # claim says recent, page says 2023
     "lead_investor": "Alpha Ventures", "other_investors": "", "hq_country": "India", "source_url": "https://news.example/oldfarm"},
    {"company": "NoInv Agro", "what_it_does": "Farmer app", "round_stage": "Seed", "amount": "$1M", "announced_date": d(30),
     "lead_investor": "Delta Fund", "other_investors": "", "hq_country": "India", "source_url": "https://news.example/noinvestor"},
    {"company": "Ghost Farms", "what_it_does": "Hallucinated", "round_stage": "Seed", "amount": "$9M", "announced_date": d(10),
     "lead_investor": "Alpha Ventures", "other_investors": "", "hq_country": "India", "source_url": "https://news.example/does-not-exist"},
]


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send(self, code, obj):
        b = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def body(self):
        n = int(self.headers.get("Content-Length", 0))
        return json.loads(self.rfile.read(n) or b"{}")

    def do_POST(self):
        u, b = urlparse(self.path), self.body()
        if u.path == "/":  # Fetch
            LOG["fetch_batches"].append(list(b["urls"]))
            if len(b["urls"]) > 10:
                return self.send(400, {"error": "max 10 urls"})
            res, errs = [], []
            for url in b["urls"]:
                r = page(url)
                (res.append(r) if r else errs.append({"url": url, "error": "page_not_found", "status": 404}))
            return self.send(200, {"results": res, "errors": errs})
        if u.path == "/v1/datasets":
            LOG["dataset_specs"].append(b)
            if len(b["spec"]["description"]) > 2000:
                return self.send(400, {"error": {"code": "INVALID_INPUT"}})
            did = str(uuid.uuid4())
            DS[did] = {"polls": 0, "n": b.get("target_rows", 10)}
            return self.send(202, {"id": did, "status": "populating"})
        if u.path == "/v1/automation/run-async":
            LOG["agent_bodies"].append(b)
            if "max_steps" in json.dumps(b):
                return self.send(403, {"error": "max_steps is beta-only"})
            if "forbidden" in b["url"]:
                return self.send(403, {"error": "forbidden"})
            rid = str(uuid.uuid4())
            RUNS[rid] = {"url": b["url"], "polls": 0}
            return self.send(200, {"run_id": rid})
        self.send(404, {})

    def do_GET(self):
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        if u.path == "/":  # Search
            LOG["search_params"].append(q)
            query = q.get("query", "")
            if "official website" in query:
                name = query.split(" venture")[0]
                slug = re.sub(r"[^a-z]", "", name.lower())
                if slug.startswith("gamma"):
                    slug = "forbidden-gamma"
                return self.send(200, {"results": [{"title": "x", "url": f"https://www.crunchbase.com/organization/{slug}"},
                                                   {"title": name, "url": f"https://{slug}.example/about"}]})
            if q.get("include_domains"):           # site-restricted search: pages we have on that domain
                dom = q["include_domains"]
                hits = [{"title": "p", "url": u} for u in PAGES if urlparse(u).netloc == dom]
                return self.send(200, {"results": hits})
            qm = re.match(r'^"([^"]+)"', query)
            if qm:                                  # investor-name search: news + its own site
                site = SITES.get(qm.group(1).lower())
                res = [{"title": "news", "url": "https://news.example/krishi-ai-seed"}]
                if site:
                    res.append({"title": qm.group(1), "url": f"https://{site}/"})
                return self.send(200, {"results": res})
            return self.send(200, {"results": [
                {"title": "SoilSense raises Rs 20 crore Series A led by Gamma Partners", "snippet": "Indian soil and crop analytics startup for farmers",
                 "url": "https://news.example/soilsense", "date": d(40)},
                {"title": "Top 10 agritech trends of the year", "snippet": "listicle", "url": "https://news.example/trends", "date": d(5)},
                {"title": "Krishi AI raises $3M seed round led by Alpha Ventures", "snippet": "", "url": "https://news.example/krishi-ai-seed", "date": d(70)},
            ]})
        rm = re.match(r"/v1/runs/([\w-]+)$", u.path)
        if rm and rm.group(1) in RUNS:
            r = RUNS[rm.group(1)]
            r["polls"] += 1
            if r["polls"] < 2:
                return self.send(200, {"run_id": rm.group(1), "status": "RUNNING"})
            host = urlparse(r["url"]).netloc
            return self.send(200, {"run_id": rm.group(1), "status": "COMPLETED", "result": {
                "cheque_size": "$250k-$1.5M", "cheque_size_url": f"https://{host}/faq",
                "stage_focus": "Pre-seed, Seed", "stage_focus_url": "",  # no URL -> must become unknown
                "reach_route": "application form", "reach_url": f"https://{host}/apply",
                "partners": [{"company": "Krishi AI", "partner": "Jane Smith", "url": f"https://{host}/portfolio/krishi"}]}})
        m = re.match(r"/v1/datasets/([\w-]+)(/rows)?$", u.path)
        if m and m.group(1) in DS:
            ds = DS[m.group(1)]
            if not m.group(2):
                ds["polls"] += 1
                return self.send(200, {"id": m.group(1), "status": "ready" if ds["polls"] > 1 else "populating"})
            return self.send(200, {"rows": [{"_id": str(i), "data": r} for i, r in enumerate(TS_ROWS)], "truncated": False, "next_cursor": None})
        self.send(404, {})


def serve(port=0):
    srv = ThreadingHTTPServer(("127.0.0.1", port), H)
    return srv


if __name__ == "__main__":
    serve(int(sys.argv[1]) if len(sys.argv) > 1 else 8799).serve_forever()
