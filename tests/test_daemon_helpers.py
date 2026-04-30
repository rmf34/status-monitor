"""
Tests for daemon_helpers (the GTK-free pieces of the daemon).
"""

from __future__ import annotations

import socket
from unittest.mock import patch

import pytest

import daemon_helpers as dh


# ---------------------------------------------------- filter_by_min_level ---

class TestFilterByMinLevel:
    def _issues(self):
        return [
            {"level": "info", "summary": "i"},
            {"level": "minor", "summary": "m"},
            {"level": "major", "summary": "M"},
            {"level": "critical", "summary": "C"},
        ]

    def test_default_info_keeps_everything(self):
        assert len(dh.filter_by_min_level(self._issues(), "info")) == 4

    def test_minor_drops_info(self):
        levels = {i["level"] for i in dh.filter_by_min_level(self._issues(), "minor")}
        assert levels == {"minor", "major", "critical"}

    def test_critical_keeps_only_critical(self):
        levels = {i["level"] for i in dh.filter_by_min_level(self._issues(), "critical")}
        assert levels == {"critical"}

    def test_unknown_min_level_falls_through(self):
        # Unknown threshold maps to floor 0, so nothing is dropped.
        assert len(dh.filter_by_min_level(self._issues(), "totally-bogus")) == 4

    def test_unknown_issue_level_treated_as_floor(self):
        issues = [{"level": "unknown"}, {"level": "minor"}]
        # The unknown one passes the info filter (floor=info, level<info treated as 0 which is < info)
        # but is dropped at minor.
        assert dh.filter_by_min_level(issues, "minor") == [{"level": "minor"}]


# ---------------------------------------------------------- match_* ---------

class TestMatchers:
    def test_gcp_bare_label(self):
        assert dh.match_gcp({"project": "GCP"})
        assert dh.match_gcp({"project": "GCP / k1flow-prod"})
        assert not dh.match_gcp({"project": "GitHub"})
        assert not dh.match_gcp({"project": "GCPish"})  # prefix without separator

    def test_github_prefix(self):
        assert dh.match_github({"project": "GitHub / o/r"})
        assert dh.match_github({"project": "GitHub"})
        assert not dh.match_github({"project": "Anthropic"})

    def test_anthropic_exact(self):
        assert dh.match_anthropic({"project": "Anthropic"})
        assert not dh.match_anthropic({"project": "Anthropic / Claude API"})

    def test_anthropic_uses_pollers_constant(self):
        # Sanity: the matcher is wired to ANTHROPIC_LABEL, so changing
        # the constant in pollers must keep the matcher in sync.
        from pollers import ANTHROPIC_LABEL
        assert dh.match_anthropic({"project": ANTHROPIC_LABEL})


# --------------------------------------------------- configured_sources ----

class TestConfiguredSources:
    def test_empty_config_returns_empty(self):
        assert dh.configured_sources({"projects": []}) == []

    def test_default_order_when_unset(self):
        cfg = {"projects": [
            {"type": "gcp"}, {"type": "github"}, {"type": "anthropic"},
        ]}
        keys = [s[0] for s in dh.configured_sources(cfg)]
        assert keys == ["gcp", "github", "anthropic"]

    def test_explicit_order_respected(self):
        cfg = {
            "projects": [{"type": "gcp"}, {"type": "github"}, {"type": "anthropic"}],
            "settings": {"indicator_order": ["anthropic", "gcp", "github"]},
        }
        keys = [s[0] for s in dh.configured_sources(cfg)]
        assert keys == ["anthropic", "gcp", "github"]

    def test_unknown_keys_in_order_are_skipped(self):
        cfg = {
            "projects": [{"type": "gcp"}, {"type": "github"}],
            "settings": {"indicator_order": ["bogus", "github", "alsoBogus", "gcp"]},
        }
        keys = [s[0] for s in dh.configured_sources(cfg)]
        assert keys == ["github", "gcp"]

    def test_configured_but_unlisted_goes_last(self):
        # github is configured but not mentioned in order; it should appear last.
        cfg = {
            "projects": [{"type": "gcp"}, {"type": "github"}, {"type": "anthropic"}],
            "settings": {"indicator_order": ["anthropic", "gcp"]},
        }
        keys = [s[0] for s in dh.configured_sources(cfg)]
        assert keys == ["anthropic", "gcp", "github"]

    def test_missing_type_dropped(self):
        cfg = {"projects": [{"type": "gcp"}]}
        keys = [s[0] for s in dh.configured_sources(cfg)]
        assert keys == ["gcp"]


