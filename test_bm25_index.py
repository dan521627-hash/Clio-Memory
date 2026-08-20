import unittest

from bm25_index import BM25Index


class BM25IndexTests(unittest.TestCase):
    def test_chinese_rare_terms_rank_the_right_bucket_first(self):
        index = BM25Index(
            {"matching": {"bm25_enabled": True, "bm25_max_content_chars": 12000}}
        )
        buckets = [
            {
                "id": "memory-vector",
                "metadata": {"name": "向量检索", "domain": ["技术"], "tags": []},
                "content": "语义向量负责找到同义表达。",
            },
            {
                "id": "memory-dinner",
                "metadata": {"name": "晚饭", "domain": ["日常"], "tags": []},
                "content": "今天吃了鱼和蔬菜。",
            },
            {
                "id": "memory-backup",
                "metadata": {"name": "备份", "domain": ["技术"], "tags": []},
                "content": "数据库备份附带校验清单。",
            },
        ]

        scores = index.scores("语义向量怎么检索", buckets)

        self.assertEqual(max(scores, key=scores.get), "memory-vector")
        self.assertEqual(scores["memory-vector"], 1.0)

    def test_disabled_index_is_a_clean_noop(self):
        index = BM25Index({"matching": {"bm25_enabled": False}})
        self.assertEqual(index.scores("任何查询", []), {})
