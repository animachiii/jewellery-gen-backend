"""OAuth plumbing for the Higgsfield MCP prototype.

Standalone, manual-use only -- not part of `app`, not part of the automated
test suite, and deliberately kept out of app/providers/ (docs/conventions.md
-> Adapters: "no code outside app/providers/ may import a concrete
provider"). This talks to Higgsfield's *hosted MCP connector*
(https://mcp.higgsfield.ai/mcp), which is an OAuth-authenticated, credit-billed
surface built for an interactive agentic IDE (Claude Code, Codex, Cursor) --
not the production Higgsfield REST API app/providers/higgsfield.py targets.
See scripts/higgsfield_mcp_prototype/README.md for why this stays a
prototype and is never wired into the ARQ worker.

Runs a tiny localhost HTTP server to catch the OAuth redirect (the same
pattern the MCP Python SDK's own example auth client uses), and persists the
resulting token to a local file so re-runs after the first don't need
another browser round-trip.
"""

import asyncio
import json
import threading
import webbrowser
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from mcp.client.auth import OAuthClientProvider, TokenStorage
from mcp.shared.auth import OAuthClientInformationFull, OAuthClientMetadata, OAuthToken

TOKEN_FILE = Path(__file__).parent / ".higgsfield_mcp_token.json"
REDIRECT_PORT = 9821
REDIRECT_URI = f"http://127.0.0.1:{REDIRECT_PORT}/callback"


@dataclass
class _CallbackResult:
    code: str | None = None
    state: str | None = None
    error: str | None = None


class _CallbackHandler(BaseHTTPRequestHandler):
    """Minimal handler for the single OAuth redirect hit. Server is torn
    down right after the first request, so keeping this dumb is fine."""

    result: _CallbackResult

    def do_GET(self) -> None:  # noqa: N802 -- http.server's naming
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)
        self.result.code = params.get("code", [None])[0]
        self.result.state = params.get("state", [None])[0]
        self.result.error = params.get("error", [None])[0]

        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        body = (
            b"<html><body><h3>Higgsfield MCP auth complete.</h3>"
            b"You can close this tab and return to the terminal.</body></html>"
        )
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        pass  # silence the default per-request stderr logging


async def _wait_for_callback() -> tuple[str, str | None]:
    """Blocks (in a thread) for the single OAuth redirect, returns (code, state)."""
    result = _CallbackResult()

    class Handler(_CallbackHandler):
        pass

    Handler.result = result
    server = HTTPServer(("127.0.0.1", REDIRECT_PORT), Handler)

    def _serve_one() -> None:
        server.handle_request()

    thread = threading.Thread(target=_serve_one, daemon=True)
    thread.start()
    await asyncio.to_thread(thread.join)

    if result.error:
        raise RuntimeError(f"Higgsfield OAuth error: {result.error}")
    if not result.code:
        raise RuntimeError("Higgsfield OAuth callback received no 'code' parameter")
    return result.code, result.state


async def _redirect_handler(authorization_url: str) -> None:
    print("\nOpen this URL to authorize the Higgsfield MCP prototype:")
    print(f"  {authorization_url}\n")
    try:
        webbrowser.open(authorization_url)
    except Exception:
        pass  # headless environment -- the printed URL is enough


class FileTokenStorage(TokenStorage):
    """Persists the OAuth token + dynamically-registered client info to a
    local JSON file so re-running the prototype doesn't re-prompt for
    browser auth every time. Never commit this file -- see .gitignore."""

    def __init__(self, path: Path = TOKEN_FILE) -> None:
        self._path = path

    def _read(self) -> dict[str, object]:
        if not self._path.exists():
            return {}
        return json.loads(self._path.read_text())

    def _write(self, data: dict[str, object]) -> None:
        self._path.write_text(json.dumps(data, indent=2))
        self._path.chmod(0o600)

    async def get_tokens(self) -> OAuthToken | None:
        data = self._read()
        raw = data.get("tokens")
        return OAuthToken.model_validate(raw) if raw else None

    async def set_tokens(self, tokens: OAuthToken) -> None:
        data = self._read()
        data["tokens"] = tokens.model_dump(mode="json")
        self._write(data)

    async def get_client_info(self) -> OAuthClientInformationFull | None:
        data = self._read()
        raw = data.get("client_info")
        return OAuthClientInformationFull.model_validate(raw) if raw else None

    async def set_client_info(self, client_info: OAuthClientInformationFull) -> None:
        data = self._read()
        data["client_info"] = client_info.model_dump(mode="json")
        self._write(data)


def build_oauth_provider(server_url: str) -> OAuthClientProvider:
    return OAuthClientProvider(
        server_url=server_url,
        client_metadata=OAuthClientMetadata(
            redirect_uris=[REDIRECT_URI],  # type: ignore[arg-type]
            client_name="jewellery-gen-backend Higgsfield MCP prototype",
            grant_types=["authorization_code", "refresh_token"],
            response_types=["code"],
        ),
        storage=FileTokenStorage(),
        redirect_handler=_redirect_handler,
        callback_handler=_wait_for_callback,
    )
