"""Tests for the Dropbox metadata client.

Only the pure parts are covered here -- the content hash and the PKCE URL. The
network paths are exercised against the real API by `auth` and by a push.
"""

from __future__ import annotations

import hashlib
import sys
import unittest
import urllib.parse
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from delta_retroarch_sync import dropbox_api  # noqa: E402


class ContentHashTests(unittest.TestCase):
    def test_matches_dropbox_definition_for_a_small_file(self) -> None:
        # Under one block: SHA-256 of the single block digest.
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "small.bin"
            path.write_bytes(b"hello world")
            expected = hashlib.sha256(
                hashlib.sha256(b"hello world").digest()
            ).hexdigest()
            self.assertEqual(dropbox_api.content_hash(path), expected)

    def test_blocks_at_four_mebibytes(self) -> None:
        # Exactly two blocks, so a wrong block size would give a wrong answer.
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "big.bin"
            block = dropbox_api.CONTENT_HASH_BLOCK
            data = b"a" * block + b"b" * block
            path.write_bytes(data)
            expected = hashlib.sha256(
                hashlib.sha256(b"a" * block).digest()
                + hashlib.sha256(b"b" * block).digest()
            ).hexdigest()
            self.assertEqual(dropbox_api.content_hash(path), expected)

    def test_empty_file_hashes_the_empty_digest_list(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "empty.bin"
            path.write_bytes(b"")
            self.assertEqual(
                dropbox_api.content_hash(path), hashlib.sha256(b"").hexdigest()
            )


class AuthorizeUrlTests(unittest.TestCase):
    def test_requests_offline_access_and_s256_challenge(self) -> None:
        verifier = "a" * 64
        url = dropbox_api.build_authorize_url("APPKEY", verifier)
        query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)

        # Without offline access Dropbox returns no refresh token, and the tool
        # would need re-authorising by hand every few hours.
        self.assertEqual(query["token_access_type"], ["offline"])
        self.assertEqual(query["code_challenge_method"], ["S256"])
        self.assertEqual(query["client_id"], ["APPKEY"])

        import base64

        expected = (
            base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
            .decode()
            .rstrip("=")
        )
        self.assertEqual(query["code_challenge"], [expected])

    def test_verifiers_are_unique(self) -> None:
        self.assertNotEqual(dropbox_api.make_verifier(), dropbox_api.make_verifier())


if __name__ == "__main__":
    unittest.main()
