"""Jewellery type classification (docs/ai-integration.md §1).

Phase 1: `StubClassifier` returns a fixed high-confidence prediction. Phase 3
replaces this with a real Gemini 2.5 Flash call behind the same
`classify(image_bytes) -> ClassificationResult` signature.

SDK note (introspected against the installed `google-genai==0.3.0`, do not
assume from general knowledge — see phases/phase-3-classification-matrix.md
Step 1's mandatory introspection instruction):

- `genai.Client(api_key=..., http_options=HttpOptions(timeout=...))` — plain
  constructor-injected API key, no builder/transport step. `http_options`
  (per-client, in **milliseconds**) is the only place this SDK version
  exposes a request timeout; `GenerateContentConfig` itself has no
  `http_options`/timeout field.
- The async surface is `client.aio.models.generate_content(model=..., \
  contents=..., config=...)` — a coroutine, unlike the sync `client.models.\
  generate_content`.
- Image bytes go in as `types.Part.from_bytes(data=..., mime_type=...)` inside
  `contents`.
- Structured output is `GenerateContentConfig(response_mime_type=\
  "application/json", response_schema=<dict|Schema>)`. `types.Schema` DOES
  support an `enum` field (JSON-schema-style literal constraint), so the
  `jewelry_type` field's allowed values can be constrained server-side.
  Client-side validation/discarding is still implemented as a defensive
  second layer per the phase spec, in case the constraint is ever loosened
  or the server returns something unexpected.
- The response's `.text` property returns the raw JSON string (safer to
  `json.loads` ourselves than to rely on `.parsed`, which only works
  reliably when `response_schema` is a Python type/TypedDict rather than a
  plain dict schema).

Testing strategy: option (a) from the phase spec — `GeminiClassifier.__init__`
accepts an injectable `client` object satisfying the minimal
`client.aio.models.generate_content(...)` surface used here, mirroring
`GoogleSheetsClient`/`GoogleDriveClient`'s constructor-injected-client
pattern. Tests substitute a fake; production code lets `get_classifier()`
construct the real `google.genai.Client`.
"""

import json
import time
from dataclasses import dataclass
from typing import Any, Protocol

from google import genai
from google.genai import types
from google.genai._api_client import HttpOptions

from app.config import settings
from app.core.logging import get_logger
from app.models.enums import JewelryType

log = get_logger(__name__)

# docs/ai-integration.md §1 "System instruction (verbatim)" — must stay
# byte-identical to that doc. tests/test_classifier.py asserts this directly
# against the doc file, so a future doc edit that isn't mirrored here fails
# the suite instead of silently drifting.
SYSTEM_INSTRUCTION = (
    "You are a jewellery classification system for a product catalogue pipeline.\n"
    "\n"
    "Given a product photograph, identify the single jewellery type it depicts.\n"
    "\n"
    "Rules:\n"
    "- Choose only from the provided enum. Never invent a type.\n"
    "- If the image contains no jewellery, set is_jewelry to false.\n"
    "- If multiple pieces are present, classify the most prominent one.\n"
    "- Judge by the physical form of the piece, not by how it is worn or styled.\n"
    "- Confidence must reflect genuine uncertainty. Do not default to high\n"
    "  confidence. Visually similar categories (anklet vs bracelet, chain vs\n"
    "  necklace, pendant vs necklace) should receive proportionally split scores\n"
    "  when the image does not clearly distinguish them.\n"
    "\n"
    "Return exactly three predictions ordered by descending confidence."
)

# docs/ai-integration.md §4 — 15s classifier timeout, expressed in ms per
# this SDK version's HttpOptions.
_CLASSIFIER_TIMEOUT_MS = 15_000

_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "OBJECT",
    "properties": {
        "is_jewelry": {"type": "BOOLEAN"},
        "predictions": {
            "type": "ARRAY",
            "min_items": 3,
            "max_items": 3,
            "items": {
                "type": "OBJECT",
                "properties": {
                    "jewelry_type": {
                        "type": "STRING",
                        "enum": [t.value for t in JewelryType],
                    },
                    "confidence": {"type": "NUMBER"},
                },
                "required": ["jewelry_type", "confidence"],
            },
        },
        "notes": {"type": "STRING"},
    },
    "required": ["is_jewelry", "predictions"],
}


@dataclass
class Prediction:
    jewelry_type: JewelryType
    confidence: float


@dataclass
class ClassificationResult:
    is_jewelry: bool
    predictions: list[Prediction]


class Classifier(Protocol):
    async def classify(self, image_bytes: bytes) -> ClassificationResult: ...


