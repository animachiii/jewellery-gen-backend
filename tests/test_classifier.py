"""Phase 3 Step 1 — real Gemini classifier (docs/ai-integration.md §1).

Testing strategy (a) from phases/phase-3-classification-matrix.md: fake at the
constructor-injected-client boundary, mirroring GoogleSheetsClient/
GoogleDriveClient. `GeminiClassifier.__init__` accepts an injectable client
object; tests substitute a fake exposing the minimal `aio.models.
generate_content` surface `GeminiClassifier` actually calls (confirmed via
SDK introspection against the installed google-genai==0.3.0), never touching
the real network.
"""

import json
import re
from dataclasses import dataclass
from typing import Any

import pytest

from app.models.enums import JewelryType
from app.services.classifier import (
    SYSTEM_INSTRUCTION,
    ClassificationResult,
    Classifier,
    GeminiClassifier,
    Prediction,
    StubClassifier,
    get_classifier,
)


@dataclass
class _FakeResponse:
    _text: str

    @property
    def text(self) -> str:
        return self._text


class _FakeModels:
    """Fakes the `client.aio.models` surface `GeminiClassifier` calls."""

    def __init__(self, response_json: dict[str, Any]) -> None:
        self.response_json = response_json
        self.calls: list[dict[str, Any]] = []

    async def generate_content(self, *, model: str, contents: Any, config: Any) -> _FakeResponse:
        self.calls.append({"model": model, "contents": contents, "config": config})
        return _FakeResponse(json.dumps(self.response_json))


class _FakeAio:
    def __init__(self, models: _FakeModels) -> None:
        self.models = models


class _FakeGeminiClient:
    """Minimal stand-in for `google.genai.Client` — only the `.aio.models.
    generate_content` surface `GeminiClassifier` actually calls."""

    def __init__(self, response_json: dict[str, Any]) -> None:
        self.models = _FakeModels(response_json)
        self.aio = _FakeAio(self.models)


def _three_prediction_response(
    is_jewelry: bool = True,
    predictions: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "is_jewelry": is_jewelry,
        "predictions": predictions
        or [
            {"jewelry_type": "ANKLET", "confidence": 0.94},
            {"jewelry_type": "BRACELET", "confidence": 0.04},
            {"jewelry_type": "CHAIN", "confidence": 0.02},
        ],
        "notes": "debug only",
    }


def test_gemini_classifier_satisfies_classifier_protocol() -> None:
    c: Classifier = GeminiClassifier(client=_FakeGeminiClient(_three_prediction_response()))
    assert isinstance(c, GeminiClassifier)


async def test_classify_round_trips_three_predictions_descending_confidence() -> None:
    fake_client = _FakeGeminiClient(
        _three_prediction_response(
            predictions=[
                {"jewelry_type": "RING", "confidence": 0.9},
                {"jewelry_type": "BRACELET", "confidence": 0.07},
                {"jewelry_type": "BANGLE", "confidence": 0.03},
            ]
        )
    )
    classifier = GeminiClassifier(client=fake_client)

    result = await classifier.classify(b"fake-image-bytes")

    assert isinstance(result, ClassificationResult)
    assert result.is_jewelry is True
    assert len(result.predictions) == 3
    assert result.predictions[0] == Prediction(jewelry_type=JewelryType.RING, confidence=0.9)
    assert result.predictions[1].confidence >= result.predictions[2].confidence
    confidences = [p.confidence for p in result.predictions]
    assert confidences == sorted(confidences, reverse=True)


async def test_classify_not_jewelry_round_trips() -> None:
    fake_client = _FakeGeminiClient(_three_prediction_response(is_jewelry=False))
    classifier = GeminiClassifier(client=fake_client)

    result = await classifier.classify(b"fake-image-bytes")

    assert result.is_jewelry is False


async def test_system_instruction_sent_is_byte_identical_to_docs() -> None:
    fake_client = _FakeGeminiClient(_three_prediction_response())
    classifier = GeminiClassifier(client=fake_client)

    await classifier.classify(b"fake-image-bytes")

    call = fake_client.models.calls[0]
    sent_instruction = call["config"].system_instruction
    assert sent_instruction == SYSTEM_INSTRUCTION

    # Cross-check straight from docs/ai-integration.md so a future doc edit
    # that isn't mirrored into classifier.py fails this test.
    from pathlib import Path

    doc_text = Path("docs/ai-integration.md").read_text()
    m = re.search(r"### System instruction \(verbatim\)\n\n```\n(.*?)\n```", doc_text, re.S)
    assert m is not None
    assert SYSTEM_INSTRUCTION == m.group(1)


async def test_classify_discards_out_of_enum_predictions_and_raises_when_none_valid() -> None:
    fake_client = _FakeGeminiClient(
        _three_prediction_response(
            predictions=[
                {"jewelry_type": "NOSE_PIN", "confidence": 0.9},
                {"jewelry_type": "MANGALSUTRA", "confidence": 0.07},
                {"jewelry_type": "CHAIN", "confidence": 0.03},
            ]
        )
    )
    classifier = GeminiClassifier(client=fake_client)

    with pytest.raises(ValueError):
        await classifier.classify(b"fake-image-bytes")


async def test_classify_discards_out_of_enum_prediction_keeps_valid_ones() -> None:
    fake_client = _FakeGeminiClient(
        _three_prediction_response(
            predictions=[
                {"jewelry_type": "RING", "confidence": 0.9},
                {"jewelry_type": "CHAIN", "confidence": 0.07},
                {"jewelry_type": "BANGLE", "confidence": 0.03},
            ]
        )
    )
    classifier = GeminiClassifier(client=fake_client)

    result = await classifier.classify(b"fake-image-bytes")

    assert [p.jewelry_type for p in result.predictions] == [JewelryType.RING, JewelryType.BANGLE]


async def test_classify_raises_on_malformed_json() -> None:
    fake_client = _FakeGeminiClient({})

    class _BrokenModels(_FakeModels):
        async def generate_content(
            self, *, model: str, contents: Any, config: Any
        ) -> _FakeResponse:
            return _FakeResponse("not json{{{")

    fake_client.models = _BrokenModels({})
    fake_client.aio = _FakeAio(fake_client.models)
    classifier = GeminiClassifier(client=fake_client)

    with pytest.raises(ValueError):
        await classifier.classify(b"fake-image-bytes")


def test_get_classifier_returns_gemini_classifier_by_default() -> None:
    assert isinstance(get_classifier(), GeminiClassifier)


def test_get_classifier_monkeypatchable(monkeypatch: pytest.MonkeyPatch) -> None:
    import app.services.classifier as classifier_module

    monkeypatch.setattr(classifier_module, "get_classifier", lambda: StubClassifier())
    assert isinstance(classifier_module.get_classifier(), StubClassifier)
