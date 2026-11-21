# main.py
import json
import os
import jwt
from fastapi import Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from google.adk.cli.fast_api import get_fast_api_app


def _unauthorized(detail: str) -> JSONResponse:
    # Return instead of raising: an HTTPException raised from an HTTP
    # middleware propagates past Starlette's exception handler and becomes
    # a 500 instead of the intended 401.
    return JSONResponse(status_code=401, content={"detail": detail})

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

app = get_fast_api_app(
    agents_dir=".",
    web=False,
)

jwks_client = jwt.PyJWKClient(KEYCLOAK_JWKS_URL) if REQUIRE_JWT_SIGNATURE else None


def _extract_username(request: Request, token: str) -> str:
    header_user = (
        request.headers.get("X-Forwarded-Preferred-Username")
        or request.headers.get("X-Forwarded-User")
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


@app.middleware("http")
async def inject_oauth2_proxy_auth_to_adk(request: Request, call_next):
    if request.method == "OPTIONS" or request.url.path in ["/docs", "/openapi.json"]:
        return await call_next(request)

    token = (request.headers.get("X-Forwarded-Access-Token") or "").strip()
    if not token:
        return _unauthorized(
            "Missing X-Forwarded-Access-Token from OAuth2 Proxy"
        )

    if REQUIRE_JWT_SIGNATURE and jwks_client is not None:
        try:
            signing_key = jwks_client.get_signing_key_from_jwt(token)
            jwt.decode(
                token,
                signing_key.key,
                algorithms=["RS256"],
                audience=KEYCLOAK_AUDIENCE,
                issuer=KEYCLOAK_ISSUER,
            )
        except Exception as e:
            return _unauthorized(f"Invalid Access Token: {str(e)}")

    username = _extract_username(request, token)
    if not username:
        return _unauthorized(
            "Missing username in OAuth2 Proxy headers or token claims"
        )

    if request.method == "POST" and request.url.path in ["/run", "/run_sse"]:
        body_bytes = await request.body()
        if body_bytes:
            body_json = json.loads(body_bytes)
            body_json["user_id"] = username
            state_delta = body_json.get("state_delta") or {}
            state_delta["keycloak_token"] = token
            state_delta["username"] = username
            body_json["state_delta"] = state_delta

            new_body = json.dumps(body_json).encode("utf-8")

            async def receive():
                return {"type": "http.request", "body": new_body, "more_body": False}

            request._receive = receive

    return await call_next(request)


# Registered last so CORS is the outermost middleware: responses returned
# directly by the auth middleware above still get CORS headers.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:4180", "http://localhost:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)