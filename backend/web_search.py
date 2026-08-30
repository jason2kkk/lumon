"""
web_search.py — Tavily web search integration for Phase 2 deep dive.

Provides web search capabilities for market research, competitor analysis,
and product opportunity validation.
"""

import os
import json
import re
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Generator

from .llm_client import call_llm, record_usage, get_provider_config, get_thread_session
from prompts import DEEP_DIVE_SEARCH_PLAN_PROMPT


# ---------------------------------------------------------------------------
# Responses API helper — LiteLLM 中转站通过 /v1/responses 支持 web_search
# ---------------------------------------------------------------------------

def _responses_web_search(
    client,
    model: str,
    prompt: str,
    *,
    system: str | None = None,
    max_output_tokens: int | None = None,
) -> str | None:
    """通过 OpenAI Responses API 执行 web_search，返回 output_text。"""
    input_text = prompt
    if system:
        input_text = f"[System] {system}\n\n{prompt}"
    kwargs: dict = {
        "model": model,
        "input": input_text,
        "tools": [{"type": "web_search"}],
        "tool_choice": "auto",
    }
    if max_output_tokens:
        kwargs["max_output_tokens"] = max_output_tokens
    resp = client.responses.create(**kwargs)
    usage = getattr(resp, "usage", None)
    if usage:
        provider_key = "gpt" if "gpt" in model.lower() else "claude"
        record_usage(provider_key, usage)
    text = (resp.output_text or "").strip()
    return text if text else None


@dataclass(frozen=True)
class WebSearchProbeResult:
    ok: bool
    status: str
    retryable: bool = False


def _classify_web_search_probe_error(error: Exception) -> WebSearchProbeResult:
    """把 SDK/中转站异常归类，避免把网络故障误报为不支持工具。"""
    message = str(error).lower()
    error_name = type(error).__name__.lower()
    status_code = getattr(error, "status_code", None)

    if status_code in (401, 403):
        return WebSearchProbeResult(False, "authentication_failed")
    if status_code == 429:
        return WebSearchProbeResult(False, "rate_limited", retryable=True)
    if "timeout" in error_name or "timeout" in message or "timed out" in message:
        return WebSearchProbeResult(False, "timeout", retryable=True)

    tool_context = "web_search" in message or "web search" in message or "tool" in message
    unsupported_markers = (
        "unsupported", "not support", "unknown tool", "unrecognized tool",
        "invalid tool", "tool is not allowed",
    )
    if tool_context and any(marker in message for marker in unsupported_markers):
        return WebSearchProbeResult(False, "unsupported")
    if status_code == 404:
        return WebSearchProbeResult(False, "responses_api_unavailable")

    connection_markers = ("connection", "connect", "network", "dns", "ssl", "eof")
    if "connection" in error_name or any(marker in message for marker in connection_markers):
        return WebSearchProbeResult(False, "network_error", retryable=True)
    if isinstance(status_code, int) and status_code >= 500:
        return WebSearchProbeResult(False, "upstream_error", retryable=True)
    return WebSearchProbeResult(False, "request_failed", retryable=True)


def _probe_responses_web_search(
    client,
    model: str,
    provider: str = "GPT",
    *,
    attempts: int = 1,
) -> WebSearchProbeResult:
    """检测 Responses API web_search，并保留可供界面解释的失败类型。"""
    attempts = max(1, attempts)
    for attempt in range(attempts):
        try:
            result = _responses_web_search(
                client, model,
                "Search the web: What is the current price of Bitcoin today in USD? Reply with just the price number.",
                max_output_tokens=100,
            )
            if result and any(c.isdigit() for c in result):
                return WebSearchProbeResult(True, "available")
            no_access = ("don't have access", "cannot browse", "unable to search", "cannot search")
            if result and any(phrase in result.lower() for phrase in no_access):
                return WebSearchProbeResult(False, "unsupported")
            if result:
                print(f"[{provider} responses web_search test] ambiguous: {result[:100]}")
                return WebSearchProbeResult(True, "available")
            return WebSearchProbeResult(False, "empty_response", retryable=True)
        except Exception as error:
            probe = _classify_web_search_probe_error(error)
            print(f"[{provider} responses web_search test] {probe.status}: {error}")
            if not probe.retryable or attempt + 1 >= attempts:
                return probe

    return WebSearchProbeResult(False, "request_failed", retryable=True)


def _test_responses_web_search(client, model: str, provider: str = "GPT") -> bool:
    """兼容现有搜索流程的布尔检测。"""
    return _probe_responses_web_search(client, model, provider).ok


ROOT = Path(__file__).resolve().parent.parent
_TAVILY_USAGE_FILE = ROOT / "data" / "tavily_usage.json"
_tavily_lock = threading.Lock()
_session_credits = 0

def _load_tavily_usage() -> dict:
    """加载持久化的 Tavily 月度用量"""
    try:
        if _TAVILY_USAGE_FILE.exists():
            data = json.loads(_TAVILY_USAGE_FILE.read_text())
            month_key = datetime.now().strftime("%Y-%m")
            if data.get("month") == month_key:
                return data
    except Exception:
        pass
    return {"month": datetime.now().strftime("%Y-%m"), "credits": 0, "calls": 0}

def _save_tavily_usage(data: dict):
    _TAVILY_USAGE_FILE.parent.mkdir(parents=True, exist_ok=True)
    _TAVILY_USAGE_FILE.write_text(json.dumps(data, indent=2))

