"""Wellfound (AngelList Talent) — Tier 2 startup board.

Wellfound has no open public jobs API and its ToS disallows scraping; it also
walls listings behind login and aggressive bot defenses. Per spec Section 12 we
do NOT scrape it. This source is therefore disabled by default and supports only
a *user-provided export* (a JSON array you save from your own logged-in session),
which keeps us on the right side of ToS while still letting Wellfound data flow
through the same pipeline.

Export item shape (flexible — unknown keys are ignored)::

    [{"company": "...", "title": "...", "location": "...", "url": "...",
      "posted_date": "2026-06-10", "description": "...", "funding_stage": "Seed"}]
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from ..models import CONF_APPROXIMATE, Listing
from . import base

log = logging.getLogger("internfinder.sources.wellfound")


def fetch(ctx: base.SourceContext) -> list[Listing]:
    cfg = ctx.config.get("sources", {}).get("wellfound", {})
    if not cfg.get("enabled", False):
        return []
    export = cfg.get("export_file")
    if not export:
        log.info(
            "wellfound enabled but no export_file set. Wellfound has no public API "
            "and scraping violates its ToS, so this source only ingests a user "
            "export. See the module docstring for the expected format."
        )
        return []
    path = Path(export)
    if not path.exists():
        log.warning("wellfound export_file not found: %s", path)
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        log.warning("wellfound export parse failed: %s", exc)
        return []

    out: list[Listing] = []
    for item in data if isinstance(data, list) else []:
        title = (item.get("title") or "").strip()
        if not title or not base.looks_like_internship(title, item.get("description", "")):
            continue
        desc = item.get("description", "") or ""
        out.append(
            Listing(
                company=item.get("company", "(unknown)"),
                title=title,
                location=item.get("location", ""),
                apply_url=item.get("url", ""),
                source="wellfound:export",
                company_description=item.get("tagline", ""),
                is_startup=True,
                funding_stage=item.get("funding_stage", ""),
                description_text=desc,
                requirements=base.extract_requirements(desc),
                level=base.infer_level(title),
                work_mode=base.detect_work_mode(item.get("location", ""), desc),
                posted_date=base.parse_date_loose(item.get("posted_date")),
                date_confidence=CONF_APPROXIMATE,
                date_source="wellfound export",
            )
        )
    log.info("wellfound: %d listings from export", len(out))
    return out
