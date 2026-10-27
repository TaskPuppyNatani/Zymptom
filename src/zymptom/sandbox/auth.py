"""Desktop OAuth with a bounded loopback listener and ephemeral Firebase tokens."""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from zymptom.dictation.config import configuration_path


class AuthError(RuntimeError):
    pass


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


def config_path() -> Path:
    return configuration_path().parent / "sandbox-sync" / "firebase.json"


def read_config(path: Path | None = None) -> DesktopConfig:
    try:
        data = json.loads((path or config_path()).read_text(encoding="utf-8"))
        required = ("firebase_api_key", "firebase_project_id", "google_client_id")
        if not isinstance(data, dict) or any(not isinstance(data.get(key), str) or not data[key].strip() for key in required):
            raise ValueError()
        secret = data.get("google_client_secret", "")
        if not isinstance(secret, str):
            raise ValueError()
        return DesktopConfig(*(data[key].strip() for key in required), secret.strip())
    except (OSError, UnicodeError, ValueError, TypeError):
        raise AuthError("Sandbox OAuth configuration is missing or invalid. See the sandbox setup instructions.") from None


@dataclass(frozen=True)
class FirebaseSession:
    uid: str
    id_token: str = field(repr=False)
    expires_at: float = field(repr=False)

    def valid_token(self) -> str:
        if time.monotonic() >= self.expires_at:
            raise AuthError("Sandbox sign-in expired. Sign in again.")
        return self.id_token


def firebase_exchange(config: DesktopConfig, google_id_token: str, *, request=request_json) -> FirebaseSession:
    body = {"requestUri": "http://127.0.0.1", "postBody": urllib.parse.urlencode({"id_token": google_id_token, "providerId": "google.com"}), "returnSecureToken": True}
    url = "https://identitytoolkit.googleapis.com/v1/accounts:signInWithIdp?" + urllib.parse.urlencode({"key": config.firebase_api_key})
    try:
        result = request(url, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
        uid, token, expires = result.get("localId"), result.get("idToken"), int(result.get("expiresIn", 0))
        if not isinstance(uid, str) or not uid or "/" in uid or not isinstance(token, str) or not token or expires <= 0:
            raise ValueError()
        return FirebaseSession(uid, token, time.monotonic() + expires)
    except (HttpError, ValueError, TypeError):
        raise AuthError("Firebase sign-in failed or returned an invalid response. Local entries remain available.") from None


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
    except HttpError:
        raise AuthError("Google sign-in could not reach the authentication service. You can retry.") from None
    except OSError:
        raise AuthError("Could not start the local sign-in listener. You can retry.") from None
