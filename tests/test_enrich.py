"""Tests for the free enrichment layer: observed round sizes, lead partners, investor type, reach ladder pieces,
--funds-only, audit sample and the audit precision script. No API calls."""
import csv, json, os, shutil, sys, tempfile, unittest
from datetime import date, timedelta
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import funding_radar as fr

T = date(2026, 11, 15)
D = lambda n: (T - timedelta(days=n)).isoformat()


class Amounts(unittest.TestCase):
    def test_parse(self):
        cases = {"$3 million": ("USD", 3e6), "US$2.5 million": ("USD", 2.5e6), "Rs 12 Crore ($1.27M)": ("INR", 1.2e8),
                 "INR 10 crore": ("INR", 1e8), "2.4 million euros": ("EUR", 2.4e6), "€4.8M": ("EUR", 4.8e6),
                 "A$6.75M": ("AUD", 6.75e6), "R$ 30,3 milhões": ("BRL", 30.3e6), "£500k": ("GBP", 5e5),
                 "$1.5 million ($1.27M equity + $230K non-dilutive)": ("USD", 1.5e6), "₹25 lakh": ("INR", 2.5e6)}
        for s, want in cases.items():
            got = fr.parse_amount(s)
            self.assertEqual(got[0], want[0], s)
            self.assertAlmostEqual(got[1], want[1], delta=1, msg=s)
        for s in ("Six-digit", "undisclosed", "", "Series A in 2026"):
            self.assertIsNone(fr.parse_amount(s), s)

    def test_observed_keeps_currencies_apart(self):
        r = {"backed": [{"name": "A", "amount": "$2M", "round": "Seed", "link": "u1"},
                        {"name": "B", "amount": "$4M", "round": "Series A", "link": "u2"},
                        {"name": "C", "amount": "Rs 20 crore", "round": "Seed", "link": "u3"},
                        {"name": "D", "amount": "", "round": "Pre-seed", "link": "u4"}]}
        o = fr.observed_rounds(r)
        g = {x["currency"]: x for x in o["by_currency"]}
        self.assertEqual(set(g), {"USD", "INR"})
        self.assertEqual(g["USD"]["median"], 3e6)
        self.assertEqual(g["INR"]["median"], 2e8)
        self.assertEqual([x["link"] for x in g["USD"]["deals"]], ["u1", "u2"])
        self.assertEqual(o["undisclosed"], ["D"])
        self.assertEqual(o["stages"], ["pre-seed", "seed", "series a"])
        self.assertEqual(o["label"], "round sizes of deals it joined (observed)")
        self.assertIn("USD 3M median of 2", o["text"])


class Partners(unittest.TestCase):
    def test_patterns(self):
        t = ("Krishi AI raised $3M led by Alpha Ventures. \"Big market,\" said Jane Smith, Partner at Alpha Ventures. "
             "Beta Capital's managing partner Raj Kumar Rao added that farmers need it. "
             "Principal at Gamma Partners, Lena Fischer, will join the board. "
             "Ravi Shah, founder of Krishi AI, said the money will fund hiring.")
        got = {(x["investor"], x["name"], x["role"]) for x in fr.partner_mentions(t, ["Alpha Ventures", "Beta Capital", "Gamma Partners"], "u", "Krishi AI")}
        self.assertIn(("Alpha Ventures", "Jane Smith", "partner"), got)
        self.assertIn(("Beta Capital", "Raj Kumar Rao", "managing partner"), got)
        self.assertIn(("Gamma Partners", "Lena Fischer", "principal"), got)
        self.assertFalse(any(n == "Ravi Shah" for _, n, _ in got))          # founder of the startup, not the investor
        one = fr.partner_mentions(t, ["Alpha Ventures"], "https://n.example/a", "Krishi AI")[0]
        self.assertIn("said Jane Smith, Partner at Alpha Ventures", one["sentence"])
        self.assertEqual(one["url"], "https://n.example/a")

    def test_fund_role_name_form(self):
        t = "“Fungal threats are scary,” said Astanor partner Harry Biggs. BioScout raised A$6.75M."
        got = fr.partner_mentions(t, ["Astanor"], "https://agfundernews.com/bioscout", "BioScout")
        self.assertEqual([(x["name"], x["role"]) for x in got], [("Harry Biggs", "partner")])

    def test_fund_name_fragment_is_not_a_person(self):
        t = ("Covenant raised $4M led by Flybridge Capital Partners. Flybridge Capital Partners, general partner Jesse Middleton said. "
             "\"Great team,\" said Jesse Middleton, General Partner at Flybridge Capital Partners.")
        names = [x["name"] for x in fr.partner_mentions(t, ["Flybridge Capital Partners"], "u", "Covenant")]
        self.assertIn("Jesse Middleton", names)
        self.assertNotIn("Capital Partners", names)

    def test_none_is_unknown(self):
        r = {"key": fr.inv_key("Alpha Ventures"), "investor": "Alpha Ventures",
             "backed": [{"name": "X", "amount": "", "round": "", "link": "u", "similarity": 0.5, "date": D(5)}]}
        fr.enrich_free(None, [r], [{"company": "X", "status": "verified", "partner_mentions": []}], 0)
        self.assertEqual(r["lead_partner"], "unknown")


