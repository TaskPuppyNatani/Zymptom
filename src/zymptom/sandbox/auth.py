"""Desktop OAuth with a bounded loopback listener and ephemeral Firebase tokens."""

from __future__ import annotations

import base64
import hashlib
import json
import re
import secrets
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from uuid import UUID

from zymptom.dictation.config import configuration_path


class AuthError(RuntimeError):
    def __init__(self, message: str, *, kind: str | None = None):
        super().__init__(message)
        self.kind = kind


class CredentialStoreError(AuthError):
    def __init__(
        self,
        message: str = "Persistent sign-in is unavailable because the system credential store could not be accessed.",
    ):
        super().__init__(message, kind="credential_store")


class HttpError(RuntimeError):
    def __init__(self, status: int | None):
        self.status = status
        super().__init__("The sandbox network request failed.")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        return None


def request_json(url: str, *, data: bytes | None = None, headers=None, method=None) -> dict:
    request = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
    try:
        with urllib.request.build_opener(_NoRedirect()).open(request, timeout=15) as response:
            raw = response.read(8_000_001)
        if len(raw) > 8_000_000:
            raise HttpError(None)
        result = json.loads(raw)
        if not isinstance(result, dict):
            raise HttpError(None)
        return result
    except urllib.error.HTTPError as exc:
        raise HttpError(exc.code) from None
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise HttpError(None) from None


@dataclass(frozen=True)
class DesktopConfig:
    firebase_api_key: str = field(repr=False)
    firebase_project_id: str
    google_client_id: str = field(repr=False)
    google_client_secret: str = field(default="", repr=False)


def production_config_path() -> Path:
    return configuration_path().parent / "firebase.json"


def config_path() -> Path:
    return configuration_path().parent / "sandbox-sync" / "firebase.json"


def read_config(path: Path | None = None) -> DesktopConfig:
    try:
        if path is None:
            candidate = production_config_path()
            target = candidate if candidate.is_file() else config_path()
        else:
            target = path
        data = json.loads(target.read_text(encoding="utf-8"))
        required = ("firebase_api_key", "firebase_project_id", "google_client_id")
        if not isinstance(data, dict) or any(not isinstance(data.get(key), str) or not data[key].strip() for key in required):
            raise ValueError()
        secret = data.get("google_client_secret", "")
        if not isinstance(secret, str):
            raise ValueError()
        return DesktopConfig(*(data[key].strip() for key in required), secret.strip())
    except (OSError, UnicodeError, ValueError, TypeError):
        raise AuthError("Sandbox OAuth configuration is missing or invalid. See the sandbox setup instructions.", kind="configuration") from None


def _clean_email(value) -> str | None:
    if (
        not isinstance(value, str)
        or len(value) > 254
        or "@" not in value
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
    ):
        return None
    return value


def _clean_refresh_token(value) -> str | None:
    if not isinstance(value, str):
        return None
    token = value.strip()
    if not token or len(token) > 4096 or any(ord(char) <= 32 or ord(char) == 127 for char in token):
        return None
    return token


@dataclass(frozen=True)
class FirebaseSession:
    uid: str
    id_token: str = field(repr=False)
    expires_at: float = field(repr=False)
    email: str | None = field(default=None, repr=False)
    refresh_token: str | None = field(default=None, repr=False)
    _refresher: object = field(default=None, repr=False, compare=False)

    def valid_token(self) -> str:
        if time.monotonic() >= self.expires_at or not self.id_token:
            if callable(self._refresher):
                refreshed = self._refresher()
                object.__setattr__(self, "id_token", refreshed.id_token)
                object.__setattr__(self, "expires_at", refreshed.expires_at)
                if refreshed.refresh_token:
                    object.__setattr__(self, "refresh_token", refreshed.refresh_token)
                return self.id_token
            raise AuthError("Sandbox sign-in expired. Sign in again.", kind="auth")
        return self.id_token