def _track_tavily_call(depth: str = "basic"):
    """追踪 Tavily 调用并持久化到文件"""
    global _session_credits
    credits = 2 if depth == "advanced" else 1
    with _tavily_lock:
        _session_credits += credits
        usage = _load_tavily_usage()
        usage["credits"] = usage.get("credits", 0) + credits
        usage["calls"] = usage.get("calls", 0) + 1
        usage["last_updated"] = datetime.now().isoformat()
        _save_tavily_usage(usage)
    print(f"[Tavily] +{credits} credit ({depth}), 本月累计: {usage['credits']}, 本次会话: {_session_credits}")

def reset_tavily_counter():
    global _session_credits
    _session_credits = 0

def get_tavily_credit_count() -> int:
    return _session_credits

def get_tavily_monthly_usage() -> dict:
    """获取本月 Tavily 用量（供 API 使用）"""
    with _tavily_lock:
        return _load_tavily_usage()


def _get_tavily_client():
    from tavily import TavilyClient
    ctx = get_thread_session()
    key = ctx.get_tavily_api_key() if ctx else os.getenv("TAVILY_API_KEY", "")
    if not key:
        raise ValueError("TAVILY_API_KEY 未配置，请在设置中添加 Tavily API Key")
    return TavilyClient(api_key=key)


def generate_search_plan(product_proposal: str) -> dict:
    """Use LLM to generate search queries from the product proposal."""
    messages = [
        {"role": "system", "content": "你是一个产品调研专家，负责规划搜索策略。"},
        {
            "role": "user",
            "content": DEEP_DIVE_SEARCH_PLAN_PROMPT.format(
                product_proposal=product_proposal,
            ),
        },
    ]
    resp = call_llm(messages)

    try:
        return json.loads(resp)
    except Exception:
        import re
        m = re.search(r'\{[\s\S]*\}', resp)
        if m:
            try:
                return json.loads(m.group())
            except Exception:
                pass
    return {
        "search_queries": ["product market analysis", "competitor analysis"],
        "competitor_names": [],
        "data_points_needed": [],
    }


def search_web(query: str, max_results: int = 5, depth: str = "basic") -> list[dict]:
    """Execute a single web search via Tavily."""
    client = _get_tavily_client()
    response = client.search(
        query=query,
        search_depth=depth,
        max_results=max_results,
        include_answer=True,
    )
    _track_tavily_call(depth)
    results = []
    if response.get("answer"):
        results.append({
            "type": "answer",
            "content": response["answer"],
            "query": query,
        })
    for r in response.get("results", []):
        results.append({
            "type": "result",
            "title": r.get("title", ""),
            "url": r.get("url", ""),
            "content": r.get("content", ""),
            "score": r.get("score", 0),
        })
    return results


INVESTOR_RESEARCH_FOOTER = (
    "\n---\n**使用要求**：将片段或结论中与需求 **同一使用场景/同一类用户任务** 的产品再列为可比；"
    "若多为无关或泛用工具，须写明「高相似度标的在公开信息中较少/噪音大」——**禁止**生造、禁止张冠李戴。"
)

INVESTOR_TAVILY_QUERIES_PROMPT = """You help generate English web search queries for **investor / competitive landscape** research (US & global consumer/pro apps). Output **only** one JSON object, no markdown or code fences.

{body}

Strict rules for `search_queries` (2-3 strings):
- Each query must target the **same user job, same use context** (same niche) as the product. Think: *who* is the user, *in what moment* do they open an app, *what outcome* do they want.
- **Do NOT** use generic phrases that return unrelated verticals, e.g. for "AI to preserve a deceased loved one's voice" you must use terms like memorial, bereavement, legacy, grief, voice **clone**, **not** "AI meeting", "lecture", "transcription app", "zoom recorder" unless the product is actually meeting transcription.
- Prefer specific phrases that find **comparable** apps / services; if the space is very narrow, use narrower queries and accept "few results" — do not broaden into wrong categories to get filler hits.
- `jtbd_line`: one English sentence naming the **exact** niche and user job.

JSON shape exactly:
{{"jtbd_line": "string", "search_queries": ["q1", "q2", "q3"]}}
At most 3 search_queries."""


INVESTOR_WEBSEARCH_USER = """{body}

你现在的任务：在英文互联网上做检索，为「投资人：细分赛道与同类可比竞品」提供**高相关性**材料。请**务必使用 web_search 工具**查最新公开信息后再写结论。

**相关性纪律（与设置中的 WebSearch 要求一致）**：
- 只把与上文中 **同一类用户任务、同一使用场景** 的 App/服务作为可比品；**禁止** 为凑数把会议转写、课堂录音、泛用「AI 录音」、通用聊天机器人等当作垂类强对标，除非其明确服务同一类需求（例如：悼念/声音克隆/遗产与丧失主题 ≠ 会议记录）。
- 若找不到高相似度产品，直接写「高相似度可比标的在公开信息中较少 / 需线下验证」；**禁止** 编造公司名、融资额、下载量。

请用**简体中文**输出，分以下章节（可紧凑）：

### 细分赛道（一两句话说清 JTBD 与目标市场，默认美英等英语区主战场可在此说明）
### 网络检索：可比产品 / 公司与简要依据
### 信息缺口与噪声说明（若检索结果偏题，必须指出）
"""


