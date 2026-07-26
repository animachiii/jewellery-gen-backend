#!/usr/bin/env python
"""Exports the FastAPI app's OpenAPI spec to docs/openapi.json.

Run after any route/schema change to keep the frozen Phase 1 contract
(docs/api-routes.md) in sync with what the app actually serves:

    python scripts/export_openapi.py
"""

import json
from pathlib import Path

from app.main import app

OUTPUT_PATH = Path(__file__).resolve().parents[1] / "docs" / "openapi.json"


def main() -> None:
    spec = app.openapi()
    OUTPUT_PATH.write_text(json.dumps(spec, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"Wrote {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
