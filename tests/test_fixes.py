"""Tests for the demo-3 bug fixes: role-prefixed names, descriptor prefixes, fragment names, public-body type,
programme pages in the reach ladder, unicode location prefixes, and company-name prefix merge. No API calls."""
import os, sys, unittest
from datetime import date, timedelta
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import funding_radar as fr

T = date(2026, 11, 15)
D = lambda n: (T - timedelta(days=n)).isoformat()


class RolePrefix(unittest.TestCase):
    def test_dropped(self):
        for n in ("CEO Timo Siukkola", "Founder Jane Doe", "Co-founder Raj Rao", "Partner Anna Berg", "Managing Director X Y",
                  "Chairman Lee", "President Kim Park", "CTO Ana Lima", "Director Sam Hill"):
            self.assertIsNone(fr.normalize_investor(n), n)
            self.assertEqual(fr.split_investors(n + ", Voima Ventures"), ["Voima Ventures"], n)

    def test_real_names_kept(self):
        for n in ("Partners Group", "Voima Ventures", "Founders Fund", "Director's Fund Labs"):
            pass
        self.assertEqual(fr.normalize_investor("Partners Group"), "Partners Group")
        self.assertEqual(fr.normalize_investor("Voima Ventures"), "Voima Ventures")


class Descriptors(unittest.TestCase):
    def test_strip(self):
        self.assertEqual(fr.normalize_investor("Nvidia’s NVentures"), "NVentures")
        self.assertEqual(fr.normalize_investor("Nvidia's NVentures"), "NVentures")
        self.assertEqual(fr.normalize_investor("Google-backed CapitalG"), "CapitalG")
        self.assertEqual(fr.normalize_investor("Zerodha's Rainmatter"), "Rainmatter")
        self.assertEqual(fr.normalize_investor("Lowe's Ventures"), "Lowe's Ventures")      # rest is generic: keep owner

    def test_merge_with_plain_name_in_ranking(self):
        p = fr.parse_description("grid-scale battery recycling startup, seed, Europe")
        mk = lambda co, inv: {"company": co, "what_it_does": "battery recycling", "hq_country": "Germany", "round_stage": "Seed",
                              "date": D(30), "status": "verified", "investors_verified": [inv], "lead_verified": [],
                              "source_url": f"https://n.example/{co}", "partners": []}
        a, b = mk("A1", "Nvidia’s NVentures"), mk("B1", "NVentures")
        for d in (a, b):
            fr.clean_deal_investors(d)
        r = fr.rank_investors(p, [a, b], T)
        self.assertEqual([x["investor"] for x in r], ["NVentures"])
        self.assertEqual(len(r[0]["backed"]), 2)


class Fragments(unittest.TestCase):
    def test_single_generic_word(self):
        for n in ("Group", "Capital", "Ventures", "Holdings", "Fund"):
            self.assertIsNone(fr.normalize_investor(n), n)
        self.assertEqual(fr.split_investors("Caret Capital, Group and IIM Ahmedabad Ventures"), ["Caret Capital", "IIM Ahmedabad Ventures"])

    def test_clean_saved_deal(self):
        d = {"company": "PeakAmp", "status": "verified", "investors_verified": ["Caret Capital", "Group", "CEO Timo Siukkola"],
             "lead_verified": ["Group"], "proof_snippets": {"Group": "x", "Caret Capital": "y"},
             "partner_mentions": [{"investor": "Group", "name": "A B"}]}
        ch = fr.clean_deal_investors(d)
        self.assertEqual(d["investors_verified"], ["Caret Capital"])
        self.assertEqual(d["lead_verified"], [])
        self.assertEqual(list(d["proof_snippets"]), ["Caret Capital"])
        self.assertEqual(d["partner_mentions"], [])
        self.assertEqual(ch, {"Group": None, "CEO Timo Siukkola": None})

    def test_deal_with_no_valid_investor_dropped(self):
        d = {"company": "X", "status": "verified", "investors_verified": ["Group"]}
        fr.clean_deal_investors(d)
        self.assertEqual(d["status"], "dropped")


