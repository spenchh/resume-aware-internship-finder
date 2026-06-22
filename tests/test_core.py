"""Unit tests — focused on the freshness validation (the critical path) plus
parsing, matching, dedup, and report rendering. All offline (no network)."""

from __future__ import annotations

import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from internfinder import domain, freshness_validator as fv, matcher, report_generator
from internfinder.cache import Cache
from internfinder.models import (
    CONF_APPROXIMATE, CONF_UNVERIFIED, CONF_VERIFIED,
    LIVE_DEAD, LIVE_OK, LIVE_UNKNOWN, Listing,
)
from internfinder.resume_parser import parse_resume
from internfinder.sources import base, github_lists, schemaorg

TODAY = datetime.now(timezone.utc).date()
SAMPLE_RESUME = Path(__file__).resolve().parent.parent / "sample_data" / "sample_resume.txt"


def mk(company="SiFive", title="FPGA Design Intern", **kw) -> Listing:
    return Listing(company=company, title=title, **kw)


class TestDomain(unittest.TestCase):
    def test_synonym_expansion_bidirectional(self):
        w = domain.expand_terms({"verilog"})
        self.assertIn("rtl", w)
        self.assertIn("fpga", w)
        self.assertGreater(w["verilog"], w["rtl"])  # exact > synonym

    def test_extract_known_terms_multiword(self):
        terms = domain.extract_known_terms("Experience with digital design and power electronics.")
        self.assertIn("digital design", terms)
        self.assertIn("power electronics", terms)


class TestResumeParser(unittest.TestCase):
    def test_parses_hardware_resume(self):
        p = parse_resume(str(SAMPLE_RESUME), {"domain": {"priority_keywords": ["FPGA", "RTL"]}})
        self.assertIn("verilog", [s.lower() for s in p.skills])
        self.assertIn("fpga", p.weighted_keywords)
        self.assertTrue(p.tools_languages)
        self.assertTrue(any("intern" in t.lower() for t in p.experience_titles))
        self.assertIn("electrical", (p.major or "").lower())
        # priority keyword pinned high
        self.assertGreaterEqual(p.weighted_keywords.get("fpga", 0), 1.4)


class TestDateParsing(unittest.TestCase):
    def test_epoch_ms(self):
        # Lever createdAt is epoch ms
        ms = int(datetime(2026, 6, 1, tzinfo=timezone.utc).timestamp() * 1000)
        self.assertEqual(base.parse_date_loose(ms), date(2026, 6, 1))

    def test_iso(self):
        self.assertEqual(base.parse_date_loose("2026-06-01T12:00:00Z"), date(2026, 6, 1))

    def test_relative(self):
        d, phrase = base.parse_relative_date("Posted 5 days ago")
        self.assertEqual(d, TODAY - timedelta(days=5))
        self.assertIn("5", phrase)


class TestGithubTableParsing(unittest.TestCase):
    MD = """
| Company | Role | Location | Application/Link | Date Posted |
| ------- | ---- | -------- | ---------------- | ----------- |
| **[SiFive](https://sifive.com)** | RTL Design Intern | Santa Clara, CA | <a href="https://job/apply1"><img src="x.png"></a> <a href="https://simplify.jobs/p/abc">s</a> | Jun 18 |
| ↳ | FPGA Verification Intern | Remote | <a href="https://job/apply2">Apply</a> | Jun 15 |
| **[DeadCo](https://x.com)** | Closed Intern | NYC | 🔒 | Jun 10 |
"""

    def test_open_rows_and_closed_dropped(self):
        rows = github_lists.parse_markdown_table(self.MD, "demo/repo")
        self.assertEqual(len(rows), 2)  # DeadCo (🔒) dropped
        self.assertEqual(rows[0].company, "SiFive")
        self.assertEqual(rows[1].company, "SiFive")  # ↳ continuation
        self.assertEqual(rows[0].apply_url, "https://job/apply1")  # img + simplify skipped

    def test_no_year_date_inference_past(self):
        # "Oct 15" with today in mid-year => previous October, not a future date
        d = github_lists._parse_explicit_date("Oct 15")
        self.assertIsNotNone(d)
        self.assertLessEqual(d, TODAY + timedelta(days=7))


class TestSchemaOrg(unittest.TestCase):
    HTML = """<html><head><script type="application/ld+json">
    {"@context":"https://schema.org","@type":"JobPosting","title":"Embedded Firmware Intern",
     "datePosted":"2026-06-10","validThrough":"2026-12-31",
     "hiringOrganization":{"name":"ChipCo"},
     "jobLocation":{"address":{"addressLocality":"Austin","addressRegion":"TX"}},
     "description":"Work on firmware, RTOS, embedded C."}
    </script></head><body></body></html>"""

    def test_parses_jobposting(self):
        rows = schemaorg.parse_jobposting_jsonld(self.HTML, "https://chipco.com/jobs/1")
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertEqual(r.company, "ChipCo")
        self.assertEqual(r.posted_date, date(2026, 6, 10))
        self.assertEqual(r.deadline, date(2026, 12, 31))
        self.assertEqual(r.date_confidence, CONF_VERIFIED)

    def test_open_flag_expired(self):
        expired = self.HTML.replace("2026-12-31", "2000-01-01")
        self.assertFalse(schemaorg.has_open_jobposting(expired))
        self.assertTrue(schemaorg.has_open_jobposting(self.HTML))


