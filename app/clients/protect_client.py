"""Direct httpx client for UniFi Protect's private API.

Used for endpoints the public Integration API (X-API-KEY) does not expose:

- POST /api/auth/login                                                 (session cookies)
- GET  /proxy/protect/api/cameras/{id}/recording-snapshot?ts={ms}     (historical JPEG)
- GET  /proxy/protect/api/video/export?camera=...&start=...&end=...    (recorded MP4)
- GET  /proxy/protect/api/cameras/{id}/snapshot                       (live JPEG, full res)

`recording-snapshot` returns native channel-0 recording resolution — the result
LuxUPT's RTSP+ffmpeg path was built to deliver because Ubiquiti's public API is
resolution-capped on most camera models.

The client maintains a single httpx.AsyncClient with a cookie jar; on 401 it
re-logs in once and retries. The signed-in session is shared by every client
for the same console and account in this process, so parallel historical jobs
sign in once between them rather than once each.

Sign-in works with a local Protect user, or with a Ubiquiti (SSO) account that
has an authenticator app as its MFA method: give `totp_secret` and each login
sends the current code in the `token` field UniFi OS reads for MFA. Consoles in
a UniFi Fabric cannot have local users, so this is their only way in. It is intended to be long-lived (one instance per
app, injected via deps), not constructed per-request.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
import urllib3

from app.utils.async_fs import path_mkdir
from app.utils.totp import DEFAULT_STEP_SECONDS, parse_secret, totp

logger = logging.getLogger(__name__)

# Progress callbacks for streaming downloads.
# Args: (current_bytes, total_bytes_or_None). Must be async.
ProgressCallback = Callable[[int, int | None], Awaitable[None]]


class ProtectAuthError(RuntimeError):
    pass


class ProtectRequestError(RuntimeError):
    pass


class ProtectMfaRequiredError(ProtectAuthError):
    """The account needs a second factor that LuxUPT could not satisfy."""


# UniFi OS answers a login that still needs a second factor with HTTP 499, and
# names MFA in the body; some versions use 401/403 instead, so the body decides.
_MFA_STATUS_CODES = frozenset({400, 401, 403, 499})
_MFA_MARKERS = ("MFA", "2FA", "TWO_FACTOR", "AUTHENTICATOR")

# After a refused sign-in, further attempts fail at once for this long instead of
# reaching the console: every refusal counts towards UniFi's lockout, and a job
# retrying per frame would trip it in seconds. 429 is the lockout itself.
AUTH_BACKOFF_SECONDS = 300
LOCKOUT_BACKOFF_SECONDS = 900


class _SharedSession:
    """One console + account's sign-in, shared by every ProtectClient in the process.

    Holds the session cookies, the lock that serialises sign-ins, the last TOTP
    step sent (a code is accepted once, so a second sign-in in the same 30 s
    must wait for the next one), and the back-off after a refusal.
    """

    def __init__(self) -> None:
        self.cookies: httpx.Cookies | None = None
        self.lock = asyncio.Lock()
        self.last_totp_step = -1
        self.failed_until = 0.0
        self.failure = ""


# Keyed by the running event loop as well, so a lock is never shared across loops.
_SESSIONS: dict[tuple[int, str, str, str], _SharedSession] = {}


def _shared_session(host: str, username: str, password: str, secret: str) -> _SharedSession:
    loop = asyncio.get_running_loop()
    # The credentials are part of the key, so fixing a wrong password or secret
    # starts clean instead of waiting out the old back-off.
    fingerprint = hashlib.sha256(f"{password}\0{secret}".encode()).hexdigest()
    key = (id(loop), host, username, fingerprint)
    session = _SESSIONS.get(key)
    if session is None:
        for stale in [k for k in _SESSIONS if k[0] != id(loop)]:
            del _SESSIONS[stale]
        session = _SESSIONS[key] = _SharedSession()
    return session


def _reset_shared_sessions() -> None:
    """Forget every shared sign-in (tests)."""
    _SESSIONS.clear()


class ProtectClient:
    def __init__(
        self,
        base_url: str,
        username: str,
        password: str,
        *,
        totp_secret: str = "",
        verify_ssl: bool = False,
        timeout: float = 30.0,
    ) -> None:
        if not base_url:
            raise ValueError("base_url is required")
        if not username or not password:
            raise ValueError(
                "username and password are required for private-API access"
            )

        parsed = urlparse(base_url)
        if not parsed.scheme or not parsed.netloc:
            raise ValueError(
                f"base_url must include scheme and host (got: {base_url!r})"
            )
        self.host = f"{parsed.scheme}://{parsed.netloc}"

        self.username = username
        self.password = password
        # Decoded once so a bad secret fails at construction, not mid-job.
        self._totp_key = parse_secret(totp_secret) if totp_secret else None
        self._totp_secret = totp_secret
        self.verify_ssl = bool(verify_ssl)
        self.timeout = timeout

        if not verify_ssl:
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

        self._client: httpx.AsyncClient | None = None
        self._logged_in = False
        self._held: httpx.Cookies | None = None  # the shared session this client is using

    async def __aenter__(self) -> ProtectClient:
        await self.start()
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.close()

    async def start(self) -> None:
        if self._client is None:
            self._client = httpx.AsyncClient(
                verify=self.verify_ssl,
                timeout=httpx.Timeout(self.timeout, read=None),
                limits=httpx.Limits(max_keepalive_connections=20, max_connections=100),
                headers={"User-Agent": "LuxUPT/1.1"},
                follow_redirects=True,
            )
        shared = self._shared()
        if not self._logged_in and shared.cookies is not None:
            # Another client already signed in: reuse its session, no new login.
            self._client.cookies.update(shared.cookies)
            self._held = shared.cookies
            self._logged_in = True

    def _shared(self) -> _SharedSession:
        return _shared_session(self.host, self.username, self.password, self._totp_secret)

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None
        self._logged_in = False
        self._held = None

    async def _login(self, stale: httpx.Cookies | None = None) -> None:
        """Sign in via /api/auth/login, once for every client sharing this account.

        Sign-ins are serialised on a process-wide lock. A caller that waited finds
        the session another client established and adopts it. `stale` is the
        session the caller saw rejected: if the shared one is still that, sign in
        again; if it has already been replaced, adopt the replacement.

        With a TOTP secret the code goes in the first request, so the console never
        sends an email or push prompt. A code is never sent twice: a sign-in in the
        same 30 s step as the last one waits for the next code. If the console still
        answers with an MFA challenge, one retry follows with the next code.

        After a refusal, further sign-ins fail at once (see AUTH_BACKOFF_SECONDS)
        so a job does not hammer the console into locking the account.
        """
        shared = self._shared()
        async with shared.lock:
            assert self._client is not None
            if shared.cookies is not None and shared.cookies is not stale:
                self._client.cookies.update(shared.cookies)
                self._held = shared.cookies
                self._logged_in = True
                return
            remaining = shared.failed_until - time.monotonic()
            if remaining > 0:
                raise ProtectAuthError(
                    f"{shared.failure} (not signing in again for {remaining:.0f}s, so the "
                    "account is not locked out by repeated attempts)"
                )

            try:
                resp = await self._post_login(await self._login_payload(shared))
                if self._is_mfa_challenge(resp) and self._totp_key is not None:
                    logger.info(
                        "Protect asked for MFA; retrying with the next TOTP code",
                        extra={"host": self.host},
                    )
                    resp = await self._post_login(await self._login_payload(shared))
                self._raise_for_login(resp)
            except ProtectAuthError as exc:
                backoff = (
                    LOCKOUT_BACKOFF_SECONDS
                    if "429" in str(exc)
                    else AUTH_BACKOFF_SECONDS
                )
                shared.failed_until = time.monotonic() + backoff
                shared.failure = str(exc)
                shared.cookies = None
                logger.warning(
                    "Protect sign-in refused; backing off",
                    extra={"host": self.host, "user": self.username, "backoff_seconds": backoff},
                )
                raise

            shared.failed_until = 0.0
            shared.failure = ""
            shared.cookies = httpx.Cookies(self._client.cookies)
            self._held = shared.cookies
            self._logged_in = True
            logger.info(
                "Protect login successful",
                extra={
                    "host": self.host,
                    "user": self.username,
                    "mfa": self._totp_key is not None,
                },
            )

    def _raise_for_login(self, resp: httpx.Response) -> None:
        if self._is_mfa_challenge(resp):
            if self._totp_key is None:
                raise ProtectMfaRequiredError(
                    "login needs MFA: this is a Ubiquiti account with MFA "
                    "enabled. Set UNIFI_PROTECT_TOTP_SECRET to its "
                    "authenticator secret, or use a local Protect user."
                )
            raise ProtectMfaRequiredError(
                "login rejected the TOTP code: check UNIFI_PROTECT_TOTP_SECRET "
                "is this account's authenticator secret, that the authenticator "
                "app is its MFA method, and that the host clock is correct. "
                f"HTTP {resp.status_code} - {resp.text[:200]}"
            )
        if resp.status_code >= 400:
            raise ProtectAuthError(
                f"login failed: HTTP {resp.status_code} - {resp.text[:200]}"
            )

    async def _login_payload(self, shared: _SharedSession) -> dict[str, Any]:
        """The login body. With TOTP, the next unused code: waits out the current
        30 s step if its code was already sent, since a code is accepted once."""
        payload: dict[str, Any] = {
            "username": self.username,
            "password": self.password,
            "rememberMe": True,
        }
        if self._totp_key is not None:
            now = time.time()
            step = int(now // DEFAULT_STEP_SECONDS)
            if step <= shared.last_totp_step:
                wait = (shared.last_totp_step + 1) * DEFAULT_STEP_SECONDS - now + 1
                logger.info(
                    "Waiting for the next TOTP code (the current one was already used)",
                    extra={"host": self.host, "wait_seconds": round(wait, 1)},
                )
                await asyncio.sleep(wait)
                now = time.time()
                step = int(now // DEFAULT_STEP_SECONDS)
            payload["token"] = totp(self._totp_key, at=now)
            shared.last_totp_step = step
        return payload

    async def _post_login(self, payload: dict[str, Any]) -> httpx.Response:
        assert self._client is not None
        return await self._client.post(
            f"{self.host}/api/auth/login",
            json=payload,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
        )

    @staticmethod
    def _is_mfa_challenge(resp: httpx.Response) -> bool:
        if resp.status_code not in _MFA_STATUS_CODES:
            return False
        if resp.status_code == 499:
            return True
        body = resp.text.upper()
        return any(marker in body for marker in _MFA_MARKERS)

    async def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        """Issue a request, logging in on first call and once on 401."""
        await self.start()
        if not self._logged_in:
            await self._login()

        url = f"{self.host}{path}"
        assert self._client is not None
        resp = await self._client.request(method, url, **kwargs)
        if resp.status_code == 401:
            logger.info("Got 401, re-logging in", extra={"path": path})
            self._logged_in = False
            await self._login(stale=self._held)
            resp = await self._client.request(method, url, **kwargs)
        return resp

    @staticmethod
    def _to_js_ms(dt: datetime) -> int:
        return int(dt.timestamp() * 1000)

    async def get_all_camera_recording_ranges(
        self,
    ) -> dict[str, tuple[datetime, datetime]]:
        """Return {camera_id: (oldest, newest)} for every camera in one bootstrap call.

        Walks `bootstrap.cameras[].stats.video.recordingStart/End` (Unix ms)
        and converts to local-time datetimes. One ~161KB round-trip covers
        the whole NVR's cameras instead of N per-camera calls.
        """
        resp = await self._request(
            "GET",
            "/proxy/protect/api/bootstrap",
            headers={"Accept": "application/json"},
        )
        if resp.status_code >= 400:
            raise ProtectRequestError(
                f"bootstrap fetch failed: HTTP {resp.status_code} - {resp.text[:200]}"
            )
        data = resp.json()
        ranges: dict[str, tuple[datetime, datetime]] = {}
        for cam in data.get("cameras", []):
            cid = cam.get("id")
            video = (cam.get("stats") or {}).get("video") or {}
            start_ms = video.get("recordingStart")
            end_ms = video.get("recordingEnd")
            if not (cid and start_ms and end_ms):
                continue
            ranges[cid] = (
                datetime.fromtimestamp(start_ms / 1000, UTC),
                datetime.fromtimestamp(end_ms / 1000, UTC),
            )
        return ranges

    async def historical_snapshot(self, camera_id: str, ts: datetime) -> bytes:
        """Pull a JPEG from the recording stream at the given timestamp.

        Snaps to nearest GOP keyframe (~5s on most UniFi Protect cameras). Returned
        bytes are byte-identical for the same `ts` (deterministic).
        """
        path = f"/proxy/protect/api/cameras/{camera_id}/recording-snapshot"
        resp = await self._request(
            "GET",
            path,
            params={"ts": self._to_js_ms(ts)},
            headers={"Accept": "image/jpeg"},
        )
        if resp.status_code >= 400:
            raise ProtectRequestError(
                f"recording-snapshot failed for {camera_id} at {ts.isoformat()}: "
                f"HTTP {resp.status_code} - {resp.text[:200]}"
            )
        if not resp.headers.get("content-type", "").startswith("image/"):
            raise ProtectRequestError(
                f"recording-snapshot returned non-image content-type: {resp.headers.get('content-type')!r}"
            )
        return resp.content

    async def historical_video(
        self,
        camera_id: str,
        start: datetime,
        end: datetime,
        output_path: Path,
        *,
        channel: int = 0,
        progress_callback: ProgressCallback | None = None,
        chunk_size: int = 65536,
    ) -> None:
        """Stream a recorded MP4 for a range to disk.

        NOTE: do NOT pass `fps=N&type=timelapse` here — Protect's server-side
        timelapse renderer can't complete within the server's wall-time cutoff
        on this hardware. Use this for raw video export (Phase 2 event-manager
        use case) where Protect streams the on-disk MP4 directly (~45 MB/s).
        """
        await path_mkdir(output_path.parent, parents=True, exist_ok=True)
        path = "/proxy/protect/api/video/export"
        params: dict[str, Any] = {
            "camera": camera_id,
            "start": self._to_js_ms(start),
            "end": self._to_js_ms(end),
            "channel": channel,
        }

        await self.start()
        if not self._logged_in:
            await self._login()
        assert self._client is not None

        url = f"{self.host}{path}"
        async with self._client.stream(
            "GET", url, params=params, headers={"Accept": "video/mp4"}
        ) as resp:
            if resp.status_code == 401:
                await resp.aclose()
                self._logged_in = False
                await self._login(stale=self._held)
                async with self._client.stream(
                    "GET", url, params=params, headers={"Accept": "video/mp4"}
                ) as resp2:
                    await self._stream_to_file(
                        resp2, output_path, chunk_size, progress_callback
                    )
                return
            if resp.status_code >= 400:
                body = await resp.aread()
                raise ProtectRequestError(
                    f"video/export failed for {camera_id}: HTTP {resp.status_code} - {body[:200]!r}"
                )
            await self._stream_to_file(resp, output_path, chunk_size, progress_callback)

    @staticmethod
    async def _stream_to_file(
        resp: httpx.Response,
        output_path: Path,
        chunk_size: int,
        progress_callback: ProgressCallback | None,
    ) -> None:
        total = int(resp.headers.get("content-length", 0)) or None
        current = 0
        # Opened, written and closed off the loop. A video export is tens of megabytes, so a
        # synchronous write per chunk would freeze every concurrent request on this worker
        # for the whole download.
        f = await asyncio.to_thread(output_path.open, "wb")
        try:
            async for chunk in resp.aiter_bytes(chunk_size):
                await asyncio.to_thread(f.write, chunk)
                current += len(chunk)
                if progress_callback is not None:
                    await progress_callback(current, total)
        finally:
            await asyncio.to_thread(f.close)
