"""app/config.py's whitespace-stripping guard.

Regression test: found in practice when deploying via `docker run
--env-file` (simulating Render/Railway's real OS-environment injection) --
a `SUPABASE_SERVICE_ROLE_KEY` with a leading space (present in the local
.env, silently stripped there by python-dotenv) survived unstripped when
sourced from a real environment variable instead, and broke Supabase Storage
uploads with an opaque httpx "Illegal header value" error nowhere near this
config. `Settings._strip_whitespace_from_string_values` is a
`model_validator(mode="before")` that closes this gap for every string env
value, not just this one field.
"""

from app.config import Settings


def test_strip_whitespace_strips_leading_and_trailing_space() -> None:
    data = {"SUPABASE_SERVICE_ROLE_KEY": " abc123 ", "SUPABASE_STORAGE_BUCKET": " bucket "}
    result = Settings._strip_whitespace_from_string_values(data)
    assert result["SUPABASE_SERVICE_ROLE_KEY"] == "abc123"
    assert result["SUPABASE_STORAGE_BUCKET"] == "bucket"


def test_strip_whitespace_leaves_non_string_values_untouched() -> None:
    data = {"SOME_BOOL": True, "SOME_NUM": 5, "SOME_DICT": {"a": 1}}
    result = Settings._strip_whitespace_from_string_values(data)
    assert result == data


def test_strip_whitespace_passes_through_non_dict_input_unchanged() -> None:
    # pydantic-settings can call "before" model validators with a non-dict
    # in some source-merging paths -- must not raise, must pass through.
    assert Settings._strip_whitespace_from_string_values("not-a-dict") == "not-a-dict"


def test_strip_whitespace_does_not_collapse_internal_whitespace() -> None:
    # Only leading/trailing whitespace is incidental copy-paste noise --
    # internal spaces (e.g. a real value that legitimately contains one)
    # must survive untouched.
    data = {"SOME_FIELD": "  a b  "}
    result = Settings._strip_whitespace_from_string_values(data)
    assert result["SOME_FIELD"] == "a b"
