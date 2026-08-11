# ============================================================
# Module: Common Utilities (utils.py)
# 模块：通用工具函数
#
# Provides config loading, logging init, path safety, ID generation, etc.
# 提供配置加载、日志初始化、路径安全校验、ID 生成等基础能力
#
# Depended on by: server.py, bucket_manager.py, dehydrator.py, decay_engine.py
# 被谁依赖：server.py, bucket_manager.py, dehydrator.py, decay_engine.py
# ============================================================

import os
import re
import uuid
import yaml
import logging
from pathlib import Path
from datetime import datetime, timedelta, timezone


BEIJING_TIMEZONE = timezone(timedelta(hours=8), name="UTC+8")


def beijing_now(value: datetime | None = None) -> datetime:
    """Return an aware datetime in fixed UTC+8, independent of host timezone."""
    reference = value or datetime.now(timezone.utc)
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=timezone.utc)
    return reference.astimezone(BEIJING_TIMEZONE)


def normalize_beijing_timestamp(
    value: str | datetime, timespec: str = "seconds"
) -> str:
    """Normalize an explicit timestamp to UTC+8; naive values mean UTC+8."""
    parsed = value
    if isinstance(parsed, str):
        parsed = datetime.fromisoformat(parsed.strip().replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=BEIJING_TIMEZONE)
    else:
        parsed = parsed.astimezone(BEIJING_TIMEZONE)
    return parsed.isoformat(timespec=timespec)


def load_config(config_path: str = None) -> dict:
    """
    Load configuration file.
    加载配置文件。

    Priority: environment variables > config.yaml > built-in defaults.
    优先级：环境变量 > config.yaml > 内置默认值。
    """
    # --- Built-in defaults (fallback so it runs even without config.yaml) ---
    # --- 内置默认配置（兜底，保证即使没有 config.yaml 也能跑）---
    defaults = {
        "transport": "stdio",
        "log_level": "INFO",
        "buckets_dir": os.path.join(os.path.dirname(os.path.abspath(__file__)), "buckets"),
        "merge_threshold": 75,
        "dehydration": {
            "model": "deepseek-chat",
            "base_url": "https://api.deepseek.com/v1",
            "api_key": "",
            "max_tokens": 1024,
            "temperature": 0.1,
        },
        "decay": {
            "lambda": 0.05,
            "threshold": 0.3,
            "check_interval_hours": 24,
            "emotion_weights": {
                "base": 1.0,
                "arousal_boost": 0.8,
            },
        },
        "matching": {
            "fuzzy_threshold": 50,
            "max_results": 5,
        },
        "embeddings": {
            "enabled": True,
            "model": "BAAI/bge-small-zh-v1.5",
            "dimensions": 512,
            "batch_size": 8,
            "similarity_threshold": 0.35,
            "cache_dir": "/models",
            "db_path": "",
        },
        "conflict_detection": {
            "enabled": True,
            "similarity_threshold": 0.78,
            "max_candidates": 3,
            "max_conflicts": 5,
        },
        "history": {
            "db_path": "",
        },
        "fact_timeline": {
            "enabled": True,
            "db_path": "",
            "max_versions_per_response": 20,
        },
        "summary_cache": {
            "enabled": True,
            "db_path": "",
        },
        "retrieval_feedback": {
            "enabled": True,
            "db_path": "",
            "query_similarity_threshold": 0.82,
            "max_adjustment": 5.0,
            "pending_ttl_minutes": 30,
            "max_pending": 256,
        },
        "xinchao": {
            "enabled": True,
            "db_path": "",
            "model": "",
            "base_url": "",
            "exact_dedupe_hours": 24,
            "paraphrase_dedupe_seconds": 600,
            "flash_hours": 24,
            "obsession_hours": 168,
            "obsession_repeats": 3,
            "monologue_enabled": True,
            "monologue_after_hours": 2,
            "normal_cap": 0.90,
            "negative_cap": 0.85,
            "step_minutes": 10,
        },
        "tasks": {
            "enabled": True,
            "auto_extract": True,
            "db_path": "",
            "max_candidates": 8,
            "semantic_threshold": 0.78,
            "exact_dedupe_minutes": 10,
        },
    }

    # --- Load user config from YAML file ---
    # --- 从 YAML 文件加载用户自定义配置 ---
    if config_path is None:
        config_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "config.yaml"
        )

    config = defaults.copy()
    if os.path.exists(config_path):
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                file_config = yaml.safe_load(f) or {}
            if isinstance(file_config, dict):
                config = _deep_merge(defaults, file_config)
            else:
                logging.warning(
                    f"Config file is not a valid YAML dict, using defaults / "
                    f"配置文件不是有效的 YAML 字典，使用默认配置: {config_path}"
                )
        except yaml.YAMLError as e:
            logging.warning(
                f"Failed to parse config file, using defaults / "
                f"配置文件解析失败，使用默认配置: {e}"
            )

    # --- Environment variable overrides (highest priority) ---
    # --- 环境变量覆盖敏感/运行时配置（优先级最高）---
    env_api_key = os.environ.get("OMBRE_API_KEY", "")
    if env_api_key:
        config.setdefault("dehydration", {})["api_key"] = env_api_key

    env_base_url = os.environ.get("OMBRE_BASE_URL", "")
    if env_base_url:
        config.setdefault("dehydration", {})["base_url"] = env_base_url

    env_transport = os.environ.get("OMBRE_TRANSPORT", "")
    if env_transport:
        config["transport"] = env_transport

    env_buckets_dir = os.environ.get("OMBRE_BUCKETS_DIR", "")
    if env_buckets_dir:
        config["buckets_dir"] = env_buckets_dir

    env_embedding_db = os.environ.get("OMBRE_EMBEDDING_DB", "")
    if env_embedding_db:
        config.setdefault("embeddings", {})["db_path"] = env_embedding_db

    env_embedding_cache = os.environ.get("OMBRE_EMBEDDING_CACHE_DIR", "")
    if env_embedding_cache:
        config.setdefault("embeddings", {})["cache_dir"] = env_embedding_cache

    env_history_db = os.environ.get("OMBRE_HISTORY_DB", "")
    if env_history_db:
        config.setdefault("history", {})["db_path"] = env_history_db

    env_fact_timeline_db = os.environ.get("OMBRE_FACT_TIMELINE_DB", "")
    if env_fact_timeline_db:
        config.setdefault("fact_timeline", {})["db_path"] = env_fact_timeline_db

    env_summary_cache_db = os.environ.get("OMBRE_SUMMARY_CACHE_DB", "")
    if env_summary_cache_db:
        config.setdefault("summary_cache", {})["db_path"] = env_summary_cache_db

    env_xinchao_db = os.environ.get("OMBRE_XINCHAO_DB", "")
    if env_xinchao_db:
        config.setdefault("xinchao", {})["db_path"] = env_xinchao_db

    # --- Ensure bucket storage directories exist ---
    # --- 确保记忆桶存储目录存在 ---
    buckets_dir = config["buckets_dir"]
    for subdir in ["permanent", "dynamic", "archive"]:
        os.makedirs(os.path.join(buckets_dir, subdir), exist_ok=True)

    return config


