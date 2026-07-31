"""Phase 6 Step 4 — frozen-contract snapshot.

docs/api-routes.md declares the v1 contract frozen at the end of Phase 1:
adding an optional field is fine, but renaming, removing, or retyping an
existing field is a breaking change requiring /api/v2. Nothing previously
failed a test run when a response model changed shape. This compares the
live OpenAPI schema against a committed snapshot and fails loudly on any
diff, so a breaking change must be a deliberate, reviewed snapshot update —
never a silent side effect of an unrelated schema tweak.
"""

import json
from pathlib import Path

from app.main import app

_SNAPSHOT_PATH = Path(__file__).parent / "fixtures" / "openapi_snapshot.json"


def _current_schema() -> dict[str, object]:
    # `app.openapi()` caches its result on the FastAPI instance after the
    # first call; clear it first so this reflects the current route/model
    # state regardless of what earlier tests in the same process did.
    app.openapi_schema = None
    return app.openapi()


def test_openapi_schema_matches_committed_snapshot() -> None:
    current = _current_schema()
    snapshot = json.loads(_SNAPSHOT_PATH.read_text())

    if current != snapshot:
        raise AssertionError(
            "The live OpenAPI schema no longer matches "
            f"{_SNAPSHOT_PATH.relative_to(Path(__file__).parent.parent)}.\n"
            "Per docs/api-routes.md, adding an optional field is fine; renaming, "
            "removing, or retyping an existing field is a breaking change requiring "
            "/api/v2. If this change was deliberate and reviewed, regenerate the "
            "snapshot with:\n\n"
            '  python -c "from app.main import app; import json; '
            "json.dump(app.openapi(), open('tests/fixtures/openapi_snapshot.json', 'w'), "
            'indent=2, sort_keys=True)"\n'
        )


def test_snapshot_file_is_deterministically_sorted() -> None:
    """Guards against the snapshot itself silently drifting from the format
    the regeneration command above produces (unsorted keys would make future
    diffs noisy and hide the actual change)."""
    raw = _SNAPSHOT_PATH.read_text()
    parsed = json.loads(raw)
    resorted = json.dumps(parsed, indent=2, sort_keys=True) + "\n"
    assert raw == resorted
