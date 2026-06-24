"""Unit tests — focused on the freshness validation (the critical path) plus
parsing, matching, dedup, and report rendering. All offline (no network)."""

from __future__ import annotations

import html
import json
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from internfinder import domain, freshness_validator as fv, matcher, report_generator
from internfinder.cache import Cache
from internfinder.models import (
    CONF_APPROXIMATE, CONF_UNVERIFIED, CONF_VERIFIED,
    LIVE_DEAD, LIVE_OK, LIVE_UNKNOWN, Listing,
)
from internfinder.resume_parser import parse_resume
from internfinder.sources import base, github_lists, job_search_api, schemaorg, yc_jobs

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

    def test_extract_requirements_includes_generic_non_engineering_terms(self):
        terms = base.extract_requirements(
            "Own social media campaigns, financial modeling, customer research, and policy analysis."
        )
        self.assertIn("social media", terms)
        self.assertIn("financial modeling", terms)


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


class TestSerpApiSource(unittest.TestCase):
    def test_blank_focus_builds_broad_internship_query(self):
        class FakeResponse:
            ok = True

            def json(self):
                return {"jobs_results": []}

        class FakeHttp:
            def __init__(self):
                self.params_list = []

            def get(self, _url, params=None, obey_robots=True):
                self.params_list.append(params)
                return FakeResponse()

        http = FakeHttp()
        ctx = base.SourceContext(
            http=http,
            config={
                "search": {"term": "", "target_role": "", "locations": ["United States"]},
                "domain": {"priority_keywords": []},
                "sources": {"serpapi_google_jobs": {"enabled": True, "max_results": 5}},
            },
        )
        with patch.dict("os.environ", {"SERPAPI_API_KEY": "test"}, clear=False):
            rows = job_search_api.fetch(ctx)

        self.assertEqual(rows, [])
        self.assertEqual(http.params_list[0]["q"], "internship")
        self.assertEqual([p["q"] for p in http.params_list], ["internship"])

    def test_startup_breadth_is_opt_in(self):
        class FakeResponse:
            ok = True

            def json(self):
                return {"jobs_results": []}

        class FakeHttp:
            def __init__(self):
                self.queries = []

            def get(self, _url, params=None, obey_robots=True):
                self.queries.append(params["q"])
                return FakeResponse()

        http = FakeHttp()
        ctx = base.SourceContext(
            http=http,
            config={
                "search": {"term": "", "target_role": "", "locations": ["United States"]},
                "domain": {"priority_keywords": []},
                "sources": {
                    "serpapi_google_jobs": {
                        "enabled": True,
                        "max_results": 5,
                        "startup_breadth": True,
                        "startup_query_terms": ["startup internship"],
                    }
                },
            },
        )
        with patch.dict("os.environ", {"SERPAPI_API_KEY": "test"}, clear=False):
            rows = job_search_api.fetch(ctx)

        self.assertEqual(rows, [])
        self.assertEqual(http.queries, ["internship", "startup internship"])

    def test_remote_preference_and_startup_terms_feed_queries(self):
        class FakeResponse:
            ok = True

            def json(self):
                return {"jobs_results": []}

        class FakeHttp:
            def __init__(self):
                self.queries = []

            def get(self, _url, params=None, obey_robots=True):
                self.queries.append(params["q"])
                return FakeResponse()

        http = FakeHttp()
        ctx = base.SourceContext(
            http=http,
            config={
                "search": {
                    "term": "Fall 2026",
                    "target_role": "finance",
                    "remote_preference": "remote",
                    "locations": ["United States"],
                },
                "domain": {"priority_keywords": []},
                "sources": {
                    "serpapi_google_jobs": {
                        "enabled": True,
                        "max_results": 12,
                        "startup_breadth": True,
                        "startup_query_terms": [
                            "startup internship",
                            "venture backed startup internship",
                        ],
                    }
                },
            },
        )
        with patch.dict("os.environ", {"SERPAPI_API_KEY": "test"}, clear=False):
            rows = job_search_api.fetch(ctx)

        self.assertEqual(rows, [])
        self.assertEqual(
            http.queries,
            [
                "finance internship Fall 2026 remote",
                "finance startup internship Fall 2026 remote",
                "finance venture backed startup internship Fall 2026 remote",
            ],
        )

    def test_startup_queries_mark_listings_as_startups(self):
        class FakeResponse:
            ok = True

            def json(self):
                return {
                    "jobs_results": [
                        {
                            "title": "Marketing Intern",
                            "company_name": "Seedly",
                            "location": "Remote",
                            "description": "Remote internship with a small startup.",
                            "via": "Google",
                            "apply_options": [{"link": "https://seedly.example/jobs/intern"}],
                        }
                    ]
                }

        class FakeHttp:
            def get(self, _url, params=None, obey_robots=True):
                return FakeResponse()

        ctx = base.SourceContext(
            http=FakeHttp(),
            config={
                "search": {"term": "", "target_role": "marketing", "locations": ["United States"]},
                "domain": {"priority_keywords": []},
                "sources": {
                    "serpapi_google_jobs": {
                        "enabled": True,
                        "max_results": 2,
                        "startup_breadth": True,
                        "startup_query_terms": ["startup internship"],
                    }
                },
            },
        )
        with patch.dict("os.environ", {"SERPAPI_API_KEY": "test"}, clear=False):
            rows = job_search_api.fetch(ctx)

        self.assertEqual(len(rows), 2)
        self.assertIsNone(rows[0].is_startup)
        self.assertTrue(rows[1].is_startup)
        self.assertEqual(rows[1].raw["serpapi_query"], "marketing startup internship")


