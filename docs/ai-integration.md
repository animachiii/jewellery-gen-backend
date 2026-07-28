# AI Integration

Two AI calls exist in v1. Both sit behind adapters. Nothing else in the system may call a model directly.

| # | Purpose | Model | Trigger | Cost class |
|---|---------|-------|---------|-----------|
| 1 | Jewellery type classification | Gemini 3.1 Flash Lite | `jewelry_type` omitted on submit | cheap, retryable |
| 2 | Image generation | Higgsfield (abstracted) | Every non-mock job | **expensive, never auto-retried** |

**There is no third AI call.** In particular, no model rewrites, expands, or validates prompts — see business rule R7.

---

## 1. Classification — Gemini 3.1 Flash Lite

**Module:** `app/services/classifier.py`
**Trigger:** worker `classify` stage, only when `jewelry_type_requested` is null. A client-supplied type skips this entirely (R11).

### Model note (Phase 3 live smoke test, superseding the original Flash-vs-Lite reasoning below)
The originally-planned `gemini-2.5-flash` and `gemini-2.5-flash-lite` both return `404 NOT_FOUND` — *"no longer available to new users"* — on this project's API key, confirmed live. Tested and confirmed working on this account: `gemini-flash-latest`, `gemini-flash-lite-latest`, and `gemini-3.1-flash-lite`. `GEMINI_MODEL` is set to `gemini-3.1-flash-lite`. `gemini-3-flash-lite` and `gemini-2.5-flash-lite-preview` do **not** exist (404, not an access restriction). Re-check availability if this ever needs revisiting — Google's model lineup and per-key access change over time, and `GEMINI_MODEL` is a plain env var, not a code change, to swap.

### Why Flash, not Flash Lite (original reasoning, now moot for this account)
Classification cost is a rounding error next to generation cost, and a misclassification wastes a full paid generation *plus* client trust — full Flash was the intended safer default. That model isn't available to this API key at all, so the choice is between two Lite variants that *are* available, not a deliberate cost/accuracy tradeoff. Phase 6 still benchmarks classification accuracy against labelled client photos; the threshold (`CLASSIFIER_CONFIDENCE_THRESHOLD`) is the lever to compensate if Lite proves less reliable than full Flash would have been.

### Input
- Source image bytes (the original upload, not a re-encode)
- No prompt matrix context — the classifier must not be biased by which combinations exist

### Output — structured, schema-enforced
Use Gemini's structured output with a response schema. Do not parse free text.

```json
{
  "is_jewelry": true,
  "predictions": [
    { "jewelry_type": "ANKLET", "confidence": 0.94 },
    { "jewelry_type": "BRACELET", "confidence": 0.04 },
    { "jewelry_type": "CHAIN",   "confidence": 0.02 }
  ],
  "notes": "Fine chain with charm drops, photographed flat."
}
```

- `jewelry_type` is constrained to the `JewelryType` enum in `docs/schema.md` §1. No free-form strings.
- Exactly 3 predictions, descending by confidence.
- `notes` is for debugging only. It is never shown to the client and never influences generation.

### System instruction (verbatim)

```
You are a jewellery classification system for a product catalogue pipeline.

Given a product photograph, identify the single jewellery type it depicts.

Rules:
- Choose only from the provided enum. Never invent a type.
- If the image contains no jewellery, set is_jewelry to false.
- If multiple pieces are present, classify the most prominent one.
- Judge by the physical form of the piece, not by how it is worn or styled.
- Confidence must reflect genuine uncertainty. Do not default to high
  confidence. Visually similar categories (anklet vs bracelet, chain vs
  necklace, pendant vs necklace) should receive proportionally split scores
  when the image does not clearly distinguish them.

Return exactly three predictions ordered by descending confidence.
```

The anklet/bracelet ambiguity is called out deliberately — a flat product shot frequently cannot distinguish them, and an over-confident wrong answer is the failure mode that costs a generation.

### Decision logic
```
is_jewelry == false                → failed, NOT_JEWELRY
top.confidence >= 0.75             → jewelry_type_final = top, type_source = CLASSIFIED
top.confidence <  0.75             → needs_input, candidate_types = all 3, LOW_CONFIDENCE
```

Threshold is `CLASSIFIER_CONFIDENCE_THRESHOLD`, tunable without a deploy.

