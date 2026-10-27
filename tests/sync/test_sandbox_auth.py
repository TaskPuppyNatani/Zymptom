import base64
import hashlib
import json
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from zymptom.sandbox.auth import AuthError, DesktopConfig, FirebaseSession, HttpError, authorization_url, firebase_exchange, request_json, sign_in


CONFIG = DesktopConfig("web-key", "demo-project", "desktop-client", "local-secret")


def test_authorization_url_uses_state_pkce_and_minimal_scope():
    values = urllib.parse.parse_qs(urllib.parse.urlsplit(authorization_url(CONFIG, "http://127.0.0.1:123/callback", "state", "verifier")).query)
    expected = base64.urlsafe_b64encode(hashlib.sha256(b"verifier").digest()).rstrip(b"=").decode()
    assert values["state"] == ["state"]
    assert values["scope"] == ["openid"]
    assert values["code_challenge_method"] == ["S256"]
    assert values["code_challenge"] == [expected]
    assert "client_secret" not in values


def test_loopback_success_exchanges_code_and_never_logs_tokens(caplog):
    captured = []
    def browser(url):
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
        def callback():
            url = query["redirect_uri"][0] + "?" + urllib.parse.urlencode({"state": query["state"][0], "code": "private-code"})
            with urllib.request.urlopen(url, timeout=2) as response:
                assert response.status == 200
        threading.Thread(target=callback, daemon=True).start()
        captured.append(query)
        return True
    def request(url, *, data, headers):
        assert headers
        if "oauth2.googleapis.com" in url:
            values = urllib.parse.parse_qs(data.decode())
            challenge = base64.urlsafe_b64encode(hashlib.sha256(values["code_verifier"][0].encode()).digest()).rstrip(b"=").decode()
            assert challenge == captured[0]["code_challenge"][0]
            assert values["code"] == ["private-code"]
            return {"id_token": "private-google-token"}
        body = json.loads(data)
        assert urllib.parse.parse_qs(body["postBody"]) == {"id_token": ["private-google-token"], "providerId": ["google.com"]}
        assert body["returnSecureToken"] is True
        return {"idToken": "private-firebase-token", "localId": "test-uid", "expiresIn": "3600", "refreshToken": "private-refresh"}
    session = sign_in(CONFIG, browser_open=browser, request=request, timeout=3)
    assert session.uid == "test-uid" and session.valid_token() == "private-firebase-token"
    assert "private" not in repr(session) and "private" not in caplog.text


@pytest.mark.parametrize("kind", ["state", "unicode-state", "cancelled", "malformed"])
def test_bad_loopback_callbacks_are_rejected(kind):
    def browser(url):
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
        args = {"state": "wrong" if kind == "state" else "雪" if kind == "unicode-state" else query["state"][0]}
        if kind == "cancelled":
            args["error"] = "access_denied"
        def callback():
            try:
                urllib.request.urlopen(query["redirect_uri"][0] + "?" + urllib.parse.urlencode(args), timeout=2)
            except urllib.error.HTTPError:
                pass
        threading.Thread(target=callback, daemon=True).start()
        return True
    with pytest.raises(AuthError):
        sign_in(CONFIG, browser_open=browser, timeout=3)


def test_timeout_and_cancellation_do_not_leave_auth_state():
    with pytest.raises(AuthError, match="timed out"):
        sign_in(CONFIG, browser_open=lambda _: True, timeout=0.01)
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(AuthError, match="cancelled"):
        sign_in(CONFIG, browser_open=lambda _: True, cancel=cancel)


def test_firebase_exchange_requires_valid_response_and_enforces_expiration():
    with pytest.raises(AuthError):
        firebase_exchange(CONFIG, "google-token", request=lambda *_a, **_k: {"idToken": "token"})
    def fail(*_a, **_k):
        raise HttpError(403)
    with pytest.raises(AuthError):
        firebase_exchange(CONFIG, "google-token", request=fail)
    with pytest.raises(AuthError, match="expired"):
        FirebaseSession("uid", "secret", time.monotonic() - 1).valid_token()


def test_auth_and_transport_import_without_unix_fcntl():
    script = """
import builtins
original = builtins.__import__
def guarded(name, *args, **kwargs):
    if name == 'fcntl':
        raise ImportError('not available')
    return original(name, *args, **kwargs)
builtins.__import__ = guarded
import zymptom.sandbox.auth
import zymptom.sandbox.transport
"""
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_authenticated_http_does_not_follow_redirects():
    paths = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass
        def do_GET(self):
            paths.append(self.path)
            self.send_response(302)
            self.send_header("Location", "/target")
            self.end_headers()
    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with pytest.raises(HttpError) as error:
            request_json(f"http://127.0.0.1:{server.server_port}/start", headers={"Authorization": "Bearer private-token"})
        assert error.value.status == 302
        assert paths == ["/start"]
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
