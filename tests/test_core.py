"""Offline unit tests. Run: python -m unittest discover -s tests -v   (no live API calls; a local mock serves TinyFish)."""
import contextlib, io, json, os, shutil, sys, tempfile, threading, unittest
from datetime import date, timedelta
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))
import funding_radar as fr
import mock_tinyfish as mock

T = mock.TODAY
D = lambda n: (T - timedelta(days=n)).isoformat()
AGRI = "AI crop-disease detection app for smallholder farmers, seed, India"
LEGAL = "AI assistant that drafts and reviews contracts for small law firms, seed/Series A, USA"
BATT = "grid-scale battery recycling startup, Europe"


def pg(text, url="https://n.example/a", **kw):
    return {"url": url, "final_url": url, "text": text, **kw}


class Parser(unittest.TestCase):
    def test_agritech_india_seed(self):
        p = fr.parse_description(AGRI)
        self.assertEqual(p["country"], "India")
        self.assertEqual(p["region"], "Asia")
        self.assertEqual(p["stages"], ["seed"])
        self.assertIn("agritech", p["sectors"])
        for k in ("crop", "disease", "smallholder", "farmer"):
            self.assertIn(k, p["keywords"])
        self.assertNotIn("india", p["keywords"])
        self.assertNotIn("seed", p["keywords"])

    def test_legal_usa_two_stages(self):
        p = fr.parse_description(LEGAL)
        self.assertEqual(p["country"], "USA")
        self.assertEqual(p["stages"], ["seed", "series a"])
        self.assertIn("legaltech", p["sectors"])
        self.assertIn("contract", p["keywords"])

    def test_battery_region_only(self):
        p = fr.parse_description(BATT)
        self.assertIsNone(p["country"])
        self.assertEqual(p["region"], "Europe")
        self.assertEqual(p["stages"], [])
        self.assertTrue({"battery", "climate tech"} & set(p["sectors"]))

    def test_flags_override(self):
        p = fr.parse_description(AGRI, stage="Series A", country="Kenya", sector="foodtech")
        self.assertEqual(p["stages"], ["series a"])
        self.assertEqual(p["country"], "Kenya")
        self.assertEqual(p["region"], "Africa")
        self.assertEqual(p["sectors"][0], "foodtech")
        self.assertEqual(p["weights"]["foodtech"], 2.0)

    def test_norm_stage(self):
        self.assertEqual(fr.norm_stage("Pre-Series A"), "seed")
        self.assertEqual(fr.norm_stage("pre-seed round"), "pre-seed")
        self.assertEqual(fr.norm_stage("Series B extension"), "series b")
        self.assertEqual(fr.norm_stage("Series E"), "growth")
        self.assertIsNone(fr.norm_stage("undisclosed"))

    def test_not_hardcoded_any_description(self):
        p = fr.parse_description("drone delivery of medicines to rural clinics in Kenya, pre-seed")
        self.assertEqual((p["country"], p["stages"]), ("Kenya", ["pre-seed"]))
        self.assertIn("healthtech", p["sectors"])
        self.assertTrue(fr.search_queries(p))

    def test_spec_under_limit_and_shape(self):
        long = "AI " + "very long description of a startup " * 200 + ", seed, India"
        for desc in (AGRI, LEGAL, BATT, long):
            spec = fr.ts_spec(fr.parse_description(desc), T)
            self.assertLessEqual(len(spec["description"]), fr.TS_DESC_MAX)
            self.assertLessEqual(len(spec["columns"]), 10)
            pk = [c for c in spec["columns"] if c["is_primary_key"]]
            self.assertEqual([c["name"] for c in pk], ["company"])
            self.assertNotEqual(pk[0]["type"], "boolean")

    def test_spec_assert_fires(self):
        old = fr.TS_DESC_MAX
        try:
            fr.TS_DESC_MAX = 50
            with self.assertRaises(AssertionError):
                fr.ts_spec(fr.parse_description(AGRI), T)
        finally:
            fr.TS_DESC_MAX = old


