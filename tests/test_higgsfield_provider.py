import httpx
import pytest

from app.providers.base import GenerationRequest
from app.providers.higgsfield import (
    HiggsfieldClient,
    HiggsfieldProvider,
    HiggsfieldRequestError,
    HiggsfieldSubmitRejectedError,
    _map_status,
)


def _req(token: str = "tok-1") -> GenerationRequest:
    return GenerationRequest(
        source_image=b"raw-bytes",
        reference_image_url="https://example.com/ref.png",
        prompt="a gold ring on a marble surface",
        negative_prompt="blurry",
        params={"seed": 42},
        submission_token=token,
    )


def _client(handler: httpx.MockTransport) -> HiggsfieldClient:
    return HiggsfieldClient(base_url="https://fake.higgsfield.test", api_key="k", transport=handler)


# --- _map_status --------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("pending", "pending"),
        ("queued", "pending"),
        ("running", "running"),
        ("processing", "running"),
        ("succeeded", "succeeded"),
        ("completed", "succeeded"),
        ("failed", "failed"),
        ("error", "failed"),
    ],
)
def test_map_status_known_values(raw: str, expected: str) -> None:
    assert _map_status(raw) == expected


def test_map_status_unknown_raises() -> None:
    with pytest.raises(HiggsfieldRequestError):
        _map_status("something_else")


# --- submit --------------------------------------------------------------


async def test_submit_success_sends_idempotency_key_and_parses_job_id() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["headers"] = dict(request.headers)
        captured["url"] = str(request.url)
        return httpx.Response(200, json={"id": "hf-job-123"})

    client = _client(httpx.MockTransport(handler))
    provider_job_id = await client.submit(_req(token="submission-abc"))

    assert provider_job_id == "hf-job-123"
    headers = captured["headers"]
    assert isinstance(headers, dict)
    assert headers["idempotency-key"] == "submission-abc"
    assert headers["authorization"] == "Bearer k"


async def test_submit_accepts_job_id_field_variant() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"job_id": "hf-job-456"})

    client = _client(httpx.MockTransport(handler))
    provider_job_id = await client.submit(_req())
    assert provider_job_id == "hf-job-456"


async def test_submit_4xx_raises_submit_rejected() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(422, text="bad request")

    client = _client(httpx.MockTransport(handler))
    with pytest.raises(HiggsfieldSubmitRejectedError) as exc_info:
        await client.submit(_req())
    assert exc_info.value.status == 422


async def test_submit_5xx_raises_request_error_not_submit_rejected() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="oops")

    client = _client(httpx.MockTransport(handler))
    with pytest.raises(HiggsfieldRequestError):
        await client.submit(_req())


async def test_submit_connection_error_raises_request_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    client = _client(httpx.MockTransport(handler))
    with pytest.raises(HiggsfieldRequestError):
        await client.submit(_req())


async def test_submit_missing_id_field_raises_request_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"unexpected": "shape"})

    client = _client(httpx.MockTransport(handler))
    with pytest.raises(HiggsfieldRequestError):
        await client.submit(_req())


# --- poll ------------------------------------------------------------------


async def test_poll_maps_terminal_succeeded_status() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": "succeeded", "progress": 1.0, "error": None})

    client = _client(httpx.MockTransport(handler))
    status = await client.poll("hf-job-123")
    assert status.state == "succeeded"
    assert status.progress == 1.0
    assert status.error is None


async def test_poll_network_error_raises_not_synthetic_failed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    client = _client(httpx.MockTransport(handler))
    with pytest.raises(HiggsfieldRequestError):
        await client.poll("hf-job-123")


async def test_poll_missing_status_field_raises() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={})

    client = _client(httpx.MockTransport(handler))
    with pytest.raises(HiggsfieldRequestError):
        await client.poll("hf-job-123")


# --- fetch_assets ------------------------------------------------------------


async def test_fetch_assets_downloads_and_wraps_bytes_with_mime() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/assets"):
            return httpx.Response(200, json={"assets": ["https://cdn.test/a.png"]})
        return httpx.Response(200, content=b"\x89PNG-bytes", headers={"content-type": "image/png"})

    provider = HiggsfieldProvider(_client(httpx.MockTransport(handler)))
    assets = await provider.fetch_assets("hf-job-123")

    assert len(assets) == 1
    assert assets[0].data == b"\x89PNG-bytes"
    assert assets[0].mime == "image/png"


async def test_fetch_assets_missing_urls_raises() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={})

    client = _client(httpx.MockTransport(handler))
    with pytest.raises(HiggsfieldRequestError):
        await client.fetch_asset_urls("hf-job-123")


async def test_download_asset_error_status_raises() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    client = _client(httpx.MockTransport(handler))
    with pytest.raises(HiggsfieldRequestError):
        await client.download_asset("https://cdn.test/missing.png")


# --- HiggsfieldProvider (Protocol-level) ------------------------------------


async def test_provider_submit_end_to_end() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"id": "hf-job-789"})

    provider = HiggsfieldProvider(_client(httpx.MockTransport(handler)))
    submission = await provider.submit(_req())
    assert submission.provider_job_id == "hf-job-789"


async def test_provider_submit_never_logs_source_image_bytes(
    caplog: pytest.LogCaptureFixture,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"id": "hf-job-1"})

    provider = HiggsfieldProvider(_client(httpx.MockTransport(handler)))
    await provider.submit(_req())

    log_text = caplog.text
    assert "raw-bytes" not in log_text


def test_default_client_uses_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.config import settings

    monkeypatch.setattr(settings, "higgsfield_api_key", "real-key")
    monkeypatch.setattr(settings, "higgsfield_base_url", "https://real.higgsfield.test")

    provider = HiggsfieldProvider()
    assert provider._client._base_url == "https://real.higgsfield.test"
    assert provider._client._headers["Authorization"] == "Bearer real-key"
