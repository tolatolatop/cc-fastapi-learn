from review_console.security import (
    create_session,
    hash_password,
    read_session,
    verify_password,
)


def test_password_hash_and_signed_session_round_trip():
    encoded = hash_password("a-long-test-password")
    assert "a-long-test-password" not in encoded
    assert verify_password("a-long-test-password", encoded)
    assert not verify_password("wrong-password", encoded)

    secret = "s" * 32
    token = create_session("user-1", secret, 1)
    assert read_session(token, secret) == "user-1"
    assert read_session(token + "tampered", secret) is None
