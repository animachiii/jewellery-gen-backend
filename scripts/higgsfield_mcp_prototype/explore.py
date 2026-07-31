"""Standalone Higgsfield MCP exploration script.

NOT part of the `app` package, NOT part of the automated test suite, NOT
wired into app/providers/higgsfield.py. See README.md in this directory for
why this stays a manual prototype: the hosted Higgsfield MCP connector
(https://mcp.higgsfield.ai/mcp) is OAuth-authenticated (interactive browser
approval, no API key) and billed against personal Higgsfield credits -- a
different model from the direct REST API app/providers/higgsfield.py targets,
and not something the headless ARQ worker can drive unattended.

Run from a DEDICATED virtualenv, never the project's .venv -- installing the
`mcp` SDK into the project's shared venv previously pulled in newer
pydantic/starlette and broke the project's pinned dependencies (had to be
reinstalled from pyproject.toml to recover). From the repo root:

    python3 -m venv .venv-mcp-prototype   # if not already created
    source .venv-mcp-prototype/bin/activate
    pip install mcp==1.28.1

    python scripts/higgsfield_mcp_prototype/explore.py list-tools
    python scripts/higgsfield_mcp_prototype/explore.py generate \\
        --prompt "..." --reference-url "https://drive.google.com/..."
"""

import argparse
import asyncio
import json
import sys
from collections.abc import Awaitable, Callable
from datetime import timedelta
from typing import TypeVar

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

from oauth import build_oauth_provider

HIGGSFIELD_MCP_URL = "https://mcp.higgsfield.ai/mcp"

T = TypeVar("T")


async def _connect_and_run(fn: Callable[[ClientSession], Awaitable[T]]) -> T:
    oauth_provider = build_oauth_provider(HIGGSFIELD_MCP_URL)
    async with streamablehttp_client(
        HIGGSFIELD_MCP_URL, auth=oauth_provider, timeout=timedelta(seconds=60)
    ) as (read, write, _get_session_id):
        async with ClientSession(read, write) as session:
            await session.initialize()
            return await fn(session)


async def list_tools() -> None:
    async def _run(session: ClientSession) -> None:
        result = await session.list_tools()
        for tool in result.tools:
            print(f"\n--- {tool.name} ---")
            print(tool.description or "(no description)")
            print(json.dumps(tool.inputSchema, indent=2))

    await _connect_and_run(_run)


async def generate(prompt: str, reference_url: str | None, model: str | None) -> None:
    """Submits one real generation request against the client's actual
    matrix prompt/reference URL, to sanity-check that Higgsfield can
    actually render this client's jewellery-catalogue style prompts.

    Argument names are guesses at the real tool's input schema -- run
    `list-tools` first and adjust if the printed inputSchema disagrees."""

    async def _run(session: ClientSession) -> None:
        tools = (await session.list_tools()).tools
        image_tools = [t for t in tools if "image" in t.name.lower()]
        if not image_tools:
            print("No image-generation tool found. Full tool list:")
            for t in tools:
                print(f"  - {t.name}")
            return

        tool = image_tools[0]
        print(f"Using tool: {tool.name}")
        args: dict[str, object] = {"prompt": prompt}
        if reference_url:
            args["reference_image_url"] = reference_url
        if model:
            args["model"] = model

        result = await session.call_tool(tool.name, arguments=args)
        for block in result.content:
            if block.type == "text":
                print(block.text)
            else:
                print(f"[{block.type} content block]")

    await _connect_and_run(_run)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("list-tools", help="List every tool the Higgsfield MCP server exposes")

    gen = sub.add_parser("generate", help="Submit one real generation request")
    gen.add_argument("--prompt", required=True, help="Verbatim prompt text (e.g. from Sheet1)")
    gen.add_argument("--reference-url", default=None, help="Reference image URL from the matrix")
    gen.add_argument("--model", default=None, help="Optional model override")

    args = parser.parse_args()

    if args.command == "list-tools":
        asyncio.run(list_tools())
    elif args.command == "generate":
        asyncio.run(generate(args.prompt, args.reference_url, args.model))
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
