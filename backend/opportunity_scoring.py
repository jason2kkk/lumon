"""
opportunity_scoring.py — 需求挖掘机会评分

把 Reddit/HN 帖子的热度、评论信号、workaround、付费意愿和可软件化程度
转成可解释的 opportunity_score，供挖掘排序和需求卡片展示使用。
"""

from __future__ import annotations

import math
import re
from collections import Counter
from typing import Any


PAIN_TERMS = (
    "tired of", "sick of", "fed up", "frustrated", "annoying", "hate",
    "struggle", "struggling", "pain", "painful", "impossible", "overwhelmed",
    "driving me crazy", "takes hours", "waste time", "manual", "tedious",
    "can't keep up", "gave up", "given up",
)

WORKAROUND_TERMS = (
    "workaround", "hack", "spreadsheet", "google sheet", "excel", "notion",
    "i built", "i made", "i ended up", "my workflow", "copy paste",
    "manually", "manual process", "temporary solution", "jury-rigged",
)

SWITCHING_TERMS = (
    "alternative", "switched from", "moved from", "moved to", "replaced",
    "replacement", "vs ", "versus", "instead of", "looking for another",
)

PAYMENT_TERMS = (
    "i would pay", "i'd pay", "worth paying", "worth every penny",
    "shut up and take my money", "lifetime", "subscription", "too expensive",
    "pricing", "pricey", "premium", "free alternative", "paid plan",
)

TRUST_TERMS = (
    "privacy", "private", "trust", "security", "permission", "bank access",
    "connect my bank", "data", "oauth", "tracking", "surveillance",
)

SOFTWARE_TERMS = (
    "app", "tool", "software", "ai", "automate", "automatic", "automation",
    "scanner", "dashboard", "workflow", "notification", "reminder",
    "extension", "plugin", "integrate", "sync",
)

BROAD_SUBREDDITS = {
    "askreddit", "nostupidquestions", "todayilearned", "technology", "pics",
    "funny", "videos", "worldnews", "news", "popular",
}

SIGNAL_LABELS = {
    "pain": "强痛点",
    "workaround": "替代方案",
    "switching": "竞品不满",
    "payment": "付费信号",
    "trust": "信任阻力",
    "software": "软件可解",
    "comment": "评论共鸣",
}


def _comment_text(comment: Any) -> str:
    if isinstance(comment, str):
        return comment
    if isinstance(comment, dict):
        return str(comment.get("body") or comment.get("text") or "")
    return str(comment or "")


def _comment_score(comment: Any) -> int:
    if isinstance(comment, dict):
        try:
            return int(comment.get("score") or 0)
        except (TypeError, ValueError):
            return 0
    return 0


def _source_subreddit(post: dict) -> str:
    source = str(post.get("source") or "")
    if source.startswith("reddit/"):
        return source.split("/", 1)[1].strip().lower()
    return ""


def _combined_text(post: dict, comment_limit: int = 8) -> str:
    parts = [
        str(post.get("title") or ""),
        str(post.get("content") or ""),
    ]
    for comment in (post.get("comments") or [])[:comment_limit]:
        parts.append(_comment_text(comment))
    return "\n".join(parts)


def _count_terms(text: str, terms: tuple[str, ...]) -> int:
    lower = text.lower()
    return sum(1 for term in terms if term in lower)


def _score_0_5(base: float) -> float:
    return max(1.0, min(5.0, round(base, 2)))


def _compressed_heat(score: int, comments: int) -> float:
    safe_score = max(0, score)
    safe_comments = max(0, comments)
    return math.log1p(safe_score) * 0.65 + math.log1p(safe_comments) * 0.85


def passes_heat_gate(post: dict) -> bool:
    """热度是共鸣门槛；垂直小社区允许低热但必须有需求信号。"""
    try:
        score = int(post.get("score") or 0)
    except (TypeError, ValueError):
        score = 0
    try:
        comments = int(post.get("num_comments") or len(post.get("comments") or []) or 0)
    except (TypeError, ValueError):
        comments = 0

    if score >= 10 or comments >= 5 or (score >= 3 and comments >= 3):
        return True

    source = str(post.get("source") or "")
    if not source.startswith("reddit"):
        return score >= 5 or comments >= 2

    subreddit = _source_subreddit(post)
    if subreddit and subreddit not in BROAD_SUBREDDITS:
        text = _combined_text(post, comment_limit=5)
        signal_hits = (
            _count_terms(text, PAIN_TERMS)
            + _count_terms(text, WORKAROUND_TERMS)
            + _count_terms(text, SWITCHING_TERMS)
            + _count_terms(text, PAYMENT_TERMS)
        )
        return (score >= 2 or comments >= 2) and signal_hits > 0

    return False


