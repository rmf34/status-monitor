"""Tests for the small bits of setup_cli that are pure-function-like."""

from __future__ import annotations

import setup_cli


def test_upsert_appends_new_when_absent():
    cfg = {"projects": []}
    setup_cli.upsert_project(cfg, "gcp:p", "gcp", {"a": 1})
    assert cfg["projects"] == [
        {"name": "gcp:p", "type": "gcp", "config": {"a": 1}}
    ]


def test_upsert_replaces_existing_in_place():
    cfg = {"projects": [
        {"name": "gcp:p", "type": "gcp", "config": {"a": 1}},
        {"name": "github:o/r", "type": "github", "config": {"x": 1}},
    ]}
    setup_cli.upsert_project(cfg, "gcp:p", "gcp", {"a": 2})
    assert cfg["projects"] == [
        {"name": "gcp:p", "type": "gcp", "config": {"a": 2}},
        {"name": "github:o/r", "type": "github", "config": {"x": 1}},
    ], "must replace in place; order should not change"


def test_upsert_preserves_unrelated_entries():
    cfg = {"projects": [
        {"name": "anthropic", "type": "anthropic",
         "config": {"components": ["Claude API (api.anthropic.com)"]}}
    ]}
    setup_cli.upsert_project(cfg, "gcp:newone", "gcp",
                             {"project_id": "newone", "services": []})
    names = [p["name"] for p in cfg["projects"]]
    assert names == ["anthropic", "gcp:newone"]
