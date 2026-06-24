import pytest

from app.auth import (
    decrypt_credential,
    encrypt_credential,
    hash_password,
    verify_password,
)
from app.config import settings


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


def test_credential_encryption_round_trip():
    token = encrypt_credential("super-secret-provider-password")
    assert token != "super-secret-provider-password"
    assert decrypt_credential(token) == "super-secret-provider-password"


def test_credential_encryption_requires_secret_key():
    settings.SECRET_KEY = ""
    with pytest.raises(RuntimeError, match="SECRET_KEY"):
        encrypt_credential("whatever")
