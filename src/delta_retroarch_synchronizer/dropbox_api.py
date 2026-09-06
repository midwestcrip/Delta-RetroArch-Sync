"""A minimal Dropbox client, used for one thing: reading a file's revision.

Why this exists: Delta downloads the *exact* revision named in a record. We can
compute everything else about a pushed save locally, but not its revision --
Dropbox assigns that when the desktop client uploads, and the local mirror does
not expose it. Reading it back is the only way to write a record Delta can
follow.

Scope is deliberately tiny. This needs `files.metadata.read` and nothing else:

- It never uploads. The desktop client already does that, and doing it twice
  would race with it.
- It never touches file property groups. Dropbox scopes those to the app that
  created them ("Templates and their associated properties can't be accessed by
  any app other than the app that created them"), so Delta's are permanently
  out of reach -- and, as it turns out, do not need to be touched.

Auth is PKCE with no client secret, which is the correct flow for a desktop app
that cannot keep one. The refresh token is stored locally and gitignored.
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

AUTHORIZE_URL = "https://www.dropbox.com/oauth2/authorize"
TOKEN_URL = "https://api.dropboxapi.com/oauth2/token"
GET_METADATA_URL = "https://api.dropboxapi.com/2/files/get_metadata"

#: Dropbox hashes file content in 4 MiB blocks.
CONTENT_HASH_BLOCK = 4 * 1024 * 1024

TOKEN_FILENAME = "dropbox-token.json"

#: App key shipped with the tool, so a download works without every user
#: registering their own Dropbox app. An app key is a public identifier, not a
#: secret -- PKCE exists precisely so a desktop app can authenticate without
#: holding one. Blank means the user must supply their own in Settings.
DEFAULT_APP_KEY = ""


class DropboxError(RuntimeError):
    pass


def content_hash(path: Path) -> str:
    """Dropbox's content_hash: SHA-256 of the concatenated per-block SHA-256s.

    Used to confirm that the file Dropbox now holds is the one we wrote, before
    trusting the revision it reports. Without that check we could read back the
    revision of a *previous* upload and write it into the record, which is the
    same failure we are fixing.
    """
    digests = b""
    with path.open("rb") as handle:
        while block := handle.read(CONTENT_HASH_BLOCK):
            digests += hashlib.sha256(block).digest()
    return hashlib.sha256(digests).hexdigest()


def _post(url: str, *, data: bytes, headers: dict[str, str]) -> dict:
    request = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")
        raise DropboxError(f"HTTP {error.code} from {url}: {detail}") from error
    except urllib.error.URLError as error:
        raise DropboxError(f"could not reach {url}: {error.reason}") from error


@dataclass
class Credentials:
    app_key: str
    refresh_token: str
    access_token: str = ""
    expires_at: float = 0.0

    def to_json(self) -> dict:
        # The access token is deliberately not persisted: it is short-lived and
        # the refresh token can always mint another.
        return {"app_key": self.app_key, "refresh_token": self.refresh_token}

    @classmethod
    def load(cls, path: Path) -> "Credentials | None":
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        key, token = raw.get("app_key"), raw.get("refresh_token")
        if not isinstance(key, str) or not isinstance(token, str):
            return None
        return cls(app_key=key, refresh_token=token)

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_json(), indent=2), encoding="utf-8")


def build_authorize_url(app_key: str, verifier: str) -> str:
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest())
        .decode("ascii")
        .rstrip("=")
    )
    query = urllib.parse.urlencode(
        {
            "client_id": app_key,
            "response_type": "code",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            # Offline access is what yields a refresh token; without it the tool
            # would need re-authorising by hand every few hours.
            "token_access_type": "offline",
        }
    )
    return f"{AUTHORIZE_URL}?{query}"


def make_verifier() -> str:
    return secrets.token_urlsafe(64)


def exchange_code(app_key: str, verifier: str, code: str) -> Credentials:
    payload = urllib.parse.urlencode(
        {
            "code": code.strip(),
            "grant_type": "authorization_code",
            "client_id": app_key,
            "code_verifier": verifier,
        }
    ).encode("ascii")
    result = _post(
        TOKEN_URL,
        data=payload,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    refresh = result.get("refresh_token")
    if not isinstance(refresh, str):
        raise DropboxError(
            "Dropbox returned no refresh token. The app must be authorised with "
            "token_access_type=offline."
        )
    return Credentials(
        app_key=app_key,
        refresh_token=refresh,
        access_token=str(result.get("access_token", "")),
        expires_at=time.time() + float(result.get("expires_in", 0)) - 60,
    )


class DropboxClient:
    def __init__(self, credentials: Credentials) -> None:
        self.credentials = credentials

    def _token(self) -> str:
        if (
            self.credentials.access_token
            and time.time() < self.credentials.expires_at
        ):
            return self.credentials.access_token

        payload = urllib.parse.urlencode(
            {
                "grant_type": "refresh_token",
                "refresh_token": self.credentials.refresh_token,
                "client_id": self.credentials.app_key,
            }
        ).encode("ascii")
        result = _post(
            TOKEN_URL,
            data=payload,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        self.credentials.access_token = str(result.get("access_token", ""))
        self.credentials.expires_at = (
            time.time() + float(result.get("expires_in", 0)) - 60
        )
        if not self.credentials.access_token:
            raise DropboxError("Dropbox refused to refresh the access token")
        return self.credentials.access_token

    def get_metadata(self, remote_path: str) -> dict:
        """Metadata for one path. Returns at least ``rev`` and ``content_hash``."""
        payload = json.dumps({"path": remote_path}).encode("utf-8")
        return _post(
            GET_METADATA_URL,
            data=payload,
            headers={
                "Authorization": f"Bearer {self._token()}",
                "Content-Type": "application/json",
            },
        )

    def wait_for_revision(
        self,
        remote_path: str,
        expected_content_hash: str,
        *,
        timeout: float = 120.0,
        interval: float = 2.0,
    ) -> str:
        """Wait until Dropbox holds the content we wrote, then return its revision.

        The desktop client uploads asynchronously, so the revision is not
        available the instant the file is written locally. Polling on
        ``content_hash`` rather than on elapsed time is what makes this correct:
        it returns the revision *of our content*, never of whatever happened to
        be there beforehand.
        """
        deadline = time.time() + timeout
        last_seen = ""
        while time.time() < deadline:
            try:
                metadata = self.get_metadata(remote_path)
            except DropboxError:
                # The file may not exist remotely yet; that is a normal early
                # state, not a failure.
                metadata = {}
            remote_hash = str(metadata.get("content_hash", ""))
            last_seen = remote_hash or last_seen
            if remote_hash == expected_content_hash:
                revision = str(metadata.get("rev", ""))
                if revision:
                    return revision
            time.sleep(interval)

        raise DropboxError(
            f"timed out after {timeout:.0f}s waiting for Dropbox to upload "
            f"{remote_path}. Expected content hash {expected_content_hash[:12]}..., "
            f"last saw {last_seen[:12] or 'nothing'}. Is the desktop client running "
            "and finished syncing?"
        )
