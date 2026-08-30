"""
prompts/report.py — 报告生成 Prompt

包含：产品方案提案、深挖报告（Phase 2）、最终综合报告、直接报告

方法：搜索矩阵、可追溯引用、FEMWC 机会评估和多角色交叉检查。
"""

# ============================================================
# Phase 1 产品方案提案（讨论结束后生成，作为 Phase 2 深挖的输入）
# ============================================================

PRODUCT_PROPOSAL_PROMPT = """基于以下讨论记录，提炼出一个具体的产品赛道和初步方案。

## 需求主题
{need_title}

## 相关帖子
{posts_summary}

## 讨论记录
{debate_log}

输出 JSON（不加代码块标记）：
{{
  "verdict": "GO / CONDITIONAL / NO-GO",
  "verdict_reason": "一句话判断理由",
  "product_track": "产品赛道（一句话定义这是做什么的产品）",
  "product_name_suggestion": "建议产品名（英文，简洁好记）",
  "one_liner": "一句话产品描述（给用户看的）",
  "pain_point": "核心痛点",
  "target_users": "目标用户画像",
  "jtbd": "When I [场景], I want to [动机], So I can [期望结果]",
  "why_now": "为什么现在做",
  "solution_sketch": "初步方案思路（2-3句话描述 App/软件产品怎么解决问题，不涉及硬件）",
  "key_features": ["3-5 个核心功能点（必须是 App/软件功能，不要涉及硬件设备）"],
  "ai_fit": "strong/moderate/weak",
  "femwc_scores": {{"F": 0, "E": 0, "M": 0, "W": 0, "C": 0, "total": 0.00}},
  "verbatim_quotes": [{{"quote": "原话", "source": "来源"}}],
  "open_questions": ["需要在深挖阶段验证的问题"],
  "debate_summary": "讨论摘要（2-3句话）"
}}"""


# ============================================================
# Phase 2 深挖报告（竞品调研、市场分析、商业价值评估）
# ============================================================

DEEP_DIVE_SYSTEM_PROMPT = """你是一个资深的产品市场调研分析师，代号"调研员"。
你擅长通过联网搜索来获取真实的市场数据和竞品信息。

=== 说话风格 ===
中文交流，像在给老板做产品调研汇报。简洁有干货，不水。
数据要有具体数字，竞品要有真实名字和链接。

=== 工作职责 ===
你拿到了一个经过讨论验证的产品方案，现在要深挖：
1. 目标人群深挖：具体是谁？在哪？多大规模？消费习惯？
2. 竞品调研：App Store / Play Store 上的同类 App 有哪些？收入多少？评分？下载量？优劣势？
3. 使用场景：用户在什么场景下会用？频率？关键触发时刻？
4. 商业价值：市场天花板多大？定价策略？变现模式？增长潜力？

⚠️ 竞品必须是真实存在的 App / 在线工具，不要列举实物产品、硬件设备或线下方案。

每个维度都要有具体数据支撑，不要拍脑袋。

格式：自然语言说话 + <think> 里放详细分析数据。
⚠️ 对话部分简洁，2-3 句话概括发现。"""

DEEP_DIVE_SEARCH_PLAN_PROMPT = """根据以下产品方案，规划搜索计划。

## 产品方案
{product_proposal}

你需要搜索的信息：
1. App Store / Play Store 上同类 App 的名称、下载量、评分、定价
2. 竞品 App 的官网、定价、用户量、融资信息
3. 目标市场规模、增长趋势
4. 用户在 Reddit/HN/社交媒体上对竞品 App 的评价
5. 行业报告、市场分析文章

⚠️ 搜索词要包含 "app"、"tool"、"software" 等关键词，确保搜到的是软件产品而非实物。

输出 JSON（不加代码块标记）：
{{
  "search_queries": ["搜索关键词列表，英文为主，6-10个"],
  "competitor_names": ["已知或疑似竞品名称"],
  "data_points_needed": ["需要找到的关键数据点"]
}}"""

