# Higgsfield MCP Prototype (manual, not production)

Standalone exploration tool for Higgsfield's **hosted MCP connector**
(`https://mcp.higgsfield.ai/mcp`) — not the direct REST API
`app/providers/higgsfield.py` targets, and not wired into the ARQ worker.

## Why this is a prototype, not a provider

The Higgsfield MCP connector is built for an interactive agentic IDE (Claude
Code, Codex, Cursor):

- **No API key.** Auth is OAuth — the first tool call prints an
  authorization URL, a human opens it in a browser and approves, and the
  resulting token persists to disk.
- **Billed against personal Higgsfield credits** (100 free for a new
  account, 150/month free tier), not a metered production API key.
- Positioned as a creative/dev tool, not a documented backend integration
  surface.

`app/worker/tasks.py` runs headlessly — an ARQ background worker with no
human present when a job submits. It cannot do an interactive "open this
URL, paste this code" step per generation (or even once at boot, reliably,
across worker restarts/redeploys). Routing production client traffic through
a personal free-credit connector also isn't the same risk/ToS posture as a
real metered API key. See the conversation that produced this directory for
the full reasoning; the short version is in `phases/phase-4-provider-integration.md`
→ "Manual Verification".

This directory exists only to let a human interactively confirm, with real
credits and real matrix prompts, that Higgsfield's models can actually
render this client's jewellery-catalogue style content — useful signal while
waiting on real production API credentials, nothing more.

## Setup

Use a **dedicated virtualenv**, never the project's `.venv`. Installing the
`mcp` SDK into the project's shared venv previously upgraded pydantic and
starlette and broke the project's pinned dependencies (`pyproject.toml`)
until reinstalled from scratch.

```bash
cd /path/to/jewellery-gen-backend
python3 -m venv .venv-mcp-prototype
source .venv-mcp-prototype/bin/activate
pip install mcp==1.28.1
```

## Usage

```bash
# See the real tool names/schemas Higgsfield's MCP server exposes today —
# run this first; explore.py's `generate` command guesses field names from
# docs/ai-integration.md's contract and needs adjusting to match reality.
python scripts/higgsfield_mcp_prototype/explore.py list-tools

# Submit one real generation using an actual matrix prompt + reference URL
python scripts/higgsfield_mcp_prototype/explore.py generate \
    --prompt "the verbatim prompt text from Sheet1" \
    --reference-url "https://drive.google.com/..."
```

The first run opens a browser tab for OAuth approval. The resulting token is
saved to `.higgsfield_mcp_token.json` in this directory (git-ignored, never
commit it) so subsequent runs don't re-prompt.

## What this is NOT

- Not imported by anything in `app/` — `docs/conventions.md`'s adapter
  boundary ("no code outside `app/providers/` may import a concrete
  provider") applies to `app/providers/higgsfield.py`'s real REST
  integration, not this script.
- Not part of the automated test suite (`docs/conventions.md`: "No test may
  call a real external service").
- Not a replacement for Phase 4's `HiggsfieldProvider` — that still targets
  Higgsfield's direct REST API with a server-side `HIGGSFIELD_API_KEY`, per
  `docs/ai-integration.md` §2's frozen `GenerationProvider` contract.
