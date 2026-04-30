"""
Pure helpers shared by the daemon and unit tests.

Splitting these out of daemon.py means tests can exercise the issue
filtering and source-routing logic without importing GTK / AppIndicator.
"""

from __future__ import annotations

import socket

from pollers import (
    ANTHROPIC_LABEL, ANTHROPIC_PAGE, GCP_PAGE, GITHUB_PAGE, LEVELS, worst,
)


# Levels that are not real upstream-status values. They communicate the
# state of our knowledge: offline = the local network is down, unknown =
# we have not yet seen real data (either we have not polled, or the only
# response so far was a feed-error).
META_LEVELS = frozenset({"offline", "unknown"})


def filter_by_min_level(issues: list[dict], min_level: str) -> list[dict]:
    """Drops issues below `min_level` (per pollers.LEVELS ordering). Issues
    without a recognized level fall through unchanged."""
    floor = LEVELS.get(min_level, 0)
    return [i for i in issues if LEVELS.get(i.get("level", ""), 0) >= floor]


def match_gcp(i: dict) -> bool:
    p = i.get("project", "")
    return p == "GCP" or p.startswith("GCP / ")


def match_github(i: dict) -> bool:
    return i.get("project", "").startswith("GitHub")


def match_anthropic(i: dict) -> bool:
    return i.get("project", "") == ANTHROPIC_LABEL


# (key, short_label, long_label, status_page_url, project_type, matcher)
SOURCES = [
    ("gcp",       "GCP",  "GCP",       GCP_PAGE,       "gcp",       match_gcp),
    ("github",    "GH",   "GitHub",    GITHUB_PAGE,    "github",    match_github),
    ("anthropic", "Anth", "Anthropic", ANTHROPIC_PAGE, "anthropic", match_anthropic),
]

DEFAULT_INDICATOR_ORDER = ["gcp", "github", "anthropic"]


def configured_sources(cfg: dict) -> list[tuple]:
    """Returns SOURCES tuples filtered to configured project types and
    ordered according to settings.indicator_order. Unknown keys are
    skipped; configured-but-unlisted keys go to the end."""
    types = {p.get("type") for p in cfg.get("projects", [])}
    by_key = {s[0]: s for s in SOURCES}
    order = cfg.get("settings", {}).get("indicator_order", DEFAULT_INDICATOR_ORDER)
    seen: set[str] = set()
    out: list[tuple] = []
    for key in order:
        s = by_key.get(key)
        if s and s[4] in types:
            out.append(s)
            seen.add(key)
    for s in SOURCES:
        if s[0] not in seen and s[4] in types:
            out.append(s)
    return out


def compute_level(issues: list[dict], min_level: str, *, offline: bool,
                  has_polled: bool) -> str:
    """Returns the level string an indicator should show given the issues
    routed to it. Distinguishes the three "we cannot say" states from
    "ok":

      - offline: local network is down. Wins over everything else.
      - unknown: either we have not polled yet, or every issue we have
        is a feed-error (i.e. we asked the upstream and could not get
        an answer).
      - ok: at least one successful poll completed and there are no
        active issues at or above min_level.

    Real status levels (info / minor / major / critical) are returned as
    `worst()` of the min-level-filtered issues."""
    if offline:
        return "offline"
    if not has_polled:
        return "unknown"
    feed_errors = [i for i in issues if i.get("kind") == "feed-error"]
    filtered = filter_by_min_level(issues, min_level)
    real_filtered = [i for i in filtered if i.get("kind") != "feed-error"]
    if feed_errors and not real_filtered:
        # We received nothing usable and have no real issues to report;
        # the upstream could be on fire and we would not know.
        return "unknown"
    return worst(filtered)


def is_online(timeout: float = 2.0) -> bool:
    """Fast TCP probe to a stable public IP. Avoids hammering the actual
    status pages when the client itself has no connectivity. We try
    Cloudflare DNS first, then Google DNS, and report online if either
    handshake completes."""
    for host in ("1.1.1.1", "8.8.8.8"):
        try:
            with socket.create_connection((host, 53), timeout=timeout):
                return True
        except OSError:
            continue
    return False
