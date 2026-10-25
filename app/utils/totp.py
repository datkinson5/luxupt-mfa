"""RFC 6238 time-based one-time passwords, standard library only.

Used to answer UniFi OS's MFA prompt when LuxUPT signs in to Protect with a
Ubiquiti (SSO) account instead of a local user — the only kind of account a
console in a UniFi Fabric can have. The secret is the base32 key behind the
authenticator-app QR code; an ``otpauth://`` URI is accepted as well.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import struct
import time
from urllib.parse import parse_qs, urlparse

DEFAULT_STEP_SECONDS = 30
DEFAULT_DIGITS = 6


class TotpSecretError(ValueError):
    """The configured TOTP secret is not a usable base32 key."""


def parse_secret(secret: str) -> bytes:
    """Decode a base32 TOTP secret (or an ``otpauth://`` URI holding one) to raw key bytes.

    Spaces, dashes and lower case are tolerated because authenticator setup
    screens display the key grouped and in mixed case.
    """
    value = (secret or "").strip()
    if value.lower().startswith("otpauth://"):
        params = parse_qs(urlparse(value).query)
        value = (params.get("secret") or [""])[0]
    value = value.replace(" ", "").replace("-", "").upper()
    if not value:
        raise TotpSecretError("TOTP secret is empty")
    value += "=" * (-len(value) % 8)
    try:
        key = base64.b32decode(value)
    except binascii.Error as exc:
        raise TotpSecretError("TOTP secret is not valid base32") from exc
    if not key:
        raise TotpSecretError("TOTP secret is empty")
    return key


def totp(
    key: bytes,
    *,
    at: float | None = None,
    step: int = DEFAULT_STEP_SECONDS,
    digits: int = DEFAULT_DIGITS,
) -> str:
    """The HMAC-SHA1 TOTP code for ``key`` at unix time ``at`` (now if omitted)."""
    now = time.time() if at is None else at
    counter = int(now // step)
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    code = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF
    return str(code % (10**digits)).zfill(digits)


def seconds_until_next_step(
    *, at: float | None = None, step: int = DEFAULT_STEP_SECONDS
) -> float:
    """How long until the current code expires and the next one starts."""
    now = time.time() if at is None else at
    return step - (now % step)
