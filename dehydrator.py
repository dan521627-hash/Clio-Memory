# ============================================================
# Module: Dehydration & Auto-tagging (dehydrator.py)
# 模块：数据脱水压缩 + 自动打标
#
# Capabilities:
# 能力：
#   1. Dehydrate: compress memory content into high-density summaries (save tokens)
#      脱水：将记忆桶的原始内容压缩为高密度摘要，省 token
#   2. Sidecar summary: cache summaries without rewriting source text
#      旁路摘要：摘要只进独立缓存，不覆盖记忆桶正文
#   3. Analyze: auto-analyze content for domain/emotion/tags
#      打标：自动分析内容，输出主题域/情感坐标/标签
#
# Operating modes:
# 工作模式：
#   - Primary: OpenAI-compatible API (DeepSeek/Ollama/LM Studio/vLLM/Gemini etc.)
#     主路径：通过 OpenAI 兼容客户端调用 LLM API
#   - Fallback: local keyword extraction when API is unavailable
#     备用路径：API 不可用时用本地关键词提取
#
# Depended on by: server.py
# 被谁依赖：server.py
# ============================================================


import asyncio
import hashlib
import re
import json
import logging
from collections import Counter
import jieba

from openai import AsyncOpenAI

from utils import count_tokens_approx
from summary_cache import SummaryCache

logger = logging.getLogger("ombre_brain.dehydrator")


# --- Dehydration prompt: instructs cheap LLM to compress information ---
# --- 脱水提示词：指导廉价 LLM 压缩信息 ---
DEHYDRATE_PROMPT = """你是一个信息压缩专家。请将以下内容脱水为紧凑摘要。

压缩规则：
1. 提取所有核心事实，去除冗余修饰和重复
2. 保留最新的情绪状态和态度
3. 保留所有待办/未完成事项
4. 关键数字、日期、名称必须保留
5. 目标压缩率 > 70%

输出格式（纯 JSON，无其他内容）：
{
  "core_facts": ["事实1", "事实2"],
  "emotion_state": "当前情绪关键词",
  "todos": ["待办1", "待办2"],
  "keywords": ["关键词1", "关键词2"],
  "summary": "50字以内的核心总结"
}"""


# --- Diary digest prompt: split daily notes into independent memory entries ---
# --- 日记整理提示词：把一大段日常拆分成多个独立记忆条目 ---
DIGEST_PROMPT = """你是一个日记整理专家。用户会发送一段包含今天各种事情的文本（可能很杂乱），请你将其拆分成多个独立的记忆条目。

整理规则：
1. 每个条目应该是一个独立的主题/事件（不要混在一起）
2. 为每个条目自动分析元数据
3. 去除无意义的口水话和重复信息，保留核心内容
4. 同一主题的零散信息应合并为一个条目
5. 如果有待办事项，单独提取为一个条目

输出格式（纯 JSON 数组，无其他内容）：
[
  {
    "name": "条目标题（10字以内）",
    "content": "整理后的内容",
    "domain": ["主题域1"],
    "valence": 0.7,
    "arousal": 0.4,
    "tags": ["标签1", "标签2"],
    "importance": 5
  }
]

主题域可选（选最精确的 1~2 个，只选真正相关的）：
  日常: ["饮食", "穿搭", "出行", "居家", "购物"]
  人际: ["家庭", "恋爱", "友谊", "社交"]
  成长: ["工作", "学习", "考试", "求职"]
  身心: ["健康", "心理", "睡眠", "运动"]
  兴趣: ["游戏", "影视", "音乐", "阅读", "创作", "手工"]
  数字: ["编程", "AI", "硬件", "网络"]
  事务: ["财务", "计划", "待办"]
  内心: ["情绪", "回忆", "梦境", "自省"]
importance: 1-10，根据内容重要程度判断
valence: 0~1（0=消极, 0.5=中性, 1=积极）
arousal: 0~1（0=平静, 0.5=普通, 1=激动）"""


