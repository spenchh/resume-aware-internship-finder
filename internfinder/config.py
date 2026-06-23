"""Configuration loading: YAML defaults + .env + CLI overrides."""

from __future__ import annotations

import copy
import os
from pathlib import Path
from typing import Any

import yaml


def load_env(path: str | os.PathLike = ".env") -> None:
    """Minimal .env loader (no extra dependency). Does not overwrite existing env."""
    p = Path(path)
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key, val = key.strip(), val.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = val


def load_config(path: str | os.PathLike = "config.yaml") -> dict[str, Any]:
    """Load the YAML config, merged over built-in defaults."""
    cfg = copy.deepcopy(_DEFAULTS)
    p = Path(path)
    if p.exists():
        loaded = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        _deep_merge(cfg, loaded)
    return cfg


def deep_get(cfg: dict, dotted: str, default: Any = None) -> Any:
    """Fetch ``cfg['a']['b']['c']`` via ``deep_get(cfg, 'a.b.c')``."""
    node: Any = cfg
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return default
        node = node[part]
    return node


def apply_overrides(cfg: dict, overrides: dict[str, Any]) -> dict:
    """Apply dotted-key CLI overrides (None values are ignored)."""
    for dotted, value in overrides.items():
        if value is None:
            continue
        parts = dotted.split(".")
        node = cfg
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = value
    return cfg


def _deep_merge(base: dict, overlay: dict) -> dict:
    for k, v in overlay.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _deep_merge(base[k], v)
        else:
            base[k] = v
    return base


# Built-in defaults mirror config.yaml so the tool runs even if the YAML is
# missing or partial. config.yaml (when present) wins.
_DEFAULTS: dict[str, Any] = {
    "search": {
        "term": "",
        "target_role": "",  # candidate's stated field/roles; drives web-wide search + scoring
        "role_keywords": ["intern", "internship", "co-op"],
        "locations": ["United States", "Remote"],
        "remote_preference": "any",
    },
    "freshness": {
        "recency_days": 21,
        "deadline_lookahead_days": 14,
        "live_check": True,
        "live_check_timeout": 12,
        "include_unverified": True,
    },
    "http": {
        "user_agent": "internfinder/1.0 (+personal-job-search)",
        "request_timeout": 20,
        "rate_limit_per_host_sec": 1.0,
        "max_retries": 3,
        "backoff_base_sec": 1.5,
        "respect_robots": True,
    },
    "matching": {
        "use_llm": "auto",
        "llm_provider": "auto",
        "openrouter_model": "z-ai/glm-5.2",
        "llm_model": "claude-haiku-4-5",
        "llm_max_listings": 60,
        "min_score_to_report": 0,
    },
    "sources": {
        "greenhouse": {"enabled": True, "companies": []},
        "lever": {"enabled": True, "companies": []},
        "ashby": {"enabled": True, "companies": []},
        "schemaorg_urls": {"enabled": True, "urls": []},
        "github_lists": {"enabled": True, "repos": [], "max_commit_age_days": 14},
        "yc_jobs": {
            "enabled": True,
            "selectors": [],
            "industries": [],
            "active_only": True,
            "hiring_only": True,
            "profile_first": True,
            "skip_ats_when_profile_has_jobs": True,
            "small_team_max": 50,
            "recent_batch_year_min": 2020,
            "max_companies": 40,
        },
        "wellfound": {"enabled": False},
        "serpapi_google_jobs": {"enabled": True, "max_results": 100},
    },
    "domain": {"priority_keywords": [], "priority_sectors": []},
    "output": {"format": "markdown", "directory": "reports"},
}