class Window(unittest.TestCase):
    def test_18_months(self):
        self.assertTrue(fr.in_window(D(10), T))
        self.assertTrue(fr.in_window(D(540), T))
        self.assertFalse(fr.in_window(D(560), T))
        self.assertFalse(fr.in_window("2023-01-05", T))
        self.assertFalse(fr.in_window("", T))
        self.assertFalse(fr.in_window((T + timedelta(days=30)).isoformat(), T))
        self.assertTrue(fr.in_window("March 3, 2026", T))
        self.assertTrue(fr.in_window("2026-05", T))


class Similarity(unittest.TestCase):
    p = fr.parse_description(AGRI)

    def deal(self, **kw):
        base = {"company": "X", "what_it_does": "crop disease detection for smallholder farmers", "round_stage": "Seed",
                "hq_country": "India", "date": D(30)}
        return {**base, **kw}

    def test_parts_and_range(self):
        s, parts, hit, ds, c = fr.similarity(self.p, self.deal(), T)
        self.assertAlmostEqual(s, sum(parts[k] for k in ("what", "sector", "stage", "country", "recency")), places=3)
        self.assertTrue(0 < s <= 1)
        self.assertEqual(parts["stage"], fr.W["stage"])
        self.assertEqual(parts["country"], fr.W["country"])
        self.assertIn("crop", hit)

    def test_ordering(self):
        good = fr.similarity(self.p, self.deal(), T)[0]
        other_sector = fr.similarity(self.p, self.deal(what_it_does="payments API for banks"), T)[0]
        other_country = fr.similarity(self.p, self.deal(hq_country="Brazil"), T)[0]
        same_region = fr.similarity(self.p, self.deal(hq_country="Indonesia"), T)[0]
        far_stage = fr.similarity(self.p, self.deal(round_stage="Series C"), T)[0]
        near_stage = fr.similarity(self.p, self.deal(round_stage="Series A"), T)[0]
        older = fr.similarity(self.p, self.deal(date=D(500)), T)[0]
        self.assertGreater(good, other_sector)
        self.assertGreater(good, same_region)
        self.assertGreater(same_region, other_country)
        self.assertGreater(near_stage, far_stage)
        self.assertGreater(good, older)


