import base64
import hashlib
import secrets
from typing import Any
from urllib.parse import urlencode

import httpx
import jwt

from cc_fastapi.core.config import Settings


class OidcError(Exception):
    """Base error for an unsuccessful OIDC interaction."""


class OidcUnavailableError(OidcError):
    """The authorization server is unavailable or returned malformed data."""


class OidcValidationError(OidcError):
    """An OAuth 2.0 or OIDC value failed protocol validation."""


def generate_flow_value(length: int = 48) -> str:
    return secrets.token_urlsafe(length)


def pkce_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode()).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


class OidcClient:
    """OIDC relying party and OAuth 2.0 JWT resource-server client."""

    def __init__(self, settings: Settings):
        self.settings = settings

    @property
    def _algorithms(self) -> list[str]:
        return [
            item.strip()
            for item in self.settings.oidc_signing_algorithms.split(",")
            if item.strip()
        ]

    def _get_json(self, url: str, *, access_token: str | None = None) -> dict[str, Any]:
        headers = {"Authorization": f"Bearer {access_token}"} if access_token else None
        try:
            response = httpx.get(
                url,
                headers=headers,
                timeout=self.settings.oidc_timeout_seconds,
            )
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise OidcUnavailableError("authorization server request failed") from exc
        if not isinstance(payload, dict):
            raise OidcUnavailableError("authorization server returned invalid JSON")
        return payload

    def _metadata(self) -> dict[str, Any]:
        issuer = self.settings.oidc_issuer_url.rstrip("/")
        metadata = self._get_json(f"{issuer}/.well-known/openid-configuration")
        discovered_issuer = metadata.get("issuer")
        if not isinstance(discovered_issuer, str) or discovered_issuer.rstrip("/") != issuer:
            raise OidcValidationError("OIDC issuer mismatch")
        for field in ("authorization_endpoint", "token_endpoint", "jwks_uri"):
            if not isinstance(metadata.get(field), str):
                raise OidcUnavailableError(f"OIDC discovery is missing {field}")
        return metadata

    def authorization_url(self, *, state: str, nonce: str, code_challenge: str) -> str:
        metadata = self._metadata()
        query = urlencode(
            {
                "response_type": "code",
                "client_id": self.settings.oidc_client_id,
                "redirect_uri": self.settings.oidc_redirect_uri,
                "scope": self.settings.oidc_scopes,
                "state": state,
                "nonce": nonce,
                "code_challenge": code_challenge,
                "code_challenge_method": "S256",
            }
        )
        return f"{metadata['authorization_endpoint']}?{query}"

    def _exchange_code(
        self, metadata: dict[str, Any], *, code: str, code_verifier: str
    ) -> dict[str, Any]:
        data = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": self.settings.oidc_redirect_uri,
            "code_verifier": code_verifier,
        }
        auth: tuple[str, str] | None = None
        if self.settings.oidc_client_auth_method == "client_secret_basic":
            auth = (self.settings.oidc_client_id, self.settings.oidc_client_secret)
        elif self.settings.oidc_client_auth_method == "client_secret_post":
            data["client_id"] = self.settings.oidc_client_id
            data["client_secret"] = self.settings.oidc_client_secret
        else:
            data["client_id"] = self.settings.oidc_client_id
        try:
            response = httpx.post(
                metadata["token_endpoint"],
                data=data,
                auth=auth,
                timeout=self.settings.oidc_timeout_seconds,
            )
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise OidcUnavailableError("OIDC token exchange failed") from exc
        if not isinstance(payload, dict):
            raise OidcUnavailableError("OIDC token endpoint returned invalid JSON")
        return payload

    def _select_key(self, metadata: dict[str, Any], token: str) -> tuple[Any, str]:
        try:
            header = jwt.get_unverified_header(token)
            algorithm = str(header["alg"])
            if algorithm not in self._algorithms:
                raise OidcValidationError("OIDC signing algorithm is not allowed")
            jwks = self._get_json(metadata["jwks_uri"])
            keys = jwt.PyJWKSet.from_dict(jwks).keys
            key_id = header.get("kid")
            matching = [key for key in keys if key_id is None or key.key_id == key_id]
            if len(matching) != 1:
                raise OidcValidationError("OIDC signing key could not be selected")
            return matching[0].key, algorithm
        except OidcError:
            raise
        except (jwt.PyJWTError, KeyError, TypeError, ValueError) as exc:
            raise OidcValidationError("OIDC signing key selection failed") from exc

    def _validate_jwt(
        self,
        metadata: dict[str, Any],
        token: str,
        *,
        nonce: str | None = None,
        audience: str | None = None,
        validate_authorized_party: bool = True,
    ) -> dict[str, Any]:
        key, _ = self._select_key(metadata, token)
        expected_audience = audience or self.settings.oidc_client_id
        try:
            claims = jwt.decode(
                token,
                key=key,
                algorithms=self._algorithms,
                audience=expected_audience,
                issuer=metadata["issuer"],
                options={"require": ["exp", "iat", "iss", "sub", "aud"]},
            )
        except (jwt.PyJWTError, KeyError, TypeError, ValueError) as exc:
            raise OidcValidationError("JWT validation failed") from exc
        if nonce is not None and not secrets.compare_digest(str(claims.get("nonce", "")), nonce):
            raise OidcValidationError("OIDC nonce mismatch")
        token_audience = claims.get("aud")
        authorized_party = claims.get("azp")
        if validate_authorized_party and (
            (
                isinstance(token_audience, list)
                and len(token_audience) > 1
                and authorized_party != expected_audience
            )
            or (authorized_party is not None and authorized_party != expected_audience)
        ):
            raise OidcValidationError("OIDC authorized party mismatch")
        return claims

    def authenticate(self, *, code: str, code_verifier: str, nonce: str) -> dict[str, Any]:
        metadata = self._metadata()
        tokens = self._exchange_code(metadata, code=code, code_verifier=code_verifier)
        id_token = tokens.get("id_token")
        if not isinstance(id_token, str):
            raise OidcValidationError("OIDC token response did not include an ID token")
        claims = self._validate_jwt(metadata, id_token, nonce=nonce)
        userinfo_endpoint = metadata.get("userinfo_endpoint")
        access_token = tokens.get("access_token")
        if isinstance(userinfo_endpoint, str) and isinstance(access_token, str):
            userinfo = self._get_json(userinfo_endpoint, access_token=access_token)
            if userinfo.get("sub") != claims.get("sub"):
                raise OidcValidationError("OIDC UserInfo subject mismatch")
            issuer, subject = claims["iss"], claims["sub"]
            claims = {**claims, **userinfo, "iss": issuer, "sub": subject}
        return claims

    def validate_access_token(self, token: str) -> dict[str, Any]:
        metadata = self._metadata()
        audience = self.settings.oidc_audience.strip() or self.settings.oidc_client_id
        return self._validate_jwt(
            metadata,
            token,
            audience=audience,
            validate_authorized_party=False,
        )
