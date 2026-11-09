"""Model-lock representation regressions without model providers or downloads."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

FIXTURE = Path(__file__).resolve().parent / "services/tika_qdrant"
SPEC = importlib.util.spec_from_file_location(
    "fixture_model_lock", FIXTURE / "model_lock.py"
)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class ModelLockTests(unittest.TestCase):
    """Require exact normalization and reject ambiguous/incomplete digest entries."""

    def load(self, pins: object) -> dict:
        """Load a disposable lock through the same parser used by the provider."""
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "lock.json"
            path.write_text(json.dumps({"repository": "fixture", "sha256": pins}))
            return MODULE.load_model_lock(path)

    def test_structured_and_historical_locks_are_equivalent(self) -> None:
        """Representation changes preserve every filename, digest and metadata field."""
        pins = {"tokenizer.json": "a" * 64, "model.onnx": "b" * 64}
        entries = [{"file": name, "digest": digest} for name, digest in pins.items()]
        self.assertEqual(self.load(entries), self.load(pins))

    def test_public_lock_has_six_explicit_valid_pins(self) -> None:
        """The real lock normalizes into the existing complete verification manifest."""
        lock = MODULE.load_model_lock(FIXTURE / "model.lock.json")
        self.assertEqual(
            set(lock["sha256"]),
            {
                "config.json",
                "model.onnx",
                "special_tokens_map.json",
                "tokenizer.json",
                "tokenizer_config.json",
                "vocab.txt",
            },
        )
        self.assertEqual(lock["revision"], "d13954661f83248295ba75c1ed411eef3b7b936e")

    def test_invalid_and_duplicate_entries_fail_closed(self) -> None:
        """Reject overwritten pins, missing digests, invalid paths and bad hashes."""
        entry = {"file": "fixture.json", "digest": "a" * 64}
        for pins in (
            [],
            {},
            [entry, entry],
            [{"file": "fixture.json"}],
            [{**entry, "extra": True}],
            [{**entry, "file": "../outside"}],
            [{**entry, "digest": "short"}],
            [{**entry, "digest": None}],
            {"fixture.json": "g" * 64},
        ):
            with self.subTest(case=type(pins).__name__), self.assertRaises(ValueError):
                self.load(pins)


if __name__ == "__main__":
    unittest.main()
