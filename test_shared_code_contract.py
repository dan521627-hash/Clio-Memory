"""Prevent the two deployable images from silently drifting on shared rules."""
import importlib.util
from pathlib import Path
import unittest

import memory_upgrade as brain_rules


class SharedCodeContractTests(unittest.TestCase):
    def test_manager_and_brain_share_canonical_rules(self):
        path = Path(__file__).resolve().parent.parent / "manager-app" / "memory_upgrade.py"
        spec = importlib.util.spec_from_file_location("manager_memory_upgrade", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        for name in ("TOPIC_ALIASES", "TENDENCY_ALIASES", "QUERY_EXPANSIONS", "TENDENCY_COUNTERS"):
            self.assertEqual(getattr(module, name), getattr(brain_rules, name), name)


if __name__ == "__main__":
    unittest.main()
