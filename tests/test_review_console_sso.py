import json
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlparse

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from review_console.access_control.oidc import OidcClient, OidcValidationError
from review_console.access_control.router import get_oidc_client
from review_console.config import Settings, get_settings
from review_console.db import get_db
from review_console.main import app
from review_console.models import Base, ConsoleUser, SsoIdentity
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool


class FakeOidcClient:
    def __init__(self):
        self.authorization_request: dict[str, str] = {}
        self.authentication_request: dict[str, str] = {}

    def authorization_url(
        self, *, state: str, nonce: str, code_challenge: str
    ) -> str:
        self.authorization_request = {
            "state": state,
            "nonce": nonce,
            "code_challenge": code_challenge,
        }
        return f"https://identity.example/authorize?state={state}"

    def authenticate(
        self, *, code: str, code_verifier: str, nonce: str
    ) -> dict:
        self.authentication_request = {
            "code": code,
            "code_verifier": code_verifier,
            "nonce": nonce,
        }
        return {
            "iss": "https://identity.example",
            "sub": "employee-42",
            "preferred_username": "lin",
            "name": "Lin Reviewer",
            "groups": ["review-console-admins"],
        }

    def validate_access_token(self, token: str) -> dict:
        if token not in {"valid-access-token", "valid-admin-group-access-token"}:
            raise OidcValidationError("invalid access token")
        return {
            "iss": "https://identity.example",
            "sub": "api-operator-7",
            "preferred_username": "api-operator",
            "name": "API Operator",
            "groups": ["review-console-admins"]
            if token == "valid-admin-group-access-token"
            else [],
        }


@pytest.fixture
def sso_environment(monkeypatch):
    values = {
        "REVIEW_CONSOLE_SESSION_SECRET": "s" * 32,
        "REVIEW_CONSOLE_LOCAL_LOGIN_ENABLED": "false",
        "REVIEW_CONSOLE_SSO_ENABLED": "true",
        "REVIEW_CONSOLE_SSO_ISSUER_URL": "https://identity.example",
        "REVIEW_CONSOLE_SSO_CLIENT_ID": "review-console",
        "REVIEW_CONSOLE_SSO_CLIENT_SECRET": "client-secret",
        "REVIEW_CONSOLE_SSO_REDIRECT_URI": (
            "https://review.example/api/v1/auth/sso/callback"
        ),
        "REVIEW_CONSOLE_SSO_ADMIN_GROUP": "review-console-admins",
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    get_settings.cache_clear()
    yield
    app.dependency_overrides.clear()
    get_settings.cache_clear()


def build_client() -> tuple[TestClient, sessionmaker, FakeOidcClient]:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    Base.metadata.create_all(engine)

    def override_db():
        with sessions() as db:
            yield db

    fake_oidc = FakeOidcClient()
    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_oidc_client] = lambda: fake_oidc
    return TestClient(app), sessions, fake_oidc


def test_sso_configuration_loads_from_dotenv(tmp_path, monkeypatch):
    for name in (
        "REVIEW_CONSOLE_SSO_ENABLED",
        "REVIEW_CONSOLE_SSO_ISSUER_URL",
        "REVIEW_CONSOLE_SSO_CLIENT_ID",
        "REVIEW_CONSOLE_SSO_REDIRECT_URI",
    ):
        monkeypatch.delenv(name, raising=False)
    dotenv = tmp_path / ".env"
    dotenv.write_text(
        """REVIEW_CONSOLE_SSO_ENABLED="true"
REVIEW_CONSOLE_SSO_ISSUER_URL="https://identity.example"
REVIEW_CONSOLE_SSO_CLIENT_ID="review-console"
REVIEW_CONSOLE_SSO_REDIRECT_URI="https://review.example/api/v1/auth/sso/callback"
""",
        encoding="utf-8",
    )
    settings = Settings(_env_file=dotenv)
    assert settings.sso_enabled is True
    assert settings.sso_issuer_url == "https://identity.example"
    assert settings.sso_client_id == "review-console"


