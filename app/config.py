import base64
import hashlib
import json
import os
from typing import Annotated, Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


def _parse_api_keys(raw: str) -> dict[str, str]:
    """Parse 'name:key,name2:key2' into {sha256(key): name}. Plaintext is discarded."""
    result: dict[str, str] = {}
    for pair in raw.split(","):
        pair = pair.strip()
        if not pair:
            continue
        name, _, key = pair.partition(":")
        if not name or not key:
            raise ValueError(f"API_KEYS entry '{pair}' is not in 'name:key' format")
        result[hashlib.sha256(key.encode()).hexdigest()] = name
    if not result:
        raise ValueError("API_KEYS must contain at least one 'name:key' pair")
    return result


def _parse_cors_origins(raw: str) -> tuple[str, ...]:
    """Comma-separated list of allowed CORS origins. Empty string -> no
    cross-origin access at all (fail closed — docs/business-rules.md's CORS
    section). Whitespace around each origin is trimmed; blank entries are
    dropped so a trailing comma doesn't silently become a wildcard-like ''."""
    origins = tuple(o.strip() for o in raw.split(",") if o.strip())
    return origins


def _decode_service_account_json(raw: str) -> dict[str, object]:
    try:
        decoded = base64.b64decode(raw, validate=True)
    except Exception as exc:
        raise ValueError("GOOGLE_SERVICE_ACCOUNT_JSON is not valid base64") from exc
    try:
        parsed = json.loads(decoded)
    except json.JSONDecodeError as exc:
        raise ValueError("GOOGLE_SERVICE_ACCOUNT_JSON does not decode to valid JSON") from exc
    if not isinstance(parsed, dict) or "client_email" not in parsed:
        raise ValueError("GOOGLE_SERVICE_ACCOUNT_JSON is missing a 'client_email' field")
    return parsed


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    env: Literal["local", "staging", "prod"] = Field(default="local", alias="ENV")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")

    api_keys: Annotated[dict[str, str], NoDecode] = Field(alias="API_KEYS")
    admin_api_key: str = Field(alias="ADMIN_API_KEY")

    redis_url: str = Field(alias="REDIS_URL")

    google_sheet_id: str = Field(alias="GOOGLE_SHEET_ID")
    google_service_account_info: Annotated[dict[str, object], NoDecode] = Field(
        alias="GOOGLE_SERVICE_ACCOUNT_JSON"
    )
    # Optional: only used by DriveStorage (app/storage/drive.py), which is
    # built and tested but not the active backend (STORAGE_BACKEND=supabase
    # in production -- see docs/schema.md's storage adapter note). Required
    # only when STORAGE_BACKEND=drive, enforced below.
    gdrive_folder_id: str | None = Field(default=None, alias="GDRIVE_FOLDER_ID")

    gemini_api_key: str = Field(alias="GEMINI_API_KEY")
    gemini_model: str = Field(default="gemini-3.1-flash-lite", alias="GEMINI_MODEL")
    classifier_confidence_threshold: float = Field(
        default=0.75, alias="CLASSIFIER_CONFIDENCE_THRESHOLD"
    )
    # Used only when PROVIDER=gemini_image (app/providers/gemini_image.py).
    # Separate from gemini_model -- classification and image generation are
    # different Gemini model families and swap independently.
    gemini_image_model: str = Field(
        default="gemini-3.1-flash-image", alias="GEMINI_IMAGE_MODEL"
    )

    higgsfield_api_key: str | None = Field(default=None, alias="HIGGSFIELD_API_KEY")
    # PLACEHOLDER default -- unconfirmed real Higgsfield base URL, see
    # phases/phase-4-provider-integration.md "Manual Verification".
    higgsfield_base_url: str = Field(
        default="https://api.higgsfield.ai", alias="HIGGSFIELD_BASE_URL"
    )
    provider: Literal["higgsfield", "fake", "higgsfield_mcp_bridge", "gemini_image"] = Field(
        default="higgsfield", alias="PROVIDER"
    )
    # TESTING/SHOWCASE ONLY -- see app/providers/higgsfield_mcp_bridge.py.
    # Not the intended production path; used only when PROVIDER=higgsfield_mcp_bridge.
    higgsfield_mcp_bridge_url: str = Field(
        default="http://127.0.0.1:8799", alias="HIGGSFIELD_MCP_BRIDGE_URL"
    )

    local_storage_dir: str = Field(default="./data/storage", alias="LOCAL_STORAGE_DIR")
    storage_backend: Literal["local", "drive", "supabase"] = Field(
        default="local", alias="STORAGE_BACKEND"
    )

    supabase_url: str | None = Field(default=None, alias="SUPABASE_URL")
    supabase_service_role_key: str | None = Field(default=None, alias="SUPABASE_SERVICE_ROLE_KEY")
    supabase_storage_bucket: str | None = Field(default=None, alias="SUPABASE_STORAGE_BUCKET")

    # Testing/dev-only knob for FakeProvider failure injection (not a deployment var).
    fake_fail_mode: Literal["submit", "poll", "timeout", "none"] = Field(
        default="none", alias="FAKE_FAIL_MODE"
    )

    max_image_bytes: int = Field(default=15_728_640, alias="MAX_IMAGE_BYTES")
    job_deadline_seconds: int = Field(default=900, alias="JOB_DEADLINE_SECONDS")
    dedupe_window_seconds: int = Field(default=86_400, alias="DEDUPE_WINDOW_SECONDS")
    matrix_cache_ttl: int = Field(default=300, alias="MATRIX_CACHE_TTL")
    worker_concurrency: int = Field(default=4, alias="WORKER_CONCURRENCY")
    daily_generation_cap: int = Field(default=200, alias="DAILY_GENERATION_CAP")
    rate_limit_per_minute: int = Field(default=60, alias="RATE_LIMIT_PER_MINUTE")
    # Phase 7 Step 5 — resolves docs/business-rules.md R20's documented
    # tension: 10 concurrent jobs polling GET /jobs/{id} at the recommended
    # 5s interval is 120/min, which would trip the 60/min default. This is a
    # separate, more generous limit for that one hot, cheap, read-only route
    # — everything else (POST /generate, resolve, assets, matrix) keeps the
    # tighter default. 180/min clears 10-concurrent-at-5s (120/min) with
    # comfortable headroom for the client polling a few extra in-flight jobs.
    polling_rate_limit_per_minute: int = Field(default=180, alias="POLLING_RATE_LIMIT_PER_MINUTE")

    # Phase 7 R21 — fail closed by default (empty = no cross-origin access at
    # all). A real cross-origin client (Flutter web build, separately-hosted
    # showcase) must be added explicitly; there is no implicit same-origin
    # assumption baked into this setting.
    cors_allowed_origins: Annotated[tuple[str, ...], NoDecode] = Field(
        default=(), alias="CORS_ALLOWED_ORIGINS"
    )

    sentry_dsn: str | None = Field(default=None, alias="SENTRY_DSN")

    # Phase 9 (free-tier deploy path only): run the ARQ worker loop as a
    # background task inside the API process instead of a separate worker
    # process/container. Exists solely for hosts with no free always-on
    # background-worker tier (e.g. Render's free Web Service) — see
    # docs/deployment-free-tier.md. Never set true for the Railway/Compose
    # topology, which runs api and worker as separate processes as designed.
    worker_in_process: bool = Field(default=False, alias="WORKER_IN_PROCESS")

    @model_validator(mode="before")
    @classmethod
    def _strip_whitespace_from_string_values(cls, data: object) -> object:
        """Deploy platforms (Render/Railway dashboards, `docker run
        --env-file`) inject raw OS environment variables directly -- unlike
        pydantic-settings' own `.env`-file loading (python-dotenv), that path
        does NOT strip incidental leading/trailing whitespace from a pasted
        value. A single leading space in a copy-pasted secret (found in
        practice: a `SUPABASE_SERVICE_ROLE_KEY` pasted with a leading space)
        passes silently in local `.env`-based dev and testing (dotenv strips
        it there) and then breaks in real deployment with an opaque httpx
        "Illegal header value" error at request time -- nowhere near this
        config, and hard to trace back here. Stripped once, for every string
        value, before any other validation (including the field-level
        `_parse_*_field` validators below) runs."""
        if isinstance(data, dict):
            return {k: (v.strip() if isinstance(v, str) else v) for k, v in data.items()}
        return data

    @field_validator("api_keys", mode="before")
    @classmethod
    def _parse_api_keys_field(cls, v: object) -> dict[str, str]:
        # Transforms the raw 'name:key,...' string into {sha256(key): name} at validation
        # time so the plaintext key never persists as an attribute on the instance.
        if isinstance(v, dict):
            return v
        if not isinstance(v, str):
            raise ValueError("API_KEYS must be a string")
        return _parse_api_keys(v)

    @field_validator("cors_allowed_origins", mode="before")
    @classmethod
    def _parse_cors_origins_field(cls, v: object) -> tuple[str, ...]:
        if isinstance(v, tuple):
            return v
        if not isinstance(v, str):
            raise ValueError("CORS_ALLOWED_ORIGINS must be a string")
        return _parse_cors_origins(v)

    @field_validator("google_service_account_info", mode="before")
    @classmethod
    def _parse_service_account_field(cls, v: object) -> dict[str, object]:
        if isinstance(v, dict):
            return v
        if not isinstance(v, str):
            raise ValueError("GOOGLE_SERVICE_ACCOUNT_JSON must be a string")
        return _decode_service_account_json(v)

    @model_validator(mode="after")
    def _require_higgsfield_key_outside_local(self) -> "Settings":
        if self.env != "local" and self.provider == "higgsfield" and not self.higgsfield_api_key:
            raise ValueError("HIGGSFIELD_API_KEY is required when ENV != local")
        return self

    @model_validator(mode="after")
    def _require_supabase_config_when_selected(self) -> "Settings":
        if self.storage_backend == "supabase" and not (
            self.supabase_url and self.supabase_service_role_key and self.supabase_storage_bucket
        ):
            raise ValueError(
                "SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY, and SUPABASE_STORAGE_BUCKET are "
                "all required when STORAGE_BACKEND=supabase"
            )
        return self

    @model_validator(mode="after")
    def _require_gdrive_folder_id_when_selected(self) -> "Settings":
        if self.storage_backend == "drive" and not self.gdrive_folder_id:
            raise ValueError("GDRIVE_FOLDER_ID is required when STORAGE_BACKEND=drive")
        return self

    def __repr__(self) -> str:
        return (
            f"Settings(env={self.env!r}, redis_url={self.redis_url!r}, "
            f"google_sheet_id={self.google_sheet_id!r}, provider={self.provider!r}, "
            f"api_keys=<{len(self.api_keys)} configured>)"
        )

    __str__ = __repr__