class Verify(unittest.TestCase):
    def cand(self, **kw):
        base = {"company": "Krishi AI", "lead_investor": "Alpha Ventures", "other_investors": "Beta Capital",
                "announced_date": D(70), "source_url": "https://n.example/a", "round_stage": "Seed", "amount": "$3M"}
        return {**base, **kw}

    def test_verified(self):
        d = fr.verify_deal(self.cand(), pg(f"Published {D(70)}. Krishi AI raised $3M led by Alpha Ventures."), T)
        self.assertEqual(d["status"], "verified")
        self.assertEqual(d["investors_verified"], ["Alpha Ventures"])
        self.assertEqual(d["lead_verified"], ["Alpha Ventures"])

    def test_dropped_company_missing(self):
        d = fr.verify_deal(self.cand(), pg(f"{D(5)} Alpha Ventures backs another startup."), T)
        self.assertEqual((d["status"], d["drop_reason"]), ("dropped", "company name not on source page"))

    def test_dropped_no_investor(self):
        d = fr.verify_deal(self.cand(), pg(f"{D(5)} Krishi AI launches an app."), T)
        self.assertEqual(d["drop_reason"], "no investor named near a funding phrase on source page")

    def test_dropped_old_page_even_if_claim_recent(self):
        d = fr.verify_deal(self.cand(announced_date=D(10)), pg("Published 2023-01-05. Krishi AI raised from Alpha Ventures."), T)
        self.assertEqual(d["status"], "dropped")
        self.assertIn("older than 18 months", d["drop_reason"])

    def test_dropped_fetch_error_and_no_url(self):
        self.assertIn("not fetched", fr.verify_deal(self.cand(), {"error": "page_not_found"}, T)["drop_reason"])
        self.assertEqual(fr.verify_deal(self.cand(source_url=""), None, T)["drop_reason"], "no source_url")

    def test_undated_dropped(self):
        d = fr.verify_deal(self.cand(announced_date=""), pg("Krishi AI raised from Alpha Ventures."), T)
        self.assertEqual(d["drop_reason"], "no date on page or in claim")

    def test_investor_found_on_page_when_claim_lacks_it(self):
        d = fr.verify_deal(self.cand(lead_investor="", other_investors=""),
                           pg(f"{D(3)} Krishi AI has raised $3M in a round led by Omega Capital and Zeta."), T)
        self.assertEqual(d["status"], "verified")
        self.assertIn("Omega Capital", d["investors_verified"])

    def test_fragment_names_dropped(self):
        d = fr.verify_deal(self.cand(lead_investor="High, High-Tech Gruenderfonds", other_investors=""),
                           pg(f"{D(3)} Krishi AI raised from High-Tech Gruenderfonds."), T)
        self.assertEqual(d["investors_verified"], ["High-Tech Gruenderfonds"])

    def test_word_boundary_match(self):
        pt = fr.plain("Accelerate your farm with Krishify")
        self.assertFalse(fr.name_on_page("Accel", pt))
        self.assertFalse(fr.name_on_page("Krishi AI", pt))
        self.assertTrue(fr.name_on_page("Accel Partners", fr.plain("backed by [Accel](https://accel.com)")))

    def test_partner_extraction(self):
        d = fr.verify_deal(self.cand(), pg(f"{D(5)} Krishi AI raised led by Alpha Ventures. \"Big,\" said Jane Smith, partner at Alpha Ventures."), T)
        self.assertEqual(d["partners"], [{"investor": "Alpha Ventures", "partner": "Jane Smith"}])


class Headlines(unittest.TestCase):
    def test_parse(self):
        h = fr.parse_headline("Exclusive: SoilSense raises Rs 20 crore Series A led by Gamma Partners and Beta Capital")
        self.assertEqual(h["company"], "SoilSense")
        self.assertEqual(h["round_stage"], "series a")
        self.assertIn("Gamma Partners", h["lead_investor"])
        self.assertIn("20 crore", h["amount"])
        self.assertIsNone(fr.parse_headline("Top 10 agritech trends of the year"))

    def test_junk_investor_names_rejected(self):
        self.assertEqual(fr.split_investors("Unicorn India Ventures to expand markets"), ["Unicorn India Ventures"])
        for junk in ("000 farmers", "May 28", "2026", "German startup ecosystem straight to your inbox"):
            self.assertEqual(fr.split_investors(junk), [], junk)
        self.assertEqual(fr.split_investors("Technical University of Denmark"), ["Technical University of Denmark"])
        self.assertEqual(fr.split_investors("216 Capital, 54Co and 8VC"), ["216 Capital", "54Co", "8VC"])
        self.assertEqual(fr.split_investors("000 farmers"), [])

    def test_company_key(self):
        self.assertEqual(fr.company_key("GROWiT India"), fr.company_key("GROWiT"))
        self.assertNotEqual(fr.company_key("Farm to Feed"), fr.company_key("Farm"))

    def test_page_investors_near_company_only(self):
        text = ("Newsletter: news from Big Media Group. " + "x " * 600 +
                "Krishi AI has raised $2M from Omega Capital and Zeta Partners. Krishi AI's funding now stands at $4M.")
        lead, allv = fr.page_investors(text, "Krishi AI")
        self.assertIn("Omega Capital", allv)
        self.assertNotIn("Big Media Group", allv)
        self.assertFalse(any("Krishi" in x for x in allv))

    def test_split_investors(self):
        self.assertEqual(fr.split_investors("Accel, Elevation Capital and existing investors"), ["Accel", "Elevation Capital"])