class StubClassifier:
    """Always returns a fixed high-confidence RING prediction plus two lower-
    confidence filler predictions, descending by confidence. Real Gemini
    classification is Phase 3 (docs/ai-integration.md §1)."""

    async def classify(self, image_bytes: bytes) -> ClassificationResult:
        return ClassificationResult(
            is_jewelry=True,
            predictions=[
                Prediction(jewelry_type=JewelryType.RING, confidence=0.95),
                Prediction(jewelry_type=JewelryType.BRACELET, confidence=0.03),
                Prediction(jewelry_type=JewelryType.BANGLE, confidence=0.02),
            ],
        )


class GeminiClassifier:
    """Real Gemini 2.5 Flash classifier (docs/ai-integration.md §1).

    No retry logic here — the worker's `_do_classify_body` already wraps this
    call in `retry_free` (3x, 2s/8s/30s per R1). Adding a second retry layer
    here would multiply the actual API retry count against the real Gemini
    endpoint, which is explicitly forbidden by the phase spec.
    """

    def __init__(self, client: Any | None = None) -> None:
        self._client = client or genai.Client(
            api_key=settings.gemini_api_key,
            http_options=HttpOptions(timeout=_CLASSIFIER_TIMEOUT_MS),
        )

    async def classify(self, image_bytes: bytes) -> ClassificationResult:
        start = time.monotonic()
        model = settings.gemini_model
        try:
            response = await self._client.aio.models.generate_content(
                model=model,
                contents=[
                    types.Part.from_bytes(data=image_bytes, mime_type="image/jpeg"),
                ],
                config=types.GenerateContentConfig(
                    system_instruction=SYSTEM_INSTRUCTION,
                    response_mime_type="application/json",
                    response_schema=_RESPONSE_SCHEMA,
                ),
            )
            result = self._parse_response(response.text)
        except Exception:
            latency_ms = int((time.monotonic() - start) * 1000)
            log.warning(
                "classifier.gemini.call_failed",
                model=model,
                latency_ms=latency_ms,
            )
            raise

        latency_ms = int((time.monotonic() - start) * 1000)
        top_confidence = result.predictions[0].confidence if result.predictions else None
        log.info(
            "classifier.gemini.call_completed",
            model=model,
            latency_ms=latency_ms,
            outcome="is_jewelry" if result.is_jewelry else "not_jewelry",
            top_confidence=top_confidence,
        )
        return result

    @staticmethod
    def _parse_response(raw_text: str | None) -> ClassificationResult:
        """Parses the raw JSON text into a `ClassificationResult`, discarding
        any prediction whose `jewelry_type` isn't a valid `JewelryType`
        (docs/ai-integration.md §1's failure table: "Type not in enum ...
        Discard prediction"). Malformed JSON, a missing `predictions` key, or
        zero valid predictions surviving that filter are all treated as an
        API-error-equivalent failure — raise, so the caller's `retry_free`
        wrapper retries; if retries exhaust, it surfaces as `CLASSIFIER_ERROR`
        same as any other malformed-output failure (there is no clean way to
        synthesize `candidate_types` from zero valid predictions)."""
        if raw_text is None:
            raise ValueError("classifier.gemini: empty response body")

        try:
            data = json.loads(raw_text)
        except json.JSONDecodeError as exc:
            raise ValueError("classifier.gemini: malformed JSON response") from exc

        if not isinstance(data, dict) or "is_jewelry" not in data or "predictions" not in data:
            raise ValueError("classifier.gemini: schema-violating response")

        valid_types = {t.value for t in JewelryType}
        predictions: list[Prediction] = []
        for entry in data["predictions"]:
            jewelry_type_raw = entry.get("jewelry_type")
            if jewelry_type_raw not in valid_types:
                continue
            predictions.append(
                Prediction(
                    jewelry_type=JewelryType(jewelry_type_raw),
                    confidence=float(entry["confidence"]),
                )
            )

        if not predictions:
            raise ValueError("classifier.gemini: no predictions with a valid jewelry_type")

        predictions.sort(key=lambda p: p.confidence, reverse=True)

        return ClassificationResult(is_jewelry=bool(data["is_jewelry"]), predictions=predictions)


def get_classifier() -> Classifier:
    """Factory mirroring app/providers/factory.py / app/storage/factory.py's
    pattern. No settings branch — there's no "fake" mode toggle for the
    classifier the way there is for provider/storage backends; tests
    monkeypatch this function itself (e.g. `app.worker.tasks.get_classifier`)
    to substitute `StubClassifier()` or a custom fake."""
    return GeminiClassifier()
