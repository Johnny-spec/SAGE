"""Inbound principal resolution for anonymous development and Microsoft Entra ID."""

from typing import Any, Protocol

import jwt
from jwt import PyJWKClient

from sage.domain import Principal


class AuthenticationError(ValueError):
    """Raised when an inbound credential cannot establish a trusted Principal."""


class PrincipalResolver(Protocol):
    def resolve(self, authorization: str | None) -> Principal: ...


class AnonymousPrincipalResolver:
    def resolve(self, authorization: str | None) -> Principal:
        del authorization
        return Principal.anonymous()


class EntraPrincipalResolver:
    def __init__(
        self,
        *,
        tenant_id: str,
        audience: str,
        jwks_client: Any | None = None,
    ) -> None:
        self._tenant_id = tenant_id
        self._audience = audience
        self._issuer = f"https://login.microsoftonline.com/{tenant_id}/v2.0"
        self._jwks_client = jwks_client or PyJWKClient(
            f"https://login.microsoftonline.com/{tenant_id}/discovery/v2.0/keys"
        )

    def resolve(self, authorization: str | None) -> Principal:
        scheme, _, token = (authorization or "").partition(" ")
        if scheme.casefold() != "bearer" or not token:
            raise AuthenticationError("A Bearer access token is required")
        try:
            signing_key = self._jwks_client.get_signing_key_from_jwt(token).key
            claims = jwt.decode(
                token,
                signing_key,
                algorithms=["RS256"],
                audience=self._audience,
                issuer=self._issuer,
                options={"require": ["exp", "iss", "aud", "tid"]},
            )
        except jwt.PyJWTError as exc:
            raise AuthenticationError("The access token is invalid") from exc
        if claims.get("tid") != self._tenant_id:
            raise AuthenticationError("The access token tenant is invalid")
        principal_id = claims.get("oid") or claims.get("sub")
        if not isinstance(principal_id, str) or not principal_id.strip():
            raise AuthenticationError("The access token has no subject identifier")
        raw_roles = claims.get("roles", ())
        roles = (
            frozenset(role for role in raw_roles if isinstance(role, str) and role.strip())
            if isinstance(raw_roles, list)
            else frozenset()
        )
        return Principal(
            workspace_id=self._tenant_id,
            principal_id=principal_id,
            roles=roles | {"user"},
            authentication_method="entra-id",
        )
