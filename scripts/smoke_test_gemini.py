"""Manual, one-off smoke test for the real GeminiClassifier.

Not part of the automated test suite (docs/conventions.md -> Testing: no
test may call a real external service). Run this by hand against real
GEMINI_API_KEY credentials.

Usage:
    python scripts/smoke_test_gemini.py [path/to/image.jpg]

With no argument, generates a synthetic solid-color PNG (not real jewellery
-- expect is_jewelry: false, which still proves the API round-trip works).
"""

import asyncio
import io
import sys
import time

from PIL import Image

from app.services.classifier import get_classifier


def _synthetic_image() -> bytes:
    img = Image.new("RGB", (512, 512), color=(180, 160, 40))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


async def main(image_bytes: bytes) -> None:
    classifier = get_classifier()
    print("Calling the real Gemini API...")
    start = time.monotonic()
    result = await classifier.classify(image_bytes)
    elapsed_ms = round((time.monotonic() - start) * 1000, 1)

    print(f"\nLatency: {elapsed_ms}ms")
    print(f"is_jewelry: {result.is_jewelry}")
    print("predictions (descending confidence):")
    for p in result.predictions:
        print(f"  {p.jewelry_type.value:12s} confidence={p.confidence:.3f}")

    assert len(result.predictions) == 3, "Expected exactly 3 predictions"
    confidences = [p.confidence for p in result.predictions]
    assert confidences == sorted(confidences, reverse=True), "Not descending by confidence"

    print("\nSMOKE TEST PASSED.")


if __name__ == "__main__":
    if len(sys.argv) > 1:
        _path = sys.argv[1]
        print(f"Loading real image: {_path}")
        with open(_path, "rb") as _f:
            _image_bytes = _f.read()
    else:
        print("No image path given -- using a synthetic non-jewellery PNG.")
        print("(Expect is_jewelry: false -- this still proves the API call works.)")
        _image_bytes = _synthetic_image()

    asyncio.run(main(_image_bytes))
