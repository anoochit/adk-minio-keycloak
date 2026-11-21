# main.py
import json
import os
import re

import jwt
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from google.adk.cli.fast_api import get_fast_api_app
from starlette.types import ASGIApp, Message, Receive, Scope, Send

REQUIRE_JWT_SIGNATURE = (
    os.getenv("REQUIRE_JWT_SIGNATURE", "false").lower() == "true"
)
KEYCLOAK_JWKS_URL = os.getenv(
    "KEYCLOAK_JWKS_URL",
    "http://keycloak:8080/realms/my-realm/protocol/openid-connect/certs",
)
KEYCLOAK_AUDIENCE = os.getenv("KEYCLOAK_AUDIENCE", "adk-minio-client")
KEYCLOAK_ISSUER = os.getenv(
    "KEYCLOAK_ISSUER",
    "http://keycloak:8080/realms/my-realm",
)

# Origins the React app is served from (oauth2-proxy / Vite dev server).
# ADK's own origin+host gate rejects the request unless these are declared
# here: nginx used to forward Host without the port, so ADK's same-origin
# fallback compared Origin "http://localhost:4180" against "http://localhost".
ALLOWED_ORIGINS = [
    origin.strip()
    for origin in os.getenv(
        "ALLOWED_ORIGINS",
        "http://localhost:4180,http://127.0.0.1:4180,"
        "http://localhost:5173,http://127.0.0.1:5173",
    ).split(",")
    if origin.strip()
]

jwks_client = jwt.PyJWKClient(KEYCLOAK_JWKS_URL) if REQUIRE_JWT_SIGNATURE else None

# This ADK build never reads GOOGLE_GENAI_API_KEY; the google-genai client only
# resolves GOOGLE_API_KEY / GEMINI_API_KEY, so bridge the configured key over
# (without clobbering an explicit value) or every model call fails with
# "No API key was provided".
if os.getenv("GOOGLE_GENAI_API_KEY") and not (
    os.getenv("GOOGLE_API_KEY") or os.getenv("GEMINI_API_KEY")
):
    os.environ["GOOGLE_API_KEY"] = os.environ["GOOGLE_GENAI_API_KEY"]


def _unauthorized(detail: str) -> JSONResponse:
    # Returned rather than raised: an HTTPException raised from inside an HTTP
    # middleware bypasses Starlette's exception handler and surfaces as a 500
    # instead of the intended 401.
    return JSONResponse(status_code=401, content={"detail": detail})


def _headers_dict(scope: Scope) -> dict[str, str]:
    return {
        key.decode("latin-1").lower(): value.decode("latin-1")
        for key, value in scope.get("headers", [])
    }


def _token_error(token: str) -> str | None:
    """Validate the JWT signature when enabled; return an error detail or None."""
    if not (REQUIRE_JWT_SIGNATURE and jwks_client is not None):
        return None
    try:
        signing_key = jwks_client.get_signing_key_from_jwt(token)
        jwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256"],
            audience=KEYCLOAK_AUDIENCE,
            issuer=KEYCLOAK_ISSUER,
        )
        return None
    except Exception as e:
        return f"Invalid Access Token: {str(e)}"


def _extract_username(headers: dict[str, str], token: str) -> str:
    header_user = (
        headers.get("x-forwarded-preferred-username")
        or headers.get("x-forwarded-user")
        or ""
    ).strip()
    if header_user:
        return header_user

    # Fallback: decode claims from JWT without verifying signature if not enabled
    try:
        claims = jwt.decode(token, options={"verify_signature": False})
        return str(claims.get("preferred_username") or claims.get("sub") or "").strip()
    except Exception:
        return ""


async def _read_request_body(receive: Receive) -> bytes | None:
    """Drain the request body, or None if the client disconnected first."""
    chunks: list[bytes] = []
    while True:
        message = await receive()
        if message["type"] == "http.disconnect":
            return None
        if message["type"] == "http.request":
            chunks.append(message.get("body", b""))
            if not message.get("more_body", False):
                return b"".join(chunks)