class TestFreshnessDates(unittest.TestCase):
    def test_resolve_keeps_hard_date(self):
        l = mk(posted_date=TODAY, date_confidence=CONF_VERIFIED, date_source="lever createdAt")
        fv.resolve_date(l)
        self.assertEqual(l.date_confidence, CONF_VERIFIED)

    def test_resolve_falls_back_to_first_seen(self):
        l = mk()
        l.first_seen = datetime.now(timezone.utc) - timedelta(days=2)
        fv.resolve_date(l)
        self.assertEqual(l.date_confidence, CONF_APPROXIMATE)
        self.assertEqual(l.posted_date, l.first_seen.date())

    def test_resolve_unverified_when_nothing(self):
        l = mk()
        fv.resolve_date(l)
        self.assertEqual(l.date_confidence, CONF_UNVERIFIED)


class TestFreshnessEligibility(unittest.TestCase):
    CFG = {"freshness": {"recency_days": 21, "deadline_lookahead_days": 14, "include_unverified": True}}

    def test_recent_post_eligible(self):
        l = mk(posted_date=TODAY - timedelta(days=5), date_confidence=CONF_VERIFIED)
        self.assertTrue(fv.mark_eligibility(l, self.CFG))

    def test_old_post_not_eligible(self):
        l = mk(posted_date=TODAY - timedelta(days=90), date_confidence=CONF_VERIFIED)
        self.assertFalse(fv.mark_eligibility(l, self.CFG))

    def test_upcoming_deadline_eligible(self):
        l = mk(posted_date=TODAY - timedelta(days=90), date_confidence=CONF_VERIFIED,
               deadline=TODAY + timedelta(days=7))
        self.assertTrue(fv.mark_eligibility(l, self.CFG))

    def test_unverified_included_when_configured(self):
        l = mk(date_confidence=CONF_UNVERIFIED)
        self.assertTrue(fv.mark_eligibility(l, self.CFG))

    def test_unverified_excluded_when_disabled(self):
        cfg = {"freshness": {"recency_days": 21, "deadline_lookahead_days": 14, "include_unverified": False}}
        l = mk(date_confidence=CONF_UNVERIFIED)
        self.assertFalse(fv.mark_eligibility(l, cfg))


class TestLiveCheckClassifier(unittest.TestCase):
    def test_404_dead(self):
        s, _ = fv.classify_live(404, "u", "u", "", False)
        self.assertEqual(s, LIVE_DEAD)

    def test_closure_phrase_dead(self):
        s, why = fv.classify_live(200, "u", "u", "<p>This position is no longer available.</p>", False)
        self.assertEqual(s, LIVE_DEAD)
        self.assertIn("closure", why)

    def test_validthrough_expired_dead(self):
        body = ('<script type="application/ld+json">{"@type":"JobPosting","title":"X Intern",'
                '"validThrough":"2000-01-01","description":"intern"}</script>')
        s, _ = fv.classify_live(200, "u", "u", body, False)
        self.assertEqual(s, LIVE_DEAD)

    def test_generic_redirect_dead(self):
        s, _ = fv.classify_live(
            200, "https://co.com/careers", "https://co.com/careers/jobs/123", "ok", True)
        self.assertEqual(s, LIVE_DEAD)

    def test_200_open_live(self):
        s, _ = fv.classify_live(200, "https://co/jobs/1", "https://co/jobs/1",
                                "Apply now for this internship", False)
        self.assertEqual(s, LIVE_OK)

    def test_403_unknown_not_dropped(self):
        s, _ = fv.classify_live(403, "u", "u", "", False)
        self.assertEqual(s, LIVE_UNKNOWN)


class TestDedupe(unittest.TestCase):
    def test_merge_prefers_verified_and_records_sources(self):
        a = mk(source="serpapi:x", date_confidence=CONF_UNVERIFIED)
        b = mk(source="greenhouse:sifive", posted_date=TODAY, date_confidence=CONF_VERIFIED,
               description_text="RTL Verilog FPGA role")
        merged = fv.dedupe([a, b])
        self.assertEqual(len(merged), 1)
        m = merged[0]
        self.assertEqual(m.date_confidence, CONF_VERIFIED)  # verified wins as primary
        self.assertIn("also_seen_sources", m.raw)


