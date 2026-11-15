"""Tests for the match-quality rules: self-published proof, funding-phrase proximity, what-it-does similarity,
country weight, similarity threshold, agent-entity check, country note, and offline rescoring. No API calls."""
import json, os, shutil, sys, tempfile, unittest
from datetime import date, timedelta
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import funding_radar as fr

T = date(2026, 11, 15)
D = lambda n: (T - timedelta(days=n)).isoformat()
AGRI = fr.parse_description("AI crop-disease detection app for smallholder farmers, seed, India")


def pg(text, url="https://news.example/a"):
    return {"url": url, "final_url": url, "text": text}


def deal(company="X", what="", country="India", stage="Seed", days=30, invs=("Fund A",), lead=(), url="https://news.example/x", **kw):
    return {"company": company, "what_it_does": what, "hq_country": country, "round_stage": stage, "date": D(days), "status": "verified",
            "investors_verified": list(invs), "lead_verified": list(lead), "source_url": url, "partners": [], **kw}


class Rule1SelfPublished(unittest.TestCase):
    def test_is_self_published(self):
        self.assertTrue(fr.is_self_published("AgFunder", "https://agfundernews.com/olsaro-raises-5-5m"))
        self.assertTrue(fr.is_self_published("Calculus Capital", "https://calculuscapital.com/resurrect-bio"))
        self.assertTrue(fr.is_self_published("Astanor Ventures", "https://www.astanor.com/news/x"))
        self.assertFalse(fr.is_self_published("PINC", "https://agfundernews.com/olsaro-raises-5-5m"))
        self.assertFalse(fr.is_self_published("Accel", "https://techcrunch.com/x"))

    def test_drop_unless_third_party_link(self):
        a = deal("OlsAro", invs=["AgFunder", "PINC"], lead=["AgFunder"], url="https://agfundernews.com/olsaro")
        b = deal("Resurrect", invs=["AgFunder"], url="https://agfundernews.com/resurrect", extra_links=["https://prnewswire.com/resurrect"])
        c = deal("Solo", invs=["AgFunder"], url="https://agfundernews.com/solo")
        n = fr.apply_self_published([a, b, c])
        self.assertEqual(n, 2)
        self.assertEqual(a["investors_verified"], ["PINC"])
        self.assertEqual(a["lead_verified"], [])
        self.assertEqual(a["self_published_dropped"], ["AgFunder"])
        self.assertEqual(b["investors_verified"], ["AgFunder"])            # third-party page also proves it
        self.assertEqual(c["status"], "dropped")
        self.assertIn("self-published", c["drop_reason"])

    def test_dropped_match_never_ranks(self):
        a = deal("OlsAro", what="crop disease detection app for smallholder farmers", invs=["AgFunder", "PINC"], url="https://agfundernews.com/olsaro")
        fr.apply_self_published([a])
        names = [r["investor"] for r in fr.rank_investors(AGRI, [a], T)]
        self.assertEqual(names, ["PINC"])


class Rule1Proximity(unittest.TestCase):
    cand = {"company": "Krishi AI", "lead_investor": "", "other_investors": "Alpha Ventures", "announced_date": D(10),
            "source_url": "https://news.example/a"}

    def test_name_far_from_funding_phrase_is_not_proof(self):
        text = f"{D(10)} Krishi AI launches its crop app. " + "Filler text about farming. " * 20 + \
               "Our newsletter is sponsored by Alpha Ventures."
        d = fr.verify_deal(dict(self.cand), pg(text), T)
        self.assertEqual(d["status"], "dropped")
        self.assertEqual(d["drop_reason"], "no investor named near a funding phrase on source page")

    def test_name_near_funding_phrase_is_proof(self):
        for phrase in ("led by", "backed by", "investors include", "with participation from", "funded by"):
            text = f"{D(10)} Krishi AI announced a seed round {phrase} Alpha Ventures."
            d = fr.verify_deal(dict(self.cand), pg(text), T)
            self.assertEqual(d["status"], "verified", phrase)

    def test_real_phrasings_from_demo1(self):
        cases = [("Catalyst Fund", "The round attracted a diverse group of investors, including Catalyst Fund, Mercy Corps Ventures."),
                 ("Triple Jump", "Pyramidia Ventures has raised $1.5 million from Dutch impact investor Triple Jump to expand."),
                 ("Nucleus Capital", "Bindbridge has raised $3.8 million from Nucleus Capital and Speedinvest."),
                 ("Yield Lab Latam", "A rodada foi liderada pelo The Yield Lab Latam, fundo global."),
                 ("Astanor", "A rodada contou com a participação da Astanor, que agora se junta ao time de investidores."),
                 ("Alpha", "Die Runde wurde angeführt von Alpha."), ("Alpha", "Une levée menée par Alpha.")]
        for name, text in cases:
            self.assertTrue(fr.near_funding(name, text), text)

    def test_listicle_table_is_not_proof(self):
        t = "| RoboCare | Tunisia | Precision-agriculture AI | Six-figure seed | 216 Capital | | AgriDex | Pan-African |"
        self.assertFalse(fr.near_funding("216 Capital", t))

    def test_near_funding_window(self):
        self.assertTrue(fr.near_funding("Alpha Ventures", "The round led by Alpha Ventures closed."))
        self.assertFalse(fr.near_funding("Alpha Ventures", "led by Beta. " + "x" * 400 + " Alpha Ventures sponsors this page."))