class Ranking(unittest.TestCase):
    def test_rank_and_why(self):
        p = fr.parse_description(AGRI)
        mk = lambda co, inv, lead, days, what="crop disease app for farmers": {
            "company": co, "status": "verified", "investors_verified": inv, "lead_verified": lead, "round_stage": "Seed",
            "hq_country": "India", "what_it_does": what, "date": D(days), "source_url": f"https://n.example/{co}", "amount": "$1M", "partners": []}
        deals = [mk("A1", ["Alpha Ventures", "Beta"], ["Alpha Ventures"], 20), mk("A2", ["Alpha Ventures"], [], 100),
                 mk("B1", ["Beta"], [], 20), mk("C1", ["Gamma"], ["Gamma"], 20, "payments for banks"),
                 {"company": "Z", "status": "dropped", "investors_verified": ["Zed"], "source_url": "x"}]
        r = fr.rank_investors(p, deals, T)
        names = [x["investor"] for x in r]
        self.assertEqual(names[0], "Alpha Ventures")
        self.assertNotIn("Zed", names)               # dropped deals never count
        self.assertIn("Beta", names)
        self.assertNotIn("Gamma", names)              # payments deal: below MIN_SIM, does not count
        alpha = r[0]
        self.assertEqual(alpha["lead_n"], 1)
        self.assertGreater(alpha["parts"]["lead"], 0)
        self.assertIn("A1", alpha["why"])
        self.assertIn("led 1", alpha["why"])
        self.assertIn("India", alpha["why"])
        self.assertEqual([x["rank"] for x in r], list(range(1, len(r) + 1)))

    def test_lead_bonus_breaks_tie(self):
        p = fr.parse_description(AGRI)
        base = {"status": "verified", "round_stage": "Seed", "hq_country": "India", "what_it_does": "crop app", "date": D(20), "partners": []}
        deals = [{**base, "company": "X", "investors_verified": ["Led Fund", "Follow Fund"], "lead_verified": ["Led Fund"], "source_url": "u"}]
        r = fr.rank_investors(p, deals, T)
        self.assertEqual(r[0]["investor"], "Led Fund")
        self.assertGreater(r[0]["score"], r[1]["score"])

    def test_investor_name_merge(self):
        self.assertEqual(fr.inv_key("Accel Partners"), fr.inv_key("Accel"))
        self.assertEqual(fr.inv_key("The Alpha Ventures Fund"), fr.inv_key("Alpha Ventures"))


class AgentParse(unittest.TestCase):
    def test_facts_without_url_unknown(self):
        o = fr.parse_agent({"cheque_size": "$1M", "cheque_size_url": "", "stage_focus": "Seed", "stage_focus_url": "https://f.example/x",
                            "reach_route": "Warm intro only", "reach_url": "https://f.example/contact"}, "https://f.example/")
        self.assertEqual(o["cheque_size"], "unknown")
        self.assertEqual(o["stage_focus"], "Seed")
        self.assertEqual(o["reach_route"], "warm intro only")

    def test_json_text_and_garbage(self):
        o = fr.parse_agent('Here: {"cheque_size":"$2M","cheque_size_url":"https://f.example/faq","reach_route":"open intake","reach_url":"mailto:pitch@f.example"}', "")
        self.assertEqual((o["cheque_size"], o["reach_route"]), ("$2M", "open intake"))
        self.assertEqual(fr.parse_agent("no idea", "")["reach_route"], "unknown")
        self.assertEqual(fr.parse_agent({"cheque_size": "Not stated", "cheque_size_url": "https://f.example"}, "")["cheque_size"], "unknown")


