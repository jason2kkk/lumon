"""
debate.py — 话题制讨论引擎

架构：导演拆话题 → 逐话题 PM/杠精交锋（有界上下文） → 导演最终判决

有界上下文原则：
  - PM：话题问题 + 杠精最新发言 + 前序话题一句话结论（~500字）
  - 杠精：话题问题 + PM 刚说的话 + 前序话题一句话结论（~400字）
  - 导演小结：当前话题完整交锋 + 全部前序结论（~600字）
  - 导演判决：全部话题一句话结论列表（~300字）
"""

import json
import re
from pathlib import Path
from .llm_client import call_for_role, estimate_tokens

from prompts import (
    DIRECTOR_SYSTEM_PROMPT,
    DIRECTOR_TOPICS_PROMPT,
    DIRECTOR_FREE_TOPICS_PROMPT,
    DIRECTOR_WRAP_PROMPT,
    DIRECTOR_VERDICT_PROMPT,
    PM_SYSTEM_PROMPT,
    PM_FIRST_TOPIC_PROMPT,
    PM_FREE_FIRST_TOPIC_PROMPT,
    PM_TOPIC_PROMPT,
    PM_COUNTER_PROMPT,
    CRITIC_SYSTEM_PROMPT,
    CRITIC_TOPIC_PROMPT,
    CRITIC_FREE_TOPIC_PROMPT,
    CRITIC_FOLLOWUP_PROMPT,
    CRITIC_FREE_FOLLOWUP_PROMPT,
    INVESTOR_SYSTEM_PROMPT,
    INVESTOR_BG_ANALYSIS_PROMPT,
    INVESTOR_FREE_BG_ANALYSIS_PROMPT,
    INVESTOR_FINAL_PROMPT,
    INVESTOR_FREE_FINAL_PROMPT,
    HUMAN_INJECT_PM,
    HUMAN_INJECT_CRITIC,
    PRODUCT_PROPOSAL_PROMPT,
    FINAL_REPORT_PROMPT,
    DEEP_DIVE_SYSTEM_PROMPT,
    DEEP_DIVE_ANALYSIS_PROMPT,
)

ROOT = Path(__file__).resolve().parent.parent
_ROLE_NAMES_FILE = ROOT / "data" / "cache" / "role_names.json"
_DEFAULT_ROLE_NAMES = {"director": "导演", "analyst": "产品经理", "critic": "杠精"}
DEBATE_LANGUAGE_ZH = "zh-CN"
DEBATE_LANGUAGE_EN = "en-US"


def _normalize_debate_language(value: str | None) -> str:
    return DEBATE_LANGUAGE_EN if str(value or "").strip().lower() in {"en", "en-us", "english"} else DEBATE_LANGUAGE_ZH


def _is_debate_en(language: str | None) -> bool:
    return _normalize_debate_language(language) == DEBATE_LANGUAGE_EN


def _need_title(need: dict, language: str | None = None) -> str:
    if _is_debate_en(language):
        title = str(need.get("need_title_en") or "").strip()
        if title:
            return title
    return str(need.get("need_title") or "").strip()


def _need_description(need: dict, language: str | None = None) -> str:
    if _is_debate_en(language):
        desc = str(need.get("need_description_en") or "").strip()
        if desc:
            return desc
    return str(need.get("need_description") or "").strip()


def _language_instruction(language: str | None) -> str:
    if not _is_debate_en(language):
        return ""
    return (
        "\n\n=== Output language override ===\n"
        "All visible discussion output must be in natural English. Keep original Reddit/user quotes in their original language. "
        "If a prompt asks for JSON, keep the JSON keys exactly as requested but write all human-facing values in English. "
        "If a prompt asks for [STRUCTURAL] or [MINOR], keep that tag exactly, then continue in English. "
        "Do not mention that you are translating or following a language override."
    )


