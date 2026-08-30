from pathlib import Path

from django.test import SimpleTestCase


ROOT = Path(__file__).resolve().parents[1]
CMS_SHA = "7693319869c6c4bd2268b7cea1941498fc919ba4"  # pragma: allowlist secret


class ReleaseContractTests(SimpleTestCase):
    def test_ci_uses_node24_actions_and_hash_locked_dependencies(self):
        workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(
            encoding="utf-8"
        )
        ci_lock = (ROOT / "requirements-ci.lock").read_text(encoding="utf-8")
        audit_lock = (ROOT / "requirements-audit.lock").read_text(encoding="utf-8")

        self.assertIn(
            "actions/checkout@fbc6f3992d24b796d5a048ff273f7fcc4a7b6c09",
            workflow,
        )
        self.assertIn(
            "actions/setup-python@ece7cb06caefa5fff74198d8649806c4678c61a1",
            workflow,
        )
        self.assertIn(f"ref: {CMS_SHA}", workflow)
        self.assertIn("--require-hashes -r requirements-ci.lock", workflow)
        self.assertIn("pip-audit --requirement requirements-audit.lock", workflow)
        self.assertIn("pip==25.3", ci_lock)
        self.assertIn("setuptools==80.9.0", ci_lock)
        self.assertIn("django==5.2.17", audit_lock)
        self.assertGreater(ci_lock.count("--hash=sha256:"), 20)
