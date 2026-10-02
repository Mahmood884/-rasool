import unittest

from rasool.safety import Safety


class SafetyTests(unittest.TestCase):
    def test_empty_allowlist_denies_every_action(self) -> None:
        safety = Safety()

        self.assertFalse(safety.check("agent.health", {}))

    def test_allowlist_permits_only_configured_actions(self) -> None:
        safety = Safety({"agent.health"})

        self.assertTrue(safety.check("agent.health", {}))
        self.assertFalse(safety.check("agent.error", {}))

    def test_allowlist_is_copied(self) -> None:
        allowed = {"agent.health"}
        safety = Safety(allowed)
        allowed.clear()

        self.assertTrue(safety.check("agent.health", {}))


if __name__ == "__main__":
    unittest.main()
