"""
quote_extractor.py — 原文摘录提取模块

从帖子和评论中提取 verbatim quotes，严禁改写，附带 URL 溯源和信号分类。
"""

import json
from dataclasses import dataclass, asdict

from .llm_client import call_llm
from prompts import QUOTE_EXTRACTION_PROMPT, FEMWC_SCORING_PROMPT


@dataclass
class Quote:
    text: str
    source_url: str
    author: str
    score: int
    platform: str
    context: str
    signal_type: str  # pain / workaround / willingness_to_pay / competitor_complaint / journey


@dataclass
class FemwcResult:
    F: dict
    E: dict
    M: dict
    W: dict
    C: dict
    total: float
    verdict: str
    summary: str


@dataclass
class NeedPackage:
    title: str
    description: str
    femwc: dict
    total_score: float
    quotes: list[dict]
    representative_posts: list[dict]
    user_segments: list[str]
    existing_solutions: list[str]
    signal_summary: str


def _parse_json_safe(text: str):
    if not text:
        return None
    text = text.strip()
    try:
        return json.loads(text)
    except Exception:
        pass
    import re
    m = re.search(r'```(?:json)?\s*\n?([\s\S]*?)\n?\s*```', text)
    if m:
        try:
            return json.loads(m.group(1).strip())
        except Exception:
            pass
    first = text.find('[')
    last = text.rfind(']')
    if first != -1 and last > first:
        try:
            return json.loads(text[first:last + 1])
        except Exception:
            pass
    first = text.find('{')
    last = text.rfind('}')
    if first != -1 and last > first:
        try:
            return json.loads(text[first:last + 1])
        except Exception:
            pass
    return None


def extract_quotes(posts: list[dict]) -> list[Quote]:
    posts_for_llm = []
    for i, p in enumerate(posts):
        entry = {
            "idx": i,
            "title": p.get("title", ""),
            "content": (p.get("content", "") or "")[:1500],
            "url": p.get("url", ""),
            "source": p.get("source", ""),
            "score": p.get("score", 0),
            "comments": [],
        }
        for ci, c in enumerate(p.get("comments", [])[:15]):
            entry["comments"].append({
                "idx": ci,
                "text": c[:800] if isinstance(c, str) else str(c)[:800],
            })
        posts_for_llm.append(entry)

    prompt = QUOTE_EXTRACTION_PROMPT.format(
        posts_json=json.dumps(posts_for_llm, ensure_ascii=False, indent=2)
    )
    messages = [{"role": "user", "content": prompt}]

    try:
        response = call_llm(messages)
        raw = _parse_json_safe(response)
        if not raw or not isinstance(raw, list):
            print(f"[QuoteExtractor] Parse failed, raw: {response[:300]}")
            return []

        quotes = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            text = item.get("text", "").strip()
            if len(text) < 30:
                continue
            quotes.append(Quote(
                text=text,
                source_url=item.get("source_url", ""),
                author=item.get("author", ""),
                score=item.get("score", 0),
                platform=item.get("platform", ""),
                context=item.get("context", ""),
                signal_type=item.get("signal_type", "pain"),
            ))
        print(f"[QuoteExtractor] Extracted {len(quotes)} quotes from {len(posts)} posts")
        return quotes

    except Exception as e:
        print(f"[QuoteExtractor] LLM call failed: {e}")
        return []


def score_femwc(need: dict, quotes: list[Quote]) -> dict:
    quotes_json = json.dumps(
        [asdict(q) for q in quotes],
        ensure_ascii=False, indent=2
    )
    prompt = FEMWC_SCORING_PROMPT.format(
        need_title=need.get("need_title", ""),
        need_description=need.get("need_description", ""),
        quotes_json=quotes_json,
        post_count=len(need.get("posts", [])),
        total_score=need.get("total_score", 0),
        total_comments=need.get("total_comments", 0),
    )
    messages = [{"role": "user", "content": prompt}]

    try:
        response = call_llm(messages)
        result = _parse_json_safe(response)
        if result and isinstance(result, dict):
            total = (
                result.get("F", {}).get("score", 1) * 0.30 +
                result.get("E", {}).get("score", 1) * 0.20 +
                result.get("M", {}).get("score", 1) * 0.20 +
                result.get("W", {}).get("score", 1) * 0.20 +
                result.get("C", {}).get("score", 1) * 0.10
            )
            result["total"] = round(total, 2)
            return result
    except Exception as e:
        print(f"[FEMWC] LLM call failed: {e}")

    return {
        "F": {"score": 1, "reasoning": "未评分"},
        "E": {"score": 1, "reasoning": "未评分"},
        "M": {"score": 1, "reasoning": "未评分"},
        "W": {"score": 1, "reasoning": "未评分"},
        "C": {"score": 1, "reasoning": "未评分"},
        "total": 1.0,
        "verdict": "未评分",
        "summary": "评分失败",
    }


def build_need_package(need: dict, quotes: list[Quote], femwc: dict) -> dict:
    quote_dicts = [asdict(q) for q in quotes]

    user_segments = set()
    solutions = set()
    for q in quotes:
        if q.signal_type == "competitor_complaint":
            parts = q.context.split()
            for p in parts:
                if p[0].isupper() and len(p) > 2:
                    solutions.add(p)

    return {
        "title": need.get("need_title", ""),
        "description": need.get("need_description", ""),
        "femwc": femwc,
        "total_score": femwc.get("total", 0),
        "quotes": quote_dicts,
        "representative_posts": [
            {
                "title": p.get("title", ""),
                "title_zh": p.get("title_zh", ""),
                "url": p.get("url", ""),
                "score": p.get("score", 0),
                "source": p.get("source", ""),
            }
            for p in need.get("posts", [])[:5]
        ],
        "user_segments": list(user_segments) if user_segments else ["待分析"],
        "existing_solutions": list(solutions) if solutions else ["待调研"],
        "signal_summary": femwc.get("summary", ""),
    }
