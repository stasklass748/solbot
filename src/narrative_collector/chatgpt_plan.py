"""Sign in with ChatGPT for a user-operated, open-source installation.

This module never reads ChatGPT browser cookies or an API key. OAuth credentials
are saved outside the repository and are only used after interactive consent.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import secrets
import threading
import urllib.error
import urllib.parse
import urllib.request
import uuid
import webbrowser
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

AUTH_URL = "https://auth.openai.com/api/accounts/authorize"
TOKEN_URL = "https://auth.openai.com/api/accounts/oauth/token"
DISCOVERY_URL = "https://auth.openai.com/.well-known/openid-configuration"
RESOURCE = "https://api.openai.com/v1"
SCOPES = "openid profile email offline_access resource.invoke chatgpt.tokens.use.direct"
AGENT_NAME = "Meme Narrative Collector"


def default_credentials_path() -> Path:
    return Path.home() / ".config" / "meme-narrative-collector" / "chatgpt-credentials.json"


def _read(path: Path) -> dict:
    if not path.exists():
        return {"version": 1, "active_client_id": "", "accounts": {}}
    if os.name == "posix" and path.stat().st_mode & 0o077:
        raise PermissionError("ChatGPT credential file must be owner-only (chmod 600)")
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("version") != 1 or not isinstance(data.get("accounts"), dict):
        raise ValueError("Unsupported ChatGPT credential file")
    return data


@contextmanager
def _credential_lock(path: Path):
    """Serialize refreshes across workers without placing tokens in the lock."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    lock_path = path.with_name(path.name + ".lock")
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        if os.name == "nt":
            import msvcrt
            if os.fstat(fd).st_size == 0:
                os.write(fd, b"0")
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
        else:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            yield
        finally:
            if os.name == "nt":
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def _save(path: Path, data: dict) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    candidate = path.with_name(path.name + "." + secrets.token_hex(8) + ".tmp")
    fd = os.open(candidate, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(candidate, path)
    finally:
        candidate.unlink(missing_ok=True)
    if os.name == "posix":
        path.chmod(0o600)


def host_id_path(credentials: Path) -> Path:
    return credentials.with_name("chatgpt-host-id")


def ensure_host_id(credentials: Path) -> str:
    path = host_id_path(credentials)
    if path.exists():
        return _host_id(credentials)
    value = "urn:uuid:" + str(uuid.uuid4())
    _save(path, {"host_id": value})
    # The JSON wrapper makes creation atomic; use the same format on every read.
    return value


def _host_id(credentials: Path) -> str:
    path = host_id_path(credentials)
    if path.exists():
        data = json.loads(path.read_text(encoding="utf-8"))
        value = data.get("host_id", "")
        if not isinstance(value, str) or not value.startswith("urn:uuid:"):
            raise ValueError("Invalid stored ChatGPT host ID")
        uuid.UUID(value[len("urn:uuid:"):])
        return value
    return ensure_host_id(credentials)


def _form_post(url: str, values: dict[str, str]) -> dict:
    request = urllib.request.Request(
        url, data=urllib.parse.urlencode(values).encode("ascii"), method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"ChatGPT authorization failed (HTTP {exc.code})") from None


def _validate_id_token(token: str, client_id: str, nonce: str) -> dict:
    import jwt

    with urllib.request.urlopen(DISCOVERY_URL, timeout=15) as response:
        discovery = json.load(response)
    if discovery.get("issuer") != "https://auth.openai.com":
        raise ValueError("Unexpected ChatGPT identity issuer")
    jwks_uri = discovery.get("jwks_uri", "")
    if not isinstance(jwks_uri, str) or not jwks_uri.startswith(
        "https://auth.openai.com/"
    ):
        raise ValueError("Unexpected ChatGPT signing-key endpoint")
    jwk_client = jwt.PyJWKClient(jwks_uri)
    key = jwk_client.get_signing_key_from_jwt(token).key
    claims = jwt.decode(
        token, key, algorithms=["RS256", "ES256"], audience=client_id,
        issuer=discovery["issuer"], leeway=5,
        options={"require": ["sub", "exp", "iat"]},
    )
    if claims.get("nonce") != nonce or not isinstance(claims.get("sub"), str):
        raise ValueError("ChatGPT identity nonce or subject mismatch")
    return claims


def _require_grant(tokens: dict) -> None:
    scopes = set(str(tokens.get("scope", "")).split())
    if "chatgpt.tokens.use.direct" not in scopes or "offline_access" not in scopes:
        raise ValueError("ChatGPT plan usage or offline access was not granted")
    if not all(isinstance(tokens.get(k), str) and tokens[k] for k in
               ("access_token", "refresh_token", "id_token")):
        raise ValueError("ChatGPT token response is incomplete")


def login(credentials: Path, *, client_id: str = "", open_browser: bool = True) -> str:
    """Run a loopback OAuth flow on the machine with the user's browser."""
    host_id = _host_id(credentials)
    with _credential_lock(credentials):
        saved = _read(credentials)
        prior = saved["accounts"].get(client_id) if client_id else None
        if client_id and prior is None:
            raise ValueError("Unknown saved ChatGPT account")
    state = secrets.token_urlsafe(32)
    nonce = secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode("ascii")
    result: dict[str, str] = {}
    event = threading.Event()

    class Callback(BaseHTTPRequestHandler):
        def log_message(self, *args: object) -> None:
            return  # Never log authorization codes or callback URLs.

        def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path != "/auth/callback":
                self.send_error(404)
                return
            query = urllib.parse.parse_qs(parsed.query)
            if not secrets.compare_digest(query.get("state", [""])[0], state):
                self.send_error(400)
                return
            for key in ("code", "client_id", "error"):
                result[key] = query.get(key, [""])[0]
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"Return to the Meme Narrative Collector terminal.")
            event.set()

    server = HTTPServer(("127.0.0.1", 0), Callback)
    server.timeout = 1
    redirect_uri = f"http://127.0.0.1:{server.server_port}/auth/callback"
    params = {
        "client_id": client_id or "dynamic_agent_client", "ext_agent_host_id": host_id,
        "response_type": "code", "redirect_uri": redirect_uri, "scope": SCOPES,
        "resource": RESOURCE, "state": state, "nonce": nonce,
        "code_challenge_method": "S256", "code_challenge": challenge,
    }
    if prior:
        if prior.get("id_token"):
            params["id_token_hint"] = prior["id_token"]
        if prior.get("email"):
            params["login_hint"] = prior["email"]
    else:
        params["agent_name_hint"] = AGENT_NAME
    url = AUTH_URL + "?" + urllib.parse.urlencode(params)
    try:
        if open_browser and not webbrowser.open(url):
            raise RuntimeError("Could not open the local browser")
        print("Finish Continue with ChatGPT in the browser; waiting up to 5 minutes.")
        for _ in range(300):
            if event.is_set():
                break
            server.handle_request()
    finally:
        server.server_close()
    if not event.is_set():
        raise TimeoutError("ChatGPT sign-in timed out")
    if result.get("error"):
        raise RuntimeError("ChatGPT sign-in was declined or failed")
    issued_id = result.get("client_id") or client_id
    if not issued_id.startswith("oaiapp_") or (client_id and issued_id != client_id):
        raise ValueError("ChatGPT registration returned an unexpected client ID")
    if not result.get("code"):
        raise ValueError("ChatGPT callback had no authorization code")
    tokens = _form_post(TOKEN_URL, {
        "grant_type": "authorization_code", "client_id": issued_id,
        "code": result["code"], "code_verifier": verifier,
        "redirect_uri": redirect_uri, "resource": RESOURCE,
    })
    _require_grant(tokens)
    identity = _validate_id_token(tokens["id_token"], issued_id, nonce)
    if prior and identity["sub"] != prior.get("subject"):
        raise ValueError("ChatGPT account changed during reauthorization")
    record = {
        "client_id": issued_id, "subject": identity["sub"],
        "email": identity.get("email", ""), "issuer": "https://auth.openai.com",
        "id_token": tokens["id_token"], "access_token": tokens["access_token"],
        "refresh_token": tokens["refresh_token"], "token_type": tokens.get("token_type", "Bearer"),
        "scopes": sorted(set(tokens["scope"].split())),
        "saved_at": datetime.now(timezone.utc).isoformat(),
        "expires_in": int(tokens.get("expires_in", 3600)),
    }
    with _credential_lock(credentials):
        saved = _read(credentials)
        saved["accounts"][issued_id] = record
        saved["active_client_id"] = issued_id
        _save(credentials, saved)
    return str(record["email"] or record["subject"])


