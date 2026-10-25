#!/usr/bin/env python3
"""Check that a Ubiquiti account with an authenticator app can sign in to a Protect console.

Standard library only, so it runs on any host with Python 3.11+ — no LuxUPT install needed.
It signs in the way LuxUPT does (TOTP code in the login's `token` field), reports what the
console answered, and optionally pulls one recording snapshot to prove the session reaches
the recordings API.

    UNIFI_PROTECT_USERNAME=svc@example.com \\
    python3 scripts/protect_mfa_login_check.py https://192.168.1.1 --camera <camera id>

To finish enrolling the authenticator (UniFi asks for a code from it), print the current
code from the secret key instead of using a phone app:

    python3 scripts/protect_mfa_login_check.py --code

The password and TOTP secret are read from UNIFI_PROTECT_PASSWORD / UNIFI_PROTECT_TOTP_SECRET,
or prompted for without echo. Nothing secret is printed.
"""

from __future__ import annotations

import argparse
import base64
import getpass
import hashlib
import hmac
import http.cookiejar
import json
import os
import ssl
import struct
import sys
import time
import urllib.error
import urllib.request
from urllib.parse import parse_qs, urlparse


def totp_code(secret: str, at: float | None = None) -> str:
    value = secret.strip()
    if value.lower().startswith("otpauth://"):
        value = (parse_qs(urlparse(value).query).get("secret") or [""])[0]
    value = value.replace(" ", "").replace("-", "").upper()
    try:
        key = base64.b32decode(value + "=" * (-len(value) % 8))
    except ValueError:
        key = b""
    if not key:
        hint = (
            "That looks like a one-time code from the app, not the secret key."
            if value.isdigit()
            else "It should be letters A-Z and digits 2-7 only (spaces are fine)."
        )
        raise SystemExit(
            f"The TOTP secret isn't a valid authenticator key ({len(value)} characters entered). "
            f"{hint}\nUse the key shown under the QR code when the authenticator was set up. "
            "If you can't see it any more, remove and re-add the authenticator on the account. "
            "In PowerShell, paste at the hidden prompt with a right-click."
        )
    counter = int((time.time() if at is None else at) // 30)
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    return f"{(struct.unpack('>I', digest[offset:offset + 4])[0] & 0x7FFFFFFF) % 1_000_000:06d}"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("console", nargs="?", help="Console URL, e.g. https://192.168.1.1")
    parser.add_argument("--code", action="store_true", help="Just print the current 6-digit code for the secret, then exit")
    parser.add_argument("--camera", help="Camera id: also fetch one recording snapshot")
    parser.add_argument("--no-token", action="store_true", help="Sign in without a code, to see the raw MFA challenge")
    parser.add_argument("--verify-ssl", action="store_true", help="Verify the console's TLS certificate")
    args = parser.parse_args()

    if args.code:
        secret = os.getenv("UNIFI_PROTECT_TOTP_SECRET") or getpass.getpass("TOTP secret: ")
        now = time.time()
        print(f"Code: {totp_code(secret, now)}  (valid for another {30 - now % 30:.0f} s)")
        print(f"Next: {totp_code(secret, now + 30)}")
        return 0
    if not args.console:
        parser.error("the console URL is required unless --code is given")

    host = f"{urlparse(args.console).scheme}://{urlparse(args.console).netloc}"
    username = os.getenv("UNIFI_PROTECT_USERNAME") or input("Username (email): ")
    password = os.getenv("UNIFI_PROTECT_PASSWORD") or getpass.getpass("Password: ")
    secret = "" if args.no_token else (
        os.getenv("UNIFI_PROTECT_TOTP_SECRET") or getpass.getpass("TOTP secret: ")
    )

    context = ssl.create_default_context() if args.verify_ssl else ssl._create_unverified_context()
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(
        urllib.request.HTTPSHandler(context=context),
        urllib.request.HTTPCookieProcessor(jar),
    )

    payload: dict[str, object] = {"username": username, "password": password, "rememberMe": True}
    if secret:
        payload["token"] = totp_code(secret)
        print(f"Code valid for another {30 - time.time() % 30:.0f} s")

    request = urllib.request.Request(
        f"{host}/api/auth/login",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    try:
        with opener.open(request, timeout=30) as resp:
            status, body = resp.status, resp.read().decode(errors="replace")
    except urllib.error.HTTPError as exc:
        status, body = exc.code, exc.read().decode(errors="replace")

    print(f"Login: HTTP {status}")
    try:
        parsed = json.loads(body)
        # Show the shape of the answer, never its contents: a success body describes the account.
        print("Body keys:", sorted(parsed) if isinstance(parsed, dict) else type(parsed).__name__)
        if status >= 400:
            print("Body:", json.dumps(parsed)[:500])
    except ValueError:
        print("Body (not JSON):", body[:300])
    print("Cookies set:", sorted(c.name for c in jar) or "none")

    if status >= 400:
        print("\nSign-in failed. With a TOTP secret, check it is this account's authenticator key,")
        print("that the authenticator app is its MFA method, and that this host's clock is right.")
        return 1
    print("\nSigned in.")

    if args.camera:
        ts = int((time.time() - 120) * 1000)
        url = f"{host}/proxy/protect/api/cameras/{args.camera}/recording-snapshot?ts={ts}"
        try:
            with opener.open(urllib.request.Request(url, headers={"Accept": "image/jpeg"}), timeout=60) as resp:
                data = resp.read()
                kind = resp.headers.get("content-type")
        except urllib.error.HTTPError as exc:
            print(f"Recording snapshot: HTTP {exc.code} - {exc.read()[:200]!r}")
            return 1
        out = f"recording-snapshot-{args.camera}.jpg"
        with open(out, "wb") as fh:
            fh.write(data)
        print(f"Recording snapshot: {len(data):,} bytes ({kind}) saved to {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
