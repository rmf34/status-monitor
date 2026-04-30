"""
Unit tests for the pure status-polling functions in pollers.py.
Network is patched out; everything runs against fixture JSON.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch
from urllib.error import URLError

import pytest

import pollers

FIXTURES = Path(__file__).parent / "fixtures"


def _load(name: str):
    with open(FIXTURES / name) as f:
        return json.load(f)


def _gcp_proj(services):
    return {"name": "gcp:p", "type": "gcp",
            "config": {"project_id": "p", "services": services}}


# ---------------------------------------------------------------- worst() ---

class TestWorst:
    def test_empty_returns_ok(self):
        assert pollers.worst([]) == "ok"

    def test_single_minor(self):
        assert pollers.worst([{"level": "minor"}]) == "minor"

    def test_picks_highest(self):
        assert pollers.worst([
            {"level": "minor"},
            {"level": "critical"},
            {"level": "info"},
        ]) == "critical"

    def test_unknown_level_treated_as_ok(self):
        # Defensive: a typo should not crash; treat as floor.
        assert pollers.worst([{"level": "bogus"}]) == "ok"


# ------------------------------------------------------- gcp severity map ---

@pytest.mark.parametrize("sev,level", [
    ("low", "minor"),
    ("medium", "major"),
    ("high", "critical"),
    ("", "minor"),
    ("unknown", "minor"),
])
def test_gcp_severity_to_level(sev, level):
    assert pollers._gcp_severity_to_level(sev) == level


# ---------------------------------------------------------------- poll_gcp -

class TestPollGcp:
    def _patch_feed(self, fixture_name):
        return patch.object(pollers, "fetch_json",
                            return_value=_load(fixture_name))

    def test_resolved_incident_skipped(self):
        with self._patch_feed("gcp_one_resolved.json"):
            issues = pollers.poll_gcp([_gcp_proj([{"name": "Cloud Run"}])])
        assert issues == []

    def test_active_incident_emits(self):
        with self._patch_feed("gcp_one_active_us_central1.json"):
            issues = pollers.poll_gcp([_gcp_proj([{"name": "Cloud Run"}])])
        assert len(issues) == 1
        i = issues[0]
        assert i["level"] == "major"            # severity=medium
        assert "Cloud Run" in i["summary"]
        assert i["kind"] == "incident"
        assert i["url"].startswith(pollers.GCP_PAGE)

    def test_unwatched_product_ignored(self):
        with self._patch_feed("gcp_one_active_us_central1.json"):
            issues = pollers.poll_gcp([_gcp_proj([{"name": "Google BigQuery"}])])
        assert issues == []

    def test_region_filter_excludes_other_region(self):
        with self._patch_feed("gcp_one_active_us_central1.json"):
            issues = pollers.poll_gcp([_gcp_proj(
                [{"name": "Cloud Run", "regions": ["europe-west1"]}]
            )])
        assert issues == []

    def test_region_filter_includes_matching_region(self):
        with self._patch_feed("gcp_one_active_us_central1.json"):
            issues = pollers.poll_gcp([_gcp_proj(
                [{"name": "Cloud Run", "regions": ["us-central1"]}]
            )])
        assert len(issues) == 1

    def test_global_incident_matches_any_region(self):
        with self._patch_feed("gcp_global_incident.json"):
            issues = pollers.poll_gcp([_gcp_proj(
                [{"name": "Cloud Run", "regions": ["us-central1"]}]
            )])
        assert len(issues) == 1
        assert issues[0]["level"] == "critical"  # severity=high

    def test_empty_regions_in_config_matches_any(self):
        # services entry with no `regions` key should match incidents in any region.
        with self._patch_feed("gcp_one_active_us_central1.json"):
            issues = pollers.poll_gcp([_gcp_proj([{"name": "Cloud Run"}])])
        assert len(issues) == 1

    def test_empty_regions_list_matches_any(self):
        with self._patch_feed("gcp_one_active_us_central1.json"):
            issues = pollers.poll_gcp([_gcp_proj(
                [{"name": "Cloud Run", "regions": []}]
            )])
        assert len(issues) == 1

    def test_feed_unreachable_emits_info(self):
        with patch.object(pollers, "fetch_json", side_effect=OSError("nope")):
            issues = pollers.poll_gcp([_gcp_proj([{"name": "Cloud Run"}])])
        assert len(issues) == 1
        assert issues[0]["level"] == "info"
        assert issues[0]["kind"] == "feed-error"

    def test_multiple_projects_separate_grouping(self):
        with self._patch_feed("gcp_one_active_us_central1.json"):
            issues = pollers.poll_gcp([
                {"name": "gcp:a", "type": "gcp",
                 "config": {"project_id": "a", "services": [{"name": "Cloud Run"}]}},
                {"name": "gcp:b", "type": "gcp",
                 "config": {"project_id": "b", "services": [{"name": "Cloud Run"}]}},
            ])
        assert len(issues) == 2
        labels = {i["project"] for i in issues}
        assert labels == {"GCP / a", "GCP / b"}


# --------------------------------------------------- statuspage severity ---

@pytest.mark.parametrize("status,level", [
    ("operational",          "ok"),
    ("degraded_performance", "minor"),
    ("partial_outage",       "major"),
    ("major_outage",         "critical"),
    ("under_maintenance",    "info"),
    ("totally_made_up",      "minor"),
])
def test_statuspage_status_to_level(status, level):
    assert pollers._statuspage_status_to_level(status) == level


@pytest.mark.parametrize("impact,level", [
    ("none",        "info"),
    ("maintenance", "info"),
    ("minor",       "minor"),
    ("major",       "major"),
    ("critical",    "critical"),
    ("",            "minor"),
])
def test_statuspage_impact_to_level(impact, level):
    assert pollers._statuspage_impact_to_level(impact) == level


# ------------------------------------------------------- poll_statuspage ---

class TestPollStatuspage:
    def _patch_feed(self, fixture_name):
        return patch.object(pollers, "fetch_json",
                            return_value=_load(fixture_name))

    def test_all_operational_no_issues(self):
        with self._patch_feed("statuspage_all_operational.json"):
            issues = pollers.poll_statuspage(
                "url", "Test", ["Component A", "Component B"], "page")
        assert issues == []

    def test_degraded_component_emits_minor(self):
        with self._patch_feed("statuspage_one_degraded.json"):
            issues = pollers.poll_statuspage(
                "url", "Test", ["Component A"], "page")
        assert len(issues) == 1
        assert issues[0]["level"] == "minor"
        assert issues[0]["kind"] == "component"
        assert "Component A" in issues[0]["summary"]

    def test_major_outage_emits_critical(self):
        with self._patch_feed("statuspage_one_major.json"):
            issues = pollers.poll_statuspage(
                "url", "Test", ["Component A"], "page")
        assert any(i["level"] == "critical" for i in issues)

    def test_unwatched_component_ignored(self):
        # Feed shows Component A degraded, but watcher cares about Component B.
        with self._patch_feed("statuspage_one_degraded.json"):
            issues = pollers.poll_statuspage(
                "url", "Test", ["Component B"], "page")
        assert issues == []

    def test_resolved_incident_skipped(self):
        with self._patch_feed("statuspage_resolved_incident.json"):
            issues = pollers.poll_statuspage(
                "url", "Test", ["Component A"], "page")
        assert issues == []

    def test_open_incident_emits(self):
        with self._patch_feed("statuspage_open_incident.json"):
            issues = pollers.poll_statuspage(
                "url", "Test", ["Component A"], "page")
        kinds = {i["kind"] for i in issues}
        assert "incident" in kinds
        inc_issues = [i for i in issues if i["kind"] == "incident"]
        assert "investigating thing" in inc_issues[0]["summary"].lower()

    def test_feed_unreachable_emits_info(self):
        with patch.object(pollers, "fetch_json", side_effect=OSError("nope")):
            issues = pollers.poll_statuspage(
                "url", "Test", ["Component A"], "page")
        assert len(issues) == 1
        assert issues[0]["level"] == "info"
        assert issues[0]["kind"] == "feed-error"


# ----------------------------------------------------------- poll_all ----

class TestPollAll:
    """End-to-end-ish: real config shapes feeding through the dispatcher."""

    def test_dispatches_per_type(self):
        cfg = {
            "projects": [
                _gcp_proj([{"name": "Cloud Run"}]),
                {"name": "github:o/r", "type": "github",
                 "config": {"repo": "o/r",
                            "components": ["Component A"]}},
                {"name": "anthropic", "type": "anthropic",
                 "config": {"components": ["Component A"]}},
            ]
        }

        def fake(url, timeout=12):
            if "cloud.google.com" in url:
                return _load("gcp_one_active_us_central1.json")
            return _load("statuspage_one_degraded.json")

        with patch.object(pollers, "fetch_json", side_effect=fake):
            issues = pollers.poll_all(cfg)
        # 1 from gcp, 1 from github, 1 from anthropic
        labels = {i["project"] for i in issues}
        assert "GCP / p" in labels
        assert any(l.startswith("GitHub /") for l in labels)
        assert "Anthropic" in labels

    def test_empty_config_no_issues(self):
        # No projects -> no issues; should never even call fetch_json.
        with patch.object(pollers, "fetch_json",
                          side_effect=AssertionError("should not be called")):
            assert pollers.poll_all({"projects": []}) == []

    def test_anthropic_label_is_constant(self):
        # The label that poll_all emits for Anthropic issues is the
        # ANTHROPIC_LABEL constant, which daemon_helpers.match_anthropic
        # matches against. If this drifts, the indicator silently shows
        # zero issues.
        cfg = {"projects": [
            {"name": "anthropic", "type": "anthropic",
             "config": {"components": ["Component A"]}}
        ]}
        with patch.object(pollers, "fetch_json",
                          return_value=_load("statuspage_one_degraded.json")):
            issues = pollers.poll_all(cfg)
        assert issues, "expected at least one issue from the degraded fixture"
        assert all(i["project"] == pollers.ANTHROPIC_LABEL for i in issues)


# ---------------------------------------------------------- fetch_json ----

class TestFetchJson:
    def _fake_response(self, body: bytes):
        """Returns a context manager that mimics urlopen()'s return value."""
        class _Resp:
            def __enter__(self_inner): return self_inner
            def __exit__(self_inner, *a): return False
            def read(self_inner): return body
        return _Resp()

    def test_html_body_raises_url_error(self):
        # Status pages occasionally serve a Cloudflare error page during
        # their own outage. Decode failure must be raised as URLError so
        # the existing except blocks catch it.
        with patch.object(pollers, "urlopen",
                          return_value=self._fake_response(b"<html>oops</html>")):
            with pytest.raises(URLError):
                pollers.fetch_json("https://example.invalid/feed")

    def test_invalid_utf8_raises_url_error(self):
        with patch.object(pollers, "urlopen",
                          return_value=self._fake_response(b"\xff\xfe not utf 8")):
            with pytest.raises(URLError):
                pollers.fetch_json("https://example.invalid/feed")

    def test_valid_json_returned_through(self):
        with patch.object(pollers, "urlopen",
                          return_value=self._fake_response(b'{"a": 1}')):
            assert pollers.fetch_json("https://example.invalid/feed") == {"a": 1}


