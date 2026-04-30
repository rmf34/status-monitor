#!/usr/bin/env bash
# Refreshes the GCP products.json snapshot used by test_map.py.
# Run this when test_map.py fails because GCP renamed/added a product.
set -euo pipefail
cd "$(dirname "$0")"
curl -fsSL https://status.cloud.google.com/products.json -o fixtures/gcp_products_snapshot.json
echo "Refreshed fixtures/gcp_products_snapshot.json"
echo "Re-run tests:  python3 -m pytest -q"