# -------------------------------------------------------------- compute_level

class TestComputeLevel:
    """Verifies the rules described in `compute_level`'s docstring. The
    big point of these tests is that we never silently report 'ok' (green)
    when the actual situation is 'we have no idea'."""

    def test_offline_wins_over_everything(self):
        assert dh.compute_level(
            [{"level": "critical", "kind": "incident"}],
            "minor", offline=True, has_polled=True,
        ) == "offline"

    def test_offline_wins_even_pre_poll(self):
        assert dh.compute_level(
            [], "minor", offline=True, has_polled=False,
        ) == "offline"

    def test_pre_poll_is_unknown_not_ok(self):
        # The whole point of the feature: do NOT show 'ok' on startup.
        assert dh.compute_level(
            [], "minor", offline=False, has_polled=False,
        ) == "unknown"

    def test_post_poll_with_no_issues_is_ok(self):
        assert dh.compute_level(
            [], "minor", offline=False, has_polled=True,
        ) == "ok"

    def test_only_feed_error_is_unknown_not_ok(self):
        # Even though min_level=minor filters the info-level feed-error
        # out of the worst() calculation, we must not report 'ok'; the
        # source is unreachable.
        issues = [{"level": "info", "kind": "feed-error",
                   "summary": "feed unreachable"}]
        assert dh.compute_level(
            issues, "minor", offline=False, has_polled=True,
        ) == "unknown"

    def test_only_feed_error_with_min_info_is_unknown(self):
        # Even when the feed-error survives the min-level filter, we
        # still want 'unknown' rather than 'info' because the meaning is
        # "we cannot answer", not "scheduled maintenance".
        issues = [{"level": "info", "kind": "feed-error"}]
        assert dh.compute_level(
            issues, "info", offline=False, has_polled=True,
        ) == "unknown"

    def test_real_issue_shadows_feed_error(self):
        # If we have a known-real problem, show its level. The feed-error
        # is still rendered in the dropdown via the menu code; it just
        # does not change the headline color.
        issues = [
            {"level": "info", "kind": "feed-error"},
            {"level": "major", "kind": "incident"},
        ]
        assert dh.compute_level(
            issues, "minor", offline=False, has_polled=True,
        ) == "major"

    def test_worst_picks_highest_real(self):
        issues = [
            {"level": "minor", "kind": "incident"},
            {"level": "critical", "kind": "incident"},
        ]
        assert dh.compute_level(
            issues, "minor", offline=False, has_polled=True,
        ) == "critical"

    def test_min_level_filter_drops_below_threshold(self):
        # An info-only real issue is below 'minor', so under min_level=minor
        # it should not push the indicator off green.
        issues = [{"level": "info", "kind": "incident"}]
        assert dh.compute_level(
            issues, "minor", offline=False, has_polled=True,
        ) == "ok"

    def test_min_level_info_keeps_info_issue(self):
        issues = [{"level": "info", "kind": "incident"}]
        assert dh.compute_level(
            issues, "info", offline=False, has_polled=True,
        ) == "info"


# -------------------------------------------------------------- is_online --

class TestIsOnline:
    def test_returns_true_when_first_host_succeeds(self):
        with patch.object(dh.socket, "create_connection") as cc:
            cc.return_value.__enter__.return_value = object()
            cc.return_value.__exit__.return_value = False
            assert dh.is_online() is True
            assert cc.call_count == 1   # short-circuits after 1.1.1.1

    def test_falls_through_to_second_host(self):
        calls = []

        def fake(addr, timeout):
            calls.append(addr)
            if addr[0] == "1.1.1.1":
                raise OSError("no route")

            class _Ctx:
                def __enter__(self_inner): return self_inner
                def __exit__(self_inner, *a): return False
            return _Ctx()

        with patch.object(dh.socket, "create_connection", side_effect=fake):
            assert dh.is_online() is True
        assert [a[0] for a in calls] == ["1.1.1.1", "8.8.8.8"]

    def test_returns_false_when_all_fail(self):
        with patch.object(dh.socket, "create_connection",
                          side_effect=OSError("nope")):
            assert dh.is_online() is False

    def test_timeout_propagates_to_socket(self):
        with patch.object(dh.socket, "create_connection") as cc:
            cc.return_value.__enter__.return_value = object()
            cc.return_value.__exit__.return_value = False
            dh.is_online(timeout=5.0)
            kwargs = cc.call_args.kwargs
            args = cc.call_args.args
            # create_connection is called positionally; verify timeout kwarg.
            assert kwargs.get("timeout") == 5.0 or 5.0 in args
