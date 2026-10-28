import json
import warnings
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlparse

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from cc_fastapi.api.auth import get_oidc_client
from cc_fastapi.api.auth import router as auth_router
from cc_fastapi.api.dependencies import require_token
from cc_fastapi.auth.oidc import OidcClient, OidcUnavailableError, OidcValidationError
from cc_fastapi.auth.session import create_signed_payload
from cc_fastapi.core.config import get_settings


class FakeOidcClient:
    def __init__(self) -> None:
        self.authorization_request: dict[str, str] = {}
        self.authentication_request: dict[str, str] = {}

    def authorization_url(self, *, state: str, nonce: str, code_challenge: str) -> str:
        self.authorization_request = {
            "state": state,
            "nonce": nonce,
            "code_challenge": code_challenge,
        }
        return f"https://identity.example/authorize?state={state}"

    def authenticate(self, *, code: str, code_verifier: str, nonce: str) -> dict:
        self.authentication_request = {
            "code": code,
            "code_verifier": code_verifier,
            "nonce": nonce,
        }
        return {
            "iss": "https://identity.example",
            "sub": "operator-42",
            "preferred_username": "lin",
            "name": "Lin Operator",
            "email": "lin@example.com",
        }


@pytest.fixture
def oidc_environment(monkeypatch):
    values = {
        "API_TOKEN": "legacy-secret",
        "OIDC_ENABLED": "true",
        "OIDC_ISSUER_URL": "https://identity.example",
        "OIDC_CLIENT_ID": "cc-console",
        "OIDC_CLIENT_SECRET": "client-secret",
        "OIDC_REDIRECT_URI": "https://console.example/api/v1/auth/callback",
        "OIDC_SESSION_SECRET": "s" * 32,
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def build_app(fake_oidc: FakeOidcClient | None = None) -> FastAPI:
    app = FastAPI()
    app.include_router(auth_router)

    @app.get("/protected", dependencies=[Depends(require_token)])
    def protected() -> dict[str, bool]:
        return {"ok": True}

    if fake_oidc is not None:
        app.dependency_overrides[get_oidc_client] = lambda: fake_oidc
    return app


def test_oidc_login_uses_discovery_flow_state_nonce_and_pkce(oidc_environment):
    fake_oidc = FakeOidcClient()
    client = TestClient(build_app(fake_oidc))

    config = client.get("/v1/auth/config")
    assert config.json() == {
        "oidc_enabled": True,
        "login_url": "/v1/auth/login",
        "button_label": "使用企业账号登录",
    }

    start = client.get(
        "/v1/auth/login",
        params={"next": "/?view=tasks"},
        follow_redirects=False,
    )
    assert start.status_code == 302
    state = parse_qs(urlparse(start.headers["location"]).query)["state"][0]
    assert fake_oidc.authorization_request["code_challenge"]
    assert fake_oidc.authorization_request["nonce"]
    assert "HttpOnly" in start.headers["set-cookie"]
    assert "SameSite=lax" in start.headers["set-cookie"]

    callback = client.get(
        "/v1/auth/callback",
        params={"code": "authorization-code", "state": state},
        follow_redirects=False,
    )
    assert callback.status_code == 303
    assert callback.headers["location"] == "/?view=tasks"
    assert "SameSite=strict" in callback.headers["set-cookie"]
    assert fake_oidc.authentication_request["code"] == "authorization-code"
    assert fake_oidc.authentication_request["code_verifier"]
    assert fake_oidc.authentication_request["nonce"] == fake_oidc.authorization_request["nonce"]

    me = client.get("/v1/auth/me")
    assert me.status_code == 200
    assert me.json() == {
        "sub": "operator-42",
        "username": "lin",
        "display_name": "Lin Operator",
        "email": "lin@example.com",
    }
    assert client.get("/protected").status_code == 200


def test_callback_rejects_state_mismatch_without_authenticating(oidc_environment):
    fake_oidc = FakeOidcClient()
    client = TestClient(build_app(fake_oidc))
    client.get("/v1/auth/login", follow_redirects=False)

    response = client.get(
        "/v1/auth/callback",
        params={"code": "authorization-code", "state": "wrong"},
        follow_redirects=False,
    )

    assert response.status_code == 401
    assert fake_oidc.authentication_request == {}


def test_external_next_url_is_not_used_for_redirect(oidc_environment):
    fake_oidc = FakeOidcClient()
    client = TestClient(build_app(fake_oidc))
    start = client.get(
        "/v1/auth/login",
        params={"next": "//malicious.example/steal"},
        follow_redirects=False,
    )
    state = parse_qs(urlparse(start.headers["location"]).query)["state"][0]

    callback = client.get(
        "/v1/auth/callback",
        params={"code": "authorization-code", "state": state},
        follow_redirects=False,
    )

    assert callback.headers["location"] == "/"


def test_legacy_api_token_remains_supported(oidc_environment):
    client = TestClient(build_app())
    assert client.get("/protected", headers={"X-API-Token": "legacy-secret"}).status_code == 200


def test_missing_credentials_return_bearer_challenge(oidc_environment):
    client = TestClient(build_app())
    response = client.get("/protected")
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


def test_protected_api_fails_closed_when_oidc_session_secret_is_weak(
    oidc_environment, monkeypatch
):
    monkeypatch.setenv("OIDC_SESSION_SECRET", "weak")
    get_settings.cache_clear()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        forged = create_signed_payload(
            {
                "kind": "session",
                "sub": "attacker",
                "username": "attacker",
                "display_name": "Attacker",
                "email": "",
            },
            "weak",
            datetime.now(UTC) + timedelta(hours=1),
        )
    client = TestClient(build_app())
    client.cookies.set("cc_session", forged)
    response = client.get("/protected")
    assert response.status_code == 503


def test_openapi_advertises_api_key_and_http_bearer_security(oidc_environment):
    document = build_app().openapi()
    schemes = document["components"]["securitySchemes"]
    assert schemes["ApiToken"]["in"] == "header"
    assert schemes["ApiToken"]["name"] == "X-API-Token"
    assert schemes["OAuth2Bearer"]["scheme"] == "bearer"


def test_oauth2_bearer_access_token_is_validated(oidc_environment, monkeypatch):
    claims = {
        "iss": "https://identity.example",
        "sub": "service-42",
        "aud": "cc-console",
        "iat": datetime.now(UTC),
        "exp": datetime.now(UTC) + timedelta(minutes=5),
    }
    monkeypatch.setattr(OidcClient, "validate_access_token", lambda self, token: claims if token == "valid" else (_ for _ in ()).throw(OidcValidationError("invalid")))
    client = TestClient(build_app())

    assert client.get("/protected", headers={"Authorization": "Bearer valid"}).status_code == 200
    assert client.get("/protected", headers={"Authorization": "Bearer invalid"}).status_code == 401


def test_oauth2_provider_outage_returns_service_unavailable(oidc_environment, monkeypatch):
    def unavailable(self, token):
        raise OidcUnavailableError("offline")

    monkeypatch.setattr(OidcClient, "validate_access_token", unavailable)
    response = TestClient(build_app()).get(
        "/protected", headers={"Authorization": "Bearer token"}
    )
    assert response.status_code == 503


def test_oidc_client_validates_id_token_signature_issuer_audience_and_nonce(
    oidc_environment, monkeypatch
):
    settings = get_settings()
    client = OidcClient(settings)
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(private_key.public_key()))
    public_jwk.update({"kid": "signing-key", "use": "sig", "alg": "RS256"})
    now = datetime.now(UTC)
    token = jwt.encode(
        {
            "iss": settings.oidc_issuer_url,
            "sub": "operator-42",
            "aud": settings.oidc_client_id,
            "iat": now,
            "exp": now + timedelta(minutes=5),
            "nonce": "expected-nonce",
        },
        private_key,
        algorithm="RS256",
        headers={"kid": "signing-key"},
    )
    monkeypatch.setattr(client, "_get_json", lambda url, **kwargs: {"keys": [public_jwk]})
    metadata = {
        "issuer": settings.oidc_issuer_url,
        "jwks_uri": "https://identity.example/keys",
    }

    assert client._validate_jwt(metadata, token, nonce="expected-nonce")["sub"] == "operator-42"
    with pytest.raises(OidcValidationError, match="nonce"):
        client._validate_jwt(metadata, token, nonce="wrong-nonce")


def test_access_token_allows_azp_to_differ_from_resource_audience(
    oidc_environment, monkeypatch
):
    settings = get_settings()
    client = OidcClient(settings)
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(private_key.public_key()))
    public_jwk.update({"kid": "signing-key", "use": "sig", "alg": "RS256"})
    now = datetime.now(UTC)
    token = jwt.encode(
        {
            "iss": settings.oidc_issuer_url,
            "sub": "operator-42",
            "aud": "resource-api",
            "azp": settings.oidc_client_id,
            "iat": now,
            "exp": now + timedelta(minutes=5),
        },
        private_key,
        algorithm="RS256",
        headers={"kid": "signing-key"},
    )
    monkeypatch.setattr(client, "_get_json", lambda url, **kwargs: {"keys": [public_jwk]})
    metadata = {
        "issuer": settings.oidc_issuer_url,
        "jwks_uri": "https://identity.example/keys",
    }

    claims = client._validate_jwt(
        metadata,
        token,
        audience="resource-api",
        validate_authorized_party=False,
    )
    assert claims["azp"] == settings.oidc_client_id