class EndToEnd(unittest.TestCase):
    """Full pipeline against the local mock: TinySet + Search + Fetch + Agent."""

    @classmethod
    def setUpClass(cls):
        cls.srv = mock.serve(0)
        cls.th = threading.Thread(target=cls.srv.serve_forever, daemon=True)
        cls.th.start()
        base = f"http://127.0.0.1:{cls.srv.server_port}"
        cls.old = (fr.FETCH_URL, fr.SEARCH_URL, fr.AGENT_URL)
        fr.FETCH_URL = fr.SEARCH_URL = fr.AGENT_URL = base
        cls.tmp = tempfile.mkdtemp()
        os.environ["TINYFISH_API_KEY"] = "sk-test-SECRET-123"
        cls.args = [AGRI, "--ts-rows", "15", "--yes", "--ts-poll", "0", "--search-per-min", "6000", "--queries", "3",
                    "--agent-top", "3", "--today", T.isoformat(), "--out", os.path.join(cls.tmp, "out"),
                    "--state", os.path.join(cls.tmp, "state"), "--slug", "agri"]
        buf_err = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()) as cls.stdout, contextlib.redirect_stderr(buf_err):
            fr.main(cls.args)
        cls.stderr = buf_err.getvalue()
        cls.res = json.load(open(os.path.join(cls.tmp, "out", "agri", "results.json"), encoding="utf-8"))

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        fr.FETCH_URL, fr.SEARCH_URL, fr.AGENT_URL = cls.old
        shutil.rmtree(cls.tmp, ignore_errors=True)
        os.environ.pop("TINYFISH_API_KEY", None)

    def test_outputs_exist(self):
        for f in ("results.json", "results.csv", "report.md"):
            self.assertTrue(os.path.exists(os.path.join(self.tmp, "out", "agri", f)), f)

    def test_verified_and_dropped(self):
        st = {d["company"]: (d["status"], d.get("drop_reason", "")) for d in self.res["deals"]}
        self.assertEqual(st["Krishi AI"][0], "verified")
        self.assertEqual(st["FarmEye"][0], "verified")
        self.assertEqual(st["SoilSense"][0], "verified")                   # added by Search, not in TinySet
        self.assertIn("older than 18 months", st["OldFarm"][1])
        self.assertEqual(st["NoInv Agro"][1], "no investor named near a funding phrase on source page")
        self.assertIn("not fetched", st["Ghost Farms"][1])
        self.assertEqual(self.res["meta"]["verified_companies"], 3)

    def test_every_backed_company_has_verified_link(self):
        ver = {d["source_url"] for d in self.res["deals"] if d["status"] == "verified"}
        for inv in self.res["investors"]:
            self.assertTrue(inv["backed_companies"])
            for b in inv["backed_companies"]:
                self.assertIn(b["link"], ver)

    def test_ranking_and_agent(self):
        inv = {i["investor"]: i for i in self.res["investors"]}
        self.assertEqual(self.res["investors"][0]["investor"], "Alpha Ventures")
        a = inv["Alpha Ventures"]
        self.assertEqual(a["cheque_size"], "$250k-$1.5M")
        self.assertTrue(a["cheque_size_source"].startswith("https://alphaventures.example/"))
        self.assertEqual(a["stage_focus"], "unknown")                       # agent gave no page URL
        self.assertEqual(a["reach_route_agent"], "application form")
        lp = a["lead_partner"]
        self.assertEqual((lp[0]["name"], lp[0]["role"], lp[0]["company"]), ("Jane Smith", "partner", "Krishi AI"))
        self.assertIn("said Jane Smith, partner at Alpha Ventures", lp[0]["sentence"])
        self.assertEqual(lp[0]["url"], "https://news.example/krishi-ai-seed")
        g = inv["Gamma Partners"]
        self.assertEqual(g["cheque_size"], "unknown")                       # agent 403 -> unknown, run continues
        self.assertIn("403", g["agent_note"])
        self.assertNotIn("Delta Fund", inv)                                 # only from a dropped deal

    def test_reach_ladder(self):
        inv = {i["investor"]: i for i in self.res["investors"]}
        a, g, b = inv["Alpha Ventures"], inv["Gamma Partners"], inv["Beta Capital"]
        self.assertEqual(a["own_domain"], "alphaventures.example")
        self.assertEqual((a["reach_route"], a["reach_url"]), ("application form", "https://alphaventures.example/apply"))
        self.assertEqual(g["reach_route"], "no unsolicited pitches")
        self.assertEqual(g["reach"]["source_url"], "https://gammapartners.example/contact")
        self.assertEqual(b["own_domain"], "")                               # site names Beta Capital but no portfolio company
        self.assertIn("entity check", b["own_domain_evidence"]["note"])
        self.assertEqual(b["reach_route"], "warm intro via founder")
        self.assertEqual(b["reach"]["company"], "Krishi AI")
        self.assertEqual(b["reach_url"], "https://news.example/krishi-ai-seed")

    def test_observed_and_type(self):
        inv = {i["investor"]: i for i in self.res["investors"]}
        ob = inv["Alpha Ventures"]["observed"]
        self.assertEqual(ob["label"], "round sizes of deals it joined (observed)")
        cur = {g["currency"]: g for g in ob["by_currency"]}
        self.assertEqual(cur["USD"]["median"], 3e6)
        self.assertEqual(cur["INR"]["median"], 1e8)                          # INR stays INR, never converted
        self.assertEqual(ob["stages"], ["seed"])
        self.assertEqual(inv["Alpha Ventures"]["investor_type"], "fund")

    def test_api_usage_rules(self):
        self.assertTrue(all(len(b) <= 10 for b in mock.LOG["fetch_batches"]))
        self.assertTrue(all("max_steps" not in json.dumps(b) for b in mock.LOG["agent_bodies"]))
        q = [x for x in mock.LOG["search_params"] if "official website" not in x["query"]
             and "include_domains" not in x and not x["query"].startswith('"')]
        self.assertTrue(q)
        self.assertTrue(all("recency_minutes" in x and "exclude_domains" in x for x in q))
        self.assertEqual(len(mock.LOG["dataset_specs"]), 1)
        self.assertEqual(mock.LOG["dataset_specs"][0]["target_rows"], 15)
        self.assertIn("estimated $1.50", self.stderr)

    def test_key_never_written(self):
        for root, _, files in os.walk(self.tmp):
            for f in files:
                self.assertNotIn("SECRET-123", open(os.path.join(root, f), encoding="utf-8", errors="replace").read())
        self.assertNotIn("SECRET-123", self.stderr)

    def test_rerun_reuses_tinyset(self):
        n = len(mock.LOG["dataset_specs"])
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            fr.main(self.args + ["--no-search", "--agent-top", "0"])
        self.assertEqual(len(mock.LOG["dataset_specs"]), n)

    def test_report_top_cap(self):
        import csv
        p = fr.parse_description(AGRI)
        ranked = [{"investor": f"Fund {i}", "rank": i, "score": 1.0 / i, "why": "w", "parts": {}, "backed": [], "lead_n": 0} for i in range(1, 21)]
        out = os.path.join(self.tmp, "cap")
        meta = dict(self.res["meta"])
        fr.write_outputs(out, p, [], ranked, meta, top=15)
        with open(os.path.join(out, "results.csv"), encoding="utf-8") as f:
            self.assertEqual(len(list(csv.reader(f))) - 1, 15)
        with open(os.path.join(out, "results.json"), encoding="utf-8") as f:
            self.assertEqual(len(json.load(f)["investors"]), 20)
        with open(os.path.join(out, "report.md"), encoding="utf-8") as f:
            rep = f.read()
        self.assertIn("Fund 15**", rep)
        self.assertNotIn("Fund 16**", rep)

    def test_csv_columns(self):
        import csv
        rows = list(csv.reader(open(os.path.join(self.tmp, "out", "agri", "results.csv"), encoding="utf-8")))
        self.assertEqual(rows[0], fr.CSV_COLS)
        self.assertGreaterEqual(len(rows), 3)


class TsCap(unittest.TestCase):
    def test_cap(self):
        self.assertEqual(fr.TS_ROWS_CAP, 60)


if __name__ == "__main__":
    unittest.main()
