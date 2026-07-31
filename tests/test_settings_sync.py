"""Phase 6 Step 4 — .env.example / Settings sync.

docs/conventions.md -> Configuration: "`.env.example` lists every variable
with a safe placeholder and stays in sync with `docs/schema.md` §6." Nothing
previously enforced this; a new Settings field could be added without ever
touching .env.example (or vice versa) and no test would notice.
"""

import re
from pathlib import Path

from app.config import Settings

_ENV_EXAMPLE_PATH = Path(__file__).parent.parent / ".env.example"
_SCHEMA_DOC_PATH = Path(__file__).parent.parent / "docs" / "schema.md"

# Testing/dev-only knob that's a real Settings field but deliberately absent
# from .env.example and docs/schema.md §6 (per app/config.py's own comment:
# "not a deployment var") — named here so a genuinely-missing production var
# can't hide behind this exclusion.
_DEV_ONLY_ALIASES = {"FAKE_FAIL_MODE"}


def _env_example_var_names() -> set[str]:
    names: set[str] = set()
    for line in _ENV_EXAMPLE_PATH.read_text().splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        match = re.match(r"^([A-Z][A-Z0-9_]*)=", stripped)
        if match:
            names.add(match.group(1))
    return names


def _settings_env_var_aliases() -> set[str]:
    aliases: set[str] = set()
    for field in Settings.model_fields.values():
        if field.alias:
            aliases.add(field.alias)
    return aliases


def test_every_settings_field_has_an_env_example_entry() -> None:
    settings_aliases = _settings_env_var_aliases()
    env_example_names = _env_example_var_names()

    missing = settings_aliases - env_example_names - _DEV_ONLY_ALIASES
    assert not missing, (
        f"Settings field(s) {sorted(missing)} have no corresponding entry in "
        ".env.example — add one with a safe placeholder (docs/conventions.md -> "
        "Configuration)."
    )


def test_every_env_example_entry_maps_to_a_real_settings_field() -> None:
    settings_aliases = _settings_env_var_aliases()
    env_example_names = _env_example_var_names()

    orphaned = env_example_names - settings_aliases
    assert not orphaned, (
        f".env.example entry/entries {sorted(orphaned)} do not correspond to any "
        "Settings field — either a stale leftover or a typo'd alias."
    )


def test_schema_doc_env_table_matches_settings_fields() -> None:
    """docs/schema.md §6 is a hand-maintained table of every env var. Extract
    the `Var` column (first `|`-delimited cell of each data row) and confirm
    it's the same set as Settings' actual aliases — this is what "stays in
    sync with docs/schema.md §6" (docs/conventions.md) actually means,
    checked mechanically rather than by eyeballing a doc on each phase."""
    text = _SCHEMA_DOC_PATH.read_text()
    section = text.split("## 6. Environment Variables", 1)[1]
    section = section.split("\n## ", 1)[0]

    doc_vars: set[str] = set()
    for line in section.splitlines():
        line = line.strip()
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if not cells:
            continue
        name = cells[0].strip("`")
        if re.match(r"^[A-Z][A-Z0-9_]*$", name):
            doc_vars.add(name)

    settings_aliases = _settings_env_var_aliases()
    missing_from_doc = settings_aliases - doc_vars - _DEV_ONLY_ALIASES
    extra_in_doc = doc_vars - settings_aliases

    assert not missing_from_doc, (
        f"Settings field(s) {sorted(missing_from_doc)} are not documented in " "docs/schema.md §6."
    )
    assert not extra_in_doc, (
        f"docs/schema.md §6 documents {sorted(extra_in_doc)}, which no longer "
        "correspond to a real Settings field."
    )