def _apply_language(messages: list[dict], language: str | None) -> list[dict]:
    instruction = _language_instruction(language)
    if not instruction:
        return messages
    out: list[dict] = []
    applied = False
    for msg in messages:
        copied = dict(msg)
        if copied.get("role") == "system" and not applied:
            copied["content"] = str(copied.get("content", "")) + instruction
            applied = True
        out.append(copied)
    if not applied:
        out.insert(0, {"role": "system", "content": instruction.strip()})
    return out


def _get_role_names() -> dict[str, str]:
    names = dict(_DEFAULT_ROLE_NAMES)
    if _ROLE_NAMES_FILE.exists():
        try:
            saved = json.loads(_ROLE_NAMES_FILE.read_text(encoding="utf-8"))
            if isinstance(saved, dict):
                names.update(saved)
        except Exception:
            pass
    return names


# ============================================================
# 帖子格式化
# ============================================================

def _format_comments(comments: list[str]) -> str:
    if not comments:
        return "（无评论）"
    lines = []
    for i, c in enumerate(comments, 1):
        lines.append(f"{i}. {c}")
    return "\n".join(lines)


def _format_need_posts(need: dict) -> str:
    parts = []
    for i, post in enumerate(need["posts"], 1):
        comments_text = _format_comments(post.get("comments", []))
        parts.append(
            f"### 帖子 {i}: {post['title']}\n"
            f"得分: {post.get('score', 0)} | 评论数: {post.get('num_comments', 0)}\n"
            f"正文: {post.get('content') or '（无正文）'}\n"
            f"精选评论:\n{comments_text}\n"
            f"链接: {post.get('hn_url', post.get('url', ''))}"
        )
    return "\n\n".join(parts)


def _format_need_posts_summary(need: dict) -> str:
    lines = []
    for i, post in enumerate(need["posts"], 1):
        lines.append(f"{i}. {post['title']} (▲{post.get('score', 0)}, 💬{post.get('num_comments', 0)})")
    return "\n".join(lines)


def _format_need_posts_compact(need: dict, max_posts: int = 8) -> str:
    """精简版帖子格式：标题 + 正文前 150 字 + 前 2 条评论前 100 字。"""
    parts = []
    for i, post in enumerate(need["posts"][:max_posts], 1):
        title = post.get("title", "")
        score = post.get("score", 0)
        num_comments = post.get("num_comments", 0)
        content = (post.get("content") or "")[:150]
        if len(post.get("content") or "") > 150:
            content += "..."

        comments = post.get("comments", [])[:2]
        comment_lines = []
        for c in comments:
            short = c[:100] + ("..." if len(c) > 100 else "")
            comment_lines.append(f"  - {short}")

        block = f"{i}. {title} (▲{score}, 💬{num_comments})"
        if content:
            block += f"\n{content}"
        if comment_lines:
            block += "\n" + "\n".join(comment_lines)
        parts.append(block)
    return "\n\n".join(parts)


# ============================================================
# 前序结论格式化
# ============================================================

def format_prior_conclusions(conclusions: list[dict], language: str | None = None) -> str:
    """把前序话题结论格式化为简短文本。"""
    if not conclusions:
        return ""
    lines = []
    for c in conclusions:
        sep = ": " if _is_debate_en(language) else "："
        lines.append(f"- {c['title']}{sep}{c['summary']}")
    heading = "## Prior Topic Conclusions" if _is_debate_en(language) else "## 前序话题结论"
    return heading + "\n" + "\n".join(lines)


def _build_prior_context(conclusions: list[dict], language: str | None = None) -> str:
    """构建 prior_context 字段，空则返回空字符串。"""
    text = format_prior_conclusions(conclusions, language)
    return text if text else ""


# ============================================================
# 话题交锋记录格式化
# ============================================================

def format_topic_exchanges(exchanges: list[dict], language: str | None = None) -> str:
    """把单个话题内的交锋格式化为文本。"""
    rn = _get_role_names()
    if _is_debate_en(language):
        label_map = {"director": "Director", "analyst": "Product Manager", "critic": "Skeptical User", "investor": "Investor", "human": "Project Owner"}
    else:
        label_map = {**rn, "human": "负责人"}
    parts = []
    for e in exchanges:
        label = label_map.get(e["role"], e["role"])
        content = e["content"]
        content = re.sub(r'<think>[\s\S]*?</think>', '', content).strip()
        parts.append(f"【{label}】{content}")
    return "\n\n".join(parts)


