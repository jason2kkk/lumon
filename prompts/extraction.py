"""
prompts/extraction.py — 数据提取与处理 Prompt

包含：原文摘录提取、FEMWC 评分、帖子过滤、需求聚类
"""

QUOTE_EXTRACTION_PROMPT = """从以下帖子和评论中提取高质量的原文摘录（verbatim quotes）。

## 帖子列表
{posts_json}

输出 JSON 数组（不加代码块标记）：
[
  {{
    "text": "原文摘录（英文原文，严禁翻译或改写）",
    "source_url": "帖子/评论的链接",
    "author": "作者用户名（如有）",
    "score": 评论赞数,
    "platform": "reddit 或 hackernews",
    "context": "这条摘录的上下文（中文简述，1句话）",
    "signal_type": "pain / workaround / willingness_to_pay / competitor_complaint / journey"
  }}
]

=== 提取标准（严格执行） ===

必须提取（高优先级）：
- Lv7 完整用户旅程：尝试 → 失败 → 后果（"I tried X, then Y happened, now I..."）
- Lv6 根因分析：解释了「为什么」痛（"The real problem is..."）
- Lv5 具体场景 + 下游后果（"Every time I [场景], it [后果]"）
- Lv4 Workaround 描述（"I ended up [DIY方案]"）—— 最强产品信号
- 付费信号（"I would pay $X for..."、"worth every penny"）
- 竞品切换（"I switched from X to Y because..."）

过滤掉：
- 模糊抱怨不到 50 字符（"this sucks"、"hate it"）
- 纯 meme/段子
- 无具体信息的附和（"+1"、"same here" 但无细节）
- 跑题内容

=== 关键规则（硬性约束，违反即无效） ===
- 严禁改写原文！必须从帖子/评论内容中逐字符复制（保留原始拼写错误、俚语、格式）
- 禁止将 WebSearch 搜索摘要当作引用来源
- 每条必须附带 source_url（格式：https://reddit.com/r/xxx/comments/xxx/...）
- 如果无法获取原文内容或链接，必须排除该引述，严禁编造
- 优先选高赞评论（score >= 3）
- 目标：6-10 条高质量摘录
- signal_type 要准确分类"""


FEMWC_SCORING_PROMPT = """基于以下需求信息和原文摘录，用 FEMWC 五维模型进行机会评分。

## 需求
{need_title}
{need_description}

## 原文摘录
{quotes_json}

## 帖子统计
帖子数: {post_count}
总赞数: {total_score}
总评论: {total_comments}

输出 JSON（不加代码块标记）：
{{
  "F": {{"score": 1-5, "reasoning": "频率评判依据"}},
  "E": {{"score": 1-5, "reasoning": "情感评判依据"}},
  "M": {{"score": 1-5, "reasoning": "市场评判依据"}},
  "W": {{"score": 1-5, "reasoning": "付费意愿评判依据"}},
  "C": {{"score": 1-5, "reasoning": "竞争空白评判依据"}},
  "total": 0.00,
  "verdict": "值得深挖 / 有潜力但需验证 / 机会有限",
  "summary": "一句话总结（中文）"
}}

=== FEMWC 评分标准 ===

F 频率（权重 30%）：
  1=仅1-2个帖子提到  2=3-5个帖子  3=6-10个帖子  4=11-20个帖子  5=20+个帖子

E 情感强度（权重 20%）：
  1=轻微不便("slightly annoying")  2=中度沮丧("frustrating")
  3=强烈受挫("I can't stand")  4=强烈痛苦("destroying my")  5=绝望/危机("I've given up")

M 市场规模（权重 20%）：
  1=<10K人  2=10K-100K  3=100K-1M  4=1M-10M  5=10M+

W 付费意愿（权重 20%）：
  1=无付费信号  2=价格极度敏感  3=有些付费兴趣
  4=多人提到愿付费("I'd pay for this")  5=不惜代价("shut up and take my money")

C 竞争空白（权重 10%）：
  1=大厂已充分解决  2=有可用方案但不完美  3=部分解决但有明显缺口
  4=现有方案很差  5=完全空白

总分 = F×0.30 + E×0.20 + M×0.20 + W×0.20 + C×0.10"""


POST_FILTER_PROMPT = ""  # 已废弃，过滤逻辑合并到 CLUSTERING_STEP1_PROMPT


# ────────────────────── 两步聚类 ──────────────────────

CLUSTERING_STEP1_PROMPT = """你是需求分析专家。请对以下帖子做两件事：过滤 + 粗分组。

## 研究主题
{topic}

## 帖子列表（每条包含 idx / title / content / score / top_comments）
{posts_json}

=== 任务 ===
1. **过滤**：与研究主题完全无关的帖子（纯新闻、meme、硬件评测、编程教程、Lv1 模糊抱怨）放入 skipped。
   - 必须保留：高情感强度 / 付费意愿 / Workaround / 竞品切换 / 多人共鸣的帖子
   - 只关注能通过 App/软件/AI 解决的需求
   - 数据量保护：帖子 ≤ 15 条时只过滤完全跑题的；至少保留 50%
2. **粗分组**：将保留的帖子按底层需求/痛点分成若干组（只输出索引号）。
   - MECE：每个帖子只属于一个组，不遗漏
   - 组数弹性：≤3帖→1组, 3-8帖→2-3组, 8-20帖→3-5组, 20+帖→4-7组

输出纯 JSON（不加代码块标记）：
{{"groups": [[0, 3, 7], [1, 4, 8], [2, 6]], "skipped": [5, 9]}}"""


CLUSTERING_STEP2_PROMPT = """你是需求分析专家。以下是同一需求组内的帖子，请为这个组生成需求标题、描述和标题翻译。

## 研究主题
{topic}

## 本组帖子
{group_posts_json}

输出纯 JSON（不加代码块标记）：
{{
  "need_title": "具体场景化的需求名称（中文，5-15字）",
  "need_description": "需求描述（中文，2-3句话）",
  "need_title_en": "specific scenario-based demand title in English, 5-12 words",
  "need_description_en": "English demand description, 2-3 concise sentences",
  "title_translations": {{"原始idx": "帖子标题中文翻译"}}
}}

=== 命名质量（最关键） ===
- need_title 必须围绕研究主题「{topic}」的具体场景
- 体现核心用户群体和场景，禁止偏离到相邻但不同的领域
- ❌ 太抽象："用户体验差"、"翻译工具不好用"
- ✅ 好：具体痛点场景，如"视频聊天实时翻译延迟高"

=== need_description ===
- 回答：谁在痛？什么场景？为什么痛（根因）？
- 描述用户行为链条：尝试了什么 → 为什么失败 → 什么后果
- 引用帖子中的具体原话（引号包裹，标注帖子索引）

=== English fields ===
- need_title_en / need_description_en must be natural English versions of the same demand, not literal word-by-word translations.
- Keep the meaning aligned with the Chinese title/description and the evidence posts.
- Do not include Chinese characters in the English fields.

=== title_translations ===
- key 是帖子的原始索引号（字符串），value 是该帖子标题的中文翻译"""


# 保留旧名以兼容回退逻辑
CLUSTERING_PROMPT = CLUSTERING_STEP1_PROMPT