def test_sso_login_uses_signed_flow_pkce_and_creates_identity(sso_environment):
    client, sessions, fake_oidc = build_client()
    config = client.get("/v1/auth/config")
    assert config.json() == {
        "local_login_enabled": False,
        "sso_enabled": True,
        "oauth_bearer_enabled": False,
        "sso_button_label": "使用企业账号登录",
    }

    start = client.get("/v1/auth/sso/login", follow_redirects=False)
    assert start.status_code == 302
    state = parse_qs(urlparse(start.headers["location"]).query)["state"][0]
    assert fake_oidc.authorization_request["code_challenge"]
    assert "HttpOnly" in start.headers["set-cookie"]
    assert "SameSite=lax" in start.headers["set-cookie"]

    callback = client.get(
        "/v1/auth/sso/callback",
        params={"code": "authorization-code", "state": state},
        follow_redirects=False,
    )
    assert callback.status_code == 303
    assert callback.headers["location"] == "/"
    assert fake_oidc.authentication_request["code"] == "authorization-code"
    assert fake_oidc.authentication_request["code_verifier"]
    assert (
        fake_oidc.authentication_request["nonce"]
        == fake_oidc.authorization_request["nonce"]
    )

    me = client.get("/v1/auth/me")
    assert me.status_code == 200
    assert me.json()["username"] == "lin"
    assert me.json()["display_name"] == "Lin Reviewer"
    assert me.json()["auth_source"] == "sso"
    assert me.json()["is_admin"] is True
    with sessions() as db:
        identity = db.scalar(select(SsoIdentity))
        assert identity is not None
        assert identity.subject == "employee-42"
        user = db.get(ConsoleUser, identity.user_id)
        assert user is not None
        assert user.password_hash == "!sso"


def test_sso_does_not_link_an_existing_local_username(sso_environment):
    client, sessions, _ = build_client()
    with sessions() as db:
        db.add(
            ConsoleUser(
                username="lin",
                display_name="Existing Local User",
                password_hash="local-password-hash",
            )
        )
        db.commit()

    start = client.get("/v1/auth/sso/login", follow_redirects=False)
    state = parse_qs(urlparse(start.headers["location"]).query)["state"][0]
    callback = client.get(
        "/v1/auth/sso/callback",
        params={"code": "authorization-code", "state": state},
        follow_redirects=False,
    )
    assert callback.status_code == 303
    assert client.get("/v1/auth/me").json()["username"].startswith("lin-")
    with sessions() as db:
        local = db.scalar(
            select(ConsoleUser).where(ConsoleUser.username == "lin")
        )
        assert local is not None
        assert local.display_name == "Existing Local User"


def test_sso_callback_rejects_state_mismatch(sso_environment):
    client, sessions, fake_oidc = build_client()
    client.get("/v1/auth/sso/login", follow_redirects=False)
    response = client.get(
        "/v1/auth/sso/callback",
        params={"code": "authorization-code", "state": "wrong-state"},
        follow_redirects=False,
    )
    assert response.status_code == 401
    assert fake_oidc.authentication_request == {}
    with sessions() as db:
        assert db.scalar(select(SsoIdentity)) is None


def test_sso_preserves_safe_pull_request_return_url(sso_environment):
    client, _, _ = build_client()
    return_url = "/?view=issues&provider=github&project_path=team%2Fservice&pr=17"
    start = client.get(
        "/v1/auth/sso/login",
        params={"next": return_url},
        follow_redirects=False,
    )
    state = parse_qs(urlparse(start.headers["location"]).query)["state"][0]
    callback = client.get(
        "/v1/auth/sso/callback",
        params={"code": "authorization-code", "state": state},
        follow_redirects=False,
    )
    assert callback.headers["location"] == return_url


def test_sso_rejects_external_return_url(sso_environment):
    client, _, _ = build_client()
    start = client.get(
        "/v1/auth/sso/login",
        params={"next": "//malicious.example/steal"},
        follow_redirects=False,
    )
    state = parse_qs(urlparse(start.headers["location"]).query)["state"][0]
    callback = client.get(
        "/v1/auth/sso/callback",
        params={"code": "authorization-code", "state": state},
        follow_redirects=False,
    )
    assert callback.headers["location"] == "/"


def test_local_login_can_be_disabled(sso_environment):
    client, _, _ = build_client()
    response = client.post(
        "/v1/auth/login", json={"username": "admin", "password": "password"}
    )
    assert response.status_code == 403


