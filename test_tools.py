"""Ombre Brain MCP tool-level end-to-end test: direct calls to @mcp.tool() functions
   Ombre Brain MCP 工具层端到端测试：直接调用 @mcp.tool() 函数"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from utils import load_config, setup_logging

config = load_config()
setup_logging("INFO")

# Must import after config is set, since server.py does module-level init
# 必须在配置好后导入，因为 server.py 有模块级初始化
from server import breath, hold, trace, pulse, grow


async def main():
    passed = 0
    failed = 0

    # ===== pulse =====
    print("=== [1/6] pulse ===")
    try:
        r = await pulse()
        assert "Ombre Brain" in r
        print(f"  {r.splitlines()[0]}")
        print("  [OK]")
        passed += 1
    except Exception as e:
        print(f"  [FAIL] {e}")
        failed += 1
    print()

    # ===== hold =====
    print("=== [2/6] hold ===")
    try:
        r = await hold(content="P酱最喜欢的编程语言是 Python，喜欢用 FastAPI 写后端", tags="编程,偏好", importance=8)
        print(f"  {r.splitlines()[0]}")
        assert any(kw in r for kw in ["新建", "合并", "📌"])
        print("  [OK]")
        passed += 1
    except Exception as e:
        print(f"  [FAIL] {e}")
        failed += 1
    print()

    # ===== hold (merge test / 合并测试) =====
    print("=== [2b/6] hold (合并测试) ===")
    try:
        r = await hold(content="P酱也喜欢用 Python 写爬虫和数据分析", tags="编程", importance=6)
        print(f"  {r.splitlines()[0]}")
        print("  [OK]")
        passed += 1
    except Exception as e:
        print(f"  [FAIL] {e}")
        failed += 1
    print()

    # ===== breath =====
    print("=== [3/6] breath ===")
    try:
        r = await breath(query="Python 编程", max_results=3)
        print(f"  结果前80字: {r[:80]}...")
        assert "未找到" not in r
        print("  [OK]")
        passed += 1
    except Exception as e:
        print(f"  [FAIL] {e}")
        failed += 1
    print()

    # ===== breath (emotion resonance / 情感共鸣) =====
    print("=== [3b/6] breath (情感共鸣检索) ===")
    try:
        r = await breath(query="编程", domain="编程", valence=0.8, arousal=0.5)
        print(f"  结果前80字: {r[:80]}...")
        print("  [OK]")
        passed += 1
    except Exception as e:
        print(f"  [FAIL] {e}")
        failed += 1
    print()

    # --- Get a bucket ID for subsequent tests / 取一个桶 ID 用于后续测试 ---
    bucket_id = None
    from bucket_manager import BucketManager
    bm = BucketManager(config)
    all_buckets = await bm.list_all()
    if all_buckets:
        bucket_id = all_buckets[0]["id"]

    # ===== trace =====
    print("=== [4/6] trace ===")
    if bucket_id:
        try:
            r = await trace(bucket_id=bucket_id, domain="编程,创作", importance=9)
            print(f"  {r}")
            assert "已修改" in r
            print("  [OK]")
            passed += 1
        except Exception as e:
            print(f"  [FAIL] {e}")
            failed += 1
    else:
        print("  [SKIP] 没有可编辑的桶")
    print()

    # ===== grow =====
    print("=== [5/6] grow ===")
    try:
        diary = (
            "今天早上复习了线性代数，搞懂了特征值分解。"
            "中午和室友去吃了拉面，聊了聊暑假实习的事。"
            "下午写了一个 Flask 项目的 API 接口。"
            "晚上看了部电影叫《星际穿越》，被结尾感动哭了。"
        )
        r = await grow(content=diary)
        print(f"  {r.splitlines()[0]}")
        for line in r.splitlines()[1:]:
            if line.strip():
                print(f"  {line}")
        assert "新建→" in r or "追加→" in r
        print("  [OK]")
        passed += 1
    except Exception as e:
        print(f"  [FAIL] {e}")
        failed += 1
    print()

    # ===== cleanup via trace(delete=True) / 清理测试数据 =====
    print("=== [6/6] cleanup (清理全部测试数据) ===")
    try:
        all_buckets = await bm.list_all()
        for b in all_buckets:
            r = await trace(bucket_id=b["id"], delete=True)
            print(f"  {r}")
        print("  [OK]")
        passed += 1
    except Exception as e:
        print(f"  [FAIL] {e}")
        failed += 1
    print()

    # ===== Confirm cleanup / 确认清理干净 =====
    final = await pulse()
    print(f"清理后: {final.splitlines()[0]}")
    print()
    print("=" * 50)
    print(f"MCP tool test complete / 工具测试完成: {passed} passed / {failed} failed")
    if failed == 0:
        print("All passed ✓")
    else:
        print(f"{failed} failed ✗")


if __name__ == "__main__":
    asyncio.run(main())
