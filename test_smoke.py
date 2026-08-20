"""Ombre Brain 冒烟测试：验证核心功能链路"""
import asyncio
import os

# 确保模块路径
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from utils import load_config, setup_logging
from bucket_manager import BucketManager
from dehydrator import Dehydrator
from decay_engine import DecayEngine


async def main():
    config = load_config()
    setup_logging("INFO")
    bm = BucketManager(config)
    dh = Dehydrator(config)
    de = DecayEngine(config, bm)

    print(f"API available: {dh.api_available}")
    print(f"base_url: {dh.base_url}")
    print()

    # ===== 1. 自动打标 =====
    print("=== 1. analyze (自动打标) ===")
    try:
        result = await dh.analyze("今天学了 Python 的 asyncio，感觉收获很大，心情不错")
        print(f"  domain:  {result['domain']}")
        print(f"  valence: {result['valence']}, arousal: {result['arousal']}")
        print(f"  tags:    {result['tags']}")
        print("  [OK]")
    except Exception as e:
        print(f"  [FAIL] {e}")
    print()

    # ===== 2. 建桶 =====
    print("=== 2. create (建桶) ===")
    try:
        bid = await bm.create(
            content="P酱喜欢猫，家里养了一只橘猫叫小橘",
            tags=["猫", "宠物"],
            importance=7,
            domain=["生活"],
            valence=0.8,
            arousal=0.4,
        )
        print(f"  bucket_id: {bid}")
        print("  [OK]")
    except Exception as e:
        print(f"  [FAIL] {e}")
        return
    print()

    # ===== 3. 搜索 =====
    print("=== 3. search (检索) ===")
    try:
        hits = await bm.search("猫", limit=3)
        print(f"  found {len(hits)} results")
        for h in hits:
            name = h["metadata"].get("name", h["id"])
            print(f"    - {name} (score={h['score']:.1f})")
        print("  [OK]")
    except Exception as e:
        print(f"  [FAIL] {e}")
    print()

    # ===== 4. 脱水压缩 =====
    print("=== 4. dehydrate (脱水压缩) ===")
    try:
        text = (
            "这是一段很长的内容用来测试脱水功能。"
            "P酱今天去了咖啡厅，点了一杯拿铁，然后坐在窗边看书看了两个小时。"
            "期间遇到了一个朋友，聊了聊最近的工作情况。回家之后写了会代码。"
        )
        summary = await dh.dehydrate(text, {})
        print(f"  summary: {summary[:120]}...")
        print("  [OK]")
    except Exception as e:
        print(f"  [FAIL] {e}")
    print()

    # ===== 5. 衰减评分 =====
    print("=== 5. decay score (衰减评分) ===")
    try:
        bucket = await bm.get(bid)
        score = de.calculate_score(bucket["metadata"])
        print(f"  score: {score:.3f}")
        print("  [OK]")
    except Exception as e:
        print(f"  [FAIL] {e}")
    print()

    # ===== 6. 日记整理 =====
    print("=== 6. digest (日记整理) ===")
    try:
        diary = (
            "今天上午写了个 Python 脚本处理数据，下午和朋友去吃了火锅很开心，"
            "晚上失眠了有点焦虑，想了想明天的面试。"
        )
        items = await dh.digest(diary)
        print(f"  拆分出 {len(items)} 条记忆:")
        for it in items:
            print(f"    - [{it.get('name','')}] domain={it['domain']} V{it['valence']:.1f}/A{it['arousal']:.1f}")
        print("  [OK]")
    except Exception as e:
        print(f"  [FAIL] {e}")
    print()

    # ===== 7. 清理测试数据 =====
    print("=== 7. cleanup (删除测试桶) ===")
    try:
        ok = await bm.delete(bid)
        print(f"  deleted: {ok}")
        print("  [OK]")
    except Exception as e:
        print(f"  [FAIL] {e}")
    print()

    print("=" * 40)
    print("冒烟测试完成!")


if __name__ == "__main__":
    asyncio.run(main())