class Types(unittest.TestCase):
    def test_labels(self):
        cases = {"Unicorn India Ventures": "fund", "Catalyst Fund": "fund", "Exim Bank": "bank", "SIDBI": "bank",
                 "Gates Foundation": "foundation", "Sahyadri Farms": "farmer-company", "Corteva": "corporate",
                 "Corteva Catalyst": "corporate", "GrainCorp Ventures": "corporate", "Hyderabad Angels": "fund",
                 "Rainmatter": "unknown", "JITO": "unknown", "Magrabi Agriculture": "unknown"}
        for n, t in cases.items():
            self.assertEqual(fr.investor_type(n), t, n)
        self.assertEqual(fr.investor_type("Rainmatter", "rainmatter.vc"), "fund")
        self.assertTrue(set(fr.INVESTOR_TYPES) >= {"fund", "corporate", "bank", "foundation", "farmer-company", "unknown"})


class ReachPieces(unittest.TestCase):
    def test_email_intake_and_order(self):
        pages = {"https://f.example/contact": {"final_url": "https://f.example/contact",
                                               "html": "<p>Founders: send your deck to pitch@f.example</p>"}}
        r = fr.classify_reach(pages, "f.example")
        self.assertEqual((r["route"], r["url"]), ("open intake (email)", "mailto:pitch@f.example"))
        pages["https://f.example/apply"] = {"final_url": "https://f.example/apply", "html": "<h1>Apply for funding</h1><form></form>"}
        self.assertEqual(fr.classify_reach(pages, "f.example")["route"], "application form")   # (a) beats (b)

    def test_other_domain_pages_ignored(self):
        pages = {"https://other.example/apply": {"final_url": "https://other.example/apply", "html": "Apply now<form></form>"}}
        self.assertIsNone(fr.classify_reach(pages, "f.example"))

    def test_generic_email_needs_context(self):
        pages = {"https://f.example/": {"final_url": "https://f.example/", "html": "Office: hello@f.example, Berlin"}}
        self.assertIsNone(fr.classify_reach(pages, "f.example"))

    def test_warm_intro(self):
        r = {"backed": [{"name": "Krishi AI", "link": "https://n.example/k"}]}
        w = fr.warm_intro(r)
        self.assertEqual((w["route"], w["company"], w["url"]), ("warm intro via founder", "Krishi AI", "https://n.example/k"))