# ============================================================
# 导演 — 话题拆分
# ============================================================

def prepare_topic_analysis(need: dict, language: str | None = None) -> list[dict]:
    """导演分析帖子，拆出 3-5 个争议话题。返回 messages。"""
    posts_compact = _format_need_posts_compact(need)
    return _apply_language([
        {"role": "system", "content": DIRECTOR_SYSTEM_PROMPT},
        {
            "role": "user",
            "content": DIRECTOR_TOPICS_PROMPT.format(
                need_title=_need_title(need, language),
                need_description=_need_description(need, language),
                post_count=len(need["posts"]),
                posts_compact=posts_compact,
            ),
        },
    ], language)


def prepare_free_topic_analysis(user_input: str, language: str | None = None) -> list[dict]:
    """自由话题模式：导演根据用户输入拆话题。"""
    return _apply_language([
        {"role": "system", "content": DIRECTOR_SYSTEM_PROMPT},
        {
            "role": "user",
            "content": DIRECTOR_FREE_TOPICS_PROMPT.format(user_input=user_input),
        },
    ], language)


# ============================================================
# 产品经理 — 话题级发言
# ============================================================

def prepare_topic_pm(
    need: dict,
    topic: dict,
    critic_latest: str,
    prior_conclusions: list[dict],
    is_first: bool = False,
    language: str | None = None,
) -> list[dict]:
    """PM 对单个话题发言。is_first=True 时使用带 FEMWC 分析的首话题 prompt。"""
    prior_context = _build_prior_context(prior_conclusions, language)

    if is_first:
        posts_compact = _format_need_posts_compact(need)
        return _apply_language([
            {"role": "system", "content": PM_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": PM_FIRST_TOPIC_PROMPT.format(
                    need_title=_need_title(need, language),
                    topic_question=topic["question"],
                    need_description=_need_description(need, language),
                    post_count=len(need["posts"]),
                    posts_compact=posts_compact,
                ),
            },
        ], language)

    return _apply_language([
        {"role": "system", "content": PM_SYSTEM_PROMPT},
        {
            "role": "user",
            "content": PM_TOPIC_PROMPT.format(
                need_title=_need_title(need, language),
                topic_question=topic["question"],
                prior_context=prior_context,
            ),
        },
    ], language)


def prepare_topic_pm_counter(
    need: dict,
    topic: dict,
    pm_latest: str,
    critic_latest: str,
    prior_conclusions: list[dict],
    language: str | None = None,
) -> list[dict]:
    """PM 对杠精的结构性质疑进行反击。"""
    prior_context = _build_prior_context(prior_conclusions, language)
    return _apply_language([
        {"role": "system", "content": PM_SYSTEM_PROMPT},
        {
            "role": "user",
            "content": PM_COUNTER_PROMPT.format(
                topic_title=topic["title"],
                critic_latest=critic_latest,
                prior_context=prior_context,
            ),
        },
    ], language)


# ============================================================
# 杠精 — 话题级回应
# ============================================================

def prepare_topic_critic(
    need: dict,
    topic: dict,
    pm_latest: str,
    prior_conclusions: list[dict],
    language: str | None = None,
) -> list[dict]:
    """杠精对 PM 的话进行回应（含反馈分级）。"""
    prior_context = _build_prior_context(prior_conclusions, language)
    return _apply_language([
        {"role": "system", "content": CRITIC_SYSTEM_PROMPT},
        {
            "role": "user",
            "content": CRITIC_TOPIC_PROMPT.format(
                topic_title=topic["title"],
                topic_question=topic["question"],
                pm_latest=pm_latest,
                prior_context=prior_context,
            ),
        },
    ], language)


