"""
Pure-function status polling logic.

No GTK / no AppIndicator dependency lives here so this module can be
imported and exercised directly from unit tests without a display.

Public surface used by daemon.py:
    LEVELS, LEVEL_NAMES
    GCP_PAGE, GITHUB_PAGE, ANTHROPIC_PAGE, ANTHROPIC_LABEL
    fetch_json
    load_config
    poll_gcp, poll_statuspage, poll_all
    worst
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from urllib.error import URLError
from urllib.request import Request, urlopen

import yaml

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.yaml"

GCP_INCIDENTS_URL = "https://status.cloud.google.com/incidents.json"
GCP_PAGE = "https://status.cloud.google.com"
GITHUB_SUMMARY_URL = "https://www.githubstatus.com/api/v2/summary.json"
GITHUB_PAGE = "https://www.githubstatus.com"
ANTHROPIC_SUMMARY_URL = "https://status.anthropic.com/api/v2/summary.json"
ANTHROPIC_PAGE = "https://status.anthropic.com"

# Project-name label used for Anthropic issues. daemon.py matches against
# this exact string, so changes here must be deliberate.
ANTHROPIC_LABEL = "Anthropic"

LEVELS = {"ok": 0, "info": 1, "minor": 2, "major": 3, "critical": 4}
LEVEL_NAMES = {v: k for k, v in LEVELS.items()}


def fetch_json(url: str, timeout: int = 12):
    """GETs `url` and parses the body as JSON. Decode failures are
    re-raised as URLError so callers can use a single exception class for
    both transport and content problems (a status page returning HTML
    during its own outage is a real case)."""
    req = Request(url, headers={"User-Agent": "status-monitor/0.1"})
    with urlopen(req, timeout=timeout) as r:
        body = r.read()
    try:
        return json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as e:
        raise URLError(f"invalid JSON from {url}: {e}") from e


def load_config() -> dict:
    if not CONFIG_PATH.exists():
        print(f"config not found: {CONFIG_PATH}\nrun: python3 setup_cli.py setup",
              file=sys.stderr)
        sys.exit(1)
    with open(CONFIG_PATH) as f:
        return yaml.safe_load(f) or {}


def save_config(cfg: dict) -> None:
    with open(CONFIG_PATH, "w") as f:
        yaml.safe_dump(cfg, f, sort_keys=False)


def _gcp_severity_to_level(sev: str) -> str:
    return {"low": "minor", "medium": "major", "high": "critical"}.get(sev, "minor")


def poll_gcp(gcp_entries: list[dict]) -> list[dict]:
    issues: list[dict] = []
    try:
        feed = fetch_json(GCP_INCIDENTS_URL, timeout=15)
    except (URLError, TimeoutError, OSError) as e:
        return [{"level": "info", "project": "GCP",
                 "summary": f"feed unreachable: {e}",
                 "url": GCP_PAGE, "kind": "feed-error"}]
    if not isinstance(feed, list):
        return [{"level": "info", "project": "GCP",
                 "summary": f"unexpected feed shape: {type(feed).__name__}",
                 "url": GCP_PAGE, "kind": "feed-error"}]

    for inc in feed:
        if inc.get("end"):
            continue  # resolved
        affected = {p.get("title") for p in inc.get("affected_products", [])}
        if not affected:
            continue
        affected_locs = {loc.get("id") for loc in inc.get("currently_affected_locations", [])}
        sev = inc.get("severity", "low")
        level = _gcp_severity_to_level(sev)
        desc = inc.get("external_desc", "incident")
        url = GCP_PAGE + (inc.get("uri") or "")

        for proj in gcp_entries:
            cfg = proj.get("config", {})
            pid = cfg.get("project_id", "?")
            for svc in cfg.get("services", []):
                if svc.get("name") not in affected:
                    continue
                regs = svc.get("regions") or []
                if regs and affected_locs:
                    if not (set(regs) & affected_locs or "global" in affected_locs):
                        continue
                issues.append({
                    "level": level,
                    "project": f"GCP / {pid}",
                    "summary": f"{svc['name']}: {desc}",
                    "url": url,
                    "kind": "incident",
                })
    return issues


def _statuspage_status_to_level(status: str) -> str:
    return {
        "operational":          "ok",
        "degraded_performance": "minor",
        "partial_outage":       "major",
        "major_outage":         "critical",
        "under_maintenance":    "info",
    }.get(status, "minor")


def _statuspage_impact_to_level(impact: str) -> str:
    return {
        "none":        "info",
        "maintenance": "info",
        "minor":       "minor",
        "major":       "major",
        "critical":    "critical",
    }.get(impact, "minor")


def poll_statuspage(url: str, label: str, watched: list[str], page_url: str) -> list[dict]:
    try:
        data = fetch_json(url, timeout=15)
    except (URLError, TimeoutError, OSError) as e:
        return [{"level": "info", "project": label,
                 "summary": f"feed unreachable: {e}",
                 "url": page_url, "kind": "feed-error"}]
    if not isinstance(data, dict):
        return [{"level": "info", "project": label,
                 "summary": f"unexpected feed shape: {type(data).__name__}",
                 "url": page_url, "kind": "feed-error"}]
    issues: list[dict] = []
    watched_set = set(watched)
    components_by_name = {c.get("name"): c for c in data.get("components", [])}

    for name in watched:
        c = components_by_name.get(name)
        if not c:
            continue
        status = c.get("status", "operational")
        if status == "operational":
            continue
        issues.append({
            "level": _statuspage_status_to_level(status),
            "project": label,
            "summary": f"{name}: {status.replace('_', ' ')}",
            "url": page_url,
            "kind": "component",
        })

    for inc in data.get("incidents", []):
        if inc.get("status") == "resolved":
            continue
        affected = {c.get("name") for c in inc.get("components", [])}
        watched_hits = affected & watched_set
        if not watched_hits:
            continue
        # Use current component statuses rather than the incident's headline
        # impact. An open incident whose watched components are all operational
        # does not warrant an alert (e.g. a model-access suspension that leaves
        # the API endpoint itself serving normally).
        watched_component_levels = [
            _statuspage_status_to_level(components_by_name[name].get("status", "operational"))
            for name in watched_hits
            if name in components_by_name
        ]
        level = (
            LEVEL_NAMES[max(LEVELS.get(s, 0) for s in watched_component_levels)]
            if watched_component_levels
            else _statuspage_impact_to_level(inc.get("impact", "minor"))
        )
        if level == "ok":
            continue
        issues.append({
            "level": level,
            "project": label,
            "summary": f"{inc.get('name', 'incident')} ({', '.join(sorted(watched_hits))})",
            "url": inc.get("shortlink") or page_url,
            "kind": "incident",
        })
    return issues


def poll_all(cfg: dict) -> list[dict]:
    issues: list[dict] = []
    projects = cfg.get("projects", [])

    gcp_entries = [p for p in projects if p.get("type") == "gcp"]
    if gcp_entries:
        issues.extend(poll_gcp(gcp_entries))

    for p in projects:
        if p.get("type") == "github":
            comps = p.get("config", {}).get("components", [])
            label = f"GitHub / {p.get('config', {}).get('repo', '?')}"
            if comps:
                issues.extend(poll_statuspage(GITHUB_SUMMARY_URL, label, comps, GITHUB_PAGE))
        elif p.get("type") == "anthropic":
            comps = p.get("config", {}).get("components", [])
            if comps:
                issues.extend(poll_statuspage(
                    ANTHROPIC_SUMMARY_URL, ANTHROPIC_LABEL, comps, ANTHROPIC_PAGE))
    return issues


def worst(issues: list[dict]) -> str:
    if not issues:
        return "ok"
    return LEVEL_NAMES[max(LEVELS.get(i["level"], 0) for i in issues)]