def _investor_context_body(
    need_title: str = "",
    need_description: str = "",
    posts_compact: str = "",
    user_input: str = "",
) -> str:
    if user_input and not (need_title.strip() or need_description.strip()):
        return f"## 用户输入话题（英语区市场信息检索）\n{user_input[:4000]}\n"
    return (
        f"## 需求标题\n{need_title}\n## 需求描述\n"
        f"{(need_description or '')[:2000]}\n## 用户帖子摘要\n{(posts_compact or '')[:3000]}\n"
    )


def _investor_body_for_tavily_queries(
    need_title: str = "",
    need_description: str = "",
    posts_compact: str = "",
    user_input: str = "",
) -> str:
    if user_input and not (need_title.strip() or need_description.strip()):
        return f"## User topic (overseas / English market search)\n{user_input[:4000]}\n"
    return (
        f"## Product / need title\n{need_title}\n## Description\n"
        f"{(need_description or '')[:2000]}\n## User posts (excerpt)\n{(posts_compact or '')[:3000]}\n"
    )


def _investor_websearch_via_gpt_or_claude(body: str, engine: str) -> str | None:
    """用 Responses API web_search 做投资人赛道/竞品联网检索。engine: gpt | claude"""
    from openai import OpenAI

    if engine == "gpt":
        cfg = get_provider_config("GPT")
    else:
        cfg = get_provider_config("CLAUDE")
    base_url, api_key, model = cfg["base_url"], cfg["api_key"], cfg["model"]
    if not base_url or not api_key:
        return None

    client = OpenAI(base_url=base_url, api_key=api_key)
    user_text = INVESTOR_WEBSEARCH_USER.format(body=body)
    try:
        return _responses_web_search(
            client, model, user_text,
            system="你是投资尽调研究助理。必须使用 web_search 工具联网检索，再用简体中文分节输出。",
        )
    except Exception as e:
        print(f"[Investor] {engine} web_search: {e}")
    return None


def investor_competitor_web_context(
    need_title: str = "",
    need_description: str = "",
    posts_compact: str = "",
    user_input: str = "",
    web_search_engine: str = "tavily",
) -> str:
    """
    为投资人后台分析拉取「赛道/同类竞品」材料，**与设置中的 WebSearch 引擎一致**：
    tavily → TAVILY API 多查；gpt / claude → 对应模型的 web_search 工具。
    失败时返回短说明，提示勿编造、勿乱贴竞品。
    """
    eng = (web_search_engine or "tavily").strip().lower()
    if eng not in ("tavily", "gpt", "claude"):
        eng = "tavily"

    body_ctx = _investor_context_body(need_title, need_description, posts_compact, user_input)

    if eng in ("gpt", "claude"):
        label = "GPT" if eng == "gpt" else "Claude"
        out = _investor_websearch_via_gpt_or_claude(body_ctx, eng)
        if out:
            return (
                f"### 联网检索（{label} — 与设置中 WebSearch 引擎一致）\n\n"
                f"{out}\n"
                f"{INVESTOR_RESEARCH_FOOTER}"
            )
        return (
            f"（{label} WebSearch 未返回有效内容，或 API/中转站不支持 web_search。"
            f"请前往设置 > WebSearch 检查引擎与密钥。赛道与竞品请**仅**依帖子与需求归纳，**禁止**编造。）"
        )

    body_en = _investor_body_for_tavily_queries(need_title, need_description, posts_compact, user_input)
    try:
        raw = call_llm(
            [
                {
                    "role": "system",
                    "content": "You only output a single valid JSON object. No markdown, no code fences, no other text.",
                },
                {
                    "role": "user",
                    "content": INVESTOR_TAVILY_QUERIES_PROMPT.format(body=body_en),
                },
            ],
            max_tokens=500,
        )
    except Exception as e:  # noqa: BLE001
        print(f"[Investor] search-plan generation failed: {e}")
        return (
            "（投资助理检索词生成失败，请检查本地模型配置。"
            f"请仅依帖子与需求做赛道归纳；**禁止**编造具体竞品与融资信息。）"
        )

    data = None
    try:
        m = re.search(r"\{[\s\S]*\}", raw or "")
        if m:
            data = json.loads(m.group())
    except Exception:
        data = None
    if not data or not isinstance(data.get("search_queries"), list):
        return (
            "（未得到有效 JSON 检索词。请勿编造产品名；可写「公开检索未执行」后按帖子与需求定性。）"
        )

    queries = [q.strip() for q in data["search_queries"] if isinstance(q, str) and q.strip()][:3]
    jtbd = (data.get("jtbd_line") or "").strip()
    if not queries:
        return "（检索词为空；请不做具体竞品展开，可说明「待验证」即可。）"

    try:
        _get_tavily_client()
    except Exception:  # noqa: BLE001
        return (
            "（Tavily 未配置。当前设置 WebSearch 为 Tavily 时需配置 TAVILY_API_KEY；"
            "或在设置中切换为 GPT/Claude 的 web_search。赛道归纳请**仅**基于帖子与需求。）"
        )

    parts: list[str] = [
        "### 联网检索（Tavily — 与设置中 WebSearch 引擎一致）\n",
        f"### 细分赛道（英文 JTBD 归纳）\n{jtbd}\n",
        "\n### 海外网页检索片段（高相关性；仅供对照，有噪声时以需求为准）\n",
    ]
    for i, q in enumerate(queries, 1):
        parts.append(f"\n**Query {i}:** {q}\n")
        try:
            results = search_web(q, max_results=4, depth="basic")
        except Exception as ex:  # noqa: BLE001
            print(f"[Investor] Tavily query failed: {ex}")
            parts.append("（Tavily 查询失败，请检查本机网络和 WebSearch 配置。）\n")
            continue
        if not results:
            parts.append("（无结果）\n")
            continue
        for r in results:
            if r.get("type") == "answer":
                c = (r.get("content") or "")[:800]
                parts.append(f"- Summary: {c}\n")
            else:
                title = (r.get("title") or "")[:200]
                url = (r.get("url") or "")[:500]
                c = (r.get("content") or "")[:480]
                parts.append(f"- {title} | {url}\n  {c}\n")

    parts.append(INVESTOR_RESEARCH_FOOTER)
    return "".join(parts)