def prepare_free_topic_pm(
    user_input: str,
    topic: dict,
    prior_conclusions: list[dict],
    is_first: bool = False,
    language: str | None = None,
) -> list[dict]:
    """自由话题模式：PM 对话题发言（无帖子数据）。"""
    prior_context = _build_prior_context(prior_conclusions, language)

    if is_first:
        return _apply_language([
            {"role": "system", "content": PM_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": PM_FREE_FIRST_TOPIC_PROMPT.format(
                    topic_title=topic["title"],
                    topic_question=topic["question"],
                    user_input=user_input,
                ),
            },
        ], language)

    return _apply_language([
        {"role": "system", "content": PM_SYSTEM_PROMPT},
        {
            "role": "user",
            "content": PM_TOPIC_PROMPT.format(
                need_title=user_input,
                topic_question=topic["question"],
                prior_context=prior_context,
            ),
        },
    ], language)


def prepare_free_topic_critic(
    user_input: str,
    topic: dict,
    pm_latest: str,
    prior_conclusions: list[dict],
    language: str | None = None,
) -> list[dict]:
    """自由话题模式：杠精对 PM 回应（无帖子数据）。"""
    prior_context = _build_prior_context(prior_conclusions, language)
    return _apply_language([
        {"role": "system", "content": CRITIC_SYSTEM_PROMPT},
        {
            "role": "user",
            "content": CRITIC_FREE_TOPIC_PROMPT.format(
                topic_title=topic["title"],
                topic_question=topic["question"],
                pm_latest=pm_latest,
                prior_context=prior_context,
            ),
        },
    ], language)


def prepare_topic_critic_followup(
    need: dict,
    topic: dict,
    critic_prev: str,
    pm_counter: str,
    prior_conclusions: list[dict],
    language: str | None = None,
) -> list[dict]:
    """杠精第二轮跟进——针对 PM 的反击做回应。"""
    prior_context = _build_prior_context(prior_conclusions, language)
    return _apply_language([
        {"role": "system", "content": CRITIC_SYSTEM_PROMPT},
        {
            "role": "user",
            "content": CRITIC_FOLLOWUP_PROMPT.format(
                topic_title=topic["title"],
                critic_prev=critic_prev,
                pm_counter=pm_counter,
                prior_context=prior_context,
            ),
        },
    ], language)


def prepare_free_topic_critic_followup(
    user_input: str,
    topic: dict,
    critic_prev: str,
    pm_counter: str,
    prior_conclusions: list[dict],
    language: str | None = None,
) -> list[dict]:
    """自由话题模式：杠精第二轮跟进。"""
    prior_context = _build_prior_context(prior_conclusions, language)
    return _apply_language([
        {"role": "system", "content": CRITIC_SYSTEM_PROMPT},
        {
            "role": "user",
            "content": CRITIC_FREE_FOLLOWUP_PROMPT.format(
                topic_title=topic["title"],
                critic_prev=critic_prev,
                pm_counter=pm_counter,
                prior_context=prior_context,
            ),
        },
    ], language)


# ============================================================
# 导演 — 话题小结 + 最终判决
# ============================================================

def prepare_topic_wrap(
    topic: dict,
    topic_exchanges: list[dict],
    prior_conclusions: list[dict],
    language: str | None = None,
) -> list[dict]:
    """导演对单个话题做一句话小结。"""
    exchanges_text = format_topic_exchanges(topic_exchanges, language)
    prior_context = _build_prior_context(prior_conclusions, language)
    return _apply_language([
        {"role": "system", "content": DIRECTOR_SYSTEM_PROMPT},
        {
            "role": "user",
            "content": DIRECTOR_WRAP_PROMPT.format(
                topic_title=topic["title"],
                topic_exchanges=exchanges_text,
                prior_context=prior_context,
            ),
        },
    ], language)


