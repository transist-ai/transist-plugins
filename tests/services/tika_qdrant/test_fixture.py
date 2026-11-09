"""Offline structural and fail-fast regression tests for the Step 1 fixture."""

import io
import re
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch
from xml.etree import ElementTree

import fixtures
import validate
import yaml
from model_lock import load_model_lock


class FixtureTests(unittest.TestCase):
    """Verify generated documents, complete pin sets and missing prerequisites."""

    def test_privileged_units_use_locked_network_none_image(self) -> None:
        """Certificate ownership/model dependencies belong to the fixture container."""
        prefix = ["docker-compose", "-p", "fixture", "-f", "compose.yaml"]
        command = validate.fixture_unit_command(prefix)
        self.assertEqual(command[: len(prefix)], prefix)
        self.assertEqual(
            command[len(prefix) : len(prefix) + 7],
            ["run", "--rm", "--no-deps", "--entrypoint", "python", "offline", "-B"],
        )
        self.assertEqual(command[-2:], ["-p", "test_tika_fixture.py"])
        services = yaml.safe_load((Path(__file__).parent / "compose.yaml").read_text())[
            "services"
        ]
        self.assertEqual(services["offline"]["network_mode"], "none")

    def test_real_document_structures(self) -> None:
        """Validate PDF offsets and OOXML parts rather than treating text as PDF."""
        pdf = fixtures.pdf_bytes()
        start = int(pdf.split(b"startxref\n")[1].splitlines()[0])
        self.assertTrue(pdf[start:].startswith(b"xref\n"))
        for index, offset in enumerate(re.findall(rb"(\d{10}) 00000 n", pdf), 1):
            self.assertTrue(pdf[int(offset) :].startswith(f"{index} 0 obj".encode()))
        with zipfile.ZipFile(io.BytesIO(fixtures.docx_bytes())) as archive:
            self.assertEqual(
                set(archive.namelist()),
                {"[Content_Types].xml", "_rels/.rels", "word/document.xml"},
            )
            for name in archive.namelist():
                ElementTree.fromstring(archive.read(name))
            self.assertIn(fixtures.TEXT.encode(), archive.read("word/document.xml"))

    def test_hash_locks(self) -> None:
        """Require every resolved distribution to have an exact pin and SHA-256."""
        root = Path(__file__).parent
        for name, count in (("server.lock", 68), ("client.lock", 31)):
            lines = [
                line
                for line in (root / name).read_text().splitlines()
                if not line.startswith("#")
            ]
            self.assertEqual(len(lines), count)
            for line in lines:
                self.assertRegex(line, r"^[\w.-]+==[\w.]+ --hash=sha256:[a-f0-9]{64}$")
        model = load_model_lock(root / "model.lock.json")
        self.assertEqual(model["revision"], "d13954661f83248295ba75c1ed411eef3b7b936e")
        self.assertEqual(len(model["sha256"]), 6)

    def test_missing_prerequisite_is_failure(self) -> None:
        """Fail before provisioning when a required executable is missing."""
        with patch("validate.shutil.which", return_value=None):
            with self.assertRaisesRegex(RuntimeError, "Missing prerequisite: docker"):
                validate.validate()

    def test_wrong_runtime_is_failure(self) -> None:
        """Reject nonmatching runtime binaries without touching Docker resources."""
        with patch("validate.shutil.which", return_value="/tmp/fixture-tool"):
            with patch("validate.output", return_value="transist 0.11.0"):
                with self.assertRaisesRegex(RuntimeError, "Matching Transist/admin"):
                    validate.validate()


if __name__ == "__main__":
    unittest.main()