def run_deep_dive_searches(
    product_proposal: str,
    progress_callback=None,
) -> Generator[tuple[str, list[dict]], None, None]:
    """Run the full search plan, yielding (query, results) pairs with progress updates."""
    if progress_callback:
        progress_callback("正在分析产品方案，规划搜索策略...")

    plan = generate_search_plan(product_proposal)
    queries = plan.get("search_queries", [])
    competitor_names = plan.get("competitor_names", [])

    for name in competitor_names:
        queries.append(f"{name} pricing revenue users review")

    if progress_callback:
        progress_callback(f"搜索计划就绪，共 {len(queries)} 个搜索任务")

    for i, query in enumerate(queries[:8]):
        if progress_callback:
            progress_callback(f"搜索 ({i+1}/{min(len(queries), 8)})：{query}")
        try:
            results = search_web(query, max_results=4, depth="basic")
            yield query, results
        except Exception as e:
            print(f"[WebSearch] query failed: {e}")
            if progress_callback:
                progress_callback(f"搜索失败：{query}，请检查本地 WebSearch 配置")
            yield query, []


def format_search_results_for_llm(all_results: list[tuple[str, list[dict]]]) -> str:
    """Format all search results into a string for LLM consumption."""
    parts = []
    for query, results in all_results:
        parts.append(f"\n### 搜索：{query}")
        if not results:
            parts.append("（无结果）")
            continue
        for r in results:
            if r["type"] == "answer":
                parts.append(f"**AI 摘要**: {r['content']}")
            else:
                parts.append(f"- [{r['title']}]({r['url']})")
                parts.append(f"  {r['content'][:300]}")
    return "\n".join(parts)


_REDDIT_URL_PATTERN = re.compile(r'reddit\.com/r/\w+/comments/([a-z0-9]+)')


def discover_reddit_urls(
    topic: str,
    search_queries: list[str],
    subreddits: list[str] | None = None,
    discovery_queries: list[str] | None = None,
    progress_callback=None,
) -> list[dict]:
    """用 Tavily WebSearch 语义搜索发现高相关 Reddit 帖子。

    Skill 核心机制：不用 Reddit 自身的 OR 关键词搜索，而是通过 Web 搜索引擎
    的语义匹配来发现最相关的 Reddit 讨论帖。

    Returns: [{"post_id": "abc123", "url": "...", "title": "...", "snippet": "..."}]
    """
    try:
        client = _get_tavily_client()
    except ValueError:
        if progress_callback:
            progress_callback("Tavily API Key 未配置，跳过 WebSearch 发现")
        return []

    tavily_queries = _build_discovery_queries(topic, search_queries, subreddits, discovery_queries)

    if progress_callback:
        progress_callback(f"WebSearch 发现模式：{len(tavily_queries)} 条语义搜索")

    seen_post_ids: set[str] = set()
    discovered: list[dict] = []

    for i, query in enumerate(tavily_queries):
        if progress_callback and i % 4 == 0:
            progress_callback(f"WebSearch ({i+1}/{len(tavily_queries)})：{query[:50]}")
        try:
            response = client.search(
                query=query,
                search_depth="basic",
                max_results=5,
                include_answer=False,
            )
            _track_tavily_call("basic")
            for r in response.get("results", []):
                url = r.get("url", "")
                m = _REDDIT_URL_PATTERN.search(url)
                if m:
                    post_id = m.group(1)
                    if post_id not in seen_post_ids:
                        seen_post_ids.add(post_id)
                        discovered.append({
                            "post_id": post_id,
                            "url": url,
                            "title": r.get("title", ""),
                            "snippet": r.get("content", "")[:300],
                            "score": r.get("score", 0),
                        })
        except Exception as e:
            print(f"[WebSearch] query failed: {query[:50]} — {e}")

    discovered.sort(key=lambda x: x.get("score", 0), reverse=True)

    if progress_callback:
        progress_callback(f"WebSearch 发现 {len(discovered)} 个独立 Reddit 帖子")

    return discovered


