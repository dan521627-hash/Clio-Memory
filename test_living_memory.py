import tempfile
import unittest

from living_memory import LivingMemoryStore


class LivingMemoryTests(unittest.IsolatedAsyncioTestCase):
    async def test_builds_and_persists_five_numeric_axes_without_changing_content(self):
        with tempfile.TemporaryDirectory() as root:
            store = LivingMemoryStore({"buckets_dir": root})
            bucket = {
                "id": "bucket-1",
                "metadata": {
                    "created": "2026-08-18T08:00:00+08:00",
                    "importance": 8,
                    "valence": -0.6,
                    "arousal": 0.7,
                    "tension": 0.5,
                    "domain": ["关系与亲密"],
                },
                "content": "--- 2026-08-18T08:00 ---\n第一段\n\n--- 2026-08-19T09:00 ---\n第二段",
            }
            original = bucket["content"]
            built = store.build(
                bucket,
                relations=[{"bucket_id": "bucket-2", "similarity": 0.82}],
                facts=[
                    {
                        "fact_label": "所在地",
                        "fact_value": "北京",
                        "effective_date": "2026-08-19",
                        "is_current": 1,
                    }
                ],
                topic={"main_topic": "我们的关系"},
            )
            saved = await store.save(built)
            loaded = await store.get("bucket-1")

            self.assertEqual(bucket["content"], original)
            self.assertEqual(set(saved["coordinate"]), {"X", "Y", "Z", "E", "M"})
            self.assertTrue(all(0.0 <= value <= 1.0 for value in saved["coordinate"].values()))
            self.assertEqual(loaded["bucket_id"], "bucket-1")
            self.assertEqual(loaded["E"]["score"], 0.7)
            self.assertEqual(set(loaded["coordinate"]), {"X", "Y", "Z", "E", "M"})


if __name__ == "__main__":
    unittest.main()