DEEP_DIVE_ANALYSIS_PROMPT = """基于搜索结果，对产品方案进行深度分析。

## 产品方案
{product_proposal}

## 搜索结果
{search_results}

先说你的发现（2-3 句话），然后在 <think> 里输出详细分析 JSON：

<think>
{{
  "target_audience": {{
    "primary": "核心目标用户（具体到职业/身份）",
    "size_estimate": "目标市场人群规模估算",
    "where_they_are": "他们在哪些平台/社区活跃",
    "spending_habits": "消费习惯和付费意愿",
    "demographics": "年龄/地域/收入水平"
  }},
  "competitors": [
    {{
      "name": "竞品 App 名（必须是真实软件产品，不要列实物）",
      "url": "官网或 App Store 链接",
      "description": "一句话描述",
      "pricing": "定价策略",
      "revenue_estimate": "收入估算（如果有数据）",
      "funding": "融资情况",
      "user_count": "用户量估算",
      "strengths": ["优势"],
      "weaknesses": ["劣势"],
      "user_sentiment": "用户评价倾向"
    }}
  ],
  "market": {{
    "tam": "总可达市场",
    "sam": "可服务市场",
    "som": "可获得市场",
    "growth_rate": "年增长率",
    "trends": ["市场趋势"]
  }},
  "usage_scenarios": [
    {{
      "scenario": "具体使用场景",
      "frequency": "使用频率",
      "trigger": "触发时刻",
      "current_solution": "目前怎么解决的"
    }}
  ],
  "business_model": {{
    "pricing_strategy": "建议定价策略",
    "revenue_streams": ["收入来源"],
    "unit_economics": "单位经济学分析",
    "ceiling": "市场天花板评估",
    "payback_period": "回本周期估算"
  }},
  "risks": ["关键风险"],
  "recommendation": "最终建议（1-2句话）"
}}
</think>"""


# ============================================================
# 最终综合报告（融合 Phase 1 + Phase 2）
# ============================================================

