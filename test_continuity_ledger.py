import tempfile
import unittest

from continuity_ledger import ContinuityLedger


class ContinuityLedgerTests(unittest.IsolatedAsyncioTestCase):
    async def test_hash_chain_is_valid_and_secret_fields_are_redacted(self):
        with tempfile.TemporaryDirectory() as root:
            ledger = ContinuityLedger({"buckets_dir": root})
            await ledger.append(
                "narrative_write",
                "hold",
                "bucket-1",
                {"characters": 20, "api_key": "must-not-survive"},
            )
            await ledger.append("lmc5_refreshed", "living_memory", "bucket-1", {})

            rows = await ledger.list(10)
            verification = await ledger.verify()
            self.assertEqual(rows[-1]["payload"]["api_key"], "[REDACTED]")
            self.assertNotIn("must-not-survive", str(rows))
            self.assertTrue(verification["valid"])
            self.assertEqual(verification["checked"], 2)


if __name__ == "__main__":
    unittest.main()