def _single_body_receive(body: bytes, fallback: Receive) -> Receive:
    """Replay one fixed body, then defer to the real channel.

    Later reads must *not* return ``http.disconnect`` eagerly: a streaming
    response (``/run_sse``) waits for a disconnect concurrently, and an
    immediate one cancels the stream before the model ever replies.
    ``fallback`` has already had its body drained, so it only ever yields the
    real disconnect.
    """
    sent = False

    async def receive() -> Message:
        nonlocal sent
        if not sent:
            sent = True
            return {"type": "http.request", "body": body, "more_body": False}
        return await fallback()

    return receive


# /apps/{app}/users/{user}[/sessions[/{id}]] — the user segment we re-pin to
# the authenticated caller.
_USER_SCOPED_PATH = re.compile(
    r"^(?P<prefix>/apps/[^/]+/users/)(?P<user>[^/]+)(?P<suffix>/.*)?$"
)


class AuthInjectMiddleware:
    """Verify the OAuth2-Proxy identity and bind it onto the ADK request.

    Deliberately raw ASGI middleware rather than BaseHTTPMiddleware: there,
    calling ``request.body()`` caches the original bytes and Starlette forwards
    *those* downstream, so rewriting ``request._receive`` never changes what the
    route sees and instead corrupts the disconnect handshake
    (``RuntimeError: Unexpected message received: http.request`` on /run_sse).
    """

    SKIP_PATHS = frozenset({"/docs", "/openapi.json"})
    REWRITTEN_PATHS = frozenset({"/run", "/run_sse"})

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        if scope.get("method") == "OPTIONS" or path in self.SKIP_PATHS:
            await self.app(scope, receive, send)
            return

        headers = _headers_dict(scope)
        token = (headers.get("x-forwarded-access-token") or "").strip()
        if not token:
            await _unauthorized(
                "Missing X-Forwarded-Access-Token from OAuth2 Proxy"
            )(scope, receive, send)
            return

        token_error = _token_error(token)
        if token_error:
            await _unauthorized(token_error)(scope, receive, send)
            return

        username = _extract_username(headers, token)
        if not username:
            await _unauthorized(
                "Missing username in OAuth2 Proxy headers or token claims"
            )(scope, receive, send)
            return

        # Force the authenticated identity onto the user segment of
        # /apps/{app}/users/{user}/... as well. The React client sends a fixed
        # placeholder ("proxy-user") while /run_sse's user_id is overridden
        # below, so without this the session created here would belong to a
        # different user than the one used to run it.
        match = _USER_SCOPED_PATH.match(path)
        if match and match.group("user") != username:
            path = f"{match.group('prefix')}{username}{match.group('suffix') or ''}"
            scope["path"] = path
            scope["raw_path"] = path.encode("utf-8")

        if scope.get("method") != "POST" or path not in self.REWRITTEN_PATHS:
            await self.app(scope, receive, send)
            return

        raw_body = await _read_request_body(receive)
        if raw_body is None:  # client disconnected before sending a body
            return

        payload = None
        if raw_body:
            try:
                decoded = json.loads(raw_body)
                payload = decoded if isinstance(decoded, dict) else None
            except json.JSONDecodeError:
                payload = None

        if payload is None:
            # Nothing we can safely rewrite; pass the original body through.
            await self.app(scope, _single_body_receive(raw_body, receive), send)
            return

        # Force the authenticated identity: a caller must not be able to act as
        # another user_id, and the agent receives the token/user via state.
        payload["user_id"] = username
        state_delta = payload.get("state_delta")
        if not isinstance(state_delta, dict):
            state_delta = {}
        state_delta["keycloak_token"] = token
        state_delta["username"] = username
        payload["state_delta"] = state_delta

        new_body = json.dumps(payload).encode("utf-8")
        await self.app(scope, _single_body_receive(new_body, receive), send)


app = get_fast_api_app(
    agents_dir=".",
    allow_origins=ALLOWED_ORIGINS,
    web=False,
)

# Starlette's add_middleware() prepends, so the last one added is outermost.
# CORS must wrap the auth middleware to stamp headers on its 401 responses.
app.add_middleware(AuthInjectMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