FINAL_REPORT_PROMPT = """基于讨论记录和深挖调研数据，生成最终产品评估报告。严格按模板章节顺序输出 Markdown，每个章节不可省略。

## 需求主题
{need_title}

## 相关帖子
{posts_summary}

## 讨论记录
{debate_log}

## 深挖调研数据（如有）
{deep_dive_data}

---

严格按以下模板输出完整报告（7 个章节 + 附录）：

# {need_title}

- 数据来源：讨论 + 帖子调研
- Agent：Lumon v1.0

## 结论

**[用一句明确但校准过的判断句，说明当前证据支持到什么程度。例如"当前样本显示问题反复出现，但单一社区的 5 条帖子不足以证明普遍付费需求，建议先验证"。当样本少于 10 条、只有单一社区或没有直接付费证据时，禁止写"需求真实且付费信号明确"、"建议立项"或类似强结论。]**

判断依据：[只写帖子/讨论中可定位的信号；没有直接付费或市场数据时，明确写"未观察到"，不要推断为已验证。]

**核心发现：**

1. [发现1：基于讨论中的关键论点]

2. [发现2]

3. [发现3]

4. [发现4]

**关键争议：**

| 议题 | 产品经理立场 | 杠精立场 | 结论 |
|------|-------------|---------|------|
（从讨论记录中提炼 2-3 个核心争议点）

## 需求洞察

Top 5 痛点，按强度从高到低。每个痛点格式：

### 1. [痛点名称]
**强度：[高/中高/中]** | **场景：**[用户在什么场景下遇到这个痛点]

[1-2 句话解释为什么痛]

> "[原文完整句子引述，不少于 10 个英文词]"（中文翻译：[翻译]）— 来源：[帖子标题]

> "[原文完整句子引述]"（中文翻译：[翻译]）— 来源：[帖子标题]

### 2. [痛点名称]
...（同上格式，共 5 个）

## 竞品格局

| 竞品名称 | 定价 | 近30天收入 | 近30天下载量 | 产品定位 | App Store链接 | SensorTower链接 |
|---|---|---:|---:|---|---|---|
3-6 个软件竞品。定价具体到 $X/月；近30天收入/下载量必须使用结构化竞品表数据里的 SensorTower 数字，无数据填 "-"，不要编造。产品定位只写一句话。两个链接列使用 [↗](url)，没有链接填 "-"。

**定位空白：** [用一句话描述竞品集体未覆盖的象限，格式如"现有产品集中在'X+Y'象限，空白在'A+B'象限"]

## 产品方案

3 个具体可落地的产品方案。⚠️ 三个方案必须代表**不同的产品形态或商业模型**（如一个订阅 App、一个免费工具+增值、一个内容/社区驱动），不能是同一产品的功能拆分。

### 方案 1：[方案名称]

**清晰的用户**

- **目标人群**：[精确到"什么身份/职业 + 什么行为特征 + 什么场景"]

- **触达渠道**：[这群人聚集在哪里、通过什么渠道可以精准找到他们]

**真实的需求**

- **核心痛点**：[1-2 句说清这群人当前的具体困难]

- **证据**：[来自帖子/讨论的原文完整句子引述（不少于 10 词），证明痛点真实存在]

- **竞品盲区**：[现有竞品为什么没解决好]

**简单的产品**

- **产品形态**：[App / Chrome 扩展 / Web 应用 / AI 工具]

- **核心流程**：
  1. [步骤1]
  2. [步骤2]
  3. [步骤3]

- **MVP 范围**：[第一版做什么 + 不做什么]

**冒烟测试**

- **验证方式**：[具体在哪个渠道、用什么具体文案/形式投放、需要多少样本量]

- **成功标准**：[基于该品类基准的合理数据目标，附简要推导]

### 方案 2：[方案名称]
...（同上结构）

### 方案 3：[方案名称]
...（同上结构）

## 商业评估

- **市场规模**：[只有带来源的公开数据才写具体数字；没有可靠来源时写"本次未取得可验证的市场规模数据"，不要用常识或模型记忆补数字]

- **商业模式**：[建议的定价策略和收入模式]

- **AI 适配度**：[strong / moderate / weak] — [一句话说明]

- **关键风险**：

  1. [风险1]

  2. [风险2]

  3. [风险3]

## 下一步行动

1. [可执行行动1，附具体做法]

2. [可执行行动2]

3. [可执行行动3]

## 附录

**证据来源：** 列出高/中相关帖子标题和 URL（Markdown 链接格式）。

**研究局限：** [1-2 句话说明数据量、覆盖范围等限制]

---
⚠️ 约束：
- 分析用中文，用户原话保留原文 + 括号内中文翻译
- 引用来自帖子和讨论记录，不编造；每条引述必须是完整句子（不少于 10 个英文词），并标注来源帖标题
- 只建议面向 C 端海外市场的 App / AI 工具 / 软件方案
- 不编造下载量/收入/月活等数字；市场规模必须给出具体数字或数量级
- 输出纯 Markdown（不要输出 JSON）
- 主题锚定：所有内容紧密围绕「{need_title}」，不偏离
- **产品方案**是核心输出章节，三个方案必须代表不同产品形态/商业模型，严格按四维度输出
- **排版要求**：所有编号列表项（1. 2. 3. ...）和 bullet 列表项（- ...）之间必须用空行分隔；核心流程的步骤必须缩进在"核心流程"下方（用 2 空格缩进），确保渲染时对齐
- 整份报告控制在 7000-8000 字以内
"""


# ============================================================
# 报告前置：需求理解 + 信号提炼（从帖子中提取与需求相关的内容）
# ============================================================