class TestYCStartupSource(unittest.TestCase):
    def test_empty_selectors_fetches_broad_yc_dataset(self):
        class FakeResponse:
            ok = True

            def json(self):
                return [{"name": "AnyField Startup", "status": "Active", "isHiring": True}]

        class FakeHttp:
            def __init__(self):
                self.urls = []

            def get(self, url):
                self.urls.append(url)
                return FakeResponse()

        http = FakeHttp()
        ctx = base.SourceContext(http=http, config={})
        companies = yc_jobs._yc_companies(ctx, [])
        self.assertEqual(companies[0]["name"], "AnyField Startup")
        self.assertTrue(http.urls[0].endswith("/companies/all.json"))

    def test_prefers_active_small_recent_companies(self):
        companies = [
            {"name": "OldTiny", "status": "Active", "team_size": 5, "batch": "Winter 2018"},
            {"name": "BigRecent", "status": "Active", "isHiring": True, "team_size": 200, "batch": "Winter 2024"},
            {"name": "InactiveTiny", "status": "Inactive", "isHiring": True, "team_size": 4, "batch": "Summer 2024"},
            {"name": "NotHiring", "status": "Active", "isHiring": False, "team_size": 6, "batch": "Summer 2024"},
            {"name": "TinyRecent", "status": "Active", "isHiring": True, "team_size": 8, "batch": "Winter 2024"},
            {"name": "TinyRecentB", "status": "Active", "isHiring": True, "team_size": 22, "batch": "Summer 2023"},
            {"name": "UnknownTeam", "status": "Active", "isHiring": True, "batch": "Summer 2024"},
        ]
        ranked = yc_jobs._filter_and_rank_companies(
            companies,
            {"active_only": True, "hiring_only": True, "small_team_max": 50, "recent_batch_year_min": 2020},
        )
        self.assertEqual([c["name"] for c in ranked], ["TinyRecent", "TinyRecentB", "UnknownTeam"])

    def test_slug_candidates_use_yc_slug_and_website_domain(self):
        candidates = yc_jobs._slug_candidates({
            "name": "Acme Labs Inc.",
            "slug": "acme-labs",
            "website": "https://jobs.acme.ai/careers",
        })
        self.assertEqual(candidates[0], "acme-labs")
        self.assertIn("acmelabs", candidates)
        self.assertIn("acme", candidates)

    def test_public_profile_jobs_create_startup_listing(self):
        payload = {
            "props": {
                "company": {
                    "id": 123,
                    "slug": "acme-labs",
                    "name": "Acme Labs",
                    "batch": "W26",
                    "team_size": 9,
                    "one_liner": "Developer tools for hardware teams.",
                    "tags": ["Developer Tools"],
                },
                "jobPostings": [{
                    "id": 987,
                    "title": "Product Engineering Intern",
                    "url": "/companies/acme-labs/jobs/abc-product-engineering-intern",
                    "location": "Remote",
                    "type": "Internship",
                    "prettyRole": "Engineering",
                    "minExperience": "Any (new grads ok)",
                    "skills": ["Python", "TypeScript"],
                    "createdAt": "5 days",
                }],
            }
        }

        class FakeResponse:
            ok = True
            text = f'<div data-page="{html.escape(json.dumps(payload))}"></div>'

        class FakeHttp:
            def get(self, _url):
                return FakeResponse()

        ctx = base.SourceContext(http=FakeHttp(), config={})
        rows = yc_jobs._fetch_profile_jobs(ctx, {"slug": "acme-labs"})
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row.company, "Acme Labs")
        self.assertTrue(row.is_startup)
        self.assertEqual(row.source, "yc:profile:acme-labs")
        self.assertEqual(row.requirements, ["Python", "TypeScript"])
        self.assertEqual(row.date_confidence, CONF_APPROXIMATE)
        self.assertIn("9-person team", row.funding_stage)


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

    def test_auto_prefers_openrouter_when_key_is_set(self):
        cfg = {"matching": {"use_llm": "auto", "llm_provider": "auto", "openrouter_model": "z-ai/glm-5.2"}}
        with patch.dict("os.environ", {"OPENROUTER_API_KEY": "or-test"}, clear=False):
            llm = matcher._maybe_client(cfg)
        self.assertEqual(llm["provider"], "openrouter")
        self.assertEqual(llm["model"], "z-ai/glm-5.2")

    def test_openrouter_scoring_updates_listing(self):
        class FakeResponse:
            status_code = 200

            def raise_for_status(self):
                pass

            def json(self):
                return {
                    "choices": [{
                        "message": {
                            "content": json.dumps({
                                "score": 82,
                                "rationale": "Strong overlap for the candidate.",
                                "matched_skills": ["research", "python"],
                                "missing_skills": ["sql"],
                            })
                        }
                    }]
                }

        calls = []

        def fake_post(_url, **kwargs):
            calls.append(kwargs["json"])
            return FakeResponse()

        listing = mk(title="Research Intern", description_text="Research and Python analysis.")
        cfg = {
            "matching": {
                "use_llm": "always",
                "llm_provider": "openrouter",
                "openrouter_model": "z-ai/glm-5.2",
                "llm_max_listings": 1,
            }
        }
        with patch.dict("os.environ", {"OPENROUTER_API_KEY": "or-test"}, clear=False):
            with patch("requests.post", side_effect=fake_post):
                matcher.score_listings(self.profile, [listing], cfg)

        self.assertEqual(listing.match_score, 82)
        self.assertEqual(listing.matched_keywords, ["research", "python"])
        self.assertEqual(calls[0]["model"], "z-ai/glm-5.2")


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
