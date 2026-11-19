"""Regression tests for the release dependency gate (no network needed)."""

import contextlib
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[1] / "build-aux/audit-dependencies.py"
SPEC = importlib.util.spec_from_file_location("dependency_audit", SCRIPT)
audit = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(audit)


class DependencyAuditTests(unittest.TestCase):
    def setUp(self):
        self.source = {
            "type": "file",
            "url": "https://files.pythonhosted.org/packages/test/example_pkg-1.2.3-py3-none-any.whl",
            "sha256": "a" * 64,
        }
        self.manifest = {"modules": [{"name": "python3-modules", "modules": [
            {"name": "python3-example", "sources": [self.source]},
        ]}]}
        self.pin = audit.pinned_distributions(self.manifest)[0]
        self.metadata = {
            "info": {"name": "example-pkg", "version": "1.2.3"},
            "urls": [{"filename": self.pin["filename"], "url": self.pin["url"],
                      "digests": {"sha256": self.pin["sha256"]}, "yanked": False}],
            "vulnerabilities": [],
        }

    def test_nested_wheel_and_source_distribution_pins(self):
        self.assertEqual(self.pin["name"], "example-pkg")
        self.source["url"] = "https://files.pythonhosted.org/packages/test/example-pkg-1.2.3.tar.gz"
        self.assertEqual(audit.pinned_distributions(self.manifest)[0]["version"], "1.2.3")

    def test_changed_artifact_hash_is_rejected(self):
        self.metadata["urls"][0]["digests"]["sha256"] = "b" * 64
        with self.assertRaisesRegex(ValueError, "SHA256"):
            audit.check_pin(self.pin, self.metadata)

    def test_missing_pin_is_rejected(self):
        del self.source["sha256"]
        with self.assertRaisesRegex(ValueError, "SHA256"):
            audit.pinned_distributions(self.manifest)

    def test_external_python_source_is_not_silently_skipped(self):
        self.source["url"] = "https://example.com/example-1.2.3.tar.gz"
        with self.assertRaisesRegex(ValueError, "Unsupported Python source"):
            audit.pinned_distributions(self.manifest)

    def test_withdrawn_and_duplicate_advisories(self):
        self.metadata["vulnerabilities"] = [
            {"id": "GHSA-old", "withdrawn": "2026-01-01"},
            {"id": "GHSA-current", "aliases": ["CVE-2026-1"]},
            {"id": "PYSEC-current", "aliases": ["CVE-2026-1"]},
        ]
        self.assertEqual([v["id"] for v in audit.check_pin(self.pin, self.metadata)], ["GHSA-current"])

    def test_missing_advisory_feed_fails_closed(self):
        del self.metadata["vulnerabilities"]
        with self.assertRaises(KeyError):
            audit.check_pin(self.pin, self.metadata)

    def test_yanked_file_is_rejected(self):
        self.metadata["urls"][0]["yanked"] = True
        with self.assertRaisesRegex(ValueError, "yanked"):
            audit.check_pin(self.pin, self.metadata)

    def run_audit(self, *, failure=None):
        with tempfile.TemporaryDirectory() as directory:
            manifest = Path(directory) / "manifest.json"
            manifest.write_text(json.dumps(self.manifest))
            with patch.object(audit, "fetch_metadata", return_value=self.metadata, side_effect=failure), contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                return audit.main([str(manifest)])

    def test_clean_metadata_passes(self):
        self.assertEqual(self.run_audit(), 0)

    def test_active_advisory_fails_gate(self):
        self.metadata["vulnerabilities"] = [{"id": "GHSA-current", "fixed_in": ["1.2.4"]}]
        self.assertEqual(self.run_audit(), 1)

    def test_network_failure_does_not_pass_gate(self):
        self.assertEqual(self.run_audit(failure=OSError("network unavailable")), 2)


if __name__ == "__main__":
    unittest.main()