def access_token(credentials: Path) -> str:
    """Read or refresh the active plan grant, serializing rotating refresh tokens."""
    with _credential_lock(credentials):
        saved = _read(credentials)
        account = saved["accounts"].get(saved.get("active_client_id", ""))
        if not isinstance(account, dict):
            raise RuntimeError("No ChatGPT account connected; run meme-chatgpt-auth login locally")
        if "chatgpt.tokens.use.direct" not in account.get("scopes", []):
            raise RuntimeError("ChatGPT plan usage is not authorized")
        saved_at = datetime.fromisoformat(account["saved_at"])
        if saved_at.tzinfo is None:
            raise ValueError("Invalid ChatGPT credential timestamp")
        expiry = saved_at + timedelta(seconds=int(account["expires_in"]))
        if expiry - datetime.now(timezone.utc) > timedelta(minutes=5):
            return account["access_token"]
        tokens = _form_post(TOKEN_URL, {
            "grant_type": "refresh_token", "client_id": account["client_id"],
            "refresh_token": account["refresh_token"], "resource": RESOURCE,
        })
        if not tokens.get("access_token") or not tokens.get("refresh_token"):
            raise RuntimeError("ChatGPT refresh response is incomplete; reconnect the account")
        if tokens.get("scope") and "chatgpt.tokens.use.direct" not in tokens["scope"].split():
            raise RuntimeError("ChatGPT plan permission was revoked")
        account["access_token"] = tokens["access_token"]
        account["refresh_token"] = tokens["refresh_token"]
        account["expires_in"] = int(tokens.get("expires_in", 3600))
        account["saved_at"] = datetime.now(timezone.utc).isoformat()
        if tokens.get("scope"):
            account["scopes"] = sorted(set(tokens["scope"].split()))
        if tokens.get("id_token"):
            account["id_token"] = tokens["id_token"]
        _save(credentials, saved)
        return account["access_token"]