SIGNAL_EXTRACTION_PROMPT = """你是一位资深的用户研究分析师。你的任务是：先深入理解需求主题，再从帖子数据中提炼出与该需求**直接相关**的信号。

## 需求主题
{need_title}

## 需求描述
{need_description}

## 帖子数据
{posts_summary}

=== 第一步：需求理解 ===
先用 2-3 句话描述你对「{need_title}」这个需求的理解：
- 核心用户是谁？
- 核心场景是什么？
- 核心痛点是什么？

=== 第二步：信号提炼 ===
逐个分析每个帖子，从中提取与「{need_title}」**直接相关**的信号。

对每个帖子：
1. 判断与需求的关联度：高 / 中 / 低 / 无关
2. 如果有关联，提取帖子中与需求相关的具体内容（原文引述、场景描述、痛点表达）
3. 即使帖子主题不完全匹配，也要挖掘其中与需求相关的子话题或侧面信息

⚠️ 关键规则：
- 如果帖子主要谈"语言学习"但涉及"和伴侣/家人的跨语言沟通"，提取后者相关的信号
- 关注帖子评论区中与需求相关的讨论
- 只提取事实和原文，不要编造

输出格式（JSON，不加代码块标记）：
{{
  "need_understanding": {{
    "core_users": "核心用户画像",
    "core_scenario": "核心使用场景",
    "core_pain": "核心痛点"
  }},
  "extracted_signals": [
    {{
      "post_title": "帖子标题",
      "post_url": "帖子URL",
      "relevance": "高/中/低/无关",
      "relevant_content": "与需求直接相关的内容摘要（保留原文关键引述）",
      "verbatim_quotes": ["与需求相关的原文逐字引述"],
      "pain_points": ["从这个帖子中提取的、与需求相关的痛点"],
      "scenarios": ["与需求相关的使用场景"]
    }}
  ],
  "overall_signal_summary": "综合所有帖子的信号，用 3-5 句话总结与「{need_title}」最相关的核心发现"
}}"""


# ============================================================
# 直接生成报告（无需辩论，从 need + posts 直接产出 Markdown 报告）
# ============================================================

