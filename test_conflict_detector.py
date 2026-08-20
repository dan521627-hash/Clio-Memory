import unittest

from conflict_detector import ConflictDetector


class FakeEmbeddingIndex:
    def __init__(self, scores):
        self.scores = scores

    async def query_scores(self, _query):
        return self.scores


class FakeBucketManager:
    def __init__(self, buckets, scores):
        self.buckets = buckets
        self.embedding_index = FakeEmbeddingIndex(scores)

    async def list_all(self, include_archive=False):
        return self.buckets


class FakeDehydrator:
    api_available = False


def bucket(bucket_id, content, name="old memory"):
    return {
        "id": bucket_id,
        "content": content,
        "metadata": {"name": name},
    }


class ConflictDetectorTests(unittest.IsolatedAsyncioTestCase):
    async def test_high_similarity_numeric_conflict_is_reported(self):
        old = bucket("budget", "每月购物预算是3000元")
        detector = ConflictDetector(
            {"conflict_detection": {"similarity_threshold": 0.78}},
            FakeBucketManager([old], {"budget": 0.91}),
            FakeDehydrator(),
        )

        conflicts = await detector.detect("每月购物预算是5000元")

        self.assertEqual(len(conflicts), 1)
        self.assertEqual(conflicts[0]["old_bucket_id"], "budget")
        self.assertIn("3000元", conflicts[0]["point"])
        self.assertIn("5000元", conflicts[0]["point"])

    async def test_candidate_below_threshold_is_ignored(self):
        old = bucket("budget", "每月购物预算是3000元")
        detector = ConflictDetector(
            {"conflict_detection": {"similarity_threshold": 0.78}},
            FakeBucketManager([old], {"budget": 0.70}),
            FakeDehydrator(),
        )

        self.assertEqual(await detector.detect("每月购物预算是5000元"), [])

    async def test_related_but_compatible_fact_is_not_reported(self):
        old = bucket("birthday", "我的生日是1995年5月12日")
        detector = ConflictDetector(
            {"conflict_detection": {"similarity_threshold": 0.78}},
            FakeBucketManager([old], {"birthday": 0.90}),
            FakeDehydrator(),
        )

        self.assertEqual(await detector.detect("我的生日会和家人一起过"), [])

    async def test_high_similarity_polarity_conflict_is_reported(self):
        old = bucket("coffee", "我喜欢喝咖啡")
        detector = ConflictDetector(
            {"conflict_detection": {"similarity_threshold": 0.78}},
            FakeBucketManager([old], {"coffee": 0.82}),
            FakeDehydrator(),
        )

        conflicts = await detector.detect("我不喜欢喝咖啡")

        self.assertEqual(len(conflicts), 1)
        self.assertIn("肯定与否定", conflicts[0]["point"])


if __name__ == "__main__":
    unittest.main()