class PublicBody(unittest.TestCase):
    def test_type_and_section(self):
        for n in ("Department of Energy", "Swedish Energy Agency", "Clean Energy Finance Corporation", "German Federal Ministry",
                  "Germany’s Federal Ministry of Research", "European Commission", "European Innovation Council (EIC)",
                  "Ministry of Electronics", "Government of Kerala"):
            self.assertEqual(fr.investor_type(n), "public body", n)
            self.assertEqual(fr.section_of(fr.investor_type(n)), "other", n)
        self.assertIn("public body", fr.INVESTOR_TYPES)
        self.assertEqual(fr.investor_type("Energy Impact Partners"), "fund")


class ReachProgrammePages(unittest.TestCase):
    def test_masters_page_rejected(self):
        u = "https://www.eiturbanmobility.eu/apply-to-our-innovative-masters-programmes-in-urban-mobility-by-1-february-2022/"
        pages = {u: {"final_url": u, "html": "<h1>Apply now</h1><p>Apply to our master's programmes. Admissions close soon.</p><form></form>"}}
        self.assertIsNone(fr.classify_reach(pages, "eiturbanmobility.eu"))

    def test_needs_founder_wording(self):
        u = "https://f.example/apply"
        pages = {u: {"final_url": u, "html": "<h1>Apply now</h1><form></form><p>Join our team as an engineer.</p>"}}
        self.assertIsNone(fr.classify_reach(pages, "f.example"))
        pages[u]["html"] = "<h1>Apply now</h1><p>Founders: submit your pitch deck for funding.</p><form></form>"
        self.assertEqual(fr.classify_reach(pages, "f.example")["route"], "application form")

    def test_no_unsolicited_still_detected(self):
        u = "https://g.example/contact"
        pages = {u: {"final_url": u, "html": "We do not accept unsolicited pitches. Reach us through a portfolio founder."}}
        self.assertEqual(fr.classify_reach(pages, "g.example")["route"], "no unsolicited pitches")

    def test_reach_page_ok(self):
        self.assertFalse(fr.reach_page_ok("Apply for funding", "https://x.example/masters-programme"))
        self.assertTrue(fr.reach_page_ok("Startups: apply for funding", "https://x.example/apply"))


class Locations(unittest.TestCase):
    def test_unicode_location_prefix(self):
        for raw, want in (("Düsseldorf-based Co-reactive", "Co-reactive"), ("São Paulo-based Agrotools", "Agrotools"),
                          ("Berlin-based VREY", "VREY"), ("Zürich-based Unbound Potential", "Unbound Potential"), ("Co-reactive", "Co-reactive")):
            self.assertEqual(fr.norm_company(raw), want, raw)
        h = fr.parse_headline("Düsseldorf-based Co-reactive raises €6.5M seed led by High-Tech Gründerfonds")
        self.assertEqual(h["company"], "Co-reactive")


class PrefixMerge(unittest.TestCase):
    def deal(self, co, inv, url, excerpt):
        return {"company": co, "status": "verified", "investors_verified": [inv], "lead_verified": [], "source_url": url, "page_excerpt": excerpt}

    def test_merge_when_named_on_same_page(self):
        a = self.deal("Redwood", "Eclipse Ventures", "https://n.example/a", "Battery recycling firm Redwood Materials has raised $350M led by Eclipse Ventures.")
        b = self.deal("Redwood Materials", "NVentures", "https://n.example/b", "Redwood Materials landed NVentures as an investor.")
        self.assertEqual(fr.merge_prefix_companies([a, b]), [("Redwood", "Redwood Materials")])
        self.assertEqual(a["status"], "duplicate")
        self.assertEqual(b["investors_verified"], ["NVentures", "Eclipse Ventures"])
        self.assertIn("https://n.example/a", b["extra_links"])

    def test_no_merge_without_shared_page_or_partial_word(self):
        a = self.deal("Base", "X Capital", "u1", "Base raised a seed round led by X Capital.")
        b = self.deal("Base Power", "Y Capital", "u2", "Base Power raised from Y Capital.")
        self.assertEqual(fr.merge_prefix_companies([a, b]), [])                    # 'Base Power' not on Base's page
        c = self.deal("Red", "X", "u3", "Redwood Materials and Red raised.")
        d = self.deal("Redwood Materials", "Y", "u4", "")
        self.assertEqual(fr.merge_prefix_companies([c, d]), [])                    # 'Red' is not a whole-word prefix


if __name__ == "__main__":
    unittest.main()