def _build_discovery_queries(
    topic: str,
    search_queries: list[str],
    subreddits: list[str] | None = None,
    discovery_queries: list[str] | None = None,
) -> list[str]:
    """构建 Tavily 搜索查询，复刻 Skill Phase 1 的多角度搜索矩阵（20-40 条）。"""
    queries = []

    # 最高优先级：LLM 生成的语义化长查询（ROI 最高，每条都精准覆盖一个角度）
    if discovery_queries:
        for dq in discovery_queries[:10]:
            queries.append(f"reddit {dq}")

    # Problem-focused（痛点角度，命中率高）
    pain_suffixes = ["frustrated", "problem", "wish", "struggle", "workaround", "alternative"]
    for suffix in pain_suffixes:
        queries.append(f"reddit {topic} {suffix}")

    # Solution-seeking
    queries.append(f"reddit best {topic} app recommendation")

    # 搜索词场景查询（LLM 规划的关键词组合）
    for q in search_queries[:5]:
        queries.append(f"reddit {q}")

    # 社区定向（精准投放到核心社区）
    if subreddits:
        for sub in subreddits[:4]:
            queries.append(f"reddit r/{sub} {topic}")

    return queries[:25]


_REDDIT_SUB_FROM_URL = re.compile(r'reddit\.com/r/(\w+)')


def _parse_reddit_urls_from_text(content: str, seen_post_ids: set[str], discovered: list[dict], discovered_subs: set[str], source_tag: str = "gpt_websearch"):
    """从文本中解析 Reddit 帖子 URL 和 subreddit。"""
    if not content:
        return
    content = content.strip()
    if content.startswith("```"):
        content = re.sub(r'^```\w*\n?', '', content)
        content = re.sub(r'\n?```$', '', content)
    try:
        items = json.loads(content)
        if isinstance(items, list):
            for item in items:
                url = item.get("url", "")
                m_post = _REDDIT_URL_PATTERN.search(url)
                if m_post:
                    post_id = m_post.group(1)
                    if post_id not in seen_post_ids:
                        seen_post_ids.add(post_id)
                        discovered.append({
                            "post_id": post_id,
                            "url": url,
                            "title": item.get("title", ""),
                            "snippet": item.get("snippet", "")[:300],
                            "_discovery_source": source_tag,
                        })
                m_sub = _REDDIT_SUB_FROM_URL.search(url)
                if m_sub:
                    discovered_subs.add(m_sub.group(1))
            return
    except (json.JSONDecodeError, TypeError):
        pass
    for url_match in _REDDIT_URL_PATTERN.finditer(content):
        post_id = url_match.group(1)
        if post_id not in seen_post_ids:
            seen_post_ids.add(post_id)
            full_url_match = re.search(r'https?://[^\s"]+reddit\.com/r/\w+/comments/[^\s"]+', content)
            discovered.append({
                "post_id": post_id,
                "url": full_url_match.group(0) if full_url_match else f"https://reddit.com/comments/{post_id}",
                "title": "", "snippet": "",
                "_discovery_source": source_tag,
            })
    for sub_match in _REDDIT_SUB_FROM_URL.finditer(content):
        discovered_subs.add(sub_match.group(1))


def gpt_discover_reddit_urls(
    topic: str,
    search_queries: list[str],
    subreddits: list[str] | None = None,
    discovery_queries: list[str] | None = None,
    progress_callback=None,
) -> tuple[list[dict], set[str]]:
    """用 Responses API web_search 发现高相关 Reddit 帖子 URL。"""
    from openai import OpenAI

    cfg = get_provider_config("GPT")
    base_url, api_key, model = cfg["base_url"], cfg["api_key"], cfg["model"]

    if not base_url or not api_key:
        if progress_callback:
            progress_callback("⚠️ GPT 未配置，请在设置中配置 GPT")
        return [], set()

    client = OpenAI(base_url=base_url, api_key=api_key)

    if not _test_responses_web_search(client, model, "GPT"):
        if progress_callback:
            progress_callback("⚠️ GPT 中转站不支持 web_search，请切换引擎")
        return [], set()

    all_queries = _build_gpt_discovery_queries(topic, search_queries, subreddits, discovery_queries)

    if progress_callback:
        progress_callback(f"GPT WebSearch 发现模式：{len(all_queries)} 条搜索")

    seen_post_ids: set[str] = set()
    discovered: list[dict] = []
    discovered_subs: set[str] = set()

    batch_size = 5
    for batch_start in range(0, len(all_queries), batch_size):
        batch = all_queries[batch_start:batch_start + batch_size]
        combined_query = "\n".join(f"- {q}" for q in batch)

        if progress_callback:
            progress_callback(f"GPT WebSearch ({batch_start+1}-{min(batch_start+batch_size, len(all_queries))}/{len(all_queries)})...")

        try:
            content = _responses_web_search(
                client, model,
                f"""Search Reddit for threads related to these queries:
{combined_query}

Find real Reddit discussion threads (posts with comments). For each result, output:
[{{"url": "full reddit thread URL", "title": "thread title", "snippet": "brief content preview"}}]

Requirements:
- Only include reddit.com URLs that point to actual post threads (containing /comments/)
- Find 3-8 results total across all queries
- Prioritize high-engagement threads (many comments, upvotes)
- Output JSON array only""",
                system="You are a Reddit research assistant. Search for relevant Reddit threads and return their URLs. Output ONLY a JSON array, no markdown or extra text.",
            )
            _parse_reddit_urls_from_text(content or "", seen_post_ids, discovered, discovered_subs, "gpt_websearch")
        except Exception as e:
            err_str = str(e)
            print(f"[GPT WebSearch] batch failed: {e}")
            if progress_callback:
                if any(kw in err_str.lower() for kw in ("web_search", "tool", "not support")):
                    progress_callback("⚠️ GPT 中转站不支持 web_search，请切换到 GPT 兼容的联网配置或 Tavily")
                    break
                else:
                    progress_callback("⚠️ GPT WebSearch 批次失败，请检查本地模型配置和网络")

    if progress_callback:
        progress_callback(f"GPT WebSearch 发现 {len(discovered)} 个 Reddit 帖子, {len(discovered_subs)} 个 subreddit")

    return discovered, discovered_subs


