import base64
import hashlib
import secrets
from typing import Any
from urllib.parse import urlencode

import httpx
import jwt

from review_console.config import Settings


class OidcError(Exception):
    """Base error for a failed OIDC interaction."""


class OidcUnavailableError(OidcError):
    """The identity provider could not be reached or returned an invalid response."""


class OidcValidationError(OidcError):
    """The identity provider response failed protocol validation."""


def generate_flow_value(length: int = 48) -> str:
    return secrets.token_urlsafe(length)


def pkce_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode()).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


class OidcClient:
    def __init__(self, settings: Settings):
        self.settings = settings

    def _get_json(self, url: str, *, access_token: str | None = None) -> dict[str, Any]:
        headers = {"Authorization": f"Bearer {access_token}"} if access_token else None
        try:
            response = httpx.get(
                url,
                headers=headers,
                timeout=self.settings.sso_timeout_seconds,
            )
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise OidcUnavailableError("identity provider request failed") from exc
        if not isinstance(payload, dict):
            raise OidcUnavailableError("identity provider returned invalid JSON")
        return payload

    def _metadata(self) -> dict[str, Any]:
        issuer = self.settings.sso_issuer_url.rstrip("/")
        metadata = self._get_json(f"{issuer}/.well-known/openid-configuration")
        discovered_issuer = metadata.get("issuer")
        if not isinstance(discovered_issuer, str) or discovered_issuer.rstrip("/") != issuer:
            raise OidcValidationError("OIDC issuer mismatch")
        for field in ("authorization_endpoint", "token_endpoint", "jwks_uri"):
            if not isinstance(metadata.get(field), str):
                raise OidcUnavailableError(f"OIDC discovery is missing {field}")
        return metadata

    def authorization_url(
        self, *, state: str, nonce: str, code_challenge: str
    ) -> str:
        metadata = self._metadata()
        query = urlencode(
            {
                "response_type": "code",
                "client_id": self.settings.sso_client_id,
                "redirect_uri": self.settings.sso_redirect_uri,
                "scope": self.settings.sso_scopes,
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
            "redirect_uri": self.settings.sso_redirect_uri,
            "code_verifier": code_verifier,
        }
        auth: tuple[str, str] | None = None
        if self.settings.sso_client_auth_method == "client_secret_basic":
            auth = (self.settings.sso_client_id, self.settings.sso_client_secret)
        elif self.settings.sso_client_auth_method == "client_secret_post":
            data["client_id"] = self.settings.sso_client_id
            data["client_secret"] = self.settings.sso_client_secret
        else:
            data["client_id"] = self.settings.sso_client_id
        try:
            response = httpx.post(
                metadata["token_endpoint"],
                data=data,
                auth=auth,
                timeout=self.settings.sso_timeout_seconds,
            )
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise OidcUnavailableError("OIDC token exchange failed") from exc
        if not isinstance(payload, dict):
            raise OidcUnavailableError("OIDC token endpoint returned invalid JSON")
        return payload

    def _validate_id_token(
        self, metadata: dict[str, Any], token: str, *, nonce: str
    ) -> dict[str, Any]:
        try:
            header = jwt.get_unverified_header(token)
            algorithm = str(header["alg"])
            allowed_algorithms = [
                value.strip()
                for value in self.settings.sso_signing_algorithms.split(",")
                if value.strip()
            ]
            if algorithm not in allowed_algorithms:
                raise OidcValidationError("OIDC signing algorithm is not allowed")
            jwks = self._get_json(metadata["jwks_uri"])
            keys = jwt.PyJWKSet.from_dict(jwks).keys
            key_id = header.get("kid")
            matching = [key for key in keys if key_id is None or key.key_id == key_id]
            if len(matching) != 1:
                raise OidcValidationError("OIDC signing key could not be selected")
            claims = jwt.decode(
                token,
                key=matching[0].key,
                algorithms=allowed_algorithms,
                audience=self.settings.sso_client_id,
                issuer=metadata["issuer"],
                options={"require": ["exp", "iat", "iss", "sub", "aud"]},
            )
        except OidcError:
            raise
        except (jwt.PyJWTError, KeyError, TypeError, ValueError) as exc:
            raise OidcValidationError("OIDC ID token validation failed") from exc
        if not secrets.compare_digest(str(claims.get("nonce", "")), nonce):
            raise OidcValidationError("OIDC nonce mismatch")
        audience = claims.get("aud")
        authorized_party = claims.get("azp")
        if (
            isinstance(audience, list)
            and len(audience) > 1
            and authorized_party != self.settings.sso_client_id
        ) or (
            authorized_party is not None
            and authorized_party != self.settings.sso_client_id
        ):
            raise OidcValidationError("OIDC authorized party mismatch")
        return claims

    def authenticate(
        self, *, code: str, code_verifier: str, nonce: str
    ) -> dict[str, Any]:
        metadata = self._metadata()
        tokens = self._exchange_code(metadata, code=code, code_verifier=code_verifier)
        id_token = tokens.get("id_token")
        if not isinstance(id_token, str):
            raise OidcValidationError("OIDC token response did not include an ID token")
        claims = self._validate_id_token(metadata, id_token, nonce=nonce)
        userinfo_endpoint = metadata.get("userinfo_endpoint")
        access_token = tokens.get("access_token")
        if isinstance(userinfo_endpoint, str) and isinstance(access_token, str):
            userinfo = self._get_json(userinfo_endpoint, access_token=access_token)
            if userinfo.get("sub") != claims.get("sub"):
                raise OidcValidationError("OIDC UserInfo subject mismatch")
            issuer = claims["iss"]
            subject = claims["sub"]
            claims = {**claims, **userinfo, "iss": issuer, "sub": subject}
        return claims