class TestMatcher(unittest.TestCase):
    def setUp(self):
        self.profile = parse_resume(str(SAMPLE_RESUME), {"domain": {"priority_keywords": ["FPGA", "RTL", "Verilog"]}})

    def test_relevant_role_scores_high(self):
        l = mk(title="FPGA RTL Design Intern",
               description_text="Design RTL in Verilog/SystemVerilog for an ASIC. FPGA prototyping.")
        s, matched, _ = matcher.keyword_score(self.profile, l)
        self.assertGreater(s, 55)
        self.assertIn("verilog", [m.lower() for m in matched])

    def test_synonym_only_match_counts(self):
        # JD says "RTL"/"HDL" but resume lists "Verilog" -> should still match via synonyms
        l = mk(title="Digital Design Intern", description_text="HDL and RTL for digital design.")
        s, matched, _ = matcher.keyword_score(self.profile, l)
        self.assertGreater(s, 0)

    def test_irrelevant_role_scores_low(self):
        l = mk(title="Marketing Intern", description_text="Social media, copywriting, SEO, branding.")
        s, _, _ = matcher.keyword_score(self.profile, l)
        self.assertLess(s, 25)


class TestCache(unittest.TestCase):
    def test_first_seen_stable_and_diff(self):
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "c.db"
            c = Cache(db)
            l = mk()
            first = c.observe(l)
            again = c.observe(l)  # same key -> first_seen unchanged
            self.assertEqual(first, again)

            run1 = c.start_run()
            c.record_run_listing(run1, l, reported=True)
            c.finish_run(run1, 1, 1)

            run2 = c.start_run()
            l2 = mk(company="NewCo", title="ASIC Intern")
            new_keys, closed_keys = c.diff_since_last_run(run2, [l2])
            self.assertIn(l2.cache_key(), new_keys)       # NewCo is new
            self.assertIn(l.cache_key(), closed_keys)     # SiFive no longer listed
            c.close()


class TestReport(unittest.TestCase):
    def test_generates_both_sections(self):
        verified = mk(posted_date=TODAY, date_confidence=CONF_VERIFIED, live_status=LIVE_OK,
                      live_checked_at=datetime.now(timezone.utc), match_score=80,
                      apply_url="https://x/jobs/1", requirements=["FPGA", "Verilog"],
                      eligibility_reason="posted 0d ago")
        unverified = mk(company="Mystery", date_confidence=CONF_UNVERIFIED, live_status=LIVE_OK,
                        match_score=40, eligibility_reason="date unverified")
        with tempfile.TemporaryDirectory() as td:
            cfg = {"output": {"format": "both", "directory": td},
                   "freshness": {"recency_days": 21, "deadline_lookahead_days": 14}}
            paths = report_generator.generate([verified, unverified], None, cfg)
            self.assertIn("markdown", paths)
            self.assertIn("html", paths)
            md = paths["markdown"].read_text(encoding="utf-8")
            self.assertIn("Verified-fresh", md)
            self.assertIn("Unverified-date", md)
            self.assertIn("SiFive", md)
            self.assertIn("Mystery", md)


class TestEndToEndOffline(unittest.TestCase):
    """Run the post-fetch pipeline end to end without any network."""

    def test_pipeline(self):
        profile = parse_resume(str(SAMPLE_RESUME), {"domain": {"priority_keywords": ["FPGA", "Verilog"]}})
        listings = [
            mk(company="ChipCo", title="FPGA Design Intern", source="greenhouse:chipco",
               posted_date=TODAY - timedelta(days=3), date_confidence=CONF_VERIFIED,
               description_text="RTL, Verilog, FPGA, timing closure.", apply_url="https://chipco/jobs/1"),
            mk(company="OldCo", title="ASIC Intern", source="lever:oldco",
               posted_date=TODAY - timedelta(days=200), date_confidence=CONF_VERIFIED,
               description_text="ASIC physical design.", apply_url="https://oldco/jobs/1"),
            mk(company="Mystery", title="Embedded Intern", source="serpapi:x",
               date_confidence=CONF_UNVERIFIED, description_text="firmware, embedded c"),
        ]
        for l in listings:
            fv.resolve_date(l)
        listings = fv.dedupe(listings)
        eligible = [l for l in listings if fv.mark_eligibility(
            l, {"freshness": {"recency_days": 21, "deadline_lookahead_days": 14, "include_unverified": True}})]
        # OldCo (200d) drops out; ChipCo + Mystery remain
        self.assertEqual({l.company for l in eligible}, {"ChipCo", "Mystery"})

        matcher.score_listings(profile, eligible, {"matching": {"use_llm": "never"}})
        chip = next(l for l in eligible if l.company == "ChipCo")
        self.assertGreater(chip.match_score, 40)

        with tempfile.TemporaryDirectory() as td:
            cfg = {"output": {"format": "markdown", "directory": td},
                   "freshness": {"recency_days": 21, "deadline_lookahead_days": 14}}
            paths = report_generator.generate(eligible, profile, cfg, diff=([], []))
            self.assertTrue(paths["markdown"].exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