class Rule2Similarity(unittest.TestCase):
    def test_what_it_does_beats_broad_sector(self):
        exact = fr.similarity(AGRI, deal(what="AI app that detects crop disease for smallholder farmers"), T)
        broad = fr.similarity(AGRI, deal(what="agritech startup selling salt-tolerant wheat seed"), T)
        self.assertGreater(exact[0], 0.6)
        self.assertTrue(broad[1]["sector_only_penalty"])
        self.assertLess(broad[0], fr.MIN_SIM)                               # sector-only deal does not count
        self.assertIn("crop", exact[2])
        self.assertIn("disease", exact[2])

    def test_page_excerpt_counts_toward_overlap(self):
        no_page = fr.similarity(AGRI, deal(what="agritech startup"), T)[0]
        with_page = fr.similarity(AGRI, deal(what="agritech startup", page_excerpt="uses phone photos to detect crop disease for farmers"), T)[0]
        self.assertGreater(with_page, no_page)

    def test_country_weight(self):
        what = "AI crop disease detection for smallholder farmers"
        india = fr.similarity(AGRI, deal(what=what, country="India"), T)
        kenya = fr.similarity(AGRI, deal(what=what, country="Kenya"), T)
        indo = fr.similarity(AGRI, deal(what=what, country="Indonesia"), T)
        self.assertGreaterEqual(india[0] - kenya[0], 0.25 - 1e-9)
        self.assertGreater(indo[0], kenya[0])                               # same region gets a share
        self.assertLess(indo[0], india[0])

    def test_threshold_excludes_deal_from_investor_score(self):
        good = deal("Good", what="crop disease detection for farmers", invs=["Fund A"])
        weak = deal("Weak", what="payments for banks", invs=["Fund A", "Fund B"])
        r = fr.rank_investors(AGRI, [good, weak], T)
        self.assertEqual([x["investor"] for x in r], ["Fund A"])
        self.assertEqual([b["name"] for b in r[0]["backed"]], ["Good"])
        self.assertFalse(weak["counted"])
        self.assertTrue(good["counted"])
        self.assertIn("what", weak["sim_parts"])                            # components kept for results.json

    def test_india_investor_outranks_foreign_on_same_deals(self):
        what = "crop disease detection app for smallholder farmers"
        deals = [deal("In1", what=what, country="India", invs=["India Fund"]),
                 deal("Ke1", what=what, country="Kenya", invs=["Africa Fund"])]
        r = fr.rank_investors(AGRI, deals, T)
        self.assertEqual(r[0]["investor"], "India Fund")


