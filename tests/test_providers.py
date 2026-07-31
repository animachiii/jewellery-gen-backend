import pytest

from app.providers.base import GenerationProvider, GenerationRequest
from app.providers.factory import get_provider
from app.providers.fake import FakeProvider
from app.providers.higgsfield import HiggsfieldProvider


def _req(token: str = "tok-1") -> GenerationRequest:
    return GenerationRequest(
        source_image=b"img",
        reference_image_url="https://example.com/ref.png",
        prompt="a ring",
        negative_prompt=None,
        params=None,
        submission_token=token,
    )


async def test_fake_provider_success_path_transitions_and_fetches_asset() -> None:
    provider = FakeProvider(latency_seconds=0, fail_mode="none")
    submission = await provider.submit(_req())
    assert submission.provider_job_id

    states = []
    for _ in range(4):
        status = await provider.poll(submission.provider_job_id)
        states.append(status.state)
        if status.state in ("succeeded", "failed"):
            break

    assert states[-1] == "succeeded"

    assets = await provider.fetch_assets(submission.provider_job_id)
    assert len(assets) == 1
    assert assets[0].mime == "image/png"
    assert isinstance(assets[0].data, bytes)
    assert len(assets[0].data) > 0


async def test_fake_provider_fetch_assets_is_deterministic() -> None:
    provider = FakeProvider(latency_seconds=0, fail_mode="none")
    a1 = (await provider.fetch_assets("job-1"))[0]
    a2 = (await provider.fetch_assets("job-1"))[0]
    assert a1.data == a2.data


async def test_fake_provider_submit_fail_mode_raises() -> None:
    provider = FakeProvider(latency_seconds=0, fail_mode="submit")
    with pytest.raises(Exception):  # noqa: B017
        await provider.submit(_req())


async def test_fake_provider_poll_fail_mode_returns_failed() -> None:
    provider = FakeProvider(latency_seconds=0, fail_mode="poll")
    submission = await provider.submit(_req())
    status = await provider.poll(submission.provider_job_id)
    assert status.state == "failed"


async def test_fake_provider_timeout_fail_mode_never_terminal() -> None:
    provider = FakeProvider(latency_seconds=0, fail_mode="timeout")
    submission = await provider.submit(_req())
    for _ in range(5):
        status = await provider.poll(submission.provider_job_id)
        assert status.state not in ("succeeded", "failed")


def test_get_provider_returns_higgsfield_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.config import settings

    monkeypatch.setattr(settings, "provider", "higgsfield")
    provider = get_provider()
    assert isinstance(provider, HiggsfieldProvider)
    assert provider.name == "higgsfield"


def test_get_provider_returns_fake_when_provider_setting_fake(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.config import settings

    monkeypatch.setattr(settings, "provider", "fake")
    provider = get_provider()
    assert isinstance(provider, FakeProvider)
    assert provider.name == "fake"


def test_get_provider_mock_override_always_returns_fake(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.config import settings

    monkeypatch.setattr(settings, "provider", "higgsfield")
    provider = get_provider(mock=True)
    assert isinstance(provider, FakeProvider)


def test_fake_provider_is_assignable_to_generation_provider() -> None:
    provider: GenerationProvider = FakeProvider()
    assert provider.name == "fake"


def test_higgsfield_provider_is_assignable_to_generation_provider() -> None:
    provider: GenerationProvider = HiggsfieldProvider()
    assert provider.name == "higgsfield"