class FundsOnlyAndAudit(unittest.TestCase):
    def test_funds_only_rows(self):
        rows = [{"investor": "A Ventures", "investor_type": "fund"}, {"investor": "B Bank", "investor_type": "bank"},
                {"investor": "C Capital", "investor_type": "fund"}]
        self.assertEqual([r["investor"] for r in fr.shown_rows(rows, 15, True)], ["A Ventures", "C Capital"])
        self.assertEqual(len(fr.shown_rows(rows, 15, False)), 3)

    def test_audit_sample_seeded_and_precision_script(self):
        tmp = tempfile.mkdtemp()
        try:
            deals = [{"company": f"Co{i}", "status": "verified", "counted": True, "source_url": f"https://n.example/{i}",
                      "investors_verified": [f"Fund {i}", f"Other {i}"],
                      "proof_snippets": {f"Fund {i}": f"Co{i} raised a seed round led by Fund {i}. Other text."}} for i in range(15)]
            n = fr.write_audit(tmp, deals, 20)
            self.assertEqual(n, 20)
            with open(os.path.join(tmp, "audit.csv"), encoding="utf-8") as f:
                rows = list(csv.reader(f))
            self.assertEqual(rows[0], ["investor", "company", "link", "sentence", "correct y/n"])
            first = rows[1:]
            fr.write_audit(tmp, deals, 20)
            with open(os.path.join(tmp, "audit.csv"), encoding="utf-8") as f:
                self.assertEqual(list(csv.reader(f))[1:], first)                # seed 42 -> same sample every time
            # fill and score
            for i, r in enumerate(first):
                r[4] = "y" if i % 4 else "n"
            filled = os.path.join(tmp, "filled.csv")
            with open(filled, "w", encoding="utf-8", newline="") as f:
                csv.writer(f).writerows([rows[0]] + first)
            sub = os.path.join(tmp, "SUBMISSION.md")
            with open(sub, "w", encoding="utf-8") as f:
                f.write("# S\n\nintro\n")
            sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
            import audit_precision
            res = audit_precision.main([filled, "--submission", sub, "--label", "demo 1"])
            self.assertEqual((res["correct"], res["wrong"], res["blank"]), (15, 5, 0))
            self.assertAlmostEqual(res["precision"], 0.75)
            txt = open(sub, encoding="utf-8").read()
            self.assertIn("demo 1: 15 / 20 correct = 75.0%", txt)
            audit_precision.main([filled, "--submission", sub, "--label", "demo 1"])   # idempotent: one block per label
            self.assertEqual(open(sub, encoding="utf-8").read().count("demo 1: 15 / 20"), 1)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class Sections(unittest.TestCase):
    rows = [{"rank": 1, "investor": "A Ventures", "investor_type": "fund"}, {"rank": 2, "investor": "B Bank", "investor_type": "bank"},
            {"rank": 3, "investor": "Rainmatter", "investor_type": "unknown"}, {"rank": 4, "investor": "Corteva", "investor_type": "corporate"},
            {"rank": 5, "investor": "C Capital", "investor_type": "fund"}]

    def test_split_keeps_order(self):
        s = dict(fr.sections(self.rows, 15))
        self.assertEqual([r["rank"] for r in s["main"]], [1, 3, 5])
        self.assertEqual([r["rank"] for r in s["other"]], [2, 4])

    def test_all_in_one_and_funds_only(self):
        self.assertEqual([r["rank"] for r in dict(fr.sections(self.rows, 15, all_in_one=True))["all"]], [1, 2, 3, 4, 5])
        s = fr.sections(self.rows, 15, funds_only=True)
        self.assertEqual([(n, [r["rank"] for r in p]) for n, p in s], [("main", [1, 5])])

    def test_section_labels(self):
        for t, sec in (("fund", "main"), ("unknown", "main"), ("bank", "other"), ("corporate", "other"),
                       ("foundation", "other"), ("farmer-company", "other")):
            self.assertEqual(fr.section_of(t), sec)

    def test_report_has_both_sections(self):
        tmp = tempfile.mkdtemp()
        try:
            p = fr.parse_description("AI crop-disease detection app for smallholder farmers, seed, India")
            mk = lambda n, rank, t: {"investor": n, "rank": rank, "score": 1.0 / rank, "base_score": 1.0 / rank, "why": "w", "parts": {},
                                     "backed": [], "lead_n": 0, "investor_type": t, "section": fr.section_of(t)}
            ranked = [mk("A Ventures", 1, "fund"), mk("Exim Bank", 2, "bank"), mk("Rainmatter", 3, "unknown")]
            meta = {"run_date": T.isoformat(), "window_start": D(548), "candidates": 0, "tinyset_rows": 0, "tinyset_note": "",
                    "tinyset_cost_est": 0, "stats": {k: 0 for k in ("tinyset_datasets", "search", "fetch_urls", "fetch_calls", "agent_runs",
                                                                    "agent_completed", "agent_403")}}
            fr.write_outputs(tmp, p, [], ranked, meta)
            rep = open(os.path.join(tmp, "report.md"), encoding="utf-8").read()
            self.assertLess(rep.index("## Investors to approach"), rep.index("## Other backers (not seed funds)"))
            self.assertLess(rep.index("**Rainmatter**"), rep.index("## Other backers"))
            self.assertGreater(rep.index("**Exim Bank**"), rep.index("## Other backers"))
            with open(os.path.join(tmp, "results.csv"), encoding="utf-8") as f:
                rows = list(csv.reader(f))
            self.assertEqual([(r[0], r[2]) for r in rows[1:]], [("main", "A Ventures"), ("main", "Rainmatter"), ("other", "Exim Bank")])
            with open(os.path.join(tmp, "results.json"), encoding="utf-8") as f:
                js = json.load(f)
            self.assertEqual([(i["investor"], i["section"]) for i in js["investors"]],
                             [("A Ventures", "main"), ("Exim Bank", "other"), ("Rainmatter", "main")])   # one full list, rank order
            fr.write_outputs(tmp, p, [], ranked, meta, all_in_one=True)
            self.assertNotIn("Other backers", open(os.path.join(tmp, "report.md"), encoding="utf-8").read().split("## Verified")[0])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class PartnerPass(unittest.TestCase):
    class FakeTF:
        def __init__(self, results, pages):
            self.results, self.pages, self.queries = results, pages, []

        def search(self, q, **kw):
            self.queries.append(q)
            return self.results.get(q, [])

        def fetch(self, urls, fmt="markdown"):
            return {u: ({"text": self.pages[u]} if u in self.pages else {"error": "x"}) for u in urls}

    def inv(self, name, company):
        return {"investor": name, "backed": [{"name": company}], "lead_partner": "unknown"}

    def test_found_only_on_pages_naming_both(self):
        tf = self.FakeTF({"Myca Kriscore Capital partner": [{"url": "u1"}, {"url": "u2"}, {"url": "u3"}, {"url": "u4"}]},
                         {"u1": "Kriscore Capital partner Asha Rao spoke at a summit.",                                   # no company
                          "u2": "Myca raised from Kriscore Capital. \"Huge,\" said Asha Rao, Partner at Kriscore Capital.",
                          "u4": "Myca and Kriscore Capital. Vik Iyer, partner at Kriscore Capital."})                       # 4th result: not fetched
        r = self.inv("Kriscore Capital", "Myca")
        n = fr.partner_pass(tf, [r])
        self.assertEqual(n, 1)
        self.assertEqual([(x["name"], x["url"], x["company"]) for x in r["lead_partner"]], [("Asha Rao", "u2", "Myca")])
        self.assertIn("said Asha Rao, Partner at Kriscore Capital", r["lead_partner"][0]["sentence"])

    def test_nothing_found_is_unknown_and_cap(self):
        tf = self.FakeTF({}, {})
        rs = [self.inv(f"Fund {i}", f"Co{i}") for i in range(20)]
        n = fr.partner_pass(tf, rs, top=20)
        self.assertEqual(n, fr.PARTNER_PASS_MAX)
        self.assertTrue(all(r["lead_partner"] == "unknown" for r in rs))

    def test_skips_investors_with_partner(self):
        tf = self.FakeTF({}, {})
        r = self.inv("A Fund", "Co")
        r["lead_partner"] = [{"name": "X Y"}]
        self.assertEqual(fr.partner_pass(tf, [r]), 0)