DIRECT_REPORT_PROMPT = """基于以下数据生成一份**产品机会研究报告**。严格按模板章节顺序输出，每个章节不可省略。

## 需求
- 主题：{need_title}
- 描述：{need_description}

## 帖子数据（按相关度分层）
{posts_summary}

## 深挖数据
{deep_dive_data}

## 竞品调研数据
{competitor_research}

---

严格按以下模板输出完整报告（7 个章节 + 附录）：

# {need_title}

- 数据来源：{sources}
- 帖子数：{post_count}
- Agent：Lumon v1.0

## 结论

**[用一句明确但校准过的判断句，说明当前证据支持到什么程度。样本少于 10 条、只有单一社区或没有直接付费证据时，必须明确写"证据不足/待验证"，禁止把小样本写成普遍需求或明确付费意愿。]**

判断依据：[只写可由帖子定位的数量和原文信号；没有直接付费证据时写"未观察到"，不要把痛点表达升级为付费意愿。]

**核心发现：**

1. [发现1：揭示"为什么"和"意味着什么"]

2. [发现2]

3. [发现3]

4. [发现4]

## 需求洞察

Top 5 痛点，按强度从高到低。每个痛点格式：

### 1. [痛点名称]
**强度：[高/中高/中]** | **场景：**[用户在什么场景下遇到这个痛点]

[1-2 句话解释为什么痛]

> "[原文完整句子引述，不少于 10 个英文词]"（中文翻译：[翻译]）— 来源：[帖子标题]

> "[原文完整句子引述]"（中文翻译：[翻译]）— 来源：[帖子标题]

### 2. [痛点名称]
...（同上格式，共 5 个）

## 竞品格局

| 竞品名称 | 定价 | 近30天收入 | 近30天下载量 | 产品定位 | App Store链接 | SensorTower链接 |
|---|---|---:|---:|---|---|---|
3-6 个软件竞品。定价具体到 $X/月；近30天收入/下载量必须使用结构化竞品表数据里的 SensorTower 数字，无数据填 "-"，不要编造。产品定位只写一句话。两个链接列使用 [↗](url)，没有链接填 "-"。

**定位空白：** [用一句话描述竞品集体未覆盖的象限，格式如"现有产品集中在'X+Y'象限，空白在'A+B'象限"]

## 产品方案

3 个具体可落地的产品方案。⚠️ 三个方案必须代表**不同的产品形态或商业模型**（如一个订阅 App、一个免费工具+增值、一个内容/社区驱动），不能是同一产品的功能拆分。

### 方案 1：[方案名称]

**清晰的用户**

- **目标人群**：[精确到"什么身份/职业 + 什么行为特征 + 什么场景"。不要写"手机用户""年轻人"等模糊群体]

- **触达渠道**：[这群人聚集在哪里、通过什么渠道可以精准找到他们]

**真实的需求**

- **核心痛点**：[1-2 句说清这群人当前的具体困难]

- **证据**：[来自帖子的原文完整句子引述（不少于 10 词），证明痛点真实存在]

- **竞品盲区**：[现有竞品为什么没解决好]

**简单的产品**

- **产品形态**：[App / Chrome 扩展 / Web 应用 / AI 工具]

- **核心流程**：
  1. [步骤1]
  2. [步骤2]
  3. [步骤3]

- **MVP 范围**：[第一版做什么 + 不做什么]

**冒烟测试**

- **验证方式**：[具体在哪个渠道（如 r/xxx 子版块、ProductHunt）、用什么具体文案/形式投放、需要多少样本量]

- **成功标准**：[基于该品类基准的合理数据目标，附简要推导]

### 方案 2：[方案名称]
...（同上结构）

### 方案 3：[方案名称]
...（同上结构）

## 商业评估

- **市场规模**：[只有带来源的公开数据才写具体数字；没有可靠来源时写"本次未取得可验证的市场规模数据"，不要用模型记忆补数字]

- **商业模式**：[建议的定价策略和收入模式]

- **AI 适配度**：[strong / moderate / weak] — [一句话说明 AI 在产品中的角色]

- **关键风险**：

  1. [风险1]

  2. [风险2]

  3. [风险3]

## 下一步行动

1. [可执行行动1，附具体做法]

2. [可执行行动2]

3. [可执行行动3]

## 附录

**证据来源：** 列出高/中相关帖子标题和 URL（Markdown 链接格式）。

**研究局限：** [1-2 句话说明数据量、覆盖范围等限制]

---
⚠️ 约束：
- 分析用中文，用户原话保留原文 + 括号内中文翻译
- 引用来自帖子数据，不编造；每条引述必须是完整句子（不少于 10 个英文词），并标注来源帖标题
- 只建议面向 C 端海外市场的 App / AI 工具 / 软件方案
- 不编造下载量/收入/月活等数字；市场规模必须给出具体数字或数量级
- 输出纯 Markdown
- 主题锚定：所有内容紧密围绕「{need_title}」，不偏离
- **产品方案**是核心输出章节，三个方案必须代表不同产品形态/商业模型，严格按四维度输出
- **排版要求**：所有编号列表项（1. 2. 3. ...）和 bullet 列表项（- ...）之间必须用空行分隔；核心流程的步骤必须缩进在"核心流程"下方（用 2 空格缩进），确保渲染时对齐
- 整份报告控制在 7000-8000 字以内，优先保证产品方案的深度
"""