def _deep_merge(base: dict, override: dict) -> dict:
    """
    Deep-merge two dicts; override values take precedence.
    深度合并两个字典，override 的值覆盖 base。
    """
    result = base.copy()
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def setup_logging(level: str = "INFO") -> None:
    """
    Initialize logging system.
    初始化日志系统。

    Note: In MCP stdio mode, stdout is occupied by the protocol;
    logs must go to stderr.
    注意：MCP stdio 模式下 stdout 被协议占用，日志只能走 stderr。
    """
    log_level = getattr(logging, level.upper(), None)
    if not isinstance(log_level, int):
        log_level = logging.INFO

    logging.basicConfig(
        level=log_level,
        format="[%(asctime)s] %(name)s %(levelname)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[logging.StreamHandler()],  # StreamHandler defaults to stderr
    )


def generate_bucket_id() -> str:
    """
    Generate a unique bucket ID (12-char short UUID for readability).
    生成唯一的记忆桶 ID（12 位短 UUID，方便人类阅读）。
    """
    return uuid.uuid4().hex[:12]


def sanitize_name(name: str) -> str:
    """
    Sanitize bucket name, keeping only safe characters.
    Prevents path traversal attacks (e.g. ../../etc/passwd).
    清洗桶名称，只保留安全字符。防止路径遍历攻击。
    """
    if not isinstance(name, str):
        return "unnamed"
    cleaned = re.sub(r"[^\w\s\u4e00-\u9fff-]", "", name, flags=re.UNICODE)
    cleaned = cleaned.strip()[:80]
    return cleaned if cleaned else "unnamed"


def safe_path(base_dir: str, filename: str) -> Path:
    """
    Construct a safe file path, ensuring it stays within base_dir.
    Prevents directory traversal.
    构造安全的文件路径，确保最终路径始终在 base_dir 内部。
    """
    base = Path(base_dir).resolve()
    target = (base / filename).resolve()
    if not str(target).startswith(str(base)):
        raise ValueError(
            f"Path safety check failed / 路径安全检查失败: "
            f"{target} is not inside / 不在 {base} 内"
        )
    return target


def count_tokens_approx(text: str) -> int:
    """
    Rough token count estimate.
    粗略估算 token 数。

    Chinese ≈ 1 char = 1.5 tokens, English ≈ 1 word = 1.3 tokens.
    Used to decide whether dehydration is needed; precision not required.
    中文 ≈ 1字=1.5token，英文 ≈ 1词=1.3token。
    用于判断是否需要脱水压缩，不追求精确。
    """
    if not text:
        return 0
    chinese_chars = len(re.findall(r"[\u4e00-\u9fff]", text))
    english_words = len(re.findall(r"[a-zA-Z]+", text))
    return int(chinese_chars * 1.5 + english_words * 1.3 + len(text) * 0.05)


def now_iso(timespec: str = "seconds") -> str:
    """
    Return current time as ISO format string.
    返回当前时间的 ISO 格式字符串。
    """
    return beijing_now().isoformat(timespec=timespec)