class CountryFactor(unittest.TestCase):
    def test_factor_applied_and_explained(self):
        what = "crop disease detection app for smallholder farmers"
        deals = [deal("In1", what=what, country="India", invs=["Home Fund"]),
                 deal("Ke1", what=what, country="Kenya", invs=["Away Fund", "Home Fund"]),
                 deal("Ke2", what=what, country="Kenya", invs=["Away Fund"])]
        r = {x["investor"]: x for x in fr.rank_investors(AGRI, deals, T)}
        h, a = r["Home Fund"], r["Away Fund"]
        self.assertEqual(h["country_factor"], fr.COUNTRY_FACTOR)
        self.assertTrue(h["has_target_country_deal"])
        self.assertAlmostEqual(h["score"], round(h["base_score"] * 1.5, 4), places=3)
        self.assertIn("x1.5 country factor", h["why"])
        self.assertEqual(a["country_factor"], 1.0)
        self.assertEqual(a["score"], a["base_score"])
        self.assertIn("no verified deal in India", a["why"])

    def test_factor_reorders(self):
        what = "crop disease detection app for smallholder farmers"
        deals = [deal("In1", what=what, country="India", invs=["Home Fund"]),
                 deal("Ke1", what=what, country="Kenya", invs=["Away Fund"]),
                 deal("Ke2", what=what, country="Kenya", invs=["Away Fund"], days=200)]
        names = [x["investor"] for x in fr.rank_investors(AGRI, deals, T)]
        self.assertEqual(names[0], "Home Fund")

    def test_factor_in_output_row(self):
        what = "crop disease detection app for smallholder farmers"
        r = fr.rank_investors(AGRI, [deal("In1", what=what, country="India", invs=["Home Fund"])], T)[0]
        r.update(cheque_size="unknown", stage_focus="unknown", reach_route="unknown")
        row = fr.investor_row(r)
        self.assertEqual((row["country_factor"], row["has_target_country_deal"]), (1.5, True))

    def test_region_target(self):
        p = fr.parse_description("grid-scale battery recycling startup, Europe")
        what = "grid-scale battery recycling"
        r = {x["investor"]: x for x in fr.rank_investors(p, [deal("De1", what=what, country="Germany", invs=["EU Fund"]),
                                                             deal("Us1", what=what, country="USA", invs=["US Fund"])], T)}
        self.assertEqual(r["EU Fund"]["country_factor"], 1.5)
        self.assertEqual(r["US Fund"]["country_factor"], 1.0)


class Refetch(unittest.TestCase):
    class FakeTF:
        def __init__(self, pages):
            self.pages, self.calls = pages, []

        def fetch(self, urls, fmt="markdown"):
            self.calls.append((list(urls), fmt))
            return {u: ({"text": self.pages[u]} if u in self.pages else {"error": "bot_blocked"}) for u in urls}

    def test_refetch_applies_proximity_on_any_link(self):
        a = deal("Krishi", invs=["Alpha Ventures", "Sidebar Fund"], lead=["Alpha Ventures"], url="https://n.example/a",
                 extra_links=["https://n.example/a2"])
        b = deal("Blocked", invs=["Beta"], url="https://n.example/blocked")
        c = deal("Gone", invs=["Gamma"], url="https://n.example/c")
        tf = self.FakeTF({"https://n.example/a": "Krishi raised a seed round led by Alpha Ventures. " + "x " * 300 + "Sidebar Fund newsletter",
                          "https://n.example/a2": "Krishi news. " + "y " * 300 + "Sidebar Fund is hiring.",
                          "https://n.example/c": "Gone launched. " + "z " * 300 + "Gamma sponsors this site."})
        st = fr.refetch_proximity(tf, [a, b, c])
        self.assertEqual(tf.calls[0][1], "markdown")
        self.assertEqual(a["investors_verified"], ["Alpha Ventures"])
        self.assertIn("led by Alpha Ventures", a["proof_snippets"]["Alpha Ventures"])
        self.assertTrue(a["page_excerpt"].startswith("Krishi"))
        self.assertEqual(b["investors_verified"], ["Beta"])                   # fetch failed: kept, marked unchecked
        self.assertFalse(b["proximity_checked"])
        self.assertEqual(c["status"], "dropped")
        self.assertEqual(st["removed"], 2)
        self.assertEqual(st["failed_companies"], ["Blocked"])
        self.assertEqual(st["links"], 4)


