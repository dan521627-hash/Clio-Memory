# ============================================================
# Module: Memory Bucket Manager (bucket_manager.py)
# 模块：记忆桶管理器
#
# CRUD operations, multi-dimensional index search, activation updates
# for memory buckets.
# 记忆桶的增删改查、多维索引搜索、激活更新。
#
# Core design:
# 核心逻辑：
#   - Each bucket = one Markdown file (YAML frontmatter + body)
#     每个记忆桶 = 一个 Markdown 文件
#   - Storage by type: permanent / dynamic / archive
#     存储按类型分目录
#   - Multi-dimensional soft index: domain + valence/arousal + fuzzy text
#     多维软索引：主题域 + 情感坐标 + 文本模糊匹配
#   - Search strategy: domain pre-filter → weighted multi-dim ranking
#     搜索策略：主题域预筛 → 多维加权精排
#   - Emotion coordinates based on Russell circumplex model:
#     情感坐标基于环形情感模型（Russell circumplex）：
#       valence (0~1): 0=negative → 1=positive
#       arousal (0~1): 0=calm → 1=excited
#
# Depended on by: server.py, decay_engine.py
# 被谁依赖：server.py, decay_engine.py
# ============================================================

import os
import math
import logging
import re
import shutil
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import frontmatter
import jieba
from rapidfuzz import fuzz

from bm25_index import BM25Index
from embedding_index import EmbeddingIndex
from history_store import HistoryStore
from retrieval_feedback import RetrievalFeedbackStore
from utils import generate_bucket_id, sanitize_name, safe_path, now_iso

logger = logging.getLogger("ombre_brain.bucket")