def firebase_exchange(config: DesktopConfig, google_id_token: str, *, request=request_json) -> FirebaseSession:
    body = {"requestUri": "http://127.0.0.1", "postBody": urllib.parse.urlencode({"id_token": google_id_token, "providerId": "google.com"}), "returnSecureToken": True}
    url = "https://identitytoolkit.googleapis.com/v1/accounts:signInWithIdp?" + urllib.parse.urlencode({"key": config.firebase_api_key})
    try:
        result = request(url, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
        uid, token, expires = result.get("localId"), result.get("idToken"), int(result.get("expiresIn", 0))
        if not isinstance(uid, str) or not uid or "/" in uid or not isinstance(token, str) or not token or expires <= 0:
            raise ValueError()
        email = _clean_email(result.get("email"))
        refresh_token = _clean_refresh_token(result.get("refreshToken"))
        return FirebaseSession(uid, token, time.monotonic() + expires, email, refresh_token=refresh_token)
    except HttpError as exc:
        kind = "offline" if exc.status is None or exc.status >= 500 else "signin"
        raise AuthError("Firebase sign-in failed or returned an invalid response. Local entries remain available.", kind=kind) from None
    except (ValueError, TypeError):
        raise AuthError("Firebase sign-in failed or returned an invalid response. Local entries remain available.", kind="signin") from None


def refresh_firebase_session(
    config: DesktopConfig,
    refresh_token: str,
    *,
    expected_uid: str | None = None,
    email: str | None = None,
    request=None,
) -> FirebaseSession:
    clean_refresh = _clean_refresh_token(refresh_token)
    if not clean_refresh:
        raise AuthError("Sandbox sign-in expired. Sign in again.", kind="auth")
    requester = request or request_json
    body = urllib.parse.urlencode({"grant_type": "refresh_token", "refresh_token": clean_refresh}).encode("ascii")
    url = "https://securetoken.googleapis.com/v1/token?" + urllib.parse.urlencode({"key": config.firebase_api_key})
    try:
        result = requester(url, data=body, headers={"Content-Type": "application/x-www-form-urlencoded"})
        uid = result.get("user_id") or result.get("localId")
        token = result.get("id_token") or result.get("idToken")
        expires = int(result.get("expires_in") or result.get("expiresIn") or 0)
        rotated = _clean_refresh_token(result.get("refresh_token") or result.get("refreshToken")) or clean_refresh
        if expected_uid is not None and uid != expected_uid:
            raise AuthError("Saved sign-in belongs to a different account.", kind="auth")
        if (
            not isinstance(uid, str)
            or not re.fullmatch(r"[A-Za-z0-9:_-]{1,128}", uid)
            or not isinstance(token, str)
            or not token.strip()
            or expires <= 0
        ):
            raise ValueError()
        resolved_email = _clean_email(result.get("email")) or _clean_email(email)
        return FirebaseSession(uid, token.strip(), time.monotonic() + expires, resolved_email, refresh_token=rotated)
    except HttpError as exc:
        kind = "offline" if exc.status is None or exc.status >= 500 else "auth"
        raise AuthError("Sandbox sign-in expired. Sign in again.", kind=kind) from None
    except (ValueError, TypeError, AttributeError):
        raise AuthError("Sandbox sign-in expired. Sign in again.", kind="auth") from None


class SecretToolCredentialStore:
    def __init__(self, *, binary: str = "secret-tool", runner=subprocess.run, timeout: float = 5.0):
        self.binary = binary
        self.runner = runner
        self.timeout = timeout

    @staticmethod
    def _attributes(scope: str, project_id: str, journal_id: str) -> list[str]:
        return [
            "application", "zymptom",
            "service", "zymptom-sync",
            "scope", scope,
            "project_id", project_id,
            "journal_id", journal_id,
        ]

    def load(self, scope: str, project_id: str, journal_id: str) -> str | None:
        cmd = [self.binary, "lookup", *self._attributes(scope, project_id, journal_id)]
        try:
            result = self.runner(cmd, capture_output=True, text=True, timeout=self.timeout, check=False)
        except (OSError, subprocess.SubprocessError):
            raise CredentialStoreError() from None
        if result.returncode == 0:
            value = (result.stdout or "").strip()
            return value or None
        if result.returncode == 1 and not (result.stderr or "").strip():
            return None
        raise CredentialStoreError()

    def save(self, scope: str, project_id: str, journal_id: str, payload: str) -> None:
        cmd = [
            self.binary,
            "store",
            f"--label=Zymptom Sync ({scope})",
            *self._attributes(scope, project_id, journal_id),
        ]
        try:
            result = self.runner(
                cmd,
                input=payload,
                capture_output=True,
                text=True,
                timeout=self.timeout,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            raise CredentialStoreError() from None
        if result.returncode != 0:
            raise CredentialStoreError()

    def delete(self, scope: str, project_id: str, journal_id: str) -> None:
        # lookup unlocks a matching item when necessary; clear only removes
        # unlocked items, so verify that no credential remains afterwards.
        if self.load(scope, project_id, journal_id) is None:
            return
        cmd = [self.binary, "clear", *self._attributes(scope, project_id, journal_id)]
        try:
            result = self.runner(cmd, capture_output=True, text=True, timeout=self.timeout, check=False)
        except (OSError, subprocess.SubprocessError):
            raise CredentialStoreError() from None
        if result.returncode != 0:
            raise CredentialStoreError()
        if self.load(scope, project_id, journal_id) is not None:
            raise CredentialStoreError()


class MemoryCredentialStore:
    def __init__(self):
        self.items: dict[tuple[str, str, str], str] = {}

    def load(self, scope: str, project_id: str, journal_id: str) -> str | None:
        return self.items.get((scope, project_id, journal_id))

    def save(self, scope: str, project_id: str, journal_id: str, payload: str) -> None:
        self.items[(scope, project_id, journal_id)] = payload

    def delete(self, scope: str, project_id: str, journal_id: str) -> None:
        self.items.pop((scope, project_id, journal_id), None)


_DEFAULT_CREDENTIAL_STORE = SecretToolCredentialStore()


def default_credential_store():
    return _DEFAULT_CREDENTIAL_STORE


def _normalize_credential_binding(scope: str, project_id: str, journal_id: UUID | str | None) -> tuple[str, str, str]:
    if scope not in {"production", "sandbox"}:
        raise ValueError("Invalid sync credential scope.")
    if not isinstance(project_id, str) or not re.fullmatch(r"[a-z][a-z0-9-]{4,28}[a-z0-9]", project_id):
        raise ValueError("Invalid Firebase project identity.")
    if scope == "production":
        if journal_id is None:
            raise ValueError("Production credential requires an enrolled journal identity.")
        parsed = UUID(str(journal_id))
        if str(parsed) != str(journal_id) or parsed.int == 0:
            raise ValueError("Invalid production journal identity.")
        return scope, project_id, str(parsed)
    if journal_id not in (None, "sandbox"):
        raise ValueError("Sandbox credential cannot bind to a production journal identity.")
    return scope, project_id, "sandbox"


def save_persisted_session(
    config: DesktopConfig,
    session: FirebaseSession,
    *,
    scope: str = "production",
    journal_id: UUID | str | None = None,
    store=None,
) -> bool:
    refresh_token = _clean_refresh_token(session.refresh_token)
    if not refresh_token:
        return False
    normalized_scope, project_id, normalized_journal = _normalize_credential_binding(
        scope, config.firebase_project_id, journal_id
    )
    if not isinstance(session.uid, str) or not re.fullmatch(r"[A-Za-z0-9:_-]{1,128}", session.uid):
        raise ValueError("Invalid Firebase account identity.")
    target_store = store if store is not None else default_credential_store()
    payload = json.dumps(
        {
            "schema_version": 1,
            "scope": normalized_scope,
            "firebase_project_id": project_id,
            "journal_id": normalized_journal,
            "uid": session.uid,
            "email": _clean_email(session.email),
            "refresh_token": refresh_token,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    target_store.save(normalized_scope, project_id, normalized_journal, payload)
    return True


def clear_persisted_session(
    config: DesktopConfig | str,
    *,
    scope: str = "production",
    journal_id: UUID | str | None = None,
    store=None,
) -> None:
    project_id = config.firebase_project_id if isinstance(config, DesktopConfig) else str(config)
    normalized_scope, normalized_project, normalized_journal = _normalize_credential_binding(
        scope, project_id, journal_id
    )
    target_store = store if store is not None else default_credential_store()
    target_store.delete(normalized_scope, normalized_project, normalized_journal)


def restore_persisted_session(
    config: DesktopConfig,
    *,
    scope: str = "production",
    journal_id: UUID | str | None = None,
    store=None,
    request=None,
) -> FirebaseSession | None:
    try:
        normalized_scope, project_id, normalized_journal = _normalize_credential_binding(
            scope, config.firebase_project_id, journal_id
        )
    except (ValueError, TypeError):
        return None
    target_store = store if store is not None else default_credential_store()
    raw = target_store.load(normalized_scope, project_id, normalized_journal)
    if raw is None:
        return None
    try:
        data = json.loads(raw)
        required = {
            "schema_version",
            "scope",
            "firebase_project_id",
            "journal_id",
            "uid",
            "email",
            "refresh_token",
        }
        if (
            not isinstance(data, dict)
            or set(data) != required
            or data["schema_version"] != 1
            or data["scope"] != normalized_scope
            or data["firebase_project_id"] != project_id
            or data["journal_id"] != normalized_journal
            or not isinstance(data["uid"], str)
            or not re.fullmatch(r"[A-Za-z0-9:_-]{1,128}", data["uid"])
        ):
            raise ValueError()
        refresh_token = _clean_refresh_token(data["refresh_token"])
        if not refresh_token:
            raise ValueError()
        raw_email = data["email"]
        email = _clean_email(raw_email) if raw_email is not None else None
        if raw_email is not None and email is None:
            raise ValueError()
        uid = data["uid"]
    except (ValueError, TypeError):
        return None

    state = {"refresh_token": refresh_token}

    def _refresh() -> FirebaseSession:
        refreshed = refresh_firebase_session(
            config,
            state["refresh_token"],
            expected_uid=uid,
            email=email,
            request=request,
        )
        if refreshed.refresh_token and refreshed.refresh_token != state["refresh_token"]:
            state["refresh_token"] = refreshed.refresh_token
            try:
                save_persisted_session(
                    config,
                    refreshed,
                    scope=normalized_scope,
                    journal_id=normalized_journal,
                    store=target_store,
                )
            except CredentialStoreError:
                pass
        return FirebaseSession(
            refreshed.uid,
            refreshed.id_token,
            refreshed.expires_at,
            refreshed.email,
            refresh_token=state["refresh_token"],
            _refresher=_refresh,
        )

    return FirebaseSession(
        uid, "", 0.0, email, refresh_token=state["refresh_token"], _refresher=_refresh,
    )


def authorization_url(config: DesktopConfig, redirect_uri: str, state: str, verifier: str) -> str:
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode("ascii")
    params = {"client_id": config.google_client_id, "redirect_uri": redirect_uri, "response_type": "code",
              "scope": "openid", "state": state, "code_challenge": challenge, "code_challenge_method": "S256"}
    return "https://accounts.google.com/o/oauth2/v2/auth?" + urllib.parse.urlencode(params)


def sign_in(config: DesktopConfig, *, cancel: threading.Event | None = None, timeout: float = 180,
            browser_open=webbrowser.open, request=request_json) -> FirebaseSession:
    cancel = cancel or threading.Event()
    state = secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(64)
    callback = {}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_GET(self):
            parsed = urllib.parse.urlsplit(self.path)
            values = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
            if parsed.path != "/callback":
                self.send_error(404)
                return
            valid_state = len(values.get("state", [])) == 1 and secrets.compare_digest(values["state"][0].encode("utf-8"), state.encode("ascii"))
            if not valid_state:
                callback["error"] = "Invalid OAuth state. Sign in again."
            elif values.get("error"):
                callback["error"] = "Google sign-in was cancelled or denied."
            elif len(values.get("code", [])) != 1 or not values["code"][0]:
                callback["error"] = "Google returned an invalid sign-in callback."
            else:
                callback["code"] = values["code"][0]
            body = b"Sign-in response received. Return to Zymptom."
            self.send_response(200 if "code" in callback else 400)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    class Listener(HTTPServer):
        def get_request(self):
            sock, address = super().get_request()
            sock.settimeout(2)
            return sock, address

    try:
        if cancel.is_set():
            raise AuthError("Google sign-in was cancelled or timed out. You can retry.")
        server = Listener(("127.0.0.1", 0), Handler)
        server.timeout = 0.25
        with server:
            redirect = f"http://127.0.0.1:{server.server_port}/callback"
            if not browser_open(authorization_url(config, redirect, state, verifier)):
                raise AuthError("Could not open the system browser for Google sign-in.")
            deadline = time.monotonic() + timeout
            while not callback and not cancel.is_set() and time.monotonic() < deadline:
                server.handle_request()
            if cancel.is_set() or not callback:
                raise AuthError("Google sign-in was cancelled or timed out. You can retry.")
            if "error" in callback:
                raise AuthError(callback["error"])
            body = {"code": callback["code"], "client_id": config.google_client_id, "redirect_uri": redirect,
                    "grant_type": "authorization_code", "code_verifier": verifier}
            if config.google_client_secret:
                body["client_secret"] = config.google_client_secret
            result = request("https://oauth2.googleapis.com/token", data=urllib.parse.urlencode(body).encode(),
                             headers={"Content-Type": "application/x-www-form-urlencoded"})
            google_token = result.get("id_token")
            if not isinstance(google_token, str) or not google_token:
                raise AuthError("Google sign-in returned an invalid token response.")
            if cancel.is_set():
                raise AuthError("Google sign-in was cancelled. You can retry.")
            return firebase_exchange(config, google_token, request=request)
    except HttpError as exc:
        kind = "offline" if exc.status is None or exc.status >= 500 else "signin"
        raise AuthError("Google sign-in could not reach the authentication service. You can retry.", kind=kind) from None
    except OSError:
        raise AuthError("Could not start the local sign-in listener. You can retry.") from None