### Failure handling
| Condition | Behaviour |
|-----------|-----------|
| API error / timeout (15s) | Retry 3× (2s/8s/30s) → `failed`, `CLASSIFIER_ERROR` |
| Malformed or schema-violating output | Treat as an API error; retry |
| Type not in enum (shouldn't happen with schema) | Discard prediction; if none valid → `needs_input` |
| Safety block | `failed`, `NOT_JEWELRY`, message notes the block |

Persist `confidence` and the full `candidate_types` on the job **always** — including on success. It's the only way to tune the threshold later against real data.

---

## 2. Generation — Higgsfield (behind `GenerationProvider`)

**Module:** `app/providers/higgsfield.py`
**Interface:** `app/providers/base.py`
**Trigger:** worker `submit` stage, after the matrix is resolved.

### Interface — the only contract the orchestrator knows

```python
class GenerationProvider(Protocol):
    name: str

    async def submit(self, req: GenerationRequest) -> ProviderSubmission: ...
    async def poll(self, provider_job_id: str) -> ProviderStatus: ...
    async def fetch_assets(self, provider_job_id: str) -> list[ProviderAsset]: ...
```

```python
@dataclass
class GenerationRequest:
    source_image: bytes
    reference_image_url: str
    prompt: str                      # verbatim from the matrix
    negative_prompt: str | None
    params: dict | None              # passthrough from matrix column F
    submission_token: str            # idempotency handle — see R2

@dataclass
class ProviderSubmission:
    provider_job_id: str

@dataclass
class ProviderStatus:
    state: Literal["pending", "running", "succeeded", "failed"]
    progress: float | None
    error: str | None
```

**No orchestration code may import `higgsfield` directly.** Providers are resolved from the `PROVIDER` env var through a factory. This is what makes the Nano Banana Pro swap a one-file change.

### Input to the provider
- `source_image` — the client's uploaded photo
- `reference_image_url` — from matrix column D, snapshotted onto the job
- `prompt` — from matrix column C, **verbatim** (R7)
- `submission_token` — passed as the provider's idempotency key or request metadata if supported. If Higgsfield supports neither, record that fact here explicitly, because it means orphaned submits can only ever be resolved manually.

### Output
One or more generated images. Downloaded in the `storing` stage, pushed through the `StorageAdapter`, and recorded as `asset_refs` in index order.

### Failure handling
| Condition | Behaviour |
|-----------|-----------|
| Submit rejected with a definite 4xx (no charge) | `failed`, `PROVIDER_SUBMIT_FAILED` |
| Submit times out / connection lost / worker dies | `needs_review`, `ORPHANED_SUBMIT`. **Never retried** (R1) |
| Poll returns `failed` | Retry the *whole job* only if `attempt_count < 1`; else `failed`, `PROVIDER_ERROR` |
| Poll exceeds `deadline_at` | `failed`, `PROVIDER_TIMEOUT` via the sweeper (R12) |
| Asset download fails | Retry 3×; then `failed`, `STORAGE_ERROR` |

Poll interval: 5s, capped by `deadline_at`. Polling is free — retry it freely. Submitting is not.

---

## 3. FakeProvider

**Module:** `app/providers/fake.py`
**Written in the same phase as the real provider, not after.**

Implements the identical protocol: ~10s simulated latency, deterministic placeholder asset, and configurable failure injection (`FAKE_FAIL_MODE=submit|poll|timeout|none`) so error paths are testable without spending money.

Used by:
- `mock=true` requests (R6)
- the entire automated test suite
- showcase-UI development

It must traverse the real state machine. A mock job that skips states is worthless as a test.

---

## 4. Cost & Safety Controls

| Control | Value | Enforced in |
|---------|-------|-------------|
| Worker concurrency | `WORKER_CONCURRENCY` (4) | ARQ settings — must not exceed the provider's cap |
| Daily generation cap | `DAILY_GENERATION_CAP` (200) | `app/services/budget.py`, at submit |
| Job deadline | 900s | Sweeper cron |
| Classifier timeout | 15s | Gemini client |
| Dedupe window | 24h | `app/services/dedupe.py` |

**Never log:** image bytes, base64 payloads, API keys, or full service-account JSON. Log `content_hash`, `job_id`, model name, latency, token/asset counts.

**Always log per AI call:** `job_id`, model, latency_ms, outcome, and — for classification — the top confidence. This is the dataset that tunes the threshold.
