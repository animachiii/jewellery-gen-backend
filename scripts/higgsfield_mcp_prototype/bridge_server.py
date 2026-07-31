"""Local MCP-to-REST bridge for Higgsfield, testing/showcase only.

Runs in this directory's OWN virtualenv (.venv-mcp-prototype), never the
project's .venv -- same reasoning as explore.py: the `mcp` SDK needs a newer
pydantic than app/'s pinned pyproject.toml allows, so it must never share an
environment with the main app.

This process is the ONLY thing in this whole setup that speaks MCP. It
exposes a tiny REST API that app/providers/higgsfield_mcp_bridge.py (running
in the main app/worker venv, plain httpx, no new app dependency) calls
instead. Delete both this file and app/providers/higgsfield_mcp_bridge.py
(plus its factory branch) once a real HIGGSFIELD_API_KEY makes
app/providers/higgsfield.py usable -- this bridge is not the intended
production path (see phases/phase-4-provider-integration.md and the
conversation that led to this file for the full reasoning: OAuth is
interactive, billing is against a personal account's credits, and observed
latency for one image was ~15 minutes against a 900s JOB_DEADLINE_SECONDS).

Auth: reuses the OAuth token already persisted by oauth.py's
FileTokenStorage (.higgsfield_mcp_token.json in this directory) -- no
re-prompt as long as that file exists and the token/refresh token stays
valid, same as explore.py.

Run:
    source .venv-mcp-prototype/bin/activate
    uvicorn bridge_server:app --host 127.0.0.1 --port 8799
"""

import re
from contextlib import asynccontextmanager
from datetime import timedelta
from typing import Any

import httpx
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client
from pydantic import BaseModel

from oauth import build_oauth_provider

HIGGSFIELD_MCP_URL = "https://mcp.higgsfield.ai/mcp"
DEFAULT_MODEL = "marketing_studio_image"
UUID_RE = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"


def _all_text(content: list[Any]) -> str:
    return "\n".join(b.text for b in content if getattr(b, "type", None) == "text")


@asynccontextmanager
async def lifespan(app: FastAPI):  # type: ignore[no-untyped-def]
    # One MCP session, reused across requests -- avoids a fresh OAuth-token
    # refresh + handshake per call. If the session dies, the next request's
    # exception will surface as a 502; restart the process to recover.
    oauth_provider = build_oauth_provider(HIGGSFIELD_MCP_URL)
    async with streamablehttp_client(
        HIGGSFIELD_MCP_URL, auth=oauth_provider, timeout=timedelta(seconds=60)
    ) as (read, write, _get_session_id):
        async with ClientSession(read, write) as session:
            await session.initialize()
            app.state.mcp_session = session
            yield


app = FastAPI(lifespan=lifespan)


class SubmitResponse(BaseModel):
    provider_job_id: str


class StatusResponse(BaseModel):
    state: str
    progress: float | None = None
    error: str | None = None


class AssetsResponse(BaseModel):
    urls: list[str]


@app.post("/submit", response_model=SubmitResponse)
async def submit(
    prompt: str = Form(...),
    negative_prompt: str | None = Form(None),
    model: str = Form(DEFAULT_MODEL),
    image: UploadFile = File(...),
) -> SubmitResponse:
    session: ClientSession = app.state.mcp_session

    upload = await session.call_tool(
        "media_upload",
        arguments={"filename": image.filename or "source.jpg", "content_type": image.content_type},
    )
    upload_text = _all_text(upload.content)
    url_match = re.search(r"https://upload\.higgsfield\.ai/\S+", upload_text)
    id_match = re.search(UUID_RE, upload_text)
    if not url_match or not id_match:
        raise HTTPException(502, f"media_upload response unparseable: {upload_text[:500]}")
    upload_url = url_match.group(0).rstrip("'.")
    media_id = id_match.group(0)

    data = await image.read()
    async with httpx.AsyncClient() as client:
        put_resp = await client.put(
            upload_url,
            content=data,
            headers={"Content-Type": image.content_type or "image/jpeg"},
            timeout=60.0,
        )
        put_resp.raise_for_status()

    confirm = await session.call_tool("media_confirm", arguments={"type": "image", "media_id": media_id})
    if "confirmed" not in _all_text(confirm.content).lower():
        raise HTTPException(502, f"media_confirm did not confirm: {_all_text(confirm.content)[:500]}")

    params: dict[str, object] = {
        "model": model,
        "prompt": prompt,
        "count": 1,
        "medias": [{"value": media_id, "role": "image"}],
    }
    if negative_prompt:
        # PLACEHOLDER -- marketing_studio_image's real negative-prompt field
        # name (if any) hasn't been confirmed; passed through best-effort.
        params["negative_prompt"] = negative_prompt

    gen = await session.call_tool("generate_image", arguments={"params": params})
    gen_text = _all_text(gen.content)
    if gen_text.lower().startswith("error"):
        raise HTTPException(502, f"generate_image rejected: {gen_text[:500]}")

    job_id_match = re.search(UUID_RE, gen_text)
    if not job_id_match:
        raise HTTPException(502, f"No job id in generate_image response: {gen_text[:500]}")

    return SubmitResponse(provider_job_id=job_id_match.group(0))


@app.get("/status/{job_id}", response_model=StatusResponse)
async def status(job_id: str) -> StatusResponse:
    session: ClientSession = app.state.mcp_session
    result = await session.call_tool("job_status", arguments={"jobId": job_id, "raw_data": True})
    text = _all_text(result.content)

    match = re.search(r"status:\s*(\w+)", text)
    if not match:
        raise HTTPException(502, f"Unparseable job_status response: {text[:500]}")
    raw_status = match.group(1).lower()

    mapping = {
        "pending": "pending",
        "queued": "pending",
        "in_progress": "running",
        "processing": "running",
        "completed": "succeeded",
        "succeeded": "succeeded",
        "failed": "failed",
        "error": "failed",
        "ip_detected": "failed",
        # Higgsfield's content-moderation terminal states -- found via manual
        # testing (a real generation returned "nsfw"). These are genuine
        # terminal outcomes, not something worth polling for; the previous
        # missing mapping made every subsequent poll raise
        # "Unrecognised job_status value: 'nsfw'" forever until deadline_at,
        # instead of the job failing cleanly.
        "nsfw": "failed",
        "content_flagged": "failed",
        "moderation_failed": "failed",
    }
    state = mapping.get(raw_status)
    if state is None:
        raise HTTPException(502, f"Unrecognised job_status value: {raw_status!r}")

    return StatusResponse(state=state, error=raw_status if state == "failed" else None)


@app.get("/assets/{job_id}", response_model=AssetsResponse)
async def assets(job_id: str) -> AssetsResponse:
    session: ClientSession = app.state.mcp_session
    result = await session.call_tool("job_status", arguments={"jobId": job_id})
    text = _all_text(result.content)
    urls = re.findall(r"https://\S+\.(?:png|jpg|jpeg|webp)", text)
    if not urls:
        raise HTTPException(502, f"No asset URLs found in job_status response: {text[:500]}")
    return AssetsResponse(urls=urls)


@app.get("/healthz")
async def healthz() -> dict[str, bool]:
    return {"ok": True}
