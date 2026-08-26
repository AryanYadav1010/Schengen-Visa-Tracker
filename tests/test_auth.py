from dataclasses import dataclass

import pytest

from app.auth import (
    check_login_rate_limit,
    decrypt_credential,
    encrypt_credential,
    hash_password,
    verify_password,
)
from app.config import settings


@dataclass
class _FakeClient:
    host: str


@dataclass
class _FakeRequest:
    client: _FakeClient


@pytest.fixture(autouse=True)
def secret_key():
    original = settings.SECRET_KEY
    settings.SECRET_KEY = "test-secret-key-for-unit-tests"
    yield
    settings.SECRET_KEY = original


def test_password_hash_round_trip():
    hashed = hash_password("correct-horse-battery-staple")
    assert verify_password("correct-horse-battery-staple", hashed) is True


def test_password_hash_rejects_wrong_password():
    hashed = hash_password("correct-horse-battery-staple")
    assert verify_password("wrong-password", hashed) is False


def test_password_hash_is_salted_differently_each_time():
    hashed_a = hash_password("same-password")
    hashed_b = hash_password("same-password")
    assert hashed_a != hashed_b
    assert verify_password("same-password", hashed_a) is True
    assert verify_password("same-password", hashed_b) is True


def test_verify_password_rejects_malformed_hash():
    assert verify_password("anything", "not-a-valid-hash") is False


def test_verify_password_accepts_legacy_two_part_hash():
    """Hashes created before the iteration count was embedded must still verify —
    otherwise raising _PBKDF2_ITERATIONS would lock out every existing user."""
    import hashlib
    import os as os_module

    from app.auth import _LEGACY_PBKDF2_ITERATIONS

    salt = os_module.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", b"an-old-password", salt, _LEGACY_PBKDF2_ITERATIONS)
    legacy_hash = f"{salt.hex()}${digest.hex()}"

    assert verify_password("an-old-password", legacy_hash) is True
    assert verify_password("wrong-password", legacy_hash) is False


def test_new_hash_embeds_current_iteration_count():
    from app.auth import _PBKDF2_ITERATIONS

    hashed = hash_password("some-password")
    iterations_str, _, _ = hashed.split("$")
    assert int(iterations_str) == _PBKDF2_ITERATIONS


def test_login_rate_limit_blocks_after_threshold():
    from app.auth import _LOGIN_MAX_ATTEMPTS, _login_attempts

    request = _FakeRequest(client=_FakeClient(host="198.51.100.1"))
    _login_attempts.pop("198.51.100.1", None)  # isolate from other tests

    for _ in range(_LOGIN_MAX_ATTEMPTS):
        assert check_login_rate_limit(request) is True
    assert check_login_rate_limit(request) is False


def test_login_rate_limit_is_per_ip():
    from app.auth import _LOGIN_MAX_ATTEMPTS, _login_attempts

    victim = _FakeRequest(client=_FakeClient(host="198.51.100.2"))
    attacker = _FakeRequest(client=_FakeClient(host="198.51.100.3"))
    _login_attempts.pop("198.51.100.2", None)
    _login_attempts.pop("198.51.100.3", None)

    for _ in range(_LOGIN_MAX_ATTEMPTS):
        assert check_login_rate_limit(attacker) is True
    assert check_login_rate_limit(attacker) is False
    # The attacker exhausting their own limit must not affect a different IP.
    assert check_login_rate_limit(victim) is True


def test_credential_encryption_round_trip():
    token = encrypt_credential("super-secret-provider-password")
    assert token != "super-secret-provider-password"
    assert decrypt_credential(token) == "super-secret-provider-password"


def test_credential_encryption_requires_secret_key():
    settings.SECRET_KEY = ""
    with pytest.raises(RuntimeError, match="SECRET_KEY"):
        encrypt_credential("whatever")
