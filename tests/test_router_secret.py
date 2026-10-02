import tempfile
import unittest
import tomllib
from pathlib import Path

from agents.router_agent.agent import RouterAgent


class RouterSecretTests(unittest.TestCase):
    def test_reads_owner_only_password_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "password"
            path.write_text("test-secret", encoding="utf-8")
            path.chmod(0o600)

            self.assertEqual(RouterAgent._read_password_file(path), "test-secret")

    def test_rejects_password_file_accessible_to_others(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "password"
            path.write_text("test-secret", encoding="utf-8")
            path.chmod(0o644)

            with self.assertRaises(PermissionError):
                RouterAgent._read_password_file(path)

    def test_rejects_inline_password(self) -> None:
        with self.assertRaisesRegex(ValueError, "Inline router passwords"):
            RouterAgent(
                config={"router": {"password": "test-secret"}},
                bus=None,  # Constructor rejects the inline secret first.
            )

    def test_router_config_and_manifest_do_not_define_inline_passwords(self) -> None:
        root = Path(__file__).parents[1]
        config = tomllib.loads(
            (root / "agents/router_agent/config.toml").read_text()
        )
        manifest = tomllib.loads(
            (root / "agents/router_agent/manifest.toml").read_text()
        )

        self.assertNotIn("password", config["router"])
        self.assertNotIn("password", manifest["config"])
        self.assertIn("password_file", manifest["config"])


if __name__ == "__main__":
    unittest.main()