settings = Settings()  # type: ignore[call-arg]


def reload_api_keys() -> int:
    """Phase 7 Step 3 — key rotation without a process restart.

    Re-reads `API_KEYS` from the process environment (not from `.env` on
    disk — the deploy platform is expected to update the env var and trigger
    `POST /api/v1/admin/keys/reload`, not have the app poll a file) and
    atomically swaps `settings.api_keys`, the in-memory hash map
    `app/api/deps.py`'s `require_client_key` looks up on every request.

    Deliberately scoped to `API_KEYS` only. `admin_api_key`,
    `google_service_account_info`, `gemini_api_key`, and `higgsfield_api_key`
    are NOT reloadable this way — those are used to construct long-lived SDK
    clients / are compared as a single static admin credential (see
    `docs/schema.md` §5's admin-key review), and rotating them safely needs a
    real process restart, not a hot-swap. This function only ever touches
    `settings.api_keys`.

    Also API-process-only by design, not an oversight: no code path under
    `app/worker/` ever reads `settings.api_keys` (client-key verification only
    happens in `require_client_key`, an API-only dependency), so a worker
    process has nothing to rotate.
    """
    raw = os.environ.get("API_KEYS")
    if not raw:
        raise ValueError("API_KEYS is not set in the current process environment")
    settings.api_keys = _parse_api_keys(raw)
    return len(settings.api_keys)
