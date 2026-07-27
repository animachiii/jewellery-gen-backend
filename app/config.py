import base64
import hashlib
import json
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
    gdrive_folder_id: str = Field(alias="GDRIVE_FOLDER_ID")

    gemini_api_key: str = Field(alias="GEMINI_API_KEY")
    gemini_model: str = Field(default="gemini-2.5-flash", alias="GEMINI_MODEL")
    classifier_confidence_threshold: float = Field(
        default=0.75, alias="CLASSIFIER_CONFIDENCE_THRESHOLD"
    )

    higgsfield_api_key: str | None = Field(default=None, alias="HIGGSFIELD_API_KEY")
    provider: Literal["higgsfield", "fake"] = Field(default="higgsfield", alias="PROVIDER")

    local_storage_dir: str = Field(default="./data/storage", alias="LOCAL_STORAGE_DIR")
    storage_backend: Literal["local", "drive"] = Field(default="local", alias="STORAGE_BACKEND")

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

    sentry_dsn: str | None = Field(default=None, alias="SENTRY_DSN")

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

    def __repr__(self) -> str:
        return (
            f"Settings(env={self.env!r}, redis_url={self.redis_url!r}, "
            f"google_sheet_id={self.google_sheet_id!r}, provider={self.provider!r}, "
            f"api_keys=<{len(self.api_keys)} configured>)"
        )

    __str__ = __repr__


settings = Settings()  # type: ignore[call-arg]
