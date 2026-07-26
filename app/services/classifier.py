"""Jewellery type classification (docs/ai-integration.md §1).

Phase 1: `StubClassifier` returns a fixed high-confidence prediction. Phase 3
replaces this with a real Gemini 2.5 Flash call behind the same
`classify(image_bytes) -> ClassificationResult` signature.
"""

from dataclasses import dataclass

from app.models.enums import JewelryType


@dataclass
class Prediction:
    jewelry_type: JewelryType
    confidence: float


@dataclass
class ClassificationResult:
    is_jewelry: bool
    predictions: list[Prediction]


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