def test_oidc_client_validates_signature_audience_issuer_and_nonce(
    sso_environment, monkeypatch
):
    settings = get_settings()
    client = OidcClient(settings)
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(private_key.public_key()))
    public_jwk.update({"kid": "signing-key", "use": "sig", "alg": "RS256"})
    now = datetime.now(UTC)
    token = jwt.encode(
        {
            "iss": settings.sso_issuer_url,
            "sub": "employee-42",
            "aud": settings.sso_client_id,
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
        "issuer": settings.sso_issuer_url,
        "jwks_uri": "https://identity.example/keys",
    }
    claims = client._validate_id_token(metadata, token, nonce="expected-nonce")
    assert claims["sub"] == "employee-42"
    with pytest.raises(OidcValidationError, match="nonce"):
        client._validate_id_token(metadata, token, nonce="wrong-nonce")


def test_oauth2_bearer_access_token_authenticates_console_user(
    sso_environment, monkeypatch
):
    monkeypatch.setenv("REVIEW_CONSOLE_OAUTH_BEARER_ENABLED", "true")
    monkeypatch.setenv("REVIEW_CONSOLE_OAUTH_AUDIENCE", "review-console-api")
    get_settings.cache_clear()
    client, sessions, _ = build_client()

    response = client.get(
        "/v1/auth/me", headers={"Authorization": "Bearer valid-access-token"}
    )

    assert response.status_code == 200
    assert response.json()["username"] == "api-operator"
    assert response.json()["auth_source"] == "sso"
    with sessions() as db:
        identity = db.scalar(
            select(SsoIdentity).where(SsoIdentity.subject == "api-operator-7")
        )
        assert identity is not None


def test_invalid_oauth2_bearer_token_returns_bearer_challenge(
    sso_environment, monkeypatch
):
    monkeypatch.setenv("REVIEW_CONSOLE_OAUTH_BEARER_ENABLED", "true")
    monkeypatch.setenv("REVIEW_CONSOLE_OAUTH_AUDIENCE", "review-console-api")
    get_settings.cache_clear()
    client, _, _ = build_client()

    response = client.get(
        "/v1/auth/me", headers={"Authorization": "Bearer invalid-access-token"}
    )

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


def test_oauth2_bearer_rejects_inactive_mapped_user(sso_environment, monkeypatch):
    monkeypatch.setenv("REVIEW_CONSOLE_OAUTH_BEARER_ENABLED", "true")
    monkeypatch.setenv("REVIEW_CONSOLE_OAUTH_AUDIENCE", "review-console-api")
    get_settings.cache_clear()
    client, sessions, _ = build_client()
    with sessions() as db:
        user = ConsoleUser(
            username="disabled-api-operator",
            display_name="Disabled API Operator",
            password_hash="!sso",
            is_active=False,
        )
        user.sso_identities.append(
            SsoIdentity(
                issuer="https://identity.example",
                subject="api-operator-7",
            )
        )
        db.add(user)
        db.commit()

    response = client.get(
        "/v1/auth/me", headers={"Authorization": "Bearer valid-access-token"}
    )

    assert response.status_code == 403


def test_oauth2_bearer_does_not_overwrite_existing_sso_profile(
    sso_environment, monkeypatch
):
    monkeypatch.setenv("REVIEW_CONSOLE_OAUTH_BEARER_ENABLED", "true")
    monkeypatch.setenv("REVIEW_CONSOLE_OAUTH_AUDIENCE", "review-console-api")
    get_settings.cache_clear()
    client, sessions, _ = build_client()
    with sessions() as db:
        user = ConsoleUser(
            username="oidc-admin",
            display_name="OIDC Administrator",
            password_hash="!sso",
            is_admin=True,
        )
        user.sso_identities.append(
            SsoIdentity(
                issuer="https://identity.example",
                subject="api-operator-7",
            )
        )
        db.add(user)
        db.commit()

    response = client.get(
        "/v1/auth/me", headers={"Authorization": "Bearer valid-access-token"}
    )

    assert response.status_code == 200
    assert response.json()["display_name"] == "OIDC Administrator"
    assert response.json()["is_admin"] is True


def test_oauth2_bearer_never_auto_grants_admin_to_new_user(
    sso_environment, monkeypatch
):
    monkeypatch.setenv("REVIEW_CONSOLE_OAUTH_BEARER_ENABLED", "true")
    monkeypatch.setenv("REVIEW_CONSOLE_OAUTH_AUDIENCE", "review-console-api")
    get_settings.cache_clear()
    client, _, _ = build_client()

    response = client.get(
        "/v1/auth/me",
        headers={"Authorization": "Bearer valid-admin-group-access-token"},
    )

    assert response.status_code == 200
    assert response.json()["is_admin"] is False


def test_review_console_openapi_advertises_oauth2_bearer(sso_environment):
    document = app.openapi()
    scheme = document["components"]["securitySchemes"]["ReviewConsoleOAuth2Bearer"]
    assert scheme["type"] == "http"
    assert scheme["scheme"] == "bearer"


def test_access_token_azp_is_not_compared_with_resource_audience(
    sso_environment, monkeypatch
):
    monkeypatch.setenv("REVIEW_CONSOLE_OAUTH_BEARER_ENABLED", "true")
    monkeypatch.setenv("REVIEW_CONSOLE_OAUTH_AUDIENCE", "review-console-api")
    get_settings.cache_clear()
    settings = get_settings()
    client = OidcClient(settings)
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(private_key.public_key()))
    public_jwk.update({"kid": "signing-key", "use": "sig", "alg": "RS256"})
    now = datetime.now(UTC)
    token = jwt.encode(
        {
            "iss": settings.sso_issuer_url,
            "sub": "api-operator-7",
            "aud": "review-console-api",
            "azp": settings.sso_client_id,
            "iat": now,
            "exp": now + timedelta(minutes=5),
        },
        private_key,
        algorithm="RS256",
        headers={"kid": "signing-key", "typ": "at+jwt"},
    )
    monkeypatch.setattr(client, "_get_json", lambda url, **kwargs: {"keys": [public_jwk]})
    metadata = {
        "issuer": settings.sso_issuer_url,
        "authorization_endpoint": "https://identity.example/authorize",
        "token_endpoint": "https://identity.example/token",
        "jwks_uri": "https://identity.example/keys",
    }
    monkeypatch.setattr(client, "_metadata", lambda: metadata)

    claims = client.validate_access_token(token)
    assert claims["azp"] == settings.sso_client_id


