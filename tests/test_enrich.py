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
