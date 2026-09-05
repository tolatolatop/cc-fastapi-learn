import base64
import hashlib
import hmac
import json
import os
from datetime import datetime, timedelta, timezone

PBKDF2_ITERATIONS = 600_000


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, PBKDF2_ITERATIONS)
    return f"pbkdf2_sha256${PBKDF2_ITERATIONS}${salt.hex()}${digest.hex()}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, iterations, salt, expected = encoded.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        actual = hashlib.pbkdf2_hmac(
            "sha256", password.encode(), bytes.fromhex(salt), int(iterations)
        )
        return hmac.compare_digest(actual, bytes.fromhex(expected))
    except (ValueError, TypeError):
        return False


def create_signed_payload(payload: dict, secret: str) -> str:
    body = base64.urlsafe_b64encode(
        json.dumps(payload, separators=(",", ":")).encode()
    ).rstrip(b"=")
    signature = hmac.new(secret.encode(), body, hashlib.sha256).digest()
    return (
        f"{body.decode()}.{base64.urlsafe_b64encode(signature).rstrip(b'=').decode()}"
    )


def read_signed_payload(token: str, secret: str) -> dict | None:
    try:
        body_text, signature_text = token.split(".", 1)
        body = body_text.encode()
        supplied = base64.urlsafe_b64decode(
            signature_text + "=" * (-len(signature_text) % 4)
        )
        expected = hmac.new(secret.encode(), body, hashlib.sha256).digest()
        if not hmac.compare_digest(supplied, expected):
            return None
        payload = json.loads(
            base64.urlsafe_b64decode(body_text + "=" * (-len(body_text) % 4))
        )
        if not isinstance(payload, dict):
            return None
        if int(payload["exp"]) <= int(datetime.now(timezone.utc).timestamp()):
            return None
        return payload
    except (ValueError, KeyError, TypeError, json.JSONDecodeError):
        return None


def create_session(user_id: str, secret: str, hours: int) -> str:
    return create_signed_payload(
        {
            "sub": user_id,
            "exp": int(
                (datetime.now(timezone.utc) + timedelta(hours=hours)).timestamp()
            ),
        },
        secret,
    )


def read_session(token: str, secret: str) -> str | None:
    payload = read_signed_payload(token, secret)
    if payload is None or "sub" not in payload:
        return None
    return str(payload["sub"])