class OwnSitePaths(unittest.TestCase):
    def test_extra_paths(self):
        for pth in ("/portfolio", "/companies", "/investments", "/contact"):
            self.assertIn(pth, fr.REACH_PATHS)

    def test_ladder_reads_portfolio_page(self):
        pages = {"https://f.example/portfolio": {"final_url": "https://f.example/portfolio",
                                                 "html": "<h1>Portfolio</h1><p>Founders: pitch us at deals@f.example</p>"}}
        self.assertEqual(fr.classify_reach(pages, "f.example")["url"], "mailto:deals@f.example")


class OfflineRescoreKeepsLiveEnrichment(unittest.TestCase):
    def test_keeps_reach_and_search_partners_and_drops_fragments(self):
        import contextlib, io
        tmp = tempfile.mkdtemp()
        try:
            p = fr.parse_description("AI assistant that drafts and reviews contracts for small law firms, seed/Series A, USA")
            deal = {"company": "Covenant", "what_it_does": "AI contract review for law firms", "hq_country": "USA", "round_stage": "Seed",
                    "date": D(30), "status": "verified", "counted": True, "source_url": "https://n.example/cov", "amount": "$4M",
                    "investors_verified": ["Flybridge Capital Partners", "Collide Capital"], "lead_verified": ["Flybridge Capital Partners"],
                    "partner_mentions": [{"investor": "Flybridge Capital Partners", "name": "Capital Partners", "role": "general partner",
                                          "sentence": "s", "url": "https://n.example/cov"},
                                         {"investor": "Flybridge Capital Partners", "name": "Jesse Middleton", "role": "general partner",
                                          "sentence": "s", "url": "https://n.example/cov"}]}
            reach = {"route": "open intake (email)", "url": "mailto:investors@collidecap.com", "source_url": "https://collidecap.com/contact"}
            jd = {"input": {k: p[k] for k in ("description", "keywords", "sectors", "stages", "country", "region")},
                  "meta": {"run_date": T.isoformat(), "window_start": D(548), "candidates": 1, "tinyset_rows": 1, "tinyset_note": "",
                           "tinyset_cost_est": 0, "stats": {k: 0 for k in ("tinyset_datasets", "search", "fetch_urls", "fetch_calls",
                                                                           "agent_runs", "agent_completed", "agent_403")}},
                  "investors": [{"investor": "Flybridge Capital Partners", "agent_note": "completed", "cheque_size": "$1M to $3M",
                                 "cheque_size_source": "https://www.flybridge.com/faq", "stage_focus": "unknown",
                                 "reach_route": "warm intro via founder", "reach_url": "https://n.example/cov",           # ladder result
                                 "reach_route_agent": "application form", "reach_url_agent": "https://www.flybridge.com/apply",
                                 "reach": {"route": "warm intro via founder", "url": "https://n.example/cov", "company": "Covenant"}},
                                {"investor": "Collide Capital", "reach": reach, "own_domain": "collidecap.com", "agent_note": "not enriched",
                                 "cheque_size": "unknown", "stage_focus": "unknown", "reach_route": "open intake (email)",
                                 "lead_partner": [{"investor": "Collide Capital", "name": "Aaron Samuels", "role": "founder", "sentence": "s",
                                                   "url": "https://x.example/a", "company": "Covenant", "via": "search pass"}]}],
                  "deals": [deal]}
            path = os.path.join(tmp, "results.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump(jd, f)
            with contextlib.redirect_stdout(io.StringIO()):
                fr.rescore(path)
            with open(path, encoding="utf-8") as f:
                out = {i["investor"]: i for i in json.load(f)["investors"]}
            self.assertEqual(out["Collide Capital"]["reach_url"], "mailto:investors@collidecap.com")
            self.assertEqual(out["Collide Capital"]["own_domain"], "collidecap.com")
            self.assertEqual([x["name"] for x in out["Collide Capital"]["lead_partner"]], ["Aaron Samuels"])
            fb = out["Flybridge Capital Partners"]
            self.assertEqual([x["name"] for x in fb["lead_partner"]], ["Jesse Middleton"])
            self.assertEqual((fb["reach_route_agent"], fb["reach_url_agent"]), ("application form", "https://www.flybridge.com/apply"))
            self.assertEqual(fb["reach_route"], "warm intro via founder")
            self.assertEqual(fb["cheque_size"], "$1M to $3M")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class Ampersand(unittest.TestCase):
    def test_not_split(self):
        self.assertEqual(fr.split_investors("White & Case, Accel and Sequoia"), ["White & Case", "Accel", "Sequoia"])
        self.assertEqual(fr.split_investors("Rainmatter, SIDBI & Exim Bank"), ["Rainmatter", "SIDBI", "Exim Bank"])

    def test_list_ampersand_not_rejoined(self):
        d = {"investors_verified": ["Rainmatter", "SIDBI", "Exim Bank"],
             "page_excerpt": "ScaNxt gets Zerodha's Rainmatter, SIDBI & Exim Bank's support"}
        self.assertEqual(fr.rejoin_ampersand(d), [])
        self.assertEqual(d["investors_verified"], ["Rainmatter", "SIDBI", "Exim Bank"])

    def test_rejoin_saved(self):
        d = {"investors_verified": ["White", "Case", "Accel"], "lead_verified": ["White"],
             "page_excerpt": "Clauze raised a seed round led by White & Case with participation from Accel."}
        self.assertEqual(fr.rejoin_ampersand(d), ["White & Case"])
        self.assertEqual(d["investors_verified"], ["White & Case", "Accel"])
        self.assertEqual(d["lead_verified"], ["White & Case"])

    def test_invest_in_headline(self):
        h = fr.parse_headline("White & Case announces strategic investment in Saudi AI contract review platform Clauze.AI")
        self.assertEqual((h["company"], h["other_investors"]), ("Clauze.AI", "White & Case"))
        h = fr.parse_headline("Accel invests in Krishi AI")
        self.assertEqual((h["company"], h["other_investors"]), ("Krishi AI", "Accel"))
        h = fr.parse_headline("Krishi AI raises $3M seed round led by Alpha Ventures")      # normal shape unchanged
        self.assertEqual(h["company"], "Krishi AI")

    def test_company_fragment_dropped(self):
        cand = {"company": "Case", "lead_investor": "", "other_investors": "White", "announced_date": D(10), "source_url": "https://n.example/w"}
        page = {"url": "https://n.example/w", "final_url": "https://n.example/w",
                "text": f"{D(10)} White & Case has invested in AI contract review start-up Clauze.AI. The investment from White & Case reflects."}
        d = fr.verify_deal(cand, page, T)
        self.assertEqual(d["status"], "dropped")
        self.assertIn("headline mis-parse", d["drop_reason"])

    def test_no_rejoin_without_text(self):
        d = {"investors_verified": ["White", "Case"], "page_excerpt": "White led it. Case joined."}
        self.assertEqual(fr.rejoin_ampersand(d), [])


class ServeRequests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.calls = []

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def runner(self, argv):
        self.calls.append(argv)
        slug = argv[argv.index("--slug") + 1]
        os.makedirs(os.path.join(self.tmp, "out", slug), exist_ok=True)
        with open(os.path.join(self.tmp, "out", slug, "results.json"), "w", encoding="utf-8") as f:
            json.dump({"investors": [], "meta": {}}, f)

    def req(self, body):
        return fr.rank_request(body, os.path.join(self.tmp, "out"), os.path.join(self.tmp, "state"), self.runner)

    def err(self, body):
        with self.assertRaises(fr.RankError) as cm:
            self.req(body)
        return cm.exception

    def test_default_is_free(self):
        self.req({"description": "AI contract review for small law firms", "country": "USA"})
        a = self.calls[0]
        self.assertIn("--no-tinyset", a)
        self.assertNotIn("--yes", a)
        self.assertEqual(a[a.index("--agent-top") + 1], "0")
        self.assertEqual(a[a.index("--country") + 1], "USA")

    def test_tinyset_needs_confirm(self):
        e = self.err({"description": "x startup", "ts_rows": 20})
        self.assertEqual((e.code, e.status, e.extra["estimated_tinyset_usd"]), ("confirm_cost_required", 402, 2.0))
        self.assertEqual(self.calls, [])

    def test_confirm_needs_cap(self):
        self.assertEqual(self.err({"description": "x startup", "confirm_cost": True}).code, "ts_rows_required")
        self.assertEqual(self.err({"description": "x startup", "confirm_cost": True, "ts_rows": 500}).code, "ts_rows_invalid")
        self.assertEqual(self.err({"description": "x startup", "confirm_cost": "yes", "ts_rows": 10}).code, "confirm_cost_required")
        self.assertEqual(self.err({"description": "x startup", "agent_top": 3}).code, "confirm_cost_required")
        self.assertEqual(self.calls, [])

    def test_confirmed_run(self):
        self.req({"description": "x startup", "confirm_cost": True, "ts_rows": 12, "agent_top": 2})
        a = self.calls[0]
        self.assertEqual(a[a.index("--ts-rows") + 1], "12")
        self.assertIn("--yes", a)
        self.assertNotIn("--no-tinyset", a)

    def test_bad_input(self):
        self.assertEqual(self.err({}).code, "description_required")
        self.assertEqual(self.err([1]).code, "invalid_json")


if __name__ == "__main__":
    unittest.main()