def test_oidc_id_token_cannot_be_used_as_oauth2_access_token(
    sso_environment, monkeypatch
):
    monkeypatch.setenv("REVIEW_CONSOLE_OAUTH_BEARER_ENABLED", "true")
    monkeypatch.setenv("REVIEW_CONSOLE_OAUTH_AUDIENCE", "review-console-api")
    get_settings.cache_clear()
    settings = get_settings()
    client = OidcClient(settings)
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(private_key.public_key()))
    public_jwk.update({"kid": "signing-key", "use": "sig", "alg": "RS256"})
    now = datetime.now(UTC)
    id_token = jwt.encode(
        {
            "iss": settings.sso_issuer_url,
            "sub": "employee-42",
            "aud": "review-console-api",
            "iat": now,
            "exp": now + timedelta(minutes=5),
        },
        private_key,
        algorithm="RS256",
        headers={"kid": "signing-key", "typ": "JWT"},
    )
    metadata = {
        "issuer": settings.sso_issuer_url,
        "authorization_endpoint": "https://identity.example/authorize",
        "token_endpoint": "https://identity.example/token",
        "jwks_uri": "https://identity.example/keys",
    }
    monkeypatch.setattr(client, "_metadata", lambda: metadata)
    monkeypatch.setattr(client, "_get_json", lambda url, **kwargs: {"keys": [public_jwk]})

    with pytest.raises(OidcValidationError, match="token type"):
        client.validate_access_token(id_token)


def test_oauth_and_oidc_audiences_must_be_distinct(sso_environment, monkeypatch):
    monkeypatch.setenv("REVIEW_CONSOLE_OAUTH_BEARER_ENABLED", "true")
    monkeypatch.setenv("REVIEW_CONSOLE_OAUTH_AUDIENCE", "review-console")
    get_settings.cache_clear()

    with pytest.raises(RuntimeError, match="must differ"):
        get_settings().validate_runtime()


def test_oauth2_bearer_only_mode_does_not_require_oidc_client_credentials(
    sso_environment, monkeypatch
):
    monkeypatch.setenv("REVIEW_CONSOLE_LOCAL_LOGIN_ENABLED", "false")
    monkeypatch.setenv("REVIEW_CONSOLE_SSO_ENABLED", "false")
    monkeypatch.setenv("REVIEW_CONSOLE_OAUTH_BEARER_ENABLED", "true")
    monkeypatch.setenv("REVIEW_CONSOLE_OAUTH_AUDIENCE", "review-console-api")
    monkeypatch.setenv("REVIEW_CONSOLE_SSO_CLIENT_ID", "")
    monkeypatch.setenv("REVIEW_CONSOLE_SSO_CLIENT_SECRET", "")
    monkeypatch.setenv("REVIEW_CONSOLE_SSO_REDIRECT_URI", "")
    get_settings.cache_clear()

    get_settings().validate_runtime()


def test_oidc_discovery_requires_exact_issuer_match(sso_environment, monkeypatch):
    client = OidcClient(get_settings())
    monkeypatch.setattr(
        client,
        "_get_json",
        lambda url, **kwargs: {
            "issuer": "https://identity.example/",
            "authorization_endpoint": "https://identity.example/authorize",
            "token_endpoint": "https://identity.example/token",
            "jwks_uri": "https://identity.example/keys",
        },
    )

    with pytest.raises(OidcValidationError, match="issuer"):
        client._metadata()
