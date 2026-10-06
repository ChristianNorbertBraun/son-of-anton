"""GitHub App auth: private key -> JWT -> short-lived installation token."""
from __future__ import annotations

import base64
import json
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

API = "https://api.github.com"


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def make_jwt(app_id: int | str, key_path: Path, now: int | None = None) -> str:
    now = int(time.time()) if now is None else now
    header = _b64url(json.dumps({"alg": "RS256", "typ": "JWT"}).encode())
    payload = _b64url(json.dumps({"iat": now - 60, "exp": now + 540, "iss": str(app_id)}).encode())
    unsigned = f"{header}.{payload}".encode()
    sig = subprocess.run(
        ["openssl", "dgst", "-sha256", "-sign", str(key_path)],
        input=unsigned, capture_output=True, check=True,
    ).stdout
    return f"{header}.{payload}.{_b64url(sig)}"


def api(method: str, path: str, token: str, body: dict | None = None) -> dict:
    req = urllib.request.Request(
        API + path,
        method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "son-of-anton",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"GitHub API {method} {path}: {e.code} {e.read()[:300]!r}") from None


def installation_token(app_id: int | str, key_path: Path, installation_id: int, repo: str) -> str:
    """Token scoped to ONE repository, even if the installation covers more."""
    jwt = make_jwt(app_id, key_path)
    name = repo.split("/", 1)[1]
    res = api("POST", f"/app/installations/{installation_id}/access_tokens", jwt,
              {"repositories": [name]})
    return res["token"]
