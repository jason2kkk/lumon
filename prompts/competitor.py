"""
prompts/competitor.py — 竞品调研 Prompt

包含：竞品搜索规划、竞品信息提取
"""

COMPETITOR_SEARCH_PROMPT = """你是竞品调研专家。根据以下需求主题，生成用于搜索 App Store / 软件市场竞品的关键词。

## 需求主题
{need_title}

## 需求描述
{need_description}

## 已有帖子概要
{posts_hint}

输出 JSON（不加代码块标记）：
{{
  "search_queries": [
    "best [topic] app",
    "[topic] app review",
    "[topic] software tool",
    "site:apps.apple.com [关键词]",
    "[竞品名] alternative app",
    "[竞品名] app store",
    "[topic] ios app"
  ],
  "competitor_hints": ["从帖子中提到的 App/工具名"]
}}

要求：
- 生成 8-12 个英文搜索词
- 必须包含 2-3 条 "site:apps.apple.com [关键词]" 格式的搜索词，用于直接定位 App Store 链接
- 必须包含 1-2 条 "[竞品名/关键词] app store" 格式的搜索词
- 搜索词要能搜到 App Store / Play Store / ProductHunt 中的真实软件产品
- 如果帖子中已提到了具体的 App 或工具名称，放入 competitor_hints，并额外生成 "[竞品名] ios app store" 搜索词
- 避免搜到实物/硬件产品的搜索词"""


COMPETITOR_EXTRACT_PROMPT = """你是竞品分析专家。根据以下搜索结果，提取出真实的 App/软件竞品信息。

## 需求主题
{need_title}

## 搜索结果
{search_results}

输出 JSON 数组（不加代码块标记）：
[
  {{
    "name": "App/产品名称",
    "type": "app / chrome_extension / web_tool",
    "description": "一句话描述",
    "url": "官网链接",
    "app_store_url": "iOS App Store 链接（https://apps.apple.com/...），如无则留空",
    "play_store_url": "Google Play 链接（https://play.google.com/...），如无则留空",
    "pricing": "定价信息（如有）",
    "b2b_b2c": "B2B / B2C / Both",
    "ai_driven": "是 / 部分 / 否",
    "strengths": "核心优势",
    "weaknesses": "核心劣势（从用户评价中提取）",
    "app_store_rating": "评分（如有）",
    "downloads_estimate": "下载量估算（如有）"
  }}
]

严格要求：
- 只提取真实存在的 App、在线工具、Chrome 扩展等**软件产品**
- **绝对排除**：实物产品（家具、设备、喷雾剂等）、线下服务、课程培训
- 如果搜索结果中没有明确的软件竞品信息，返回空数组 []
- 每个竞品必须有真实的产品名称
- **app_store_url 最重要**：这是最高优先级字段！
  - 从搜索结果中提取 apps.apple.com 链接
  - 如果搜索结果中明确出现了 apps.apple.com/... 的 URL，必须完整填入
  - 格式示例：https://apps.apple.com/app/appname/id123456789
- **play_store_url**：同理提取 play.google.com 链接
- **url（官网）**：只有在找不到 app_store_url 和 play_store_url 时才作为备选
- 目标：提取 5-10 个最相关的竞品"""