def score_post_opportunity(post: dict) -> dict[str, Any]:
    """计算单帖机会分，并返回可解释信号。"""
    text = _combined_text(post)
    title_content = _combined_text(post, comment_limit=0)
    comments = post.get("comments") or []

    try:
        post_score = int(post.get("score") or 0)
    except (TypeError, ValueError):
        post_score = 0
    try:
        num_comments = int(post.get("num_comments") or len(comments) or 0)
    except (TypeError, ValueError):
        num_comments = len(comments)

    pain_hits = _count_terms(text, PAIN_TERMS)
    workaround_hits = _count_terms(text, WORKAROUND_TERMS)
    switching_hits = _count_terms(text, SWITCHING_TERMS)
    payment_hits = _count_terms(text, PAYMENT_TERMS)
    trust_hits = _count_terms(text, TRUST_TERMS)
    software_hits = _count_terms(text, SOFTWARE_TERMS)

    signal_comment_count = 0
    high_signal_comment_count = 0
    for comment in comments[:20]:
        body = _comment_text(comment)
        c_hits = (
            _count_terms(body, PAIN_TERMS)
            + _count_terms(body, WORKAROUND_TERMS)
            + _count_terms(body, SWITCHING_TERMS)
            + _count_terms(body, PAYMENT_TERMS)
        )
        if c_hits:
            signal_comment_count += 1
            if _comment_score(comment) >= 3:
                high_signal_comment_count += 1

    heat_score = _score_0_5(_compressed_heat(post_score, num_comments))
    content_len_bonus = 0.35 if len(title_content) >= 220 else 0
    consequence_bonus = 0.35 if re.search(r"\b(because|every time|hours?|can't|cannot|so that|end up)\b", text.lower()) else 0
    pain_specificity = _score_0_5(1.15 + min(pain_hits, 5) * 0.55 + content_len_bonus + consequence_bonus)
    comment_signal_quality = _score_0_5(1.0 + min(signal_comment_count, 4) * 0.6 + min(high_signal_comment_count, 3) * 0.45 + min(num_comments, 20) * 0.035)
    workaround_or_switching = _score_0_5(1.0 + min(workaround_hits, 4) * 0.7 + min(switching_hits, 4) * 0.65)
    willingness_to_pay = _score_0_5(1.0 + min(payment_hits, 4) * 0.8)
    software_solvability = _score_0_5(2.4 + min(software_hits, 4) * 0.45 - min(trust_hits, 3) * 0.05)

    opportunity_score = round(
        heat_score * 0.25
        + pain_specificity * 0.20
        + comment_signal_quality * 0.20
        + workaround_or_switching * 0.15
        + willingness_to_pay * 0.10
        + software_solvability * 0.10,
        2,
    )

    signal_counts = {
        "pain": pain_hits,
        "workaround": workaround_hits,
        "switching": switching_hits,
        "payment": payment_hits,
        "trust": trust_hits,
        "software": software_hits,
        "comment": signal_comment_count,
    }
    top_signals = [
        SIGNAL_LABELS[key]
        for key, _ in Counter(signal_counts).most_common()
        if signal_counts[key] > 0 and key in SIGNAL_LABELS
    ][:4]

    comment_read_score = round(
        math.log1p(max(num_comments, 0)) * 8
        + (pain_hits + payment_hits) * 3
        + (workaround_hits + switching_hits) * 4
        + (2 if _source_subreddit(post) and _source_subreddit(post) not in BROAD_SUBREDDITS else 0)
        + math.log1p(max(post_score, 0)) * 2,
        2,
    )

    return {
        "opportunity_score": opportunity_score,
        "heat_score": heat_score,
        "comment_read_score": comment_read_score,
        "passes_heat_gate": passes_heat_gate(post),
        "top_signals": top_signals,
        "signal_counts": signal_counts,
        "score_breakdown": {
            "heat": heat_score,
            "pain_specificity": pain_specificity,
            "comment_signal_quality": comment_signal_quality,
            "workaround_or_switching": workaround_or_switching,
            "willingness_to_pay": willingness_to_pay,
            "software_solvability": software_solvability,
        },
    }