# ----------------------------------------- shape guards on poll_*  --------

class TestFeedShapeGuards:
    def test_poll_statuspage_handles_non_dict(self):
        with patch.object(pollers, "fetch_json", return_value=["not", "a", "dict"]):
            issues = pollers.poll_statuspage("u", "Test", ["X"], "page")
        assert len(issues) == 1
        assert issues[0]["kind"] == "feed-error"
        assert "unexpected feed shape" in issues[0]["summary"]

    def test_poll_statuspage_handles_null(self):
        with patch.object(pollers, "fetch_json", return_value=None):
            issues = pollers.poll_statuspage("u", "Test", ["X"], "page")
        assert len(issues) == 1
        assert issues[0]["kind"] == "feed-error"

    def test_poll_gcp_handles_non_list(self):
        with patch.object(pollers, "fetch_json", return_value={"oops": True}):
            issues = pollers.poll_gcp([{"name": "gcp:p", "type": "gcp",
                                        "config": {"project_id": "p",
                                                   "services": [{"name": "Cloud Run"}]}}])
        assert len(issues) == 1
        assert issues[0]["kind"] == "feed-error"
        assert "unexpected feed shape" in issues[0]["summary"]

    def test_poll_statuspage_decode_failure_path(self):
        # Simulate fetch_json raising URLError because of a JSON decode
        # failure. Caller must surface a feed-error issue, not crash.
        with patch.object(pollers, "fetch_json",
                          side_effect=URLError("invalid JSON")):
            issues = pollers.poll_statuspage("u", "Test", ["X"], "page")
        assert len(issues) == 1
        assert issues[0]["kind"] == "feed-error"