DIRECT_REPORT_PROMPT_EN = """Generate a **Product Opportunity Research Report** from the data below. Follow the section order exactly and do not omit any section.

## Demand
- Topic: {need_title}
- Description: {need_description}

## Post Data (tiered by relevance)
{posts_summary}

## Deep Research Data
{deep_dive_data}

## Competitor Research Data
{competitor_research}

---

Output the full report in this exact Markdown structure (7 sections + appendix):

# [Concise English opportunity title based on "{need_title}"]

- Data sources: {sources}
- Posts reviewed: {post_count}
- Agent: Lumon v1.0

## Conclusion

**[Write one clear judgment sentence explaining whether this opportunity is worth pursuing and why. For example: "The demand is real and shows clear willingness to pay; recommend validating an MVP." Do not use vague GO/NO-GO labels.]**

Basis for judgment: [Key quantitative signals extracted from posts, such as "X posts expressed explicit willingness to pay" or "Y% of high-relevance posts describe the same pain point".]

**Key findings:**

1. [Finding 1: explain the "why" and what it means]

2. [Finding 2]

3. [Finding 3]

4. [Finding 4]

## Demand Insights

Top 5 pain points, ordered by strength. Use this format for each pain point:

### 1. [Pain point name]
**Intensity: [High / Medium-high / Medium]** | **Scenario:** [The situation where users experience this pain]

[1-2 sentences explaining why it hurts]

> "[Original full quote, at least 10 English words]" (English note: [brief explanation if needed]) — Source: [post title]

> "[Original full quote]" (English note: [brief explanation if needed]) — Source: [post title]

### 2. [Pain point name]
... (same format, 5 pain points total)

## Competitive Landscape

| Competitor | Pricing | Last 30D Revenue | Last 30D Downloads | Positioning | App Store | SensorTower |
|---|---|---:|---:|---|---|---|
List 3-6 real software competitors. Pricing should be specific, such as $X/month; Last 30D Revenue / Downloads must copy the SensorTower numbers from the structured competitor table. Use "-" when data is unavailable. Do not fabricate. Positioning must be one concise sentence. Link columns must use [↗](url); use "-" when no link is available.

**Positioning gap:** [One sentence describing the uncovered segment, e.g. "Most competitors cluster around 'X + Y', while the gap is 'A + B'."]

## Product Concepts

Provide 3 concrete product concepts. Each concept must be a simple app/software product and must differ by user segment, product angle, or monetization model. Do not split one product into three feature lists.

### Concept 1: [Concept name]

**Clear users**

- **Target users:** [Specific identity/role + behavior + scenario. Avoid vague groups like "mobile users" or "young people".]

- **Acquisition channels:** [Where these users gather and how to reach them precisely.]

**Real demand**

- **Core pain:** [1-2 sentences explaining the user's concrete difficulty.]

- **Evidence:** [A full original quote from posts, at least 10 words, proving the pain exists.]

- **Competitor gap:** [Why existing competitors do not solve this well.]

**Simple product**

- **Product function:** [A one-sentence description of what the app does and the smallest feature set needed to validate the demand. Do not recommend web/community/hardware products.]

- **Core flow:**
  1. [Step 1]
  2. [Step 2]
  3. [Step 3]

- **MVP scope:** [What version 1 includes + what it intentionally excludes.]

**Smoke test**

- **Validation method:** [Exact channel, copy angle/form, and sample size.]

- **Success criteria:** [Reasonable benchmark with brief reasoning.]

### Concept 2: [Concept name]
... (same structure)

### Concept 3: [Concept name]
... (same structure)

## Business Assessment

- **Market size:** [Give a concrete number or order of magnitude, such as "the global XXX market is about $XB, with the YYY niche around $ZM"; can be inferred from public data but must not be fabricated.]

- **Business model:** [Recommended pricing and monetization model.]

- **AI fit:** [strong / moderate / weak] — [One sentence explaining AI's role in the product.]

- **Key risks:**

  1. [Risk 1]

  2. [Risk 2]

  3. [Risk 3]

## Next Steps

1. [Actionable step 1 with concrete execution details]

2. [Actionable step 2]

3. [Actionable step 3]

## Appendix

**Evidence sources:** List high/medium relevance post titles and URLs in Markdown link format.

**Research limitations:** [1-2 sentences about data volume, coverage, and confidence limits.]

---
Constraints:
- Write the entire analysis in English. Keep original user quotes verbatim; do not translate quote text unless it is non-English.
- Use a natural English report title. If the original demand topic is non-English, translate it instead of copying it verbatim.
- Use only evidence from the provided post data. Do not invent quotes. Each quote must be a full sentence of at least 10 English words and must cite the source post title.
- Recommend only consumer-facing overseas-market apps, AI tools, or software products. Do not recommend physical hardware, offline services, broad websites, or communities as the product form.
- Do not fabricate downloads, revenue, MAU, market size, or pricing. Market size must include a concrete number or order of magnitude.
- Output pure Markdown.
- Topic anchoring: every section must stay tightly focused on "{need_title}".
- The Product Concepts section is the core output. Each of the three concepts must follow the four-part structure exactly.
- Formatting requirement: put a blank line between all numbered list items and bullet list items. Core-flow steps must be indented under "Core flow" with two spaces.
- Keep the full report within 6,000-8,000 English words, prioritizing depth in Product Concepts.
"""
