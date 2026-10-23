# Copyright © 2025, SAS Institute Inc., Cary, NC, USA.  All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Authentication provider for the HTTP-mode server.

Split out of :mod:`sas_mcp_server.config`, which should read as a settings
module rather than carry behaviour. The proxy takes its one policy switch as a
constructor argument instead of reading a module global, so it depends on
nothing in ``config`` — that keeps the import one-directional (``config``
imports ``auth``) and makes the class testable on its own.
"""

import json
import logging
from typing import Any

from fastmcp.server.auth import OAuthProxy
from mcp.server.auth.provider import AccessToken
from mcp.shared.auth import OAuthClientInformationFull
from starlette.routing import Route

logger = logging.getLogger(__name__)

# The token-endpoint authentication methods that involve a client secret.
SECRET_AUTH_METHODS = ("client_secret_basic", "client_secret_post")
_METADATA_PATH = "/.well-known/oauth-authorization-server"


class PermissiveOAuthProxy(OAuthProxy):
    """OAuthProxy that optionally accepts raw upstream JWTs.

    When *allow_raw_bearer* is set, a bearer token that fails the standard MCP
    JWT swap (because it isn't a proxy-issued JWT) falls through to the
    configured ``token_verifier``. If the verifier accepts it (i.e. the token is
    a valid Viya JWT signed by the upstream JWKS), the request proceeds with the
    raw token used directly as the upstream credential.

    This lets PKCE clients and pre-authenticated programmatic clients hit the
    same MCP endpoint without conflict — the additive path only kicks in after
    the standard swap has already failed.

    NOTE on scope: the fallback trusts whatever ``token_verifier`` accepts. With
    the server's default ``JWTVerifier(jwks_uri=..., audience=[])`` that is any
    JWT the upstream SASLogon signed, including tokens minted for other clients,
    with no client registration and no consent step. Enable deliberately.
    """

    def __init__(
        self,
        *args,
        allow_raw_bearer: bool = False,
        client_secrets: bool = False,
        **kwargs,
    ) -> None:
        super().__init__(*args, **kwargs)
        self._allow_raw_bearer = allow_raw_bearer
        self._client_secrets = client_secrets

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        """Register a client; with *client_secrets*, keep the secret it asked for.

        FastMCP 4 registers every client as public: whatever the client asked
        for, it is stored and answered with ``token_endpoint_auth_method: none``
        and no secret. Most MCP clients take that in their stride. Microsoft 365
        Copilot does not — its registration service asks for
        ``client_secret_basic`` and rejects a response without ``client_secret``.

        With *client_secrets* on, a client that asks for a secret-based method
        keeps the secret the SDK generated for it, and the token endpoint then
        requires that secret (the SDK's authenticator enforces whatever the
        stored client says). A client that asks for ``none`` is unaffected.
        """
        wanted = client_info.token_endpoint_auth_method
        secret = client_info.client_secret
        expires_at = client_info.client_secret_expires_at
        await super().register_client(client_info)
        if not (self._client_secrets and secret and wanted in SECRET_AUTH_METHODS):
            return
        client_id = client_info.client_id
        stored = await self._client_store.get(key=client_id) if client_id else None
        if stored is None or not client_id:
            return
        for record in (stored, client_info):
            record.token_endpoint_auth_method = wanted
            record.client_secret = secret
            record.client_secret_expires_at = expires_at
        await self._client_store.put(key=client_id, value=stored)

    def get_routes(self, mcp_path: str | None = None) -> list[Route]:
        """The proxy's routes; with *client_secrets*, the authorization-server
        metadata also lists the secret-based methods, since a client that
        requires one reads the metadata before it registers."""
        routes = super().get_routes(mcp_path)
        if not self._client_secrets:
            return routes
        return [
            Route(
                route.path,
                endpoint=_AdvertiseSecretMethods(route.app),
                methods=sorted(route.methods or ["GET", "OPTIONS"]),
            )
            if isinstance(route, Route) and route.path.startswith(_METADATA_PATH)
            else route
            for route in routes
        ]

    async def load_access_token(self, token: str) -> AccessToken | None:
        validated = await super().load_access_token(token)
        if validated is not None:
            return validated
        if not self._allow_raw_bearer:
            return None
        raw = await self._token_validator.verify_token(token)
        if raw is not None:
            logger.info(
                "Accepted raw bearer token (ALLOW_RAW_BEARER=true); "
                "bypassing MCP JWT swap"
            )
        return raw


class _AdvertiseSecretMethods:
    """ASGI wrapper for the metadata route: add the secret-based methods to
    ``token_endpoint_auth_methods_supported``.

    FastMCP builds that document inside ``get_routes`` and hard-codes the list,
    so the response is the only place left to amend it.
    """

    def __init__(self, app: Any) -> None:
        self._app = app

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        start: dict[str, Any] = {}
        chunks: list[bytes] = []

        async def amend(message: dict[str, Any]) -> None:
            if message["type"] == "http.response.start":
                start.update(message)
                return
            if message["type"] != "http.response.body":
                await send(message)
                return
            chunks.append(message.get("body", b""))
            if message.get("more_body"):
                return
            body = b"".join(chunks)
            try:
                document = json.loads(body)
                methods = list(document["token_endpoint_auth_methods_supported"])
            except (ValueError, KeyError, TypeError):
                pass  # not the metadata document (a CORS preflight): pass it on
            else:
                methods += [m for m in SECRET_AUTH_METHODS if m not in methods]
                document["token_endpoint_auth_methods_supported"] = methods
                body = json.dumps(document).encode("utf-8")
            headers = [
                (name, value)
                for name, value in start.get("headers", [])
                if name.lower() != b"content-length"
            ]
            headers.append((b"content-length", str(len(body)).encode("ascii")))
            await send({**start, "headers": headers})
            await send({"type": "http.response.body", "body": body})

        await self._app(scope, receive, amend)