class BucketManager:
    """
    Memory bucket manager — entry point for all bucket CRUD operations.
    Buckets are stored as Markdown files with YAML frontmatter for metadata
    and body for content. Natively compatible with Obsidian browsing/editing.
    记忆桶管理器 —— 所有桶的 CRUD 操作入口。
    桶以 Markdown 文件存储，YAML frontmatter 存元数据，正文存内容。
    天然兼容 Obsidian 直接浏览和编辑。
    """

    def __init__(self, config: dict):
        # --- Read storage paths from config / 从配置中读取存储路径 ---
        self.base_dir = config["buckets_dir"]
        self.permanent_dir = os.path.join(self.base_dir, "permanent")
        self.dynamic_dir = os.path.join(self.base_dir, "dynamic")
        self.archive_dir = os.path.join(self.base_dir, "archive")
        self.fuzzy_threshold = config.get("matching", {}).get("fuzzy_threshold", 50)
        self.max_results = config.get("matching", {}).get("max_results", 5)

        # --- Wikilink config / 双链配置 ---
        wikilink_cfg = config.get("wikilink", {})
        self.wikilink_enabled = wikilink_cfg.get("enabled", True)
        self.wikilink_use_tags = wikilink_cfg.get("use_tags", False)
        self.wikilink_use_domain = wikilink_cfg.get("use_domain", True)
        self.wikilink_use_auto_keywords = wikilink_cfg.get("use_auto_keywords", True)
        self.wikilink_auto_top_k = wikilink_cfg.get("auto_top_k", 8)
        self.wikilink_min_len = wikilink_cfg.get("min_keyword_len", 2)
        self.wikilink_exclude_keywords = set(wikilink_cfg.get("exclude_keywords", []))
        self.wikilink_stopwords = {
            "的", "了", "在", "是", "我", "有", "和", "就", "不", "人",
            "都", "一个", "上", "也", "很", "到", "说", "要", "去",
            "你", "会", "着", "没有", "看", "好", "自己", "这", "他", "她",
            "我们", "你们", "他们", "然后", "今天", "昨天", "明天", "一下",
            "the", "and", "for", "are", "but", "not", "you", "all", "can",
            "had", "her", "was", "one", "our", "out", "has", "have", "with",
            "this", "that", "from", "they", "been", "said", "will", "each",
        }
        self.wikilink_stopwords |= {w.lower() for w in self.wikilink_exclude_keywords}

        # --- Search scoring weights / 检索权重配置 ---
        scoring = config.get("scoring_weights", {})
        self.w_topic = scoring.get("topic_relevance", 4.0)
        self.w_emotion = scoring.get("emotion_resonance", 2.0)
        self.w_time = scoring.get("time_proximity", 1.5)
        self.w_importance = scoring.get("importance", 1.0)
        self.w_semantic = scoring.get("semantic_similarity", 10.0)
        self.w_bm25 = scoring.get("bm25_relevance", 2.5)
        embedding_cfg = config.get("embeddings", {})
        self.semantic_threshold = float(embedding_cfg.get("similarity_threshold", 0.45))
        self.embedding_index = EmbeddingIndex(config)
        self.embedding_index.set_bucket_loader(self._load_bucket_for_embedding)
        self.bm25_index = BM25Index(config)
        self.retrieval_feedback = RetrievalFeedbackStore(
            config,
            self.embedding_index.model_name,
            self.embedding_index.dimensions,
        )
        self.history_store = HistoryStore(config)

    def _load_bucket_for_embedding(self, bucket_id: str) -> Optional[dict]:
        file_path = self._find_bucket_file(bucket_id)
        return self._load_bucket(file_path) if file_path else None

    async def _queue_embedding(self, bucket: dict) -> bool:
        enqueue = getattr(self.embedding_index, "enqueue_bucket", None)
        if enqueue is not None:
            return await enqueue(bucket)
        return await self.embedding_index.upsert_bucket(bucket)

    @staticmethod
    def _serialize_markdown(metadata: dict, content: str) -> str:
        """Serialize metadata while preserving every source-text character."""
        header = frontmatter.dumps(frontmatter.Post("", **metadata))
        return f"{header}\n\n{content}"

    @staticmethod
    def _read_exact_body(file_path: str) -> str:
        """Read the Markdown body without python-frontmatter's whitespace trim."""
        with open(file_path, "r", encoding="utf-8", newline="") as source_file:
            raw = source_file.read()
        boundary = re.match(
            r"\A---\r?\n.*?^---[ \t]*(?:\r?\n|\Z)",
            raw,
            flags=re.DOTALL | re.MULTILINE,
        )
        if not boundary:
            return raw
        body = raw[boundary.end() :]
        if body.startswith("\r\n"):
            return body[2:]
        if body.startswith("\n"):
            return body[1:]
        return body

    # ---------------------------------------------------------
    # Create a new bucket
    # 创建新桶
    # Write content and metadata into a .md file
    # 将内容和元数据写入一个 .md 文件
    # ---------------------------------------------------------
    async def create(
        self,
        content: str,
        tags: list[str] = None,
        importance: int = 5,
        domain: list[str] = None,
        valence: float = 0.5,
        arousal: float = 0.3,
        bucket_type: str = "dynamic",
        name: str = None,
        pinned: bool = False,
        protected: bool = False,
        ai_feeling: bool = False,
        trigger_date: str = "",
        trigger_processed: bool = False,
    ) -> str:
        """
        Create a new memory bucket, return bucket ID.
        创建一个新的记忆桶，返回桶 ID。

        pinned/protected=True: bucket won't be merged, decayed, or have importance changed.
        Importance is locked to 10 for pinned/protected buckets.
        pinned/protected 桶不参与合并与衰减，importance 强制锁定为 10。
        """
        bucket_id = generate_bucket_id()
        bucket_name = sanitize_name(name) if name else bucket_id
        domain = domain or ["未分类"]
        tags = tags or []

        # --- Pinned/protected buckets: lock importance to 10 ---
        # --- 钉选/保护桶：importance 强制锁定为 10 ---
        if pinned or protected:
            importance = 10

        # --- Build YAML frontmatter metadata / 构建元数据 ---
        metadata = {
            "id": bucket_id,
            "name": bucket_name,
            "tags": tags,
            "domain": domain,
            "valence": max(0.0, min(1.0, valence)),
            "arousal": max(0.0, min(1.0, arousal)),
            "importance": max(1, min(10, importance)),
            "type": bucket_type,
            "created": now_iso(),
            "last_active": now_iso(),
            "activation_count": 1,
        }
        if pinned:
            metadata["pinned"] = True
            metadata["pin_level"] = "core"
        if protected:
            metadata["protected"] = True
        if ai_feeling:
            metadata["ai_feeling"] = True
        if trigger_date:
            metadata["trigger_date"] = trigger_date
            metadata["trigger_processed"] = bool(trigger_processed)

        # --- Choose directory by type + primary domain ---
        # --- 按类型 + 主题域选择存储目录 ---
        type_dir = self.permanent_dir if bucket_type == "permanent" else self.dynamic_dir
        primary_domain = sanitize_name(domain[0]) if domain else "未分类"
        target_dir = os.path.join(type_dir, primary_domain)
        os.makedirs(target_dir, exist_ok=True)

        # --- Filename: readable_name_bucketID.md (Obsidian friendly) ---
        # --- 文件名：可读名称_桶ID.md ---
        if bucket_name and bucket_name != bucket_id:
            filename = f"{bucket_name}_{bucket_id}.md"
        else:
            filename = f"{bucket_id}.md"
        file_path = safe_path(target_dir, filename)

        try:
            with open(file_path, "w", encoding="utf-8", newline="") as f:
                f.write(self._serialize_markdown(metadata, content))
        except OSError as e:
            logger.error(f"Failed to write bucket file / 写入桶文件失败: {file_path}: {e}")
            raise

        logger.info(
            f"Created bucket / 创建记忆桶: {bucket_id} ({bucket_name}) → {primary_domain}/"
            + (" [PINNED]" if pinned else "") + (" [PROTECTED]" if protected else "")
        )
        try:
            await self._queue_embedding(self._load_bucket(file_path))
        except Exception as e:
            logger.warning("Embedding update failed for new bucket %s: %s", bucket_id, e)
        return bucket_id

    # ---------------------------------------------------------
    # Read bucket content
    # 读取桶内容
    # Returns {"id", "metadata", "content", "path"} or None
    # ---------------------------------------------------------
    async def get(self, bucket_id: str) -> Optional[dict]:
        """
        Read a single bucket by ID.
        根据 ID 读取单个桶。
        """
        if not bucket_id or not isinstance(bucket_id, str):
            return None
        file_path = self._find_bucket_file(bucket_id)
        if not file_path:
            return None
        return self._load_bucket(file_path)

    async def get_history(self, bucket_id: str, limit: int = 10) -> list[dict]:
        """Return newest write-before snapshots for a bucket."""
        return await self.history_store.list(bucket_id, limit)

    # ---------------------------------------------------------
    # Update bucket
    # 更新桶
    # Supports: content, tags, importance, valence, arousal, name, resolved
    # ---------------------------------------------------------
    async def update(self, bucket_id: str, **kwargs) -> bool:
        """
        Update bucket content or metadata fields.
        更新桶的内容或元数据字段。
        """
        history_operation = kwargs.pop("_history_operation", "content_update")
        require_content_prefix = bool(kwargs.pop("_require_content_prefix", False))
        allow_content_shorten = bool(kwargs.pop("_allow_content_shorten", False))
        file_path = self._find_bucket_file(bucket_id)
        if not file_path:
            return False

        current_bucket = self._load_bucket(file_path)
        if not current_bucket:
            logger.error("Cannot load current bucket before update: %s", bucket_id)
            return False
        if "content" in kwargs:
            old_content = str(current_bucket.get("content", ""))
            next_content = str(kwargs["content"])
            if len(next_content) < len(old_content) and not allow_content_shorten:
                logger.error(
                    "Blocked shortening write for bucket %s: old=%d new=%d",
                    bucket_id,
                    len(old_content),
                    len(next_content),
                )
                return False
            if require_content_prefix and not next_content.startswith(old_content):
                logger.error(
                    "Blocked non-prefix append for bucket %s: existing source changed",
                    bucket_id,
                )
                return False

        if kwargs:
            try:
                await self.history_store.snapshot(current_bucket, history_operation)
            except Exception as e:
                logger.error(
                    "History snapshot failed; update blocked for bucket %s: %s",
                    bucket_id,
                    e,
                )
                return False

        try:
            post = frontmatter.load(file_path)
        except Exception as e:
            logger.warning(f"Failed to load bucket for update / 加载桶失败: {file_path}: {e}")
            return False

        # --- Pinned/protected buckets: lock importance to 10, ignore importance changes ---
        # --- 钉选/保护桶：importance 不可修改，强制保持 10 ---
        current_sealed = bool(post.get("sealed", False))
        requested_sealed = kwargs.get("sealed")
        effective_sealed = (
            bool(requested_sealed)
            if requested_sealed is not None
            else current_sealed
        )
        if kwargs.get("pinned") is True and effective_sealed:
            logger.warning("Cannot pin sealed bucket: %s", bucket_id)
            return False
        requested_pin_level = str(kwargs.get("pin_level", "")).strip().lower()
        if requested_pin_level and requested_pin_level not in ("core", "important"):
            logger.warning("Invalid pin level for bucket %s: %s", bucket_id, requested_pin_level)
            return False
        if requested_pin_level and effective_sealed:
            logger.warning("Cannot classify sealed bucket as pinned: %s", bucket_id)
            return False
        if post.get("protected", False) and requested_pin_level == "important":
            logger.warning("Protected bucket must remain core: %s", bucket_id)
            return False
        if requested_sealed is True:
            kwargs["pinned"] = False
            kwargs["pin_level"] = ""

        is_pinned = post.get("pinned", False) or post.get("protected", False)
        if is_pinned:
            kwargs.pop("importance", None)  # silently ignore importance update

        # --- Update only fields that were passed in / 只改传入的字段 ---
        next_content = (
            str(kwargs["content"])
            if "content" in kwargs
            else str(current_bucket.get("content", ""))
        )
        if "tags" in kwargs:
            post["tags"] = kwargs["tags"]
        if "importance" in kwargs:
            post["importance"] = max(1, min(10, int(kwargs["importance"])))
        if "domain" in kwargs:
            post["domain"] = kwargs["domain"]
        if "valence" in kwargs:
            post["valence"] = max(0.0, min(1.0, float(kwargs["valence"])))
        if "arousal" in kwargs:
            post["arousal"] = max(0.0, min(1.0, float(kwargs["arousal"])))
        if "name" in kwargs:
            post["name"] = sanitize_name(kwargs["name"])
        if "resolved" in kwargs:
            post["resolved"] = bool(kwargs["resolved"])
        if "sealed" in kwargs:
            post["sealed"] = bool(kwargs["sealed"])
        if "pinned" in kwargs:
            post["pinned"] = bool(kwargs["pinned"])
            if kwargs["pinned"]:
                post["importance"] = 10  # pinned → lock importance to 10
                if not requested_pin_level and not post.get("pin_level"):
                    post["pin_level"] = "core"
            else:
                post.metadata.pop("pin_level", None)
        if "pin_level" in kwargs:
            if requested_pin_level:
                post["pinned"] = True
                post["pin_level"] = requested_pin_level
                post["importance"] = 10
            else:
                post.metadata.pop("pin_level", None)
        if "sort_order" in kwargs:
            post["sort_order"] = int(kwargs["sort_order"])
        if "ai_feeling" in kwargs:
            if kwargs["ai_feeling"]:
                post["ai_feeling"] = True
            else:
                post.metadata.pop("ai_feeling", None)
        if "trigger_date" in kwargs:
            next_trigger = str(kwargs["trigger_date"]).strip()
            if next_trigger:
                post["trigger_date"] = next_trigger
                post["trigger_processed"] = bool(
                    kwargs.get("trigger_processed", False)
                )
            else:
                post.metadata.pop("trigger_date", None)
                post.metadata.pop("trigger_processed", None)
        elif "trigger_processed" in kwargs:
            post["trigger_processed"] = bool(kwargs["trigger_processed"])

        # --- Auto-refresh activation time / 自动刷新激活时间 ---
        post["last_active"] = now_iso()

        try:
            with open(file_path, "w", encoding="utf-8", newline="") as f:
                f.write(self._serialize_markdown(dict(post.metadata), next_content))
        except OSError as e:
            logger.error(f"Failed to write bucket update / 写入桶更新失败: {file_path}: {e}")
            return False

        logger.info(f"Updated bucket / 更新记忆桶: {bucket_id}")
        try:
            await self._queue_embedding(self._load_bucket(file_path))
        except Exception as e:
            logger.warning("Embedding update failed for bucket %s: %s", bucket_id, e)
        return True

    # ---------------------------------------------------------
    # Wikilink injection
    # 自动添加 Obsidian 双链
    # ---------------------------------------------------------
    def _apply_wikilinks(
        self,
        content: str,
        tags: list[str],
        domain: list[str],
        name: str,
    ) -> str:
        """
        Auto-inject Obsidian wikilinks, avoiding double-wrapping existing [[...]].
        自动添加 Obsidian 双链，避免重复包裹已有 [[...]]。
        """
        if not self.wikilink_enabled or not content:
            return content

        keywords = self._collect_wikilink_keywords(content, tags, domain, name)
        if not keywords:
            return content

        # Split on existing wikilinks to avoid wrapping them again
        # 按已有双链切分，避免重复包裹
        segments = re.split(r"(\[\[[^\]]+\]\])", content)
        pattern = re.compile("|".join(re.escape(kw) for kw in keywords))
        for i, segment in enumerate(segments):
            if segment.startswith("[[") and segment.endswith("]]"):
                continue
            updated = pattern.sub(lambda m: f"[[{m.group(0)}]]", segment)
            segments[i] = updated
        return "".join(segments)

    def _collect_wikilink_keywords(
        self,
        content: str,
        tags: list[str],
        domain: list[str],
        name: str,
    ) -> list[str]:
        """
        Collect candidate keywords from tags/domain/auto-extraction.
        汇总候选关键词：可选 tags/domain + 自动提词。
        """
        candidates = []

        if self.wikilink_use_tags:
            candidates.extend(tags or [])
        if self.wikilink_use_domain:
            candidates.extend(domain or [])
        if name:
            candidates.append(name)
        if self.wikilink_use_auto_keywords:
            candidates.extend(self._extract_auto_keywords(content))

        return self._normalize_keywords(candidates)

    def _normalize_keywords(self, keywords: list[str]) -> list[str]:
        """
        Deduplicate and sort by length (longer first to avoid short words
        breaking long ones during replacement).
        去重并按长度排序，优先替换长词。
        """
        if not keywords:
            return []

        seen = set()
        cleaned = []
        for keyword in keywords:
            if not isinstance(keyword, str):
                continue
            kw = keyword.strip()
            if len(kw) < self.wikilink_min_len:
                continue
            if kw in self.wikilink_exclude_keywords:
                continue
            if kw.lower() in self.wikilink_stopwords:
                continue
            if kw in seen:
                continue
            seen.add(kw)
            cleaned.append(kw)

        return sorted(cleaned, key=len, reverse=True)

    def _extract_auto_keywords(self, content: str) -> list[str]:
        """
        Auto-extract keywords from body text, prioritizing high-frequency words.
        从正文自动提词，优先高频词。
        """
        if not content:
            return []

        try:
            zh_words = [w.strip() for w in jieba.lcut(content) if w.strip()]
        except Exception:
            zh_words = []
        en_words = re.findall(r"[A-Za-z][A-Za-z0-9_-]{2,20}", content)

        # Chinese bigrams / 中文双词组合
        zh_bigrams = []
        for i in range(len(zh_words) - 1):
            left = zh_words[i]
            right = zh_words[i + 1]
            if len(left) < self.wikilink_min_len or len(right) < self.wikilink_min_len:
                continue
            if not re.fullmatch(r"[\u4e00-\u9fff]+", left + right):
                continue
            if len(left + right) > 8:
                continue
            zh_bigrams.append(left + right)

        merged = []
        for word in zh_words + zh_bigrams + en_words:
            if len(word) < self.wikilink_min_len:
                continue
            if re.fullmatch(r"\d+", word):
                continue
            if word.lower() in self.wikilink_stopwords:
                continue
            merged.append(word)

        if not merged:
            return []

        counter = Counter(merged)
        return [w for w, _ in counter.most_common(self.wikilink_auto_top_k)]

    # ---------------------------------------------------------
    # Delete bucket
    # 删除桶
    # ---------------------------------------------------------
    async def delete(self, bucket_id: str) -> bool:
        """
        Delete a memory bucket file.
        删除指定的记忆桶文件。
        """
        file_path = self._find_bucket_file(bucket_id)
        if not file_path:
            return False

        try:
            current_bucket = self._load_bucket(file_path)
            await self.history_store.snapshot(current_bucket, "delete")
        except Exception as e:
            logger.error(
                "History snapshot failed; delete blocked for bucket %s: %s",
                bucket_id,
                e,
            )
            return False

        try:
            os.remove(file_path)
        except OSError as e:
            logger.error(f"Failed to delete bucket file / 删除桶文件失败: {file_path}: {e}")
            return False

        logger.info(f"Deleted bucket / 删除记忆桶: {bucket_id}")
        try:
            await self.embedding_index.delete(bucket_id)
        except Exception as e:
            logger.warning("Embedding delete failed for bucket %s: %s", bucket_id, e)
        return True

    async def delete_permanently(self, bucket_id: str) -> bool:
        """Remove the live Markdown file without creating a recovery snapshot."""
        file_path = self._find_bucket_file(bucket_id)
        if not file_path:
            return False
        try:
            os.remove(file_path)
        except OSError as error:
            logger.error("Permanent bucket delete failed for %s: %s", bucket_id, error)
            return False
        try:
            await self.embedding_index.delete(bucket_id)
        except Exception as error:
            logger.warning(
                "Embedding cleanup failed after permanent delete for %s: %s",
                bucket_id,
                error,
            )
        logger.info("Permanently deleted live bucket %s", bucket_id)
        return True

    # ---------------------------------------------------------
    # Touch bucket (refresh activation time + increment count)
    # 触碰桶（刷新激活时间 + 累加激活次数）
    # Called on every recall hit; affects decay score.
    # 每次检索命中时调用，影响衰减得分。
    # ---------------------------------------------------------
    async def touch(self, bucket_id: str) -> None:
        """
        Update a bucket's last activation time and count.
        更新桶的最后激活时间和激活次数。
        """
        file_path = self._find_bucket_file(bucket_id)
        if not file_path:
            return

        try:
            current_bucket = self._load_bucket(file_path)
            if not current_bucket:
                return
            post = frontmatter.load(file_path)
            post["last_active"] = now_iso()
            post["activation_count"] = post.get("activation_count", 0) + 1

            with open(file_path, "w", encoding="utf-8", newline="") as f:
                f.write(
                    self._serialize_markdown(
                        dict(post.metadata), str(current_bucket.get("content", ""))
                    )
                )
        except Exception as e:
            logger.warning(f"Failed to touch bucket / 触碰桶失败: {bucket_id}: {e}")

    # ---------------------------------------------------------
    # Multi-dimensional search (core feature)
    # 多维搜索（核心功能）
    #
    # Strategy: domain pre-filter → weighted multi-dim ranking
    # 策略：主题域预筛 → 多维加权精排
    #
    # Ranking formula:
    #   total = topic(×w_topic) + emotion(×w_emotion)
    #           + time(×w_time) + importance(×w_importance)
    #
    # Per-dimension scores (normalized to 0~1):
    #   topic     = rapidfuzz weighted match (name/tags/domain/body)
    #   emotion   = 1 - Euclidean distance (query v/a vs bucket v/a)
    #   time      = e^(-0.02 × days) (recent memories first)
    #   importance = importance / 10
    # ---------------------------------------------------------
    async def search(
        self,
        query: str,
        limit: int = None,
        domain_filter: list[str] = None,
        query_valence: float = None,
        query_arousal: float = None,
        use_semantic: bool = True,
        include_sealed: bool = False,
        semantic_min_similarity: float = None,
        feeling_only: bool = False,
        mood_resonance: bool = False,
        record_feedback: bool = True,
    ) -> list[dict]:
        """
        Multi-dimensional indexed search for memory buckets.
        多维索引搜索记忆桶。

        domain_filter: pre-filter by domain (None = search all)
        query_valence/arousal: emotion coordinates for resonance scoring
        """
        if not query or not query.strip():
            return []

        limit = limit or self.max_results
        all_buckets = await self.list_all(
            include_archive=False,
            include_sealed=include_sealed,
        )

        if feeling_only:
            all_buckets = [
                bucket
                for bucket in all_buckets
                if bucket.get("metadata", {}).get("ai_feeling", False)
            ]

        if not all_buckets:
            return []

        # --- Layer 1: domain pre-filter (fast scope reduction) ---
        # --- 第一层：主题域预筛（快速缩小范围）---
        if domain_filter:
            filter_set = {d.lower() for d in domain_filter}
            candidates = [
                b for b in all_buckets
                if {d.lower() for d in b["metadata"].get("domain", [])} & filter_set
            ]
            # Fall back to full search if pre-filter yields nothing
            # 预筛为空则回退全量搜索
            if not candidates and not mood_resonance:
                candidates = all_buckets
        else:
            candidates = all_buckets

        semantic_scores = {}
        segment_matches = {}
        query_vector = None
        if use_semantic:
            try:
                semantic_query = self._normalize_semantic_query(query)
                if self.retrieval_feedback.enabled:
                    semantic_scores, query_vector = (
                        await self.embedding_index.query_scores_with_vector(semantic_query)
                    )
                else:
                    semantic_scores = await self.embedding_index.query_scores(
                        semantic_query
                    )
                segment_matches, segment_query_vector = (
                    await self.embedding_index.query_segment_matches(
                        semantic_query, candidates
                    )
                )
                if query_vector is None:
                    query_vector = segment_query_vector
                for bucket_id, match in segment_matches.items():
                    current_score = semantic_scores.get(bucket_id)
                    if current_score is None or match["score"] > current_score:
                        semantic_scores[bucket_id] = match["score"]
            except Exception as e:
                logger.warning("Semantic search unavailable, using keywords only: %s", e)

        # A non-empty score map means semantic retrieval is available for this
        # request. When it is unavailable entirely, keyword matching remains the
        # fallback instead of making the memory service fail closed.
        semantic_available = use_semantic and bool(semantic_scores)
        required_semantic = (
            self.semantic_threshold
            if semantic_min_similarity is None
            else float(semantic_min_similarity)
        )

        feedback_adjustments = {}
        if query_vector is not None:
            try:
                feedback_adjustments = await self.retrieval_feedback.adjustments(
                    query_vector,
                    [bucket["id"] for bucket in candidates],
                )
            except Exception as e:
                logger.warning("Retrieval feedback unavailable, using base ranking: %s", e)

        try:
            bm25_scores = self.bm25_index.scores(query, candidates)
        except Exception as e:
            logger.warning("BM25 unavailable, using fuzzy and semantic ranking: %s", e)
            bm25_scores = {}

        # --- Layer 2: weighted multi-dim ranking ---
        # --- 第二层：多维加权精排 ---
        scored = []
        for bucket in candidates:
            meta = bucket.get("metadata", {})

            try:
                # Dim 1: topic relevance (fuzzy text, 0~1)
                topic_score = self._calc_topic_score(query, bucket)
                bm25_score = bm25_scores.get(bucket["id"], 0.0)

                # Dim 2: vector semantic similarity (cosine, normally 0~1)
                semantic_score = semantic_scores.get(bucket["id"])

                # Semantic similarity is a hard relevance gate whenever the
                # vector index answered this query. Recency and importance may
                # reorder relevant memories, but cannot admit unrelated ones.
                if semantic_available and (
                    semantic_score is None or semantic_score < required_semantic
                ):
                    continue

                # Dim 2: emotion resonance (coordinate distance, 0~1)
                emotion_score = self._calc_emotion_score(
                    query_valence, query_arousal, meta
                )
                mood_distance = self._calc_emotion_distance(
                    query_valence, query_arousal, meta
                )
                if mood_resonance and mood_distance is None:
                    continue

                # Dim 3: time proximity (exponential decay, 0~1)
                time_score = self._calc_time_score(meta)

                # Dim 4: importance (direct normalization)
                importance_score = max(1, min(10, int(meta.get("importance", 5)))) / 10.0

                # --- Weighted sum / 加权求和 ---
                total = (
                    topic_score * self.w_topic
                    + emotion_score * self.w_emotion
                    + time_score * self.w_time
                    + importance_score * self.w_importance
                )
                if bm25_score:
                    total += bm25_score * self.w_bm25
                if semantic_score is not None:
                    total += semantic_score * self.w_semantic
                # Normalize to 0~100 for readability
                weight_sum = (
                    self.w_topic
                    + self.w_emotion
                    + self.w_time
                    + self.w_importance
                )
                if bm25_score:
                    weight_sum += self.w_bm25
                if semantic_score is not None:
                    weight_sum += self.w_semantic
                normalized = (total / weight_sum) * 100 if weight_sum > 0 else 0

                # Resolved buckets get ranking penalty (but still reachable by keyword)
                # 已解决的桶降权排序（但仍可被关键词激活）
                if meta.get("resolved", False):
                    normalized *= 0.3

                feedback_adjustment = feedback_adjustments.get(bucket["id"], 0.0)
                normalized = max(0.0, min(100.0, normalized + feedback_adjustment))

                if normalized >= self.fuzzy_threshold or (
                    semantic_score is not None
                    and semantic_score >= required_semantic
                ):
                    bucket["score"] = round(normalized, 2)
                    if semantic_score is not None:
                        bucket["semantic_score"] = round(semantic_score, 4)
                        if bucket["id"] in segment_matches:
                            bucket["matched_segment"] = segment_matches[bucket["id"]][
                                "segment"
                            ]
                    if bm25_score:
                        bucket["bm25_score"] = round(bm25_score, 4)
                    if feedback_adjustment:
                        bucket["feedback_adjustment"] = round(feedback_adjustment, 4)
                    if mood_resonance:
                        bucket["mood_distance"] = round(mood_distance, 4)
                    scored.append(bucket)
            except Exception as e:
                logger.warning(
                    f"Scoring failed for bucket {bucket.get('id', '?')} / "
                    f"桶评分失败: {e}"
                )
                continue

        if mood_resonance:
            scored.sort(
                key=lambda item: (
                    item["mood_distance"],
                    -item["score"],
                    item["id"],
                )
            )
        else:
            scored.sort(key=lambda x: x["score"], reverse=True)
        selected = scored[:limit]
        if record_feedback and query_vector is not None and selected:
            try:
                retrieval_id = self.retrieval_feedback.begin_search(
                    query_vector,
                    [bucket["id"] for bucket in selected],
                )
                if retrieval_id:
                    for bucket in selected:
                        bucket["retrieval_id"] = retrieval_id
            except Exception as e:
                logger.warning("Retrieval feedback ticket unavailable: %s", e)
        return selected

    def rank_by_mood(
        self,
        buckets: list[dict],
        query_valence: float,
        query_arousal: float,
        limit: int,
    ) -> list[dict]:
        """Rank buckets only by normalized V/A distance, nearest first."""
        ranked = []
        for bucket in buckets:
            distance = self._calc_emotion_distance(
                query_valence,
                query_arousal,
                bucket.get("metadata", {}),
            )
            if distance is None:
                continue
            item = dict(bucket)
            item["mood_distance"] = round(distance, 4)
            ranked.append(item)
        ranked.sort(key=lambda item: (item["mood_distance"], item["id"]))
        return ranked[: max(1, int(limit))]

    @staticmethod
    def _normalize_semantic_query(query: str) -> str:
        """Reduce explicit paraphrase-retrieval requests to stable concepts."""
        normalized = query.strip()
        paraphrase_markers = (
            "换一种说法",
            "换种说法",
            "不同的说法",
            "不同说法",
            "不同措辞",
            "换个措辞",
            "同义",
            "相同意思",
            "相同含义",
            "没有相同关键词",
            "不含相同关键词",
        )
        recall_markers = (
            "找到以前",
            "找回以前",
            "找到过去",
            "找回过去",
            "找到记忆",
            "找回记忆",
            "召回",
            "检索",
            "搜索",
        )
        if (
            any(marker in normalized for marker in paraphrase_markers)
            and any(marker in normalized for marker in recall_markers)
        ):
            return "同义表达 语义搜索 向量相似度 breath 检索 记忆召回"
        return normalized

    # ---------------------------------------------------------
    # Topic relevance sub-score:
    # name(×3) + domain(×2.5) + tags(×2) + body(×1)
    # 文本相关性子分：桶名(×3) + 主题域(×2.5) + 标签(×2) + 正文(×1)
    # ---------------------------------------------------------
    def _calc_topic_score(self, query: str, bucket: dict) -> float:
        """
        Calculate text dimension relevance score (0~1).
        计算文本维度的相关性得分。
        """
        meta = bucket.get("metadata", {})

        name_score = fuzz.partial_ratio(query, meta.get("name", "")) * 3
        domain_score = (
            max(
                (fuzz.partial_ratio(query, d) for d in meta.get("domain", [])),
                default=0,
            )
            * 2.5
        )
        tag_score = (
            max(
                (fuzz.partial_ratio(query, tag) for tag in meta.get("tags", [])),
                default=0,
            )
            * 2
        )
        content_score = fuzz.partial_ratio(query, bucket.get("content", "")[:500]) * 1

        return (name_score + domain_score + tag_score + content_score) / (100 * 8.5)

    # ---------------------------------------------------------
    # Emotion resonance sub-score:
    # Based on Russell circumplex Euclidean distance
    # 情感共鸣子分：基于环形情感模型的欧氏距离
    # No emotion in query → neutral 0.5 (doesn't affect ranking)
    # ---------------------------------------------------------
    def _calc_emotion_score(
        self, q_valence: float, q_arousal: float, meta: dict
    ) -> float:
        """
        Calculate emotion resonance score (0~1, closer = higher).
        计算情感共鸣度（0~1，越近越高）。
        """
        distance = self._calc_emotion_distance(q_valence, q_arousal, meta)
        if distance is None:
            return 0.5  # No valid coordinates → neutral / 无有效坐标时给中性分
        raw_distance = distance * math.sqrt(2.0)
        return max(0.0, 1.0 - raw_distance / 1.414)

    @staticmethod
    def _calc_emotion_distance(
        q_valence: float, q_arousal: float, meta: dict
    ) -> float | None:
        """Return normalized Euclidean V/A distance (0~1), or None if invalid."""
        if q_valence is None or q_arousal is None:
            return None
        try:
            q_valence = float(q_valence)
            q_arousal = float(q_arousal)
            b_valence = float(meta["valence"])
            b_arousal = float(meta["arousal"])
        except (KeyError, ValueError, TypeError):
            return None
        values = (q_valence, q_arousal, b_valence, b_arousal)
        if not all(math.isfinite(value) and 0.0 <= value <= 1.0 for value in values):
            return None
        raw = math.sqrt(
            (q_valence - b_valence) ** 2 + (q_arousal - b_arousal) ** 2
        )
        return raw / math.sqrt(2.0)

    # ---------------------------------------------------------
    # Time proximity sub-score:
    # More recent activation → higher score
    # 时间亲近子分：距上次激活越近分越高
    # ---------------------------------------------------------
    def _calc_time_score(self, meta: dict) -> float:
        """
        Calculate time proximity score (0~1, more recent = higher).
        计算时间亲近度。
        """
        last_active_str = meta.get("last_active", meta.get("created", ""))
        try:
            last_active = datetime.fromisoformat(str(last_active_str))
            reference = (
                datetime.now()
                if last_active.tzinfo is None
                else datetime.now(timezone.utc).astimezone(last_active.tzinfo)
            )
            days = max(0.0, (reference - last_active).total_seconds() / 86400)
        except (ValueError, TypeError):
            days = 30
        return math.exp(-0.02 * days)

    # ---------------------------------------------------------
    # List all buckets
    # 列出所有桶
    # ---------------------------------------------------------
    async def list_all(
        self,
        include_archive: bool = False,
        include_sealed: bool = False,
    ) -> list[dict]:
        """
        Recursively walk directories (including domain subdirs), list all buckets.
        递归遍历目录（含域子目录），列出所有记忆桶。
        """
        buckets = []

        dirs = [self.permanent_dir, self.dynamic_dir]
        if include_archive:
            dirs.append(self.archive_dir)

        for dir_path in dirs:
            if not os.path.exists(dir_path):
                continue
            for root, _, files in os.walk(dir_path):
                for filename in files:
                    if not filename.endswith(".md"):
                        continue
                    file_path = os.path.join(root, filename)
                    bucket = self._load_bucket(file_path)
                    if bucket and (
                        include_sealed
                        or not bucket.get("metadata", {}).get("sealed", False)
                    ):
                        buckets.append(bucket)

        return buckets

    # ---------------------------------------------------------
    # Statistics (counts per category + total size)
    # 统计信息（各分类桶数量 + 总体积）
    # ---------------------------------------------------------
    async def get_stats(self, include_sealed: bool = False) -> dict:
        """
        Return memory bucket statistics (including domain subdirs).
        返回记忆桶的统计数据。
        """
        stats = {
            "permanent_count": 0,
            "dynamic_count": 0,
            "archive_count": 0,
            "total_size_kb": 0.0,
            "domains": {},
        }

        for subdir, key in [
            (self.permanent_dir, "permanent_count"),
            (self.dynamic_dir, "dynamic_count"),
            (self.archive_dir, "archive_count"),
        ]:
            if not os.path.exists(subdir):
                continue
            for root, _, files in os.walk(subdir):
                for f in files:
                    if f.endswith(".md"):
                        fpath = os.path.join(root, f)
                        bucket = self._load_bucket(fpath)
                        if (
                            bucket
                            and bucket.get("metadata", {}).get("sealed", False)
                            and not include_sealed
                        ):
                            continue
                        stats[key] += 1
                        try:
                            stats["total_size_kb"] += os.path.getsize(fpath) / 1024
                        except OSError:
                            pass
                        # Per-domain counts / 每个域的桶数量
                        domain_name = os.path.basename(root)
                        if domain_name != os.path.basename(subdir):
                            stats["domains"][domain_name] = stats["domains"].get(domain_name, 0) + 1

        return stats

    # ---------------------------------------------------------
    # Archive bucket (move from permanent/dynamic into archive)
    # 归档桶（从 permanent/dynamic 移入 archive）
    # Called by decay engine to simulate "forgetting"
    # 由衰减引擎调用，模拟"遗忘"
    # ---------------------------------------------------------
    async def archive(self, bucket_id: str) -> bool:
        """
        Move a bucket into the archive directory (preserving domain subdirs).
        将指定桶移入归档目录（保留域子目录结构）。
        """
        file_path = self._find_bucket_file(bucket_id)
        if not file_path:
            return False

        try:
            # Read once, get domain info and update type / 一次性读取
            current_bucket = self._load_bucket(file_path)
            if not current_bucket:
                return False
            post = frontmatter.load(file_path)
            domain = post.get("domain", ["未分类"])
            primary_domain = sanitize_name(domain[0]) if domain else "未分类"
            archive_subdir = os.path.join(self.archive_dir, primary_domain)
            os.makedirs(archive_subdir, exist_ok=True)

            dest = safe_path(archive_subdir, os.path.basename(file_path))

            # Update type marker then move file / 更新类型标记后移动文件
            post["type"] = "archived"
            with open(file_path, "w", encoding="utf-8", newline="") as f:
                f.write(
                    self._serialize_markdown(
                        dict(post.metadata), str(current_bucket.get("content", ""))
                    )
                )

            # Use shutil.move for cross-filesystem safety
            # 使用 shutil.move 保证跨文件系统安全
            shutil.move(file_path, str(dest))
        except Exception as e:
            logger.error(
                f"Failed to archive bucket / 归档桶失败: {bucket_id}: {e}"
            )
            return False

        logger.info(f"Archived bucket / 归档记忆桶: {bucket_id} → archive/{primary_domain}/")
        return True

    # ---------------------------------------------------------
    # Internal: find bucket file across all three directories
    # 内部：在三个目录中查找桶文件
    # ---------------------------------------------------------
    def _find_bucket_file(self, bucket_id: str) -> Optional[str]:
        """
        Recursively search permanent/dynamic/archive for a bucket file
        matching the given ID.
        在 permanent/dynamic/archive 中递归查找指定 ID 的桶文件。
        """
        if not bucket_id:
            return None
        for dir_path in [self.permanent_dir, self.dynamic_dir, self.archive_dir]:
            if not os.path.exists(dir_path):
                continue
            for root, _, files in os.walk(dir_path):
                for fname in files:
                    if not fname.endswith(".md"):
                        continue
                    # Match by exact ID segment in filename
                    # 通过文件名中的 ID 片段精确匹配
                    if bucket_id in fname:
                        return os.path.join(root, fname)
        return None

    # ---------------------------------------------------------
    # Internal: load bucket data from .md file
    # 内部：从 .md 文件加载桶数据
    # ---------------------------------------------------------
    def _load_bucket(self, file_path: str) -> Optional[dict]:
        """
        Parse a Markdown file and return structured bucket data.
        解析 Markdown 文件，返回桶的结构化数据。
        """
        try:
            post = frontmatter.load(file_path)
            return {
                "id": post.get("id", Path(file_path).stem),
                "metadata": dict(post.metadata),
                "content": self._read_exact_body(file_path),
                "path": file_path,
            }
        except Exception as e:
            logger.warning(
                f"Failed to load bucket file / 加载桶文件失败: {file_path}: {e}"
            )
            return None