def prepare_final_verdict(
    need: dict,
    all_conclusions: list[dict],
    investor_analysis: str = "",
    language: str | None = None,
) -> list[dict]:
    """导演看完所有话题 + 投资人分析后的最终判决。"""
    sep = ": " if _is_debate_en(language) else "："
    conclusions_text = "\n".join(
        f"{i+1}. {c['title']}{sep}{c['summary']}" for i, c in enumerate(all_conclusions)
    )
    fallback_investor = "Investor analysis is not ready." if _is_debate_en(language) else "（投资人分析未就绪）"
    return _apply_language([
        {"role": "system", "content": DIRECTOR_SYSTEM_PROMPT},
        {
            "role": "user",
            "content": DIRECTOR_VERDICT_PROMPT.format(
                need_title=_need_title(need, language),
                all_conclusions=conclusions_text,
                investor_analysis=investor_analysis or fallback_investor,
            ),
        },
    ], language)


# ============================================================
# 投资人 — 后台并行分析 + 最终商业分析
# ============================================================

def prepare_investor_bg(
    need: dict,
    posts_compact: str,
    post_count: int,
    competitor_research: str = "",
    language: str | None = None,
) -> list[dict]:
    """投资人后台并行商业分析（非流式，与话题讨论同时进行）。"""
    cr = competitor_research.strip() or (
        "No web research material is available in this step; rely only on posts and demand context, and do not invent competitor names."
        if _is_debate_en(language) else
        "（本环节无网页检索材料；请仅依据帖子与需求做定性分析，**禁止**编造竞品名。）"
    )
    return _apply_language([
        {"role": "system", "content": INVESTOR_SYSTEM_PROMPT},
        {
            "role": "user",
            "content": INVESTOR_BG_ANALYSIS_PROMPT.format(
                need_title=_need_title(need, language),
                need_description=_need_description(need, language),
                posts_compact=posts_compact,
                post_count=post_count,
                competitor_research=cr,
            ),
        },
    ], language)


def prepare_free_investor_bg(user_input: str, competitor_research: str = "", language: str | None = None) -> list[dict]:
    """自由话题模式：投资人后台分析。"""
    cr = competitor_research.strip() or (
        "No web research material is available in this step; rely only on the topic context, and do not invent competitor names."
        if _is_debate_en(language) else
        "（本环节无网页检索材料；请仅依据话题做定性分析，**禁止**编造竞品名。）"
    )
    return _apply_language([
        {"role": "system", "content": INVESTOR_SYSTEM_PROMPT},
        {
            "role": "user",
            "content": INVESTOR_FREE_BG_ANALYSIS_PROMPT.format(
                user_input=user_input,
                competitor_research=cr,
            ),
        },
    ], language)


def prepare_investor_final(
    need: dict,
    all_conclusions: list[dict],
    bg_analysis: str,
    language: str | None = None,
) -> list[dict]:
    """投资人结合讨论结论的最终商业分析（流式输出）。"""
    sep = ": " if _is_debate_en(language) else "："
    conclusions_text = "\n".join(
        f"{i+1}. {c['title']}{sep}{c['summary']}" for i, c in enumerate(all_conclusions)
    )
    return _apply_language([
        {"role": "system", "content": INVESTOR_SYSTEM_PROMPT},
        {
            "role": "user",
            "content": INVESTOR_FINAL_PROMPT.format(
                need_title=_need_title(need, language),
                bg_analysis=bg_analysis,
                all_conclusions=conclusions_text,
            ),
        },
    ], language)


def prepare_free_investor_final(
    user_input: str,
    all_conclusions: list[dict],
    bg_analysis: str,
    language: str | None = None,
) -> list[dict]:
    """自由话题模式：投资人结合讨论的最终分析。"""
    sep = ": " if _is_debate_en(language) else "："
    conclusions_text = "\n".join(
        f"{i+1}. {c['title']}{sep}{c['summary']}" for i, c in enumerate(all_conclusions)
    )
    return _apply_language([
        {"role": "system", "content": INVESTOR_SYSTEM_PROMPT},
        {
            "role": "user",
            "content": INVESTOR_FREE_FINAL_PROMPT.format(
                topic_title=user_input,
                bg_analysis=bg_analysis,
                all_conclusions=conclusions_text,
            ),
        },
    ], language)


