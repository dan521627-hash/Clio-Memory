"""Build or repair the local embedding index without changing bucket files."""

import asyncio

from bucket_manager import BucketManager
from utils import load_config, setup_logging


async def main() -> None:
    config = load_config()
    setup_logging(config.get("log_level", "INFO"))
    manager = BucketManager(config)
    buckets = await manager.list_all(include_archive=True, include_sealed=True)
    result = await manager.embedding_index.backfill(buckets)
    print(
        "Embedding backfill complete: "
        f"total={result['total']} updated={result['updated']} "
        f"unchanged={result['unchanged']} removed={result['removed']} "
        f"indexed={manager.embedding_index.count()}"
    )


if __name__ == "__main__":
    asyncio.run(main())
