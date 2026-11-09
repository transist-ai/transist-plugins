"""Scoped regression: Tika provisioning must never fetch or create model assets."""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cryptography import x509

import provision


class TikaProvisionTests(unittest.TestCase):
    """Exercise real document/certificate generation with download access forbidden."""

    def test_tika_only_without_downloads(self) -> None:
        """Generate usable non-root TLS assets and documents without model/database."""
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with patch(
                "provision.httpx.Client",
                side_effect=AssertionError("Download attempted"),
            ):
                provision.provision(root, tika_only=True)
            self.assertEqual(
                set(json.loads((root / "credentials.json").read_text())), {"tika"}
            )
            self.assertFalse((root / "model").exists())
            self.assertFalse((root / "qdrant.yaml").exists())
            self.assertEqual(
                {p.suffix for p in (root / "documents").iterdir()},
                {".pdf", ".docx", ".txt"},
            )
            for name in ("credentials.json", "server.key"):
                stat = (root / name).stat()
                self.assertEqual(stat.st_mode & 0o777, 0o600)
                self.assertEqual(stat.st_uid, 65534)
            cert = x509.load_pem_x509_certificate((root / "server.pem").read_bytes())
            names = cert.extensions.get_extension_for_class(
                x509.SubjectAlternativeName
            ).value
            self.assertNotIn("localhost", names.get_values_for_type(x509.DNSName))
            self.assertEqual(
                [str(ip) for ip in names.get_values_for_type(x509.IPAddress)],
                ["127.0.0.1"],
            )
            self.assertEqual(os.stat(root).st_uid, os.getuid())

    def test_refuses_reusing_private_assets(self) -> None:
        """Refuse an occupied directory before overwriting any fixture credentials."""
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            sentinel = root / "existing"
            sentinel.write_bytes(b"preserve")
            with self.assertRaisesRegex(RuntimeError, "empty fixture volume"):
                provision.provision(root, tika_only=True)
            self.assertEqual(sentinel.read_bytes(), b"preserve")


if __name__ == "__main__":
    unittest.main(verbosity=2)
