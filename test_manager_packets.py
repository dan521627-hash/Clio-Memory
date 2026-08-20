import os
import tempfile
import unittest
from unittest.mock import patch

import manager_server
from bucket_manager import BucketManager
from manager_server import BucketUpdate
from memory_segments import split_memory_segments


def config_for(root: str) -> dict:
    return {
        "buckets_dir": root,
        "embeddings": {"enabled": False},
        "history": {"db_path": os.path.join(root, "history.sqlite3")},
        "wikilink": {"enabled": False},
    }


def five_packets() -> str:
    return (
        "第一包"
        "\n\n--- 2026-07-18T10:00 ---\n第二包"
        "\n\n--- 2026-07-19T10:00 ---\n第三包"
        "\n\n--- 2026-07-20T10:00 ---\n第四包"
        "\n\n--- 2026-07-21T10:00 ---\n第五包"
    )


class ManagerPacketTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.manager = BucketManager(config_for(self.temp.name))
        self.original = five_packets()
        self.bucket_id = await self.manager.create(
            content=self.original,
            name="五包测试",
            tags=["日常生活"],
            domain=["日常生活"],
        )
        self.patch = patch.object(manager_server, "bucket_manager", self.manager)
        self.patch.start()

    async def asyncTearDown(self):
        self.patch.stop()
        self.temp.cleanup()

    async def test_append_mode_creates_sixth_packet_without_duplication(self):
        await manager_server.update_bucket(
            self.bucket_id,
            BucketUpdate(content=self.original + "\n新发生的事", append=True),
        )
        current = await self.manager.get(self.bucket_id)
        packets = split_memory_segments(current["content"])
        self.assertEqual(len(packets), 6)
        self.assertEqual(current["content"].count(self.original), 1)
        self.assertIn("新发生的事", packets[-1]["content"])

    async def test_prepend_edit_creates_one_older_packet(self):
        edited = "【2026-07-10】\n更早发生的事\n\n" + self.original
        await manager_server.update_bucket(
            self.bucket_id,
            BucketUpdate(content=edited),
        )
        current = await self.manager.get(self.bucket_id)
        packets = split_memory_segments(current["content"])
        history = await self.manager.get_history(self.bucket_id, 5)
        self.assertEqual(len(packets), 6)
        self.assertIn("更早发生的事", packets[0]["content"])
        self.assertIn(self.original, current["content"])
        self.assertEqual(history[0]["content"], self.original)

    async def test_insertion_before_existing_marker_creates_one_packet(self):
        marker = "--- 2026-07-20T10:00 ---"
        edited = self.original.replace(
            marker,
            "【2026-07-19】\n插在第三包与第四包之间\n\n" + marker,
        )
        await manager_server.update_bucket(
            self.bucket_id,
            BucketUpdate(content=edited),
        )
        current = await self.manager.get(self.bucket_id)
        packets = split_memory_segments(current["content"])
        self.assertEqual(len(packets), 6)
        self.assertIn("插在第三包与第四包之间", packets[3]["content"])

    async def test_insertion_inside_old_packet_still_becomes_one_new_packet(self):
        edited = self.original.replace("第三包", "第三包前半段【2026-07-19】补写旧事后半段")
        await manager_server.update_bucket(
            self.bucket_id,
            BucketUpdate(content=edited),
        )
        current = await self.manager.get(self.bucket_id)
        packets = split_memory_segments(current["content"])
        self.assertEqual(len(packets), 6)
        self.assertEqual(
            sum("【2026-07-19】补写旧事" in packet["content"] for packet in packets),
            1,
        )


if __name__ == "__main__":
    unittest.main()