class Rule3AgentEntity(unittest.TestCase):
    def inv(self, name, url, backed=("OlsAro",), partners=()):
        return {"investor": name, "backed": [{"name": b} for b in backed], "agent_partners": list(partners),
                "cheque_size": "EUR 200k-1M", "cheque_size_url": url, "stage_focus": "Seed", "stage_focus_url": url,
                "reach_route": "application form", "reach_url": url, "agent_note": "completed"}

    def test_domain_match_passes(self):
        for name, url in (("Astanor", "https://astanor.com/venture/"), ("Unicorn India Ventures", "https://www.unicornivc.com/faq"),
                          ("Corteva", "https://www.openinnovation.corteva.com/proposal-upload.html")):
            r = self.inv(name, url)
            self.assertEqual(fr.check_agent_entity(r), [], name)
            self.assertEqual(r["cheque_size"], "EUR 200k-1M")

    def test_short_name_other_domain_rejected(self):
        r = self.inv("PINC", "https://www.pauliggroup.com/this-is-pinc")
        rj = fr.check_agent_entity(r)
        self.assertEqual(len(rj), 3)
        self.assertEqual((r["cheque_size"], r["stage_focus"], r["reach_route"]), ("unknown",) * 3)
        self.assertIn("rejected", r["agent_note"])

    def test_page_mentions_portfolio_company_passes(self):
        u = "https://www.pauliggroup.com/this-is-pinc"
        r = self.inv("PINC", u)
        self.assertEqual(fr.check_agent_entity(r, {u: "PINC portfolio: OlsAro, Mycoverse and others"}), [])
        r = self.inv("PINC", u)
        self.assertEqual(len(fr.check_agent_entity(r, {u: "Paulig coffee and spices"})), 3)

    def test_agent_portfolio_finding_on_same_domain_passes(self):
        u = "https://www.pauliggroup.com/this-is-pinc"
        r = self.inv("PINC", u, partners=[{"company": "OlsAro", "partner": "A B", "url": "https://www.pauliggroup.com/pinc/portfolio"}])
        self.assertEqual(fr.check_agent_entity(r), [])


class Rule4CountryNote(unittest.TestCase):
    def test_few_home_deals_note(self):
        ver = [deal("K1", country="Kenya"), deal("K2", country="Kenya"), deal("N1", country="Netherlands"), deal("I1", country="India")]
        txt = "\n".join(fr.country_note(AGRI, ver))
        self.assertIn("Verified deals by country: Kenya 2, India 1, Netherlands 1.", txt)
        self.assertIn("Few verified India seed deals (1); remaining matches are from Kenya (2), Netherlands (1)", txt)
        ver.append(deal("I2", country="India", stage="Series B"))
        txt = " ".join(fr.country_note(AGRI, ver))
        self.assertIn("remaining matches are from India at other stages (1), Kenya (2)", txt)

    def test_no_note_when_enough(self):
        ver = [deal(f"I{i}", country="India") for i in range(6)]
        self.assertNotIn("Few verified", "\n".join(fr.country_note(AGRI, ver)))


class Rescore(unittest.TestCase):
    def test_offline_rescore_rewrites_outputs(self):
        tmp = tempfile.mkdtemp()
        try:
            deals = [deal("OlsAro", what="crop disease detection for farmers", invs=["AgFunder", "PINC"], url="https://agfundernews.com/olsaro"),
                     deal("Krishi", what="crop disease detection app for smallholder farmers", invs=["Alpha Ventures"],
                          page_excerpt="Krishi raised a seed round led by Alpha Ventures.")]
            jd = {"input": {k: AGRI[k] for k in ("description", "keywords", "sectors", "stages", "country", "region")},
                  "meta": {"run_date": T.isoformat(), "window_start": D(548), "candidates": 2, "tinyset_rows": 2, "tinyset_note": "x",
                           "tinyset_cost_est": 0, "stats": {"tinyset_datasets": 1, "search": 0, "fetch_urls": 2, "fetch_calls": 1,
                                                             "agent_runs": 1, "agent_completed": 1, "agent_403": 0}},
                  "investors": [{"investor": "PINC", "cheque_size": "EUR 1M", "cheque_size_source": "https://www.pauliggroup.com/this-is-pinc",
                                 "stage_focus": "unknown", "stage_focus_source": "", "reach_route": "unknown", "reach_url": "",
                                 "investor_site": "https://pinc.com/", "agent_note": "completed"}],
                  "deals": deals}
            path = os.path.join(tmp, "results.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump(jd, f)
            import contextlib, io
            with contextlib.redirect_stdout(io.StringIO()):
                fr.rescore(path)
            with open(path, encoding="utf-8") as f:
                out = json.load(f)
            names = [i["investor"] for i in out["investors"]]
            self.assertNotIn("AgFunder", names)
            pinc = next(i for i in out["investors"] if i["investor"] == "PINC")
            self.assertEqual(pinc["cheque_size"], "unknown")
            self.assertEqual(out["meta"]["self_published_dropped"], 1)
            for f_ in ("results.csv", "report.md"):
                self.assertTrue(os.path.exists(os.path.join(tmp, f_)))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
