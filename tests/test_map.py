"""
Validates data/gcp_product_map.yaml against a snapshotted GCP products
catalog. Fails the build when a map entry points at a title that no
longer exists upstream, before that quietly turns into "no alert ever".
"""

from __future__ import annotations

import json
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
MAP_PATH = ROOT / "data" / "gcp_product_map.yaml"
SNAPSHOT_PATH = Path(__file__).parent / "fixtures" / "gcp_products_snapshot.json"


def _load_map() -> dict[str, str]:
    with open(MAP_PATH) as f:
        return yaml.safe_load(f) or {}


def _load_catalog_titles() -> set[str]:
    with open(SNAPSHOT_PATH) as f:
        data = json.load(f)
    return {p["title"] for p in data["products"]}


def test_map_loads():
    m = _load_map()
    assert isinstance(m, dict) and m, "map empty or unparseable"


def test_all_keys_look_like_api_names():
    bad = [k for k in _load_map() if not k.endswith(".googleapis.com")]
    assert bad == [], f"keys must be googleapis.com names: {bad}"


def test_no_duplicate_keys_in_source():
    """YAML silently dedupes duplicate keys; check the raw text."""
    raw = MAP_PATH.read_text()
    keys: list[str] = []
    for line in raw.splitlines():
        line = line.split("#", 1)[0].strip()
        if not line or ":" not in line:
            continue
        keys.append(line.split(":", 1)[0].strip())
    seen: set[str] = set()
    dupes: list[str] = []
    for k in keys:
        if k in seen:
            dupes.append(k)
        seen.add(k)
    assert not dupes, f"duplicate API keys: {dupes}"


def test_all_map_targets_exist_in_catalog():
    titles = _load_catalog_titles()
    bad = [(api, t) for api, t in _load_map().items() if t not in titles]
    assert not bad, (
        f"map points at titles not in products.json: {bad}\n"
        "Either fix the title, or refresh the snapshot with "
        "tests/refresh_snapshot.sh."
    )