# --- Auto-tagging prompt: analyze content for domain and emotion coords ---
# --- 自动打标提示词：分析内容的主题域和情感坐标 ---
ANALYZE_PROMPT = """你是一个内容分析器。请分析以下文本，输出结构化的元数据。

分析规则：
1. domain（系统分类）：只能从下列七类中选一个，不得创造新词：
   核心与世界观、关系与亲密、日常生活、健康与照护、计划与事务、技术与创作、社交与社区
2. valence（情感效价）：0.0~1.0，0=极度消极 → 0.5=中性 → 1.0=极度积极
3. arousal（情感唤醒度）：0.0~1.0，0=非常平静 → 0.5=普通 → 1.0=非常激动
4. tags：必须与 domain 完全相同，只输出同一个系统分类，不得输出关键词标签
5. suggested_name（建议桶名）：10字以内的简短标题

输出格式（纯 JSON，无其他内容）：
{
  "domain": ["系统分类"],
  "valence": 0.7,
  "arousal": 0.4,
  "tags": ["系统分类"],
  "suggested_name": "简短标题"
}"""


class Dehydrator:
    """
    Data dehydrator + content analyzer.
    Capabilities: sidecar summaries, diary digestion, and auto-tagging.
    Prefers API (better quality); auto-degrades to local (guaranteed availability).
    数据脱水器 + 内容分析器。
    能力：旁路摘要、日记拆分和自动打标；不会改写记忆桶正文。
    优先走 API，API 挂了自动降级到本地。
    """

    def __init__(self, config: dict):
        # --- Read dehydration API config / 读取脱水 API 配置 ---
        dehy_cfg = config.get("dehydration", {})
        self.api_key = dehy_cfg.get("api_key", "")
        self.model = dehy_cfg.get("model", "deepseek-chat")
        self.base_url = dehy_cfg.get("base_url", "https://api.deepseek.com/v1")
        self.max_tokens = dehy_cfg.get("max_tokens", 1024)
        self.temperature = dehy_cfg.get("temperature", 0.1)

        # --- API availability / 是否有可用的 API ---
        self.api_available = bool(self.api_key)

        # --- Initialize OpenAI-compatible client ---
        # --- 初始化 OpenAI 兼容客户端 ---
        # Supports any OpenAI-format API: DeepSeek / Ollama / LM Studio / vLLM / Gemini etc.
        # User only needs to set base_url in config.yaml
        if self.api_available:
            self.client = AsyncOpenAI(
                api_key=self.api_key,
                base_url=self.base_url,
                timeout=60.0,
            )
        else:
            self.client = None
        self.summary_cache = SummaryCache(config)
        self.summary_prompt_hash = hashlib.sha256(
            DEHYDRATE_PROMPT.encode("utf-8")
        ).hexdigest()[:16]
        self._summary_locks: dict[str, asyncio.Lock] = {}

    # ---------------------------------------------------------
    # Dehydrate: compress raw content into concise summary
    # 脱水：将原始内容压缩为精简摘要
    # Try API first, fallback to local
    # 先尝试 API，失败则回退本地
    # ---------------------------------------------------------
    async def dehydrate(self, content: str, metadata: dict = None) -> str:
        """
        Dehydrate/compress memory content.
        Returns formatted summary string ready for Claude context injection.
        对记忆内容做脱水压缩。
        返回格式化的摘要字符串，可直接注入 Claude 上下文。
        """
        if not content or not content.strip():
            return "（空记忆 / empty memory）"

        # --- Content is short enough, no compression needed ---
        # --- 内容已经很短，不需要压缩 ---
        if count_tokens_approx(content) < 100:
            return self._format_output(content, metadata)

        bucket_id = ""
        if metadata and isinstance(metadata, dict):
            bucket_id = str(metadata.get("id", "")).strip()
        content_hash = self.summary_cache.fingerprint(content)

        # --- Reuse a summary only when bucket, source text, model, and prompt match. ---
        if self.api_available and bucket_id:
            try:
                cached = await self.summary_cache.get(
                    bucket_id, content_hash, self.model, self.summary_prompt_hash
                )
                if cached is not None:
                    return self._format_output(cached, metadata)
            except Exception as e:
                logger.warning("Summary cache read failed; continuing normally: %s", e)

        # --- Try API compression first (best quality) ---
        # --- 优先尝试 API 压缩 ---
        if self.api_available:
            lock_key = f"{bucket_id}:{content_hash}:{self.model}:{self.summary_prompt_hash}"
            lock = self._summary_locks.setdefault(lock_key, asyncio.Lock())
            async with lock:
                if bucket_id:
                    try:
                        cached = await self.summary_cache.get(
                            bucket_id,
                            content_hash,
                            self.model,
                            self.summary_prompt_hash,
                        )
                        if cached is not None:
                            return self._format_output(cached, metadata)
                    except Exception as e:
                        logger.warning(
                            "Summary cache recheck failed; continuing normally: %s", e
                        )
                try:
                    result = await self._api_dehydrate(content)
                    if result:
                        if bucket_id:
                            try:
                                await self.summary_cache.put(
                                    bucket_id,
                                    content_hash,
                                    self.model,
                                    self.summary_prompt_hash,
                                    result,
                                )
                            except Exception as e:
                                logger.warning(
                                    "Summary cache write failed; response still usable: %s", e
                                )
                        return self._format_output(result, metadata)
                except Exception as e:
                    logger.warning(
                        f"API dehydration failed, degrading to local / "
                        f"API 脱水失败，降级到本地压缩: {e}"
                    )

        # --- Local compression fallback (works without API) ---
        # --- 本地压缩兜底 ---
        result = self._local_dehydrate(content)
        return self._format_output(result, metadata)

    # ---------------------------------------------------------
    # API call: dehydration
    # API 调用：脱水压缩
    # ---------------------------------------------------------
    async def _api_dehydrate(self, content: str) -> str:
        """
        Call LLM API for intelligent dehydration (via OpenAI-compatible client).
        调用 LLM API 执行智能脱水。
        """
        response = await self.client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": DEHYDRATE_PROMPT},
                {"role": "user", "content": content[:3000]},
            ],
            max_tokens=self.max_tokens,
            temperature=self.temperature,
        )
        if not response.choices:
            return ""
        return response.choices[0].message.content or ""

    # ---------------------------------------------------------
    # Local dehydration (fallback when API is unavailable)
    # 本地脱水（无 API 时的兜底方案）
    # Keyword frequency + sentence position weighting
    # 基于关键词频率 + 句子位置权重
    # ---------------------------------------------------------
    def _local_dehydrate(self, content: str) -> str:
        """
        Local keyword extraction + position-weighted simple compression.
        本地关键词提取 + 位置加权的简单压缩。
        """
        # --- Split into sentences / 分句 ---
        sentences = re.split(r"[。！？\n.!?]+", content)
        sentences = [s.strip() for s in sentences if len(s.strip()) > 5]

        if not sentences:
            return content[:200]

        # --- Extract high-frequency keywords / 提取高频关键词 ---
        keywords = self._extract_keywords(content)

        # --- Score sentences: position weight + keyword hits ---
        # --- 句子评分：开头结尾权重高 + 关键词命中加分 ---
        scored = []
        for i, sent in enumerate(sentences):
            position_weight = 1.5 if i < 3 else (1.2 if i > len(sentences) - 3 else 1.0)
            keyword_hits = sum(1 for kw in keywords if kw in sent)
            score = position_weight * (1 + keyword_hits)
            scored.append((score, sent))

        scored.sort(key=lambda x: x[0], reverse=True)

        # --- Top-8 sentences + keyword list / 取高分句 + 关键词列表 ---
        selected = [s for _, s in scored[:8]]
        summary = "。".join(selected)
        keyword_str = ", ".join(keywords[:10])

        return f"[摘要] {summary}\n[关键词] {keyword_str}"

    # ---------------------------------------------------------
    # Keyword extraction
    # 关键词提取
    # Chinese + English tokenization → stopword filter → frequency sort
    # 中英文分词 + 停用词过滤 + 词频排序
    # ---------------------------------------------------------
    def _extract_keywords(self, text: str) -> list[str]:
        """
        Extract high-frequency keywords using jieba (Chinese + English mixed).
        用 jieba 分词提取高频关键词。
        """
        try:
            words = jieba.lcut(text)
        except Exception:
            words = []
        # English words / 英文单词
        english_words = re.findall(r"[a-zA-Z]{3,}", text.lower())
        words += english_words

        # Stopwords / 停用词
        stopwords = {
            "的", "了", "在", "是", "我", "有", "和", "就", "不", "人",
            "都", "一个", "上", "也", "很", "到", "说", "要", "去",
            "你", "会", "着", "没有", "看", "好", "自己", "这", "他", "她",
            "the", "and", "for", "are", "but", "not", "you", "all", "can",
            "had", "her", "was", "one", "our", "out", "has", "have", "with",
            "this", "that", "from", "they", "been", "said", "will", "each",
        }
        filtered = [
            w for w in words
            if w not in stopwords and len(w.strip()) > 1 and not re.match(r"^[0-9]+$", w)
        ]
        counter = Counter(filtered)
        return [word for word, _ in counter.most_common(15)]

    # ---------------------------------------------------------
    # Output formatting
    # 输出格式化
    # Wraps dehydrated result with bucket name, tags, emotion coords
    # 把脱水结果包装成带桶名、标签、情感坐标的可读文本
    # ---------------------------------------------------------
    def _format_output(self, content: str, metadata: dict = None) -> str:
        """
        Format dehydrated result into context-injectable text.
        将脱水结果格式化为可注入上下文的文本。
        """
        header = ""
        if metadata and isinstance(metadata, dict):
            name = metadata.get("name", "未命名")
            tags = ", ".join(metadata.get("tags", []))
            domains = ", ".join(metadata.get("domain", []))
            try:
                valence = float(metadata.get("valence", 0.5))
                arousal = float(metadata.get("arousal", 0.3))
            except (ValueError, TypeError):
                valence, arousal = 0.5, 0.3
            header = f"📌 记忆桶: {name}"
            if domains:
                header += f" [主题:{domains}]"
            if tags:
                header += f" [标签:{tags}]"
            header += f" [情感:V{valence:.1f}/A{arousal:.1f}]"
            header += "\n"
        return f"{header}{content}"

    # ---------------------------------------------------------
    # Auto-tagging: analyze content for domain + emotion + tags
    # 自动打标：分析内容，输出主题域 + 情感坐标 + 标签
    # Called by server.py when storing new memories
    # 存新记忆时由 server.py 调用
    # ---------------------------------------------------------
    async def analyze(self, content: str) -> dict:
        """
        Analyze content and return structured metadata.
        分析内容，返回结构化元数据。

        Returns: {"domain", "valence", "arousal", "tags", "suggested_name"}
        """
        if not content or not content.strip():
            return self._default_analysis()

        # --- Try API first (best quality) / 优先走 API ---
        if self.api_available:
            try:
                result = await self._api_analyze(content)
                if result:
                    return result
            except Exception as e:
                logger.warning(
                    f"API tagging failed, degrading to local / "
                    f"API 打标失败，降级到本地分析: {e}"
                )

        # --- Local analysis fallback / 本地分析兜底 ---
        return self._local_analyze(content)

    # ---------------------------------------------------------
    # API call: auto-tagging
    # API 调用：自动打标
    # ---------------------------------------------------------
    async def _api_analyze(self, content: str) -> dict:
        """
        Call LLM API for content analysis / tagging.
        调用 LLM API 执行内容分析打标。
        """
        response = await self.client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": ANALYZE_PROMPT},
                {"role": "user", "content": content[:2000]},
            ],
            max_tokens=256,
            temperature=0.1,
        )
        if not response.choices:
            return self._default_analysis()
        raw = response.choices[0].message.content or ""
        if not raw.strip():
            return self._default_analysis()
        return self._parse_analysis(raw)

    # ---------------------------------------------------------
    # Parse API JSON response with safety checks
    # 解析 API 返回的 JSON，做安全校验
    # Ensure valence/arousal in 0~1, domain/tags valid
    # ---------------------------------------------------------
    def _parse_analysis(self, raw: str) -> dict:
        """
        Parse and validate API tagging result.
        解析并校验 API 返回的打标结果。
        """
        try:
            # Handle potential markdown code block wrapping
            # 处理可能的 markdown 代码块包裹
            cleaned = raw.strip()
            if cleaned.startswith("```"):
                cleaned = cleaned.split("\n", 1)[-1].rsplit("```", 1)[0]
            result = json.loads(cleaned)
        except (json.JSONDecodeError, IndexError, ValueError):
            logger.warning(f"API tagging JSON parse failed / JSON 解析失败: {raw[:200]}")
            return self._default_analysis()

        if not isinstance(result, dict):
            return self._default_analysis()

        # --- Validate and clamp value ranges / 校验并钳制数值范围 ---
        try:
            valence = max(0.0, min(1.0, float(result.get("valence", 0.5))))
            arousal = max(0.0, min(1.0, float(result.get("arousal", 0.3))))
        except (ValueError, TypeError):
            valence, arousal = 0.5, 0.3

        return {
            "domain": result.get("domain", ["未分类"])[:3],
            "valence": valence,
            "arousal": arousal,
            "tags": result.get("tags", [])[:5],
            "suggested_name": str(result.get("suggested_name", ""))[:20],
        }

    # ---------------------------------------------------------
    # Local analysis (fallback when API is unavailable)
    # 本地分析（无 API 时的兜底方案）
    # Keyword matching + simple sentiment dictionary
    # 基于关键词 + 简单情感词典匹配
    # ---------------------------------------------------------
    def _local_analyze(self, content: str) -> dict:
        """
        Local keyword + sentiment dictionary analysis.
        本地关键词 + 情感词典的简单分析。
        """
        keywords = self._extract_keywords(content)
        text_lower = content.lower()

        # --- Domain matching by keyword hits ---
        # --- 主题域匹配：基于关键词命中 ---
        domain_keywords = {
            # Daily / 日常
            "饮食": {"吃", "饭", "做饭", "外卖", "奶茶", "咖啡", "麻辣烫", "面包",
                    "超市", "零食", "水果", "牛奶", "食堂", "减肥", "节食"},
            "出行": {"旅行", "出发", "航班", "酒店", "地铁", "打车", "高铁", "机票",
                    "景点", "签证", "护照"},
            "居家": {"打扫", "洗衣", "搬家", "快递", "收纳", "装修", "租房"},
            "购物": {"买", "下单", "到货", "退货", "优惠", "折扣", "代购"},
            # Relationships / 人际
            "家庭": {"爸", "妈", "父亲", "母亲", "家人", "弟弟", "姐姐", "哥哥",
                    "奶奶", "爷爷", "亲戚", "家里"},
            "恋爱": {"爱人", "男友", "女友", "恋", "约会", "接吻", "分手",
                    "暧昧", "在一起", "想你", "同床"},
            "友谊": {"朋友", "闺蜜", "兄弟", "聚", "约饭", "聊天", "群"},
            "社交": {"见面", "被人", "圈子", "消息", "评论", "点赞"},
            # Growth / 成长
            "工作": {"会议", "项目", "客户", "汇报", "deadline", "同事",
                    "老板", "薪资", "合同", "需求", "加班", "实习"},
            "学习": {"课", "考试", "论文", "笔记", "作业", "教授", "讲座",
                    "分数", "选课", "学分"},
            "求职": {"面试", "简历", "offer", "投递", "薪资", "岗位"},
            # Health / 身心
            "健康": {"医院", "复查", "吃药", "抽血", "手术", "心率",
                    "病", "症状", "指标", "体检", "月经"},
            "心理": {"焦虑", "抑郁", "恐慌", "创伤", "人格", "咨询",
                    "安全感", "自残", "崩溃", "压力"},
            "睡眠": {"睡", "失眠", "噩梦", "清醒", "熬夜", "早起", "午觉"},
            # Interests / 兴趣
            "游戏": {"游戏", "steam", "极乐迪斯科", "存档", "通关", "角色",
                    "mod", "DLC", "剧情"},
            "影视": {"电影", "番剧", "动漫", "剧", "综艺", "追番", "上映"},
            "音乐": {"歌", "音乐", "专辑", "live", "演唱会", "耳机"},
            "阅读": {"书", "小说", "读完", "kindle", "连载", "漫画"},
            "创作": {"写", "画", "预设", "脚本", "视频", "剪辑", "P图",
                    "SillyTavern", "插件", "正则", "人设"},
            # Digital / 数字
            "编程": {"代码", "code", "python", "bug", "api", "docker",
                    "git", "调试", "框架", "部署", "开发", "server"},
            "AI": {"模型", "GPT", "Claude", "gemini", "LLM", "token",
                   "prompt", "LoRA", "微调", "推理", "MCP"},
            "网络": {"VPN", "梯子", "代理", "域名", "隧道", "服务器",
                    "cloudflare", "tunnel", "反代"},
            # Affairs / 事务
            "财务": {"钱", "转账", "工资", "花了", "欠", "还款", "借",
                    "账单", "余额", "预算", "黄金"},
            "计划": {"计划", "目标", "deadline", "日程", "清单", "安排"},
            "待办": {"要做", "记得", "别忘", "提醒", "下次"},
            # Inner / 内心
            "情绪": {"开心", "难过", "生气", "哭", "泪", "孤独", "幸福",
                    "伤心", "烦", "委屈", "感动", "温柔"},
            "回忆": {"以前", "小时候", "那时", "怀念", "曾经", "记得"},
            "梦境": {"梦", "梦到", "梦见", "噩梦", "清醒梦"},
            "自省": {"反思", "觉得自己", "问自己", "意识到", "明白了"},
        }

        matched_domains = []
        for domain, kws in domain_keywords.items():
            hits = sum(1 for kw in kws if kw in text_lower)
            if hits >= 2:
                matched_domains.append((domain, hits))
        matched_domains.sort(key=lambda x: x[1], reverse=True)
        domains = [d for d, _ in matched_domains[:3]] or ["未分类"]

        # --- Emotion estimation via simple sentiment dictionary ---
        # --- 情感坐标估算：基于简单情感词典 ---
        positive_words = {"开心", "高兴", "喜欢", "哈哈", "棒", "赞", "爱",
                          "幸福", "成功", "感动", "兴奋", "棒极了",
                          "happy", "love", "great", "awesome", "nice"}
        negative_words = {"难过", "伤心", "生气", "焦虑", "害怕", "无聊",
                          "烦", "累", "失望", "崩溃", "愤怒", "痛苦",
                          "sad", "angry", "hate", "tired", "afraid"}
        intense_words = {"太", "非常", "极", "超", "特别", "十分", "炸",
                         "崩溃", "激动", "愤怒", "狂喜", "very", "so", "extremely"}

        pos_count = sum(1 for w in positive_words if w in text_lower)
        neg_count = sum(1 for w in negative_words if w in text_lower)
        intense_count = sum(1 for w in intense_words if w in text_lower)

        # valence: positive/negative emotion balance
        if pos_count + neg_count > 0:
            valence = 0.5 + 0.4 * (pos_count - neg_count) / (pos_count + neg_count)
        else:
            valence = 0.5

        # arousal: intensity level
        arousal = min(1.0, 0.3 + intense_count * 0.15 + (pos_count + neg_count) * 0.08)

        return {
            "domain": domains,
            "valence": round(max(0.0, min(1.0, valence)), 2),
            "arousal": round(max(0.0, min(1.0, arousal)), 2),
            "tags": keywords[:5],
            "suggested_name": "",
        }

    # ---------------------------------------------------------
    # Default analysis result (empty content or total failure)
    # 默认分析结果（内容为空或完全失败时用）
    # ---------------------------------------------------------
    def _default_analysis(self) -> dict:
        """
        Return default neutral analysis result.
        返回默认的中性分析结果。
        """
        return {
            "domain": ["未分类"],
            "valence": 0.5,
            "arousal": 0.3,
            "tags": [],
            "suggested_name": "",
        }

    # ---------------------------------------------------------
    # Diary digest: split daily notes into independent memory entries
    # 日记整理：把一大段日常拆分成多个独立记忆条目
    # For the "grow" tool — "dump a day's content and it gets organized"
    # 给 grow 工具用，"一天结束发一坨内容"靠这个
    # ---------------------------------------------------------
    async def digest(self, content: str) -> list[dict]:
        """
        Split a large chunk of daily content into independent memory entries.
        将一大段日常内容拆分成多个独立记忆条目。

        Returns: [{"name", "content", "domain", "valence", "arousal", "tags", "importance"}, ...]
        """
        if not content or not content.strip():
            return []

        # --- Try API digest first (best quality, understands semantic splits) ---
        # --- 优先 API 整理 ---
        if self.api_available:
            try:
                result = await self._api_digest(content)
                if result:
                    return result
            except Exception as e:
                logger.warning(
                    f"API diary digest failed, degrading to local / "
                    f"API 日记整理失败，降级到本地拆分: {e}"
                )

        # --- Local split fallback / 本地拆分兜底 ---
        return await self._local_digest(content)

    # ---------------------------------------------------------
    # API call: diary digest
    # API 调用：日记整理
    # ---------------------------------------------------------
    async def _api_digest(self, content: str) -> list[dict]:
        """
        Call LLM API for diary organization.
        调用 LLM API 执行日记整理。
        """
        response = await self.client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": DIGEST_PROMPT},
                {"role": "user", "content": content[:5000]},
            ],
            max_tokens=2048,
            temperature=0.2,
        )
        if not response.choices:
            return []
        raw = response.choices[0].message.content or ""
        if not raw.strip():
            return []
        return self._parse_digest(raw)

    # ---------------------------------------------------------
    # Parse diary digest result with safety checks
    # 解析日记整理结果，做安全校验
    # ---------------------------------------------------------
    def _parse_digest(self, raw: str) -> list[dict]:
        """
        Parse and validate API diary digest result.
        解析并校验 API 返回的日记整理结果。
        """
        try:
            cleaned = raw.strip()
            if cleaned.startswith("```"):
                cleaned = cleaned.split("\n", 1)[-1].rsplit("```", 1)[0]
            items = json.loads(cleaned)
        except (json.JSONDecodeError, IndexError, ValueError):
            logger.warning(f"Diary digest JSON parse failed / JSON 解析失败: {raw[:200]}")
            return []

        if not isinstance(items, list):
            return []

        validated = []
        for item in items:
            if not isinstance(item, dict) or not item.get("content"):
                continue
            try:
                importance = max(1, min(10, int(item.get("importance", 5))))
            except (ValueError, TypeError):
                importance = 5
            try:
                valence = max(0.0, min(1.0, float(item.get("valence", 0.5))))
                arousal = max(0.0, min(1.0, float(item.get("arousal", 0.3))))
            except (ValueError, TypeError):
                valence, arousal = 0.5, 0.3

            validated.append({
                "name": str(item.get("name", ""))[:20],
                "content": str(item.get("content", "")),
                "domain": item.get("domain", ["未分类"])[:3],
                "valence": valence,
                "arousal": arousal,
                "tags": item.get("tags", [])[:5],
                "importance": importance,
            })
        return validated

    # ---------------------------------------------------------
    # Local diary split (fallback when API is unavailable)
    # 本地日记拆分（无 API 时的兜底）
    # Split by blank lines/separators, analyze each segment
    # 按空行/分隔符拆段，每段独立分析
    # ---------------------------------------------------------
    async def _local_digest(self, content: str) -> list[dict]:
        """
        Local paragraph split + per-segment analysis.
        本地按段落拆分 + 逐段分析。
        """
        # Split by blank lines or separators / 按空行或分隔线拆分
        segments = re.split(r"\n{2,}|---+|\n-\s", content)
        segments = [s.strip() for s in segments if len(s.strip()) > 20]

        if not segments:
            # Content too short, treat as single entry
            # 内容太短，整个作为一个条目
            analysis = self._local_analyze(content)
            return [{
                "name": analysis.get("suggested_name", "日记"),
                "content": content.strip(),
                "domain": analysis["domain"],
                "valence": analysis["valence"],
                "arousal": analysis["arousal"],
                "tags": analysis["tags"],
                "importance": 5,
            }]

        items = []
        for seg in segments[:10]:  # Max 10 segments / 最多 10 段
            analysis = self._local_analyze(seg)
            items.append({
                "name": analysis.get("suggested_name", "") or seg[:10],
                "content": seg,
                "domain": analysis["domain"],
                "valence": analysis["valence"],
                "arousal": analysis["arousal"],
                "tags": analysis["tags"],
                "importance": 5,
            })
        return items
