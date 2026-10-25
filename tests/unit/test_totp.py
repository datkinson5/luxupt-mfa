"""TOTP codes match RFC 6238, and secrets are read the way authenticator setup screens show them."""

import pytest

from app.utils.totp import TotpSecretError, parse_secret, seconds_until_next_step, totp

# RFC 6238 Appendix B, SHA1: key is ASCII "12345678901234567890".
RFC_KEY = b"12345678901234567890"
RFC_KEY_B32 = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"


@pytest.mark.parametrize(
    ("at", "expected"),
    [
        (59, "94287082"),
        (1111111109, "07081804"),
        (1111111111, "14050471"),
        (1234567890, "89005924"),
        (2000000000, "69279037"),
        (20000000000, "65353130"),
    ],
)
def test_rfc6238_vectors(at: int, expected: str) -> None:
    assert totp(RFC_KEY, at=at, digits=8) == expected


def test_six_digit_code_is_the_low_digits() -> None:
    assert totp(RFC_KEY, at=59) == "287082"


@pytest.mark.parametrize(
    "secret",
    [
        RFC_KEY_B32,
        RFC_KEY_B32.lower(),
        "GEZD GNBV GY3T QOJQ GEZD GNBV GY3T QOJQ",
        "gezd-gnbv-gy3t-qojq-gezd-gnbv-gy3t-qojq",
        f"otpauth://totp/UI:svc@example.test?secret={RFC_KEY_B32}&issuer=UI",
    ],
)
def test_secret_formats_decode_to_the_same_key(secret: str) -> None:
    assert parse_secret(secret) == RFC_KEY


def test_unpadded_secret_is_padded() -> None:
    assert parse_secret("JBSWY3DPEE") == b"Hello!"


@pytest.mark.parametrize("secret", ["", "   ", "not*base32!", "otpauth://totp/x?issuer=UI"])
def test_bad_secret_is_refused(secret: str) -> None:
    with pytest.raises(TotpSecretError):
        parse_secret(secret)


def test_seconds_until_next_step() -> None:
    assert seconds_until_next_step(at=60) == 30
    assert seconds_until_next_step(at=89) == 1