# ============================================================
# 人类介入（话题制版本）
# ============================================================

def prepare_human_inject_topic(
    need: dict,
    topic: dict,
    topic_exchanges: list[dict],
    human_message: str,
    target: str,
    language: str | None = None,
) -> list[dict]:
    """人类介入后构建目标角色的回复 messages（话题制版本）。"""
    exchanges_text = format_topic_exchanges(topic_exchanges, language)
    if target == "analyst":
        return _apply_language([
            {"role": "system", "content": PM_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": HUMAN_INJECT_PM.format(
                    need_title=_need_title(need, language),
                    topic_title=topic["title"],
                    topic_exchanges=exchanges_text,
                    human_message=human_message,
                ),
            },
        ], language)
    return _apply_language([
        {"role": "system", "content": CRITIC_SYSTEM_PROMPT},
        {
            "role": "user",
            "content": HUMAN_INJECT_CRITIC.format(
                need_title=_need_title(need, language),
                topic_title=topic["title"],
                topic_exchanges=exchanges_text,
                human_message=human_message,
            ),
        },
    ], language)


# ============================================================
# 动态推进：判断杠精反馈级别
# ============================================================

def is_structural_feedback(critic_response: str) -> bool:
    """检查杠精的回复是否标注了 [STRUCTURAL]，决定 PM 是否需要反击。"""
    clean = critic_response.strip().upper()
    return "[STRUCTURAL]" in clean


# ============================================================
# 兼容旧接口
# ============================================================

def build_full_discussion_log(debate_log: list[dict], strip_early_think: bool = True) -> str:
    """旧版：格式化 debate_log 为完整讨论记录文本。保留兼容。"""
    rn = _get_role_names()
    label_map = {**rn, "human": "负责人"}
    parts: list[str] = []
    for idx, entry in enumerate(debate_log):
        label = label_map.get(entry["role"], entry["role"])
        content = entry["content"]
        if strip_early_think and idx < len(debate_log) - 3:
            content = re.sub(r'<think>[\s\S]*?</think>', '[详细分析已省略]', content)
        parts.append(f"【{label}】{content}")
    return "\n\n".join(parts)


def prepare_initial_messages(need: dict) -> list[dict]:
    """旧版 PM 第一轮。保留兼容。"""
    return prepare_topic_pm(need, {"question": "", "title": ""}, "", [], is_first=True)


def prepare_analyst_reply(need: dict, debate_log: list[dict]) -> list[dict]:
    return prepare_initial_messages(need)


def prepare_critic_messages(need: dict, analysis: str) -> list[dict]:
    return prepare_topic_critic(need, {"title": "", "question": ""}, analysis, [])


def prepare_critic_reply(need: dict, debate_log: list[dict]) -> list[dict]:
    return prepare_critic_messages(need, "")


def prepare_director_conclude(need: dict, debate_log: list[dict]) -> list[dict]:
    return prepare_final_verdict(need, [])


def prepare_human_inject(need: dict, debate_log: list[dict], human_message: str, target: str) -> list[dict]:
    return prepare_human_inject_topic(need, {"title": "讨论", "question": ""}, [], human_message, target)


def prepare_director_initial(need: dict) -> list[dict]:
    return [
        {"role": "system", "content": DIRECTOR_SYSTEM_PROMPT},
        {"role": "user", "content": ""},
    ]


def prepare_director_evaluate(**kwargs) -> dict:
    return {"role": "user", "content": ""}


def prepare_analyst_inject(critic_response: str, director_instruction: str) -> dict:
    return {"role": "user", "content": ""}


def prepare_critic_inject(analyst_response: str, director_instruction: str) -> dict:
    return {"role": "user", "content": ""}


def compress_if_needed(messages: list[dict], threshold: int = 10000) -> list[dict]:
    return list(messages)