def _build_gpt_discovery_queries(
    topic: str,
    search_queries: list[str],
    subreddits: list[str] | None = None,
    discovery_queries: list[str] | None = None,
) -> list[str]:
    """构建给 GPT WebSearch 的查询列表，痛点优先，控制总量避免过慢。"""
    queries = []

    if discovery_queries:
        for dq in discovery_queries[:4]:
            queries.append(f"reddit {dq}")

    pain_suffixes = ["frustrated problem", "wish alternative", "struggle workaround"]
    for suffix in pain_suffixes:
        queries.append(f"reddit {topic} {suffix}")

    queries.append(f"reddit best {topic} app recommendation 2026")

    for q in search_queries[:4]:
        queries.append(f"reddit {q}")

    if subreddits:
        for sub in subreddits[:2]:
            queries.append(f"reddit r/{sub} {topic}")

    return queries[:15]


def claude_discover_reddit_urls(
    topic: str,
    search_queries: list[str],
    subreddits: list[str] | None = None,
    discovery_queries: list[str] | None = None,
    progress_callback=None,
) -> tuple[list[dict], set[str]]:
    """用 Responses API web_search 发现高相关 Reddit 帖子 URL（Claude 引擎）。"""
    from openai import OpenAI

    cfg = get_provider_config("CLAUDE")
    base_url, api_key, model = cfg["base_url"], cfg["api_key"], cfg["model"]

    if not base_url or not api_key:
        if progress_callback:
            progress_callback("⚠️ Claude 未配置，请在设置中检查模型配置")
        return [], set()

    client = OpenAI(base_url=base_url, api_key=api_key)

    if not _test_responses_web_search(client, model, "Claude"):
        if progress_callback:
            progress_callback("⚠️ Claude 中转站不支持 web_search，请切换到 GPT 或 Tavily")
        return [], set()

    all_queries = _build_gpt_discovery_queries(topic, search_queries, subreddits, discovery_queries)

    if progress_callback:
        progress_callback(f"Claude WebSearch 发现模式：{len(all_queries)} 条搜索")

    seen_post_ids: set[str] = set()
    discovered: list[dict] = []
    discovered_subs: set[str] = set()

    batch_size = 5
    for batch_start in range(0, len(all_queries), batch_size):
        batch = all_queries[batch_start:batch_start + batch_size]
        combined_query = "\n".join(f"- {q}" for q in batch)

        if progress_callback:
            progress_callback(f"Claude WebSearch ({batch_start+1}-{min(batch_start+batch_size, len(all_queries))}/{len(all_queries)})...")

        try:
            content = _responses_web_search(
                client, model,
                f"""Search Reddit for threads related to these queries:
{combined_query}

Find real Reddit discussion threads (posts with comments). For each result, output:
[{{"url": "full reddit thread URL", "title": "thread title", "snippet": "brief content preview"}}]

Requirements:
- Only include reddit.com URLs that point to actual post threads (containing /comments/)
- Find 3-8 results total across all queries
- Prioritize high-engagement threads (many comments, upvotes)
- Output JSON array only""",
                system="You are a Reddit research assistant. Search for relevant Reddit threads and return their URLs. Output ONLY a JSON array, no markdown or extra text.",
            )
            _parse_reddit_urls_from_text(content or "", seen_post_ids, discovered, discovered_subs, "claude_websearch")
        except Exception as e:
            print(f"[Claude WebSearch] batch failed: {e}")
            if progress_callback and batch_start == 0:
                progress_callback("⚠️ Claude WebSearch 失败，请检查本地模型配置和网络")
                return [], set()

    if progress_callback:
        progress_callback(f"Claude WebSearch 发现 {len(discovered)} 个 Reddit 帖子, {len(discovered_subs)} 个 subreddit")

    return discovered, discovered_subs


def discover_hn_urls(
    topic: str,
    search_queries: list[str],
    progress_callback=None,
) -> list[dict]:
    """用 Tavily WebSearch 发现高相关 HackerNews 帖子。"""
    try:
        client = _get_tavily_client()
    except ValueError:
        return []

    hn_queries = [
        f"hacker news {topic} discussion",
        f"site:news.ycombinator.com {topic}",
    ]
    for q in search_queries[:3]:
        hn_queries.append(f"hacker news {q}")

    hn_pattern = re.compile(r'news\.ycombinator\.com/item\?id=(\d+)')
    seen_ids: set[str] = set()
    discovered: list[dict] = []

    for query in hn_queries[:5]:
        try:
            response = client.search(
                query=query,
                search_depth="basic",
                max_results=5,
                include_answer=False,
            )
            _track_tavily_call("basic")
            for r in response.get("results", []):
                url = r.get("url", "")
                m = hn_pattern.search(url)
                if m:
                    hn_id = m.group(1)
                    if hn_id not in seen_ids:
                        seen_ids.add(hn_id)
                        discovered.append({
                            "hn_id": hn_id,
                            "url": url,
                            "title": r.get("title", ""),
                            "snippet": r.get("content", "")[:300],
                        })
        except Exception:
            pass

    if progress_callback:
        progress_callback(f"WebSearch 发现 {len(discovered)} 个 HN 讨论")

    return discovered