def annotate_posts_with_opportunity(posts: list[dict]) -> list[dict]:
    for post in posts:
        scoring = score_post_opportunity(post)
        post["opportunity_score"] = scoring["opportunity_score"]
        post["comment_read_score"] = scoring["comment_read_score"]
        post["passes_heat_gate"] = scoring["passes_heat_gate"]
        post["top_signals"] = scoring["top_signals"]
        post["signal_counts"] = scoring["signal_counts"]
        post["score_breakdown"] = scoring["score_breakdown"]
    return posts


def _heat_summary(posts: list[dict]) -> str:
    total_score = sum(int(p.get("score") or 0) for p in posts)
    total_comments = sum(int(p.get("num_comments") or len(p.get("comments") or []) or 0) for p in posts)
    return f"{len(posts)}帖，{total_score}赞，{total_comments}评"


def _need_signal_summary(posts: list[dict]) -> tuple[list[str], dict[str, int]]:
    counts: Counter[str] = Counter()
    for post in posts:
        if "signal_counts" not in post:
            scoring = score_post_opportunity(post)
            post["signal_counts"] = scoring["signal_counts"]
            post["top_signals"] = scoring["top_signals"]
            post["opportunity_score"] = scoring["opportunity_score"]
        for key, value in (post.get("signal_counts") or {}).items():
            try:
                counts[key] += int(value)
            except (TypeError, ValueError):
                continue
    labels = [
        SIGNAL_LABELS[key]
        for key, _ in counts.most_common()
        if counts[key] > 0 and key in SIGNAL_LABELS
    ][:5]
    return labels, dict(counts)


def annotate_need_with_opportunity(need: dict) -> dict:
    posts = need.get("posts") or []
    if not posts:
        need["opportunity_score"] = 1.0
        need["top_signals"] = []
        need["heat_summary"] = "0帖，0赞，0评"
        need["why_this_matters"] = "缺少支撑帖子，暂不判断机会价值。"
        return need

    annotate_posts_with_opportunity(posts)
    post_scores = sorted(
        [float(p.get("opportunity_score") or 1.0) for p in posts],
        reverse=True,
    )
    top_avg = sum(post_scores[: min(5, len(post_scores))]) / min(5, len(post_scores))
    subreddit_count = len({
        _source_subreddit(p)
        for p in posts
        if _source_subreddit(p)
    })
    support_bonus = 0.12 if len(posts) >= 2 else 0
    diversity_bonus = 0.12 if subreddit_count >= 2 else 0
    score = max(1.0, min(5.0, round(top_avg + support_bonus + diversity_bonus, 2)))

    top_signals, signal_counts = _need_signal_summary(posts)
    heat_summary = _heat_summary(posts)
    if score >= 4.2:
        level = "高机会"
    elif score >= 3.4:
        level = "值得观察"
    else:
        level = "早期信号"

    if top_signals:
        signal_text = "、".join(top_signals[:3])
        why = f"{heat_summary}，主要信号是{signal_text}，建议优先查看证据和评论共鸣。"
    else:
        why = f"{heat_summary}，有一定相关讨论，但产品机会信号仍需深挖验证。"

    need["opportunity_score"] = score
    need["opportunity_level"] = level
    need["top_signals"] = top_signals
    need["signal_counts"] = signal_counts
    need["heat_summary"] = heat_summary
    need["why_this_matters"] = why
    return need


def annotate_needs_with_opportunity(needs: list[dict]) -> list[dict]:
    for need in needs:
        annotate_need_with_opportunity(need)
    needs.sort(
        key=lambda n: (
            float(n.get("opportunity_score") or 0),
            int(n.get("total_comments") or 0),
            int(n.get("total_score") or 0),
        ),
        reverse=True,
    )
    return needs