def logout(credentials: Path) -> bool:
    """Revoke the active renewable session, then remove its local tokens."""
    with _credential_lock(credentials):
        saved = _read(credentials)
        client_id = saved.get("active_client_id", "")
        account = saved["accounts"].get(client_id)
        if not account:
            return False
        with urllib.request.urlopen(DISCOVERY_URL, timeout=15) as response:
            discovery = json.load(response)
        endpoint = discovery.get("revocation_endpoint", "")
        if not isinstance(endpoint, str) or not endpoint.startswith("https://auth.openai.com/"):
            raise RuntimeError("Cannot verify ChatGPT revocation endpoint")
        request = urllib.request.Request(
            endpoint, method="POST",
            data=urllib.parse.urlencode({"token": account["refresh_token"],
                                         "token_type_hint": "refresh_token",
                                         "client_id": client_id}).encode("ascii"),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        with urllib.request.urlopen(request, timeout=30) as response:
            if response.status != 200:
                raise RuntimeError("ChatGPT session revocation was not confirmed")
        for key in ("access_token", "refresh_token", "id_token"):
            account.pop(key, None)
        saved["active_client_id"] = ""
        _save(credentials, saved)
        return True


def main() -> None:
    parser = argparse.ArgumentParser(description="Connect an open-source installation to ChatGPT plan usage")
    parser.add_argument("--credentials", type=Path, default=default_credentials_path())
    sub = parser.add_subparsers(dest="action", required=True)
    connect = sub.add_parser("login", help="interactive local browser sign-in")
    connect.add_argument("--account", default="", help="saved issued client ID to reauthorize")
    sub.add_parser("host-init", help="persist a separate ID for this installation / VM")
    sub.add_parser("status", help="show connected account without revealing tokens")
    sub.add_parser("models", help="show account-specific model choices")
    sub.add_parser("logout", help="revoke and clear the active renewable session")
    args = parser.parse_args()
    if args.action == "host-init":
        print("Host ID ready:", _host_id(args.credentials))
    elif args.action == "login":
        print("Connected ChatGPT account:", login(args.credentials, client_id=args.account))
    elif args.action == "status":
        saved = _read(args.credentials)
        account = saved["accounts"].get(saved.get("active_client_id", ""))
        print(json.dumps({"connected": bool(account), "email": account.get("email", "") if account else "",
                          "plan_usage": bool(account and "chatgpt.tokens.use.direct" in account.get("scopes", []))}))
    elif args.action == "logout":
        print("Revoked ChatGPT session" if logout(args.credentials) else "No active ChatGPT session")
    else:
        token = access_token(args.credentials)
        request = urllib.request.Request(RESOURCE + "/models", headers={"Authorization": "Bearer " + token})
        with urllib.request.urlopen(request, timeout=30) as response:
            catalog = json.load(response)
        for model in catalog.get("models", []):
            if model.get("visibility") == "list":
                print(model.get("slug", ""), "-", model.get("display_name", ""))


if __name__ == "__main__":
    main()