_COMPETITOR_SEARCH_PROMPT = """Search for the top 5-8 apps/tools for "{need_title}".
Topic: {need_description}

Do 2 searches max:
1. "best {need_title} apps tools 2025 2026 pricing reviews"
2. "top {need_title} alternatives comparison app store"

Output JSON array (no code blocks):
[{{"name": "App Name", "type": "app/web_tool/chrome_extension", "description": "one line", "app_store_url": "", "play_store_url": "", "url": "official website", "pricing": "$X.XX/mo or free + $X.XX/yr", "rating": "4.5", "estimated_downloads": "10M+ / unknown", "b2b_b2c": "B2C/Both", "ai_driven": "yes/partial/no", "strengths": "2-3 points", "weaknesses": "2-3 points from real reviews", "notable_reviews": "1-2 review snippets if found"}}]

Only real software products. Pricing must be specific. Sort by popularity."""


def _gpt_web_search_competitors(need_title: str, need_description: str, progress_callback=None) -> str | None:
    """用 Responses API web_search 搜索真实竞品。"""
    from openai import OpenAI

    cfg = get_provider_config("GPT")
    base_url, api_key, model = cfg["base_url"], cfg["api_key"], cfg["model"]
    if not base_url or not api_key:
        return None

    try:
        client = OpenAI(base_url=base_url, api_key=api_key, timeout=90.0)
        if progress_callback:
            progress_callback("GPT 正在联网搜索竞品...")

        return _responses_web_search(
            client, model,
            _COMPETITOR_SEARCH_PROMPT.format(need_title=need_title, need_description=need_description[:500]),
            system="You are a competitive analysis expert. Search the web and output a JSON array of competitor apps. No code blocks or extra text.",
        )
    except Exception as e:
        print(f"[GPT web_search] failed: {e}")
        if progress_callback:
            progress_callback("⚠️ GPT 联网搜索失败，请检查本地模型配置和网络")
    return None


def _test_web_search_support(client, model: str, provider: str = "Claude") -> bool:
    """兼容旧调用签名，内部转发到 Responses API 检测。"""
    return _test_responses_web_search(client, model, provider)


def _probe_web_search_support(
    client,
    model: str,
    provider: str = "Claude",
    *,
    attempts: int = 1,
) -> WebSearchProbeResult:
    """返回包含失败原因的检测结果，供预检和设置页使用。"""
    return _probe_responses_web_search(client, model, provider, attempts=attempts)


_claude_ws_cache: dict[str, bool] = {}

def _claude_web_search_competitors(need_title: str, need_description: str, progress_callback=None) -> str | None:
    """用 Responses API web_search 联网搜索竞品（Claude 引擎）。"""
    from openai import OpenAI

    cfg = get_provider_config("CLAUDE")
    base_url, api_key, model = cfg["base_url"], cfg["api_key"], cfg["model"]
    if not base_url or not api_key:
        return None

    try:
        client = OpenAI(base_url=base_url, api_key=api_key, timeout=90.0)

        cache_key = f"{base_url}|{model}"
        if cache_key not in _claude_ws_cache:
            _claude_ws_cache[cache_key] = _test_responses_web_search(client, model, "Claude")
        if not _claude_ws_cache[cache_key]:
            print("[Claude] web_search not supported by this relay")
            if progress_callback:
                progress_callback("⚠️ Claude 中转站不支持 web_search，请切换到 GPT 或 Tavily")
            return None

        if progress_callback:
            progress_callback("Claude 正在联网搜索竞品...")

        return _responses_web_search(
            client, model,
            _COMPETITOR_SEARCH_PROMPT.format(need_title=need_title, need_description=need_description[:500]),
            system="You are a competitive analysis expert. Search the web and output a JSON array of competitor apps. No code blocks or extra text.",
        )
    except Exception as e:
        print(f"[Claude web_search competitors] failed: {e}")
        if progress_callback:
            progress_callback("⚠️ Claude 联网搜索失败，请检查本地模型配置和网络")
    return None


def _claude_analyze_competitors(need_title: str, need_description: str, posts_hint: str = "", progress_callback=None) -> str | None:
    """用 Claude web_search 联网搜索竞品。不支持则返回 None。"""
    return _claude_web_search_competitors(need_title, need_description, progress_callback)


