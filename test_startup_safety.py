import os
import unittest
from unittest.mock import patch

import server


class StartupSafetyTests(unittest.TestCase):
    def test_missing_response_seal_is_rejected(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "未配置"):
                server._validate_response_seal()

    def test_example_response_seal_is_rejected(self):
        with patch.dict(
            os.environ,
            {"OMBRE_RESPONSE_SEAL": "CHANGE_ME_TO_A_RANDOM_PRIVATE_SEAL"},
            clear=True,
        ):
            with self.assertRaisesRegex(RuntimeError, "示例值"):
                server._validate_response_seal()

    def test_private_response_seal_is_accepted(self):
        with patch.dict(
            os.environ,
            {"OMBRE_RESPONSE_SEAL": "a-random-private-seal-for-this-installation"},
            clear=True,
        ):
            self.assertEqual(
                server._validate_response_seal(),
                "a-random-private-seal-for-this-installation",
            )


if __name__ == "__main__":
    unittest.main()