def build_discussion_summary(debate_log: list[dict], max_chars: int = 1500) -> str:
    return build_full_discussion_log(debate_log)


def parse_director_action(response: str) -> dict:
    text = response.strip()
    chat_text = text
    action_json = {"action": "ask_analyst", "instruction": "继续分析", "reason": ""}
    matches = list(re.finditer(r'\{[^{}]*\}', text))
    if matches:
        last_match = matches[-1]
        try:
            parsed = json.loads(last_match.group())
            if "action" in parsed:
                action_json = parsed
                chat_text = text[:last_match.start()].strip()
        except Exception:
            pass
    if "chat" in action_json and action_json["chat"]:
        chat_text = action_json["chat"]
    elif "chat" in action_json and action_json["chat"] == "":
        chat_text = ""
    action_json["chat"] = chat_text
    return action_json


# ============================================================
# 报告生成（保持不变）
# ============================================================

def _format_debate_log(debate_log: list[dict]) -> str:
    """格式化讨论记录。去掉 <think> 内容以减小 prompt 体积。"""
    log_text = ""
    for entry in debate_log:
        rn = _get_role_names()
        role_label = {**rn, "human": "负责人"}.get(entry["role"], entry["role"])
        content = entry["content"]
        content = re.sub(r'<think>[\s\S]*?</think>', '', content).strip()
        if not content:
            continue
        log_text += f"\n### {role_label}\n{content}\n"
    return log_text


def generate_product_proposal(need: dict, debate_log: list[dict], language: str | None = None) -> str:
    log_text = _format_debate_log(debate_log)
    posts_summary = _format_need_posts_summary(need)
    messages = _apply_language([
        {"role": "system", "content": "You are a product strategy analyst. Extract a product proposal from the discussion." if _is_debate_en(language) else "你是一个产品策略分析师。根据讨论结果提炼产品方案。"},
        {
            "role": "user",
            "content": PRODUCT_PROPOSAL_PROMPT.format(
                need_title=_need_title(need, language),
                posts_summary=posts_summary,
                debate_log=log_text,
            ),
        },
    ], language)
    return call_for_role("director", messages)


def prepare_deep_dive_messages(product_proposal: str, search_results: str) -> list[dict]:
    return [
        {"role": "system", "content": DEEP_DIVE_SYSTEM_PROMPT},
        {
            "role": "user",
            "content": DEEP_DIVE_ANALYSIS_PROMPT.format(
                product_proposal=product_proposal,
                search_results=search_results,
            ),
        },
    ]


def generate_final_report(
    need: dict,
    debate_log: list[dict],
    analyst_messages: list[dict],
    deep_dive_data: str = "",
    language: str | None = None,
) -> str:
    log_text = _format_debate_log(debate_log)
    posts_summary = _format_need_posts_summary(need)
    original_topic = need.get("original_topic", "")
    report_title = _need_title(need, language) if _is_debate_en(language) else (original_topic if original_topic else need.get("need_title", ""))
    system_prompt = (
        f"You are a product analyst responsible for writing the final product evaluation report. Output pure Markdown.\n\nMost important constraint: the report must stay tightly focused on \"{report_title}\" and must not drift into other directions."
        if _is_debate_en(language) else
        f"你是一个产品分析师，负责生成最终的产品评估报告。输出纯 Markdown 格式。\n\n⚠️ 最重要的约束：报告必须紧密围绕「{report_title}」展开，不要偏离到其他方向。"
    )
    fallback_deep_dive = "No deep-dive data is available yet." if _is_debate_en(language) else "（暂无深挖数据）"
    report_messages = _apply_language([
        {"role": "system", "content": system_prompt},
        {
            "role": "user",
            "content": FINAL_REPORT_PROMPT.format(
                need_title=report_title,
                posts_summary=posts_summary,
                debate_log=log_text,
                deep_dive_data=deep_dive_data or fallback_deep_dive,
            ),
        },
    ], language)
    return call_for_role("director", report_messages, max_tokens=8000)