def _tavily_search_competitors(need_title: str, need_description: str, posts_hint: str = "", progress_callback=None) -> str | None:
    """用 Tavily API 搜索竞品信息，再用 Claude 整理为结构化 JSON。"""
    try:
        client = _get_tavily_client()
    except ValueError:
        return None

    queries = [
        f"best {need_title} apps tools 2025 2026 pricing reviews",
        f"top {need_title} alternatives comparison app store",
        f"{need_title} app pricing downloads ratings",
    ]

    import concurrent.futures as _tv_cf
    all_results = []
    def _run_tavily_q(q):
        try:
            response = client.search(query=q, search_depth="basic", max_results=5, include_answer=True)
            _track_tavily_call("basic")
            items = []
            if response.get("answer"):
                items.append(f"[Answer] {response['answer']}")
            for r in response.get("results", []):
                items.append(f"- {r.get('title', '')}: {r.get('content', '')[:300]} ({r.get('url', '')})")
            return items
        except Exception as e:
            print(f"[Tavily competitor] query failed: {q} — {e}")
            return []

    with _tv_cf.ThreadPoolExecutor(max_workers=3) as ex:
        for items in ex.map(_run_tavily_q, queries):
            all_results.extend(items)

    if not all_results:
        return None

    search_context = "\n".join(all_results[:30])
    prompt = f"""基于以下搜索结果，提取该领域的竞品信息。

## 需求主题
{need_title}

## 搜索结果
{search_context}

输出 JSON 数组（不加代码块标记），包含 5-8 个最相关的竞品：
[
  {{
    "name": "产品名称",
    "type": "app / chrome_extension / web_tool",
    "description": "一句话描述核心功能",
    "url": "官网链接",
    "app_store_url": "iOS App Store 链接（不确定则留空）",
    "play_store_url": "Google Play 链接（不确定则留空）",
    "pricing": "具体定价（如 free / $4.99/mo / $29.99/yr，不要只写 freemium）",
    "rating": "App Store 评分（如 4.5）",
    "estimated_downloads": "下载量估算（如 10M+ / 500K+，搜索结果中没有就写 unknown）",
    "b2b_b2c": "B2B / B2C / Both",
    "ai_driven": "是 / 部分 / 否",
    "strengths": "核心优势（2-3 条）",
    "weaknesses": "核心劣势（从搜索结果中的用户评价提取，2-3 条）",
    "notable_reviews": "搜索结果中提到的用户评价（1-2 条原文，没有就留空）"
  }}
]

严格要求：
- 只输出真实软件产品，排除实物和线下服务
- pricing 必须具体，不要只写"freemium"
- 从搜索结果中尽可能提取下载量、评分等数据"""
    messages = [
        {"role": "system", "content": "你是竞品分析专家，只输出 JSON 数组。"},
        {"role": "user", "content": prompt},
    ]
    try:
        return call_llm(messages)
    except Exception as e:
        print(f"[Tavily competitor → LLM] parse failed: {e}")
        return None


def search_competitors(
    need_title: str,
    need_description: str,
    posts_hint: str = "",
    progress_callback=None,
    web_search_engine: str = "claude",
) -> str:
    """搜索竞品：根据 web_search_engine 设置选择搜索方式（gpt/claude/tavily）。"""
    import re

    engine_label = {"gpt": "GPT", "claude": "Claude", "tavily": "Tavily"}.get(web_search_engine, web_search_engine)
    if progress_callback:
        progress_callback(f"{engine_label} 正在搜索竞品...")

    resp = None

    if web_search_engine == "gpt":
        gpt_result = _gpt_web_search_competitors(need_title, need_description, progress_callback)
        if gpt_result:
            resp = gpt_result
            if progress_callback:
                progress_callback("GPT 联网竞品搜索完成")
        else:
            if progress_callback:
                progress_callback("⚠️ GPT 竞品搜索失败，请检查 GPT 配置或在设置中切换 WebSearch 引擎")
            return "（GPT 竞品搜索失败，请检查配置）"

    elif web_search_engine == "tavily":
        tavily_result = _tavily_search_competitors(need_title, need_description, posts_hint, progress_callback)
        if tavily_result:
            resp = tavily_result
            if progress_callback:
                progress_callback("Tavily 竞品搜索完成")
        else:
            if progress_callback:
                progress_callback("⚠️ Tavily 竞品搜索失败，请检查 Tavily API Key 或在设置中切换 WebSearch 引擎")
            return "（Tavily 竞品搜索失败，请检查配置）"

    else:
        resp = _claude_analyze_competitors(need_title, need_description, posts_hint, progress_callback)
        if not resp:
            if progress_callback:
                progress_callback("⚠️ Claude 联网搜索失败，请确认中转站支持 web_search 工具或在设置中切换 WebSearch 引擎")
            return "（Claude 联网搜索失败，中转站可能不支持 web_search 工具，请切换引擎）"

    # 解析 JSON
    competitors_json = "[]"
    try:
        parsed = json.loads(resp)
        competitors_json = json.dumps(parsed, ensure_ascii=False, indent=2)
    except Exception:
        m = re.search(r'\[[\s\S]*\]', resp)
        if m:
            try:
                parsed = json.loads(m.group())
                competitors_json = json.dumps(parsed, ensure_ascii=False, indent=2)
            except Exception:
                competitors_json = resp

    link_summary = ""
    try:
        comp_list = json.loads(competitors_json)
        if isinstance(comp_list, list):
            lines = []
            for c in comp_list:
                name = c.get("name", "?")
                link = c.get("app_store_url") or c.get("play_store_url") or c.get("url") or ""
                if link:
                    link_type = "App Store" if "apps.apple.com" in link else "Play Store" if "play.google.com" in link else "官网"
                    lines.append(f"- {name}: [{link_type}]({link})")
                else:
                    lines.append(f"- {name}: （无链接）")
            link_summary = "\n\n### 竞品链接速查（报告中链接列请直接使用以下链接）\n" + "\n".join(lines)
    except Exception:
        pass

    result_parts = [
        "### 竞品 App/工具分析",
        competitors_json,
        link_summary,
    ]

    if progress_callback:
        progress_callback("竞品分析完成")

    return "\n".join(result_parts)
