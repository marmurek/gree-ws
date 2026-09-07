"""Shared token authorisation.

Off by default. When enabled, every endpoint except the ones needed to inspect
or monitor the service requires the configured token.
"""

import logging
import secrets
from typing import Optional

from fastapi import Request, WebSocket
from fastapi.responses import JSONResponse

from gree_ws.config import AuthSettings

logger = logging.getLogger(__name__)

# Reachable without a token even when authorisation is on: the health probe so
# an orchestrator can watch the service, and the schema, which describes the API
# without exposing anything about the devices behind it.
PUBLIC_PATHS = frozenset({"/health", "/docs", "/docs/oauth2-redirect", "/redoc", "/openapi.json"})

# Closing code for a WebSocket the server refuses to serve.
WS_POLICY_VIOLATION = 1008


def presented_token(request: Request) -> Optional[str]:
    """The token a client offered, whichever way it chose to offer it"""
    header = request.headers.get("authorization", "")
    if header.lower().startswith("bearer "):
        return header[len("bearer ") :].strip()

    return request.headers.get("x-api-key")


def websocket_token(websocket: WebSocket) -> Optional[str]:
    """The token a WebSocket client offered.

    Browsers cannot set headers on a WebSocket, so the query parameter is the
    only option there; other clients should prefer the header.
    """
    header = websocket.headers.get("authorization", "")
    if header.lower().startswith("bearer "):
        return header[len("bearer ") :].strip()

    return websocket.headers.get("x-api-key") or websocket.query_params.get("token")


def is_valid(auth: AuthSettings, token: Optional[str]) -> bool:
    """Is this the configured token, compared without leaking its length"""
    if not auth.enabled:
        return True
    if not token:
        return False
    return secrets.compare_digest(token, auth.token)


def install(app, auth: AuthSettings) -> None:
    """Require the token on every request, unless the path is public"""
    if not auth.enabled:
        logger.warning("Authorisation is disabled: anyone who can reach the port can control the devices")
        return

    logger.info("Authorisation is enabled")

    @app.middleware("http")
    async def require_token(request: Request, call_next):
        """Reject a request that does not carry the configured token"""
        if request.url.path in PUBLIC_PATHS:
            return await call_next(request)

        if not is_valid(auth, presented_token(request)):
            return JSONResponse(status_code=401, content={"detail": "Missing or invalid token"})

        return await call_next(request)
