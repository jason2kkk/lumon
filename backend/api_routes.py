"""
api_routes.py — FastAPI 路由定义

职责：REST 端点（采集、帖子、报告、配置）+ SSE 端点（辩论流式、报告生成流式）
"""

import json
import hashlib
import os
import random
import re
import re as _re_tag
import threading
import time as _time
import uuid
from collections import Counter
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Generator

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from .llm_client import (
    check_config, get_config_values, estimate_debate_cost, test_connection,
    reset_clients, set_runtime_config, get_provider_config,
    call_for_role, call_for_role_stream, get_role_model_config,
    get_token_stats, reset_token_stats,
    call_llm, call_llm_stream, check_llm_available, check_role_models_available,
    get_general_model, set_general_model,
    set_thread_session, get_thread_session, clear_thread_session,
    is_transient_connection_error,
)
from .session_context import get_session, SessionContext, cleanup_expired_sessions, _sessions, _sessions_lock, SESSIONS_DIR
from .scrapers import search_hackernews, fetch_hackernews, REDDIT_CATEGORIES, hard_filter
from .rdt_client import get_reddit_fetcher, init_reddit_fetcher
from .quote_extractor import extract_quotes, score_femwc, build_need_package
from .opportunity_scoring import annotate_needs_with_opportunity, annotate_posts_with_opportunity
from .debate import (
    generate_final_report, generate_product_proposal,
    prepare_initial_messages, prepare_critic_messages,
    prepare_analyst_reply, prepare_critic_reply,
    prepare_director_conclude, prepare_human_inject,
    prepare_deep_dive_messages,
    prepare_topic_analysis, prepare_topic_pm, prepare_topic_critic,
    prepare_topic_pm_counter, prepare_topic_wrap, prepare_final_verdict,
    prepare_human_inject_topic, is_structural_feedback,
    format_topic_exchanges,
    prepare_free_topic_analysis, prepare_free_topic_pm, prepare_free_topic_critic,
    prepare_topic_critic_followup, prepare_free_topic_critic_followup,
    prepare_investor_bg, prepare_investor_final,
    prepare_free_investor_bg, prepare_free_investor_final,
    _format_need_posts_compact,
)
from prompts import (
    CLUSTERING_PROMPT, CLUSTERING_STEP1_PROMPT, CLUSTERING_STEP2_PROMPT,
    SEARCH_PLANNING_PROMPT, POST_FILTER_PROMPT,
    BATCH_RELEVANCE_PROMPT, DEEP_MINING_QUERY_PROMPT, AUTO_DISCOVER_PROMPT,
    DIRECT_REPORT_PROMPT, DIRECT_REPORT_PROMPT_EN, SIGNAL_EXTRACTION_PROMPT,
    QUICK_RELEVANCE_PROMPT,
    POC_EVAL_PROMPT,
    QUICK_SEARCH_QUERY_CLASSIFIER_PROMPT,
    QUICK_SEARCH_PLANNING_PROMPT,
    QUICK_SEARCH_MARKET_PLANNING_PROMPT,
    QUICK_SEARCH_MARKET_REPAIR_PROMPT,
    QUICK_SEARCH_SUMMARY_PROMPT,
    QUICK_SEARCH_SUMMARY_PROMPT_EN,
    QUICK_SEARCH_PROCESS_SUMMARY_PROMPT,
    QUICK_SEARCH_PROCESS_SUMMARY_PROMPT_EN,
    QUICK_SEARCH_TOPIC_PROMPT,
    QUICK_SEARCH_TRANSLATE_PROMPT,
)
from .web_search import (
    search_competitors,
    discover_reddit_urls,
    discover_hn_urls,
    gpt_discover_reddit_urls,
    claude_discover_reddit_urls,
    investor_competitor_web_context,
)
from .st_client import (
    check_available as st_check_available,
    fetch_app_snapshot as st_fetch_app_snapshot,
    fetch_app_reviews as st_fetch_app_reviews,
    fetch_apps_country_platform_trends as st_fetch_apps_country_platform_trends,
    fetch_apps_revenue_download_timeseries as st_fetch_apps_revenue_download_timeseries,
    search_market_apps,
    _market_app_competitor_query_name as st_app_competitor_query_name,
    _market_direct_app_query_name as st_direct_app_query_name,
    sensor_tower_search_url,
)

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data" / "reports"
CACHE_DIR = ROOT / "data" / "cache"
DATA_DIR.mkdir(parents=True, exist_ok=True)
CACHE_DIR.mkdir(parents=True, exist_ok=True)

def _safe_json_write(path: Path, data, **kwargs):
    """原子写入 JSON：先写 .tmp 再 rename，防止 crash 损坏文件。"""
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, **kwargs)
    tmp.replace(path)

# ---- 产品埋点统计（只记录低敏功能事件，不记录用户输入正文） ----
_ANALYTICS_DIR = ROOT / "data" / "analytics"
_ANALYTICS_EVENTS_DIR = _ANALYTICS_DIR / "events"
_ANALYTICS_SUMMARY_FILE = _ANALYTICS_DIR / "summary.json"
_analytics_lock = threading.Lock()
_session_usage_cache: dict[str, Any] = {"ts": 0.0, "data": None}

_SENSITIVE_ANALYTICS_KEYWORDS = (
    "api", "key", "secret", "token", "password", "query", "text", "content",
    "prompt", "report", "message", "filename", "url",
)


def _analytics_request_host(request: Request) -> tuple[str, str]:
    """返回标准化 host/port，用于区分本地和远程访问。"""
    raw_host = (
        request.headers.get("x-forwarded-host")
        or request.headers.get("host")
        or ""
    ).split(",")[0].strip().lower()
    if raw_host.startswith("[::1]"):
        return "::1", raw_host.rsplit(":", 1)[-1] if ":" in raw_host else ""
    if ":" in raw_host:
        hostname, port = raw_host.rsplit(":", 1)
        return hostname, port
    return raw_host, ""


def _analytics_environment(request: Request) -> str:
    """按请求来源给本地诊断事件分桶。"""
    hostname, _port = _analytics_request_host(request)
    if hostname in {"localhost", "127.0.0.1", "::1"}:
        return "local"
    return "production"


def _is_local_analytics_request(request: Request) -> bool:
    """统计面板只允许本地查看，避免向远程请求暴露聚合数据。"""
    hostname, _port = _analytics_request_host(request)
    return hostname in {"localhost", "127.0.0.1", "::1"}


def _is_local_dev_request(request: Request) -> bool:
    """判断当前请求是否来自本地开发入口，用于隐藏未稳定上线的实验接口。"""
    if os.getenv("LUMON_ENABLE_LOCAL_EXPERIMENTS", "").strip() == "1":
        return True
    return _analytics_environment(request) == "local"


def _quick_search_enabled() -> bool:
    """读取雷达搜索功能开关。"""
    raw = os.getenv("LUMON_QUICK_SEARCH_ENABLED", "1").strip().lower()
    return raw not in {"0", "false", "off", "no"}


def _ensure_quick_search_enabled() -> None:
    if not _quick_search_enabled():
        raise HTTPException(status_code=404, detail="not found")


def _session_analytics_id(session_id: str) -> str:
    """把 session id 转成不可逆短哈希，避免埋点文件保存原始浏览器 id。"""
    return hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:16]


def _analytics_feature(event_name: str) -> str:
    if "." in event_name:
        return event_name.split(".", 1)[0]
    return event_name


def _safe_analytics_value(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return value[:160]
    if isinstance(value, (list, tuple)):
        return [_safe_analytics_value(v) for v in value[:12]]
    if isinstance(value, dict):
        cleaned: dict[str, Any] = {}
        for k, v in list(value.items())[:24]:
            key = str(k)[:64]
            if any(word in key.lower() for word in _SENSITIVE_ANALYTICS_KEYWORDS):
                continue
            cleaned[key] = _safe_analytics_value(v)
        return cleaned
    return str(value)[:160]


def _sanitize_analytics_props(props: dict[str, Any] | None) -> dict[str, Any]:
    if not props:
        return {}
    return _safe_analytics_value(props)


def _load_analytics_summary() -> dict:
    if _ANALYTICS_SUMMARY_FILE.exists():
        try:
            data = json.loads(_ANALYTICS_SUMMARY_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
        except Exception:
            pass
    return {
        "total_events": 0,
        "event_counts": {},
        "feature_counts": {},
        "users": {},
        "daily": {},
        "by_env": {},
        "last_event_at": None,
    }


def _read_session_usage_stats() -> dict[str, int]:
    """只读统计历史 session 目录，用于埋点上线前已有用户的兜底口径。"""
    now = _time.time()
    cached = _session_usage_cache.get("data")
    if cached is not None and now - float(_session_usage_cache.get("ts", 0)) < 60:
        return cached

    stats = {"total_users": 0, "active_users_1d": 0, "active_users_7d": 0, "active_users_30d": 0}
    if not SESSIONS_DIR.exists():
        _session_usage_cache.update({"ts": now, "data": stats})
        return stats

    for session_dir in SESSIONS_DIR.iterdir():
        if not session_dir.is_dir():
            continue
        stats["total_users"] += 1
        ts_file = session_dir / ".last_active"
        try:
            last = float(ts_file.read_text().strip()) if ts_file.exists() else session_dir.stat().st_mtime
        except Exception:
            try:
                last = ts_file.stat().st_mtime if ts_file.exists() else session_dir.stat().st_mtime
            except Exception:
                last = 0
        if now - last <= 86400:
            stats["active_users_1d"] += 1
        if now - last <= 7 * 86400:
            stats["active_users_7d"] += 1
        if now - last <= 30 * 86400:
            stats["active_users_30d"] += 1

    _session_usage_cache.update({"ts": now, "data": stats})
    return stats


def _record_analytics_event(session_id: str, event_name: str, props: dict[str, Any] | None = None, env: str = "production") -> dict:
    """记录一次产品事件：追加 JSONL 明细，并原子更新聚合 summary。"""
    event_name = event_name.strip().lower()
    if not _re_tag.fullmatch(r"[a-z0-9_.:-]{2,80}", event_name):
        raise HTTPException(status_code=400, detail="invalid event name")
    env = env if env in {"production", "local"} else "production"

    now_ms = int(_time.time() * 1000)
    day = datetime.utcfromtimestamp(now_ms / 1000).strftime("%Y-%m-%d")
    analytics_id = _session_analytics_id(session_id)
    feature = _analytics_feature(event_name)
    safe_props = _sanitize_analytics_props(props)

    event = {
        "ts": now_ms,
        "day": day,
        "session": analytics_id,
        "event": event_name,
        "feature": feature,
        "env": env,
        "props": safe_props,
    }

    def bump_summary_bucket(bucket: dict) -> None:
        bucket["total_events"] = int(bucket.get("total_events", 0)) + 1
        bucket["last_event_at"] = now_ms

        event_counts = bucket.setdefault("event_counts", {})
        event_counts[event_name] = int(event_counts.get(event_name, 0)) + 1

        feature_counts = bucket.setdefault("feature_counts", {})
        feature_counts[feature] = int(feature_counts.get(feature, 0)) + 1

        users = bucket.setdefault("users", {})
        user_info = users.setdefault(analytics_id, {"first_seen": now_ms, "last_seen": now_ms, "events": 0})
        user_info["first_seen"] = min(int(user_info.get("first_seen", now_ms)), now_ms)
        user_info["last_seen"] = now_ms
        user_info["events"] = int(user_info.get("events", 0)) + 1

        daily = bucket.setdefault("daily", {})
        day_info = daily.setdefault(day, {"events": 0, "event_counts": {}, "feature_counts": {}, "users": {}})
        day_info["events"] = int(day_info.get("events", 0)) + 1
        day_event_counts = day_info.setdefault("event_counts", {})
        day_event_counts[event_name] = int(day_event_counts.get(event_name, 0)) + 1
        day_feature_counts = day_info.setdefault("feature_counts", {})
        day_feature_counts[feature] = int(day_feature_counts.get(feature, 0)) + 1
        day_info.setdefault("users", {})[analytics_id] = True

    with _analytics_lock:
        _ANALYTICS_EVENTS_DIR.mkdir(parents=True, exist_ok=True)
        event_file = _ANALYTICS_EVENTS_DIR / f"{day}.jsonl"
        with event_file.open("a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n")

        summary = _load_analytics_summary()
        bump_summary_bucket(summary)
        env_bucket = summary.setdefault("by_env", {}).setdefault(env, {
            "total_events": 0,
            "event_counts": {},
            "feature_counts": {},
            "users": {},
            "daily": {},
            "last_event_at": None,
        })
        bump_summary_bucket(env_bucket)

        _safe_json_write(_ANALYTICS_SUMMARY_FILE, summary, indent=2)

    return {"ok": True}


def _empty_analytics_bucket() -> dict:
    return {
        "total_events": 0,
        "event_counts": {},
        "feature_counts": {},
        "users": {},
        "daily": {},
        "last_event_at": None,
    }


def _analytics_summary_public(env: str = "production") -> dict:
    """返回聚合埋点，不暴露 session 哈希列表。"""
    summary = _load_analytics_summary()
    env = env if env in {"production", "local", "all"} else "production"
    if env == "all":
        bucket = summary
    else:
        bucket = (summary.get("by_env") or {}).get(env) or _empty_analytics_bucket()

    users = bucket.get("users", {}) if isinstance(bucket.get("users"), dict) else {}
    session_stats = _read_session_usage_stats()
    now_ms = int(_time.time() * 1000)

    def active_since(days: int) -> int:
        threshold = now_ms - days * 86400 * 1000
        return sum(1 for info in users.values() if int(info.get("last_seen", 0)) >= threshold)

    daily_public = {}
    for day, info in sorted((bucket.get("daily") or {}).items()):
        users_for_day = info.get("users", {}) if isinstance(info, dict) else {}
        daily_public[day] = {
            "events": int(info.get("events", 0)),
            "unique_users": len(users_for_day),
            "event_counts": info.get("event_counts", {}),
            "feature_counts": info.get("feature_counts", {}),
        }

    use_session_fallback = env == "all"
    return {
        "environment": env,
        "total_events": int(bucket.get("total_events", 0)),
        "unique_users": max(len(users), session_stats["total_users"]) if use_session_fallback else len(users),
        "tracked_unique_users": len(users),
        "historical_session_users": session_stats["total_users"] if use_session_fallback else None,
        "active_users_1d": max(active_since(1), session_stats["active_users_1d"]) if use_session_fallback else active_since(1),
        "active_users_7d": max(active_since(7), session_stats["active_users_7d"]) if use_session_fallback else active_since(7),
        "active_users_30d": max(active_since(30), session_stats["active_users_30d"]) if use_session_fallback else active_since(30),
        "event_counts": bucket.get("event_counts", {}),
        "feature_counts": bucket.get("feature_counts", {}),
        "daily": daily_public,
        "last_event_at": bucket.get("last_event_at"),
    }


# ---- 挖掘结果全局缓存（相同参数 7 天内复用） ----
_FETCH_CACHE_DIR = CACHE_DIR / "fetch"
_FETCH_CACHE_DIR.mkdir(parents=True, exist_ok=True)
_FETCH_CACHE_EXPIRE = 7 * 86400  # 7 天
_FETCH_CACHE_SCOPE = "strategy-v13-market-allcards-sourceids-i18n-needs"
_FETCH_CACHE_SCHEMA_VERSION = 2

def _fetch_cache_key(req: "FetchRequest") -> str | None:
    """为 sentence/keywords 模式生成缓存 key，open 模式返回 None（不缓存）。"""
    import hashlib
    if req.mode not in ("sentence", "keywords") or req.demo:
        return None
    payload = json.dumps({
        "mode": req.mode,
        "query": req.query.strip().lower() if req.mode == "sentence" else "",
        "keywords": sorted(k.strip().lower() for k in req.keywords) if req.mode == "keywords" else [],
        "sources": sorted(req.sources),
        "time_period": req.time_period,
        "category": req.category,
        "reddit_categories": sorted(req.reddit_categories),
        "product": req.product.strip().lower(),
        "market": req.market.strip().lower(),
        "demographics": req.demographics.strip().lower(),
        "segment": req.segment.strip().lower(),
        "competitors": req.competitors.strip().lower(),
        "fetch_model": req.fetch_model or "default",
        "fetch_model_scope": _FETCH_CACHE_SCOPE,
        "cache_schema_version": _FETCH_CACHE_SCHEMA_VERSION,
    }, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode()).hexdigest()[:16]

def _fetch_cache_read(cache_key: str) -> list | None:
    """读取未过期的缓存，返回 needs 列表或 None。"""
    path = _FETCH_CACHE_DIR / f"{cache_key}.json"
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if _time.time() - data.get("created_at", 0) > _FETCH_CACHE_EXPIRE:
            path.unlink(missing_ok=True)
            return None
        if data.get("fetch_model_scope") != _FETCH_CACHE_SCOPE:
            return None
        if int(data.get("cache_schema_version") or 0) != _FETCH_CACHE_SCHEMA_VERSION:
            return None
        needs = data.get("needs")
        if not isinstance(needs, list):
            return None
        # 第二阶段以后，每张需求卡都必须带商业信号兜底和 source id。
        if any(not isinstance(n, dict) or not n.get("market_validation") or not n.get("source_ids") for n in needs):
            return None
        return needs
    except Exception:
        return None

def _fetch_cache_write(cache_key: str, needs: list, req: "FetchRequest"):
    """将挖掘结果写入全局缓存。"""
    _safe_json_write(_FETCH_CACHE_DIR / f"{cache_key}.json", {
        "created_at": _time.time(),
        "cache_key": cache_key,
        "mode": req.mode,
        "query": req.query if req.mode == "sentence" else ", ".join(req.keywords),
        "fetch_model": req.fetch_model or "default",
        "fetch_model_scope": _FETCH_CACHE_SCOPE,
        "cache_schema_version": _FETCH_CACHE_SCHEMA_VERSION,
        "needs": needs,
    }, indent=2)

def _cleanup_fetch_cache():
    """清理过期的挖掘缓存文件。"""
    now = _time.time()
    for f in _FETCH_CACHE_DIR.glob("*.json"):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            if now - data.get("created_at", 0) > _FETCH_CACHE_EXPIRE:
                f.unlink(missing_ok=True)
        except Exception:
            pass


# 启动时清理过期 session 和挖掘缓存，并启动定期清理定时器
cleanup_expired_sessions()
_cleanup_fetch_cache()


def _schedule_session_cleanup():
    """每 10 分钟执行一次过期 session 和挖掘缓存清理。"""
    cleanup_expired_sessions()
    _cleanup_fetch_cache()
    _timer = threading.Timer(600, _schedule_session_cleanup)
    _timer.daemon = True
    _timer.start()


_cleanup_timer = threading.Timer(600, _schedule_session_cleanup)
_cleanup_timer.daemon = True
_cleanup_timer.start()


def _check_cli_available(sources: list[str]) -> tuple[bool, str]:
    """预检数据源 CLI 可用性（rdt-cli / st-cli），返回 (ok, err_msg)。"""
    import asyncio as _aio

    if "reddit" in sources:
        loop = _aio.new_event_loop()
        try:
            from .rdt_client import get_reddit_fetcher
            fetcher = get_reddit_fetcher()
            info = loop.run_until_complete(fetcher.rdt.check_available())
            if not (info.get("installed") and info.get("authenticated")):
                if not info.get("installed"):
                    return False, "rdt-cli 不可用，请在运行 Lumon 的本机安装"
                return False, "rdt-cli 未认证，请在本机完成登录"
        except Exception as e:
            return False, "rdt-cli 检测失败，请检查本机 CLI 状态"
        finally:
            loop.close()

    return True, ""


def _web_search_probe_message(label: str, status: str, model: str = "") -> str:
    suffix = f"（{model}）" if model else ""
    messages = {
        "unsupported": f"{label} 模型或中转站不支持 web_search 工具{suffix}",
        "responses_api_unavailable": f"{label} 中转站未提供 Responses API（/v1/responses）{suffix}",
        "authentication_failed": f"{label} WebSearch 认证失败，请检查 API Key{suffix}",
        "rate_limited": f"{label} WebSearch 请求频率受限，请稍后重试{suffix}",
        "timeout": f"{label} WebSearch 检测超时，中转站响应较慢，请重试{suffix}",
        "network_error": f"{label} WebSearch 网络连接失败，请检查网络后重试{suffix}",
        "upstream_error": f"{label} WebSearch 上游服务暂时异常，请稍后重试{suffix}",
        "empty_response": f"{label} WebSearch 未返回有效内容，请重试{suffix}",
        "request_failed": f"{label} WebSearch 检测请求失败，请重试或检查模型配置{suffix}",
    }
    return messages.get(status, f"{label} WebSearch 检测失败，请重试{suffix}")


def _check_web_search_available(ctx: SessionContext) -> tuple[bool, str]:
    """检测当前选择的 WebSearch 引擎是否已配置且工具可用。"""
    import os
    engine = ctx.web_search_engine
    if engine == "tavily":
        key = ctx._runtime_config.get("TAVILY_API_KEY") or os.getenv("TAVILY_API_KEY", "")
        if not key:
            return False, "Tavily WebSearch 不可用，请在设置中配置自己的 API Key"
        try:
            from .web_search import _get_tavily_client
            client = _get_tavily_client()
            r = client.search(query="test", search_depth="basic", max_results=1, include_answer=False)
            if r is None or r.get("results") is None:
                return False, "Tavily WebSearch 不可用，请检查自己的 API Key"
        except ValueError:
            return False, "Tavily WebSearch 不可用，请检查自己的 API Key"
        except Exception as e:
            return False, "Tavily WebSearch 检测失败，请检查本机网络和 API 配置"
    elif engine in ("gpt", "claude"):
        label = "GPT" if engine == "gpt" else "Claude"
        prefix = "GPT" if engine == "gpt" else "CLAUDE"
        cfg = ctx.get_config(prefix)
        if not cfg.get("api_key"):
            return False, f"{label} WebSearch 不可用，请在设置中完成模型配置"
        try:
            from openai import OpenAI
            from .web_search import _probe_web_search_support
            client = OpenAI(
                base_url=cfg["base_url"], api_key=cfg["api_key"],
                timeout=45.0, max_retries=0,
            )
            probe = _probe_web_search_support(client, cfg["model"], label, attempts=2)
            if not probe.ok:
                return False, _web_search_probe_message(label, probe.status, cfg["model"])
        except Exception:
            return False, f"{label} WebSearch 检测失败，请检查本机网络和模型配置"
    return True, ""


def _get_session(request: Request) -> SessionContext:
    """从请求 header 中提取 session_id 并获取对应的 SessionContext。"""
    sid = request.headers.get("x-session-id", "default")
    ctx = get_session(sid)
    set_thread_session(ctx)
    return ctx


def _normalize_need_dict(n: object) -> dict:
    """保证每条 need 含 posts 列表，避免不完整缓存导致前端崩溃。"""
    if not isinstance(n, dict):
        return {"need_title": "未命名需求", "need_description": "", "posts": []}
    posts = n.get("posts")
    if not isinstance(posts, list):
        posts = []
    out = dict(n)
    out["need_title"] = str(out.get("need_title") or "未命名需求")
    out["need_description"] = str(out.get("need_description") or "")
    out["need_title_en"] = str(out.get("need_title_en") or "")
    out["need_description_en"] = str(out.get("need_description_en") or "")
    out["posts"] = posts
    return out


def _normalize_needs_list(raw: object) -> list[dict]:
    if not isinstance(raw, list):
        return []
    return [_normalize_need_dict(x) for x in raw]

ENV_PATH = ROOT / ".env"


def _safe_path(base_dir: Path, filename: str) -> Path:
    """校验文件路径不超出 base_dir，防止 ../ 穿越攻击。"""
    base_resolved = base_dir.resolve()
    resolved = (base_resolved / filename).resolve()
    try:
        resolved.relative_to(base_resolved)
    except ValueError:
        raise HTTPException(status_code=400, detail="非法文件名")
    return resolved


router = APIRouter(prefix="/api")

def _normalize_fetch_strategy(value: Any) -> str:
    """Map current and legacy client values to execution depth, never to a model."""
    raw = str(value or "default").strip().lower()
    if raw == "deep" or raw.startswith(("review:", "full:")):
        return "deep"
    return "fast"


def _auto_discover_exploration_note() -> str:
    """给自主挖掘加入随机探索偏置，避免每次都落到同一批常见方向。"""
    notes = [
        "本轮优先探索冷门但软件可解的生活/家庭/个人管理场景，避免选择常见效率工具方向。",
        "本轮优先探索有强烈 workaround、迁移或付费阻力的垂直人群，不要集中在主流生产力赛道。",
        "本轮优先选择彼此差异很大的方向，覆盖不同用户角色、不同消费场景和不同 subreddit 圈层。",
        "本轮优先寻找非显而易见机会：小众职业、特殊家庭关系、跨语言/跨地域协作、线下流程数字化。",
        "本轮避开过度拥挤的 AI 写作、笔记、待办和通用聊天方向，寻找更具体的高摩擦用户旅程。",
    ]
    return random.choice(notes)


def _friendly_error(e: Exception) -> str:
    """Convert raw API exceptions to user-friendly Chinese messages."""
    msg = str(e)
    low = msg.lower()
    if "429" in msg or "rate" in low or "cooldown" in low or "cooling" in low:
        return "请求太频繁，请等 1-2 分钟再试"
    if "503" in msg or "no available" in low or "service unavailable" in low:
        return "模型额度暂时不足，请检查自己的账号余额"
    if "403" in msg or "no access" in low:
        return "模型访问被拒，请检查自己的账号权限"
    if "401" in msg or "unauthorized" in low:
        if "one_api" in low or "令牌" in low:
            return "中转站令牌暂时失效，请检查本地模型配置"
        return "API Key 无效，请检查本地模型配置"
    if "timeout" in low or "timed out" in low:
        return "模型响应超时，请再试一次"
    if "connection" in low:
        return "模型服务连接失败，请检查 Base URL、中转站状态和本机网络"
    if "网页内容" in msg or "非标准文本响应" in msg:
        return "模型接口返回了网页内容，请检查 Base URL 是否包含 /v1"
    if "stream" in low or "codex" in low:
        return "模型输出中断，请重试并检查模型服务状态"
    if "500" in msg or "server" in low or "internal" in low:
        return "模型服务异常，请稍后重试"
    return "模型调用失败，请检查本地模型配置后重试"


def _log_sse_error(tag: str, e: Exception, ctx: "SessionContext | None" = None):
    """统一 SSE 流异常日志：打印 tag、session_id、堆栈。"""
    import traceback as _tb
    sid = ctx.session_id if ctx else "?"
    print(f"[{tag}] ERROR session={sid}: {e}\n{_tb.format_exc()}")


# ============================================================
# Clustering: group posts into needs
# ============================================================

def _fix_unescaped_quotes(s: str) -> str:
    """修复 LLM 在 JSON 字符串值中输出的未转义 ASCII 双引号。

    例如 "名为"大都会"的" → "名为「大都会」的"
    策略：逐字符扫描，跟踪是否在字符串值内部，
    遇到字符串内部不该出现的裸引号时替换为中文书名号。
    """
    result = []
    i = 0
    in_string = False
    n = len(s)
    while i < n:
        ch = s[i]
        if ch == '\\' and in_string and i + 1 < n:
            result.append(ch)
            result.append(s[i + 1])
            i += 2
            continue
        if ch == '"':
            if not in_string:
                in_string = True
                result.append(ch)
            else:
                after = s[i + 1:i + 10].lstrip() if i + 1 < n else ""
                if after and after[0] in (',', '}', ']', ':'):
                    in_string = False
                    result.append(ch)
                elif i + 1 >= n:
                    result.append(ch)
                else:
                    result.append('「')
                    j = s.find('"', i + 1)
                    if j != -1 and j < i + 60:
                        result.append(s[i + 1:j])
                        result.append('」')
                        i = j + 1
                        continue
                    else:
                        pass
        else:
            result.append(ch)
        i += 1
    return ''.join(result)


def _parse_json_from_text(text: str):
    """Extract JSON from LLM response text, with truncation-aware repair."""
    if not text:
        return None
    text = text.strip()
    import re
    # 先剥离 <think>/<thinking> 标签，避免标签内的 [ ] { } 干扰 JSON 定位
    text = re.sub(r'<think(?:ing)?[\s\S]*?</think(?:ing)?>', '', text, flags=re.IGNORECASE).strip()
    # 处理未闭合的 <think> 标签（模型输出被截断时可能只有开头没有结尾）
    text = re.sub(r'<think(?:ing)?[^>]*>[\s\S]*$', '', text, flags=re.IGNORECASE).strip() if '<think' in text.lower() else text
    try:
        return json.loads(text)
    except Exception:
        pass
    m = re.search(r'```(?:json)?\s*\n?([\s\S]*?)\n?\s*```', text)
    if m:
        inner = m.group(1).strip()
        try:
            return json.loads(inner)
        except json.JSONDecodeError:
            fixed = _fix_unescaped_quotes(inner)
            try:
                return json.loads(fixed)
            except Exception:
                pass
            repaired = _repair_truncated_json(inner)
            if repaired is not None:
                return repaired
    first_bracket = text.find('[')
    last_bracket = text.rfind(']')
    if first_bracket != -1 and last_bracket > first_bracket:
        sub = text[first_bracket:last_bracket + 1]
        try:
            return json.loads(sub)
        except json.JSONDecodeError:
            fixed = _fix_unescaped_quotes(sub)
            try:
                return json.loads(fixed)
            except Exception:
                pass
    first_brace = text.find('{')
    last_brace = text.rfind('}')
    if first_brace != -1 and last_brace > first_brace:
        sub = text[first_brace:last_brace + 1]
        try:
            return json.loads(sub)
        except json.JSONDecodeError:
            fixed = _fix_unescaped_quotes(sub)
            try:
                return json.loads(fixed)
            except Exception:
                pass
    start = first_bracket if first_bracket != -1 else first_brace
    if start != -1:
        repaired = _repair_truncated_json(text[start:])
        if repaired is not None:
            return repaired
    return None


def _build_research_context(req: "FetchRequest") -> str:
    """Build extra context block from optional research parameters."""
    parts: list[str] = []
    period_labels = {"month": "过去1个月", "3months": "过去3个月", "6months": "过去6个月", "9months": "过去9个月"}
    parts.append(f"时间范围：{period_labels.get(req.time_period, '过去6个月')}")
    if req.product:
        parts.append(f"现有产品：{req.product}")
    if req.market:
        parts.append(f"目标市场：{req.market}")
    if req.demographics:
        parts.append(f"目标用户画像：{req.demographics}")
    if req.segment:
        parts.append(f"用户行为/情境细分：{req.segment}")
    if req.competitors:
        parts.append(f"已知竞品：{req.competitors}")
    if req.pain_points and req.pain_points != 10:
        parts.append(f"目标痛点数量：{req.pain_points}")
    if not parts:
        return ""
    return "## 研究参数\n" + "\n".join(f"- {p}" for p in parts)


def _plan_search(user_input: str, req: "FetchRequest | None" = None) -> dict | None:
    """Ask Claude to generate search queries and subreddits from user input.

    新版返回结构支持四分类搜索矩阵（向后兼容 search_queries 字段）。
    """
    research_context = _build_research_context(req) if req else ""
    prompt_text = SEARCH_PLANNING_PROMPT.format(
        user_input=user_input,
        research_context=research_context,
    )
    messages = [{"role": "user", "content": prompt_text}]
    try:
        response = call_llm(messages)
        result = _parse_json_from_text(response)
        if result and isinstance(result, dict):
            # 四分类合并为统一 search_queries（按优先级排序：痛点 > 方案 > 竞品 > 平台）
            if "problem_queries" in result and "search_queries" not in result:
                merged = []
                merged.extend(result.get("problem_queries", []))
                merged.extend(result.get("solution_queries", []))
                merged.extend(result.get("competitor_queries", []))
                merged.extend(result.get("platform_queries", []))
                result["search_queries"] = merged
            print(f"[SearchPlan] queries={len(result.get('search_queries', []))}, "
                  f"discovery={len(result.get('discovery_queries', []))}, "
                  f"subreddits={result.get('subreddits')}, "
                  f"competitors={result.get('known_competitors', [])}")
            return result
    except Exception as e:
        print(f"[SearchPlan] LLM call failed: {e}")
    return None


_SUBREDDIT_BLOCK_TERMS = (
    "hentai", "porn", "nsfw", "gonewild", "onlyfans", "xxx", "adult",
    "sex", "hookup", "r4r", "roleplay",
)

_SUBREDDIT_STOP_TERMS = {
    "the", "and", "for", "with", "from", "into", "that", "this", "your",
    "you", "are", "app", "apps", "tool", "tools", "tips", "help", "ask",
    "reddit", "community", "advice", "support", "general", "discussion",
}


def _subreddit_terms(text: str) -> set[str]:
    """把 topic/query/subreddit 名称压成可比较的轻量词集。"""
    raw = re.sub(r"([a-z])([A-Z])", r"\1 \2", str(text or ""))
    raw = raw.replace("_", " ").replace("-", " ").replace("/", " ")
    terms: set[str] = set()
    for term in re.findall(r"[a-zA-Z0-9]+", raw.lower()):
        if len(term) < 3 or term in _SUBREDDIT_STOP_TERMS:
            continue
        terms.add(term)
        if term.endswith("s") and len(term) > 4:
            terms.add(term[:-1])
    return terms


def _is_relevant_discovered_subreddit(
    sub_name: str,
    *,
    topic: str,
    search_queries: list[str],
    original_subreddits: list[str],
) -> tuple[bool, str]:
    """对 WebSearch 动态发现的 subreddit 做轻量守门，避免明显跑偏社区进入核心搜索。"""
    normalized = re.sub(r"[^a-z0-9]", "", str(sub_name or "").lower())
    if not normalized:
        return False, "empty"
    for blocked in _SUBREDDIT_BLOCK_TERMS:
        if blocked in normalized:
            return False, f"blocked:{blocked}"

    topic_terms = _subreddit_terms(topic)
    for q in search_queries[:16]:
        topic_terms.update(_subreddit_terms(q))
    for sub in original_subreddits[:20]:
        topic_terms.update(_subreddit_terms(sub))

    sub_terms = _subreddit_terms(sub_name)
    overlap = sub_terms & topic_terms
    substring_hits = {
        term for term in topic_terms
        if len(term) >= 4 and term in normalized
    }
    if overlap or substring_hits:
        reason_terms = sorted((overlap | substring_hits))[:4]
        return True, "match:" + ",".join(reason_terms)
    return False, "no_topic_overlap"


def _quick_relevance_check(posts: list[dict], topic: str) -> bool:
    """快速检查一批搜索结果的相关性：取 top 5 标题，>=3 跑题则丢弃整批。

    Returns: True = 保留, False = 丢弃整批
    """
    if len(posts) < 3:
        return True

    top5 = sorted(posts, key=lambda p: p.get("score", 0), reverse=True)[:5]
    titles_text = "\n".join(f"{i+1}. {p.get('title', '(无标题)')}" for i, p in enumerate(top5))

    prompt = QUICK_RELEVANCE_PROMPT.format(topic=topic, titles_text=titles_text)
    try:
        resp = call_llm([{"role": "user", "content": prompt}])
        result = _parse_json_from_text(resp)
        if result and isinstance(result, dict):
            verdict = result.get("verdict", "keep")
            off_topic = result.get("off_topic_count", 0)
            reason = result.get("reason", "")
            print(f"[QuickCheck] off_topic={off_topic}, verdict={verdict}: {reason}")
            return verdict != "discard"
    except Exception as e:
        print(f"[QuickCheck] LLM error, keeping batch: {e}")
    return True


def _batch_relevance_check(posts: list[dict], topic: str) -> list[dict]:
    """Check batches of posts for relevance. Per-post granularity with content."""
    if len(posts) <= 3:
        return posts

    kept: list[dict] = []
    batch_size = 8

    for i in range(0, len(posts), batch_size):
        batch = posts[i:i + batch_size]
        titles = [
            {"idx": j, "title": p["title"], "snippet": (p.get("content", "") or "")[:150]}
            for j, p in enumerate(batch)
        ]
        prompt = BATCH_RELEVANCE_PROMPT.format(
            topic=topic,
            titles_json=json.dumps(titles, ensure_ascii=False),
        )
        try:
            resp = call_llm([{"role": "user", "content": prompt}])
            result = _parse_json_from_text(resp)
            if result and isinstance(result, dict):
                keep_indices = set(result.get("keep_indices", []))
                discard_indices = set(result.get("discard_indices", []))
                if keep_indices:
                    for j, p in enumerate(batch):
                        if j in keep_indices:
                            kept.append(p)
                    discarded = len(discard_indices)
                    if discarded:
                        print(f"[BatchCheck] batch {i//batch_size+1}: kept {len(keep_indices)}, discarded {discarded}: {result.get('reason', '')}")
                    continue
        except Exception as e:
            print(f"[BatchCheck] LLM error, keeping batch: {e}")

        kept.extend(batch)

    print(f"[BatchCheck] {len(posts)} → {len(kept)} posts after relevance check")
    return kept


def _filter_posts(posts: list[dict], topic: str = "") -> list[dict]:
    """Ask Claude to filter out posts with no product opportunity."""
    if len(posts) <= 3:
        return posts

    posts_summary = []
    for i, p in enumerate(posts):
        posts_summary.append({
            "idx": i,
            "title": p["title"],
            "content": (p.get("content", "") or "")[:600],
            "score": p.get("score", 0),
            "num_comments": p.get("num_comments", 0),
            "top_comments": [c[:200] for c in p.get("comments", [])[:5]],
        })

    prompt_text = POST_FILTER_PROMPT.format(
        topic=topic or "（未指定）",
        posts_json=json.dumps(posts_summary, ensure_ascii=False, indent=2),
    )
    messages = [{"role": "user", "content": prompt_text}]

    try:
        response = call_llm(messages, max_tokens=4096)
        result = _parse_json_from_text(response)
        if result and isinstance(result, dict):
            keep = result.get("keep_indices", [])
            removed = result.get("removed_reasons", {})
            if keep:
                filtered = [posts[i] for i in keep if 0 <= i < len(posts)]
                print(f"[Filter] {len(posts)} → {len(filtered)} posts. "
                      f"Removed {len(removed)}: {list(removed.values())[:3]}")
                return filtered if filtered else posts
    except Exception as e:
        print(f"[Filter] LLM call failed: {e}")
    return posts


def _comment_body_for_prompt(comment: Any, limit: int = 200) -> str:
    """把 rdt 字符串或 dict 评论统一压成 prompt 可用的短文本。"""
    if isinstance(comment, dict):
        body = str(comment.get("body") or comment.get("text") or "")
        score = comment.get("score")
        prefix = f"[score {score}] " if score not in (None, "") else ""
        return (prefix + body)[:limit]
    return str(comment or "")[:limit]


_EVIDENCE_SIGNAL_TERMS: dict[str, tuple[str, ...]] = {
    "pain": (
        "tired of", "sick of", "fed up", "frustrated", "annoying", "hate",
        "struggle", "struggling", "pain", "overwhelmed", "manual", "tedious",
        "can't", "cannot", "takes hours", "waste time",
    ),
    "workaround": (
        "workaround", "hack", "spreadsheet", "google sheet", "excel", "notion",
        "i built", "i made", "i ended up", "my workflow", "copy paste", "manually",
    ),
    "switching": (
        "alternative", "switched from", "moved from", "replacement", "vs ",
        "versus", "instead of", "looking for another",
    ),
    "payment": (
        "i would pay", "i'd pay", "worth paying", "lifetime", "subscription",
        "too expensive", "pricing", "pricey", "premium", "paid plan",
    ),
    "trust": (
        "privacy", "private", "trust", "security", "permission", "bank access",
        "connect my bank", "data",
    ),
    "software": (
        "app", "tool", "software", "ai", "automate", "automatic", "automation",
        "scanner", "dashboard", "workflow", "notification", "sync",
    ),
}

_EVIDENCE_SIGNAL_LABELS: dict[str, str] = {
    "pain": "痛点",
    "workaround": "临时方案",
    "switching": "替代/迁移",
    "payment": "付费/价格",
    "trust": "信任阻力",
    "software": "软件可解",
}


def _comment_text_and_score(comment: Any) -> tuple[str, int]:
    """返回评论正文和分数；兼容字符串评论与 dict 评论。"""
    if isinstance(comment, dict):
        text = str(comment.get("body") or comment.get("text") or "")
        try:
            score = int(comment.get("score") or 0)
        except (TypeError, ValueError):
            score = 0
        return text, score
    text = str(comment or "")
    score = 0
    m = re.match(r"^\[score\s+(-?\d+)\]\s+", text, flags=re.I)
    if m:
        try:
            score = int(m.group(1))
        except ValueError:
            score = 0
        text = text[m.end():]
    return text, score


def _source_subreddit_from_post(post: dict) -> str:
    source = str(post.get("source") or "")
    if source.startswith("reddit/"):
        return source.split("/", 1)[1].strip()
    return ""


def _infer_evidence_signals(text: str, post: dict | None = None) -> list[str]:
    """从真实文本中识别证据信号；无命中时回退到帖子已有 AI 信号。"""
    lower = text.lower()
    signals = [
        signal
        for signal, terms in _EVIDENCE_SIGNAL_TERMS.items()
        if any(term in lower for term in terms)
    ]
    if signals:
        return signals[:4]
    if post:
        raw = post.get("ai_signal_types") or []
        if isinstance(raw, list):
            normalized: list[str] = []
            for item in raw:
                s = str(item).strip().lower()
                if s in _EVIDENCE_SIGNAL_LABELS and s not in normalized:
                    normalized.append(s)
            if normalized:
                return normalized[:3]
    return ["pain"]


def _signal_hit_count(text: str) -> int:
    lower = text.lower()
    return sum(
        1
        for terms in _EVIDENCE_SIGNAL_TERMS.values()
        for term in terms
        if term in lower
    )


def _clean_evidence_text(text: str, limit: int = 360) -> str:
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    text = re.sub(r"^\[reply-L\d+\]\s*", "", text)
    if len(text) > limit:
        return text[: limit - 1].rstrip() + "…"
    return text


def _post_platform(post: dict) -> str:
    source = str(post.get("source") or "").lower()
    if source.startswith("reddit"):
        return "reddit"
    if source.startswith("hackernews") or source.startswith("hn"):
        return "hackernews"
    return source or "unknown"


def _find_verified_snippet_source(post: dict, snippet: str) -> tuple[str, str, int, dict | None] | None:
    """只有当 snippet 能在正文或评论中找到时，才把它当作原文证据。"""
    text = _clean_evidence_text(snippet, limit=220)
    if len(text) < 24:
        return None
    needle = text.lower()
    content = str(post.get("content") or "")
    if needle and needle in content.lower():
        return text, "post", int(post.get("score") or 0), None
    comments = post.get("comments") or []
    meta = post.get("_comment_meta") or []
    for idx, comment in enumerate(comments[:12]):
        body, score = _comment_text_and_score(comment)
        if needle and needle in body.lower():
            c_meta = meta[idx] if isinstance(meta, list) and idx < len(meta) and isinstance(meta[idx], dict) else None
            return text, "comment", score, c_meta
    return None


def _best_comment_evidence(post: dict) -> tuple[str, int, int, dict | None] | None:
    """选择最适合做证据的真实评论，优先高信号和高赞。"""
    comments = post.get("comments") or []
    meta = post.get("_comment_meta") or []
    candidates: list[tuple[float, int, str, int, dict | None]] = []
    for idx, comment in enumerate(comments[:18]):
        body, score = _comment_text_and_score(comment)
        text = _clean_evidence_text(body)
        if len(text) < 40:
            continue
        signal_hits = _signal_hit_count(text)
        if signal_hits == 0 and idx > 5:
            continue
        c_meta = meta[idx] if isinstance(meta, list) and idx < len(meta) and isinstance(meta[idx], dict) else None
        rank = signal_hits * 5 + max(score, 0) * 0.2 + max(0, 8 - idx) * 0.15
        candidates.append((rank, idx, text, score, c_meta))
    if not candidates:
        return None
    candidates.sort(key=lambda item: item[0], reverse=True)
    _, idx, text, score, c_meta = candidates[0]
    return text, score, idx, c_meta


def _build_post_evidence(post: dict, need_idx: int, post_idx: int) -> dict | None:
    """从单个帖子中构造一条可追溯证据，不使用模型摘要伪装成原文。"""
    verified: tuple[str, str, int, dict | None] | None = None
    snippets = post.get("ai_evidence_snippets") or []
    if isinstance(snippets, list):
        for snippet in snippets[:3]:
            verified = _find_verified_snippet_source(post, str(snippet))
            if verified:
                break

    comment_idx: int | None = None
    if verified:
        text, source_type, comment_score, comment_meta = verified
    else:
        best_comment = _best_comment_evidence(post)
        if best_comment:
            text, comment_score, comment_idx, comment_meta = best_comment
            source_type = "comment"
        else:
            content = _clean_evidence_text(post.get("content") or "", limit=320)
            if len(content) < 40:
                return None
            text = content
            source_type = "post"
            comment_score = 0
            comment_meta = None

    post_id = str(post.get("_post_id") or "")
    source_url = str(post.get("url") or post.get("hn_url") or "")
    if comment_meta:
        source_url = str(comment_meta.get("permalink") or source_url)
    raw_id = f"{post_id}|{source_url}|{source_type}|{comment_idx}|{text[:120]}"
    evidence_id = "ev_" + hashlib.sha1(raw_id.encode("utf-8", errors="ignore"), usedforsecurity=False).hexdigest()[:12]
    signals = _infer_evidence_signals(text, post)
    evidence = {
        "evidence_id": evidence_id,
        "source_id": evidence_id,
        "text": text,
        "source_url": source_url,
        "post_id": post_id,
        "comment_id": str((comment_meta or {}).get("id") or "") if comment_meta else "",
        "post_score": int(post.get("score") or 0),
        "comment_score": int(comment_score or 0),
        "subreddit": _source_subreddit_from_post(post),
        "platform": _post_platform(post),
        "source_type": source_type,
        "source_title": post.get("title", ""),
        "signal_type": signals[0],
        "signal_label": _EVIDENCE_SIGNAL_LABELS.get(signals[0], signals[0]),
        "supports": signals,
        "context": str(post.get("ai_evidence_summary") or post.get("ai_read_reason") or "")[:220],
        "quality_score": post.get("ai_evidence_score") or post.get("opportunity_score"),
        "verbatim": True,
    }
    if post.get("_discovery_source") == "evidence_probe":
        evidence["discovery_source"] = "evidence_probe"
        evidence["probe_id"] = post.get("_evidence_probe_id", "")
        evidence["probe_query"] = post.get("_evidence_probe_query", "")
        evidence["probe_reason"] = post.get("_evidence_probe_reason", "")
    return evidence


def _attach_evidence_bundles(needs: list[dict]) -> list[dict]:
    """给每个需求组绑定 Evidence Bundle，供卡片、报告和后续反幻觉校验使用。"""
    for need_idx, need in enumerate(needs):
        posts = need.get("posts") or []
        ranked_posts = sorted(
            enumerate(posts),
            key=lambda item: (
                float(item[1].get("ai_evidence_score") or 0),
                float(item[1].get("opportunity_score") or 0),
                float(item[1].get("comment_read_score") or 0),
                int(item[1].get("num_comments") or 0),
                int(item[1].get("score") or 0),
            ),
            reverse=True,
        )
        bundle: list[dict] = []
        seen_texts: set[str] = set()
        for post_idx, post in ranked_posts[:10]:
            evidence = _build_post_evidence(post, need_idx, post_idx)
            if not evidence:
                continue
            text_key = evidence["text"][:120].lower()
            if text_key in seen_texts:
                continue
            seen_texts.add(text_key)
            bundle.append(evidence)
            post.setdefault("evidence_ids", [])
            if evidence["evidence_id"] not in post["evidence_ids"]:
                post["evidence_ids"].append(evidence["evidence_id"])
            if len(bundle) >= 8:
                break

        need["evidence"] = bundle
        need["evidence_ids"] = [item["evidence_id"] for item in bundle]
        existing_source_ids = [str(s) for s in (need.get("source_ids") or []) if str(s).strip()]
        need["source_ids"] = list(dict.fromkeys(existing_source_ids + [item["source_id"] for item in bundle if item.get("source_id")]))
        if bundle:
            labels = []
            for ev in bundle:
                label = ev.get("signal_label") or ev.get("signal_type")
                if label and label not in labels:
                    labels.append(label)
            need["evidence_summary"] = f"{len(bundle)}条可追溯证据" + (f"，覆盖{'、'.join(labels[:3])}" if labels else "")
        else:
            need["evidence_summary"] = "暂无可追溯证据，后续报告需谨慎推断。"
    return needs


_PROBE_STOPWORDS = {
    "the", "and", "for", "with", "without", "from", "into", "about", "that",
    "this", "there", "their", "your", "mine", "ours", "what", "when", "where",
    "which", "would", "could", "should", "reddit", "best", "app", "apps",
    "tool", "tools", "software", "solution", "solutions", "using", "use",
    "need", "needs", "want", "wants", "looking", "recommendation",
}

_PROBE_COMPETITOR_STOPWORDS = {
    "Reddit", "Google", "Apple", "Microsoft", "Android", "Iphone", "Phone",
    "Internet", "English", "American", "European", "Monday", "Tuesday",
    "Wednesday", "Thursday", "Friday", "Saturday", "Sunday", "January",
    "February", "March", "April", "June", "July", "August", "September",
    "October", "November", "December", "AI", "App", "Apps", "SaaS",
}

_PROBE_PATTERNS: dict[str, tuple[str, ...]] = {
    "payment": ("too expensive", "subscription", "pricing", "pricey", "paid plan", "lifetime"),
    "trust": ("privacy", "security", "trust", "permission", "bank access", "data"),
    "workaround": ("spreadsheet", "google sheet", "excel", "manual", "manually", "workaround", "copy paste"),
    "switching": ("alternative", "replacement", "switched from", "moved from", "instead of"),
    "pain": ("frustrated", "annoying", "hate", "struggle", "tedious", "overwhelmed"),
}

_PROBE_SIGNAL_LABELS = {
    "payment": "价格/订阅阻力",
    "trust": "隐私/信任阻力",
    "workaround": "临时方案",
    "switching": "替代/迁移",
    "pain": "高频痛点",
    "competitor": "竞品/工具线索",
}


def _probe_clean_words(text: str, limit: int = 5) -> list[str]:
    words = [
        w.lower()
        for w in re.findall(r"[A-Za-z][A-Za-z0-9+.-]{1,}", str(text or ""))
        if w.lower() not in _PROBE_STOPWORDS and len(w) > 2
    ]
    deduped: list[str] = []
    for word in words:
        if word not in deduped:
            deduped.append(word)
        if len(deduped) >= limit:
            break
    return deduped


def _probe_topic_anchor(topic: str, fallback_queries: list[str]) -> str:
    """从用户输入或初始搜索词里取英文主题锚点，避免二轮 query 过长。"""
    topic_words = _probe_clean_words(topic, limit=4)
    if len(topic_words) >= 2:
        return " ".join(topic_words)
    for query in fallback_queries:
        query_words = _probe_clean_words(query, limit=4)
        if len(query_words) >= 2:
            return " ".join(query_words)
    return "productivity workflow"


def _post_probe_text(post: dict, comment_limit: int = 8) -> str:
    parts = [
        str(post.get("title") or ""),
        str(post.get("content") or ""),
        str(post.get("ai_evidence_summary") or ""),
        " ".join(str(s) for s in (post.get("ai_evidence_snippets") or [])[:3]),
    ]
    for comment in (post.get("comments") or [])[:comment_limit]:
        body, _ = _comment_text_and_score(comment)
        parts.append(body)
    return "\n".join(parts)


def _extract_probe_competitors(posts: list[dict], known_competitors: list[str]) -> list[tuple[str, float]]:
    """从已有证据文本里找竞品/工具名；只做候选探针，不当作事实输出。"""
    counts: Counter[str] = Counter()
    known = [str(c).strip() for c in known_competitors if str(c).strip()]
    for post in posts[:28]:
        text = _post_probe_text(post, comment_limit=5)
        lower = text.lower()
        for name in known:
            if len(name) >= 3 and name.lower() in lower:
                counts[name] += 3
        for match in re.finditer(r"\b[A-Z][A-Za-z0-9+.-]{2,}(?:\s+[A-Z][A-Za-z0-9+.-]{2,}){0,2}\b", text):
            name = match.group(0).strip()
            if name in _PROBE_COMPETITOR_STOPWORDS or len(name) > 32:
                continue
            if any(ch.isdigit() for ch in name) and len(name) < 5:
                continue
            counts[name] += 1
    ranked = [
        (name, float(score))
        for name, score in counts.most_common()
        if score >= 2 and name not in _PROBE_COMPETITOR_STOPWORDS
    ]
    return ranked[:4]


def _rank_probe_subreddits(posts: list[dict], fallback_subreddits: list[str], max_count: int = 4) -> list[str]:
    counts: Counter[str] = Counter()
    for post in posts[:40]:
        sub = _source_subreddit_from_post(post)
        if sub:
            counts[sub] += max(1, int(float(post.get("opportunity_score") or 1)))
    ranked = [sub for sub, _ in counts.most_common()]
    for sub in fallback_subreddits:
        clean = str(sub).strip().lstrip("r/")
        if clean and clean not in ranked:
            ranked.append(clean)
        if len(ranked) >= max_count:
            break
    return ranked[:max_count]


def _build_evidence_search_probes(
    posts: list[dict],
    topic: str,
    search_queries: list[str],
    known_competitors: list[str],
    subreddits: list[str],
    max_probes: int = 3,
) -> list[dict]:
    """基于第一轮真实帖子/评论生成小预算 rdt 补搜探针。"""
    if not posts:
        return []
    anchor = _probe_topic_anchor(topic, search_queries)
    ranked_posts = sorted(
        posts,
        key=lambda p: (
            float(p.get("ai_evidence_score") or 0),
            float(p.get("opportunity_score") or 0),
            float(p.get("comment_read_score") or 0),
            int(p.get("num_comments") or 0),
        ),
        reverse=True,
    )[:28]
    corpus = "\n".join(_post_probe_text(post, comment_limit=8).lower() for post in ranked_posts)
    probe_subs = _rank_probe_subreddits(ranked_posts, subreddits, max_count=4)
    candidates: list[dict] = []

    competitors = _extract_probe_competitors(ranked_posts, known_competitors)
    for name, score in competitors[:2]:
        candidates.append({
            "probe_id": "probe_competitor_" + hashlib.sha1(name.lower().encode("utf-8"), usedforsecurity=False).hexdigest()[:8],
            "query": f"{name} alternative",
            "signal_type": "competitor",
            "reason": f"第一轮证据中出现 {name}，补搜验证竞品不满或替代需求。",
            "subreddits": probe_subs[:2],
            "priority": 7.5 + score,
        })

    pattern_queries = {
        "payment": f"{anchor} too expensive",
        "trust": f"{anchor} privacy",
        "workaround": f"{anchor} spreadsheet workaround",
        "switching": f"{anchor} alternative",
        "pain": f"{anchor} frustrated",
    }
    for signal_type, phrases in _PROBE_PATTERNS.items():
        hits = sum(corpus.count(phrase) for phrase in phrases)
        if hits <= 0:
            continue
        candidates.append({
            "probe_id": f"probe_{signal_type}",
            "query": pattern_queries[signal_type],
            "signal_type": signal_type,
            "reason": f"第一轮评论/证据出现{_PROBE_SIGNAL_LABELS[signal_type]}，补搜验证是否跨帖子共鸣。",
            "subreddits": probe_subs[:2],
            "priority": 5.0 + hits,
        })

    if not candidates:
        for q in search_queries[:2]:
            words = _probe_clean_words(q, limit=4)
            if len(words) >= 2:
                query = " ".join(words + ["alternative"])
                candidates.append({
                    "probe_id": "probe_open_" + hashlib.sha1(query.encode("utf-8"), usedforsecurity=False).hexdigest()[:8],
                    "query": query,
                    "signal_type": "switching",
                    "reason": "第一轮证据不足以形成明确追问，使用替代方案角度做一次开放验证。",
                    "subreddits": probe_subs[:2],
                    "priority": 3.0,
                })

    deduped: list[dict] = []
    seen_queries: set[str] = set()
    for item in sorted(candidates, key=lambda c: float(c.get("priority") or 0), reverse=True):
        query = re.sub(r"\s+", " ", str(item.get("query") or "")).strip()
        if len(query.split()) < 2 or query.lower() in seen_queries:
            continue
        seen_queries.add(query.lower())
        item["query"] = query[:90]
        item["label"] = _PROBE_SIGNAL_LABELS.get(item.get("signal_type"), "二轮补搜")
        deduped.append(item)
        if len(deduped) >= max_probes:
            break
    return deduped


def _post_identity_key(post: dict) -> str:
    return str(post.get("_post_id") or post.get("url") or post.get("title") or "").lower().strip()


async def _run_evidence_probe_search(
    fetcher,
    probes: list[dict],
    existing_posts: list[dict],
    *,
    time_filter: str,
    cutoff_ts: float,
    req_mode: str,
    max_searches: int = 6,
    limit_per_search: int = 5,
    max_extra_posts: int = 12,
    read_limit: int = 2,
) -> tuple[list[dict], list[dict]]:
    """执行小预算二轮 rdt search；返回新增帖子和探针贡献统计。"""
    if not probes:
        return [], []
    existing_keys = {_post_identity_key(post) for post in existing_posts if _post_identity_key(post)}
    added: list[dict] = []
    stats: list[dict] = []
    search_count = 0

    for probe in probes:
        if search_count >= max_searches or len(added) >= max_extra_posts:
            break
        query = str(probe.get("query") or "").strip()
        if not query:
            continue
        sub_targets = [str(s).strip().lstrip("r/") for s in (probe.get("subreddits") or []) if str(s).strip()]
        if not sub_targets:
            sub_targets = [""]
        probe_added = 0
        probe_seen = 0
        for sub in sub_targets[:2]:
            if search_count >= max_searches or len(added) >= max_extra_posts:
                break
            try:
                batch = await fetcher.search(
                    query=query,
                    subreddit=sub,
                    sort="relevance",
                    time_filter=time_filter,
                    limit=limit_per_search,
                )
            except Exception as e:
                print(f"[EvidenceProbe] search failed q={query[:40]} sub={sub}: {e}")
                batch = []
            search_count += 1
            annotate_posts_with_opportunity(batch)
            for post in batch:
                probe_seen += 1
                key = _post_identity_key(post)
                if key and key in existing_keys:
                    continue
                created = float(post.get("created_utc") or 0)
                if created and created < cutoff_ts:
                    continue
                if not post.get("passes_heat_gate"):
                    continue
                if req_mode != "open" and not hard_filter(post):
                    continue
                if key:
                    existing_keys.add(key)
                post["_discovery_source"] = "evidence_probe"
                post["_evidence_probe_id"] = probe.get("probe_id")
                post["_evidence_probe_query"] = query
                post["_evidence_probe_reason"] = probe.get("reason", "")
                post["_evidence_probe_signal"] = probe.get("signal_type", "")
                post["_evidence_probe_label"] = probe.get("label", "二轮补搜")
                added.append(post)
                probe_added += 1
                if len(added) >= max_extra_posts:
                    break
        stats.append({
            "probe_id": probe.get("probe_id"),
            "query": query,
            "label": probe.get("label", "二轮补搜"),
            "reason": probe.get("reason", ""),
            "seen_posts": probe_seen,
            "added_posts": probe_added,
            "subreddits": sub_targets[:2],
        })

    if added and read_limit > 0:
        enrich_targets = sorted(
            [p for p in added if p.get("_post_id") and len(p.get("comments") or []) < 3],
            key=lambda p: (
                float(p.get("comment_read_score") or 0),
                float(p.get("opportunity_score") or 0),
                int(p.get("num_comments") or 0),
            ),
            reverse=True,
        )[:read_limit]
        for post in enrich_targets:
            try:
                detail = await fetcher.read_post(post["_post_id"])
            except Exception as e:
                print(f"[EvidenceProbe] read_post failed {post.get('_post_id')}: {e}")
                detail = None
            if detail and detail.get("title"):
                probe_meta = {k: v for k, v in post.items() if k.startswith("_evidence_probe") or k == "_discovery_source"}
                detail.update(probe_meta)
                post.update(detail)

    annotate_posts_with_opportunity(added)
    added.sort(
        key=lambda p: (
            float(p.get("opportunity_score") or 0),
            float(p.get("comment_read_score") or 0),
            int(p.get("score") or 0),
        ),
        reverse=True,
    )
    return added[:max_extra_posts], stats


def _attach_second_round_metadata(needs: list[dict], probe_stats: list[dict]) -> list[dict]:
    """把二轮补搜对需求组的贡献保存在结构化字段里，不增加卡片默认信息密度。"""
    stats_by_id = {str(s.get("probe_id")): s for s in probe_stats if s.get("probe_id")}
    for need in needs:
        posts = need.get("posts") or []
        probe_counts: Counter[str] = Counter(
            str(p.get("_evidence_probe_id"))
            for p in posts
            if p.get("_discovery_source") == "evidence_probe" and p.get("_evidence_probe_id")
        )
        if not probe_counts:
            continue
        ids = [probe_id for probe_id, _ in probe_counts.most_common()]
        summaries = []
        for probe_id in ids[:3]:
            stat = stats_by_id.get(probe_id, {})
            label = stat.get("label") or "二轮补搜"
            query = stat.get("query") or ""
            count = probe_counts[probe_id]
            summaries.append(f"{label}「{query}」贡献{count}帖")
        need["second_round_probe_ids"] = ids
        need["second_round_post_count"] = sum(probe_counts.values())
        need["second_round_summary"] = "；".join(summaries)
    return needs


def _post_item_for_model(post: dict, idx: int, content_limit: int = 320, comment_limit: int = 0) -> dict[str, Any]:
    item = {
        "idx": idx,
        "title": post.get("title", ""),
        "source": post.get("source", ""),
        "score": post.get("score", 0),
        "num_comments": post.get("num_comments", 0),
        "opportunity_score": post.get("opportunity_score"),
        "comment_read_score": post.get("comment_read_score"),
        "top_signals": post.get("top_signals", []),
        "content": (post.get("content", "") or "")[:content_limit],
    }
    if post.get("ai_evidence_summary"):
        item["ai_evidence_summary"] = post.get("ai_evidence_summary")
        item["ai_signal_types"] = post.get("ai_signal_types", [])
    if comment_limit > 0 and post.get("comments"):
        item["top_comments"] = [
            _comment_body_for_prompt(c, 220)
            for c in (post.get("comments") or [])[:comment_limit]
        ]
    return item


def _model_json_list(parsed: Any, *keys: str) -> list | None:
    """兼容模型把列表包在不同字段名里的情况。"""
    if isinstance(parsed, list):
        return parsed
    if not isinstance(parsed, dict):
        return None
    for key in keys:
        value = parsed.get(key)
        if isinstance(value, list):
            return value
    for value in parsed.values():
        if isinstance(value, list) and value and all(isinstance(item, dict) for item in value[:3]):
            return value
    return None


def _item_index(item: dict) -> int | None:
    for key in ("idx", "index", "post_idx", "post_index", "need_idx", "need_index"):
        try:
            value = item.get(key)
            if value is not None:
                return int(value)
        except (TypeError, ValueError):
            continue
    return None


def _clean_ai_title(title: Any, fallback: str = "未命名需求", max_len: int = 40) -> str:
    text = _re_tag.sub(r"\s+", " ", str(title or "").strip())
    if not text or text == "未命名需求":
        text = fallback
    if len(text) <= max_len:
        return text
    cut = text[: max_len - 1].rstrip(" -_/，、。：:；;（(")
    return (cut or text[: max_len - 1]) + "…"


def _prioritize_comment_reads_with_model(
    posts: list[dict],
    topic: str,
    model_label: str,
    target_reads: int = 18,
) -> list[dict]:
    """让强模型在 WebSearch/关键词采集之后选择最值得深读评论的帖子。"""
    if len(posts) < 8:
        return posts
    ctx = get_thread_session()
    sample_limit = min(len(posts), 45)
    items = [
        _post_item_for_model(post, idx, content_limit=260, comment_limit=0)
        for idx, post in enumerate(posts[:sample_limit])
    ]
    prompt = f"""你是产品机会研究员。你不会做 WebSearch，也不会生成新搜索词。
请只基于以下已经采集到的候选帖子，选择最值得继续读取深层评论的帖子，目标是减少 rdt read 请求并提高证据质量。

研究方向：{topic or '开放式自主挖掘'}

优先选择：
- 痛点具体、有场景和后果
- 有 workaround、竞品迁移、价格/付费、信任阻力、软件可解信号
- 评论数或点赞能体现共鸣
- 小热度但明显有产品机会的帖子

降权：
- 泛情绪、新闻、娱乐、纯讨论
- 高热但无法软件化
- 与研究方向关系弱

输出纯 JSON：
{{
  "selected": [
    {{"idx": 0, "priority": 1-5, "reason": "为什么值得读评论", "signals": ["pain","workaround"]}}
  ]
}}

最多选择 {target_reads} 个 idx。候选帖子：
{json.dumps(items, ensure_ascii=False, indent=2)}
"""
    try:
        response = call_llm([
            {"role": "system", "content": "直接输出纯 JSON 对象，不要添加代码块标记或多余文字。"},
            {"role": "user", "content": prompt},
        ], max_tokens=2048)
        parsed = _parse_json_from_text(response)
        selected = _model_json_list(parsed, "selected", "selected_posts", "recommendations", "posts", "items", "ranked", "ranking")
        if not isinstance(selected, list):
            return posts
        selected_map: dict[int, dict] = {}
        for item in selected[:target_reads]:
            if not isinstance(item, dict):
                continue
            idx = _item_index(item)
            if idx is None:
                continue
            if 0 <= idx < sample_limit:
                selected_map[idx] = item

        if len(selected_map) < 3:
            return posts

        for idx, post in enumerate(posts):
            item = selected_map.get(idx)
            if item:
                try:
                    post["ai_read_priority"] = max(1.0, min(5.0, float(item.get("priority") or 0)))
                except (TypeError, ValueError):
                    post["ai_read_priority"] = 3.0
                post["ai_read_reason"] = str(item.get("reason") or "")[:180]
                signals = item.get("signals")
                if isinstance(signals, list):
                    post["ai_read_signals"] = [str(s)[:32] for s in signals[:5]]
                post["ai_read_model"] = model_label
            else:
                post["ai_read_priority"] = 0

        posts.sort(
            key=lambda p: (
                float(p.get("ai_read_priority") or 0),
                float(p.get("comment_read_score") or 0),
                float(p.get("opportunity_score") or 0),
                int(p.get("num_comments") or 0),
                int(p.get("score") or 0),
            ),
            reverse=True,
        )
        if ctx:
            ctx.fetch_emit(f"{model_label} 已筛选深读评论优先级：{len(selected_map)} 个高价值帖子", 61)
        return posts
    except Exception as e:
        print(f"[AIReadPriority] {model_label} failed: {e}")
        return posts


def _extract_evidence_with_model(posts: list[dict], topic: str, model_label: str) -> list[dict]:
    """把评论充实后的帖子压缩成结构化证据，供后续聚类和二审使用。"""
    if not posts:
        return posts
    ctx = get_thread_session()
    working = posts[: min(len(posts), 28)]
    items = [
        _post_item_for_model(post, idx, content_limit=420, comment_limit=5)
        for idx, post in enumerate(working)
    ]
    prompt = f"""你是需求证据分析员。你不会搜索网页，也不会补充外部事实。
请只基于下面已经读取到的帖子和评论，提取能支撑产品机会判断的证据。

研究方向：{topic or '开放式自主挖掘'}

每条证据要判断：
- pain：明确痛点
- workaround：用户绕路/自建流程
- switching：替代方案、竞品迁移或竞品不满
- payment：价格、付费、订阅、lifetime 等
- trust：隐私、权限、数据、可信度
- software：App/SaaS/AI 可解决性

输出纯 JSON：
{{
  "evidence": [
    {{
      "idx": 0,
      "quality_score": 1-5,
      "summary": "一句话概括这条帖子的高价值证据信号",
      "signal_types": ["pain","workaround"],
      "snippets": ["不超过18个词的原文短摘录"],
      "solvability": "软件/AI 可解决性判断"
    }}
  ]
}}

只输出有价值证据，最多 {len(working)} 条。帖子：
{json.dumps(items, ensure_ascii=False, indent=2)}
"""
    try:
        response = call_llm([
            {"role": "system", "content": "直接输出纯 JSON 对象，不要添加代码块标记或多余文字。"},
            {"role": "user", "content": prompt},
        ], max_tokens=4096)
        parsed = _parse_json_from_text(response)
        evidence = _model_json_list(parsed, "evidence", "evidences", "items", "posts", "signals")
        if not isinstance(evidence, list):
            return posts
        by_idx: dict[int, dict] = {}
        for item in evidence:
            if not isinstance(item, dict):
                continue
            idx = _item_index(item)
            if idx is None:
                continue
            if 0 <= idx < len(working):
                by_idx[idx] = item

        for idx, post in enumerate(working):
            item = by_idx.get(idx)
            if not item:
                continue
            try:
                post["ai_evidence_score"] = max(1.0, min(5.0, round(float(item.get("quality_score") or 0), 2)))
            except (TypeError, ValueError):
                post["ai_evidence_score"] = 1.0
            post["ai_evidence_summary"] = str(item.get("summary") or "")[:260]
            post["ai_evidence_solvability"] = str(item.get("solvability") or "")[:160]
            signal_types = item.get("signal_types")
            if isinstance(signal_types, list):
                post["ai_signal_types"] = [str(s)[:32] for s in signal_types[:6]]
            snippets = item.get("snippets")
            if isinstance(snippets, list):
                post["ai_evidence_snippets"] = [str(s)[:120] for s in snippets[:3]]
            post["ai_evidence_model"] = model_label

        posts.sort(
            key=lambda p: (
                float(p.get("ai_evidence_score") or 0),
                float(p.get("ai_read_priority") or 0),
                float(p.get("opportunity_score") or 0),
                float(p.get("comment_read_score") or 0),
            ),
            reverse=True,
        )
        if ctx:
            ctx.fetch_emit(f"{model_label} 已提取 {len(by_idx)} 条结构化证据", 72)
        return posts
    except Exception as e:
        print(f"[AIEvidence] {model_label} failed: {e}")
        return posts


def _refine_need_groups_with_model(needs: list[dict], topic: str, model_label: str) -> list[dict]:
    """让强模型基于已有聚类做合并/拆分建议；只重组已有帖子，不创造新证据。"""
    if len(needs) <= 1:
        return needs
    ctx = get_thread_session()
    items = []
    for idx, need in enumerate(needs[:8]):
        posts = need.get("posts") or []
        items.append({
            "idx": idx,
            "title": need.get("need_title", ""),
            "description": need.get("need_description", "")[:420],
            "opportunity_score": need.get("opportunity_score"),
            "heat_summary": need.get("heat_summary", ""),
            "top_signals": need.get("top_signals", []),
            "posts": [
                {
                    "title": p.get("title", ""),
                    "score": p.get("score", 0),
                    "num_comments": p.get("num_comments", 0),
                    "evidence": p.get("ai_evidence_summary", ""),
                    "signals": p.get("ai_signal_types", []),
                }
                for p in posts[:5]
            ],
        })
    prompt = f"""你是需求聚类质检员。你不会搜索网页，也不能创造新帖子。
请基于已有需求组和证据，判断是否需要合并重复组、压低泛化组、改进标题。

研究方向：{topic or '开放式自主挖掘'}

要求：
- 只能使用 source_indices 指向的已有需求组。
- 如果两个组本质重复，请合并。
- 如果某组太宽但无法凭现有帖子可靠拆分，就保留但降低 score，并在 reason 说明。
- 标题要表达具体产品机会，避免泛泛的“需求挖掘/综合优化”。
- 输出 2-6 个最终组。

输出纯 JSON：
{{
  "groups": [
    {{
      "source_indices": [0, 1],
      "title": "中文机会标题",
      "description": "1-2句中文描述",
      "title_en": "English opportunity title",
      "description_en": "1-2 sentence English description",
      "score": 1-5,
      "reason": "合并/保留/降权原因"
    }}
  ]
}}

已有需求组：
{json.dumps(items, ensure_ascii=False, indent=2)}
"""
    try:
        response = call_llm([
            {"role": "system", "content": "直接输出纯 JSON 对象，不要添加代码块标记或多余文字。"},
            {"role": "user", "content": prompt},
        ], max_tokens=3072)
        parsed = _parse_json_from_text(response)
        groups = _model_json_list(parsed, "groups", "final_groups", "needs", "clusters")
        if not isinstance(groups, list):
            return needs

        used: set[int] = set()
        refined: list[dict] = []
        for group in groups[:6]:
            if not isinstance(group, dict):
                continue
            raw_indices = group.get("source_indices") or group.get("indices") or group.get("need_indices")
            if not isinstance(raw_indices, list):
                continue
            indices: list[int] = []
            for raw_idx in raw_indices[:4]:
                try:
                    idx = int(raw_idx)
                except (TypeError, ValueError):
                    continue
                if 0 <= idx < len(needs) and idx not in indices:
                    indices.append(idx)
            if not indices:
                continue
            used.update(indices)
            merged_posts: list[dict] = []
            seen_posts: set[str] = set()
            for idx in indices:
                for post in needs[idx].get("posts") or []:
                    key = str(post.get("_post_id") or post.get("url") or post.get("title") or "")
                    if key and key in seen_posts:
                        continue
                    if key:
                        seen_posts.add(key)
                    merged_posts.append(post)
            if not merged_posts:
                continue
            title = _clean_ai_title(
                group.get("title") or group.get("need_title"),
                fallback=needs[indices[0]].get("need_title") or "未命名需求",
                max_len=40,
            )
            refined.append({
                "need_title": title,
                "need_description": str(group.get("description") or needs[indices[0]].get("need_description") or "")[:900],
                "need_title_en": str(group.get("title_en") or needs[indices[0]].get("need_title_en") or "")[:120],
                "need_description_en": str(group.get("description_en") or needs[indices[0]].get("need_description_en") or "")[:900],
                "posts": merged_posts,
                "total_score": sum(int(p.get("score") or 0) for p in merged_posts),
                "total_comments": sum(int(p.get("num_comments") or 0) for p in merged_posts),
                "ai_refine_model": model_label,
                "ai_refine_source_indices": indices,
                "ai_refine_reason": str(group.get("reason") or "")[:240],
                "ai_refine_score": group.get("score"),
            })

        for idx, need in enumerate(needs):
            if idx not in used:
                refined.append(need)

        if not refined:
            return needs
        refined = annotate_needs_with_opportunity(refined)
        if ctx:
            ctx.fetch_emit(f"{model_label} 已完成聚类合并/拆分校验：{len(needs)} → {len(refined)} 组", 89)
        return refined
    except Exception as e:
        print(f"[AIGroupRefine] {model_label} failed: {e}")
        return needs


def _repair_truncated_json(text: str):
    """Try to salvage a truncated JSON array by closing open braces/brackets."""
    import re
    text = text.strip()
    if not text.startswith("["):
        start = text.find("[")
        if start == -1:
            return None
        text = text[start:]

    opens = 0
    open_sq = 0
    in_str = False
    escape = False
    for ch in text:
        if escape:
            escape = False
            continue
        if ch == '\\':
            escape = True
            continue
        if ch == '"':
            in_str = not in_str
            continue
        if in_str:
            continue
        if ch == '{':
            opens += 1
        elif ch == '}':
            opens -= 1
        elif ch == '[':
            open_sq += 1
        elif ch == ']':
            open_sq -= 1

    if opens == 0 and open_sq == 0:
        try:
            return json.loads(text)
        except Exception:
            return None

    patched = text.rstrip().rstrip(',')
    if in_str:
        patched += '"'
    patched += '}'  * max(opens, 0)
    patched += ']' * max(open_sq, 0)
    try:
        result = json.loads(patched)
        print(f"[Clustering] Repaired truncated JSON ({opens} braces, {open_sq} brackets, in_str={in_str})")
        return result
    except Exception:
        pass

    last_complete = text.rfind('},')
    if last_complete > 0:
        attempt = text[:last_complete + 1] + ']' * max(open_sq, 1)
        try:
            result = json.loads(attempt)
            print(f"[Clustering] Salvaged partial JSON (truncated last entry)")
            return result
        except Exception:
            pass

    last_obj_end = text.rfind('}')
    if last_obj_end > 0:
        attempt = text[:last_obj_end + 1] + ']' * max(open_sq, 1)
        try:
            result = json.loads(attempt)
            print(f"[Clustering] Salvaged JSON (cut at last complete object)")
            return result
        except Exception:
            pass

    return None


def _cluster_posts_into_needs(posts: list[dict], topic: str = "") -> list[dict]:
    """两步聚类：Step1 过滤+粗分组（只输出索引），Step2 逐组生成标题/描述（可并发）。"""
    ctx = get_thread_session()
    _emit = ctx.fetch_emit if ctx else (lambda msg, prog: None)
    _lock = ctx.fetch_lock if ctx else threading.Lock()
    _job = ctx.fetch_job if ctx else {}
    # 帖子过多时截取 top 70（按 score 排序），避免 prompt 过大
    if len(posts) > 70:
        sorted_posts = sorted(posts, key=lambda p: p.get("score", 0), reverse=True)[:70]
        idx_map = {id(sp): i for i, sp in enumerate(posts)}
        reindexed = []
        for sp in sorted_posts:
            orig_idx = idx_map.get(id(sp), 0)
            reindexed.append((orig_idx, sp))
        reindexed.sort(key=lambda x: x[0])
        working_posts = [sp for _, sp in reindexed]
        print(f"[Clustering] 帖子过多（{len(posts)}），截取 top 70 进入聚类")
    else:
        working_posts = posts

    posts_summary = []
    many_posts = len(working_posts) > 20
    for i, p in enumerate(working_posts):
        entry = {
            "idx": i,
            "title": p["title"],
            "content": (p.get("content", "") or "")[:250 if many_posts else 400],
            "score": p.get("score", 0),
            "num_comments": p.get("num_comments", 0),
        }
        comments = p.get("comments", [])
        if comments:
            entry["top_comments"] = [
                _comment_body_for_prompt(c, 100 if many_posts else 150)
                for c in comments[:2 if many_posts else 3]
            ]
        if p.get("ai_evidence_summary"):
            entry["ai_evidence_summary"] = p.get("ai_evidence_summary")
            entry["ai_signal_types"] = p.get("ai_signal_types", [])
            entry["ai_evidence_score"] = p.get("ai_evidence_score")
        if p.get("_discovery_source") == "evidence_probe":
            entry["second_round_probe"] = {
                "query": p.get("_evidence_probe_query", ""),
                "reason": p.get("_evidence_probe_reason", ""),
                "signal": p.get("_evidence_probe_signal", ""),
            }
        posts_summary.append(entry)

    json_indent = None if many_posts else 2
    posts_json_str = json.dumps(posts_summary, ensure_ascii=False, indent=json_indent)

    # ── Step 1: 过滤 + 粗分组（只输出索引，JSON 极小，几乎不会解析失败） ──
    _emit("分析帖子关联性，过滤 + 粗分组...", 80)
    step1_prompt = CLUSTERING_STEP1_PROMPT.format(
        topic=topic or "（未指定）",
        posts_json=posts_json_str,
    )
    step1_messages = [
        {"role": "system", "content": "直接输出纯 JSON 对象，不要添加代码块标记或多余文字。"},
        {"role": "user", "content": step1_prompt},
    ]
    print(f"[Clustering Step1] {len(working_posts)} posts, prompt ~{len(step1_prompt)} chars")

    grouping = None
    for attempt in range(3):
        try:
            response = call_llm(step1_messages, max_tokens=2048)
            grouping = _parse_json_from_text(response)
            if grouping and isinstance(grouping, dict) and "groups" in grouping:
                break
            print(f"[Clustering Step1] attempt {attempt+1} 格式不对: {str(response)[:200]}")
            if attempt < 2:
                _emit(f"分组解析失败，正在重试（{attempt+1}/3）...", 82)
            grouping = None
        except Exception as e:
            print(f"[Clustering Step1] attempt {attempt+1} failed: {e}")
            if attempt < 2:
                _emit(f"分组模型调用失败，正在重试...", 82)
                import time; time.sleep(2)

    if not grouping or not isinstance(grouping.get("groups"), list):
        _emit("分组未成功，尝试轻量聚类...", 86)
        with _lock:
            _job["clustering_fallback"] = True
        return _fallback_needs(posts, topic=topic)

    groups = grouping["groups"]
    skipped = set(grouping.get("skipped", []))
    valid_groups = [g for g in groups if isinstance(g, list) and len(g) > 0]
    print(f"[Clustering Step1] 完成：{len(valid_groups)} 组，跳过 {len(skipped)} 帖子")

    if not valid_groups:
        with _lock:
            _job["clustering_fallback"] = True
        return _fallback_needs(posts, topic=topic)

    # ── Step 2: 逐组生成标题/描述/翻译（并发调用） ──
    _emit(f"为 {len(valid_groups)} 个需求组生成标题和描述...", 85)

    import concurrent.futures

    def _name_one_group(group_indices: list[int]) -> dict | None:
        """为一个组生成 need_title / need_description / title_translations。"""
        if ctx:
            set_thread_session(ctx)
        group_posts = []
        for idx in group_indices:
            if 0 <= idx < len(working_posts):
                p = working_posts[idx]
                group_posts.append({
                    "idx": idx,
                    "title": p["title"],
                    "content": (p.get("content", "") or "")[:500],
                    "score": p.get("score", 0),
                    "num_comments": p.get("num_comments", 0),
                    "top_comments": [_comment_body_for_prompt(c, 200) for c in p.get("comments", [])[:3]],
                    "ai_evidence_summary": p.get("ai_evidence_summary", ""),
                    "ai_signal_types": p.get("ai_signal_types", []),
                    "second_round_probe": {
                        "query": p.get("_evidence_probe_query", ""),
                        "reason": p.get("_evidence_probe_reason", ""),
                        "signal": p.get("_evidence_probe_signal", ""),
                    } if p.get("_discovery_source") == "evidence_probe" else None,
                })
        if not group_posts:
            return None
        prompt = CLUSTERING_STEP2_PROMPT.format(
            topic=topic or "（未指定）",
            group_posts_json=json.dumps(group_posts, ensure_ascii=False, indent=2),
        )
        msgs = [
            {"role": "system", "content": "直接输出纯 JSON 对象，不要添加代码块标记或多余文字。"},
            {"role": "user", "content": prompt},
        ]
        for att in range(2):
            try:
                resp = call_llm(msgs, max_tokens=2048)
                result = _parse_json_from_text(resp)
                if result and isinstance(result, dict) and "need_title" in result:
                    result["_indices"] = group_indices
                    return result
            except Exception as e:
                print(f"[Clustering Step2] group {group_indices[:3]}... attempt {att+1} failed: {e}")
        return None

    with concurrent.futures.ThreadPoolExecutor(max_workers=min(len(valid_groups), 4)) as pool:
        futures = [pool.submit(_name_one_group, g) for g in valid_groups]
        step2_results = [f.result() for f in futures]

    if len(step2_results) != len(valid_groups) or any(result is None for result in step2_results):
        with _lock:
            _job["clustering_fallback"] = True
        print("[Clustering] 需求组命名未全部成功，拒绝输出不完整需求卡片")
        return []

    # ── 组装最终 needs ──
    needs = []
    for r in step2_results:
        indices = r.get("_indices", [])
        translations = r.get("title_translations", {})
        if not str(r.get("need_title") or "").strip() or not str(r.get("need_description") or "").strip():
            with _lock:
                _job["clustering_fallback"] = True
            print("[Clustering] 需求组缺少标题或描述，拒绝输出不完整需求卡片")
            return []
        need_posts = []
        for idx in indices:
            if 0 <= idx < len(working_posts):
                post = dict(working_posts[idx])
                post["title_zh"] = translations.get(str(idx), "")
                need_posts.append(post)
        if need_posts:
            needs.append({
                "need_title": r.get("need_title", "未命名需求"),
                "need_description": r.get("need_description", ""),
                "need_title_en": r.get("need_title_en", ""),
                "need_description_en": r.get("need_description_en", ""),
                "posts": need_posts,
                "total_score": sum(p.get("score", 0) for p in need_posts),
                "total_comments": sum(p.get("num_comments", 0) for p in need_posts),
            })

    if not needs:
        with _lock:
            _job["clustering_fallback"] = True
        return []
    print(f"[Clustering] 两步聚类完成：{len(needs)} 个需求组")
    return needs


def _fallback_needs(posts: list[dict], topic: str = "") -> list[dict]:
    """Fallback: 聚类失败时用更简化的 prompt 做轻量聚类，避免机械分组。"""
    if not posts:
        return []
    valid = [p for p in posts if p.get("title")]
    if not valid:
        return []

    titles_block = "\n".join(f"{i}: {p['title']}" for i, p in enumerate(valid))
    fallback_prompt = (
        f"将以下帖子标题按语义分成 3-6 组，围绕研究主题「{topic or '未指定'}」聚类。\n"
        "输出 JSON（不加代码块标记），格式：\n"
        '[{"need_title":"中文需求名(5-15字)","need_description":"中文描述(1-2句)","indices":[0,1,3],'
        '"translations":{"0":"标题中文翻译","1":"标题中文翻译"}}]\n\n'
        f"帖子列表：\n{titles_block}"
    )
    try:
        resp = call_llm(
            [{"role": "system", "content": "你是需求分析专家，直接输出纯 JSON 数组。"},
             {"role": "user", "content": fallback_prompt}],
            max_tokens=4096,
        )
        groups = _parse_json_from_text(resp)
        if groups and isinstance(groups, list) and len(groups) >= 1:
            needs = []
            for g in groups:
                if not isinstance(g, dict):
                    continue
                if not str(g.get("need_title") or "").strip() or not str(g.get("need_description") or "").strip():
                    continue
                indices = g.get("indices", [])
                translations = g.get("translations", {})
                if not isinstance(indices, list):
                    continue
                chunk = []
                for idx in indices:
                    if 0 <= idx < len(valid):
                        post = dict(valid[idx])
                        post["title_zh"] = translations.get(str(idx), "")
                        chunk.append(post)
                if chunk:
                    needs.append({
                        "need_title": g.get("need_title", "未命名需求"),
                        "need_description": g.get("need_description", ""),
                        "posts": chunk,
                        "total_score": sum(p.get("score", 0) for p in chunk),
                        "total_comments": sum(p.get("num_comments", 0) for p in chunk),
                    })
            if needs:
                print(f"[Fallback] 轻量聚类成功: {len(needs)} 组")
                return needs
    except Exception as e:
        print(f"[Fallback] 轻量聚类也失败: {e}")

    # 没有通过模型聚类就不能把帖子按热度伪装成需求主题。
    print("[Fallback] 轻量聚类未返回有效分组，拒绝生成需求卡片")
    return []


def _review_needs_with_model(needs: list[dict], topic: str, model_label: str) -> list[dict]:
    """用强理解模型做后置机会二审；失败时返回原排序，不影响主挖掘。"""
    if not needs:
        return needs
    ctx = get_thread_session()
    review_items = []
    for idx, need in enumerate(needs[:10]):
        posts = need.get("posts") or []
        review_items.append({
            "idx": idx,
            "title": need.get("need_title", ""),
            "description": need.get("need_description", "")[:360],
            "opportunity_score": need.get("opportunity_score"),
            "heat_summary": need.get("heat_summary", ""),
            "top_signals": need.get("top_signals", []),
            "posts": [
                {
                    "title": p.get("title", ""),
                    "score": p.get("score", 0),
                    "num_comments": p.get("num_comments", 0),
                    "evidence": p.get("ai_evidence_summary", ""),
                    "signals": p.get("ai_signal_types", []),
                }
                for p in posts[:3]
            ],
        })
    prompt = f"""你是产品机会评审专家。请基于给定证据做需求主题二审重排。

研究方向：{topic or '未指定'}

要求：
- 只基于给定证据，不要编造新事实。
- 优先考虑：真实痛点具体性、评论共鸣、替代方案/迁移/付费行为、软件或 AI 可解决性、非显而易见机会。
- 如果标题太泛，可以给出更好的中文标题，并同步给出英文标题。
- 必须输出纯 JSON 对象，顶层字段必须叫 ranked。

格式：
{{
  "ranked": [
    {{"idx": 0, "score": 4.0, "title": "中文标题", "title_en": "English title", "reason": "一句话理由"}}
  ]
}}

需求列表：
{json.dumps(review_items, ensure_ascii=False, indent=2)}
"""
    try:
        response = call_llm([
            {"role": "system", "content": "直接输出纯 JSON 对象，不要添加代码块标记或多余文字。"},
            {"role": "user", "content": prompt},
        ], max_tokens=2048)
        parsed = _parse_json_from_text(response)
        ranked = _model_json_list(parsed, "ranked", "ranked_needs", "ranking", "reviews", "needs", "items", "groups", "results")
        if not isinstance(ranked, list):
            return needs
        by_idx: dict[int, dict] = {}
        for item in ranked:
            if not isinstance(item, dict):
                continue
            idx = _item_index(item)
            if idx is None:
                continue
            by_idx[idx] = item
        reviewed = []
        for idx, need in enumerate(needs):
            item = by_idx.get(idx)
            if item:
                try:
                    need["ai_review_score"] = max(1.0, min(5.0, round(float(item.get("score") or 0), 2)))
                except (TypeError, ValueError):
                    need["ai_review_score"] = None
                reason = str(item.get("reason") or "").strip()
                if reason:
                    need["ai_review_reason"] = reason[:240]
                title = str(item.get("title") or "").strip()
                if title and title != "未命名需求" and len(title) <= 44:
                    need["need_title"] = _clean_ai_title(title, fallback=need.get("need_title", ""), max_len=40)
                title_en = str(item.get("title_en") or "").strip()
                if title_en and not _contains_cjk(title_en) and len(title_en) <= 120:
                    need["need_title_en"] = title_en
                need["ai_review_model"] = model_label
            reviewed.append(need)
        reviewed.sort(
            key=lambda n: (
                float(n.get("ai_review_score") or 0),
                float(n.get("opportunity_score") or 0),
                int(n.get("total_comments") or 0),
            ),
            reverse=True,
        )
        if ctx:
            ctx.fetch_emit(f"{model_label} 完成机会二审与重排", 91)
        return reviewed
    except Exception as e:
        print(f"[AIReview] {model_label} failed: {e}")
        if ctx:
            ctx.fetch_emit(f"{model_label} 二审跳过，保留默认排序", 91)
        return needs


def _default_market_validation(reason: str, *, level: str = "weak", seed: str = "") -> dict[str, Any]:
    """给未完成 ST 校验的需求卡片提供保守商业信号，避免 UI 缺标签。"""
    source_key = f"{reason}|{seed}"
    source_id = "st_market_" + hashlib.sha1(source_key.encode("utf-8", errors="ignore"), usedforsecurity=False).hexdigest()[:12]
    return {
        "level": level,
        "label": "商业信号弱",
        "source_id": source_id,
        "source_type": "sensor_tower_market",
        "competitor_count": 0,
        "top_competitors": [],
        "queries": [],
        "risk_note": reason,
        "checked_at": datetime.utcnow().strftime("%Y-%m-%d"),
        "candidate_region": "US",
        "metrics_region": "全球",
        "market_region": "全球",
        "source_confidence": "fallback",
    }


def _attach_need_source_ids(needs: list[dict]) -> list[dict]:
    """聚合每张需求卡的 Reddit 证据和 ST 市场 source_id。"""
    for need in needs:
        source_ids: list[str] = []
        for existing in need.get("source_ids") or []:
            text = str(existing or "").strip()
            if text:
                source_ids.append(text)
        for ev in need.get("evidence") or []:
            if isinstance(ev, dict):
                sid = str(ev.get("source_id") or ev.get("evidence_id") or "").strip()
                if sid:
                    source_ids.append(sid)
        market = need.get("market_validation") if isinstance(need.get("market_validation"), dict) else None
        if market:
            sid = str(market.get("source_id") or "").strip()
            if sid:
                source_ids.append(sid)
            for comp in market.get("top_competitors") or []:
                if isinstance(comp, dict):
                    comp_sid = str(comp.get("source_id") or "").strip()
                    if comp_sid:
                        source_ids.append(comp_sid)
        need["source_ids"] = list(dict.fromkeys(source_ids))
    return needs


def _attach_market_validation(
    needs: list[dict],
    *,
    topic: str,
    known_competitors: list[str],
    search_queries: list[str],
    market_region: str = "",
    max_needs: int = 5,
) -> list[dict]:
    """为需求卡片附加轻量 ST 商业化信号；失败不影响主挖掘结果。"""
    if not needs:
        return needs
    ctx = get_thread_session()
    fallback_reason = "SensorTower 未完成稳定商业化校验，按保守口径展示为商业信号弱。"
    try:
        status = st_check_available()
        if not status.get("available") or not status.get("api_ok"):
            if ctx:
                ctx.fetch_emit("SensorTower 未可用，跳过市场商业化校验", 91)
            for idx, need in enumerate(needs):
                seed = str(need.get("need_title") or idx)
                need["market_validation"] = need.get("market_validation") or _default_market_validation(fallback_reason, seed=seed)
            return _attach_need_source_ids(needs)
    except Exception as e:
        print(f"[MarketValidation] st status failed: {e}")
        for idx, need in enumerate(needs):
            seed = str(need.get("need_title") or idx)
            need["market_validation"] = need.get("market_validation") or _default_market_validation("SensorTower 状态检测失败，按保守口径展示为商业信号弱。", seed=seed)
        return _attach_need_source_ids(needs)

    try:
        from .st_client import validate_market_for_need

        targets = needs[:max(1, min(max_needs, len(needs)))]
        if ctx:
            ctx.fetch_emit(f"SensorTower 正在校验 {len(targets)} 个需求的商业化信号...", 91)
        for idx, need in enumerate(targets):
            try:
                validation = validate_market_for_need(
                    need,
                    topic=topic,
                    known_competitors=known_competitors,
                    search_queries=search_queries,
                    market_region=market_region,
                    max_queries=8,
                )
                if validation:
                    need["market_validation"] = validation
                else:
                    need["market_validation"] = _default_market_validation(
                        "SensorTower 未生成稳定竞品结果，按保守口径展示为商业信号弱。",
                        seed=str(need.get("need_title") or idx),
                    )
            except Exception as e:
                print(f"[MarketValidation] need {idx} failed: {e}")
                need["market_validation"] = _default_market_validation(
                    "SensorTower 校验失败，按保守口径展示为商业信号弱。",
                    seed=str(need.get("need_title") or idx),
                )
        for idx, need in enumerate(needs):
            if not need.get("market_validation"):
                need["market_validation"] = _default_market_validation(
                    "未进入本轮 SensorTower 深度校验批次，按保守口径展示为商业信号弱。",
                    seed=str(need.get("need_title") or idx),
                )
        if ctx:
            ctx.fetch_emit("SensorTower 商业化信号校验完成", 92)
    except Exception as e:
        print(f"[MarketValidation] attach failed: {e}")
        for idx, need in enumerate(needs):
            seed = str(need.get("need_title") or idx)
            need["market_validation"] = need.get("market_validation") or _default_market_validation("SensorTower 商业化校验整体失败，按保守口径展示为商业信号弱。", seed=seed)
    return _attach_need_source_ids(needs)


# ============================================================
# Debate state — now per-session via SessionContext
# ============================================================
# ctx.debate_state, _save_debate_cache, _load_debate_cache, _reset_debate
# are replaced by ctx.debate_state, ctx.save_debate_cache(), ctx.reset_debate()

# ============================================================
# SSE helpers
# ============================================================

def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def _sse_response(stream) -> StreamingResponse:
    """Return an SSE response with buffering disabled for prompt first paint."""
    return StreamingResponse(
        stream,
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
        },
    )


def _provider_for_role(role: str, ctx: SessionContext | None = None) -> str:
    """返回角色对应的模型提供商（claude/gpt）。"""
    if ctx:
        return ctx.get_role_model_config().get(role, "gpt")
    thread_ctx = get_thread_session()
    if thread_ctx:
        return thread_ctx.get_role_model_config().get(role, "gpt")
    return get_role_model_config().get(role, "gpt")

# ============================================================
# Pydantic models
# ============================================================

UI_LANGUAGE_ZH = "zh-CN"
UI_LANGUAGE_EN = "en-US"


class FetchRequest(BaseModel):
    mode: str = "open"           # "sentence" | "keywords" | "open"
    language: str = UI_LANGUAGE_ZH
    query: str = ""              # for sentence mode
    keywords: list[str] = []     # for keywords mode
    sources: list[str] = ["hackernews"]  # ["hackernews", "reddit"]
    category: str = "top"        # for open mode HN category
    reddit_categories: list[str] = []  # selected reddit board categories
    limit: int = 70
    time_period: str = "6months"  # "month" | "3months" | "6months" | "9months"
    product: str = ""             # existing product name (optional)
    market: str = ""              # target market/region (optional)
    demographics: str = ""        # target user demographics (optional)
    segment: str = ""             # behavioral/situational segment (optional)
    pain_points: int = 10         # number of pain points to deep-dive
    competitors: str = ""         # known competitors, comma-separated (optional)
    demo: bool = False             # demo mode: use cached data, fake progress
    fetch_model: str = "default"   # "default"/"fast" or "deep"; model comes from user settings


class ConfigSaveRequest(BaseModel):
    CLAUDE_BASE_URL: str = Field(default="", max_length=2048)
    CLAUDE_API_KEY: str = Field(default="", max_length=4096)
    CLAUDE_MODEL: str = Field(default="", max_length=256)
    GPT_BASE_URL: str = Field(default="", max_length=2048)
    GPT_API_KEY: str = Field(default="", max_length=4096)
    GPT_MODEL: str = Field(default="", max_length=256)
    TAVILY_API_KEY: str = Field(default="", max_length=4096)
    FEISHU_APP_ID: str = Field(default="", max_length=256)
    FEISHU_APP_SECRET: str = Field(default="", max_length=4096)
    CLEAR_FIELDS: list[str] = Field(default_factory=list, max_length=4)


class TestConnectionRequest(BaseModel):
    prefix: str = Field(pattern="^(GPT|CLAUDE)$")
    base_url: str = Field(default="", max_length=2048)
    api_key: str = Field(default="", max_length=4096)
    model: str = Field(default="", max_length=256)


def _normalize_ui_language(value: Any) -> str:
    return UI_LANGUAGE_EN if str(value or "").strip().lower() in {"en", "en-us", "english"} else UI_LANGUAGE_ZH


def _is_ui_en(language: Any) -> bool:
    return _normalize_ui_language(language) == UI_LANGUAGE_EN


def _ui_text(language: Any, zh: str, en: str) -> str:
    return en if _is_ui_en(language) else zh


def _need_title_for_language(need: dict, language: Any) -> str:
    """按 UI 语言选择需求标题；只返回展示/提示用字符串，不修改原始 need。"""
    if _is_ui_en(language):
        title = str(need.get("need_title_en") or "").strip()
        if title:
            return title
    return str(need.get("need_title") or "").strip()


def _need_description_for_language(need: dict, language: Any) -> str:
    """按 UI 语言选择需求描述；英文模式优先使用英文聚类描述。"""
    if _is_ui_en(language):
        desc = str(need.get("need_description_en") or "").strip()
        if desc:
            return desc
    return str(need.get("need_description") or "").strip()


def _need_for_language(need: dict, language: Any) -> dict:
    """生成传给 LLM 的语言化 need 副本，避免英文模式把中文标题带入深链路。"""
    if not _is_ui_en(language):
        return need
    localized = dict(need)
    title = _need_title_for_language(need, language)
    desc = _need_description_for_language(need, language)
    if title:
        localized["need_title"] = title
    if desc:
        localized["need_description"] = desc
    return localized


def _friendly_error_for_language(language: Any, error: Exception | str) -> str:
    """将底层异常转成当前 UI 语言下的用户可见错误。"""
    if not _is_ui_en(language):
        return _friendly_error(error)
    zh = _friendly_error(error)
    low = zh.lower()
    if "api key" in low or "令牌" in zh:
        return "API key is invalid or sign-in has expired. Check your local settings."
    if "访问被拒" in zh or "403" in low or "forbidden" in low:
        return "Model or data service access was denied. Check your account permissions."
    if "额度" in zh or "503" in low or "service unavailable" in low:
        return "Model quota or external service capacity is unavailable. Check your account balance and provider status."
    if "模型服务连接失败" in zh or "ssl" in low or "eof" in low:
        return "The model service connection failed. Check the Base URL, gateway status, and local network."
    if "网页内容" in zh or "非标准文本响应" in zh:
        return "The model endpoint returned a web page instead of a model response. Check whether Base URL includes /v1."
    if "输出中断" in zh or "stream" in low:
        return "Model output was interrupted. Retry, then check the model service and local network."
    return "The model or external service is temporarily unavailable. Check your local settings."


_AUTO_DISCOVER_NOTE_EN = {
    "本轮优先探索冷门但软件可解的生活/家庭/个人管理场景，避免选择常见效率工具方向。":
        "This run prioritizes overlooked but software-solvable life, family, and personal management scenarios, avoiding generic productivity-tool ideas.",
    "本轮优先探索有强烈 workaround、迁移或付费阻力的垂直人群，不要集中在主流生产力赛道。":
        "This run prioritizes vertical user groups with strong workarounds, switching friction, or payment resistance, instead of crowded productivity categories.",
    "本轮优先选择彼此差异很大的方向，覆盖不同用户角色、不同消费场景和不同 subreddit 圈层。":
        "This run deliberately explores differentiated directions across user roles, consumption contexts, and subreddit circles.",
    "本轮优先寻找非显而易见机会：小众职业、特殊家庭关系、跨语言/跨地域协作、线下流程数字化。":
        "This run looks for non-obvious opportunities such as niche professions, special family workflows, cross-language collaboration, regional workflows, and offline-process digitization.",
    "本轮避开过度拥挤的 AI 写作、笔记、待办和通用聊天方向，寻找更具体的高摩擦用户旅程。":
        "This run avoids crowded AI writing, note-taking, to-do, and general-chat directions, and searches for more specific high-friction user journeys.",
}


def _contains_cjk(text: str) -> bool:
    return bool(re.search(r"[\u3400-\u9fff]", text or ""))


def _translate_fetch_progress_tail(text: str) -> str:
    """翻译挖掘进度里常见的动态中文片段，避免英文 UI 出现中英混排。"""
    cleaned = text
    for zh, en in _AUTO_DISCOVER_NOTE_EN.items():
        cleaned = cleaned.replace(zh, en)
    generic_replacements = {
        "聚焦海量照片整理、回忆管理和智能检索的用户痛点":
            "focused on user pain points around large photo libraries, memory management, and intelligent retrieval",
        "快速": "quick",
        "深度": "deep",
        "手动": "manual",
        "自主": "autonomous",
        "需求聚类": "demand clustering",
        "WebSearch发现": "WebSearch discovery",
        "搜索规划": "search planning",
        "关键词搜索": "keyword search",
        "证据补搜": "evidence probe search",
        "质量筛选": "quality filtering",
        "评论充实": "comment enrichment",
        "机会二审": "opportunity second review",
        "市场校验": "market validation",
    }
    for zh, en in generic_replacements.items():
        cleaned = cleaned.replace(zh, en)
    return cleaned


def _fetch_progress_text(language: Any, message: Any) -> str:
    """把挖掘任务的中文进度文案转换为英文 UI 文案；内部日志仍保留原文。"""
    text = str(message or "")
    if not _is_ui_en(language) or not text:
        return text

    translated_tail = _translate_fetch_progress_tail(text)
    if translated_tail != text and not _contains_cjk(translated_tail):
        return translated_tail

    exact = {
        "准备开始挖掘...": "Preparing demand mining...",
        "正在连接数据源...": "Connecting data sources...",
        "正在检测模型可用性...": "Checking model availability...",
        "正在检测 rdt-cli 连接状态...": "Checking rdt-cli connection...",
        "初始化需求识别引擎...": "Initializing demand detection engine...",
        "加载痛点检测模型...": "Loading pain-point detection model...",
        "建立社区数据管线...": "Building community data pipeline...",
        "校准信号过滤阈值...": "Calibrating signal filtering thresholds...",
        "连接数据源...": "Connecting data sources...",
        "规划搜索策略...": "Planning search strategy...",
        "使用原始输入进行搜索...": "Searching with the original input...",
        "启动多源采集调度器...": "Starting multi-source collection scheduler...",
        "Lumon 正在分析高价值挖掘方向...": "Lumon is analyzing high-value mining directions...",
        "自主发现规划失败，使用热门板块浏览...": "Autonomous planning failed. Browsing popular communities...",
        "开始开放式挖掘...": "Starting open-ended mining...",
        "语义去重与排序...": "Deduplicating and ranking semantically...",
        "评估产品机会...": "Evaluating product opportunities...",
        "SensorTower 未可用，跳过市场商业化校验": "SensorTower is unavailable. Skipping commercialization validation.",
        "SensorTower 商业化信号校验完成": "SensorTower commercialization signal validation completed.",
        "WebSearch 未提取到有效帖子": "WebSearch did not extract valid posts.",
        "WebSearch 未发现相关帖子": "WebSearch did not find relevant posts.",
        "第一轮证据未形成稳定追问探针，跳过二轮补搜": "The first evidence round did not produce stable follow-up probes. Skipping second-round search.",
        "二轮补搜未新增符合热度门槛的帖子，继续使用第一轮证据": "Second-round search did not add posts above the heat threshold. Continuing with first-round evidence.",
        "未采集到有效帖子，请尝试更换关键词或数据源": "No valid posts were collected. Try different keywords or data sources.",
        "分析帖子关联性，过滤 + 粗分组...": "Analyzing post relationships, filtering, and rough-grouping...",
        "分组模型调用失败，正在重试...": "Grouping model call failed. Retrying...",
        "分组未成功，尝试轻量聚类...": "Grouping did not succeed. Trying lightweight clustering...",
    }
    if text in exact:
        return exact[text]

    if m := re.search(r"^自主探索偏置：(.+)$", text):
        note = _translate_fetch_progress_tail(m.group(1))
        if _contains_cjk(note):
            note = "This run explores differentiated high-signal demand spaces beyond the most obvious categories."
        return f"Autonomous exploration bias: {note}"
    if m := re.search(r"^Autonomous exploration bias: (.+)$", text):
        note = _translate_fetch_progress_tail(m.group(1))
        if _contains_cjk(note):
            note = "This run explores differentiated high-signal demand spaces beyond the most obvious categories."
        return f"Autonomous exploration bias: {note}"
    if m := re.search(r"^发现 (\d+) 个方向：(.+)$", text):
        reason = _translate_fetch_progress_tail(m.group(2))
        if _contains_cjk(reason):
            reason = "exploring differentiated high-signal demand spaces."
        return f"Found {m.group(1)} directions: {reason}"
    if m := re.search(r"^Found (\d+) directions: (.+)$", text):
        reason = _translate_fetch_progress_tail(m.group(2))
        if _contains_cjk(reason):
            reason = "exploring differentiated high-signal demand spaces."
        return f"Found {m.group(1)} directions: {reason}"
    if m := re.search(r"^搜索策略：(.+)$", text):
        strategy = _translate_fetch_progress_tail(m.group(1))
        if _contains_cjk(strategy):
            strategy = "using AI-planned search terms, communities, and evidence probes."
        return f"Search strategy: {strategy}"
    if m := re.search(r"^Search strategy: (.+)$", text):
        strategy = _translate_fetch_progress_tail(m.group(1))
        if _contains_cjk(strategy):
            strategy = "using AI-planned search terms, communities, and evidence probes."
        return f"Search strategy: {strategy}"

    patterns: list[tuple[str, str]] = [
        (r"^(.+?) 模型不可用，请检查本地配置$", r"\1 model is unavailable. Please check your local settings."),
        (r"^正在检测 WebSearch（(.+?)）\.\.\.$", r"Checking WebSearch (\1)..."),
        (r"^演示数据不存在.*", "Demo data is missing. Please check your local settings."),
        (r"^挖掘完成！发现 (\d+) 个需求主题，共 (\d+) 个帖子$", r"Mining complete. Found \1 demand topics from \2 posts."),
        (r"^⏱ 总用时 (.+?) — 演示模式$", r"⏱ Total time \1 — demo mode"),
        (r"^⏱ 总用时 (.+?) — 缓存加速$", r"⏱ Total time \1 — cache accelerated"),
        (r"^⏱ 总用时 (.+?) — (.*)$", r"⏱ Total time \1 — \2"),
        (r"^检测到相同需求的历史挖掘结果，正在加载\.\.\.$", "Found a previous mining result with the same parameters. Loading..."),
        (r"^校验缓存数据完整性\.\.\.$", "Checking cached data integrity..."),
        (r"^还原需求主题结构\.\.\.$", "Restoring demand topic structure..."),
        (r"^匹配帖子关联性\.\.\.$", "Matching post relevance..."),
        (r"^验证数据时效性\.\.\.$", "Checking data freshness..."),
        (r"^整理结构\.\.\.$", "Organizing structure..."),
        (r"^Reddit 引擎: rdt-cli$", "Reddit engine: rdt-cli"),
        (r"^Reddit 引擎: rdt-cli 未认证.*", "Reddit engine: rdt-cli is not authenticated. Please check your local settings."),
        (r"^Reddit 引擎: (.+?)（未知状态）$", r"Reddit engine: \1 (unknown status)"),
        (r"^使用指定板块 \+ LLM 推荐，共 (\d+) 个社区$", r"Using selected communities + LLM recommendations: \1 communities"),
        (r"^锁定 (\d+) 条搜索词、(\d+) 个社区$", r"Locked \1 search terms and \2 communities"),
        (r"^锁定 (\d+) 个社区、(\d+) 条搜索词$", r"Locked \1 communities and \2 search terms"),
        (r"^正在分析产品方案，规划搜索策略\.\.\.$", "Analyzing the product idea and planning the search strategy..."),
        (r"^搜索计划就绪，共 (\d+) 个搜索任务$", r"Search plan ready: \1 search tasks"),
        (r"^搜索 \((\d+)/(\d+)\)：(.+)$", r"Search (\1/\2): \3"),
        (r"^搜索失败：(.+?) — (.+)$", r"Search failed: \1 — \2"),
        (r"^使用默认高价值板块（(\d+) 个）和 (\d+) 条搜索词$", r"Using \1 default high-value communities and \2 search terms"),
        (r"^Tavily API Key 未配置，跳过 WebSearch 发现$", r"Tavily API key is not configured. Skipping WebSearch discovery."),
        (r"^WebSearch 发现模式：(\d+) 条语义搜索$", r"WebSearch discovery mode: \1 semantic searches"),
        (r"^WebSearch \((\d+)/(\d+)\)：(.+)$", r"WebSearch (\1/\2): \3"),
        (r"^WebSearch 发现 (\d+) 个独立 Reddit 帖子$", r"WebSearch found \1 unique Reddit posts"),
        (r"^⚠️?\s*GPT 未配置，请在设置中配置 GPT$", r"GPT is not configured. Please check your local settings."),
        (r"^⚠️?\s*GPT 中转站不支持 web_search，请切换引擎$", r"GPT relay does not support web_search. Please check your local settings."),
        (r"^GPT WebSearch 发现模式：(\d+) 条搜索$", r"GPT WebSearch discovery mode: \1 searches"),
        (r"^GPT WebSearch \((\d+)-(\d+)/(\d+)\)\.\.\.$", r"GPT WebSearch (\1-\2/\3)..."),
        (r"^⚠️ GPT 中转站不支持 web_search：(.+)$", r"⚠️ GPT relay does not support web_search: \1"),
        (r"^⚠️ GPT WebSearch 批次失败：(.+)$", r"⚠️ GPT WebSearch batch failed: \1"),
        (r"^GPT WebSearch 发现 (\d+) 个 Reddit 帖子, (\d+) 个 subreddit$", r"GPT WebSearch found \1 Reddit posts and \2 subreddits"),
        (r"^⚠️?\s*Claude 未配置，请在设置中检查模型配置$", r"Claude is not configured. Please check your local settings."),
        (r"^⚠️?\s*Claude 中转站不支持 web_search，请切换到 GPT 或 Tavily$", r"Claude relay does not support web_search. Please check your local settings."),
        (r"^Claude WebSearch 发现模式：(\d+) 条搜索$", r"Claude WebSearch discovery mode: \1 searches"),
        (r"^Claude WebSearch \((\d+)-(\d+)/(\d+)\)\.\.\.$", r"Claude WebSearch (\1-\2/\3)..."),
        (r"^⚠️ Claude WebSearch 失败: (.+)$", r"⚠️ Claude WebSearch failed: \1"),
        (r"^Claude WebSearch 发现 (\d+) 个 Reddit 帖子, (\d+) 个 subreddit$", r"Claude WebSearch found \1 Reddit posts and \2 subreddits"),
        (r"^(.+?) WebSearch 正在发现高质量 Reddit 帖子\.\.\.$", r"\1 WebSearch is discovering high-quality Reddit posts..."),
        (r"^WebSearch 发现 (\d+) 个帖子，rdt read 并发提取全文\\+评论\.\.\.$", r"WebSearch found \1 posts. rdt read is extracting full text and comments in parallel..."),
        (r"^已提取 (\d+)/(\d+) 个帖子全文$", r"Extracted full text for \1/\2 posts"),
        (r"^WebSearch 贡献 (\d+) 个高质量帖子（含深层评论）$", r"WebSearch contributed \1 high-quality posts with deep comments"),
        (r"^WebSearch 跳过: (.+)$", r"WebSearch skipped: \1"),
        (r"^动态发现 (\d+) 个新 subreddit: (.+)$", r"Dynamically found \1 new subreddits: \2"),
        (r"^已过滤 (\d+) 个跑偏 subreddit: (.+)$", r"Filtered \1 off-topic subreddits: \2"),
        (r"^正在补充搜索 (.+?)\.\.\.$", r"Supplementing search on \1..."),
        (r"^并发搜索前 (\d+) 个核心 subreddit\.\.\.$", r"Searching the first \1 core subreddits in parallel..."),
        (r"^核心 sub 搜索完成：(\d+) 个帖子$", r"Core subreddit search completed: \1 posts"),
        (r"^已采集 (\d+) 个帖子（充足），跳过剩余 (\d+) 个 sub$", r"Collected \1 posts, enough for this run. Skipping \2 remaining subreddits."),
        (r"^已采集 (\d+) 个帖子，(.+?)模式搜索剩余 (\d+) 个 sub\.\.\.$", r"Collected \1 posts. Searching \3 remaining subreddits in \2 mode..."),
        (r"^已采集 (\d+) 个帖子（充足），停止搜索$", r"Collected \1 posts, enough for this run. Stopping search."),
        (r"^补充搜索：累计 (\d+) 个帖子$", r"Supplemental search: \1 posts collected in total"),
        (r"^Reddit 搜索完成：共 (\d+) 个帖子$", r"Reddit search completed: \1 posts"),
        (r"^(.+?): 已发现 (\d+) 个帖子$", r"\1: found \2 posts"),
        (r"^(.+?) 采集出错: (.+)$", r"\1 collection error: \2"),
        (r"^初始数据不足（(\d+) 条），正在扩展搜索\.\.\.$", r"Initial data is insufficient (\1 posts). Expanding search..."),
        (r"^扩展搜索后：共 (\d+) 条帖子$", r"After expanded search: \1 posts total"),
        (r"^时间范围过滤：移除 (\d+) 条超出 (.+?) 范围的帖子$", r"Time-range filter removed \1 posts outside \2"),
        (r"^热度门槛过滤：(\d+) → (\d+) 个有共鸣帖子$", r"Heat threshold filter: \1 -> \2 resonant posts"),
        (r"^热度门槛命中较少（(\d+) 个），保留完整候选池继续判断$", r"Few posts passed the heat threshold (\1). Keeping the full candidate pool for judgment."),
        (r"^未采集到帖子：(.+)$", r"No posts collected: \1"),
        (r"^采集完成，共 (\d+) 个帖子$", r"Collection complete: \1 posts"),
        (r"^开始质量筛选（(\d+) 个帖子）\.\.\.$", r"Starting quality filtering (\1 posts)..."),
        (r"^硬性门槛过滤：(\d+) → (\d+) 个帖子$", r"Hard threshold filter: \1 -> \2 posts"),
        (r"^并发拉取 (\d+) 个帖子的深层评论\.\.\.$", r"Fetching deep comments for \1 posts in parallel..."),
        (r"^Lumon 正在统一筛选帖子（相关性 \+ 产品机会）\.\.\.$", "Lumon is filtering posts by relevance and product opportunity..."),
        (r"^质量筛选完成：(\d+) → (\d+) 个有效帖子$", r"Quality filtering complete: \1 -> \2 valid posts"),
        (r"^分析帖子关联性\.\.\.$", "Analyzing post relationships..."),
        (r"^聚类为需求主题\.\.\.$", "Clustering posts into demand topics..."),
        (r"^正在用 (.+?) 筛选评论深读优先级\.\.\.$", r"Using \1 to prioritize deep comment reads..."),
        (r"^rdt 限额保护：评论读取前冷却 (\d+) 秒\.\.\.$", r"rdt rate-limit protection: cooling down \1 seconds before comment reads..."),
        (r"^拉取 (\d+) 个帖子的深层评论\.\.\.$", r"Fetching deep comments for \1 posts..."),
        (r"^评论充实已足够（(\d+) 帖 / (\d+) 条评论），停止$", r"Comment enrichment is sufficient (\1 posts / \2 comments). Stopping."),
        (r"^连续 (\d+) 批无结果，跳过剩余评论充实$", r"\1 consecutive empty batches. Skipping remaining comment enrichment."),
        (r"^评论充实进度：(\d+) 帖 / (\d+) 条评论$", r"Comment enrichment progress: \1 posts / \2 comments"),
        (r"^评论充实完成：(\d+)/(\d+) 帖，共 (\d+) 条评论$", r"Comment enrichment complete: \1/\2 posts, \3 comments"),
        (r"^正在用 (.+?) 提取帖子证据\.\.\.$", r"Using \1 to extract post evidence..."),
        (r"^证据驱动二轮补搜：(\d+) 个探针，最多 (\d+) 次 rdt search$", r"Evidence-driven second-round search: \1 probes, up to \2 rdt searches"),
        (r"^rdt 限额保护：二轮补搜前冷却 (\d+) 秒\.\.\.$", r"rdt rate-limit protection: cooling down \1 seconds before second-round search..."),
        (r"^二轮补搜新增 (\d+) 个有热度帖子，进入聚类验证$", r"Second-round search added \1 heated posts for clustering validation"),
        (r"^共 (\d+) 个帖子进入聚类（过滤 \+ 分组一步完成）$", r"\1 posts entered clustering (filtering + grouping in one pass)"),
        (r"^分析帖子关联性，过滤 \+ 粗分组\.\.\.$", "Analyzing post relationships, filtering, and rough-grouping..."),
        (r"^分组解析失败，正在重试（(\d+)/3）\.\.\.$", r"Grouping parse failed. Retrying (\1/3)..."),
        (r"^为 (\d+) 个需求组生成标题和描述\.\.\.$", r"Generating titles and descriptions for \1 demand groups..."),
        (r"^采集到 (\d+) 个帖子但未归纳出需求主题.*$", r"Collected \1 posts but could not form demand topics. Try clearer keywords, a more specific category, or a wider time range."),
        (r"^正在用 (.+?) 校验需求聚类\.\.\.$", r"Using \1 to validate demand clusters..."),
        (r"^正在用 (.+?) 做机会二审\.\.\.$", r"Using \1 for opportunity second review..."),
        (r"^产出 (\d+) 个需求主题，整理结构\.\.\.$", r"Produced \1 demand topics. Organizing structure..."),
        (r"^(.+?) 已筛选深读评论优先级：(\d+) 个高价值帖子$", r"\1 prioritized deep comment reads: \2 high-value posts"),
        (r"^(.+?) 已提取 (\d+) 条结构化证据$", r"\1 extracted \2 structured evidence items"),
        (r"^(.+?) 已完成聚类合并/拆分校验：(\d+) → (\d+) 组$", r"\1 completed cluster merge/split validation: \2 -> \3 groups"),
        (r"^(.+?) 完成机会二审与重排$", r"\1 completed opportunity second review and reranking"),
        (r"^(.+?) 二审跳过，保留默认排序$", r"\1 second review skipped. Keeping default ranking."),
        (r"^SensorTower 正在校验 (\d+) 个需求的商业化信号\.\.\.$", r"SensorTower is validating commercialization signals for \1 demand topics..."),
    ]
    for pattern, repl in patterns:
        if re.search(pattern, text):
            translated = re.sub(pattern, repl, text)
            cleaned = _translate_fetch_progress_tail(translated)
            if _contains_cjk(cleaned):
                return re.sub(r"[\u3400-\u9fff][\u3400-\u9fff\s，。、“”：（）/、+-]*", "", cleaned).strip() or "Processing..."
            return cleaned
    return text


def _debate_role_label(ctx: SessionContext, role: str, language: str) -> str:
    if _is_ui_en(language):
        return {
            "director": "Director",
            "analyst": "Product Manager",
            "critic": "Skeptical User",
            "investor": "Investor",
        }.get(role, role)
    fallback = {"director": "导演", "analyst": "产品经理", "critic": "杠精", "investor": "投资人"}
    return ctx.role_names.get(role, fallback.get(role, role))


class StartDebateRequest(BaseModel):
    need_index: int
    max_rounds: int = 5
    demo: bool = False
    language: str = UI_LANGUAGE_ZH


class StartFreeDebateRequest(BaseModel):
    user_input: str
    max_rounds: int = 5
    language: str = UI_LANGUAGE_ZH


class HumanMessageRequest(BaseModel):
    text: str
    target: str = "analyst"  # "analyst" | "critic"
    language: str = ""


class TranslateRequest(BaseModel):
    text: str

# ============================================================
# Config routes
# ============================================================

@router.get("/config/status")
def config_status(request: Request):
    ctx = _get_session(request)
    return ctx.check_config()


@router.get("/config/values")
def config_values(request: Request):
    ctx = _get_session(request)
    return ctx.get_config_values()


@router.post("/config")
def save_config(req: ConfigSaveRequest, request: Request):
    ctx = _get_session(request)
    config = req.model_dump()
    try:
        ctx.save_config(config)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True}


@router.post("/config/test")
def test_config(req: TestConnectionRequest, request: Request):
    ctx = _get_session(request)
    override = {}
    if req.base_url:
        override["base_url"] = req.base_url
    if req.api_key:
        override["api_key"] = req.api_key
    if req.model:
        override["model"] = req.model
    ok, msg = ctx.test_connection(req.prefix, override=override)
    return {"ok": ok, "message": msg}


@router.get("/config/role-models")
def get_role_models(request: Request):
    ctx = _get_session(request)
    return ctx.get_role_model_config()


@router.post("/config/role-models")
def save_role_models(mapping: dict, request: Request):
    ctx = _get_session(request)
    ctx.set_role_model_config(mapping)
    return {"ok": True}


@router.get("/config/general-model")
def get_general_model_api(request: Request):
    ctx = _get_session(request)
    return {"model": ctx.get_general_model()}


@router.post("/config/general-model")
def save_general_model_api(body: dict, request: Request):
    ctx = _get_session(request)
    model = body.get("model", "claude")
    ctx.set_general_model(model)
    return {"ok": True}


@router.get("/config/usage")
def get_service_usage(request: Request):
    ctx = _get_session(request)
    import httpx

    result: dict[str, dict] = {}

    for prefix, label in [("CLAUDE", "claude"), ("GPT", "gpt")]:
        cfg = ctx.get_config(prefix)
        base_url = cfg["base_url"]
        api_key = cfg["api_key"]
        if not base_url or not api_key:
            continue
        base = base_url.rstrip("/")
        billing_urls = [
            f"{base}/dashboard/billing/credit_grants",
            f"{base}/v1/dashboard/billing/credit_grants",
        ]
        for url in billing_urls:
            try:
                resp = httpx.get(
                    url,
                    headers={"Authorization": f"Bearer {api_key}"},
                    timeout=8,
                )
                if resp.status_code == 200:
                    data = resp.json()
                    balance = data.get("total_available", data.get("balance", None))
                    if balance is not None:
                        result[label] = {"balance_usd": round(float(balance), 2)}
                        break
                    total_granted = data.get("total_granted", 0)
                    total_used = data.get("total_used", 0)
                    if total_granted:
                        result[label] = {"balance_usd": round(float(total_granted) - float(total_used), 2)}
                        break
            except Exception:
                pass

    return result


@router.get("/config/token-stats")
def get_token_stats_route(request: Request):
    ctx = _get_session(request)
    return ctx.get_token_stats()


@router.post("/config/token-stats/reset")
def reset_token_stats_route(request: Request):
    ctx = _get_session(request)
    ctx.reset_token_stats()
    return {"ok": True}


@router.get("/config/role-names")
def get_role_names(request: Request):
    ctx = _get_session(request)
    return ctx.role_names


@router.post("/config/role-names")
def save_role_names(mapping: dict, request: Request):
    ctx = _get_session(request)
    ctx.save_role_names(mapping)
    return {"ok": True, "role_names": ctx.role_names}


@router.get("/reddit-categories")
def reddit_categories():
    return {"categories": REDDIT_CATEGORIES}

# ============================================================
# Fetch routes (returns needs)
# ============================================================

def _run_fetch_job(ctx: SessionContext, req_dict: dict):
    """Run the fetch job in a background thread. All progress goes to ctx.fetch_job."""
    import asyncio as _aio
    from .web_search import reset_tavily_counter, get_tavily_credit_count

    set_thread_session(ctx)

    _loop = _aio.new_event_loop()
    _aio.set_event_loop(_loop)
    def _run(coro):
        return _loop.run_until_complete(coro)

    async def _gather(*coros):
        return await _aio.gather(*coros)

    req = FetchRequest(**req_dict)
    ui_language = _normalize_ui_language(req.language)
    original_fetch_emit = ctx.fetch_emit

    def _localized_fetch_emit(message: str, progress: int):
        original_fetch_emit(_fetch_progress_text(ui_language, message), progress)

    ctx.fetch_emit = _localized_fetch_emit
    if req.mode == "open" and not req.demo:
        # AI 自主挖掘固定走深度能力，并忽略前端残留的赛道选择，确保每轮开放探索。
        req.reddit_categories = []
        req.fetch_model = "deep"

    reset_tavily_counter()
    fetch_strategy = _normalize_fetch_strategy(req.fetch_model)
    deep_fetch = (not req.demo) and fetch_strategy == "deep"
    provider = ctx.get_general_model()
    provider_cfg = ctx.get_config("GPT" if provider == "gpt" else "CLAUDE")
    fetch_model_label = str(provider_cfg.get("model") or provider.upper())
    if deep_fetch:
        ctx.fetch_emit(f"使用深度挖掘策略，模型保持为当前配置：{fetch_model_label}", 1)

    # ===== 预检：LLM 可用性 =====
    if not req.demo:
        ctx.fetch_emit("正在检测模型可用性...", 1)
        llm_ok, llm_err = check_llm_available()
        if not llm_ok:
            model_name = "GPT" if ctx._general_model == "gpt" else "Claude"
            err_msg = llm_err or f"{model_name} 模型不可用，请检查本地模型配置"
            ctx.fetch_emit(err_msg, 100)
            with ctx.fetch_lock:
                ctx.fetch_job["error"] = err_msg
                ctx.fetch_job["active"] = False
            return

        # ===== 预检：数据源 CLI 可用性 =====
        if "reddit" in req.sources:
            ctx.fetch_emit("正在检测 rdt-cli 连接状态...", 1)
            cli_ok, cli_err = _check_cli_available(req.sources)
            if not cli_ok:
                ctx.fetch_emit(cli_err, 100)
                with ctx.fetch_lock:
                    ctx.fetch_job["error"] = cli_err
                    ctx.fetch_job["active"] = False
                return

        # ===== 预检：WebSearch 引擎可用性 =====
        ws_engine = ctx.web_search_engine
        ws_label = {"gpt": "GPT", "tavily": "Tavily", "claude": "Claude"}.get(ws_engine, ws_engine)
        ctx.fetch_emit(f"正在检测 WebSearch（{ws_label}）...", 1)
        ws_ok, ws_err = _check_web_search_available(ctx)
        if not ws_ok:
            ctx.fetch_emit(ws_err, 100)
            with ctx.fetch_lock:
                ctx.fetch_job["error"] = ws_err
                ctx.fetch_job["active"] = False
            return

    _t_total_start = _time.time()
    _timing: dict[str, float] = {}

    def _t_start(phase: str):
        _timing[f"_{phase}_start"] = _time.time()

    def _t_end(phase: str):
        start_key = f"_{phase}_start"
        if start_key in _timing:
            _timing[phase] = round(_time.time() - _timing[start_key], 1)
            del _timing[start_key]

    try:
        # ===== 演示模式：读缓存 + 模拟进度 =====
        if req.demo:
            import time as _time_mod
            _DEMO_DIR = ROOT / "data" / "demo"
            _demo_needs_path = _DEMO_DIR / "demo_needs.json"
            if not _demo_needs_path.exists():
                ctx.fetch_emit("演示数据不存在，请先准备 data/demo/demo_needs.json", 100)
                with ctx.fetch_lock:
                    ctx.fetch_job["error"] = "演示数据文件不存在"
                return

            with open(_demo_needs_path, "r", encoding="utf-8") as f:
                demo_needs = json.load(f)

            _DEMO_STEPS = [
                ("初始化合成演示环境...", 2, 0.8),
                ("加载合成痛点样本...", 5, 0.7),
                ("建立本地演示数据管线...", 8, 0.6),
                ("校验演示数据声明...", 10, 0.6),
                ("规划合成场景...", 14, 1.2),
                ("演示场景：个人信息整理、小团队交接和家庭协作", 18, 0.8),
                ("载入 3 个合成场景、6 条合成帖子", 22, 0.6),
                ("启动本地演示调度器...", 25, 0.8),
                ("正在读取合成帖子和评论...", 30, 1.5),
                ("已读取 6/6 条合成帖子", 35, 1.2),
                ("正在整理 12 条合成评论信号...", 40, 0.8),
                ("合成来源校验完成", 45, 1.2),
                ("正在生成演示热度指标...", 50, 0.6),
                ("正在合并合成证据...", 55, 1.0),
                ("语义去重与排序...", 60, 0.8),
                ("开始结构质量检查（6 个帖子）...", 65, 0.8),
                ("结构检查：6 → 6 个有效帖子", 70, 0.6),
                ("正在关联合成评论与需求...", 75, 1.2),
                ("Lumon 正在演示相关性与机会筛选...", 80, 1.0),
                ("演示筛选完成：保留 6 个有效帖子", 83, 0.6),
                ("分析帖子关联性...", 86, 0.8),
                ("聚类为需求主题...", 90, 1.2),
                (f"产出 {len(demo_needs)} 个需求主题，整理结构...", 94, 0.8),
                ("评估产品机会...", 97, 0.6),
            ]
            for msg, prog, delay in _DEMO_STEPS:
                if ctx.fetch_is_stopped(): return
                ctx.fetch_emit(msg, prog)
                _time_mod.sleep(delay)

            total_posts = sum(len(n.get("posts", [])) for n in demo_needs)
            ctx.fetch_emit(f"挖掘完成！发现 {len(demo_needs)} 个需求主题，共 {total_posts} 个帖子", 100)
            ctx.fetch_emit("⏱ 总用时 20s — 演示模式", 100)

            _safe_json_write(ctx.needs_cache, demo_needs, indent=2)
            ctx.reset_debate()

            with ctx.fetch_lock:
                ctx.fetch_job["needs"] = demo_needs
                ctx.fetch_job["timing"] = {"total": 10.0, "phases": {}}
            return

        # ===== 缓存命中：相同参数 7 天内直接返回历史结果 =====
        _cache_key = _fetch_cache_key(req)
        if _cache_key:
            cached_needs = _fetch_cache_read(_cache_key)
            if cached_needs:
                import time as _time_mod
                _CACHE_STEPS = [
                    ("检测到相同需求的历史挖掘结果，正在加载...", 5, 1.0),
                    ("加载痛点检测模型...", 12, 0.8),
                    ("校验缓存数据完整性...", 25, 1.2),
                    ("还原需求主题结构...", 40, 1.5),
                    ("匹配帖子关联性...", 55, 1.5),
                    ("验证数据时效性...", 70, 1.2),
                    ("整理结构...", 85, 1.0),
                    ("评估产品机会...", 95, 0.8),
                ]
                for msg, prog, delay in _CACHE_STEPS:
                    if ctx.fetch_is_stopped(): return
                    ctx.fetch_emit(msg, prog)
                    _time_mod.sleep(delay)

                total_posts = sum(len(n.get("posts", [])) for n in cached_needs)
                ctx.fetch_emit(f"挖掘完成！发现 {len(cached_needs)} 个需求主题，共 {total_posts} 个帖子", 100)
                ctx.fetch_emit("⏱ 总用时 10s — 缓存加速", 100)

                _safe_json_write(ctx.needs_cache, cached_needs, indent=2)
                ctx.reset_debate()

                with ctx.fetch_lock:
                    ctx.fetch_job["needs"] = cached_needs
                    ctx.fetch_job["timing"] = {"total": 10.0, "phases": {"cache_hit": 10.0}}
                return

        all_posts: list[dict] = []
        second_round_probe_stats: list[dict] = []
        source_names = {"hackernews": "HackerNews", "reddit": "Reddit"}

        fetcher = get_reddit_fetcher()
        try:
            engine_info = _run(init_reddit_fetcher())
            detected_engine = engine_info.get("engine", "unknown")
            print(f"[Fetch] init_reddit_fetcher result: {engine_info}")
        except Exception as e:
            import traceback as _tb_init
            print(f"[Fetch] init_reddit_fetcher EXCEPTION: {e}\n{_tb_init.format_exc()}")
            detected_engine = "unknown"

        engine_name = detected_engine if detected_engine != "unknown" else "rdt-cli"

        if engine_name == "rdt-cli":
            ctx.fetch_emit("Reddit 引擎: rdt-cli", 2)
        elif engine_name == "none":
            ctx.fetch_emit("Reddit 引擎: rdt-cli 未认证，请在设置 > CLI 连接中检查", 2)
        else:
            ctx.fetch_emit(f"Reddit 引擎: {engine_name}（未知状态）", 2)

        fetcher._active_engine = engine_name

        with ctx.fetch_lock:
            ctx.fetch_job["engine"] = engine_name

        if ctx.fetch_is_stopped(): return

        import time as _time_mod
        _TICK = 0.4

        def _emit_slow(msg: str, prog: int, delay: float = _TICK):
            if ctx.fetch_is_stopped(): return
            ctx.fetch_emit(msg, prog)
            _time_mod.sleep(delay)

        _emit_slow("初始化需求识别引擎...", 2)
        _emit_slow("加载痛点检测模型...", 3)
        _emit_slow("建立社区数据管线...", 3)
        _emit_slow("校准信号过滤阈值...", 4)
        _emit_slow("连接数据源...", 4)

        search_queries: list[str] = []
        discovery_queries: list[str] = []
        subreddits: list[str] = []
        topic_for_check = ""

        known_competitors: list[str] = []

        if req.mode in ("sentence", "keywords"):
            user_input = req.query if req.mode == "sentence" else ", ".join(req.keywords)
            topic_for_check = user_input
            _emit_slow("规划搜索策略...", 5)
            if ctx.fetch_is_stopped(): return
            _t_start("search_planning")
            plan = _plan_search(user_input, req)
            _t_end("search_planning")
            if plan:
                search_queries = plan.get("search_queries", [])
                discovery_queries = plan.get("discovery_queries", [])
                subreddits = plan.get("subreddits", [])
                known_competitors = plan.get("known_competitors", [])
                reasoning = plan.get("reasoning", "")
                ctx.fetch_emit(f"搜索策略：{reasoning}", 10)
            else:
                search_queries = [req.query] if req.mode == "sentence" else [k.strip() for k in req.keywords if k.strip()]
                ctx.fetch_emit("使用原始输入进行搜索...", 13)

            # 用户选了 Reddit 子板块分类时，将其 subreddit 注入（优先于 LLM 自动规划）
            if req.reddit_categories:
                user_subs: list[str] = []
                for cat_key in req.reddit_categories:
                    cat = REDDIT_CATEGORIES.get(cat_key, {})
                    user_subs.extend(cat.get("subreddits", []))
                if user_subs:
                    subreddits = list(dict.fromkeys(user_subs + subreddits))
                    ctx.fetch_emit(f"使用指定板块 + LLM 推荐，共 {len(subreddits)} 个社区", 12)

            _emit_slow(f"锁定 {len(search_queries)} 条搜索词、{len(subreddits)} 个社区", 13)
            _emit_slow("启动多源采集调度器...", 14)
        else:
            ctx.fetch_emit("Lumon 正在分析高价值挖掘方向...", 5)
            if ctx.fetch_is_stopped(): return
            categories_json = json.dumps(
                {k: {"label": v["label"], "subreddits": v["subreddits"][:5]}
                 for k, v in REDDIT_CATEGORIES.items()},
                ensure_ascii=False, indent=2,
            )
            exploration_note = _auto_discover_exploration_note()
            category_constraint = f"不要限定在用户之前选择的赛道。{exploration_note}"
            research_context = _build_research_context(req)
            if research_context:
                category_constraint += (
                    "\n\n以下是用户设置的挖掘参数，必须体现在方向选择和搜索词中；"
                    "它们用于约束研究目标，但不代表限定某个 Reddit 赛道。\n"
                    f"{research_context}"
                )
            ctx.fetch_emit(f"自主探索偏置：{exploration_note}", 6)
            discover_prompt = AUTO_DISCOVER_PROMPT.format(
                categories_json=categories_json,
                category_constraint=category_constraint,
            )
            try:
                discover_resp = call_llm([{"role": "user", "content": discover_prompt}])
                discover_plan = _parse_json_from_text(discover_resp)
                if discover_plan and isinstance(discover_plan, dict):
                    directions = discover_plan.get("selected_directions", [])
                    if not isinstance(directions, list) or not directions:
                        raise RuntimeError("自主发现规划未返回有效方向，已停止本轮挖掘")
                    total_reason = discover_plan.get("total_reasoning", "")
                    ctx.fetch_emit(f"发现 {len(directions)} 个方向：{total_reason}", 12)
                    for d in directions:
                        if not isinstance(d, dict) or not str(d.get("direction") or "").strip():
                            continue
                        direction_queries = d.get("search_queries", [])
                        direction_subreddits = d.get("subreddits", [])
                        if isinstance(direction_queries, list):
                            search_queries.extend(str(q).strip() for q in direction_queries if str(q).strip())
                        if isinstance(direction_subreddits, list):
                            subreddits.extend(str(s).strip() for s in direction_subreddits if str(s).strip())
                        topic_for_check = d.get("direction", topic_for_check)
                    if not topic_for_check or not search_queries or not subreddits:
                        raise RuntimeError("自主发现规划缺少方向、搜索词或社区，已停止本轮挖掘")
                    _emit_slow(f"锁定 {len(subreddits)} 个社区、{len(search_queries)} 条搜索词", 13)
                    _emit_slow("启动多源采集调度器...", 14)
                else:
                    print(f"[AutoDiscover] JSON parse failed. Raw: {discover_resp[:500]}")
                    raise RuntimeError("自主发现规划未返回有效 JSON，已停止本轮挖掘")
            except Exception as e:
                print(f"[AutoDiscover] LLM error: {e}")
                err_msg = str(e)
                ctx.fetch_emit(err_msg, 100)
                with ctx.fetch_lock:
                    ctx.fetch_job["error"] = err_msg
                return

        if ctx.fetch_is_stopped(): return

        _time_map_local = {"month": "month", "3months": "year", "6months": "year", "9months": "all"}
        rdt_time_filter = _time_map_local.get(req.time_period, "year")

        # ========== Phase A: WebSearch 精准 URL 发现 ==========
        _t_start("websearch_discovery")
        # 根据用户设置选择搜索引擎：gpt / tavily / claude
        # 核心机制：通过 Web 语义搜索发现最相关的 Reddit 帖子，
        # 然后用 rdt read 提取全文和深层评论。同时动态发现新 subreddit。
        discovered_subs: set[str] = set()

        if "reddit" in req.sources and topic_for_check and search_queries:
            ws_engine = ctx.web_search_engine
            ws_engine_label = {"gpt": "GPT", "tavily": "Tavily", "claude": "Claude"}.get(ws_engine, ws_engine)
            ctx.fetch_emit(f"{ws_engine_label} WebSearch 正在发现高质量 Reddit 帖子...", 15)
            try:
                if ws_engine == "gpt":
                    discovered, new_subs = gpt_discover_reddit_urls(
                        topic=topic_for_check,
                        search_queries=search_queries[:10],
                        subreddits=subreddits[:6] if subreddits else None,
                        discovery_queries=discovery_queries if discovery_queries else None,
                        progress_callback=lambda msg: ctx.fetch_emit(msg, 18),
                    )
                    discovered_subs.update(new_subs)
                elif ws_engine == "claude":
                    discovered, new_subs = claude_discover_reddit_urls(
                        topic=topic_for_check,
                        search_queries=search_queries[:10],
                        subreddits=subreddits[:6] if subreddits else None,
                        discovery_queries=discovery_queries if discovery_queries else None,
                        progress_callback=lambda msg: ctx.fetch_emit(msg, 18),
                    )
                    discovered_subs.update(new_subs)
                else:
                    # Tavily
                    discovered = discover_reddit_urls(
                        topic=topic_for_check,
                        search_queries=search_queries[:6],
                        subreddits=subreddits[:4] if subreddits else None,
                        discovery_queries=discovery_queries if discovery_queries else None,
                        progress_callback=lambda msg: ctx.fetch_emit(msg, 18),
                    )
                    import re as _re
                    _sub_pat = _re.compile(r'reddit\.com/r/(\w+)')
                    for d in discovered:
                        m = _sub_pat.search(d.get("url", ""))
                        if m:
                            discovered_subs.add(m.group(1))

                if discovered:
                    ctx.fetch_emit(f"WebSearch 发现 {len(discovered)} 个帖子，rdt read 并发提取全文+评论...", 20)
                    ws_posts = []
                    disc_batch = discovered[:45]

                    import asyncio as _aio_ws
                    _WS_READ_BATCH = 3 if deep_fetch else 5
                    for batch_start in range(0, len(disc_batch), _WS_READ_BATCH):
                        if ctx.fetch_is_stopped(): return
                        batch = disc_batch[batch_start:batch_start + _WS_READ_BATCH]

                        async def _read_one(disc_item):
                            try:
                                return await fetcher.read_post(disc_item["post_id"])
                            except Exception as e:
                                print(f"[WebSearch→rdt read] {disc_item['post_id']} failed: {e}")
                                return None

                        results = _run(_gather(*[_read_one(d) for d in batch]))
                        for detail in results:
                            if detail and detail.get("title"):
                                detail["_discovery_source"] = "websearch"
                                ws_posts.append(detail)
                                src = detail.get("source", "")
                                if src.startswith("reddit/"):
                                    discovered_subs.add(src.split("/", 1)[1])
                        ctx.fetch_emit(f"已提取 {len(ws_posts)}/{len(disc_batch)} 个帖子全文", 20 + int(10 * (batch_start + len(batch)) / len(disc_batch)))

                    if ws_posts:
                        all_posts.extend(ws_posts)
                        ctx.fetch_emit(f"WebSearch 贡献 {len(ws_posts)} 个高质量帖子（含深层评论）", 30)
                    else:
                        ctx.fetch_emit("WebSearch 未提取到有效帖子", 30)
                else:
                    ctx.fetch_emit("WebSearch 未发现相关帖子", 30)
            except Exception as e:
                import traceback as _tb_ws
                print(f"[WebSearch {ws_engine}] ERROR: {e}\n{_tb_ws.format_exc()}")
                ctx.fetch_emit("WebSearch 跳过，请检查本地 WebSearch 配置", 30)

        _t_end("websearch_discovery")

        # 动态 subreddit 合并（最多追加 8 个新发现的 sub），先做轻量相关性守门，避免明显跑偏社区污染搜索。
        original_sub_set = set(subreddits)
        subreddit_query_context = list(search_queries or [])
        if discovery_queries:
            subreddit_query_context.extend(discovery_queries)
        original_subreddits_for_guard = list(subreddits)

        def _accept_discovered_subreddit(sub_name: str) -> tuple[bool, str]:
            return _is_relevant_discovered_subreddit(
                sub_name,
                topic=topic_for_check,
                search_queries=subreddit_query_context,
                original_subreddits=original_subreddits_for_guard,
            )

        new_subs_to_add: list[str] = []
        rejected_subs: list[tuple[str, str]] = []
        for sub_name in sorted(discovered_subs, key=lambda s: s.lower()):
            if sub_name in original_sub_set:
                continue
            keep_sub, reject_reason = _accept_discovered_subreddit(sub_name)
            if keep_sub:
                if len(new_subs_to_add) < 8:
                    new_subs_to_add.append(sub_name)
            else:
                rejected_subs.append((sub_name, reject_reason))
        if new_subs_to_add:
            subreddits.extend(new_subs_to_add)
            ctx.fetch_emit(f"动态发现 {len(new_subs_to_add)} 个新 subreddit: {', '.join(new_subs_to_add)}", 31)
        if rejected_subs:
            rejected_preview = ", ".join(s for s, _ in rejected_subs[:3])
            if len(rejected_subs) > 3:
                rejected_preview += " 等"
            print(f"[SubredditGuard] rejected={rejected_subs[:12]}")
            ctx.fetch_emit(f"已过滤 {len(rejected_subs)} 个跑偏 subreddit: {rejected_preview}", 31)

        if ctx.fetch_is_stopped(): return

        # ========== Phase B: rdt search / HN 关键词补充 ==========
        print(f"[Fetch] Phase B start: sources={req.sources}, queries={search_queries[:5]}, subs={subreddits[:10]}, all_posts_from_A={len(all_posts)}, engine={engine_name}")
        _t_start("keyword_search")
        total_sources = len(req.sources)
        per_source = max(req.limit // total_sources, 15) if total_sources else req.limit

        src_done = 0
        for src in req.sources:
            if ctx.fetch_is_stopped(): return
            src_label = source_names.get(src, src)
            base_progress = 32 + src_done * 15
            ctx.fetch_emit(f"正在补充搜索 {src_label}...", base_progress)

            try:
                if src == "hackernews":
                    if req.mode == "open" and search_queries:
                        hn_posts: list[dict] = []
                        hn_queries = list(search_queries[:8])
                        if discovery_queries:
                            hn_queries.extend(discovery_queries[:4])
                        per_hn_q = max(per_source // max(len(hn_queries), 1), 5)
                        for q in hn_queries:
                            if ctx.fetch_is_stopped(): return
                            hn_posts.extend(search_hackernews(q, per_hn_q, time_period=req.time_period))
                        posts = hn_posts
                    elif req.mode == "open":
                        posts = fetch_hackernews(req.category, per_source)
                    else:
                        hn_posts: list[dict] = []
                        # HN Algolia 支持语义搜索，同时用短词和自然语言查询
                        hn_queries = list(search_queries[:8])
                        if discovery_queries:
                            hn_queries.extend(discovery_queries[:6])
                        per_hn_q = max(per_source // max(len(hn_queries), 1), 5)
                        for q in hn_queries:
                            if ctx.fetch_is_stopped(): return
                            hn_posts.extend(search_hackernews(q, per_hn_q, time_period=req.time_period))
                        posts = hn_posts
                elif src == "reddit":
                    target_subs: list[str] = []
                    if req.reddit_categories:
                        for cat_key in req.reddit_categories:
                            cat = REDDIT_CATEGORIES.get(cat_key, {})
                            target_subs.extend(cat.get("subreddits", []))
                    elif subreddits:
                        target_subs = subreddits[:20]

                    reddit_posts: list[dict] = []
                    queries = search_queries[:10] if search_queries else []

                    if target_subs:
                        import asyncio as _aio_b
                        per_sub = max(per_source // max(len(target_subs[:20]), 1), 5)
                        _ADAPTIVE_THRESHOLD = 75 if deep_fetch else 50
                        _SUFFICIENT_THRESHOLD = 120 if deep_fetch else 85
                        _INITIAL_SUBS = 8 if deep_fetch else 5

                        async def _search_one_sub(sub_name: str, q_list: list[str]):
                            """并发搜索单个 sub 的多个 query"""
                            results: list[dict] = []
                            for q in q_list:
                                try:
                                    sp = await fetcher.search(
                                        query=q, subreddit=sub_name,
                                        sort="top" if not q else "relevance",
                                        time_filter=rdt_time_filter,
                                        limit=per_sub,
                                    )
                                    results.extend(sp)
                                    if sp:
                                        print(f"[Reddit] {sub_name}/{q[:30]}: {len(sp)} posts")
                                except Exception as e:
                                    print(f"[Reddit] {sub_name}/{q[:30]} FAILED: {e}")
                            return sub_name, results

                        subs_to_search = target_subs[:15 if deep_fetch else 10]
                        phase1_subs = subs_to_search[:_INITIAL_SUBS]
                        phase2_subs = subs_to_search[_INITIAL_SUBS:]
                        full_queries = queries[:8 if deep_fetch else 5] if queries else [""]

                        # Phase B-1: 前 5 个 sub 并发搜索
                        ctx.fetch_emit(f"并发搜索前 {len(phase1_subs)} 个核心 subreddit...", base_progress + 2)
                        phase1_results = _run(_gather(*[
                            _search_one_sub(s, full_queries) for s in phase1_subs
                        ]))
                        for sub_name, sub_batch in phase1_results:
                            if sub_batch:
                                reddit_posts.extend(sub_batch)
                                for sp in sub_batch:
                                    sp_src = sp.get("source", "")
                                    if sp_src.startswith("reddit/"):
                                        new_sub = sp_src.split("/", 1)[1]
                                        if new_sub not in original_sub_set and new_sub not in set(subs_to_search):
                                            keep_sub, reject_reason = _accept_discovered_subreddit(new_sub)
                                            if keep_sub:
                                                discovered_subs.add(new_sub)
                                            else:
                                                print(f"[SubredditGuard] skip phaseB sub={new_sub} reason={reject_reason}")
                        ctx.fetch_emit(f"核心 sub 搜索完成：{len(reddit_posts)} 个帖子", base_progress + 7)

                        # Phase B-2: 自适应搜索剩余 sub
                        if ctx.fetch_is_stopped(): return
                        if len(reddit_posts) >= _SUFFICIENT_THRESHOLD:
                            ctx.fetch_emit(f"已采集 {len(reddit_posts)} 个帖子（充足），跳过剩余 {len(phase2_subs)} 个 sub", base_progress + 10)
                        elif phase2_subs:
                            reduced_queries = full_queries[:3] if len(reddit_posts) >= _ADAPTIVE_THRESHOLD else full_queries
                            mode_label = "精简" if len(reduced_queries) < len(full_queries) else "完整"
                            ctx.fetch_emit(f"已采集 {len(reddit_posts)} 个帖子，{mode_label}模式搜索剩余 {len(phase2_subs)} 个 sub...", base_progress + 8)

                            _SUB_BATCH_SIZE = 3 if deep_fetch else 5
                            for sb_start in range(0, len(phase2_subs), _SUB_BATCH_SIZE):
                                if ctx.fetch_is_stopped(): return
                                if len(reddit_posts) >= _SUFFICIENT_THRESHOLD:
                                    ctx.fetch_emit(f"已采集 {len(reddit_posts)} 个帖子（充足），停止搜索", base_progress + 12)
                                    break
                                sb = phase2_subs[sb_start:sb_start + _SUB_BATCH_SIZE]
                                batch_results = _run(_gather(*[
                                    _search_one_sub(s, reduced_queries) for s in sb
                                ]))
                                for sub_name, sub_batch in batch_results:
                                    if sub_batch:
                                        reddit_posts.extend(sub_batch)
                                        for sp in sub_batch:
                                            sp_src = sp.get("source", "")
                                            if sp_src.startswith("reddit/"):
                                                new_sub = sp_src.split("/", 1)[1]
                                                if new_sub not in original_sub_set and new_sub not in set(subs_to_search):
                                                    keep_sub, reject_reason = _accept_discovered_subreddit(new_sub)
                                                    if keep_sub:
                                                        discovered_subs.add(new_sub)
                                                    else:
                                                        print(f"[SubredditGuard] skip phaseB sub={new_sub} reason={reject_reason}")
                                ctx.fetch_emit(f"补充搜索：累计 {len(reddit_posts)} 个帖子", base_progress + 10 + int(5 * (sb_start + len(sb)) / len(phase2_subs)))

                        ctx.fetch_emit(f"Reddit 搜索完成：共 {len(reddit_posts)} 个帖子", base_progress + 15)
                    elif req.mode == "open":
                        reddit_posts = _run(fetcher.search("", sort="top", time_filter=rdt_time_filter, limit=per_source))
                    else:
                        import asyncio as _aio_fb
                        fallback_subs = subreddits[:8] if subreddits else []
                        if fallback_subs:
                            per_fb = max(per_source // max(len(fallback_subs), 1), 5)
                            async def _fb_search(fb_sub, q):
                                try:
                                    return await fetcher.search(q, subreddit=fb_sub, sort="relevance", time_filter=rdt_time_filter, limit=per_fb)
                                except Exception:
                                    return []
                            tasks = [_fb_search(fb_sub, q) for fb_sub in fallback_subs for q in queries[:3]]
                            fb_results = _run(_gather(*tasks))
                            for r in fb_results:
                                reddit_posts.extend(r)
                        else:
                            for q in queries[:5]:
                                if ctx.fetch_is_stopped(): return
                                sub_posts = _run(fetcher.search(q, sort="relevance", time_filter=rdt_time_filter, limit=per_source // max(len(queries[:5]), 1)))
                                reddit_posts.extend(sub_posts)
                    posts = reddit_posts
                else:
                    posts = []

                all_posts.extend(posts)
                print(f"[Fetch] {src_label}: {len(posts)} posts collected")
                ctx.fetch_emit(f"{src_label}: 已发现 {len(posts)} 个帖子", base_progress + 18)
            except Exception as e:
                print(f"[Fetch] {src_label} ERROR: {e}")
                ctx.fetch_emit(f"{src_label} 采集出错，请检查本机 CLI 登录状态和网络", base_progress + 18)
            src_done += 1

        if ctx.fetch_is_stopped(): return

        _t_end("keyword_search")
        if ctx.fetch_is_stopped(): return

        # 自动扩展：初始采集不足时，用 discovery_queries 做更广泛搜索
        if len(all_posts) < 35 and engine_name == "rdt-cli" and req.mode != "open":
            expand_queries = []
            if discovery_queries:
                for dq in discovery_queries[:6]:
                    words = dq.split()
                    if len(words) >= 3:
                        expand_queries.append(" ".join(words[:3]))
            if not expand_queries and search_queries:
                expand_queries = search_queries[5:10]
            expand_subs = subreddits[12:20] if len(subreddits) > 12 else subreddits[:5]

            if expand_queries and expand_subs:
                ctx.fetch_emit(f"初始数据不足（{len(all_posts)} 条），正在扩展搜索...", 46)
                for exp_sub in expand_subs[:6]:
                    if ctx.fetch_is_stopped(): return
                    for eq in expand_queries[:4]:
                        try:
                            exp_posts = _run(fetcher.search(
                                query=eq, subreddit=exp_sub,
                                sort="relevance", time_filter=rdt_time_filter,
                                limit=15,
                            ))
                            all_posts.extend(exp_posts)
                        except Exception:
                            pass
                ctx.fetch_emit(f"扩展搜索后：共 {len(all_posts)} 条帖子", 48)

        if ctx.fetch_is_stopped(): return

        seen_titles: set[str] = set()
        seen_content: set[str] = set()
        deduped: list[dict] = []
        for p in all_posts:
            title_key = p["title"].lower().strip()
            content_key = (p.get("content", "") or "")[:120].lower().strip()
            if title_key in seen_titles:
                continue
            if content_key and len(content_key) > 50 and content_key in seen_content:
                continue
            seen_titles.add(title_key)
            if content_key and len(content_key) > 50:
                seen_content.add(content_key)
            deduped.append(p)
        # 先做精确时间范围过滤，再截取 top N，避免超时帖子占用名额
        _period_days = {"month": 30, "3months": 90, "6months": 183, "9months": 270}
        max_age_days = _period_days.get(req.time_period, 183)
        cutoff_ts = _time.time() - max_age_days * 86400
        before_time_filter = len(deduped)
        deduped = [p for p in deduped if (p.get("created_utc") or 0) == 0 or p["created_utc"] >= cutoff_ts]
        dropped = before_time_filter - len(deduped)
        if dropped:
            print(f"[TimeFilter] {req.time_period}（{max_age_days}天） → 移除 {dropped} 条超时帖子")
            ctx.fetch_emit(f"时间范围过滤：移除 {dropped} 条超出 {req.time_period} 范围的帖子", 52)

        annotate_posts_with_opportunity(deduped)
        heat_eligible = [p for p in deduped if p.get("passes_heat_gate")]
        min_heat_keep = min(10, max(3, req.limit // 4))
        if len(heat_eligible) >= min_heat_keep:
            ranked_source = heat_eligible
            ctx.fetch_emit(f"热度门槛过滤：{len(deduped)} → {len(heat_eligible)} 个有共鸣帖子", 52)
        else:
            ranked_source = deduped
            ctx.fetch_emit(f"热度门槛命中较少（{len(heat_eligible)} 个），保留完整候选池继续判断", 52)

        ranked_source.sort(
            key=lambda p: (
                float(p.get("opportunity_score") or 0),
                float(p.get("comment_read_score") or 0),
                int(p.get("score") or 0),
            ),
            reverse=True,
        )
        candidate_limit = min(len(ranked_source), max(req.limit, min(req.limit * 2, 70)))
        deduped = ranked_source[:candidate_limit]

        raw_count = len(deduped)
        if raw_count == 0:
            _src_hints = []
            if "reddit" in req.sources:
                if engine_name == "none":
                    _src_hints.append("rdt-cli 未认证 → 前往「设置 → CLI 连接」检查")
                else:
                    _src_hints.append("Reddit 未搜到结果，试试更换关键词或扩大时间范围")
            if "hackernews" in req.sources:
                _src_hints.append("HackerNews 未返回结果")
            _hint = "；".join(_src_hints) if _src_hints else "所选数据源均未返回结果，请更换关键词或检查网络"
            print(f"[Fetch] 0 posts. engine={engine_name}, sources={req.sources}, queries={search_queries[:5]}, subs={subreddits[:5]}")
            ctx.fetch_emit(f"未采集到帖子：{_hint}", 100)
            with ctx.fetch_lock:
                ctx.fetch_job["error"] = f"未采集到帖子：{_hint}"
            return

        ctx.fetch_emit(f"采集完成，共 {raw_count} 个帖子", 52)
        _emit_slow("语义去重与排序...", 53)
        _emit_slow(f"开始质量筛选（{raw_count} 个帖子）...", 55)

        if req.mode == "open":
            hard_filtered = [p for p in deduped if p.get("score", 0) >= 2 or p.get("num_comments", 0) >= 2]
            if len(hard_filtered) < 5:
                hard_filtered = deduped
        else:
            hard_filtered = [p for p in deduped if hard_filter(p)]
            if len(hard_filtered) < 3:
                hard_filtered = deduped
        ctx.fetch_emit(f"硬性门槛过滤：{raw_count} → {len(hard_filtered)} 个帖子", 60)

        if deep_fetch and hard_filtered:
            ctx.fetch_emit(f"正在用当前模型 {fetch_model_label} 筛选评论深读优先级...", 60)
            hard_filtered = _prioritize_comment_reads_with_model(
                hard_filtered,
                topic_for_check,
                fetch_model_label,
                target_reads=24,
            )
            ctx.fetch_emit("rdt 限额保护：评论读取前冷却 6 秒...", 61)
            _time_mod.sleep(6)

        if ctx.fetch_is_stopped(): return

        # 评论充实：并发拉取高分帖子的完整评论（2-3 层深度）
        _t_start("comment_enrichment")
        if engine_name == "rdt-cli" and hard_filtered:
            enrichable = [p for p in hard_filtered
                          if p.get("_post_id") and len(p.get("comments") or []) < 3]
            enrichable.sort(
                key=lambda p: (
                    float(p.get("ai_read_priority") or 0),
                    float(p.get("comment_read_score") or 0),
                    float(p.get("opportunity_score") or 0),
                    int(p.get("num_comments") or 0),
                    int(p.get("score") or 0),
                ),
                reverse=True,
            )
            enrich_limit = min(len(enrichable), 30 if deep_fetch else 12)
            if enrich_limit > 0:
                import asyncio as _aio_enrich
                _ENRICH_BATCH = 2 if deep_fetch else 3
                _ENRICH_COMMENT_TARGET = 120 if deep_fetch else 60
                _ENRICH_MIN_ATTEMPTED = min(enrich_limit, 12 if deep_fetch else 6)
                _ENRICH_EMPTY_BATCH_LIMIT = 4 if deep_fetch else 2
                ctx.fetch_emit(f"拉取 {enrich_limit} 个帖子的深层评论...", 62)
                enriched_count = 0
                total_comments_collected = 0
                consecutive_empty_batches = 0

                for batch_start in range(0, enrich_limit, _ENRICH_BATCH):
                    if ctx.fetch_is_stopped(): return
                    if total_comments_collected >= _ENRICH_COMMENT_TARGET and enriched_count >= 8:
                        ctx.fetch_emit(f"评论充实已足够（{enriched_count} 帖 / {total_comments_collected} 条评论），停止", 64)
                        break
                    if consecutive_empty_batches >= _ENRICH_EMPTY_BATCH_LIMIT and batch_start >= _ENRICH_MIN_ATTEMPTED:
                        ctx.fetch_emit(f"连续 {consecutive_empty_batches} 批无结果，跳过剩余评论充实", 64)
                        break
                    batch = enrichable[batch_start:batch_start + _ENRICH_BATCH]

                    async def _enrich_one(post):
                        try:
                            return post, await fetcher.read_post(post["_post_id"])
                        except Exception as e:
                            print(f"[Enrich] read_post {post.get('_post_id')} failed: {e}")
                            return post, None

                    results = _run(_gather(*[_enrich_one(p) for p in batch]))
                    prev_enriched = enriched_count
                    for post_ref, detail in results:
                        if detail and detail.get("comments"):
                            post_ref["comments"] = detail["comments"][:35]
                            if detail.get("_comment_meta"):
                                post_ref["_comment_meta"] = detail["_comment_meta"][:35]
                            if detail.get("content") and len(detail["content"]) > len(post_ref.get("content", "")):
                                post_ref["content"] = detail["content"]
                            enriched_count += 1
                            total_comments_collected += len(post_ref["comments"])
                    if enriched_count > prev_enriched:
                        consecutive_empty_batches = 0
                        ctx.fetch_emit(f"评论充实进度：{enriched_count} 帖 / {total_comments_collected} 条评论", 62 + int(3 * (batch_start + len(batch)) / enrich_limit))
                    else:
                        consecutive_empty_batches += 1

                ctx.fetch_emit(f"评论充实完成：{enriched_count}/{enrich_limit} 帖，共 {total_comments_collected} 条评论", 65)

        _t_end("comment_enrichment")
        if ctx.fetch_is_stopped(): return

        # 评论充实后重新计算机会分，避免新评论信号没有参与聚类前排序。
        annotate_posts_with_opportunity(hard_filtered)
        hard_filtered.sort(
            key=lambda p: (
                float(p.get("opportunity_score") or 0),
                float(p.get("comment_read_score") or 0),
                int(p.get("score") or 0),
            ),
            reverse=True,
        )

        if deep_fetch and hard_filtered:
            ctx.fetch_emit(f"正在用当前模型 {fetch_model_label} 提取帖子证据...", 70)
            hard_filtered = _extract_evidence_with_model(
                hard_filtered,
                topic_for_check,
                fetch_model_label,
            )

        if deep_fetch and engine_name == "rdt-cli" and "reddit" in req.sources and hard_filtered:
            _t_start("evidence_probe_search")
            max_probe_count = 1 if len(hard_filtered) >= 55 else 3
            probes = _build_evidence_search_probes(
                hard_filtered,
                topic_for_check,
                search_queries + discovery_queries,
                known_competitors,
                subreddits,
                max_probes=max_probe_count,
            )
            if probes:
                planned_searches = sum(max(1, min(len(p.get("subreddits") or []), 2)) for p in probes)
                planned_searches = min(planned_searches, 6)
                ctx.fetch_emit(f"证据驱动二轮补搜：{len(probes)} 个探针，最多 {planned_searches} 次 rdt search", 72)
                ctx.fetch_emit("rdt 限额保护：二轮补搜前冷却 4 秒...", 72)
                _time_mod.sleep(4)
                extra_posts, second_round_probe_stats = _run(_run_evidence_probe_search(
                    fetcher,
                    probes,
                    hard_filtered,
                    time_filter=rdt_time_filter,
                    cutoff_ts=cutoff_ts,
                    req_mode=req.mode,
                    max_searches=6,
                    limit_per_search=5,
                    max_extra_posts=12,
                    read_limit=2,
                ))
                if extra_posts:
                    hard_filtered.extend(extra_posts)
                    annotate_posts_with_opportunity(hard_filtered)
                    hard_filtered.sort(
                        key=lambda p: (
                            float(p.get("opportunity_score") or 0),
                            float(p.get("comment_read_score") or 0),
                            int(p.get("score") or 0),
                        ),
                        reverse=True,
                    )
                    hard_filtered = hard_filtered[:70]
                    ctx.fetch_emit(f"二轮补搜新增 {len(extra_posts)} 个有热度帖子，进入聚类验证", 74)
                else:
                    ctx.fetch_emit("二轮补搜未新增符合热度门槛的帖子，继续使用第一轮证据", 74)
            else:
                ctx.fetch_emit("第一轮证据未形成稳定追问探针，跳过二轮补搜", 74)
            _t_end("evidence_probe_search")

        # 过滤已合并到两步聚类的 Step1 中，不再单独调用 _filter_posts
        filtered = hard_filtered
        ctx.fetch_emit(f"共 {len(filtered)} 个帖子进入聚类（过滤 + 分组一步完成）", 75)

        if ctx.fetch_is_stopped(): return

        if not filtered:
            ctx.fetch_emit("未采集到有效帖子，请尝试更换关键词或数据源", 100)
            with ctx.fetch_lock:
                ctx.fetch_job["error"] = "未采集到有效帖子，请尝试更换关键词、扩大时间范围或切换数据源"
            return

        _t_start("clustering")
        needs = _cluster_posts_into_needs(filtered, topic=topic_for_check)
        _t_end("clustering")

        # 把用户原始搜索主题注入每个 need，报告生成时用它做主题锚定
        for n in needs:
            n["original_topic"] = topic_for_check

        valid_needs = annotate_needs_with_opportunity([
            n for n in needs
            if n.get("posts") and len(n["posts"]) > 0
        ])

        if not valid_needs:
            err_msg = f"采集到 {len(filtered)} 个帖子但未归纳出需求主题，建议更换关键词、选更具体的赛道或扩大时间范围"
            ctx.fetch_emit(err_msg, 100)
            with ctx.fetch_lock:
                ctx.fetch_job["error"] = err_msg
            return

        if deep_fetch:
            ctx.fetch_emit(f"正在用当前模型 {fetch_model_label} 校验需求聚类...", 88)
            valid_needs = _refine_need_groups_with_model(
                valid_needs,
                topic_for_check,
                fetch_model_label,
            )
            ctx.fetch_emit(f"正在用当前模型 {fetch_model_label} 做机会二审...", 90)
            valid_needs = _review_needs_with_model(
                valid_needs,
                topic_for_check,
                fetch_model_label,
            )

        valid_needs = _attach_second_round_metadata(valid_needs, second_round_probe_stats)
        valid_needs = _attach_evidence_bundles(valid_needs)
        valid_needs = _attach_market_validation(
            valid_needs,
            topic=topic_for_check,
            known_competitors=known_competitors,
            search_queries=search_queries + discovery_queries,
            market_region="US",
            max_needs=5 if deep_fetch else 3,
        )

        _safe_json_write(ctx.needs_cache, valid_needs, indent=2)
        if _cache_key:
            try:
                _fetch_cache_write(_cache_key, valid_needs, req)
            except Exception as _ce:
                print(f"[FetchCache] 写入缓存失败: {_ce}")
        ctx.reset_debate()

        total_posts = sum(len(n["posts"]) for n in valid_needs)
        _emit_slow(f"产出 {len(valid_needs)} 个需求主题，整理结构...", 92)
        _emit_slow("评估产品机会...", 95)
        tavily_credits = get_tavily_credit_count()
        total_elapsed = round(_time.time() - _t_total_start, 1)

        def _fmt_duration(secs: float) -> str:
            s = int(secs)
            if s < 60:
                return f"{s}s"
            return f"{s // 60}m{s % 60:02d}s"

        phase_labels = {
            "search_planning": _ui_text(ui_language, "搜索规划", "search planning"),
            "websearch_discovery": _ui_text(ui_language, "WebSearch发现", "WebSearch discovery"),
            "keyword_search": _ui_text(ui_language, "关键词搜索", "keyword search"),
            "comment_enrichment": _ui_text(ui_language, "评论充实", "comment enrichment"),
            "evidence_probe_search": _ui_text(ui_language, "证据补搜", "evidence probe search"),
            "quality_filter": _ui_text(ui_language, "质量筛选", "quality filtering"),
            "clustering": _ui_text(ui_language, "需求聚类", "demand clustering"),
        }
        timing_parts = []
        for key in ["search_planning", "websearch_discovery", "keyword_search", "comment_enrichment", "evidence_probe_search", "quality_filter", "clustering"]:
            if key in _timing:
                timing_parts.append(f"{phase_labels.get(key, key)} {_fmt_duration(_timing[key])}")
        timing_str = " | ".join(timing_parts)

        ctx.fetch_emit(f"挖掘完成！发现 {len(valid_needs)} 个需求主题，共 {total_posts} 个帖子", 100)
        ctx.fetch_emit(f"⏱ 总用时 {_fmt_duration(total_elapsed)} — {timing_str}", 100)
        print(f"[Fetch Job Done] Total: {_fmt_duration(total_elapsed)} | {timing_str} | Tavily credits: {tavily_credits}")

        with ctx.fetch_lock:
            ctx.fetch_job["needs"] = valid_needs
            ctx.fetch_job["timing"] = {"total": total_elapsed, "phases": dict(_timing)}

    except Exception as e:
        _log_sse_error("Fetch", e, ctx)
        with ctx.fetch_lock:
            ctx.fetch_job["error"] = _friendly_error_for_language(ui_language, e)
    finally:
        fetcher = get_reddit_fetcher()
        fetcher.force_engine = None
        ctx.fetch_emit = original_fetch_emit
        with ctx.fetch_lock:
            ctx.fetch_job["active"] = False
        try:
            _loop.close()
        except Exception:
            pass


@router.post("/fetch")
def fetch_posts(req: FetchRequest, request: Request):
    ctx = _get_session(request)
    ui_language = _normalize_ui_language(req.language)
    with ctx.fetch_lock:
        if ctx.fetch_job["active"]:
            if ctx.fetch_job["stop_requested"]:
                ctx.fetch_job["active"] = False
            else:
                raise HTTPException(status_code=409, detail="已有挖掘任务进行中")
        ctx.fetch_job.update({
            "active": True,
            "stop_requested": False,
            "progress": 0,
            "history": [
                _fetch_progress_text(ui_language, "准备开始挖掘..."),
                _fetch_progress_text(ui_language, "正在连接数据源..."),
            ],
            "error": "",
            "needs": None,
            "engine": "",
            "clustering_fallback": False,
        })
    if ctx.fetch_thread and ctx.fetch_thread.is_alive():
        ctx.fetch_thread.join(timeout=5)
    if ctx.needs_cache.exists():
        try:
            ctx.needs_cache.unlink()
        except Exception:
            pass

    t = threading.Thread(target=_run_fetch_job, args=(ctx, req.model_dump()), daemon=True)
    ctx.fetch_thread = t
    t.start()

    def event_stream() -> Generator[str, None, None]:
        sent_idx = 0
        while True:
            _time.sleep(0.3)
            with ctx.fetch_lock:
                active = ctx.fetch_job["active"]
                stopped = ctx.fetch_job["stop_requested"]
                history = list(ctx.fetch_job["history"])
                progress = ctx.fetch_job["progress"]
                error = ctx.fetch_job["error"]
                needs = ctx.fetch_job["needs"]

            if stopped and not needs and not error:
                yield _sse("error", {"message": _ui_text(ui_language, "挖掘已停止", "Mining stopped")})
                yield _sse("done", {})
                return

            new_messages = history[sent_idx:]
            for msg in new_messages:
                yield _sse("fetch_progress", {"message": msg, "progress": progress})
            sent_idx = len(history)

            if error:
                yield _sse("error", {"message": _fetch_progress_text(ui_language, error)})
                yield _sse("done", {})
                return

            if needs is not None:
                yield _sse("fetch_result", {
                    "needs": needs,
                    "count": len(needs),
                    "engine": ctx.fetch_job.get("engine", ""),
                    "timing": ctx.fetch_job.get("timing"),
                })
                yield _sse("done", {})
                return

            if not active:
                yield _sse("done", {})
                return

            # Keepalive: prevent Cloudflare / reverse proxy idle timeout
            if not new_messages:
                yield ": keepalive\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream")


@router.get("/fetch/status")
def fetch_status(request: Request, language: str = UI_LANGUAGE_ZH):
    ctx = _get_session(request)
    ui_language = _normalize_ui_language(language)
    with ctx.fetch_lock:
        return {
            "active": ctx.fetch_job["active"],
            "progress": ctx.fetch_job["progress"],
            "history": [_fetch_progress_text(ui_language, msg) for msg in list(ctx.fetch_job["history"])],
            "error": _fetch_progress_text(ui_language, ctx.fetch_job["error"]),
            "needs": ctx.fetch_job["needs"],
            "engine": ctx.fetch_job.get("engine", ""),
            "timing": ctx.fetch_job.get("timing"),
        }


@router.post("/fetch/stop")
def fetch_stop(request: Request):
    ctx = _get_session(request)
    with ctx.fetch_lock:
        ctx.fetch_job["stop_requested"] = True
    return {"ok": True}


@router.get("/needs")
def get_needs(request: Request):
    ctx = _get_session(request)
    with ctx.fetch_lock:
        if ctx.fetch_job["active"]:
            return {"needs": []}
    if ctx.needs_cache.exists():
        try:
            with open(ctx.needs_cache, "r", encoding="utf-8") as f:
                raw = json.load(f)
            return {"needs": _normalize_needs_list(raw)}
        except Exception:
            pass
    return {"needs": []}


class SyncNeedsRequest(BaseModel):
    needs: list


@router.put("/needs")
def sync_needs(req: SyncNeedsRequest, request: Request):
    ctx = _get_session(request)
    normalized = _normalize_needs_list(req.needs)
    _safe_json_write(ctx.needs_cache, normalized, indent=2)
    print(f"[SyncNeeds] 已同步 {len(normalized)} 个需求到缓存")
    return {"ok": True, "count": len(normalized)}


@router.delete("/needs")
def clear_needs(request: Request):
    ctx = _get_session(request)
    ctx.reset_debate()
    if ctx.needs_cache.exists():
        ctx.needs_cache.unlink()
    return {"ok": True}


@router.post("/translate")
def translate_text(req: TranslateRequest, request: Request):
    """Translate English text to Chinese using LLM."""
    ctx = _get_session(request)
    try:
        messages = [
            {"role": "user", "content": (
                "将以下英文内容翻译为中文，只输出翻译结果，不要添加任何解释：\n\n"
                + req.text[:3000]
            )},
        ]
        translation = call_llm(messages)
        return {"translation": translation.strip()}
    except Exception as exc:
        print(f"[Translate] failed: {type(exc).__name__}")
        raise HTTPException(status_code=502, detail="翻译失败，请检查本地模型配置") from exc

# ============================================================
# Debate routes
# ============================================================

@router.get("/debate/state")
def debate_state(request: Request):
    ctx = _get_session(request)
    return {
        "status": ctx.debate_state["status"],
        "round": ctx.debate_state["round"],
        "max_rounds": ctx.debate_state["max_rounds"],
        "debate_log": ctx.debate_state["debate_log"],
        "selected_need_idx": ctx.debate_state.get("selected_need_idx"),
        "final_report": ctx.debate_state["final_report"],
        "product_proposal": ctx.debate_state.get("product_proposal"),
        "topics": ctx.debate_state.get("topics", []),
        "current_topic_idx": ctx.debate_state.get("current_topic_idx", -1),
        "topic_conclusions": ctx.debate_state.get("topic_conclusions", []),
        "free_topic_input": ctx.debate_state.get("free_topic_input"),
        "language": ctx.debate_state.get("language", UI_LANGUAGE_ZH),
    }


@router.post("/debate/reset")
def debate_reset(request: Request):
    ctx = _get_session(request)
    ctx.reset_debate()
    return {"ok": True}


_DEBATE_LLM_TIMEOUT_SECONDS = 90


def _call_debate_role(role: str, messages: list[dict], max_tokens: int | None = None) -> str:
    """Keep one slow relay request from stalling the entire multi-step debate."""
    return call_for_role(
        role,
        messages,
        max_tokens,
        timeout_seconds=_DEBATE_LLM_TIMEOUT_SECONDS,
        max_attempts=1,
    )


def _stream_role(role: str, messages: list[dict], max_tokens: int | None = None):
    """Stream one role, reconnecting once only before the first output token."""
    for connect_attempt in range(2):
        emitted_content = False
        try:
            stream = call_for_role_stream(
                role,
                messages,
                max_tokens,
                timeout_seconds=_DEBATE_LLM_TIMEOUT_SECONDS,
                max_attempts=1,
            )
            for chunk in stream:
                emitted_content = True
                yield chunk
            return
        except Exception as exc:
            if connect_attempt == 0 and not emitted_content and is_transient_connection_error(exc):
                print(f"[Debate] {role} connection failed before output; reconnecting once")
                import time as _time
                _time.sleep(1)
                continue
            raise


@router.post("/debate/start")
def start_debate(req: StartDebateRequest, request: Request):
    ctx = _get_session(request)
    debate_language = _normalize_ui_language(req.language)
    if ctx.debate_state["status"] == "debating" and not req.demo:
        raise HTTPException(status_code=409, detail="已有讨论进行中")

    needs_data = get_needs(request)["needs"]
    if req.need_index < 0 or req.need_index >= len(needs_data):
        raise HTTPException(status_code=404, detail="Need not found")

    need = _need_for_language(needs_data[req.need_index], debate_language)

    # ===== 演示模式：从缓存回放讨论 =====
    if req.demo:
        import time as _t
        _DEMO_DEBATE_PATH = ROOT / "data" / "demo" / "demo_debate.json"
        if not _DEMO_DEBATE_PATH.exists():
            def _err():
                yield _sse("error", {"message": "演示讨论数据不存在，请先准备 data/demo/demo_debate.json"})
            return _sse_response(_err())

        demo_msgs = json.loads(_DEMO_DEBATE_PATH.read_text(encoding="utf-8"))
        ctx.debate_state["status"] = "debating"
        ctx.debate_state["selected_need_idx"] = req.need_index
        ctx.debate_state["debate_log"] = []
        ctx.debate_state["round"] = 0
        ctx.debate_state["language"] = debate_language

        def _demo_stream():
            _first_analyst = True
            for item in demo_msgs:
                evt = item.get("event")
                if evt == "topic_start":
                    yield _sse("topic_start", item.get("data", {}))
                    _t.sleep(0.3)
                elif evt == "round_start":
                    yield _sse("round_start", item.get("data", {}))
                    _t.sleep(0.2)
                elif evt == "message":
                    role = item.get("role", "director")
                    label = item.get("label", "")
                    content = item.get("content", "")
                    provider = item.get("provider", "claude")
                    is_first_pm = role == "analyst" and _first_analyst

                    yield _sse("message_start", {"role": role, "label": label, "provider": provider})

                    if is_first_pm:
                        think_start = content.find("<think>")
                        think_end = content.find("</think>")
                        for idx, ch in enumerate(content):
                            yield _sse("chunk", {"text": ch})
                            in_think = think_start != -1 and think_start <= idx <= (think_end + 7 if think_end != -1 else len(content))
                            _t.sleep(0.002 if in_think else 0.008)
                    else:
                        _t.sleep(0.15)
                        yield _sse("chunk", {"text": content})

                    yield _sse("message_end", {"role": role, "content": content})
                    ctx.debate_state["debate_log"].append({"role": role, "content": content})
                    if is_first_pm:
                        ctx.debate_state["analysis_result"] = content
                        _first_analyst = False
                    _t.sleep(0.6)
            yield _sse("debate_end", {})
            ctx.debate_state["status"] = "debate_done"

        return _sse_response(_demo_stream())

    def _parse_topics_json(text: str) -> list[dict]:
        """从 LLM 输出中解析话题 JSON 数组。"""
        import re as _re
        cleaned = _re.sub(r'<think>[\s\S]*?</think>', '', text, flags=_re.IGNORECASE).strip()
        cleaned = _re.sub(r'```(?:json)?\s*', '', cleaned).strip()
        cleaned = _re.sub(r'```\s*$', '', cleaned).strip()
        start = cleaned.find('[')
        end = cleaned.rfind(']')
        if start != -1 and end != -1:
            cleaned = cleaned[start:end+1]
        return json.loads(cleaned)

    def event_stream() -> Generator[str, None, None]:
        set_thread_session(ctx)
        print(f"[Debate] session={ctx.session_id} role_map={ctx._role_model_map} gpt_key_set={bool(ctx.get_config('GPT')['api_key'])}")
        ctx.debate_state["status"] = "debating"
        ctx.debate_state["selected_need_idx"] = req.need_index
        ctx.debate_state["debate_log"] = []
        ctx.debate_state["round"] = 0
        ctx.debate_state["topics"] = []
        ctx.debate_state["current_topic_idx"] = -1
        ctx.debate_state["topic_conclusions"] = []
        ctx.debate_state["current_topic_exchanges"] = []
        ctx.debate_state["language"] = debate_language

        _analyst_label = _debate_role_label(ctx, "analyst", debate_language)
        _critic_label = _debate_role_label(ctx, "critic", debate_language)
        _director_label = _debate_role_label(ctx, "director", debate_language)

        try:
            import time as _time

            # ── Phase 1: 导演即时开场白（模板，不调 LLM） ──
            instant_opening = (
                f"Alright, I’ll have {_analyst_label} and {_critic_label} discuss “{need['need_title']}”. I’ll scan the posts first and split this into a few sharp topics."
                if _is_ui_en(debate_language) else
                f"好，我来安排{_analyst_label}和{_critic_label}讨论「{need['need_title']}」。让我先看看帖子，拆几个核心话题出来。"
            )
            yield _sse("message_start", {"role": "director", "label": _director_label, "provider": _provider_for_role("director")})
            for _ch in instant_opening:
                yield _sse("chunk", {"text": _ch})
                _time.sleep(0.02)
            yield _sse("message_end", {"role": "director", "content": instant_opening})
            ctx.debate_state["debate_log"].append({"role": "director", "content": instant_opening})

            # 开场白先到达界面，再做可能耗时的联网预检，避免长时间空白。
            role_ok, role_err = check_role_models_available()
            if not role_ok:
                print(f"[Debate] Role model preflight failed: {role_err}")
                ctx.debate_state["status"] = "error"
                yield _sse("error", {"message": (
                    role_err
                    if not _is_ui_en(debate_language)
                    else _friendly_error_for_language(debate_language, role_err)
                )})
                return

            # ── Phase 2: 导演拆话题（LLM 调用前先显示占位） ──
            yield _sse("message_start", {"role": "director", "label": _director_label, "provider": _provider_for_role("director")})

            print("[Debate] Phase 2: Director analyzing topics...")
            topic_msgs = prepare_topic_analysis(need, language=debate_language)
            topic_raw = _call_debate_role("director", topic_msgs, max_tokens=2000)

            topics = []
            try:
                topics = _parse_topics_json(topic_raw)
            except Exception as parse_err:
                print(f"[Debate] Topic parse failed: {parse_err}, raw={topic_raw[:300]}")
                topics = (
                    [
                        {"title": "Is the pain real", "question": "Are these users describing a real recurring pain, or just venting?"},
                        {"title": "Willingness to pay", "question": "Is there any sign that users would pay for this direction?"},
                        {"title": "Competitive edge", "question": "With existing apps already around, why would our version win?"},
                    ]
                    if _is_ui_en(debate_language) else
                    [
                        {"title": "痛点真实性", "question": "帖子里这些人是真痛还是嘴上说说？"},
                        {"title": "付费意愿", "question": "有用户愿意为这个方向掏钱吗？"},
                        {"title": "竞品差异化", "question": "已有方案这么多，凭什么我们能做？"},
                    ]
                )

            topics = [t for t in topics if isinstance(t, dict) and "title" in t and "question" in t][:3]
            if not topics:
                topics = (
                    [
                        {"title": "Demand validation", "question": "How many users actually have this problem?"},
                        {"title": "Feasibility", "question": "Can an app or AI product really solve this?"},
                    ]
                    if _is_ui_en(debate_language) else
                    [
                        {"title": "需求验证", "question": "这个需求到底有多少用户有？"},
                        {"title": "可行性", "question": "App/AI 能解决这个问题吗？"},
                    ]
                )

            ctx.debate_state["topics"] = topics
            print(f"[Debate] Parsed {len(topics)} topics: {[t['title'] for t in topics]}")

            topic_intro = (
                "I split this into {} topics: {}. Let’s go through them one by one.".format(
                    len(topics),
                    ", ".join(t["title"] for t in topics),
                )
                if _is_ui_en(debate_language) else
                "我拆了 {} 个话题：{}。一个个来聊。".format(
                    len(topics),
                    "、".join(t["title"] for t in topics),
                )
            )
            for _ch in topic_intro:
                yield _sse("chunk", {"text": _ch})
                _time.sleep(0.02)
            yield _sse("message_end", {"role": "director", "content": topic_intro})
            ctx.debate_state["debate_log"].append({"role": "director", "content": topic_intro})

            yield _sse("topic_list", {"topics": topics})

            # ── 启动投资人后台并行分析 ──
            _posts_compact = _format_need_posts_compact(need)
            _post_count = len(need.get("posts", []))
            _investor_bg_result = {"text": "", "error": ""}

            def _run_investor_bg():
                set_thread_session(ctx)
                try:
                    _cr = investor_competitor_web_context(
                        need_title=need["need_title"],
                        need_description=need.get("need_description", "") or "",
                        posts_compact=_posts_compact,
                        web_search_engine=ctx.web_search_engine,
                    )
                    _msgs = prepare_investor_bg(need, _posts_compact, _post_count, competitor_research=_cr, language=debate_language)
                    _investor_bg_result["text"] = _call_debate_role("investor", _msgs)
                    print(f"[Debate] Investor BG analysis done ({len(_investor_bg_result['text'])} chars)")
                except Exception as _e:
                    _investor_bg_result["error"] = str(_e)[:200]
                    print(f"[Debate] Investor BG analysis failed: {_e}")
                finally:
                    clear_thread_session()

            _investor_thread = threading.Thread(target=_run_investor_bg, daemon=True)
            _investor_thread.start()
            print("[Debate] Investor BG analysis started in background")

            # ── Phase 3: 逐话题讨论 ──
            conclusions: list[dict] = []

            for t_idx, topic in enumerate(topics):
                ctx.debate_state["current_topic_idx"] = t_idx
                ctx.debate_state["current_topic_exchanges"] = []
                ctx.debate_state["round"] = t_idx + 1
                topic_exchanges: list[dict] = []

                yield _sse("round_start", {"round": t_idx + 1})
                yield _sse("topic_start", {"index": t_idx, "title": topic["title"], "total": len(topics)})

                # 导演提问（模板，逐字输出）
                director_q = (
                    f"Topic {t_idx+1}: {topic['title']}. {topic['question']}"
                    if _is_ui_en(debate_language) else
                    f"话题 {t_idx+1}：{topic['title']}。{topic['question']}"
                )
                yield _sse("message_start", {"role": "director", "label": _director_label, "provider": _provider_for_role("director")})
                for _ch in director_q:
                    yield _sse("chunk", {"text": _ch})
                    _time.sleep(0.02)
                yield _sse("message_end", {"role": "director", "content": director_q})
                ctx.debate_state["debate_log"].append({"role": "director", "content": director_q})

                # PM 表态（流式）
                is_first = (t_idx == 0)
                pm_msgs = prepare_topic_pm(need, topic, "", conclusions, is_first=is_first, language=debate_language)
                yield _sse("message_start", {"role": "analyst", "label": _analyst_label, "provider": _provider_for_role("analyst")})
                pm_parts: list[str] = []
                for chunk in _stream_role("analyst", pm_msgs):
                    pm_parts.append(chunk)
                    yield _sse("chunk", {"text": chunk})
                pm_resp = "".join(pm_parts)
                ctx.debate_state["debate_log"].append({"role": "analyst", "content": pm_resp})
                if is_first:
                    ctx.debate_state["analysis_result"] = pm_resp
                yield _sse("message_end", {"role": "analyst", "content": pm_resp})
                topic_exchanges.append({"role": "analyst", "content": pm_resp})

                # 杠精回应（流式，含反馈分级）
                critic_msgs = prepare_topic_critic(need, topic, pm_resp, conclusions, language=debate_language)
                yield _sse("message_start", {"role": "critic", "label": _critic_label, "provider": _provider_for_role("critic")})
                critic_parts: list[str] = []
                for chunk in _stream_role("critic", critic_msgs):
                    critic_parts.append(chunk)
                    yield _sse("chunk", {"text": chunk})
                critic_resp = "".join(critic_parts)
                _structural = is_structural_feedback(critic_resp)
                critic_clean = _re_tag.sub(r'\[(STRUCTURAL|MINOR)\]\s*', '', critic_resp).strip()
                ctx.debate_state["debate_log"].append({"role": "critic", "content": critic_clean})
                yield _sse("message_end", {"role": "critic", "content": critic_clean})
                topic_exchanges.append({"role": "critic", "content": critic_clean})

                # ── 第二轮：PM 反击（无论 STRUCTURAL / MINOR 都做） ──
                print(f"[Debate] Topic {t_idx+1} '{topic['title']}': {'STRUCTURAL' if _structural else 'MINOR'} feedback → PM counter")
                counter_msgs = prepare_topic_pm_counter(need, topic, pm_resp, critic_clean, conclusions, language=debate_language)
                yield _sse("message_start", {"role": "analyst", "label": _analyst_label, "provider": _provider_for_role("analyst")})
                counter_parts: list[str] = []
                for chunk in _stream_role("analyst", counter_msgs):
                    counter_parts.append(chunk)
                    yield _sse("chunk", {"text": chunk})
                counter_resp = "".join(counter_parts)
                ctx.debate_state["debate_log"].append({"role": "analyst", "content": counter_resp})
                yield _sse("message_end", {"role": "analyst", "content": counter_resp})
                topic_exchanges.append({"role": "analyst", "content": counter_resp})

                # ── 第二轮：杠精跟进 ──
                followup_msgs = prepare_topic_critic_followup(need, topic, critic_clean, counter_resp, conclusions, language=debate_language)
                yield _sse("message_start", {"role": "critic", "label": _critic_label, "provider": _provider_for_role("critic")})
                followup_parts: list[str] = []
                for chunk in _stream_role("critic", followup_msgs):
                    followup_parts.append(chunk)
                    yield _sse("chunk", {"text": chunk})
                followup_resp = "".join(followup_parts)
                followup_clean = _re_tag.sub(r'\[(STRUCTURAL|MINOR)\]\s*', '', followup_resp).strip()
                ctx.debate_state["debate_log"].append({"role": "critic", "content": followup_clean})
                yield _sse("message_end", {"role": "critic", "content": followup_clean})
                topic_exchanges.append({"role": "critic", "content": followup_clean})

                ctx.debate_state["current_topic_exchanges"] = topic_exchanges

                # 导演话题小结（流式）
                wrap_msgs = prepare_topic_wrap(topic, topic_exchanges, conclusions, language=debate_language)
                yield _sse("message_start", {"role": "director", "label": _director_label, "provider": _provider_for_role("director")})
                wrap_parts: list[str] = []
                for chunk in _stream_role("director", wrap_msgs):
                    wrap_parts.append(chunk)
                    yield _sse("chunk", {"text": chunk})
                wrap_resp = "".join(wrap_parts)
                ctx.debate_state["debate_log"].append({"role": "director", "content": wrap_resp})
                yield _sse("message_end", {"role": "director", "content": wrap_resp})

                conclusion = {"title": topic["title"], "summary": wrap_resp.strip()}
                conclusions.append(conclusion)
                ctx.debate_state["topic_conclusions"] = list(conclusions)

                yield _sse("topic_end", {"index": t_idx, "title": topic["title"], "summary": wrap_resp.strip()})
                print(f"[Debate] Topic {t_idx+1}/{len(topics)} '{topic['title']}' done")

            # ── Phase 4a: 等待投资人后台分析完成 ──
            _investor_label = _debate_role_label(ctx, "investor", debate_language)
            _investor_thread.join(timeout=120)
            if _investor_thread.is_alive():
                print("[Debate] Investor BG analysis timed out after 120s")
                _investor_bg_result["error"] = "Analysis timed out" if _is_ui_en(debate_language) else "分析超时"

            # ── Phase 4b: 投资人结合讨论结论，流式输出最终商业分析 ──
            investor_resp = ""
            try:
                print("[Debate] Phase 4b: Investor final analysis")
                investor_final_msgs = prepare_investor_final(need, conclusions, _investor_bg_result["text"], language=debate_language)
                yield _sse("message_start", {"role": "investor", "label": _investor_label, "provider": _provider_for_role("investor")})
                investor_parts: list[str] = []
                for chunk in _stream_role("investor", investor_final_msgs):
                    investor_parts.append(chunk)
                    yield _sse("chunk", {"text": chunk})
                investor_resp = "".join(investor_parts)
                ctx.debate_state["debate_log"].append({"role": "investor", "content": investor_resp})
                yield _sse("message_end", {"role": "investor", "content": investor_resp})
            except Exception as inv_err:
                print(f"[Debate] Investor final analysis failed: {inv_err}")
                _err_text = (
                    f"Investor analysis is temporarily unavailable ({_friendly_error(inv_err)}), so the director will make the call directly."
                    if _is_ui_en(debate_language) else
                    f"投资人分析暂时不可用（{_friendly_error(inv_err)}），导演将直接判决。"
                )
                yield _sse("message_end", {"role": "investor", "content": _err_text})
                ctx.debate_state["debate_log"].append({"role": "investor", "content": _err_text})

            # ── Phase 4c: 导演最终判决（综合产品讨论 + 投资人分析） ──
            print("[Debate] Phase 4c: Director final verdict")
            verdict_msgs = prepare_final_verdict(need, conclusions, investor_resp, language=debate_language)
            yield _sse("message_start", {"role": "director", "label": _director_label, "provider": _provider_for_role("director")})
            verdict_parts: list[str] = []
            for chunk in _stream_role("director", verdict_msgs):
                verdict_parts.append(chunk)
                yield _sse("chunk", {"text": chunk})
            verdict = "".join(verdict_parts)
            ctx.debate_state["debate_log"].append({"role": "director", "content": verdict})
            yield _sse("message_end", {"role": "director", "content": verdict})

            ctx.debate_state["status"] = "debate_done"
            ctx.debate_state["current_topic_idx"] = -1
            ctx.save_debate_cache()

            print(f"[Debate] Finished: {len(topics)} topics, {len(ctx.debate_state['debate_log'])} messages")
            yield _sse("debate_end", {"reason": "director_verdict", "topics": len(topics), "messages": len(ctx.debate_state["debate_log"])})

        except Exception as e:
            import traceback
            traceback.print_exc()
            ctx.debate_state["status"] = "error"
            ctx.save_debate_cache()
            yield _sse("error", {"message": _friendly_error_for_language(debate_language, e)})

    return _sse_response(event_stream())


@router.post("/debate/start-free")
def start_free_debate(req: StartFreeDebateRequest, request: Request):
    """自由话题模式：用户输入一句话/话题，三角色直接讨论。"""
    ctx = _get_session(request)
    debate_language = _normalize_ui_language(req.language)
    if ctx.debate_state["status"] == "debating":
        raise HTTPException(status_code=409, detail="已有讨论进行中")

    user_input = req.user_input.strip()
    if not user_input:
        raise HTTPException(status_code=400, detail="话题不能为空")

    def _parse_free_topics_json(text: str) -> list[dict]:
        import re as _re
        cleaned = _re.sub(r'<think>[\s\S]*?</think>', '', text).strip()
        cleaned = cleaned.strip('`').strip()
        if cleaned.startswith('json'):
            cleaned = cleaned[4:].strip()
        return json.loads(cleaned)

    def event_stream():
        set_thread_session(ctx)
        import time as _time

        ctx.reset_debate()
        ctx.debate_state["status"] = "debating"
        ctx.debate_state["selected_need_idx"] = None
        ctx.debate_state["free_topic_input"] = user_input
        ctx.debate_state["language"] = debate_language

        names = ctx.role_names
        _director_label = _debate_role_label(ctx, "director", debate_language)
        _analyst_label = _debate_role_label(ctx, "analyst", debate_language)
        _critic_label = _debate_role_label(ctx, "critic", debate_language)

        try:
            # Phase 1: 导演开场
            instant_opening = (
                f"Alright, I’ll set up a discussion around “{user_input}”. I’ll split it into a few sharp topics first."
                if _is_ui_en(debate_language) else
                f"好，我来安排讨论「{user_input}」。让我先拆几个核心话题出来。"
            )
            yield _sse("message_start", {"role": "director", "label": _director_label, "provider": _provider_for_role("director")})
            for _ch in instant_opening:
                yield _sse("chunk", {"text": _ch})
                _time.sleep(0.02)
            yield _sse("message_end", {"role": "director", "content": instant_opening})
            ctx.debate_state["debate_log"].append({"role": "director", "content": instant_opening})

            # 开场白先到达界面，再做可能耗时的联网预检，避免长时间空白。
            role_ok, role_err = check_role_models_available()
            if not role_ok:
                print(f"[FreeDeb] Role model preflight failed: {role_err}")
                ctx.debate_state["status"] = "error"
                yield _sse("error", {"message": _ui_text(debate_language, "角色模型不可用，请检查本地配置", "Role models are unavailable. Please check your local settings.")})
                return

            # Phase 2: 导演拆话题
            yield _sse("message_start", {"role": "director", "label": _director_label, "provider": _provider_for_role("director")})
            print("[FreeDeb] Phase 2: Director analyzing topics...")
            topic_msgs = prepare_free_topic_analysis(user_input, language=debate_language)
            topic_raw = _call_debate_role("director", topic_msgs, max_tokens=2000)

            topics = []
            try:
                topics = _parse_free_topics_json(topic_raw)
            except Exception as parse_err:
                print(f"[FreeDeb] Topic parse failed: {parse_err}, raw={topic_raw[:300]}")
                topics = (
                    [
                        {"title": "Real demand or fake demand", "question": "Is this a real user need, or just an interesting idea?"},
                        {"title": "Who pays", "question": "Who would actually pay for this?"},
                        {"title": "Why us", "question": "With existing options around, why could we win?"},
                    ]
                    if _is_ui_en(debate_language) else
                    [
                        {"title": "需求真伪", "question": "这个需求是真的还是伪需求？"},
                        {"title": "谁会买单", "question": "什么人愿意为这个掏钱？"},
                        {"title": "凭什么你做", "question": "已有方案那么多，凭什么我们能做？"},
                    ]
                )

            topics = [t for t in topics if isinstance(t, dict) and "title" in t and "question" in t][:3]
            if not topics:
                topics = (
                    [
                        {"title": "Demand validation", "question": "How many people actually have this need?"},
                        {"title": "Feasibility", "question": "Can this really become a product?"},
                    ]
                    if _is_ui_en(debate_language) else
                    [
                        {"title": "需求验证", "question": "这个需求到底有多少人有？"},
                        {"title": "可行性", "question": "做成产品的话能落地吗？"},
                    ]
                )

            ctx.debate_state["topics"] = topics
            print(f"[FreeDeb] Parsed {len(topics)} topics: {[t['title'] for t in topics]}")

            topic_intro = (
                "I split this into {} topics: {}. Let’s go through them one by one.".format(
                    len(topics),
                    ", ".join(t["title"] for t in topics),
                )
                if _is_ui_en(debate_language) else
                "我拆了 {} 个话题：{}。一个个来聊。".format(
                    len(topics),
                    "、".join(t["title"] for t in topics),
                )
            )
            for _ch in topic_intro:
                yield _sse("chunk", {"text": _ch})
                _time.sleep(0.02)
            yield _sse("message_end", {"role": "director", "content": topic_intro})
            ctx.debate_state["debate_log"].append({"role": "director", "content": topic_intro})

            yield _sse("topic_list", {"topics": topics})

            # ── 启动投资人后台并行分析（自由话题模式）──
            _investor_bg_result_free = {"text": "", "error": ""}

            def _run_investor_bg_free():
                set_thread_session(ctx)
                try:
                    _cr_f = investor_competitor_web_context(
                        user_input=user_input,
                        web_search_engine=ctx.web_search_engine,
                    )
                    _msgs_f = prepare_free_investor_bg(user_input, competitor_research=_cr_f, language=debate_language)
                    _investor_bg_result_free["text"] = _call_debate_role("investor", _msgs_f)
                    print(f"[FreeDeb] Investor BG analysis done ({len(_investor_bg_result_free['text'])} chars)")
                except Exception as _e:
                    _investor_bg_result_free["error"] = str(_e)[:200]
                    print(f"[FreeDeb] Investor BG analysis failed: {_e}")
                finally:
                    clear_thread_session()

            _investor_thread_free = threading.Thread(target=_run_investor_bg_free, daemon=True)
            _investor_thread_free.start()
            print("[FreeDeb] Investor BG analysis started in background")

            # Phase 3: 逐话题讨论
            conclusions: list[dict] = []

            for t_idx, topic in enumerate(topics):
                ctx.debate_state["current_topic_idx"] = t_idx
                ctx.debate_state["current_topic_exchanges"] = []
                ctx.debate_state["round"] = t_idx + 1
                topic_exchanges: list[dict] = []

                yield _sse("round_start", {"round": t_idx + 1})
                yield _sse("topic_start", {"index": t_idx, "title": topic["title"], "total": len(topics)})

                director_q = (
                    f"Topic {t_idx+1}: {topic['title']}. {topic['question']}"
                    if _is_ui_en(debate_language) else
                    f"话题 {t_idx+1}：{topic['title']}。{topic['question']}"
                )
                yield _sse("message_start", {"role": "director", "label": _director_label, "provider": _provider_for_role("director")})
                for _ch in director_q:
                    yield _sse("chunk", {"text": _ch})
                    _time.sleep(0.02)
                yield _sse("message_end", {"role": "director", "content": director_q})
                ctx.debate_state["debate_log"].append({"role": "director", "content": director_q})

                # PM（自由话题模式）
                is_first = (t_idx == 0)
                pm_msgs = prepare_free_topic_pm(user_input, topic, conclusions, is_first=is_first, language=debate_language)
                yield _sse("message_start", {"role": "analyst", "label": _analyst_label, "provider": _provider_for_role("analyst")})
                pm_parts: list[str] = []
                for chunk in _stream_role("analyst", pm_msgs):
                    pm_parts.append(chunk)
                    yield _sse("chunk", {"text": chunk})
                pm_resp = "".join(pm_parts)
                ctx.debate_state["debate_log"].append({"role": "analyst", "content": pm_resp})
                yield _sse("message_end", {"role": "analyst", "content": pm_resp})
                topic_exchanges.append({"role": "analyst", "content": pm_resp})

                # 杠精（自由话题模式）
                critic_msgs = prepare_free_topic_critic(user_input, topic, pm_resp, conclusions, language=debate_language)
                yield _sse("message_start", {"role": "critic", "label": _critic_label, "provider": _provider_for_role("critic")})
                critic_parts: list[str] = []
                for chunk in _stream_role("critic", critic_msgs):
                    critic_parts.append(chunk)
                    yield _sse("chunk", {"text": chunk})
                critic_resp = "".join(critic_parts)
                _structural = is_structural_feedback(critic_resp)
                critic_clean = _re_tag.sub(r'\[(STRUCTURAL|MINOR)\]\s*', '', critic_resp).strip()
                ctx.debate_state["debate_log"].append({"role": "critic", "content": critic_clean})
                yield _sse("message_end", {"role": "critic", "content": critic_clean})
                topic_exchanges.append({"role": "critic", "content": critic_clean})

                # ── 第二轮：PM 反击（无论 STRUCTURAL / MINOR 都做） ──
                print(f"[FreeDeb] Topic {t_idx+1} '{topic['title']}': {'STRUCTURAL' if _structural else 'MINOR'} → PM counter")
                counter_msgs = prepare_topic_pm_counter(
                    {"need_title": user_input}, topic, pm_resp, critic_clean, conclusions, language=debate_language
                )
                yield _sse("message_start", {"role": "analyst", "label": _analyst_label, "provider": _provider_for_role("analyst")})
                counter_parts: list[str] = []
                for chunk in _stream_role("analyst", counter_msgs):
                    counter_parts.append(chunk)
                    yield _sse("chunk", {"text": chunk})
                counter_resp = "".join(counter_parts)
                ctx.debate_state["debate_log"].append({"role": "analyst", "content": counter_resp})
                yield _sse("message_end", {"role": "analyst", "content": counter_resp})
                topic_exchanges.append({"role": "analyst", "content": counter_resp})

                # ── 第二轮：杠精跟进 ──
                followup_msgs = prepare_free_topic_critic_followup(user_input, topic, critic_clean, counter_resp, conclusions, language=debate_language)
                yield _sse("message_start", {"role": "critic", "label": _critic_label, "provider": _provider_for_role("critic")})
                followup_parts: list[str] = []
                for chunk in _stream_role("critic", followup_msgs):
                    followup_parts.append(chunk)
                    yield _sse("chunk", {"text": chunk})
                followup_resp = "".join(followup_parts)
                followup_clean = _re_tag.sub(r'\[(STRUCTURAL|MINOR)\]\s*', '', followup_resp).strip()
                ctx.debate_state["debate_log"].append({"role": "critic", "content": followup_clean})
                yield _sse("message_end", {"role": "critic", "content": followup_clean})
                topic_exchanges.append({"role": "critic", "content": followup_clean})

                ctx.debate_state["current_topic_exchanges"] = topic_exchanges

                wrap_msgs = prepare_topic_wrap(topic, topic_exchanges, conclusions, language=debate_language)
                yield _sse("message_start", {"role": "director", "label": _director_label, "provider": _provider_for_role("director")})
                wrap_parts: list[str] = []
                for chunk in _stream_role("director", wrap_msgs):
                    wrap_parts.append(chunk)
                    yield _sse("chunk", {"text": chunk})
                wrap_resp = "".join(wrap_parts)
                ctx.debate_state["debate_log"].append({"role": "director", "content": wrap_resp})
                yield _sse("message_end", {"role": "director", "content": wrap_resp})

                conclusion = {"title": topic["title"], "summary": wrap_resp.strip()}
                conclusions.append(conclusion)
                ctx.debate_state["topic_conclusions"] = list(conclusions)

                yield _sse("topic_end", {"index": t_idx, "title": topic["title"], "summary": wrap_resp.strip()})
                print(f"[FreeDeb] Topic {t_idx+1}/{len(topics)} '{topic['title']}' done")

            # ── Phase 4a: 等待投资人后台分析完成 ──
            _investor_label_free = _debate_role_label(ctx, "investor", debate_language)
            _investor_thread_free.join(timeout=120)
            if _investor_thread_free.is_alive():
                print("[FreeDeb] Investor BG analysis timed out after 120s")
                _investor_bg_result_free["error"] = "Analysis timed out" if _is_ui_en(debate_language) else "分析超时"

            # ── Phase 4b: 投资人最终商业分析（流式）──
            investor_resp = ""
            try:
                print("[FreeDeb] Phase 4b: Investor final analysis")
                investor_final_msgs = prepare_free_investor_final(user_input, conclusions, _investor_bg_result_free["text"], language=debate_language)
                yield _sse("message_start", {"role": "investor", "label": _investor_label_free, "provider": _provider_for_role("investor")})
                investor_parts: list[str] = []
                for chunk in _stream_role("investor", investor_final_msgs):
                    investor_parts.append(chunk)
                    yield _sse("chunk", {"text": chunk})
                investor_resp = "".join(investor_parts)
                ctx.debate_state["debate_log"].append({"role": "investor", "content": investor_resp})
                yield _sse("message_end", {"role": "investor", "content": investor_resp})
            except Exception as inv_err:
                print(f"[FreeDeb] Investor final analysis failed: {inv_err}")
                _err_text = (
                    f"Investor analysis is temporarily unavailable ({_friendly_error(inv_err)}), so the director will make the call directly."
                    if _is_ui_en(debate_language) else
                    f"投资人分析暂时不可用（{_friendly_error(inv_err)}），导演将直接判决。"
                )
                yield _sse("message_end", {"role": "investor", "content": _err_text})
                ctx.debate_state["debate_log"].append({"role": "investor", "content": _err_text})

            # ── Phase 4c: 导演最终判决 ──
            print("[FreeDeb] Phase 4c: Director final verdict")
            verdict_msgs = prepare_final_verdict({"need_title": user_input}, conclusions, investor_resp, language=debate_language)
            yield _sse("message_start", {"role": "director", "label": _director_label, "provider": _provider_for_role("director")})
            verdict_parts: list[str] = []
            for chunk in _stream_role("director", verdict_msgs):
                verdict_parts.append(chunk)
                yield _sse("chunk", {"text": chunk})
            verdict = "".join(verdict_parts)
            ctx.debate_state["debate_log"].append({"role": "director", "content": verdict})
            yield _sse("message_end", {"role": "director", "content": verdict})

            ctx.debate_state["status"] = "debate_done"
            ctx.debate_state["current_topic_idx"] = -1
            ctx.save_debate_cache()

            print(f"[FreeDeb] Finished: {len(topics)} topics, {len(ctx.debate_state['debate_log'])} messages")
            yield _sse("debate_end", {"reason": "director_verdict", "topics": len(topics), "messages": len(ctx.debate_state["debate_log"])})

        except Exception as e:
            import traceback
            traceback.print_exc()
            ctx.debate_state["status"] = "error"
            ctx.save_debate_cache()
            yield _sse("error", {"message": _friendly_error_for_language(debate_language, e)})

    return _sse_response(event_stream())


@router.post("/debate/message")
def human_message(req: HumanMessageRequest, request: Request):
    ctx = _get_session(request)
    debate_language = _normalize_ui_language(req.language or ctx.debate_state.get("language", UI_LANGUAGE_ZH))
    if not ctx.debate_state["debate_log"]:
        raise HTTPException(status_code=400, detail="No active debate")

    free_topic_input = ctx.debate_state.get("free_topic_input")
    if free_topic_input:
        need = {"need_title": free_topic_input, "posts": []}
    else:
        needs_data = get_needs(request)["needs"]
        idx = ctx.debate_state.get("selected_need_idx")
        if idx is None or idx < 0 or idx >= len(needs_data):
            raise HTTPException(status_code=400, detail="No need selected")
        need = needs_data[idx]

    ctx.debate_state["debate_log"].append({"role": "human", "content": req.text})

    topics = ctx.debate_state.get("topics", [])
    current_topic_idx = ctx.debate_state.get("current_topic_idx", -1)
    current_topic = topics[current_topic_idx] if 0 <= current_topic_idx < len(topics) else {"title": "讨论", "question": ""}
    topic_exchanges = ctx.debate_state.get("current_topic_exchanges", [])

    def event_stream() -> Generator[str, None, None]:
        set_thread_session(ctx)
        try:
            target = req.target
            role_label = _debate_role_label(ctx, target, debate_language)
            msgs = prepare_human_inject_topic(need, current_topic, topic_exchanges, req.text, target, language=debate_language)

            yield _sse("message_start", {"role": target, "label": role_label, "provider": _provider_for_role(target)})
            parts: list[str] = []
            for chunk in _stream_role(target, msgs):
                parts.append(chunk)
                yield _sse("chunk", {"text": chunk})
            resp = "".join(parts)
            ctx.debate_state["debate_log"].append({"role": target, "content": resp})
            yield _sse("message_end", {"role": target, "content": resp})

            ctx.save_debate_cache()
            yield _sse("done", {})

        except Exception as e:
            _log_sse_error("HumanMessage", e, ctx)
            yield _sse("error", {"message": _friendly_error_for_language(debate_language, e)})

    return StreamingResponse(event_stream(), media_type="text/event-stream")


@router.post("/debate/report")
def generate_report(request: Request):
    ctx = _get_session(request)
    needs_data = get_needs(request)["needs"]
    idx = ctx.debate_state.get("selected_need_idx")
    report_language = _normalize_ui_language(ctx.debate_state.get("language", UI_LANGUAGE_ZH))
    if idx is not None and 0 <= idx < len(needs_data):
        need = _need_for_language(needs_data[idx], report_language)
    elif ctx.debate_state.get("free_topic_input"):
        need = {"need_title": ctx.debate_state["free_topic_input"], "need_description": "", "posts": []}
    else:
        raise HTTPException(status_code=400, detail="No need selected")
    debate_log = ctx.debate_state["debate_log"]
    claude_msgs = ctx.debate_state["analyst_messages"]

    def event_stream() -> Generator[str, None, None]:
        set_thread_session(ctx)
        try:
            ctx.debate_state["status"] = "generating_report"
            yield _sse("report_start", {})

            deep_dive_data = ctx.debate_state.get("deep_dive_analysis", "")

            # 限制 debate_log 体积：最多取最后 20 条，并截断过长的单条消息
            trimmed_log = debate_log[-20:] if len(debate_log) > 20 else debate_log
            report = generate_final_report(need, trimmed_log, claude_msgs, deep_dive_data, language=report_language)

            ctx.debate_state["final_report"] = report
            ctx.debate_state["status"] = "done"

            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            slug = need["need_title"][:30].replace(" ", "_").replace("/", "-")
            filename = f"{timestamp}_{slug}.json"
            report_data = {
                "need": need,
                "debate_log": debate_log,
                "product_proposal": ctx.debate_state.get("product_proposal", ""),
                "deep_dive_analysis": deep_dive_data,
                "final_report": report,
                "report_format": "markdown",
                "debate_rounds": ctx.debate_state["round"],
                "created_at": datetime.now().isoformat(),
                "language": report_language,
            }
            with open(ctx.reports_dir / filename, "w", encoding="utf-8") as f:
                json.dump(report_data, f, ensure_ascii=False, indent=2)

            ctx.save_debate_cache()
            yield _sse("report_end", {"report": report, "filename": filename})

        except Exception as e:
            import traceback
            traceback.print_exc()
            ctx.debate_state["status"] = "debate_done"
            ctx.save_debate_cache()
            yield _sse("error", {"message": _friendly_error(e)})

    return StreamingResponse(event_stream(), media_type="text/event-stream")

# ============================================================
# 直接生成报告（无需辩论）
# ============================================================

def _report_competitor_key(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(name or "").lower())


def _report_competitor_tokens(name: str) -> set[str]:
    stopwords = {
        "app", "apps", "mobile", "ios", "android", "iphone", "ipad",
        "fitness", "workout", "workouts", "training", "tracker", "ai",
        "photo", "editor", "daily", "premium", "plus",
    }
    return {
        token for token in re.findall(r"[a-z0-9]+", str(name or "").lower())
        if len(token) >= 3 and token not in stopwords
    }


def _report_extract_competitor_json(text: str) -> list[dict[str, Any]]:
    """从 WebSearch 竞品结果中提取 JSON 数组，失败时返回空列表。"""
    if not text:
        return []
    try:
        data = json.loads(text)
        return data if isinstance(data, list) else []
    except Exception:
        pass
    decoder = json.JSONDecoder()
    for match in re.finditer(r"\[", text):
        try:
            data, _end = decoder.raw_decode(text[match.start():])
        except Exception:
            continue
        if isinstance(data, list):
            return [item for item in data if isinstance(item, dict)]
    return []


def _report_find_web_competitor(name: str, web_items: list[dict[str, Any]]) -> dict[str, Any]:
    key = _report_competitor_key(name)
    if not key:
        return {}
    keyed: list[tuple[str, dict[str, Any]]] = [
        (_report_competitor_key(str(item.get("name") or "")), item)
        for item in web_items if isinstance(item, dict)
    ]
    for item_key, item in keyed:
        if item_key == key:
            return item
    for item_key, item in keyed:
        if item_key and (item_key in key or key in item_key):
            return item
    tokens = _report_competitor_tokens(name)
    if tokens:
        for _item_key, item in keyed:
            item_tokens = _report_competitor_tokens(str(item.get("name") or ""))
            if tokens & item_tokens:
                return item
    return {}


def _report_table_cell(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return "-"
    cleaned = text.replace("|", " / ").replace("\n", " ")
    if re.fullmatch(r"\[[^\]]+\]\(https?://[^)]+\)", cleaned):
        return cleaned
    return cleaned[:180]


def _report_short_text(value: Any, *, limit: int = 96) -> str:
    text = re.sub(r"\[[^\]]+\]\([^)]+\)", "", str(value or ""))
    text = re.sub(r"[*_`]+", "", text)
    text = re.sub(r"\s+", " ", text).strip(" ;,，。")
    return text[:limit].strip() or "-"


def _report_clean_pricing(value: Any) -> str:
    """把竞品定价压缩成适合表格展示的中文短句。"""
    text = _report_short_text(value, limit=180)
    if text == "-":
        return "-"

    text = re.sub(r"\((?:[^)]*(?:listing|not shown|typical app-store|public subscription)[^)]*)\)", "", text, flags=re.I)
    text = re.sub(r"\(([^)]*(?:in-app purchases?|free trial|subscription)[^)]*)\)", r"；\1", text, flags=re.I)
    text = re.sub(r"\bpremium pricing not shown (?:on|in) the (?:play )?listing\b", "高级版价格未公开", text, flags=re.I)
    text = re.sub(r"\bpaid subscription pricing not shown (?:on|in) the listing\b", "订阅价格未公开", text, flags=re.I)
    text = re.sub(r"\bexact public subscription price not surfaced (?:in the listing)?\b", "公开订阅价未披露", text, flags=re.I)
    text = re.sub(r"\btypical app-store pricing is not shown (?:in the listing)?\b", "", text, flags=re.I)
    text = re.sub(r"\bpricing is not shown (?:on|in) the (?:play )?listing\b", "价格未公开", text, flags=re.I)
    text = re.sub(r"\bin-app purchases available\b", "含内购", text, flags=re.I)
    text = re.sub(r"\bin-app purchases\b", "内购", text, flags=re.I)
    text = re.sub(r"\bafter\s+(\d+)[-\s]?day free trial\b", r"\1天免费试用后", text, flags=re.I)
    text = re.sub(r"\bfree trial\b", "免费试用", text, flags=re.I)
    text = re.sub(r"\bretailer-linked shopping model\b", "零售导购分成模式", text, flags=re.I)
    text = re.sub(r"\bsubscription\b", "订阅", text, flags=re.I)
    text = re.sub(r"\bpremium\b", "高级版", text, flags=re.I)
    text = re.sub(r"\bfree\b", "免费", text, flags=re.I)
    text = re.sub(r"/\s*mo\b", "/月", text, flags=re.I)
    text = re.sub(r"/\s*month\b", "/月", text, flags=re.I)
    text = re.sub(r"/\s*yr\b", "/年", text, flags=re.I)
    text = re.sub(r"/\s*year\b", "/年", text, flags=re.I)
    text = text.replace(";", "；").replace(",", "，")
    text = re.sub(r"\s+or\s+", " 或 ", text, flags=re.I)
    text = re.sub(r"\s*/\s*", "/", text)
    text = re.sub(r"[()（）]", "", text)
    text = re.sub(r"\s*；\s*", "；", text)
    text = re.sub(r"；{2,}", "；", text)
    text = re.sub(r"\s+", " ", text).strip(" ；,，。")

    if not text or text.lower() in {"not shown", "not surfaced"}:
        return "价格未公开"
    return text[:72] or "-"


def _report_clean_pricing_en(value: Any) -> str:
    """Compress competitor pricing into a short English table cell."""
    text = _report_short_text(value, limit=180)
    if text == "-":
        return "-"
    if re.search(r"[\u4e00-\u9fff]", text):
        return "pricing details unclear"
    text = re.sub(r"\((?:[^)]*(?:listing|not shown|typical app-store|public subscription)[^)]*)\)", "", text, flags=re.I)
    text = re.sub(r"\bpricing is not shown (?:on|in) the (?:play )?listing\b", "pricing not disclosed", text, flags=re.I)
    text = re.sub(r"\bpremium pricing not shown (?:on|in) the (?:play )?listing\b", "premium pricing not disclosed", text, flags=re.I)
    text = re.sub(r"\bpaid subscription pricing not shown (?:on|in) the listing\b", "subscription pricing not disclosed", text, flags=re.I)
    text = re.sub(r"\bexact public subscription price not surfaced (?:in the listing)?\b", "subscription pricing not disclosed", text, flags=re.I)
    text = re.sub(r"\btypical app-store pricing is not shown (?:in the listing)?\b", "", text, flags=re.I)
    text = re.sub(r"\s+", " ", text).strip(" ;,，。")
    if not text or text.lower() in {"not shown", "not surfaced"}:
        return "pricing not disclosed"
    return text[:90] or "-"


def _report_clean_pricing_for_language(value: Any, language: str) -> str:
    return _report_clean_pricing_en(value) if _is_report_en(language) else _report_clean_pricing(value)


def _report_positioning_zh(value: Any, name: str = "") -> str:
    """把竞品产品定位规范成中文短句，避免英文长句撑破报告表格。"""
    text = _report_short_text(value, limit=220)
    if text == "-":
        return "相关 App，需进一步核对定位"
    if re.search(r"[\u4e00-\u9fff]", text):
        return text[:72]

    lower = text.lower()
    rules: list[tuple[str, str]] = [
        (r"automatic meal planner|meal planner.*calorie|macro", "自动生成餐食计划，支持热量/营养目标控制"),
        (r"macro-focused|macro focused", "围绕宏量营养目标生成餐食计划"),
        (r"grocery lists?.*meal|meal.*grocery lists?", "生成餐食计划并同步购物清单"),
        (r"recipe organizer|recipe manager", "整理菜谱并管理做菜流程"),
        (r"pantry|what i have|store deals", "根据库存/食材推荐可做菜谱"),
        (r"receipt|expense|budget", "管理收据、消费记录与预算"),
        (r"document scanner|ocr|scan", "扫描文档并提取关键信息"),
        (r"bible|sermon|prayer|devotional", "面向圣经/灵修场景的学习与记录 App"),
        (r"flight|airline|trip", "追踪航班状态与行程变化"),
        (r"running|marathon|run tracker", "记录跑步训练并辅助训练计划"),
        (r"fitness|workout|gym", "提供健身训练计划与运动记录"),
        (r"sleep|meditation|mindfulness", "提供睡眠、冥想与放松训练"),
    ]
    for pattern, label in rules:
        if re.search(pattern, lower):
            return label
    if name:
        return f"{name} 相关 App，需进一步核对具体定位"[:72]
    return "相关 App，需进一步核对定位"


def _report_positioning_en(value: Any, name: str = "") -> str:
    """Compress competitor positioning into one English sentence."""
    text = _report_short_text(value, limit=180)
    if text != "-" and not re.search(r"[\u4e00-\u9fff]", text):
        return text[:110]
    if name:
        return f"{name} related app; verify exact positioning."[:110]
    return "Related app; verify exact positioning."


def _report_positioning_for_language(value: Any, name: str, language: str) -> str:
    return _report_positioning_en(value, name) if _is_report_en(language) else _report_positioning_zh(value, name)


def _report_icon_link(url: str) -> str:
    url = str(url or "").strip()
    if not url or not re.match(r"^https?://", url):
        return "-"
    return f"[↗]({url})"


def _report_repair_reddit_links(report: str, need: dict[str, Any]) -> str:
    """Keep Reddit citations tied to the posts used for this report.

    Models occasionally alter a Reddit slug while copying a citation. A wrong
    source link is worse than an unlinked citation, so only repair links that
    can be matched to a known post title; otherwise remove the hyperlink.
    Non-Reddit competitor/store links are left untouched.
    """
    valid_posts = [p for p in (need.get("posts") or []) if isinstance(p, dict)]
    valid_urls = {
        str(p.get("url") or p.get("hn_url") or "").strip()
        for p in valid_posts
        if str(p.get("url") or p.get("hn_url") or "").strip()
    }
    valid_urls.update(
        str(ev.get("source_url") or "").strip()
        for ev in (need.get("evidence") or [])
        if isinstance(ev, dict) and str(ev.get("source_url") or "").strip()
    )
    title_pairs = [
        (re.sub(r"[^a-z0-9]+", " ", str(p.get("title") or "").lower()).strip(),
         str(p.get("url") or p.get("hn_url") or "").strip())
        for p in valid_posts
    ]

    def replace_link(match: re.Match[str]) -> str:
        label, url = match.group(1), match.group(2)
        if "reddit.com/" not in url.lower() or url in valid_urls:
            return match.group(0)
        label_key = re.sub(r"[^a-z0-9]+", " ", label.lower()).strip()
        if label_key:
            for title_key, known_url in title_pairs:
                if known_url and (label_key == title_key or label_key in title_key or title_key in label_key):
                    return f"[{label}]({known_url})"
        return label

    repaired = re.sub(r"\[([^\]]+)\]\((https?://[^)]+)\)", replace_link, report)

    # Markdown links were handled above. Remove any remaining bare Reddit URL
    # that cannot be traced to the evidence bundle; leaving plain fabricated
    # URLs clickable would bypass the same source-quality guard.
    markdown_destinations = {
        match.group(1)
        for match in re.finditer(r"\]\((https?://[^)]+)\)", repaired)
    }

    def replace_bare_url(match: re.Match[str]) -> str:
        url = match.group(0).rstrip(".,;:，。；：")
        suffix = match.group(0)[len(url):]
        if url in markdown_destinations or "reddit.com/" not in url.lower() or url in valid_urls:
            return match.group(0)
        return suffix

    return re.sub(r"https?://[^\s<>\])]+", replace_bare_url, repaired)


REPORT_LANGUAGE_ZH = "zh-CN"
REPORT_LANGUAGE_EN = "en-US"


def _normalize_report_language(value: Any) -> str:
    return REPORT_LANGUAGE_EN if str(value or "").strip().lower() in {"en", "en-us", "english"} else REPORT_LANGUAGE_ZH


def _is_report_en(language: str) -> bool:
    return _normalize_report_language(language) == REPORT_LANGUAGE_EN


def _report_need_title(need: dict, language: Any) -> str:
    """按报告语言选择需求标题；英文报告优先使用英文需求名，避免原始中文输入覆盖标题。"""
    if _is_report_en(_normalize_report_language(language)):
        title = str(need.get("need_title_en") or "").strip()
        if title:
            return title
    # 报告从某一张需求卡片生成时，标题应锚定这张卡片的聚类主题；
    # original_topic 只作为研究方向保留在描述中，避免列表里所有报告标题相同。
    return str(need.get("need_title") or "report").strip()


def _report_need_description(need: dict, language: Any) -> str:
    """按报告语言选择需求描述，英文报告不把中文 original_topic 拼进正文。"""
    if _is_report_en(_normalize_report_language(language)):
        desc = str(need.get("need_description_en") or "").strip()
        if desc:
            return desc
        return str(need.get("need_description") or "").strip()
    original_topic = str(need.get("original_topic") or "").strip()
    desc = str(need.get("need_description") or "").strip()
    if original_topic and original_topic != str(need.get("need_title") or "").strip():
        return f"用户研究方向：{original_topic}。聚类子主题：{need.get('need_title', '')}。{desc}"
    return desc


def _report_competitor_columns(language: str) -> dict[str, str]:
    if _is_report_en(language):
        return {
            "name": "Competitor",
            "pricing": "Pricing",
            "revenue": "Last 30D Revenue",
            "downloads": "Last 30D Downloads",
            "positioning": "Positioning",
            "app_store": "App Store",
            "sensor_tower": "SensorTower",
        }
    return {
        "name": "竞品名称",
        "pricing": "定价",
        "revenue": "近30天收入",
        "downloads": "近30天下载量",
        "positioning": "产品定位",
        "app_store": "App Store链接",
        "sensor_tower": "SensorTower链接",
    }


def _report_competitor_header(language: str) -> str:
    cols = _report_competitor_columns(language)
    return (
        f"| {cols['name']} | {cols['pricing']} | {cols['revenue']} | {cols['downloads']} | "
        f"{cols['positioning']} | {cols['app_store']} | {cols['sensor_tower']} |"
    )


def _report_sensor_tower_search_url(name: str) -> str:
    return sensor_tower_search_url(name)


def _report_st_cli_unavailable() -> bool:
    """报告生成阶段只做轻量状态判断，失败也不阻断报告。"""
    try:
        status = st_check_available()
        return not bool(status.get("available") and status.get("api_ok"))
    except Exception:
        return True


def _format_competitor_table_context(need: dict, competitor_research: str, language: str = REPORT_LANGUAGE_ZH) -> str:
    """合并 WebSearch 与 ST 数据，给报告模型稳定的竞品表字段。"""
    web_items = _report_extract_competitor_json(competitor_research)
    market = need.get("market_validation") if isinstance(need.get("market_validation"), dict) else None
    st_items = market.get("top_competitors") if market else []
    st_unavailable = not st_items and _report_st_cli_unavailable()
    is_en = _is_report_en(language)
    st_missing_cell = "ST CLI unavailable" if st_unavailable and is_en else ("st cli不可用" if st_unavailable else "-")
    rows: list[dict[str, Any]] = []
    used_names: set[str] = set()

    for comp in st_items or []:
        if not isinstance(comp, dict):
            continue
        name = str(comp.get("name") or "").strip()
        if not name:
            continue
        web = _report_find_web_competitor(name, web_items)
        if web_items and not web:
            continue
        app_store_url = (
            comp.get("app_store_url") or comp.get("store_url")
            or web.get("app_store_url") or web.get("url") or ""
        )
        sensor_tower_url = comp.get("sensor_tower_url") or _report_sensor_tower_search_url(name)
        rows.append({
            "name": name,
            "pricing": _report_clean_pricing_for_language(web.get("pricing") or "-", language),
            "revenue": comp.get("revenue_display") or st_missing_cell,
            "downloads": comp.get("downloads_display") or st_missing_cell,
            "positioning": _report_positioning_for_language(web.get("description") or "", name, language),
            "app_store_url": app_store_url,
            "sensor_tower_cell": _report_icon_link(str(sensor_tower_url or "")) if sensor_tower_url else st_missing_cell,
        })
        used_names.add(_report_competitor_key(name))
        if web.get("name"):
            used_names.add(_report_competitor_key(str(web.get("name"))))

    for web in web_items:
        if not isinstance(web, dict):
            continue
        name = str(web.get("name") or "").strip()
        key = _report_competitor_key(name)
        if not name or key in used_names:
            continue
        rows.append({
            "name": name,
            "pricing": _report_clean_pricing_for_language(web.get("pricing") or "-", language),
            "revenue": st_missing_cell,
            "downloads": st_missing_cell,
            "positioning": _report_positioning_for_language(web.get("description") or "", name, language),
            "app_store_url": web.get("app_store_url") or web.get("url") or "",
            "sensor_tower_cell": (
                st_missing_cell if st_unavailable
                else _report_icon_link(_report_sensor_tower_search_url(name))
            ),
        })
        used_names.add(key)
        if len(rows) >= 6:
            break

    if not rows:
        if is_en:
            return (
                "## Structured Competitor Table Data\n"
                "No stable structured competitor data is available. The Competitive Landscape section must still use the required 7 columns; "
                "use `-` for missing revenue/download data and do not fabricate.\n"
            )
        return (
            "## 结构化竞品表数据\n"
            "当前没有可稳定合并的竞品结构化数据。报告「竞品格局」仍必须使用指定 7 列；"
            "收入和下载无数据填 `-`，不要编造。\n"
        )

    if is_en:
        lines = [
            "## Structured Competitor Table Data (must be prioritized in Competitive Landscape)",
            "The Competitive Landscape section must output 7 columns: Competitor, Pricing, Last 30D Revenue, Last 30D Downloads, Positioning, App Store, SensorTower.",
            "Link columns must use `[↗](url)`; use `-` when no link is available. Revenue/downloads must only copy the values below; do not estimate or fabricate.",
            "Positioning must be one short sentence explaining the primary app scenario.",
            "",
            _report_competitor_header(language),
            "|---|---|---:|---:|---|---|---|",
        ]
    else:
        lines = [
            "## 结构化竞品表数据（报告「竞品格局」必须优先使用）",
            "报告中的「竞品格局」必须输出 7 列：竞品名称、定价、近30天收入、近30天下载量、产品定位、App Store链接、SensorTower链接。",
            "链接列必须使用 `[↗](url)`，没有链接填 `-`；收入/下载只能复制下表，不能自行估算或编造。",
            "产品定位只写一句话，说明该产品主要解决什么场景，不写长段。",
            "",
            _report_competitor_header(language),
            "|---|---|---:|---:|---|---|---|",
        ]
    for row in rows[:6]:
        lines.append(
            "| "
            + " | ".join([
                _report_table_cell(row["name"]),
                _report_table_cell(row["pricing"]),
                _report_table_cell(row["revenue"]),
                _report_table_cell(row["downloads"]),
                _report_table_cell(row["positioning"]),
                _report_icon_link(str(row.get("app_store_url") or "")),
                _report_table_cell(row.get("sensor_tower_cell") or "-"),
            ])
            + " |"
        )
    return "\n".join(lines)


def _report_extract_structured_competitor_table(table_context: str, language: str = REPORT_LANGUAGE_ZH) -> str:
    """从结构化竞品上下文中提取最终报告要使用的 7 列 Markdown 表格。"""
    lines = str(table_context or "").splitlines()
    header = _report_competitor_header(language)
    start = -1
    for i, line in enumerate(lines):
        if line.strip().startswith(header):
            start = i
            break
    if start >= 0:
        table_lines: list[str] = []
        for line in lines[start:]:
            if not line.strip().startswith("|"):
                break
            table_lines.append(line)
        if len(table_lines) >= 2:
            return "\n".join(table_lines)
    if _is_report_en(language):
        return "\n".join([
            _report_competitor_header(language),
            "|---|---|---:|---:|---|---|---|",
            "| - | - | - | - | No stable structured competitor data available | - | - |",
        ])
    return "\n".join([
        _report_competitor_header(language),
        "|---|---|---:|---:|---|---|---|",
        "| - | - | - | - | 暂未拿到可稳定合并的竞品结构化数据 | - | - |",
    ])


def _report_competitor_section_has_new_table(report: str, language: str = REPORT_LANGUAGE_ZH) -> bool:
    section_name = "Competitive Landscape" if _is_report_en(language) else "竞品格局"
    match = re.search(rf"(?ms)^## {re.escape(section_name)}\s*$(.*?)(?=^##\s+|\Z)", str(report or ""))
    if not match:
        return False
    section = match.group(1)
    return _report_competitor_header(language) in section


def _report_split_md_row(line: str) -> list[str]:
    text = str(line or "").strip().strip("|")
    return [cell.strip() for cell in text.split("|")]


def _report_first_link(text: str) -> str:
    match = re.search(r"\((https?://[^)]+)\)", str(text or ""))
    if match:
        return match.group(1)
    match = re.search(r"https?://\S+", str(text or ""))
    return match.group(0).rstrip(".,;") if match else ""


def _report_convert_existing_competitor_table(section_body: str, language: str = REPORT_LANGUAGE_ZH) -> str:
    """把模型已经生成的旧竞品表尽量转换成新版 7 列表。"""
    table_match = re.search(
        r"(?ms)^\s*(\|[^\n]*\|\s*\n\s*\|[ \-:|]+\|\s*\n(?:\s*\|[^\n]*\|\s*\n?)+)",
        str(section_body or ""),
    )
    if not table_match:
        return ""
    table_lines = [line for line in table_match.group(1).splitlines() if line.strip().startswith("|")]
    if len(table_lines) < 3:
        return ""
    header = _report_split_md_row(table_lines[0])
    rows = [_report_split_md_row(line) for line in table_lines[2:]]

    def _idx(*names: str) -> int:
        normalized = [re.sub(r"\s+", "", h).lower() for h in header]
        for name in names:
            key = re.sub(r"\s+", "", name).lower()
            for i, item in enumerate(normalized):
                if item == key or key in item:
                    return i
        return -1

    name_idx = _idx("竞品名称", "产品", "Competitor", "Product", "App")
    pricing_idx = _idx("定价", "价格", "Pricing", "Price")
    revenue_idx = _idx("近30天收入", "收入", "Last 30D Revenue", "Revenue")
    downloads_idx = _idx("近30天下载量", "下载", "Last 30D Downloads", "Downloads")
    positioning_idx = _idx("产品定位", "核心差异化", "类型", "场景", "Positioning", "Type", "Scenario")
    app_store_idx = _idx("App Store链接", "链接", "App Store")
    sensor_idx = _idx("SensorTower链接", "Sensor Tower链接", "SensorTower", "Sensor Tower")
    if name_idx < 0:
        return ""

    out = [
        _report_competitor_header(language),
        "|---|---|---:|---:|---|---|---|",
    ]
    converted = 0
    for row in rows[:8]:
        if name_idx >= len(row):
            continue
        name = re.sub(r"^\*\*|\*\*$", "", row[name_idx]).strip()
        if not name or name in {"-", "#"}:
            continue
        pricing = row[pricing_idx] if 0 <= pricing_idx < len(row) else "-"
        revenue = row[revenue_idx] if 0 <= revenue_idx < len(row) else "-"
        downloads = row[downloads_idx] if 0 <= downloads_idx < len(row) else "-"
        positioning = row[positioning_idx] if 0 <= positioning_idx < len(row) else ""
        app_store_url = _report_first_link(row[app_store_idx]) if 0 <= app_store_idx < len(row) else ""
        sensor_url = _report_first_link(row[sensor_idx]) if 0 <= sensor_idx < len(row) else _report_sensor_tower_search_url(name)
        out.append(
            "| "
            + " | ".join([
                _report_table_cell(name),
                _report_table_cell(_report_clean_pricing_for_language(pricing, language)),
                _report_table_cell(revenue),
                _report_table_cell(downloads),
                _report_table_cell(_report_positioning_for_language(positioning, name, language)),
                _report_icon_link(app_store_url),
                _report_icon_link(sensor_url),
            ])
            + " |"
        )
        converted += 1
    return "\n".join(out) if converted else ""


def _report_normalize_competitor_table_cells(report: str, language: str = REPORT_LANGUAGE_ZH) -> str:
    """读取/刷新报告时规范新版竞品表的定价与产品定位，不改动其它章节。"""
    header_text = _report_competitor_header(language)
    if not isinstance(report, str) or header_text not in report:
        return report
    lines = report.splitlines()
    out = list(lines)
    for idx, line in enumerate(lines):
        if not (line.startswith("|") and line.strip().startswith(header_text)):
            continue
        header = _report_split_md_row(line)
        index = {name: i for i, name in enumerate(header)}
        cols = _report_competitor_columns(language)
        name_idx = index.get(cols["name"])
        pricing_idx = index.get(cols["pricing"])
        positioning_idx = index.get(cols["positioning"])
        if name_idx is None or pricing_idx is None or positioning_idx is None:
            continue
        cursor = idx + 2
        while cursor < len(lines) and lines[cursor].startswith("|"):
            row = _report_split_md_row(lines[cursor])
            if not row or all(not cell or cell == "-" for cell in row):
                cursor += 1
                continue
            padded = row + [""] * max(0, len(header) - len(row))
            name = _strip_markdown_text(padded[name_idx]) if name_idx < len(padded) else ""
            if pricing_idx < len(padded):
                padded[pricing_idx] = _report_clean_pricing_for_language(padded[pricing_idx], language)
            if positioning_idx < len(padded):
                padded[positioning_idx] = _report_positioning_for_language(padded[positioning_idx], name, language)
            out[cursor] = "| " + " | ".join(_report_table_cell(cell) for cell in padded[:len(header)]) + " |"
            cursor += 1
        break
    return "\n".join(out)


def _report_enforce_competitor_table(report: str, table_context: str, language: str = REPORT_LANGUAGE_ZH) -> str:
    """保存报告前强制把「竞品格局」替换为新版 7 列表，避免模型沿用旧表格。"""
    section_name = "Competitive Landscape" if _is_report_en(language) else "竞品格局"
    if not isinstance(report, str) or f"## {section_name}" not in report:
        return report
    structured_table = _report_extract_structured_competitor_table(table_context, language)
    section_match = re.search(rf"(?m)^## {re.escape(section_name)}\s*$", report)
    if not section_match:
        return report

    section_start = section_match.end()
    next_match = re.search(r"(?m)^##\s+", report[section_start:])
    section_end = section_start + next_match.start() if next_match else len(report)
    section_body = report[section_start:section_end]
    converted_existing_table = _report_convert_existing_competitor_table(section_body, language)
    fallback_marker = "No stable structured competitor data available" if _is_report_en(language) else "暂未拿到可稳定合并的竞品结构化数据"
    if fallback_marker in structured_table and converted_existing_table:
        structured_table = converted_existing_table

    # 删除模型生成的任意旧竞品表及其「### 概览」小标题，保留定位空白等非表格内容。
    body = re.sub(
        r"(?ms)^\s*###\s*(?:概览|Overview)\s*\n+",
        "",
        section_body,
        count=1,
    )
    body = re.sub(
        r"(?ms)^\s*\|[^\n]*\|\s*\n\s*\|[ \-:|]+\|\s*\n(?:\s*\|[^\n]*\|\s*\n?)+",
        "",
        body,
        count=1,
    ).strip()

    if body:
        new_section = "\n\n" + structured_table + "\n\n" + body.strip() + "\n\n"
    else:
        new_section = "\n\n" + structured_table + "\n\n"
    return _report_normalize_competitor_table_cells(report[:section_start] + new_section + report[section_end:], language)


def _report_upgrade_for_response(data: dict[str, Any]) -> dict[str, Any]:
    """读取历史报告时临时升级旧竞品表，不直接改写磁盘文件。"""
    report = data.get("final_report")
    if not isinstance(report, str):
        return data
    language = _normalize_report_language(data.get("language") or (REPORT_LANGUAGE_EN if "## Competitive Landscape" in report else REPORT_LANGUAGE_ZH))
    section_name = "Competitive Landscape" if _is_report_en(language) else "竞品格局"
    if f"## {section_name}" not in report:
        return data
    if _report_competitor_section_has_new_table(report, language):
        upgraded = _report_normalize_competitor_table_cells(report, language)
    else:
        upgraded = _report_enforce_competitor_table(
            report,
            _format_competitor_table_context(data.get("need") or {}, "", language),
            language,
        )
    if upgraded == report:
        return data
    copied = dict(data)
    copied["final_report"] = upgraded
    return copied


class DirectReportRequest(BaseModel):
    need_index: int
    demo: bool = False
    language: str = REPORT_LANGUAGE_ZH

@router.post("/generate-report")
def generate_report_direct(req: DirectReportRequest, request: Request):
    ctx = _get_session(request)
    report_language = _normalize_report_language(req.language)
    report_is_en = _is_report_en(report_language)
    def _rt(zh: str, en: str) -> str:
        return en if report_is_en else zh

    needs_data = get_needs(request)["needs"]
    if req.need_index < 0 or req.need_index >= len(needs_data):
        raise HTTPException(status_code=400, detail=_rt("无效的需求索引", "Invalid demand index"))

    need = needs_data[req.need_index]

    # ===== 演示模式：读缓存报告 + 模拟生成过程 =====
    if req.demo:
        _DEMO_DIR = ROOT / "data" / "demo"
        _demo_report_path = _DEMO_DIR / "demo_report.json"
        if not _demo_report_path.exists():
            def _err_stream():
                yield _sse("error", {"message": _rt("演示报告数据不存在", "Demo report data is missing. Please check your local settings.")})
            return StreamingResponse(_err_stream(), media_type="text/event-stream")

        with open(_demo_report_path, "r", encoding="utf-8") as f:
            demo_report_data = json.load(f)
        demo_report_text = demo_report_data.get("final_report", "")
        demo_filename = "demo_report.json"
        saved_demo_report = {
            **demo_report_data,
            "language": report_language,
            "created_at": datetime.now().isoformat(),
        }
        _safe_json_write(ctx.reports_dir / demo_filename, saved_demo_report, indent=2)
        _report_list_cache.pop(str(ctx.reports_dir.resolve()), None)

        def _demo_report_stream():
            import time as _time_mod
            _DEMO_REPORT_STEPS = [
                (_rt("正在整理帖子数据...", "Organizing post data..."), 5),
                (_rt("Lumon 正在并行执行：信号提炼 + 竞品搜索...", "Lumon is running signal extraction and competitor search in parallel..."), 10),
                (_rt("信号提炼完成，分析核心痛点...", "Signal extraction completed. Analyzing core pains..."), 20),
                (_rt("竞品搜索完成：发现 5 个相关产品", "Competitor search completed: found 5 related products"), 30),
                (_rt("正在查询竞品市场数据...", "Querying competitor market data..."), 40),
                (_rt("Lumon 正在生成研究报告...", "Lumon is generating the research report..."), 50),
            ]
            for msg, prog in _DEMO_REPORT_STEPS:
                yield _sse("report_progress", {"progress": prog, "message": msg})
                _time_mod.sleep(0.5)

            chunk_size = max(len(demo_report_text) // 30, 50)
            for i in range(0, len(demo_report_text), chunk_size):
                chunk = demo_report_text[i:i + chunk_size]
                yield _sse("report_chunk", {"text": chunk})
                _time_mod.sleep(0.15)

            yield _sse("report_progress", {"progress": 100, "message": _rt("报告生成完成！", "Report generation completed.")})
            yield _sse("report_done", {"report": demo_report_text, "filename": demo_filename})

        return StreamingResponse(_demo_report_stream(), media_type="text/event-stream")

    def _format_posts_detail(need: dict) -> str:
        lines = []
        for i, post in enumerate(need.get("posts", []), 1):
            lines.append(f"### {_rt('帖子', 'Post')} {i}: {post.get('title', '')}")
            lines.append(f"- {_rt('来源', 'Source')}: {post.get('source', 'unknown')}")
            lines.append(f"- {_rt('赞数', 'Score')}: {post.get('score', 0)} | {_rt('评论数', 'Comments')}: {post.get('num_comments', 0)}")
            lines.append(f"- URL: {post.get('url', '')}")
            content = post.get("content", "")
            if content:
                lines.append(f"- {_rt('内容', 'Content')}: {content[:1500]}")
            comments = post.get("comments", [])
            if comments:
                lines.append(f"- {_rt('评论', 'Comments')}:")
                for c in comments[:8]:
                    lines.append(f"  > {c[:500]}")
            lines.append("")
        return "\n".join(lines)

    def _format_evidence_bundle(need: dict) -> str:
        evidence = need.get("evidence") or []
        market = need.get("market_validation") if isinstance(need.get("market_validation"), dict) else None
        if not evidence:
            lines = [_rt(
                "暂无可追溯 Reddit evidence bundle。没有直接社区证据的判断必须标记为推断。",
                "No traceable Reddit evidence bundle is available. Any judgment without direct community evidence must be labeled as inference.",
            )]
            if market:
                lines.append("")
                lines.append(_rt("## 可追溯 Market Source", "## Traceable Market Source"))
                lines.append(f"- source_id: {market.get('source_id', '')}")
                lines.append(f"- {_rt('级别', 'Level')}: {market.get('label', '')}")
                lines.append(f"- {_rt('口径', 'Scope')}: {_rt('候选竞品', 'candidate competitors')} {market.get('candidate_region', 'US')}, {_rt('收入/下载', 'revenue/downloads')} {market.get('metrics_region') or market.get('market_region', _rt('全球', 'global'))}")
                if market.get("risk_note"):
                    lines.append(f"- {_rt('说明', 'Note')}: {market.get('risk_note')}")
            return "\n".join(lines)
        lines = [_rt("## 可追溯 Evidence Bundle\n", "## Traceable Evidence Bundle\n")]
        for i, ev in enumerate(evidence[:10], 1):
            lines.append(f"### Evidence {i}: {ev.get('source_id') or ev.get('evidence_id', '')}")
            lines.append(f"- evidence_id: {ev.get('evidence_id', '')}")
            lines.append(f"- {_rt('信号', 'Signal')}: {ev.get('signal_label') or ev.get('signal_type', '')}")
            lines.append(f"- {_rt('来源', 'Source')}: {ev.get('platform', '')}/{ev.get('subreddit', '')} | {_rt('帖子赞', 'Post score')}: {ev.get('post_score', 0)} | {_rt('评论赞', 'Comment score')}: {ev.get('comment_score', 0)}")
            lines.append(f"- URL: {ev.get('source_url', '')}")
            context = ev.get("context", "")
            if context:
                lines.append(f"- {_rt('上下文', 'Context')}: {context}")
            lines.append(f"> {ev.get('text', '')}")
            lines.append("")
        if market:
            lines.append(_rt("## 可追溯 Market Source", "## Traceable Market Source"))
            lines.append(f"- source_id: {market.get('source_id', '')}")
            lines.append(f"- {_rt('级别', 'Level')}: {market.get('label', '')}")
            lines.append(f"- {_rt('口径', 'Scope')}: {_rt('候选竞品', 'candidate competitors')} {market.get('candidate_region', 'US')}, {_rt('收入/下载', 'revenue/downloads')} {market.get('metrics_region') or market.get('market_region', _rt('全球', 'global'))}")
            competitors = market.get("top_competitors") or []
            for comp in competitors[:5]:
                if not isinstance(comp, dict):
                    continue
                lines.append(
                    f"- {_rt('竞品', 'Competitor')} source_id={comp.get('source_id', '')}: {comp.get('name', '')}, "
                    f"{_rt('收入', 'revenue')} {comp.get('revenue_display', '-')}, {_rt('下载', 'downloads')} {comp.get('downloads_display', '-')}, "
                    f"App Store {comp.get('app_store_url') or comp.get('store_url') or '-'}, "
                    f"SensorTower {comp.get('sensor_tower_url') or '-'}"
                )
            if market.get("risk_note"):
                lines.append(f"- {_rt('说明', 'Note')}: {market.get('risk_note')}")
            lines.append("")
        return "\n".join(lines)


    def _parse_signal_result(raw: str) -> dict | None:
        """解析信号提炼的 JSON 结果，失败返回 None。"""
        import re as _re_sig
        try:
            return json.loads(raw)
        except Exception:
            m = _re_sig.search(r'\{[\s\S]*\}', raw)
            if m:
                try:
                    return json.loads(m.group())
                except Exception:
                    pass
        return None

    def _build_filtered_posts_summary(
        posts: list[dict], signal_json: dict | None, need_title: str
    ) -> tuple[str, int, int, int]:
        """根据信号提炼结果对帖子分级，生成分层摘要。

        返回 (summary_text, relevant_count, total_quotes, total_sources)
        """
        post_relevance: dict[str, str] = {}
        if signal_json:
            for sig in signal_json.get("extracted_signals", []):
                title = sig.get("post_title", "").strip()
                rel = sig.get("relevance", "低")
                if title:
                    post_relevance[title.lower()] = rel

        high_posts: list[dict] = []
        mid_posts: list[dict] = []
        low_posts: list[dict] = []

        for p in posts:
            title_key = p.get("title", "").strip().lower()
            rel = post_relevance.get(title_key, "中")
            if rel == "高":
                high_posts.append(p)
            elif rel in ("中", "中高"):
                mid_posts.append(p)
            else:
                low_posts.append(p)

        # 没有信号分类结果时，全部视为中相关
        if not signal_json:
            mid_posts = posts
            high_posts = []
            low_posts = []

        high_posts = high_posts[:15]
        mid_posts = mid_posts[:20]
        low_posts = low_posts[:10]

        lines: list[str] = []
        source_set: set[str] = set()

        if high_posts:
            lines.append(_rt(f"## 高相关帖子（{len(high_posts)} 个）\n", f"## High-Relevance Posts ({len(high_posts)})\n"))
            for i, p in enumerate(high_posts, 1):
                src = p.get("source", "").split("/")[0] if "/" in p.get("source", "") else p.get("source", "unknown")
                source_set.add(src)
                lines.append(f"### [{i}] {p.get('title', '')}")
                lines.append(f"- {_rt('来源', 'Source')}: {p.get('source', '')} | {_rt('赞', 'Score')}: {p.get('score', 0)} | {_rt('评论', 'Comments')}: {p.get('num_comments', 0)} | URL: {p.get('url', '')}")
                content = p.get("content", "")
                if content:
                    lines.append(f"- {_rt('内容', 'Content')}: {content[:800]}")
                comments = p.get("comments", [])
                if comments:
                    lines.append(f"- {_rt('评论', 'Comments')}:")
                    for c in comments[:5]:
                        lines.append(f"  > {c[:300]}")
                lines.append("")

        if mid_posts:
            lines.append(_rt(f"\n## 中相关帖子（{len(mid_posts)} 个）\n", f"\n## Medium-Relevance Posts ({len(mid_posts)})\n"))
            for i, p in enumerate(mid_posts, 1):
                src = p.get("source", "").split("/")[0] if "/" in p.get("source", "") else p.get("source", "unknown")
                source_set.add(src)
                lines.append(f"[{i}] {p.get('title', '')} | {p.get('source', '')} | {_rt('赞', 'Score')}: {p.get('score', 0)} | URL: {p.get('url', '')}")
                content = p.get("content", "")
                if content:
                    lines.append(f"  {_rt('摘要', 'Summary')}: {content[:300]}")

        if low_posts:
            lines.append(_rt(f"\n## 低相关帖子（{len(low_posts)} 个）\n", f"\n## Low-Relevance Posts ({len(low_posts)})\n"))
            for p in low_posts:
                lines.append(f"- {p.get('title', '')}（{p.get('source', '')}）")

        relevant_count = len(high_posts) + len(mid_posts)
        total_quotes = 0
        if signal_json:
            for sig in signal_json.get("extracted_signals", []):
                if sig.get("relevance") in ("高", "中", "中高"):
                    total_quotes += len(sig.get("verbatim_quotes", []))

        return "\n".join(lines), relevant_count, total_quotes, len(source_set)

    def _report_emit(progress: int, message: str):
        with ctx.report_lock:
            ctx.report_job["progress"] = progress
            ctx.report_job["message"] = message

    def _report_chunk(text: str):
        with ctx.report_lock:
            ctx.report_job["chunks"].append(text)

    def _run_report_bg():
        set_thread_session(ctx)
        try:
            _report_emit(5, _rt("正在整理帖子数据...", "Organizing post data..."))

            all_posts = need.get("posts", [])
            full_posts_summary = _format_posts_detail(need)
            evidence_bundle_text = _format_evidence_bundle(need)
            post_count = len(all_posts)

            deep_dive_data = ""
            dmp = need.get("deep_mine_package")
            if dmp:
                deep_dive_data = json.dumps(dmp, ensure_ascii=False, indent=2)

            report_title = _report_need_title(need, report_language)
            report_desc = _report_need_description(need, report_language)

            _report_emit(10, _rt("Lumon 正在并行执行：信号提炼 + 竞品搜索...", "Lumon is running signal extraction and competitor search in parallel..."))

            import concurrent.futures as _cf

            signal_prompt = SIGNAL_EXTRACTION_PROMPT \
                .replace("{need_title}", report_title) \
                .replace("{need_description}", report_desc) \
                .replace("{posts_summary}", full_posts_summary)
            signal_messages = [
                {"role": "system", "content": f"你是一位资深用户研究分析师。你的任务是深入理解「{report_title}」这个需求，然后从帖子数据中精准提炼出与之相关的信号。只输出 JSON，不要多余内容。"},
                {"role": "user", "content": signal_prompt},
            ]

            def _run_signal_extraction():
                set_thread_session(ctx)
                chunks = []
                for chunk in call_llm_stream(signal_messages):
                    chunks.append(chunk)
                return "".join(chunks)

            _comp_state = {"msgs": [], "failed": False}
            def _comp_progress(msg):
                _comp_state["msgs"].append(msg)
                if msg.startswith("⚠️"):
                    _comp_state["failed"] = True

            def _run_competitor_search():
                set_thread_session(ctx)
                try:
                    return search_competitors(
                        need_title=report_title,
                        need_description=f"{report_desc}\n\n帖子关键内容摘要：\n{full_posts_summary[:2000]}",
                        posts_hint=full_posts_summary[:2000],
                        progress_callback=_comp_progress,
                        web_search_engine=ctx.web_search_engine,
                    )
                except Exception as e:
                    _comp_state["failed"] = True
                    _comp_state["msgs"].append(_rt(
                        f"⚠️ 竞品搜索异常：{str(e)[:100]}，请在设置 > WebSearch 中检查引擎配置",
                        f"⚠️ Competitor search failed: {str(e)[:100]}. Please check your local settings.",
                    ))
                    return _rt("（竞品搜索失败）", "(Competitor search failed)")

            _PARALLEL_TIMEOUT = 180
            with _cf.ThreadPoolExecutor(max_workers=2) as executor:
                signal_future = executor.submit(_run_signal_extraction)
                comp_future = executor.submit(_run_competitor_search)

                import time as _rpt_time
                _phase_msgs = [
                    _rt("信号分析器正在评估帖子相关度...", "The signal analyzer is scoring post relevance..."),
                    _rt("提取用户痛点和使用场景...", "Extracting user pains and usage scenarios..."),
                    _rt("竞品搜索进行中，收集定价和评分...", "Competitor search is collecting pricing and ratings..."),
                    _rt("整理竞品链接和用户评价...", "Organizing competitor links and user feedback..."),
                    _rt("深度分析竞品数据...", "Analyzing competitor data in depth..."),
                    _rt("信号提炼接近完成...", "Signal extraction is nearly complete..."),
                    _rt("等待竞品搜索返回...", "Waiting for competitor search results..."),
                    _rt("竞品数据汇总中...", "Summarizing competitor data..."),
                    _rt("前置分析即将完成...", "Pre-analysis is nearly complete..."),
                ]
                _msg_idx = 0
                _max_p = 10
                _parallel_start = _rpt_time.time()
                while not (signal_future.done() and comp_future.done()):
                    if _rpt_time.time() - _parallel_start > _PARALLEL_TIMEOUT:
                        _report_emit(_max_p, _rt("并行阶段超时，继续使用已完成的结果...", "Parallel analysis timed out. Continuing with completed results..."))
                        break
                    _rpt_time.sleep(3)
                    _max_p = min(_max_p + 2, 48)
                    if _msg_idx < len(_phase_msgs):
                        _msg = _phase_msgs[_msg_idx]
                        _msg_idx += 1
                    else:
                        parts = []
                        if signal_future.done():
                            parts.append(_rt("信号提炼 ✓", "Signal extraction ✓"))
                        else:
                            parts.append(_rt("信号提炼中...", "Signal extraction in progress..."))
                        if comp_future.done():
                            parts.append(_rt("竞品搜索 ✓", "Competitor search ✓"))
                        elif _comp_state["msgs"]:
                            parts.append(_comp_state["msgs"][-1][:40])
                        else:
                            parts.append(_rt("竞品搜索中...", "Competitor search in progress..."))
                        _msg = " | ".join(parts)
                    _report_emit(_max_p, _msg)

                signal_result = signal_future.result(timeout=5) if signal_future.done() else ""
                competitor_research = comp_future.result(timeout=5) if comp_future.done() else _rt("（竞品搜索超时）", "(Competitor search timed out)")

            print(f"[SignalExtraction] 信号提炼完成，长度={len(signal_result)}")

            signal_json = _parse_signal_result(signal_result)
            if signal_json:
                sigs = signal_json.get("extracted_signals", [])
                high_count = sum(1 for s in sigs if s.get("relevance") == "高")
                mid_count = sum(1 for s in sigs if s.get("relevance") in ("中", "中高"))
                low_count = sum(1 for s in sigs if s.get("relevance") in ("低", "无关"))
                print(f"[SignalFilter] 帖子分级：高={high_count} 中={mid_count} 低/无关={low_count}")
            else:
                print("[SignalFilter] 信号提炼 JSON 解析失败，帖子将全量传入报告")

            filtered_summary, relevant_count, quote_count, source_count = \
                _build_filtered_posts_summary(all_posts, signal_json, report_title)

            sources = list(set(
                p.get("source", "").split("/")[0] if "/" in p.get("source", "") else p.get("source", "unknown")
                for p in all_posts
            ))
            sources_str = ", ".join(s.capitalize() for s in sources) if sources else ("unknown" if report_is_en else "未知")

            _report_emit(30, _rt(
                f"信号提炼完成（{relevant_count}/{post_count} 个帖子高度相关）",
                f"Signal extraction completed ({relevant_count}/{post_count} posts are highly relevant)",
            ))

            comp_failed = _comp_state["failed"]
            if comp_failed:
                _report_emit(34, _rt(
                    "竞品搜索失败，将生成无竞品数据的报告（可稍后重试）",
                    "Competitor search failed. The report will be generated without competitor data for now.",
                ))
                competitor_research = _rt(
                    "（竞品联网搜索失败，报告中竞品相关章节数据不足，请在设置 > WebSearch 中检查引擎配置后重新生成）",
                    "(Competitor web search failed. Competitor sections may be incomplete. Please check your local settings and regenerate later.)",
                )

            _report_emit(45, _rt("竞品调研完成，Lumon 正在撰写分析报告...", "Competitor research completed. Lumon is drafting the analysis report..."))

            competitor_table_context = _format_competitor_table_context(need, competitor_research, report_language)
            if report_is_en:
                comp_data_note = (
                    "\n⚠️ Competitive Landscape: must use the 7-column table "
                    "(Competitor/Pricing/Last 30D Revenue/Last 30D Downloads/Positioning/App Store/SensorTower). "
                    "Positioning must be one short sentence; link columns use `[↗](url)` or `-`. "
                    "Revenue/downloads must only use SensorTower numbers from the structured competitor table; do not fabricate."
                )
            else:
                comp_data_note = (
                    "\n⚠️ 竞品格局：必须使用 7 列表格（竞品名称/定价/近30天收入/近30天下载量/产品定位/"
                    "App Store链接/SensorTower链接）。产品定位只写一句话；两个链接列用 `[↗](url)`，没有则填 `-`。"
                    "收入和下载只能使用结构化竞品表数据中的 SensorTower 数字，不要编造。"
                )

            direct_prompt = DIRECT_REPORT_PROMPT_EN if report_is_en else DIRECT_REPORT_PROMPT
            prompt_text = direct_prompt \
                .replace("{need_title}", report_title) \
                .replace("{need_description}", report_desc) \
                .replace("{posts_summary}", filtered_summary) \
                .replace("{deep_dive_data}", deep_dive_data or _rt("（暂无深挖数据）", "(No deep-dive data available)")) \
                .replace("{competitor_research}", competitor_research) \
                .replace("{sources}", sources_str) \
                .replace("{post_count}", str(post_count))

            signal_context_parts = [
                (
                    "Before generating the report, the signal analyzer assessed demand understanding and post relevance:\n"
                    if report_is_en else
                    "在生成报告之前，信号分析器已对帖子数据做了需求理解和相关度评估：\n"
                )
            ]
            if signal_json:
                understanding = signal_json.get("need_understanding", {})
                if report_is_en:
                    signal_context_parts.append(
                        f"- Core users: {understanding.get('core_users', 'unknown')}\n"
                        f"- Core scenario: {understanding.get('core_scenario', 'unknown')}\n"
                        f"- Core pain: {understanding.get('core_pain', 'unknown')}\n"
                        f"- Overall signal summary: {signal_json.get('overall_signal_summary', '')}\n"
                    )
                else:
                    signal_context_parts.append(
                        f"- 核心用户：{understanding.get('core_users', '未知')}\n"
                        f"- 核心场景：{understanding.get('core_scenario', '未知')}\n"
                        f"- 核心痛点：{understanding.get('core_pain', '未知')}\n"
                        f"- 综合信号：{signal_json.get('overall_signal_summary', '')}\n"
                    )
            else:
                signal_context_parts.append(f"{signal_result[:800]}\n")

            if report_is_en:
                signal_context_parts.append(
                    f"\nPost data has been tiered by relevance (high=relevant full content, medium=summaries, low=titles only).\n"
                    f"📊 {post_count} posts were reviewed; {relevant_count} are directly related to the topic, across {source_count} data sources."
                    f"{comp_data_note}\n\n"
                    "⚠️ Evidence constraint: key conclusions must prioritize the Evidence Bundle below. Market size, willingness to pay, and competitor strength without source_id/evidence_id support must be framed as inference, not fact.\n\n"
                    f"{competitor_table_context}\n\n"
                    f"{evidence_bundle_text}\n\n"
                )
            else:
                signal_context_parts.append(
                    f"\n帖子数据已按相关度分层（高相关=完整内容、中相关=摘要、低相关=仅标题）。\n"
                    f"📊 共 {post_count} 个帖子，{relevant_count} 个与主题直接相关，来自 {source_count} 个数据源。"
                    f"{comp_data_note}\n\n"
                    "⚠️ 证据约束：关键结论必须优先引用下方 Evidence Bundle；没有 source_id / evidence_id 支撑的市场规模、付费意愿、竞品强弱只能写成推断，不要写成事实。\n\n"
                    f"{competitor_table_context}\n\n"
                    f"{evidence_bundle_text}\n\n"
                )
            report_context = "".join(signal_context_parts)

            _report_emit(50, _rt("Lumon 正在撰写分析报告...", "Lumon is drafting the analysis report..."))

            if report_is_en:
                system_prompt = (
                    "You are a senior product analyst who finds product opportunities from real user feedback. "
                    "Write the full report in English. Recommend only consumer-facing overseas-market apps, software, or AI tools; "
                    "do not recommend physical hardware. The competitor table must list only real software products.\n\n"
                    f"⚠️ Most important constraint: the report must stay tightly focused on \"{report_title}\". "
                    "Every pain point, competitor, and product concept must be directly relevant to this topic."
                )
            else:
                system_prompt = f"你是一位资深产品分析师，擅长从用户反馈中发现产品机会。只推荐面向 C 端海外市场的 App/软件/AI 工具方案，不涉及实物硬件。竞品格局表格只列真实存在的软件产品。\n\n⚠️ 最重要的约束：这份报告必须紧密围绕「{report_title}」展开，所有分析、痛点、竞品都必须与这个主题直接相关。不要偏离到其他方向。"

            messages = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": report_context + prompt_text},
            ]

            for chunk_text in call_llm_stream(messages, max_tokens=16000):
                _report_chunk(chunk_text)

            with ctx.report_lock:
                all_chunks = ctx.report_job["chunks"]
            if not all_chunks:
                print("[ReportGen] WARNING: LLM returned empty report")
                with ctx.report_lock:
                    ctx.report_job["error"] = _rt(
                        "模型未返回任何报告内容，请检查 API 配置后重试",
                        "The model returned no report content. Please check your local settings.",
                    )
                    ctx.report_job["active"] = False
                return

            report = _report_enforce_competitor_table("".join(all_chunks), competitor_table_context, report_language)
            report = _report_repair_reddit_links(report, need)

            _report_emit(90, _rt("正在保存报告...", "Saving report..."))

            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            slug_src = report_title or need.get("original_topic", "") or need.get("need_title", "report")
            slug = slug_src[:30].replace(" ", "_").replace("/", "-")
            filename = f"{timestamp}_{slug}.json"
            report_data = {
                "need": need,
                "final_report": report,
                "report_format": "markdown",
                "language": report_language,
                "debate_rounds": 0,
                "created_at": datetime.now().isoformat(),
            }
            _safe_json_write(ctx.reports_dir / filename, report_data, indent=2)

            with ctx.report_lock:
                ctx.report_job["progress"] = 100
                ctx.report_job["message"] = _rt("报告生成完成！", "Report generation completed.")
                ctx.report_job["filename"] = filename
                ctx.report_job["done"] = True
                ctx.report_job["active"] = False
            print(f"[ReportGen] OK session={ctx.session_id} file={filename} len={len(report)}")

        except Exception as e:
            _log_sse_error("ReportGen", e, ctx)
            with ctx.report_lock:
                ctx.report_job["error"] = _friendly_error_for_language(report_language, e)
                ctx.report_job["active"] = False
        finally:
            clear_thread_session()

    # ===== 同步预检：模型不可用直接返回错误，不启动后台线程 =====
    set_thread_session(ctx)
    try:
        llm_ok, llm_err = check_llm_available()
    finally:
        clear_thread_session()
    if not llm_ok:
        model_name = "GPT" if ctx._general_model == "gpt" else "Claude"
        err_msg = _rt(f"{model_name} 模型不可用，请检查本地配置", f"{model_name} model is unavailable. Please check your local settings.")
        def _err_stream():
            yield _sse("error", {"message": err_msg})
        return StreamingResponse(_err_stream(), media_type="text/event-stream")

    # 启动后台线程
    with ctx.report_lock:
        ctx.report_job = ctx._empty_report_job()
        ctx.report_job["active"] = True
        ctx.report_job["need_index"] = req.need_index
        ctx.report_job["language"] = report_language
    t = threading.Thread(target=_run_report_bg, daemon=True)
    ctx.report_thread = t
    t.start()

    # SSE 流从 report_job 读取事件，客户端断开不影响后台线程
    def event_stream() -> Generator[str, None, None]:
        _last_progress = -1
        _chunk_cursor = 0
        while True:
            with ctx.report_lock:
                job = ctx.report_job
                active = job["active"]
                progress = job["progress"]
                message = job["message"]
                chunks = job["chunks"]
                error = job["error"]
                done = job["done"]
                filename = job["filename"]

            if error:
                yield _sse("error", {"message": error})
                return

            if progress != _last_progress and message:
                yield _sse("report_progress", {"progress": progress, "message": message})
                _last_progress = progress

            if _chunk_cursor < len(chunks):
                for i in range(_chunk_cursor, len(chunks)):
                    yield _sse("report_chunk", {"text": chunks[i]})
                _chunk_cursor = len(chunks)

            if done:
                yield _sse("report_progress", {"progress": 100, "message": _rt("报告生成完成！", "Report generation completed.")})
                yield _sse("report_done", {"report": "".join(chunks), "filename": filename})
                yield "\n"
                return

            if not active and not done:
                return

            _time.sleep(0.5)

    return StreamingResponse(event_stream(), media_type="text/event-stream")


@router.get("/report-gen/status")
def report_gen_status(request: Request):
    """报告生成状态（用于页面刷新后恢复）。"""
    ctx = _get_session(request)
    with ctx.report_lock:
        job = ctx.report_job
        return {
            "active": job["active"],
            "need_index": job["need_index"],
            "progress": job["progress"],
            "message": job["message"],
            "error": job["error"],
            "done": job["done"],
            "filename": job["filename"],
            "chunk_count": len(job["chunks"]),
        }


@router.get("/report-gen/stream")
def report_gen_stream(request: Request):
    """重连 SSE 流，从 report_job 当前状态继续读取。"""
    ctx = _get_session(request)

    def event_stream() -> Generator[str, None, None]:
        _last_progress = -1
        _chunk_cursor = 0
        while True:
            with ctx.report_lock:
                job = ctx.report_job
                active = job["active"]
                progress = job["progress"]
                message = job["message"]
                chunks = job["chunks"]
                error = job["error"]
                done = job["done"]
                filename = job["filename"]
                language = job.get("language", REPORT_LANGUAGE_ZH)

            if error:
                yield _sse("error", {"message": error})
                return

            if progress != _last_progress and message:
                yield _sse("report_progress", {"progress": progress, "message": message})
                _last_progress = progress

            if _chunk_cursor < len(chunks):
                for i in range(_chunk_cursor, len(chunks)):
                    yield _sse("report_chunk", {"text": chunks[i]})
                _chunk_cursor = len(chunks)

            if done:
                yield _sse("report_progress", {
                    "progress": 100,
                    "message": "Report generation completed." if _is_report_en(_normalize_report_language(language)) else "报告生成完成！",
                })
                yield _sse("report_done", {"report": "".join(chunks), "filename": filename})
                yield "\n"
                return

            if not active and not done:
                return

            _time.sleep(0.5)

    return StreamingResponse(event_stream(), media_type="text/event-stream")


# ============================================================
# Reports routes
# ============================================================

_report_list_cache: dict[str, dict] = {}


def _split_markdown_table_row(line: str) -> list[str]:
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def _markdown_link_url(cell: str) -> str:
    match = re.search(r"\[[^\]]+\]\((https?://[^)]+)\)", str(cell or ""))
    if match:
        return match.group(1).strip()
    text = str(cell or "").strip()
    return text if text.startswith(("http://", "https://")) else ""


def _strip_markdown_text(cell: str) -> str:
    text = re.sub(r"\[[^\]]+\]\([^)]+\)", "", str(cell or ""))
    text = re.sub(r"[*_`]+", "", text)
    return text.strip()


def _find_report_competitor_table(lines: list[str]) -> tuple[int, int, list[str], list[list[str]]] | None:
    """定位报告中的新版竞品格局表。返回 header 起止行和 rows。"""
    valid_headers = {
        _report_competitor_header(REPORT_LANGUAGE_ZH),
        _report_competitor_header(REPORT_LANGUAGE_EN),
    }
    for idx, line in enumerate(lines):
        if not (line.startswith("|") and line.strip() in valid_headers):
            continue
        header = _split_markdown_table_row(line)
        end = idx + 1
        rows: list[list[str]] = []
        cursor = idx + 1
        if cursor < len(lines) and re.match(r"^\|[\s\-:|]+\|$", lines[cursor]):
            cursor += 1
        while cursor < len(lines) and lines[cursor].startswith("|"):
            rows.append(_split_markdown_table_row(lines[cursor]))
            cursor += 1
        end = cursor
        return idx, end, header, rows
    return None


def _result_for_competitor(name: str, results: list[dict[str, Any]]) -> dict[str, Any] | None:
    key = _report_competitor_key(name)
    if not key:
        return None
    for item in results:
        item_keys = [
            _report_competitor_key(str(item.get("original_name") or "")),
            _report_competitor_key(str(item.get("name") or "")),
        ]
        if key in item_keys:
            return item
    for item in results:
        item_keys = [
            _report_competitor_key(str(item.get("original_name") or "")),
            _report_competitor_key(str(item.get("name") or "")),
        ]
        if any(item_key and (item_key in key or key in item_key) for item_key in item_keys):
            return item
    return None


def _report_metric_window_30d() -> tuple[date, date]:
    """报告竞品表统一用 SensorTower 网页 Summary 的过去 30 天口径。"""
    end = date.today() - timedelta(days=2)
    start = end - timedelta(days=29)
    return start, end


def _report_format_currency(value: Any) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "-"
    if number >= 1_000_000:
        return f"${number / 1_000_000:.1f}M"
    if number >= 100_000:
        return f"${number / 1_000:.1f}K".replace(".0K", "K")
    if number >= 1_000:
        return f"${number / 1_000:.0f}K"
    return f"${number:.0f}"


def _report_format_number(value: Any) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "-"
    if number >= 1_000_000:
        return f"{number / 1_000_000:.1f}M"
    if number >= 1_000:
        return f"{number / 1_000:.0f}K"
    return f"{number:.0f}"


def _fetch_report_competitor_snapshot(name: str, app_store_url: str = "") -> dict[str, Any]:
    """给报告竞品表补齐单个 App 的近 30 天 ST 指标，失败不抛出。"""
    clean_name = str(name or "").strip()
    if not clean_name:
        return {
            "name": "",
            "original_name": name,
            "store_url": app_store_url,
            "sensor_tower_url": "",
            "has_st_data": False,
        }

    start_date, end_date = _report_metric_window_30d()
    try:
        app = st_fetch_app_snapshot(
            clean_name,
            start_date=start_date,
            end_date=end_date,
            app_store_url=app_store_url,
        )
    except Exception as e:
        print(f"[ReportCompetitorRefresh] snapshot failed name={clean_name}: {e}")
        app = None

    if not app:
        return {
            "name": clean_name,
            "original_name": clean_name,
            "store_url": app_store_url,
            "sensor_tower_url": _report_sensor_tower_search_url(clean_name),
            "has_st_data": False,
        }

    revenue = app.get("revenue_snapshot")
    downloads = app.get("downloads_snapshot")
    has_st_data = revenue is not None or downloads is not None
    resolved_name = str(app.get("name") or clean_name).strip()
    store_url = str(app.get("app_store_url") or app.get("store_url") or app_store_url or "").strip()
    return {
        "name": resolved_name or clean_name,
        "original_name": clean_name,
        "store_url": store_url,
        "sensor_tower_url": app.get("sensor_tower_url") or _report_sensor_tower_search_url(resolved_name or clean_name),
        "has_st_data": has_st_data,
        "revenue_display": _report_format_currency(revenue) if revenue is not None else "-",
        "downloads_display": _report_format_number(downloads) if downloads is not None else "-",
    }


def _refresh_competitor_table_markdown(report: str) -> tuple[str, dict[str, Any]]:
    """重新查询 ST 并替换报告内竞品格局表的 ST 依赖列。"""
    status = st_check_available()
    if not bool(status.get("available") and status.get("api_ok")):
        return report, {"ok": False, "error": "st cli不可用"}

    lines = report.splitlines()
    table = _find_report_competitor_table(lines)
    if not table:
        return report, {"ok": False, "error": "未找到可刷新的竞品表格"}

    start, end, header, rows = table
    index = {name: i for i, name in enumerate(header)}
    language = REPORT_LANGUAGE_EN if header and header[0] == _report_competitor_columns(REPORT_LANGUAGE_EN)["name"] else REPORT_LANGUAGE_ZH
    cols = _report_competitor_columns(language)
    name_idx = index.get(cols["name"])
    if name_idx is None:
        return report, {"ok": False, "error": "竞品表缺少竞品名称列"}

    app_store_idx = index.get(cols["app_store"])
    revenue_idx = index.get(cols["revenue"])
    downloads_idx = index.get(cols["downloads"])
    sensor_idx = index.get(cols["sensor_tower"])
    required = [revenue_idx, downloads_idx, sensor_idx]
    if any(i is None for i in required):
        return report, {"ok": False, "error": "竞品表缺少可刷新的 ST 数据列"}

    competitors: list[dict[str, str]] = []
    for row in rows:
        if name_idx >= len(row):
            continue
        name = _strip_markdown_text(row[name_idx])
        if not name or name == "-":
            continue
        app_url = _markdown_link_url(row[app_store_idx]) if app_store_idx is not None and app_store_idx < len(row) else ""
        competitors.append({"name": name, "url": app_url})

    if not competitors:
        return report, {"ok": False, "error": "竞品表没有可查询的竞品名称"}

    query_limit = min(len(competitors), 8)
    st_results = [
        _fetch_report_competitor_snapshot(item["name"], item.get("url", ""))
        for item in competitors[:query_limit]
    ]
    result_count = len([item for item in st_results if item.get("has_st_data")])
    updated_rows: list[str] = [
        "| " + " | ".join(header) + " |",
        "|---|---|---:|---:|---|---|---|",
    ]

    for row in rows:
        padded = row + [""] * max(0, len(header) - len(row))
        name = _strip_markdown_text(padded[name_idx])
        result = _result_for_competitor(name, st_results)
        if result and result.get("has_st_data"):
            padded[revenue_idx] = _report_table_cell(result.get("revenue_display") or "-")  # type: ignore[index]
            padded[downloads_idx] = _report_table_cell(result.get("downloads_display") or "-")  # type: ignore[index]
            store_url = str(result.get("store_url") or "").strip()
            current_store_url = (
                _markdown_link_url(padded[app_store_idx])
                if app_store_idx is not None and app_store_idx < len(padded)
                else ""
            )
            if app_store_idx is not None and store_url and not current_store_url and app_store_idx < len(padded):
                padded[app_store_idx] = _report_icon_link(store_url)
            padded[sensor_idx] = _report_icon_link(result.get("sensor_tower_url") or _report_sensor_tower_search_url(name))  # type: ignore[index]
        elif result:
            padded[revenue_idx] = "未匹配"  # type: ignore[index]
            padded[downloads_idx] = "未匹配"  # type: ignore[index]
            padded[sensor_idx] = _report_icon_link(result.get("sensor_tower_url") or _report_sensor_tower_search_url(name))  # type: ignore[index]
        else:
            padded[revenue_idx] = _report_table_cell(padded[revenue_idx])  # type: ignore[index]
            padded[downloads_idx] = _report_table_cell(padded[downloads_idx])  # type: ignore[index]
            padded[sensor_idx] = _report_icon_link(_report_sensor_tower_search_url(name))  # type: ignore[index]
        updated_rows.append("| " + " | ".join(_report_table_cell(cell) for cell in padded[:len(header)]) + " |")

    new_lines = lines[:start] + updated_rows + lines[end:]
    return _report_normalize_competitor_table_cells("\n".join(new_lines), language), {
        "ok": True,
        "queried": query_limit,
        "matched": result_count,
    }

@router.get("/reports")
def list_reports(request: Request):
    ctx = _get_session(request)
    reports_dir = ctx.reports_dir
    cache_key = str(reports_dir.resolve())
    report_files = sorted(reports_dir.glob("*.json"), reverse=True)
    if not report_files:
        return {"reports": []}
    latest_mtime = max(f.stat().st_mtime for f in report_files)
    cached = _report_list_cache.get(cache_key)
    if cached and cached["data"] is not None and latest_mtime <= cached["mtime"]:
        return {"reports": cached["data"]}

    reports = []
    for rf in report_files:
        try:
            with open(rf, "r", encoding="utf-8") as f:
                data = json.load(f)
            language = _normalize_report_language(data.get("language") or REPORT_LANGUAGE_ZH)
            need_obj = data.get("need", {}) if isinstance(data.get("need"), dict) else {}
            title = _report_need_title(need_obj, language) if need_obj else data.get("post", {}).get("title", "未知")
            report_content = data.get("final_report", "")
            verdict = ""
            femwc_total = None
            ai_fit = ""
            if isinstance(report_content, dict):
                verdict = report_content.get("verdict", "")
                ai_fit = report_content.get("ai_fit", "")
                fs = report_content.get("femwc_scores") or report_content.get("femwc_after") or {}
                if isinstance(fs, dict) and "total" in fs:
                    femwc_total = fs["total"]
            elif isinstance(report_content, str):
                try:
                    rj = json.loads(report_content)
                    verdict = rj.get("verdict", "")
                    ai_fit = rj.get("ai_fit", "")
                    fs = rj.get("femwc_scores") or rj.get("femwc_after") or {}
                    if isinstance(fs, dict) and "total" in fs:
                        femwc_total = fs["total"]
                except Exception:
                    pass
            reports.append({
                "filename": rf.name,
                "title": title,
                "created_at": data.get("created_at", ""),
                "rounds": data.get("debate_rounds", 0),
                "report_format": data.get("report_format", "json"),
                "verdict": verdict,
                "femwc_total": femwc_total,
                "ai_fit": ai_fit,
            })
        except Exception:
            pass
    _report_list_cache[cache_key] = {"mtime": latest_mtime, "data": reports}
    return {"reports": reports}


@router.get("/reports/{filename}")
def get_report(filename: str, request: Request):
    ctx = _get_session(request)
    fpath = _safe_path(ctx.reports_dir, filename)
    if not fpath.exists():
        raise HTTPException(status_code=404, detail="Report not found")
    with open(fpath, "r", encoding="utf-8") as f:
        data = json.load(f)
    return _report_upgrade_for_response(data)


@router.post("/reports/{filename}/refresh-competitors")
def refresh_report_competitors(filename: str, request: Request):
    """重新查询报告竞品表中的 ST 数据；只替换表格，不重写报告结论。"""
    ctx = _get_session(request)
    fpath = _safe_path(ctx.reports_dir, filename)
    if not fpath.exists():
        raise HTTPException(status_code=404, detail="Report not found")
    with open(fpath, "r", encoding="utf-8") as f:
        data = json.load(f)
    report = data.get("final_report", "")
    if not isinstance(report, str):
        return {"ok": False, "error": "当前报告格式不支持刷新竞品表", "report": data}

    try:
        updated_report, result = _refresh_competitor_table_markdown(report)
    except Exception as e:
        print(f"[ReportCompetitorRefresh] failed: {e}")
        return {"ok": False, "error": "st cli不可用", "report": data}

    if not result.get("ok"):
        return {"ok": False, "error": result.get("error") or "重新查询失败", "report": data}

    data["final_report"] = updated_report
    data["competitor_refreshed_at"] = datetime.now().isoformat()
    data["competitor_refresh_result"] = result
    _safe_json_write(fpath, data, indent=2)
    cache_key = str(ctx.reports_dir.resolve())
    _report_list_cache.pop(cache_key, None)
    return {"ok": True, "report": data, **result}


@router.delete("/reports/{filename}")
def delete_report(filename: str, request: Request):
    ctx = _get_session(request)
    fpath = _safe_path(ctx.reports_dir, filename)
    if not fpath.exists():
        raise HTTPException(status_code=404, detail="Report not found")
    fpath.unlink()
    cache_key = str(ctx.reports_dir.resolve())
    _report_list_cache.pop(cache_key, None)
    return {"ok": True}


@router.post("/reports/{filename}/export-feishu")
def export_to_feishu(filename: str, request: Request):
    """将报告导出为飞书在线文档。"""
    from .feishu_client import is_feishu_configured, create_feishu_doc

    ctx = _get_session(request)
    app_id, app_secret = ctx.get_feishu_credentials()
    if not is_feishu_configured(app_id, app_secret):
        raise HTTPException(status_code=400, detail="飞书未配置：请在设置中填写 App ID 和 App Secret")

    fpath = _safe_path(ctx.reports_dir, filename)
    if not fpath.exists():
        raise HTTPException(status_code=404, detail="Report not found")

    with open(fpath, "r", encoding="utf-8") as f:
        data = json.load(f)

    language = _normalize_report_language(data.get("language") or REPORT_LANGUAGE_ZH)
    need_obj = data.get("need", {}) if isinstance(data.get("need"), dict) else {}
    title = _report_need_title(need_obj, language) if need_obj else data.get("post", {}).get("title", "需求分析报告")
    report = data.get("final_report", "")
    if not isinstance(report, str):
        report = json.dumps(report, ensure_ascii=False, indent=2)

    try:
        result = create_feishu_doc(title, report, app_id=app_id, app_secret=app_secret)
        # 持久化飞书发布信息到报告 JSON
        data["feishu"] = {"url": result["url"], "document_id": result["document_id"]}
        _safe_json_write(fpath, data, indent=2)
        return {"ok": True, "url": result["url"], "document_id": result["document_id"]}
    except Exception as exc:
        print(f"[FeishuExport] failed: {type(exc).__name__}")
        raise HTTPException(status_code=502, detail="飞书导出失败，请检查本地飞书配置与应用权限") from exc


@router.get("/config/feishu-status")
def feishu_status(request: Request):
    """返回飞书是否已配置（不暴露密钥）。"""
    from .feishu_client import is_feishu_configured
    ctx = _get_session(request)
    app_id, app_secret = ctx.get_feishu_credentials()
    return {"configured": is_feishu_configured(app_id, app_secret)}


@router.get("/config/st-status")
def sensortower_status():
    """返回 SensorTower (st-cli) 是否已安装且已认证。"""
    status = st_check_available()
    return {
        "installed": status.get("installed", False),
        "available": status.get("available", False),
        "api_ok": status.get("api_ok", False),
        "error": status.get("error", ""),
    }



# ============================================================
# Phase 2: Deep Dive (product proposal → web research → analysis)
# ============================================================

@router.post("/debate/proposal")
def generate_proposal(request: Request):
    """Generate a product proposal from Phase 1 discussion."""
    ctx = _get_session(request)
    needs_data = get_needs(request)["needs"]
    idx = ctx.debate_state.get("selected_need_idx")
    proposal_language = _normalize_ui_language(ctx.debate_state.get("language", UI_LANGUAGE_ZH))
    if idx is not None and 0 <= idx < len(needs_data):
        need = _need_for_language(needs_data[idx], proposal_language)
    elif ctx.debate_state.get("free_topic_input"):
        need = {"need_title": ctx.debate_state["free_topic_input"], "need_description": "", "posts": []}
    else:
        raise HTTPException(status_code=400, detail="No need selected")
    debate_log = ctx.debate_state["debate_log"]

    def event_stream() -> Generator[str, None, None]:
        set_thread_session(ctx)
        try:
            ctx.debate_state["status"] = "generating_proposal"
            yield _sse("proposal_start", {})

            proposal = generate_product_proposal(need, debate_log, language=proposal_language)
            ctx.debate_state["product_proposal"] = proposal
            ctx.debate_state["status"] = "proposal_done"
            ctx.save_debate_cache()

            yield _sse("proposal_end", {"proposal": proposal})

        except Exception as e:
            _log_sse_error("DebateReport", e, ctx)
            ctx.debate_state["status"] = "debate_done"
            ctx.save_debate_cache()
            yield _sse("error", {"message": _friendly_error(e)})

    return StreamingResponse(event_stream(), media_type="text/event-stream")


@router.post("/deep-dive/start")
def start_deep_dive(request: Request):
    """Phase 2: web search + deep dive analysis (SSE stream)."""
    ctx = _get_session(request)
    proposal = ctx.debate_state.get("product_proposal")
    if not proposal:
        raise HTTPException(status_code=400, detail="No product proposal yet")

    def event_stream() -> Generator[str, None, None]:
        set_thread_session(ctx)
        try:
            from .web_search import run_deep_dive_searches, format_search_results_for_llm

            ctx.debate_state["status"] = "deep_diving"

            yield _sse("message_start", {"role": "researcher", "label": "调研员", "provider": _provider_for_role("analyst")})
            yield _sse("chunk", {"text": "收到产品方案了，我来做一轮深度调研。"})
            yield _sse("message_end", {"role": "researcher", "content": "收到产品方案了，我来做一轮深度调研。"})
            ctx.debate_state["debate_log"].append({"role": "researcher", "content": "收到产品方案了，我来做一轮深度调研。"})

            all_results: list[tuple[str, list[dict]]] = []

            def on_progress(msg: str):
                pass  # progress is sent via search_progress events

            for query, results in run_deep_dive_searches(proposal):
                all_results.append((query, results))
                result_count = sum(len(r) for _, r in all_results)
                yield _sse("search_progress", {
                    "query": query,
                    "result_count": len(results),
                    "total_results": result_count,
                    "total_queries": len(all_results),
                })

            search_text = format_search_results_for_llm(all_results)
            ctx.debate_state["search_results"] = search_text

            yield _sse("message_start", {"role": "researcher", "label": "调研员", "provider": _provider_for_role("analyst")})

            dive_msgs = prepare_deep_dive_messages(proposal, search_text)
            parts: list[str] = []
            for chunk in call_for_role_stream("analyst", dive_msgs):
                parts.append(chunk)
                yield _sse("chunk", {"text": chunk})
            analysis = "".join(parts)

            ctx.debate_state["deep_dive_analysis"] = analysis
            ctx.debate_state["debate_log"].append({"role": "researcher", "content": analysis})
            yield _sse("message_end", {"role": "researcher", "content": analysis})

            ctx.debate_state["status"] = "deep_dive_done"
            ctx.save_debate_cache()
            yield _sse("deep_dive_end", {})

        except Exception as e:
            import traceback
            traceback.print_exc()
            ctx.debate_state["status"] = "proposal_done"
            ctx.save_debate_cache()
            yield _sse("error", {"message": _friendly_error(e)})

    return StreamingResponse(event_stream(), media_type="text/event-stream")


# ============================================================
# Engine status & Deep Mine
# ============================================================

@router.get("/engine-status")
def engine_status(request: Request, force: bool = False):
    """Return current Reddit engine status for the frontend."""
    ctx = _get_session(request)
    import asyncio as _aio
    loop = _aio.new_event_loop()
    try:
        fetcher = get_reddit_fetcher()
        async def _init():
            rdt_status = await fetcher.rdt.check_available(force=force)
            if rdt_status["installed"] and rdt_status["authenticated"]:
                fetcher._active_engine = "rdt-cli"
                return {"engine": "rdt-cli", "rdt_status": rdt_status}
            fetcher._active_engine = "none"
            return {"engine": "none", "rdt_status": rdt_status}
        info = loop.run_until_complete(_init())
        info["preference"] = ctx.engine_preference
        return info
    except Exception as e:
        print(f"[engine-status] failed: {e}")
        return {"engine": "error", "error": "Reddit 引擎状态检测失败，请检查 rdt-cli 认证后重试", "preference": ctx.engine_preference}
    finally:
        loop.close()


@router.get("/engine-preference")
def get_engine_preference(request: Request):
    ctx = _get_session(request)
    return {"preference": ctx.engine_preference}


@router.post("/engine-preference")
def set_engine_preference(body: dict, request: Request):
    ctx = _get_session(request)
    pref = body.get("preference", "auto")
    if pref not in ("auto", "rdt-cli"):
        raise HTTPException(status_code=400, detail="无效的引擎偏好")
    ctx.save_engine_preference(pref)
    return {"ok": True, "preference": pref}


@router.get("/web-search-engine")
def get_web_search_engine(request: Request):
    ctx = _get_session(request)
    return {"engine": ctx.web_search_engine}


@router.post("/web-search-engine")
def set_web_search_engine(body: dict, request: Request):
    ctx = _get_session(request)
    engine = body.get("engine", "tavily")
    if engine not in ("tavily", "claude", "gpt"):
        raise HTTPException(status_code=400, detail="无效的搜索引擎")
    ctx.save_web_search_engine(engine)
    return {"ok": True, "engine": engine}


@router.post("/web-search-test")
def test_web_search(body: dict, request: Request):
    """测试当前 WebSearch 引擎是否可用。"""
    ctx = _get_session(request)
    engine = body.get("engine", ctx.web_search_engine)
    if engine == "tavily":
        try:
            from .web_search import _get_tavily_client
            client = _get_tavily_client()
            r = client.search(query="test", search_depth="basic", max_results=1, include_answer=False)
            if r and r.get("results") is not None:
                return {"ok": True, "message": "Tavily API 连接正常"}
            return {"ok": False, "message": "Tavily API 返回异常"}
        except ValueError:
            return {"ok": False, "message": "Tavily 未配置，请填写自己的 API Key"}
        except Exception:
            return {"ok": False, "message": "Tavily 连接失败，请检查本机网络和 API Key"}
    elif engine == "gpt":
        from openai import OpenAI
        cfg = get_provider_config("GPT")
        base_url, api_key, model = cfg["base_url"], cfg["api_key"], cfg["model"]
        if not base_url or not api_key:
            return {"ok": False, "message": "GPT 未配置（缺少 API Key 或 Base URL）"}
        try:
            client = OpenAI(base_url=base_url, api_key=api_key, timeout=45.0, max_retries=0)
            from .web_search import _probe_web_search_support
            probe = _probe_web_search_support(client, model, "GPT", attempts=2)
            if probe.ok:
                return {
                    "ok": True, "status": probe.status, "retryable": False,
                    "message": f"GPT WebSearch 可用（{model}）",
                }
            return {
                "ok": False, "status": probe.status, "retryable": probe.retryable,
                "message": _web_search_probe_message("GPT", probe.status, model),
            }
        except Exception:
            return {"ok": False, "message": "GPT 连接失败，请检查本地模型配置和网络"}
    elif engine == "claude":
        from openai import OpenAI
        cfg = get_provider_config("CLAUDE")
        base_url, api_key, model = cfg["base_url"], cfg["api_key"], cfg["model"]
        if not base_url or not api_key:
            return {"ok": False, "message": "Claude 未配置（缺少 API Key 或 Base URL）"}
        try:
            client = OpenAI(base_url=base_url, api_key=api_key, timeout=45.0, max_retries=0)
            from .web_search import _probe_web_search_support
            probe = _probe_web_search_support(client, model, "Claude", attempts=2)
            if probe.ok:
                return {
                    "ok": True, "status": probe.status, "retryable": False,
                    "message": f"Claude WebSearch 可用（{model}）",
                }
            return {
                "ok": False, "status": probe.status, "retryable": probe.retryable,
                "message": _web_search_probe_message("Claude", probe.status, model),
            }
        except Exception:
            return {"ok": False, "message": "Claude 连接失败，请检查本地模型配置和网络"}
    return {"ok": False, "message": "未知引擎"}



@router.post("/deep-mine")
def deep_mine(req: StartDebateRequest, request: Request):
    """Phase B: Deep mining for a specific need — quote extraction + FEMWC scoring."""
    ctx = _get_session(request)
    import asyncio as _aio
    _dm_loop = _aio.new_event_loop()
    _aio.set_event_loop(_dm_loop)

    needs_data = get_needs(request)["needs"]
    if req.need_index < 0 or req.need_index >= len(needs_data):
        raise HTTPException(status_code=404, detail="Need not found")

    deep_mine_language = _normalize_ui_language(req.language)
    need = _need_for_language(needs_data[req.need_index], deep_mine_language)

    def event_stream() -> Generator[str, None, None]:
        set_thread_session(ctx)
        try:
            yield _sse("fetch_progress", {"message": "开始深挖需求...", "progress": 5})

            # Step 1: Generate supplementary queries
            posts_summary = "\n".join(
                f"- {p['title']} (score={p.get('score', 0)})"
                for p in need.get("posts", [])[:5]
            )
            prompt = DEEP_MINING_QUERY_PROMPT.format(
                need_title=need.get("need_title", ""),
                need_description=need.get("need_description", ""),
                posts_summary=posts_summary,
            )
            try:
                resp = call_llm([{"role": "user", "content": prompt}])
                plan = _parse_json_from_text(resp)
                extra_queries = plan.get("search_queries", []) if plan else []
                extra_subs = plan.get("subreddits", []) if plan else []
                yield _sse("fetch_progress", {
                    "message": f"生成 {len(extra_queries)} 条补充搜索词",
                    "progress": 15,
                })
            except Exception as e:
                print(f"[DeepMine] Query gen failed: {e}")
                extra_queries = []
                extra_subs = []

            # Step 2: Deep fetch with rdt read for full comments
            fetcher = get_reddit_fetcher()
            all_deep_posts: list[dict] = []

            existing_posts = need.get("posts", [])
            for p in existing_posts:
                post_id = p.get("_post_id", "")
                if post_id and fetcher.engine_name == "rdt-cli":
                    full = _dm_loop.run_until_complete(fetcher.read_post(post_id))
                    if full:
                        all_deep_posts.append(full)
                        continue
                all_deep_posts.append(p)

            yield _sse("fetch_progress", {
                "message": f"已读取 {len(all_deep_posts)} 个帖子的完整评论",
                "progress": 35,
            })

            if extra_queries:
                for i, q in enumerate(extra_queries[:8]):
                    sub = extra_subs[i % len(extra_subs)] if extra_subs else ""
                    new_posts = _dm_loop.run_until_complete(fetcher.search(q, subreddit=sub, limit=5))
                    for np in new_posts:
                        if not any(np["title"].lower() == ep["title"].lower() for ep in all_deep_posts):
                            all_deep_posts.append(np)
                    yield _sse("fetch_progress", {
                        "message": f"补充搜索 {i+1}/{min(len(extra_queries), 8)}: +{len(new_posts)} 帖子",
                        "progress": 35 + int(20 * (i + 1) / min(len(extra_queries), 8)),
                    })

            # Apply hard filter
            all_deep_posts = [p for p in all_deep_posts if hard_filter(p)] or all_deep_posts

            yield _sse("fetch_progress", {
                "message": f"共 {len(all_deep_posts)} 个帖子，开始提取原文摘录...",
                "progress": 60,
            })

            # Step 3: Quote extraction
            quotes = extract_quotes(all_deep_posts)
            yield _sse("fetch_progress", {
                "message": f"提取到 {len(quotes)} 条原文摘录",
                "progress": 75,
            })

            # Step 4: FEMWC scoring
            yield _sse("fetch_progress", {"message": "FEMWC 五维评分中...", "progress": 82})

            updated_need = dict(need)
            updated_need["posts"] = all_deep_posts
            femwc = score_femwc(updated_need, quotes)

            yield _sse("fetch_progress", {
                "message": f"评分完成：{femwc.get('total', 0):.2f} 分 — {femwc.get('verdict', '')}",
                "progress": 92,
            })

            # Step 5: Build need package
            package = build_need_package(updated_need, quotes, femwc)

            # Save to cache
            updated_need["deep_mine_package"] = package
            needs_data[req.need_index] = updated_need
            _safe_json_write(ctx.needs_cache, needs_data, indent=2)

            yield _sse("fetch_progress", {"message": "深挖完成！", "progress": 100})
            yield _sse("deep_mine_result", {
                "package": package,
                "need_index": req.need_index,
            })
            yield _sse("done", {})

        except Exception as e:
            import traceback
            traceback.print_exc()
            yield _sse("error", {"message": _friendly_error(e)})

    return StreamingResponse(event_stream(), media_type="text/event-stream")



# ============================================================
# POC 产品验证
# ============================================================

class PocEvalRequest(BaseModel):
    idea_name: str = Field(max_length=200)
    idea_brief: str = Field(max_length=12000)
    target_users: str = Field(max_length=4000)
    pain_points: str = Field(max_length=12000)
    simple_product: str = Field(max_length=12000)
    report_filename: str = Field(default="", max_length=255)
    opportunity_index: int = Field(default=-1, ge=-1, le=1000)


@router.post("/poc-evaluate")
def poc_evaluate(req: PocEvalRequest, request: Request):
    """执行 POC 产品验证，只评估当前证据和下一步实验，不依赖报告。"""
    ctx = _get_session(request)
    prompt = (POC_EVAL_PROMPT
        .replace("__IDEA_NAME__", req.idea_name)
        .replace("__IDEA_BRIEF__", req.idea_brief)
        .replace("__TARGET_USERS__", req.target_users)
        .replace("__PAIN_POINTS__", req.pain_points)
        .replace("__SIMPLE_PRODUCT__", req.simple_product)
    )

    messages = [
        {"role": "system", "content": "你是产品验证分析助手。仅评估当前证据和下一步验证动作，不作立项或投资决策。直接输出 JSON。"},
        {"role": "user", "content": prompt},
    ]

    try:
        result_text = call_llm(messages, max_tokens=1500)
    except Exception as exc:
        print(f"[PocEvaluation] model call failed: {type(exc).__name__}")
        raise HTTPException(status_code=502, detail=_friendly_error(exc)) from exc

    parsed = _parse_json_from_text(result_text)
    if not parsed:
        raise HTTPException(status_code=500, detail="AI 返回格式异常，无法解析")

    eval_id = f"poc_{uuid.uuid4().hex}"
    result = {
        "id": eval_id,
        "timestamp": datetime.now().isoformat(),
        "input": {
            "idea_name": req.idea_name,
            "idea_brief": req.idea_brief,
            "target_users": req.target_users,
            "pain_points": req.pain_points,
            "simple_product": req.simple_product,
        },
        "evaluation": parsed,
    }

    poc_dir = ctx.data_dir / "poc_evaluations"
    poc_dir.mkdir(parents=True, exist_ok=True)
    filepath = poc_dir / f"{eval_id}.json"
    _safe_json_write(filepath, result, indent=2)

    # 持久化 eval_id 到报告的 opportunities 缓存
    if req.report_filename and req.opportunity_index >= 0:
        rp = _safe_path(ctx.reports_dir, req.report_filename)
        if rp.exists():
            try:
                rd = json.loads(rp.read_text(encoding="utf-8"))
                opps = rd.get("opportunities", [])
                if 0 <= req.opportunity_index < len(opps):
                    opps[req.opportunity_index]["eval_id"] = eval_id
                    rd["opportunities"] = opps
                    _safe_json_write(rp, rd, indent=2)
            except Exception:
                pass

    return result


@router.get("/poc-evaluate/{eval_id}")
def get_poc_evaluation(eval_id: str, request: Request):
    """根据 eval_id 读取历史验证结果。"""
    ctx = _get_session(request)
    filepath = _safe_path(ctx.data_dir / "poc_evaluations", f"{eval_id}.json")
    if not filepath.exists():
        raise HTTPException(status_code=404, detail="验证记录不存在")
    try:
        return json.loads(filepath.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"[PocEvaluation] read failed: {type(exc).__name__}")
        raise HTTPException(status_code=500, detail="读取验证记录失败") from exc


@router.post("/poc-evaluate/extract-opportunities")
def extract_opportunities(body: dict, request: Request):
    """从报告中确定性解析机会点，首次提取后缓存到报告文件"""
    ctx = _get_session(request)
    report_content = body.get("report_content", "")
    report_filename = body.get("report_filename", "")
    need_desc = body.get("need_description", "")

    # 尝试从缓存读取
    if report_filename:
        rp = _safe_path(ctx.reports_dir, report_filename)
        if rp.exists():
            try:
                rd = json.loads(rp.read_text(encoding="utf-8"))
                cached = rd.get("opportunities")
                if cached and isinstance(cached, list) and len(cached) > 0:
                    # 检查缓存质量：三个维度字段都应有内容
                    has_good_cache = all(
                        len(o.get("simple_product", "")) > 10
                        and len(o.get("target_users", "")) > 5
                        and len(o.get("pain_points", "")) > 5
                        for o in cached
                    )
                    if has_good_cache:
                        return {"opportunities": cached}
                # 如果没缓存，从文件中也取 need_description
                if not need_desc:
                    need_obj = rd.get("need", {})
                    if isinstance(need_obj, dict):
                        need_desc = need_obj.get("need_description", "")
            except Exception:
                pass

    if not report_content:
        return {"opportunities": []}

    opportunities = _parse_opportunities(report_content, need_desc)

    # 缓存到报告文件（保留旧缓存中的 eval_id）
    if report_filename and opportunities:
        rp = _safe_path(ctx.reports_dir, report_filename)
        if rp.exists():
            try:
                rd = json.loads(rp.read_text(encoding="utf-8"))
                old_opps = rd.get("opportunities", [])
                old_eval_ids = {}
                for i, o in enumerate(old_opps):
                    eid = o.get("eval_id")
                    if eid:
                        old_eval_ids[o.get("title", "")] = eid
                for opp in opportunities:
                    eid = old_eval_ids.get(opp.get("title", ""))
                    if eid:
                        opp["eval_id"] = eid
                rd["opportunities"] = opportunities
                _safe_json_write(rp, rd, indent=2)
            except Exception:
                pass

    return {"opportunities": opportunities}


def _parse_opportunities(report_content, need_desc: str = "") -> list[dict]:
    """从报告产品方案章节确定性解析 POC 验证候选，兼容中英文报告模板。"""
    import re

    opportunities = []

    if not isinstance(report_content, str):
        return opportunities

    def _clean_lines(text: str) -> str:
        return re.sub(r"\n{3,}", "\n\n", str(text or "").strip())

    def _field(body: str, *names: str) -> str:
        """尝试多个字段名匹配，支持 **字段**：内容 和 **字段：**内容 两种格式"""
        for name in names:
            m = re.search(rf'\*\*{re.escape(name)}[：:]*\*\*[：:\s]*(.+?)(?:\n|$)', body, re.I)
            if m:
                return _clean_lines(m.group(1))
        return ""

    def _bold_section(body: str, heading: str) -> str:
        """提取 **heading** 下的全部要点（支持 #### 和 **bold** 两种子标题格式）"""
        m = re.search(rf'####\s*{re.escape(heading)}\s*\n([\s\S]*?)(?=####|\Z)', body, re.I)
        if not m:
            m = re.search(rf'\*\*{re.escape(heading)}[：:]*\*\*\s*\n([\s\S]*?)(?=\*\*[^*]+\*\*\s*\n|\Z)', body, re.I)
        if not m:
            return ""
        items = re.findall(r'^[-*]\s+\*\*[^*]+[：:]*\*\*[：:\s]*(.+)', m.group(1), re.M)
        if items:
            return _clean_lines('；'.join(items))
        plain_items = re.findall(r'^[-*]\s+(.+)', m.group(1), re.M)
        return _clean_lines('；'.join(plain_items)) if plain_items else ""

    # 从中文「产品方案」章节提取
    section = re.search(r'## 产品方案\s*\n+([\s\S]*?)(?=\n## |$)', report_content, re.I)
    if section:
        blocks = re.findall(
            r'### 方案\s*\d+[\s：:]+(.+?)(?:\n)([\s\S]*?)(?=### 方案\s*\d+|$)',
            section.group(1),
            re.I,
        )
        for title, body in blocks:
            target_users = _field(body, '目标人群', '目标用户')
            pain_points = _field(body, '核心痛点', '具体痛点', '用户痛点', '解决的核心问题')
            product_desc = _field(body, '一句话描述', '产品描述')
            product_form = _field(body, '产品形态')
            mvp_scope = _field(body, 'MVP 范围', 'MVP范围')
            features = [f for f in re.findall(r'^[-*]\s+(.+)', body, re.M) if not f.startswith('**')][:5]

            # 提取核心流程（支持缩进的编号列表，冒号可能在 ** 内部或外部）
            flow_m = re.search(r'\*\*核心流程[：:]*\*\*[：:\s]*\n((?:\s+\d+\..+\n?)+)', body)
            flow_steps = []
            if flow_m:
                flow_steps = [s.strip().rstrip('  ') for s in re.findall(r'\d+\.\s*(.+)', flow_m.group(1))]

            if not target_users:
                target_users = _bold_section(body, '清晰的用户')
            if not pain_points:
                pain_points = _bold_section(body, '真实的需求')

            # simple_product 始终拼接完整信息：产品形态 + 核心流程 + MVP
            sp_parts = []
            if product_form:
                sp_parts.append(product_form)
            if flow_steps:
                sp_parts.append("核心流程：" + "→".join(flow_steps))
            if mvp_scope:
                sp_parts.append("MVP：" + mvp_scope)
            if sp_parts:
                product_desc = "。".join(sp_parts) + "。"
            elif not product_desc:
                product_desc = _bold_section(body, '简单的产品')
            if not product_desc and features:
                product_desc = "核心功能包括：" + "；".join(features) + "。"

            idea_brief = pain_points
            if product_form and idea_brief:
                idea_brief = f"通过{product_form}，{idea_brief}"

            opportunities.append({
                "title": title.strip().rstrip('。'),
                "description": idea_brief or product_desc,
                "target_users": target_users,
                "pain_points": pain_points,
                "features": features,
                "simple_product": product_desc,
            })

    # 从英文「Product Concepts」章节提取
    if not opportunities:
        section = re.search(r'## Product Concepts\s*\n+([\s\S]*?)(?=\n## |$)', report_content, re.I)
        if section:
            blocks = re.findall(
                r'### Concept\s*\d+[\s：:]+(.+?)(?:\n)([\s\S]*?)(?=### Concept\s*\d+|$)',
                section.group(1),
                re.I,
            )
            for title, body in blocks:
                target_users = _field(body, 'Target users', 'Clear users')
                acquisition = _field(body, 'Acquisition channels')
                pain_points = _field(body, 'Core pain', 'Real demand')
                evidence = _field(body, 'Evidence')
                product_function = _field(body, 'Product function', 'Product description')
                product_form = _field(body, 'Product form')
                mvp_scope = _field(body, 'MVP scope', 'MVP')

                flow_m = re.search(r'\*\*Core flow[：:]*\*\*[：:\s]*\n((?:\s+\d+\..+\n?)+)', body, re.I)
                flow_steps = []
                if flow_m:
                    flow_steps = [s.strip().rstrip('  ') for s in re.findall(r'\d+\.\s*(.+)', flow_m.group(1))]

                if not target_users:
                    target_users = _bold_section(body, 'Clear users')
                if acquisition:
                    target_users = _clean_lines(f"{target_users}\n\nAcquisition channels: {acquisition}") if target_users else acquisition
                if not pain_points:
                    pain_points = _bold_section(body, 'Real demand')
                if evidence:
                    pain_points = _clean_lines(f"{pain_points}\n\nEvidence: {evidence}") if pain_points else evidence

                sp_parts = []
                if product_form:
                    sp_parts.append(product_form)
                if product_function:
                    sp_parts.append(product_function)
                if flow_steps:
                    sp_parts.append("Core flow: " + " -> ".join(flow_steps))
                if mvp_scope:
                    sp_parts.append("MVP: " + mvp_scope)
                product_desc = _clean_lines("\n".join(sp_parts)) or _bold_section(body, 'Simple product')

                features = flow_steps[:5]
                opportunities.append({
                    "title": title.strip().rstrip('.'),
                    "description": pain_points or product_desc,
                    "target_users": target_users,
                    "pain_points": pain_points,
                    "features": features,
                    "simple_product": product_desc,
                })

    # 兜底：从痛点地图的机会点提取
    if not opportunities:
        pain_section = re.search(r'## 痛点地图[\s\S]*?(?=\n## )', report_content)
        if pain_section:
            pain_blocks = re.findall(
                r'### \d+\.\s+(.+?)(?:\n)([\s\S]*?)(?=### \d+\.|## |$)',
                pain_section.group(0)
            )
            for title, body in pain_blocks[:3]:
                opp_match = re.search(r'\*\*机会点\*\*\s*\n([\s\S]*?)(?=\n\*\*|\n### |\n## |$)', body)
                features = re.findall(r'^[-*]\s+(.+)', opp_match.group(1), re.M) if opp_match else []
                pain_desc_m = re.search(r'\*\*强度.+?\n\n(.+?)(?:\n\n|\n\*\*)', body, re.S)
                pain_desc = pain_desc_m.group(1).strip() if pain_desc_m else title.strip()
                fallback_sp = ("核心功能：" + "；".join(features[:3]) + "。") if features else ""
                opportunities.append({
                    "title": title.strip().split('—')[0].strip().split(' — ')[0].strip(),
                    "description": "; ".join(features[:2]) if features else title.strip(),
                    "target_users": "",
                    "pain_points": pain_desc,
                    "features": features[:3],
                    "simple_product": fallback_sp,
                })

    return opportunities[:3]


# ============================================================
# 快速搜索（Quick Search）— 轻量级 Reddit 搜索 + AI 总结
# ============================================================

class QuickSearchRequest(BaseModel):
    query: str
    language: str = UI_LANGUAGE_ZH
    time_period: str = "3months"
    min_score: int = 10
    limit: int = 50
    fetch_comments: bool = True
    market_search: bool = True
    market_time_period: str = "30d"
    strategy: str = "auto"


class QuickSearchHistorySaveRequest(BaseModel):
    items: list[dict[str, Any]] = Field(default_factory=list)


class QuickSearchReviewTranslateRequest(BaseModel):
    reviews: list[dict[str, Any]] = Field(default_factory=list)


def _qs_history_file(ctx: SessionContext) -> Path:
    return ctx.data_dir / "quick_search_history.json"


def _qs_sanitize_history_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """限制快速搜索历史体积，避免单条历史把 session 文件撑得过大。"""
    cleaned: list[dict[str, Any]] = []
    for raw in items[:12]:
        if not isinstance(raw, dict):
            continue
        query = str(raw.get("query") or "").strip()
        if not query:
            continue
        try:
            timestamp = int(raw.get("timestamp") or 0)
        except Exception:
            timestamp = 0
        posts = raw.get("posts") if isinstance(raw.get("posts"), list) else []
        safe_posts = []
        for post in posts[:30]:
            if not isinstance(post, dict):
                continue
            comments = post.get("comments") if isinstance(post.get("comments"), list) else []
            safe_posts.append({
                "title": str(post.get("title") or "")[:500],
                "title_zh": str(post.get("title_zh") or "")[:500],
                "content": str(post.get("content") or "")[:700],
                "content_zh": str(post.get("content_zh") or "")[:700],
                "url": str(post.get("url") or "")[:1000],
                "score": post.get("score") or 0,
                "num_comments": post.get("num_comments") or 0,
                "source": str(post.get("source") or "")[:120],
                "created_utc": post.get("created_utc") or 0,
                "process_dimensions": [str(item)[:40] for item in (post.get("process_dimensions") or [])[:4]],
                "process_actions": [str(item)[:40] for item in (post.get("process_actions") or [])[:8]],
                "process_scopes": [str(item)[:40] for item in (post.get("process_scopes") or [])[:4]],
                "comments": [
                    {
                        "body": str(c.get("body") or "")[:700] if isinstance(c, dict) else "",
                        "body_zh": str(c.get("body_zh") or "")[:700] if isinstance(c, dict) else "",
                        "score": c.get("score") or 0 if isinstance(c, dict) else 0,
                    }
                    for c in comments[:5]
                    if isinstance(c, dict)
                ],
            })
        market_signal = raw.get("marketSignal")
        cleaned.append({
            "id": str(raw.get("id") or f"{timestamp}-{len(cleaned)}")[:80],
            "query": query[:260],
            "timestamp": timestamp,
            "summary": str(raw.get("summary") or "")[:20000],
            "posts": safe_posts,
            "marketSignal": market_signal if isinstance(market_signal, dict) else None,
        })
    return cleaned


def _qs_read_history(ctx: SessionContext) -> list[dict[str, Any]]:
    path = _qs_history_file(ctx)
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []
    items = data.get("items") if isinstance(data, dict) else data
    if not isinstance(items, list):
        return []
    return _qs_sanitize_history_items(items)


def _qs_flatten_plan_queries(plan: dict, fallback: str) -> list[str]:
    """从规划 JSON 合并多类搜索词，兼容旧版 search_queries。"""
    queries: list[str] = []
    research_type = str(plan.get("research_type") or "").strip().lower()
    if research_type == "process_workflow":
        # 优先执行阶段/任务的不同写法，让前 4 个定向查询覆盖核心流程与不同路径。
        dimension_values: list[list[str]] = []
        for key in ("stage_queries", "task_queries"):
            val = plan.get(key, [])
            if isinstance(val, str):
                val = [val]
            dimension_values.append([str(q).strip() for q in val[:2] if str(q).strip()] if isinstance(val, list) else [])
        for round_index in range(2):
            for values in dimension_values:
                if round_index < len(values):
                    queries.append(values[round_index])
        for key in ("role_queries", "tool_queries", "problem_queries"):
            val = plan.get(key, [])
            if isinstance(val, str):
                val = [val]
            if isinstance(val, list):
                queries.extend(str(q).strip() for q in val[:2] if str(q).strip())
    else:
        for key, cap in (("problem_queries", 6), ("solution_queries", 4), ("discovery_queries", 4)):
            val = plan.get(key, [])
            if isinstance(val, str):
                val = [val]
            if isinstance(val, list):
                for q in val[:cap]:
                    s = str(q).strip()
                    if s:
                        queries.append(s)
    if not queries:
        legacy = plan.get("search_queries", [fallback])
        if isinstance(legacy, str):
            legacy = [legacy]
        queries = [str(q).strip() for q in legacy if str(q).strip()]
    if not queries:
        queries = [fallback]
    seen: set[str] = set()
    out: list[str] = []
    for q in queries:
        key = q.lower()
        if key not in seen:
            seen.add(key)
            out.append(q)
    return out[:12]


_QS_PROCESS_QUERY_RE = re.compile(
    r"流程|步骤|阶段|先后|顺序|工作流|怎么做|如何完成|具体工作|参与角色|谁负责|时间线|截止|准备工作|"
    r"workflow|process|steps?|stages?|sequence|timeline|checklist|roles?|tasks?|what does .* do|how .* work",
    re.I,
)


def _qs_is_process_research_query(query: str) -> bool:
    """识别明确要求流程/工作拆解的问题；只作保守纠正，不改变普通痛点问题。"""
    return bool(_QS_PROCESS_QUERY_RE.search(str(query or "")))


def _qs_research_type(query: str, gate: dict[str, Any] | None = None) -> str:
    value = str((gate or {}).get("research_type") or "").strip().lower()
    valid = {"process_workflow", "pain_points", "recommendations", "comparison", "market_metrics", "app_reviews", "trend", "general"}
    if value not in valid:
        value = "general"
    if _qs_is_process_research_query(query):
        value = "process_workflow"
    return value


def _qs_normalize_process_queries(plan: dict[str, Any], research_type: str) -> None:
    """只为流程查询补齐目标对象锚点，避免把 timeline/workflow 搜成泛结果。"""
    if research_type != "process_workflow":
        return
    anchors = plan.get("search_anchors") or []
    if isinstance(anchors, str):
        anchors = [anchors]
    anchors = [re.sub(r"\s+", " ", str(item or "")).strip() for item in anchors if str(item or "").strip()]
    if not anchors:
        anchor = str(plan.get("topic_anchor") or "").strip()
        anchors = [anchor] if anchor else []
    if not anchors:
        return
    plan["research_type"] = research_type
    plan["search_anchors"] = anchors[:4]
    for key in ("stage_queries", "task_queries", "role_queries", "tool_queries"):
        values = plan.get(key) or []
        if isinstance(values, str):
            values = [values]
        if not isinstance(values, list):
            values = []
        normalized: list[str] = []
        for raw in values[:4]:
            query = re.sub(r"\s+", " ", str(raw or "")).strip()
            if not query:
                continue
            query_tokens = set(re.findall(r"[a-z][a-z0-9-]{2,}", query.lower()))
            anchor_overlaps = [
                len(query_tokens & set(re.findall(r"[a-z][a-z0-9-]{2,}", anchor.lower())))
                for anchor in anchors
            ]
            if not anchor_overlaps or max(anchor_overlaps) == 0:
                query = f"{anchors[0]} {query}"
            normalized.append(query[:100])
        plan[key] = _qs_dedupe_texts(normalized, limit=2)


_QS_PROCESS_ACTION_PATTERNS: tuple[tuple[str, re.Pattern], ...] = (
    ("brainstorm", re.compile(r"brainstorm|choose (?:a )?topic|topic ideas?|story ideas?|personal inventory|reflect(?:ion|ing)?|选题|素材整理|头脑风暴", re.I)),
    ("draft", re.compile(r"outline|first draft|rough draft|draft(?:ing|ed)?|write (?:the|my|your|an?) essay|开始写|初稿|列提纲", re.I)),
    ("feedback", re.compile(r"feedback|review(?:ing|ed)?|critique|proofread|counselor|teacher|parent|mentor|顾问|老师|家长|反馈|审阅|校对", re.I)),
    ("revise", re.compile(r"revis(?:e|ing|ion)|rewrite|edit(?:ing|ed)?|structure|clarity|polish|修改|重写|润色|结构调整", re.I)),
    ("submit", re.compile(r"submit|submission|upload|deadline|finaliz|turn in|提交|截止|定稿|上传", re.I)),
    ("manage", re.compile(r"track|calendar|spreadsheet|version|google docs?|organ(?:ize|ise|izing)|checklist|管理|日历|表格|版本", re.I)),
)
_QS_PROCESS_SEQUENCE_RE = re.compile(
    r"\b(?:first|then|next|after|before|finally|early|timeline|process|steps?|junior|senior|summer|june|july|august|fall|winter)\b|"
    r"首先|然后|接着|之后|之前|最后|时间线|流程|步骤|高二|高三|暑假|春季|秋季",
    re.I,
)
_QS_PROCESS_WEAK_POST_RE = re.compile(
    r"horrible (?:college )?essay|system is broken|stupid(?:est)? thing|am i the only|brutally critique|"
    r"rate my essay|free feedback|giving feedback|can anyone help|need help|affordable resource|"
    r"writer.?s block|staring at (?:a )?blank|essay idea|很糟糕的.*文书|制度.*坏|求.*点评|免费.*点评",
    re.I,
)


def _qs_process_action_hits(post: dict[str, Any]) -> set[str]:
    comments = post.get("comments") or []
    comment_text = " ".join(
        str(item.get("body") or item.get("text") or "") if isinstance(item, dict) else str(item)
        for item in comments[:5]
    )
    text = " ".join([str(post.get("title") or ""), str(post.get("content") or ""), comment_text])
    return {name for name, pattern in _QS_PROCESS_ACTION_PATTERNS if pattern.search(text)}


def _qs_process_dimension_hits(post: dict[str, Any], plan: dict[str, Any]) -> set[str]:
    existing = post.get("_process_dimensions") or []
    if isinstance(existing, list) and existing:
        return {str(item) for item in existing if str(item) in {"stages", "tasks", "roles", "tools"}}
    text = " ".join([str(post.get("title") or ""), str(post.get("content") or "")]).lower()
    actions = _qs_process_action_hits(post)
    hits: set[str] = set()
    if actions:
        hits.add("tasks")
    if _QS_PROCESS_SEQUENCE_RE.search(text) or len(actions) >= 2:
        hits.add("stages")
    if "feedback" in actions and re.search(r"counselor|teacher|parent|mentor|advisor|student|顾问|老师|家长|学生", text, re.I):
        hits.add("roles")
    if "manage" in actions or re.search(r"common app|ucas|google docs?|notion|spreadsheet|calendar|文档|表格|日历", text, re.I):
        hits.add("tools")
    return hits


def _qs_requested_process_scopes(query: str) -> set[str]:
    text = str(query or "")
    scopes: set[str] = set()
    if re.search(r"欧美|美国|美本|美高|\bUS\b|United States|Common App", text, re.I):
        scopes.add("us")
    if re.search(r"欧美|英国|欧洲|英本|\bUK\b|Europe|European|UCAS", text, re.I):
        scopes.add("europe")
    return scopes


def _qs_process_post_scopes(post: dict[str, Any]) -> set[str]:
    text = " ".join([str(post.get("title") or ""), str(post.get("content") or ""), str(post.get("source") or "")])
    scopes: set[str] = set()
    if re.search(r"Common App|ApplyingToCollege|CollegeEssays|supplemental essays?|US college|American universit|美本", text, re.I):
        scopes.add("us")
    if re.search(r"UCAS|6thForm|UniUK|UK universit|British universit|European universit|英本|欧洲大学", text, re.I):
        scopes.add("europe")
    return scopes


def _qs_filter_process_evidence(posts: list[dict[str, Any]], query: str, plan: dict[str, Any]) -> list[dict[str, Any]]:
    """只保留正文中真正出现流程动作的帖子，检索来源本身不能充当流程证据。"""
    if str(plan.get("research_type") or "") != "process_workflow":
        return posts
    kept: list[dict[str, Any]] = []
    for post in posts:
        dimensions = _qs_process_dimension_hits(post, plan)
        actions = _qs_process_action_hits(post)
        text = " ".join([str(post.get("title") or ""), str(post.get("content") or "")])
        is_weak = bool(_QS_PROCESS_WEAK_POST_RE.search(text))
        explicit_sequence = bool(_QS_PROCESS_SEQUENCE_RE.search(text))
        if not actions or (is_weak and not explicit_sequence):
            continue
        post["_process_dimensions"] = sorted(dimensions)
        post["_process_actions"] = sorted(actions)
        post["_process_scopes"] = sorted(_qs_process_post_scopes(post))
        kept.append(post)
    print(f"[QuickSearch] process evidence {len(posts)} -> {len(kept)}")
    return kept


def _qs_process_evidence_issue(posts: list[dict[str, Any]], query: str) -> tuple[str, str]:
    """返回流程证据缺口；空字符串表示达到生成门槛。"""
    if len(posts) < 4:
        return (f"仅找到 {len(posts)} 条直接流程证据", f"Only {len(posts)} direct workflow evidence items were found")
    stage_count = sum("stages" in set(post.get("_process_dimensions") or []) for post in posts)
    actions = {action for post in posts for action in (post.get("_process_actions") or [])}
    if stage_count < 2 or len(actions) < 3:
        return ("证据没有覆盖足够的先后阶段和具体工作", "The evidence does not cover enough sequential stages and concrete tasks")
    requested_scopes = _qs_requested_process_scopes(query)
    if len(requested_scopes) > 1:
        scope_counts = Counter(scope for post in posts for scope in (post.get("_process_scopes") or []))
        missing = [scope for scope in requested_scopes if scope_counts.get(scope, 0) < 2]
        if missing:
            labels_zh = {"us": "美国路径", "europe": "英国/欧洲路径"}
            labels_en = {"us": "the US pathway", "europe": "the UK/European pathway"}
            return (
                "缺少足够的" + "、".join(labels_zh[item] for item in sorted(missing)) + "直接证据",
                "There is not enough direct evidence for " + " and ".join(labels_en[item] for item in sorted(missing)),
            )
    return "", ""


def _qs_process_query_dimension(query: str, plan: dict[str, Any]) -> str:
    normalized = re.sub(r"\s+", " ", str(query or "")).strip().lower()
    for key, label in (("stage_queries", "stages"), ("task_queries", "tasks"), ("role_queries", "roles"), ("tool_queries", "tools")):
        values = plan.get(key) or []
        if isinstance(values, str):
            values = [values]
        if any(re.sub(r"\s+", " ", str(value or "")).strip().lower() == normalized for value in values):
            return label
    return ""


def _qs_diversify_process_posts(posts: list[dict[str, Any]], plan: dict[str, Any], limit: int) -> list[dict[str, Any]]:
    """在同一候选池内优先保留不同流程维度；不为覆盖维度引入弱相关帖子。"""
    if str(plan.get("research_type") or "") != "process_workflow" or len(posts) <= 1:
        return posts[:limit]
    annotated = []
    for post in posts:
        hits = _qs_process_dimension_hits(post, plan)
        post["_process_dimensions"] = sorted(hits)
        annotated.append((post, hits))
    selected: list[dict[str, Any]] = []
    covered: set[str] = set()
    for post, hits in annotated:
        if len(selected) >= limit:
            break
        if hits - covered:
            selected.append(post)
            covered.update(hits)
    for post, hits in annotated:
        if len(selected) >= limit:
            break
        if hits and post not in selected:
            selected.append(post)
    return selected


def _qs_sanitize_process_summary(summary: str, posts_or_count: Any, language: Any = UI_LANGUAGE_ZH) -> str:
    """拒绝缺少阶段证据或引用不存在帖子的流程总结，防止常识补全进入 UI。"""
    text = str(summary or "").strip()
    posts = posts_or_count if isinstance(posts_or_count, list) else []
    post_count = len(posts) if posts else int(posts_or_count or 0)
    heading = r"流程阶段|Workflow Stages"
    if not re.search(rf"##\s*(?:{heading})", text, re.I):
        return ""
    section_match = re.search(rf"##\s*(?:{heading})\s*\n(.*?)(?=\n##\s+|$)", text, re.I | re.S)
    section = section_match.group(1) if section_match else ""
    stages = re.findall(r"^###\s+.+$", section, re.M)
    evidence_lines = re.findall(r"(?:证据|Evidence)\s*[:：]?\s*((?:帖子|Post)\s*\d+(?:\s*[,，]\s*(?:帖子|Post)?\s*\d+)*)", section, re.I)
    referenced = [int(item) for line in evidence_lines for item in re.findall(r"(?:帖子|Post)\s*(\d+)", line, re.I)]
    if not (2 <= len(stages) <= 5) or len(evidence_lines) < len(stages) or not referenced or any(index < 1 or index > post_count for index in referenced):
        return ""
    if len(set(referenced)) < len(stages):
        return ""
    if posts and any(
        not (_qs_process_dimension_hits(posts[index - 1], {}) & {"stages", "tasks"})
        for index in referenced
    ):
        return ""
    return text


_QS_COMMUNITY_RULES: tuple[tuple[re.Pattern, dict[str, list[str] | str]], ...] = (
    (
        re.compile(r"健康|疾病|身体|医疗|养生|睡眠|失眠|焦虑|抑郁|减肥|减重|疼痛|肠胃|血糖|health|sleep|insomnia|anxiety|depression|weight|pain|gut|diabetes", re.I),
        {
            "topic_anchor": "Everyday health problems Reddit users are discussing most",
            "problem_queries": [
                "chronic pain exhausted",
                "can't sleep anymore",
                "anxiety getting worse",
                "weight loss plateau",
                "gut issues daily",
                "Ozempic side effects",
            ],
            "solution_queries": [
                "how do you handle insomnia",
                "anxiety treatment recommendation",
                "chronic pain what helps",
                "gut health advice",
            ],
            "discovery_queries": [
                "What health problem has been affecting your life the most lately",
                "What health issue are people struggling with every day",
            ],
            "subreddits": ["Health", "sleep", "insomnia", "ChronicPain", "mentalhealth", "loseit", "Ozempic", "Biohackers"],
            "reasoning": "LLM 搜索规划不可用，已使用健康类垂直社区和英文高信号搜索词兜底。",
        },
    ),
    (
        re.compile(r"记账|预算|理财|消费|收据|expense|budget|finance|receipt", re.I),
        {
            "topic_anchor": "Personal finance app pain points and alternatives",
            "problem_queries": ["budgeting app frustrated", "expense tracker manual entry", "receipt scanner problems", "YNAB alternative"],
            "solution_queries": ["best budgeting app reddit", "automatic expense categorization", "personal finance app recommendation"],
            "discovery_queries": ["I am tired of manually tracking expenses", "What personal finance app do people actually trust"],
            "subreddits": ["personalfinance", "ynab", "budget", "povertyfinance", "Frugal"],
            "reasoning": "LLM 搜索规划不可用，已使用个人财务类垂直社区和英文搜索词兜底。",
        },
    ),
    (
        re.compile(r"戒饮|戒酒|酒精|sobriety|sober|quit drinking|alcohol", re.I),
        {
            "topic_anchor": "Sobriety and quit drinking app/community pain points",
            "problem_queries": ["quit drinking struggle", "sobriety app frustrated", "alcohol cravings relapse", "I Am Sober alternative"],
            "solution_queries": ["best sobriety app reddit", "quit drinking support app", "alcohol tracker recommendation"],
            "discovery_queries": ["What helps people stay sober when cravings come back", "Why do people quit using sobriety apps"],
            "subreddits": ["stopdrinking", "Sober", "Alcoholism_Medication", "dryalcoholics"],
            "reasoning": "LLM 搜索规划不可用，已使用戒饮类垂直社区和英文搜索词兜底。",
        },
    ),
)


def _qs_rule_based_community_plan(query: str) -> dict[str, Any]:
    """LLM 不可用时的社区搜索兜底规划，避免中文问题直接全局搜 Reddit。"""
    text = str(query or "")
    for pattern, plan in _QS_COMMUNITY_RULES:
        if pattern.search(text):
            return {k: list(v) if isinstance(v, list) else v for k, v in plan.items()}
    fallback_topic = re.sub(r"\s+", " ", text).strip()[:80] or "user pain points"
    return {
        "topic_anchor": fallback_topic,
        "problem_queries": [
            f"{fallback_topic} frustrated",
            f"{fallback_topic} problem",
            f"{fallback_topic} struggle",
            f"{fallback_topic} alternative",
        ],
        "solution_queries": [
            f"{fallback_topic} recommendation",
            f"best {fallback_topic}",
        ],
        "discovery_queries": [
            f"What do people complain about around {fallback_topic}",
            f"What are people looking for around {fallback_topic}",
        ],
        "subreddits": ["AskReddit", "NoStupidQuestions", "Productivity", "apps"],
        "reasoning": "LLM 搜索规划不可用，已使用通用英文高信号搜索词兜底。",
    }


def _qs_detect_strategy(query: str, requested: str = "auto") -> dict[str, str]:
    """判断雷达搜索应优先查社区讨论、竞品市场，还是两者都查。"""
    requested = (requested or "auto").strip().lower()
    if requested in {"community", "competitor", "hybrid"}:
        return {"mode": requested, "reason": "用户指定搜索策略"}

    text = str(query or "").strip().lower()
    community_patterns = (
        r"讨论|社区|reddit|帖子|评论|大家|用户|吐槽|抱怨|痛点|需求|热议|最多.*问题|"
        r"people discuss|discussion|complain|pain point|community|forum|subreddit"
    )
    competitor_patterns = (
        r"竞品|竞争|对手|替代品|有哪些.*产品|有哪些.*app|app|应用|产品|赛道|市场|收入|下载|"
        r"增长|增速|窜榜|排行|排名|榜单|商业化|素材|投放|sensor tower|app store|"
        r"competitor|alternative|top app|revenue|downloads|growth|ranking"
    )
    explicit_competitor = re.search(r"竞品|竞争|对手|有哪些.*(?:产品|app|应用)|收入|下载|增长|窜榜|排行|排名|榜单|素材|投放|competitor|revenue|downloads|growth|ranking", text)
    community_hit = bool(re.search(community_patterns, text))
    competitor_hit = bool(re.search(competitor_patterns, text))

    if explicit_competitor and community_hit:
        return {"mode": "hybrid", "reason": "问题同时包含社区讨论和竞品/市场信号"}
    if explicit_competitor or (competitor_hit and not community_hit):
        return {"mode": "competitor", "reason": "问题更像竞品、赛道或市场数据查询"}
    return {"mode": "community", "reason": "问题更像社区讨论、痛点或热议主题查询"}


def _qs_market_search_queries(plan: dict, fallback: str, reddit_queries: list[str]) -> list[str]:
    """从快速搜索规划里提取适合 SensorTower autocomplete 的 App/市场查询词。"""
    candidates: list[str] = []
    for key in ("known_competitors", "market_queries", "category_terms", "solution_queries"):
        val = plan.get(key, [])
        if isinstance(val, str):
            val = [val]
        if not isinstance(val, list):
            continue
        for raw in val[:8]:
            text = re.sub(r"\s+", " ", str(raw or "")).strip()
            if text:
                candidates.append(text)
    for q in reddit_queries[:8]:
        text = re.sub(r"\s+", " ", str(q or "")).strip()
        if not text:
            continue
        lower = text.lower()
        if any(token in lower for token in (" app", " apps", " tool", " software", "alternative", "best ")):
            candidates.append(text)
    if fallback and re.search(r"[A-Za-z]", fallback):
        candidates.append(fallback)
    candidates.extend(_qs_market_rule_queries(fallback))
    if fallback:
        candidates.extend(_qs_market_fallback_queries(fallback))
        candidates.append(fallback)

    seen: set[str] = set()
    out: list[str] = []
    for q in candidates:
        key = q.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(q[:80])
        if len(out) >= 12:
            break
    return out


_QS_MARKET_RULES: tuple[tuple[re.Pattern, list[str]], ...] = (
    (
        re.compile(r"戒饮|戒酒|酒精|戒断|戒瘾|少喝酒|sobriety|sober|quit drinking|stop drinking|alcohol", re.I),
        ["sobriety app", "quit drinking app", "alcohol tracker", "I Am Sober", "Reframe", "Sunnyside", "Nomo"],
    ),
    (
        re.compile(r"基督|圣经|祷告|教会|讲道|灵修|宗教|christian|bible|prayer|church|devotional|sermon", re.I),
        ["Christian app", "Bible app", "prayer app", "devotional app", "YouVersion Bible", "Hallow", "Pray.com", "Glorify"],
    ),
    (
        re.compile(r"攀岩|抱石|climbing|climber|bouldering|crag", re.I),
        ["climbing app", "rock climbing app", "bouldering app", "Mountain Project", "KAYA", "Vertical-Life", "27 Crags", "Crimpd"],
    ),
    (
        re.compile(r"跑步|跑者|马拉松|running|runner|marathon|jogging", re.I),
        ["running app", "run tracker", "running tracker", "marathon training", "5K training", "Strava", "Nike Run Club", "Runna", "Map My Run"],
    ),
    (
        re.compile(r"睡眠|助眠|失眠|冥想|meditation|sleep|insomnia|mindfulness", re.I),
        ["sleep app", "meditation app", "sleep meditation app", "insomnia app", "Calm", "Headspace", "BetterSleep", "Sleep Cycle"],
    ),
    (
        re.compile(r"记账|预算|财务|理财|消费|收据|expense|budget|budgeting|personal finance|receipt", re.I),
        ["budgeting app", "expense tracker", "personal finance app", "receipt scanner", "Rocket Money", "Monarch Money", "YNAB"],
    ),
    (
        re.compile(r"戒烟|尼古丁|烟草|quit smoking|stop smoking|nicotine", re.I),
        ["quit smoking app", "smoke free app", "nicotine tracker", "Smoke Free", "QuitNow", "Kwit"],
    ),
    (
        re.compile(r"健身|训练|减脂|减肥|运动|fitness|workout|weight loss|gym", re.I),
        ["fitness app", "workout app", "weight loss app", "gym app", "MyFitnessPal", "Fitbod", "Nike Training Club"],
    ),
    (
        re.compile(r"女性健康|经期|月经|排卵|备孕|怀孕|fertility|period|menstrual|ovulation|women health|pregnancy", re.I),
        ["women health app", "period tracker", "fertility app", "ovulation tracker", "pregnancy app", "Flo", "Clue", "Ovia"],
    ),
    (
        re.compile(r"糖尿病|血糖|慢病|diabetes|blood glucose|glucose", re.I),
        ["diabetes app", "glucose tracker", "blood sugar app", "mySugr", "One Drop", "Glucose Buddy"],
    ),
    (
        re.compile(r"瑜伽|普拉提|yoga|pilates", re.I),
        ["yoga app", "pilates app", "Down Dog", "Alo Moves", "Glo"],
    ),
    (
        re.compile(r"徒步|户外|露营|登山|hiking|outdoor|camping|trail", re.I),
        ["hiking app", "trail app", "camping app", "AllTrails", "Komoot", "Gaia GPS"],
    ),
    (
        re.compile(r"骑行|自行车|cycling|bike|biking", re.I),
        ["cycling app", "bike tracker", "Strava", "Komoot", "Ride with GPS"],
    ),
    (
        re.compile(r"饮食|食谱|热量|营养|断食|recipe|meal|calorie|nutrition|fasting", re.I),
        ["recipe app", "meal planner", "calorie counter", "nutrition app", "fasting app", "MyFitnessPal", "Lose It", "Yazio"],
    ),
    (
        re.compile(r"护肤|美妆|美容|发型|skin care|skincare|beauty|makeup|hair", re.I),
        ["skincare app", "beauty app", "makeup app", "hair style app", "Think Dirty", "Yuka"],
    ),
    (
        re.compile(r"心理|焦虑|抑郁|治疗|therapy|mental health|anxiety|depression", re.I),
        ["mental health app", "therapy app", "anxiety app", "CBT app", "BetterHelp", "Talkspace", "Wysa"],
    ),
    (
        re.compile(r"笔记|日记|写作|文档|note|notes|journal|writing", re.I),
        ["notes app", "journal app", "writing app", "Notion", "Evernote", "Day One", "Goodnotes"],
    ),
    (
        re.compile(r"待办|任务|效率|习惯|todo|task|productivity|habit", re.I),
        ["todo app", "task manager", "productivity app", "habit tracker", "Todoist", "TickTick", "Things", "Habitify"],
    ),
    (
        re.compile(r"语言|翻译|英语|学习|language learning|translation|translate|english learning", re.I),
        ["language learning app", "translation app", "English learning app", "Duolingo", "Babbel", "Google Translate"],
    ),
    (
        re.compile(r"育儿|怀孕|母婴|宝宝|parenting|pregnancy|baby", re.I),
        ["parenting app", "pregnancy app", "baby tracker", "What to Expect", "The Bump", "Huckleberry"],
    ),
    (
        re.compile(r"宠物|狗|猫|pet|dog|cat", re.I),
        ["pet care app", "dog training app", "pet health app", "Rover", "Chewy", "Tractive"],
    ),
    (
        re.compile(r"阅读|图书|小说|电子书|read|reading|book|ebook|audiobook", re.I),
        ["reading app", "ebook app", "audiobook app", "Kindle", "Audible", "Goodreads"],
    ),
    (
        re.compile(r"播客|音频|音乐|podcast|audio|music", re.I),
        ["podcast app", "music app", "audio app", "Spotify", "Apple Music", "Pocket Casts"],
    ),
    (
        re.compile(r"照片|修图|图片|视频|剪辑|photo|image|video|editing", re.I),
        ["photo editor app", "video editor app", "image editing app", "CapCut", "Canva", "Picsart"],
    ),
    (
        re.compile(r"约会|交友|dating|relationship", re.I),
        ["dating app", "relationship app", "Tinder", "Bumble", "Hinge"],
    ),
    (
        re.compile(r"旅行|行程|酒店|航班|travel|trip|hotel|flight", re.I),
        ["travel app", "trip planner", "flight booking app", "hotel booking app", "Tripadvisor", "Booking.com"],
    ),
)


_QS_MARKET_INTENT_TERMS = (
    "赛道", "竞品", "竞争", "对手", "产品", "应用", "市场", "收入", "下载", "增长",
    "榜单", "有哪些", "现在", "目前", "最快", "最大", "最多", "是什么", "app", "apps",
    "application", "competitor", "competitors", "alternative", "alternatives", "market",
    "revenue", "download", "downloads", "growth", "fastest", "largest", "biggest",
)


def _qs_market_rule_queries(query: str) -> list[str]:
    """常见中文/英文赛道的稳定兜底，不依赖 LLM。"""
    text = str(query or "")
    values: list[str] = []
    for pattern, queries in _QS_MARKET_RULES:
        if pattern.search(text):
            values.extend(queries)
    return _qs_dedupe_texts(values, limit=12)


def _qs_market_fallback_queries(query: str) -> list[str]:
    """从英文输入里抽取保底 App 类目词，防止规划为空。"""
    text = re.sub(r"\s+", " ", str(query or "")).strip()
    if not text or not re.search(r"[A-Za-z]", text):
        return []
    cleaned = text
    for term in _QS_MARKET_INTENT_TERMS:
        cleaned = re.sub(rf"\b{re.escape(term)}\b", " ", cleaned, flags=re.I)
    cleaned = re.sub(r"[^\w\s+-]", " ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    words = [w for w in cleaned.split() if len(w) > 1]
    topic = " ".join(words[:4]).strip()
    if not topic:
        return []
    queries = [topic]
    lower_topic = topic.lower()
    if "app" not in lower_topic:
        queries.append(f"{topic} app")
    return _qs_dedupe_texts(queries, limit=3)


def _qs_normalize_market_plan(parsed: Any) -> tuple[dict[str, Any], str]:
    """校验并归一化 LLM 生成的市场搜索规划。"""
    if not isinstance(parsed, dict):
        return {}, "LLM 未输出 JSON 对象"

    out: dict[str, Any] = {}
    total_terms = 0
    issues: list[str] = []
    blocking_issues: list[str] = []
    for key, cap in (("market_queries", 10), ("known_competitors", 8), ("category_terms", 8)):
        val = parsed.get(key, [])
        if isinstance(val, str):
            val = [val]
        if not isinstance(val, list):
            issues.append(f"{key} 不是数组")
            continue
        items: list[str] = []
        for raw in val[:cap]:
            text = re.sub(r"\s+", " ", str(raw or "")).strip()
            if not text:
                continue
            if not re.search(r"[A-Za-z]", text):
                issues.append(f"{key} 含非英文项")
                continue
            items.append(text[:80])
        items = _qs_dedupe_texts(items, limit=cap)
        if items:
            out[key] = items
            total_terms += len(items)
        else:
            issues.append(f"{key} 为空")

    topic_en = re.sub(r"\s+", " ", str(parsed.get("topic_en") or "")).strip()
    if topic_en and re.search(r"[A-Za-z]", topic_en):
        out["topic_en"] = topic_en[:80]

    intent = str(parsed.get("intent") or "").strip().lower()
    if intent:
        out["market_intent"] = intent[:40]
    reasoning = str(parsed.get("reasoning") or "").strip()
    if reasoning:
        out["market_reasoning"] = reasoning[:200]
    confidence = _qs_float_0_1(parsed.get("confidence"), 0.68)
    out["planning_confidence"] = confidence
    if confidence < 0.45:
        blocking_issues.append(f"规划置信度过低：{confidence:.2f}")

    if total_terms < 4:
        issues.append(f"英文可用词不足：{total_terms}")
    if not out.get("market_queries") and out.get("topic_en"):
        out["market_queries"] = [out["topic_en"], f"{out['topic_en']} app"]

    if blocking_issues:
        return out, "；".join(blocking_issues)
    if total_terms >= 4 or out.get("market_queries"):
        return out, ""
    return out, "；".join(_qs_dedupe_texts(issues, limit=6)) or "规划结果不可用"


def _qs_dedupe_texts(values: list[str], limit: int = 12) -> list[str]:
    """按小写去重短文本，并保留原始顺序。"""
    seen: set[str] = set()
    out: list[str] = []
    for raw in values:
        text = re.sub(r"\s+", " ", str(raw or "")).strip()
        if not text:
            continue
        key = text.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(text[:80])
        if len(out) >= limit:
            break
    return out


def _qs_float_0_1(value: Any, default: float = 0.0) -> float:
    """读取 LLM 输出的置信度，异常时给保守默认值。"""
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = default
    return max(0.0, min(1.0, number))


def _qs_market_plan(query: str) -> dict[str, Any]:
    """用 LLM 将任意用户问题规划成 SensorTower 可执行的英文市场查询词。"""
    attempts = [
        QUICK_SEARCH_MARKET_PLANNING_PROMPT.format(query=query),
    ]
    previous_output = ""
    issue = "未开始"

    for attempt_index, prompt in enumerate(attempts, start=1):
        try:
            resp = call_llm(
                [
                    {"role": "system", "content": "你是 App 市场搜索规划助手，只输出合法 JSON，不要解释。"},
                    {"role": "user", "content": prompt},
                ],
                max_tokens=1500,
            )
            previous_output = (resp or "")[:700]
            parsed = _parse_json_from_text(resp)
            out, issue = _qs_normalize_market_plan(parsed)
            if not issue:
                out["planning_source"] = "llm" if attempt_index == 1 else "repair"
                out["planning_issue"] = ""
                return out
            print(f"[QuickSearch] market plan unusable attempt={attempt_index} query={query[:80]!r} issue={issue} preview={previous_output[:180]!r}")
        except Exception as e:
            issue = _qs_planning_error_message(e)
            print(f"[QuickSearch] market plan failed attempt={attempt_index} query={query[:80]!r}: {e}")

        if attempt_index == 1:
            attempts.append(QUICK_SEARCH_MARKET_REPAIR_PROMPT.format(
                query=query,
                issue=issue,
                previous_output=previous_output or "（无输出）",
            ))

    rule_queries = _qs_market_rule_queries(query)
    weak_fallback_queries = _qs_market_fallback_queries(query)
    fallback_queries = rule_queries + weak_fallback_queries
    fallback_reason = f"LLM 市场规划未通过校验，已使用兜底查询词（{issue[:80]}）"
    out = {
        "market_queries": _qs_dedupe_texts(fallback_queries, limit=12),
        "market_intent": "competitors",
        "market_reasoning": fallback_reason,
        "planning_source": "rule_fallback" if rule_queries else "weak_fallback",
        "planning_confidence": 0.58 if rule_queries else 0.35,
        "planning_issue": issue,
    }
    return out if out["market_queries"] else {"market_reasoning": fallback_reason}


def _qs_merge_market_plan(base_plan: dict | None, market_plan: dict | None) -> dict:
    """合并 Reddit 规划和市场规划，保留两边生成的查询词。"""
    merged = dict(base_plan or {})
    for key in ("market_queries", "known_competitors", "category_terms"):
        values: list[str] = []
        for source in (base_plan or {}, market_plan or {}):
            val = source.get(key, [])
            if isinstance(val, str):
                val = [val]
            if isinstance(val, list):
                values.extend(str(v).strip() for v in val if str(v).strip())
        if values:
            merged[key] = _qs_dedupe_texts(values, limit=12)
    if market_plan:
        if market_plan.get("market_intent"):
            merged["market_intent"] = market_plan["market_intent"]
        if market_plan.get("market_reasoning"):
            merged["market_reasoning"] = market_plan["market_reasoning"]
        if market_plan.get("planning_source"):
            merged["planning_source"] = market_plan["planning_source"]
        if market_plan.get("planning_confidence") is not None:
            merged["planning_confidence"] = market_plan["planning_confidence"]
        if market_plan.get("planning_issue"):
            merged["planning_issue"] = market_plan["planning_issue"]
    return merged


def _qs_market_sort_by(query: str, plan: dict | None = None) -> str | None:
    """根据用户问题和市场规划决定 ST 排序口径。"""
    lower = str(query or "").lower()
    if re.search(r"增长|增速|窜榜|上升|fastest|growth|growing|rising", lower):
        return "growth"
    if re.search(r"下载|download|downloads|install|installs", lower):
        return "downloads"
    if re.search(r"收入|营收|商业化|revenue|sales|grossing|monetization", lower):
        return "revenue"
    intent = str((plan or {}).get("market_intent") or "").lower()
    if intent in {"app_competitors", "competitors", "category", "mixed"}:
        return "scale"
    if re.search(r"竞品|竞争|对手|有哪些.*(?:产品|app|应用)|competitor|alternative", lower):
        return "scale"
    return None


def _qs_validate_market_plan_for_search(plan: dict | None, query: str, reddit_queries: list[str]) -> tuple[bool, str, list[str]]:
    """市场搜索执行前的硬校验；低可信规划直接拦截，不再硬搜。"""
    if not isinstance(plan, dict):
        return False, "市场搜索规划不是 JSON 对象", []
    market_intent = str(plan.get("market_intent") or "").lower()
    if market_intent in {"direct_app", "app_competitors"}:
        direct_queries = plan.get("market_queries") or []
        if isinstance(direct_queries, str):
            direct_queries = [direct_queries]
        queries = _qs_dedupe_texts([str(q) for q in direct_queries if str(q).strip()], limit=3)
        return (bool(queries), "没有识别到可查询的 App 名称" if not queries else "", queries)

    queries = _qs_market_search_queries(plan, query, reddit_queries)
    if len(queries) < 2:
        return False, "没有生成足够的 Sensor Tower 英文查询词", queries
    unsafe = [
        q for q in queries
        if "site:" in q.lower()
        or "reddit" in q.lower()
        or _QS_POLITICAL_TEXT_PATTERNS.search(q)
    ]
    if unsafe:
        return False, "市场搜索词包含不支持的网页/时政方向", queries

    source = str(plan.get("planning_source") or "unknown")
    confidence = _qs_float_0_1(plan.get("planning_confidence"), 0.62 if source in {"llm", "repair"} else 0.0)
    if source == "weak_fallback":
        return False, "搜索词规划失败，只得到弱兜底词", queries
    if confidence < 0.45:
        return False, f"市场搜索规划置信度过低（{confidence:.2f}）", queries
    if _qs_is_query_too_vague(query):
        return False, "用户问题缺少可执行主题", queries
    return True, "", queries


def _qs_norm_comment(c) -> dict:
    """rdt_client 评论为 str；read_post 充实后可能为 dict。"""
    if isinstance(c, dict):
        return {
            "body": (c.get("body") or "")[:300],
            "body_zh": (c.get("body_zh") or "")[:300],
            "score": c.get("score", 0),
        }
    if isinstance(c, str):
        return {"body": c[:300], "body_zh": "", "score": 0}
    return {"body": "", "body_zh": "", "score": 0}


def _qs_llm_translate_json(prompt: str, max_tokens: int = 3000) -> dict[str, str]:
    """调用 LLM 并解析为 str->str 映射。"""
    try:
        resp = call_llm(
            [{"role": "system", "content": "只输出 JSON 对象，不要代码块。"}, {"role": "user", "content": prompt}],
            max_tokens=max_tokens,
        )
        parsed = _parse_json_from_text(resp)
        if not isinstance(parsed, dict):
            return {}
        out: dict[str, str] = {}
        for k, v in parsed.items():
            if isinstance(v, str) and v.strip():
                out[str(k).strip()] = v.strip()
            elif isinstance(v, dict) and isinstance(v.get("zh"), str):
                out[str(k).strip()] = v["zh"].strip()
        return out
    except Exception as e:
        print(f"[QuickSearch] translate chunk failed: {e}")
        return {}


def _qs_batch_translate(posts: list[dict], max_posts: int = 50) -> dict[str, str]:
    """分块翻译标题 / 正文 / 评论，避免单次 JSON 过大导致解析失败。"""
    result: dict[str, str] = {}
    n = min(len(posts), max_posts)

    # 1) 标题：每批 20 条，与采集兜底相同的序号 JSON
    for start in range(0, n, 20):
        chunk = posts[start:start + 20]
        lines = [f"{i}: {p.get('title', '')}" for i, p in enumerate(chunk) if p.get("title")]
        if not lines:
            continue
        prompt = (
            "将以下英文 Reddit 帖子标题译为自然中文。输出纯 JSON："
            '{"0":"中文标题","1":"中文标题",...}，key 为本批从 0 开始的序号。\n\n'
            + "\n".join(lines)
        )
        batch_map = _qs_llm_translate_json(prompt, max_tokens=2500)
        for k, zh in batch_map.items():
            try:
                local_i = int(k)
                result[f"t{start + local_i}"] = zh
            except ValueError:
                pass

    # 2) 正文与评论：每批最多 12 条文本；若标题批量翻译失败，也把标题并入兜底翻译。
    items: list[dict] = []
    for pi in range(n):
        p = posts[pi]
        title = (p.get("title") or "").strip()
        if title and not result.get(f"t{pi}"):
            items.append({"id": f"t{pi}", "text": title[:260]})
        content = (p.get("content") or "").strip()
        if content and len(content) > 20:
            items.append({"id": f"c{pi}", "text": content[:450]})
        for ci, c in enumerate((_qs_norm_comment(x) for x in (p.get("comments") or [])[:5])):
            body = (c.get("body") or "").strip()
            if body and len(body) > 15:
                items.append({"id": f"m{pi}_{ci}", "text": body[:350]})

    for start in range(0, len(items), 12):
        batch = items[start:start + 12]
        prompt = QUICK_SEARCH_TRANSLATE_PROMPT.format(
            items_json=json.dumps(batch, ensure_ascii=False),
        )
        batch_map = _qs_llm_translate_json(prompt, max_tokens=3500)
        for item in batch:
            item_id = item["id"]
            if item_id in batch_map:
                result[item_id] = batch_map[item_id]

    print(f"[QuickSearch] translated keys: {len(result)} (posts={n}, items={len(items)})")
    return result


def _qs_apply_translations(posts: list[dict], trans: dict[str, str]) -> None:
    """将翻译结果写回帖子 dict（就地修改）。"""
    if not trans:
        return
    for pi, p in enumerate(posts):
        if trans.get(f"t{pi}"):
            p["title_zh"] = trans[f"t{pi}"]
        if trans.get(f"c{pi}"):
            p["content_zh"] = trans[f"c{pi}"]
        comments = p.get("comments") or []
        for ci in range(len(comments)):
            zh = trans.get(f"m{pi}_{ci}")
            if not zh:
                continue
            c = comments[ci]
            if isinstance(c, dict):
                c["body_zh"] = zh
            elif isinstance(c, str):
                comments[ci] = {"body": c, "body_zh": zh, "score": 0}


_QS_PROFESSIONAL_SUBS = frozenset({"medicine", "nursing", "medicalschool", "residency"})
_QS_PROFESSIONAL_QUERY_MARKERS = (
    "医生", "护士", "医护", "医师", "住院医", "physician", "doctor", "nurse",
    "medical professional", "healthcare worker",
)


def _qs_sanitize_subreddits(subreddits: list[str], query: str) -> list[str]:
    """去掉与消费者健康问题无关的医护职业板块（除非用户明确问医护）。"""
    q_lower = query.lower()
    professional_focus = any(m in query for m in _QS_PROFESSIONAL_QUERY_MARKERS) or any(
        m in q_lower for m in ("physician", "nurse", "medical school", "residency")
    )
    out: list[str] = []
    seen: set[str] = set()
    for raw in subreddits:
        s = str(raw).strip().lstrip("r/").lower()
        if not s or s in seen:
            continue
        if s in _QS_PROFESSIONAL_SUBS and not professional_focus:
            continue
        seen.add(s)
        out.append(s)
    return out[:6]


def _qs_prioritize_process_subreddits(subreddits: list[str], query: str, research_type: str) -> list[str]:
    """多地区流程问题把不同路径的社区放进前 3 个定向搜索位，不增加请求数。"""
    if research_type != "process_workflow" or len(_qs_requested_process_scopes(query)) < 2:
        return subreddits
    us_hints = ("applyingtocollege", "collegeessays", "commonapp", "intlto")
    europe_hints = ("6thform", "uniuk", "ucas", "askuk", "europe")
    us = [item for item in subreddits if any(hint in item.lower() for hint in us_hints)]
    europe = [item for item in subreddits if any(hint in item.lower() for hint in europe_hints)]
    others = [item for item in subreddits if item not in us and item not in europe]
    ordered: list[str] = []
    priority = [*us[:1], *europe[:1], *us[1:2], *europe[1:2]]
    for group in (priority, us[2:], europe[2:], others):
        for item in group:
            if item not in ordered:
                ordered.append(item)
    return ordered[:6]


def _qs_filter_relevant_posts(
    posts: list[dict],
    topic: str,
    *,
    fallback_to_original: bool = True,
    min_keep: int = 5,
) -> list[dict]:
    """批量剔除与问题明显跑题的帖子。"""
    if len(posts) <= 3:
        return posts
    kept: list[dict] = []
    batch_size = 18
    for start in range(0, len(posts), batch_size):
        chunk = posts[start:start + batch_size]
        titles_json = json.dumps([
            {
                "idx": i,
                "title": p.get("title", ""),
                "snippet": (p.get("content") or "")[:150],
                "source": p.get("source", ""),
                "score": p.get("score", 0),
                "comments": p.get("num_comments", 0),
            }
            for i, p in enumerate(chunk)
        ], ensure_ascii=False)
        prompt = BATCH_RELEVANCE_PROMPT.format(topic=topic, titles_json=titles_json)
        try:
            resp = call_llm(
                [{"role": "system", "content": "只输出 JSON。"}, {"role": "user", "content": prompt}],
                max_tokens=800,
            )
            parsed = _parse_json_from_text(resp)
            if parsed and isinstance(parsed, dict):
                keep_idx = parsed.get("keep_indices", [])
                if isinstance(keep_idx, list):
                    for i in keep_idx:
                        try:
                            ii = int(i)
                            if 0 <= ii < len(chunk):
                                kept.append(chunk[ii])
                        except (TypeError, ValueError):
                            pass
                    continue
        except Exception as e:
            print(f"[QuickSearch] relevance filter batch failed: {e}")
        kept.extend(chunk)
    if len(kept) < max(min_keep, len(posts) // 4):
        print(f"[QuickSearch] relevance kept too few ({len(kept)})")
        if fallback_to_original:
            print("[QuickSearch] relevance fallback to original posts")
            return posts
    print(f"[QuickSearch] relevance {len(posts)} -> {len(kept)}")
    return kept


_QS_FAST_RELEVANCE_STOPWORDS = {
    "reddit", "english", "search", "queries", "query", "target", "community", "subreddit",
    "what", "which", "that", "this", "with", "around", "right", "now", "people", "users",
    "problem", "problems", "issue", "issues", "help", "best", "recommendation", "handle",
    "frustrated", "struggle", "struggling", "everyday", "afford", "lifestyle", "changes",
    "dealing", "nobody", "seems", "take", "seriously", "ruining", "life", "current",
    "identify", "ordinary", "actively", "complaining", "seeking", "comparing", "solutions",
    "getting", "worse", "side", "lately", "talking", "manage", "better", "anymore",
}

_QS_FAST_RELEVANCE_GENERIC_TERMS = {
    "health", "healthy", "problem", "problems", "issue", "issues", "topic", "topics",
    "recommendation", "recommendations", "discussion", "discussions", "biggest", "most",
}

_QS_FAST_RELEVANCE_NOISE_SUBS = {
    "aio", "amitheasshole", "amioverreacting", "bestofredditorupdates", "relationship_advice",
    "relationships", "cats", "cathelp", "dogadvice", "dogs", "lainfluencersnark",
    "moralityscaling", "claudecode", "programming", "jokes", "memes", "twohottakes",
    "doomercirclejerk", "drwillpowers", "bollyblindsngossip", "adhdmeme",
}

_QS_FAST_RELEVANCE_NOISE_SOURCE_HINTS = {"gossip", "snark", "circlejerk", "meme"}

_QS_POLITICAL_SUBS = {
    "china_irl", "real_china_irl", "china", "sino", "taiwan", "taiwanese", "hongkong",
    "politics", "worldnews", "news", "geopolitics", "europe", "ukraine", "russia",
    "dashuju", "roumanie",
}

_QS_POLITICAL_SOURCE_HINTS = {
    "politic", "worldnews", "geopolitic", "china_irl", "real_china", "dashuju",
}

_QS_POLITICAL_TEXT_PATTERNS = re.compile(
    r"习近平|邓小平|中共|共产党|政治|时政|党|政府|总统|主席|外交|战争|俄乌|台湾|香港|"
    r"极权|红色|纳粹|移民|劳工|制裁|\b(?:propaganda|communist|government|president|politics|"
    r"geopolitics|election|war|russia|ukraine|ccp)\b|xi jinping",
    re.I,
)

_QS_FAST_RELEVANCE_DOMAIN_SOURCE_HINTS = {
    "health", "medical", "mentalhealth", "sleep", "insomnia", "chronicpain", "pain",
    "loseit", "fitness", "nutrition", "supplements", "biohackers", "microbiome", "sibo",
    "diabetes", "ozempic", "adhd", "anxiety", "depression", "migraine",
}


def _qs_source_subreddit(post: dict) -> str:
    source = str(post.get("source") or "").lower()
    return source.split("reddit/", 1)[-1] if "reddit/" in source else source


def _qs_word_tokens(text: str) -> set[str]:
    return set(re.findall(r"[a-z][a-z0-9-]{1,}", str(text or "").lower()))


def _qs_is_political_or_current_affairs(post: dict) -> bool:
    """雷达搜索默认屏蔽敏感时政/地缘政治内容，避免污染业务需求搜索。"""
    source_sub = _qs_source_subreddit(post)
    if source_sub in _QS_POLITICAL_SUBS:
        return True
    if any(hint in source_sub for hint in _QS_POLITICAL_SOURCE_HINTS):
        return True
    text = " ".join([
        str(post.get("title") or ""),
        str(post.get("content") or ""),
        str(post.get("source") or ""),
    ])
    return bool(_QS_POLITICAL_TEXT_PATTERNS.search(text))


_QS_UNSUPPORTED_QUERY_PATTERNS = re.compile(
    r"天气|汇率|股票|彩票|体育比分|星座|八卦|新闻|热搜|翻译|写.*代码|写.*论文|写.*文案|"
    r"weather|stock|exchange rate|lottery|sports score|translate|write code|"
    r"news headline|breaking news",
    re.I,
)

_QS_VAGUE_ZH_TERMS = re.compile(
    r"(现在|目前|最近|今天|大家|用户|reddit|社区|讨论|最多|最大|最快|热门|"
    r"有什么|是什么|什么|有哪些|哪个好|帮我|查一下|看一下|搜索|问题|需求|痛点|"
    r"赛道|竞品|产品|应用|收入|下载|增长|排名|榜单|的|了|吗|呢|和|与|在|上|中)"
)

_QS_VAGUE_EN_TERMS = {
    "now", "current", "currently", "recent", "reddit", "community", "discussion",
    "discussed", "most", "biggest", "fastest", "popular", "what", "which", "who",
    "where", "are", "is", "the", "app", "apps", "product", "products", "problem",
    "problems", "pain", "points", "competitor", "competitors", "market", "revenue",
    "downloads", "growth", "ranking", "trend", "trends", "search",
}


def _qs_is_query_too_vague(query: str) -> bool:
    """判断用户问题是否缺少可执行主题，避免用泛问题直接扩大搜索。"""
    text = re.sub(r"\s+", " ", str(query or "")).strip()
    if not text:
        return True
    zh_residual = _QS_VAGUE_ZH_TERMS.sub("", text)
    zh_residual = re.sub(r"[^\u4e00-\u9fffA-Za-z0-9]+", "", zh_residual)
    en_tokens = [
        t for t in re.findall(r"[A-Za-z][A-Za-z0-9+-]{1,}", text.lower())
        if t not in _QS_VAGUE_EN_TERMS and len(t) >= 3
    ]
    has_specific_zh = bool(re.search(r"[\u4e00-\u9fff]{2,}", zh_residual))
    return not has_specific_zh and not en_tokens


def _qs_query_gate_rule_fallback(query: str, requested_strategy: str = "auto") -> dict[str, Any]:
    """LLM 分类器不可用时的保守兜底：只放行明确业务问题，危险或过泛问题仍然拦截。"""
    text = re.sub(r"\s+", " ", str(query or "")).strip()
    if _QS_POLITICAL_TEXT_PATTERNS.search(text):
        return {
            "ok": False,
            "status": "unsafe",
            "strategy": "community",
            "message": "雷达搜索目前只用于社区需求和竞品市场研究，不处理时政、政治、战争或新闻类问题。请换成具体业务赛道或用户痛点。",
            "reason": "规则兜底识别为时政/新闻风险",
        }
    if _QS_UNSUPPORTED_QUERY_PATTERNS.search(text):
        return {
            "ok": False,
            "status": "unsupported",
            "strategy": "community",
            "message": "这个问题不适合雷达搜索。请改成社区讨论、用户痛点、竞品、收入或下载相关的问题。",
            "reason": "规则兜底识别为不支持问题",
        }
    if _qs_is_query_too_vague(text):
        return {
            "ok": False,
            "status": "needs_clarification",
            "strategy": "community",
            "message": "请补充一个具体主题或赛道，例如“健康”“AI工具”“减肥”“出海App”等。",
            "reason": "规则兜底识别为主题过泛",
        }
    detected = _qs_detect_strategy(text, requested_strategy)
    research_type = "process_workflow" if _qs_is_process_research_query(text) else "general"
    return {
        "ok": True,
        "status": "searchable",
        "strategy": detected.get("mode", "community"),
        "topic": text[:80],
        "research_type": research_type,
        "intent_summary": text[:160],
        "requested_dimensions": ["stages", "tasks", "roles", "timeline"] if research_type == "process_workflow" else [],
        "confidence": 0.62,
        "message": "",
        "reason": f"搜索意图分类器异常，使用规则兜底：{detected.get('reason', '规则判断可搜索')}",
    }


def _qs_query_gate(query: str, requested_strategy: str = "auto") -> dict[str, Any]:
    """用 LLM 判断雷达搜索是否应该执行；代码规则只做基础和硬安全兜底。"""
    text = re.sub(r"\s+", " ", str(query or "")).strip()
    if not text:
        return {
            "ok": False,
            "status": "needs_clarification",
            "strategy": "community",
            "message": "请输入一个具体问题，例如人群、场景、痛点、赛道或 App 名称。",
            "reason": "空输入",
        }
    if len(text) < 4:
        return {
            "ok": False,
            "status": "needs_clarification",
            "strategy": "community",
            "message": "问题太短了，请补充具体人群、场景或赛道后再搜索。",
            "reason": "输入过短",
        }
    if len(text) > 260:
        return {
            "ok": False,
            "status": "needs_clarification",
            "strategy": "community",
            "message": "问题太长了，请压缩成一句明确的问题后再搜索。",
            "reason": "输入过长",
        }

    try:
        raw = call_llm(
            [
                {"role": "system", "content": "你是搜索意图与安全分类器，只输出合法 JSON。"},
                {
                    "role": "user",
                    "content": QUICK_SEARCH_QUERY_CLASSIFIER_PROMPT.format(
                        query=text,
                        requested_strategy=requested_strategy or "auto",
                    ),
                },
            ],
            max_tokens=650,
        )
        parsed = _parse_json_from_text(raw)
        if not isinstance(parsed, dict):
            raise ValueError("classifier output is not a dict")
    except Exception as e:
        print(f"[QuickSearch] query classifier failed: {e}")
        return _qs_query_gate_rule_fallback(text, requested_strategy)

    status = str(parsed.get("status") or "").strip().lower()
    if status not in {"searchable", "needs_clarification", "unsafe", "unsupported"}:
        status = "needs_clarification"
    strategy = str(parsed.get("strategy") or "").strip().lower()
    if strategy not in {"community", "competitor", "hybrid"}:
        strategy = _qs_detect_strategy(text, requested_strategy).get("mode", "community")

    # 硬安全兜底：即使 LLM 漏判，也不能让明显时政/非搜索任务继续执行。
    if status == "searchable" and _QS_POLITICAL_TEXT_PATTERNS.search(text):
        status = "unsafe"
    if status == "searchable" and _QS_UNSUPPORTED_QUERY_PATTERNS.search(text):
        status = "unsupported"
    if status == "searchable" and _qs_is_query_too_vague(text):
        status = "needs_clarification"
    confidence = _qs_float_0_1(parsed.get("confidence"), 0.72 if status == "searchable" else 0.0)
    if status == "searchable" and confidence < 0.55:
        status = "needs_clarification"

    default_messages = {
        "needs_clarification": "问题还不够具体，请补充一个明确主题，例如「人群 + 场景 + 痛点」或「赛道 + 竞品/收入/下载」。",
        "unsafe": "雷达搜索目前只用于社区需求和竞品市场研究，不处理时政、政治、战争或新闻类问题。请换成具体业务赛道或用户痛点。",
        "unsupported": "这个问题不适合雷达搜索。请改成社区讨论、用户痛点、竞品、收入或下载相关的问题。",
    }
    user_message = str(parsed.get("user_message") or "").strip()
    if status != "searchable" and not user_message:
        user_message = default_messages.get(status, default_messages["needs_clarification"])

    return {
        "ok": status == "searchable",
        "status": status,
        "strategy": strategy,
        "topic": str(parsed.get("topic") or "").strip(),
        "research_type": _qs_research_type(text, parsed),
        "intent_summary": str(parsed.get("intent_summary") or parsed.get("topic") or text).strip()[:160],
        "requested_dimensions": [
            str(item).strip()[:40]
            for item in (parsed.get("requested_dimensions") or [])
            if str(item).strip()
        ][:6] if isinstance(parsed.get("requested_dimensions") or [], list) else [],
        "confidence": confidence,
        "message": "" if status == "searchable" else user_message,
        "reason": str(parsed.get("reason") or "").strip(),
    }


def _qs_validate_community_plan(plan: dict, query: str, research_type: str = "general") -> tuple[bool, str]:
    """校验 LLM 社区搜索规划，失败时不允许继续硬搜 Reddit。"""
    if not isinstance(plan, dict):
        return False, "规划结果不是 JSON 对象"
    topic_anchor = str(plan.get("topic_anchor") or "")
    reasoning = str(plan.get("reasoning") or "")
    if _QS_POLITICAL_TEXT_PATTERNS.search(" ".join([topic_anchor, reasoning])):
        return False, "规划结果包含时政或新闻方向"

    plan["research_type"] = research_type
    if research_type == "process_workflow":
        _qs_normalize_process_queries(plan, research_type)
        dimension_queries = sum(bool(plan.get(key)) for key in ("stage_queries", "task_queries", "role_queries", "tool_queries"))
        if not plan.get("stage_queries") or not plan.get("task_queries") or dimension_queries < 2:
            return False, "流程搜索维度不足"
    queries = _qs_flatten_plan_queries(plan, "")
    english_queries = [q for q in queries if re.search(r"[A-Za-z]", q)]
    if len(english_queries) < 3:
        return False, "英文搜索词不足"
    unsafe_queries = [
        q for q in english_queries
        if "site:" in q.lower() or _QS_POLITICAL_TEXT_PATTERNS.search(q)
    ]
    if unsafe_queries:
        return False, "搜索词包含不安全或不支持的内容"

    raw_subs = plan.get("subreddits", [])
    if isinstance(raw_subs, str):
        raw_subs = [raw_subs]
    if not isinstance(raw_subs, list):
        return False, "subreddit 不是数组"
    safe_subs = _qs_sanitize_subreddits(raw_subs, query)
    safe_subs = [
        s for s in safe_subs
        if s not in _QS_POLITICAL_SUBS and not any(hint in s for hint in _QS_POLITICAL_SOURCE_HINTS)
    ]
    if len(safe_subs) < 2:
        return False, "安全垂直社区不足"
    plan["subreddits"] = safe_subs
    return True, ""


def _qs_gate_message_for_language(gate: dict[str, Any], language: Any) -> str:
    """把搜索拦截原因转换成当前 UI 语言，避免英文模式下弹中文提示。"""
    if not _is_ui_en(language):
        return str(gate.get("message") or "").strip()
    status = str(gate.get("status") or "").strip().lower()
    if status == "unsafe":
        return "Radar Search is for community demand and competitor market research. It does not handle politics, war, news, or public-event topics."
    if status == "unsupported":
        return "This question is outside Radar Search. Try asking about community discussions, pain points, competitors, revenue, or downloads."
    return "Please add a clearer topic, audience, scenario, category, or App name."


def _qs_planning_error_message(error: Exception, language: Any = UI_LANGUAGE_ZH) -> str:
    """把规划阶段异常转换成安全的用户提示，不暴露 403/HTML/key 信息。"""
    msg = str(error).lower()
    if "403" in msg or "forbidden" in msg:
        return _ui_text(
            language,
            "搜索词规划服务暂不可用，请稍后重试。本次不会继续执行 Reddit 搜索。",
            "Search planning is temporarily unavailable. Please try again later. Reddit search will not continue for this request.",
        )
    if "ssl" in msg or "eof" in msg or "connection" in msg or "timeout" in msg:
        return _ui_text(
            language,
            "搜索词规划服务连接不稳定，请稍后重试。本次不会继续执行 Reddit 搜索。",
            "Search planning connection is unstable. Please try again later. Reddit search will not continue for this request.",
        )
    return _ui_text(
        language,
        "搜索词规划没有通过安全校验，请重试，或把问题改得更具体一些。",
        "Search planning did not pass the safety check. Please retry or make the question more specific.",
    )


def _qs_runtime_error_message(error: Exception, language: Any = UI_LANGUAGE_ZH) -> str:
    """统一快速搜索运行期错误文案，避免把原始接口错误展示给用户。"""
    msg = str(error).lower()
    if "403" in msg or "forbidden" in msg:
        return _ui_text(language, "模型或数据服务暂不可用，请稍后重试。", "The model or data service is temporarily unavailable. Please try again later.")
    if "ssl" in msg or "eof" in msg or "connection" in msg or "timeout" in msg:
        return _ui_text(language, "外部数据服务连接不稳定，请稍后重试。", "The external data service connection is unstable. Please try again later.")
    if "rdt" in msg or "reddit" in msg:
        return _ui_text(language, "Reddit 本地引擎暂不可用，请检查 rdt-cli 认证后重试。", "The local Reddit engine is unavailable. Please check your local settings.")
    return _ui_text(language, "搜索过程中出现异常，请重试，或把问题改得更具体一些。", "Search failed unexpectedly. Please retry or make the question more specific.")


_QS_SENSOR_TOWER_UNAVAILABLE_HINT = "SensorTower因帐号问题常出现登录状态失效，如果不可用请检查本地配置"


_QS_TREND_REGION_PATTERNS: tuple[tuple[re.Pattern, str], ...] = (
    (re.compile(r"美国|美区|\bUS\b|United States", re.I), "US"),
    (re.compile(r"日本|日区|\bJP\b|Japan", re.I), "JP"),
    (re.compile(r"英国|英区|\bUK\b|\bGB\b|United Kingdom", re.I), "GB"),
    (re.compile(r"加拿大|加区|\bCA\b|Canada", re.I), "CA"),
    (re.compile(r"澳大利亚|澳洲|\bAU\b|Australia", re.I), "AU"),
    (re.compile(r"巴西|巴区|\bBR\b|Brazil", re.I), "BR"),
    (re.compile(r"菲律宾|\bPH\b|Philippines", re.I), "PH"),
    (re.compile(r"德国|德区|\bDE\b|Germany", re.I), "DE"),
    (re.compile(r"法国|法区|\bFR\b|France", re.I), "FR"),
    (re.compile(r"韩国|韩区|\bKR\b|Korea", re.I), "KR"),
    (re.compile(r"印度|\bIN\b|India", re.I), "IN"),
    (re.compile(r"印尼|印度尼西亚|\bID\b|Indonesia", re.I), "ID"),
    (re.compile(r"墨西哥|\bMX\b|Mexico", re.I), "MX"),
)


def _qs_parse_metric_trend_period(query: str) -> dict[str, Any]:
    """从自然语言里解析趋势查询周期；默认过去 60 天。"""
    text = str(query or "")
    today = date.today()
    yesterday = today - timedelta(days=1)

    def _last_month() -> dict[str, date]:
        first_this_month = date(today.year, today.month, 1)
        end = first_this_month - timedelta(days=1)
        return {"start_date": date(end.year, end.month, 1), "end_date": end}

    if re.search(r"上个?月|last\s+month", text, re.I):
        return _last_month()
    if re.search(r"本月|这个月|this\s+month", text, re.I):
        return {"start_date": date(today.year, today.month, 1), "end_date": yesterday}
    if re.search(r"今年|this\s+year", text, re.I):
        return {"start_date": date(today.year, 1, 1), "end_date": yesterday}

    month_match = re.search(r"(?:过去|近|最近|last\s+)(\d{1,2})\s*(?:个)?(?:月|months?|m\b)", text, re.I)
    if month_match:
        months = max(1, min(int(month_match.group(1)), 12))
        return {"days": months * 30}
    day_match = re.search(r"(?:过去|近|最近|last\s+)(\d{1,3})\s*(?:天|days?|d\b)", text, re.I)
    if day_match:
        days = max(7, min(int(day_match.group(1)), 365))
        return {"days": days}
    explicit_month = re.search(r"(?:(20\d{2})\s*年)?\s*(\d{1,2})\s*月(?:份)?", text)
    if explicit_month and not re.search(r"(?:过去|近|最近)\s*\d{1,2}\s*(?:个)?月", text):
        year = int(explicit_month.group(1) or today.year)
        month = int(explicit_month.group(2))
        if 1 <= month <= 12:
            start = date(year, month, 1)
            next_month = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
            end = min(next_month - timedelta(days=1), yesterday)
            return {"start_date": start, "end_date": end}
    if re.search(r"半年|6\s*(?:个)?月|six\s+months", text, re.I):
        return {"days": 180}
    if re.search(r"一年|12\s*(?:个)?月|one\s+year|year", text, re.I):
        return {"days": 365}
    return {"days": 60}


def _qs_extract_metric_trend_regions(query: str) -> list[str]:
    """解析用户问到的国家；没有国家时默认美区，避免多国查询过重。"""
    regions: list[str] = []
    for pattern, region in _QS_TREND_REGION_PATTERNS:
        if pattern.search(query):
            regions.append(region)
    out = _qs_dedupe_texts(regions, limit=6)
    return out or ["US"]


def _qs_metric_trend_granularity(query: str) -> str:
    text = str(query or "")
    if re.search(r"每天|每日|按天|日粒度|day|daily", text, re.I):
        return "day"
    if re.search(r"每周|按周|周数据|周粒度|week|weekly", text, re.I):
        return "week"
    return "week"


def _qs_metric_trend_metrics(query: str) -> list[str]:
    """识别用户明确要求的趋势指标；没有明确指标时默认给完整指标。"""
    text = str(query or "")
    metrics: list[str] = []
    if re.search(r"RPD|revenue\s*per\s*download", text, re.I):
        metrics.append("rpd")
    if re.search(r"收入|营收|流水|revenue|grossing", text, re.I):
        metrics.append("revenue")
    if re.search(r"下载|新增|安装|downloads?|installs?|新增量", text, re.I):
        metrics.append("downloads")
    if not metrics and re.search(r"数据|指标|表现|情况|trend|trends|趋势|走势", text, re.I):
        metrics = ["revenue", "downloads", "rpd"]
    return _qs_dedupe_texts(metrics, limit=3) or ["revenue"]


def _qs_metric_trend_labels(metrics: list[str] | None) -> list[str]:
    label_map = {"revenue": "收入", "downloads": "新增", "rpd": "RPD"}
    return [label_map.get(metric, metric) for metric in (metrics or []) if metric in label_map]


def _qs_metric_trend_labels_for_language(metrics: list[str] | None, language: Any = UI_LANGUAGE_ZH) -> list[str]:
    if not _is_ui_en(language):
        return _qs_metric_trend_labels(metrics)
    label_map = {"revenue": "revenue", "downloads": "new downloads", "rpd": "RPD"}
    return [label_map.get(metric, metric) for metric in (metrics or []) if metric in label_map]


def _qs_filter_trend_flags_for_metrics(flags: list[Any] | None, metrics: list[str]) -> list[str]:
    metric_set = set(metrics or [])
    kept: list[str] = []
    for flag in flags or []:
        text = str(flag or "").strip()
        if not text:
            continue
        if "收入" in text and "revenue" in metric_set:
            kept.append(text)
        elif "新增" in text and "downloads" in metric_set:
            kept.append(text)
    return kept


def _qs_is_timeseries_query(query: str) -> bool:
    return bool(re.search(r"趋势|走势|曲线|图表|折线|每周|按周|周(?:的)?数据|那几个周|每[一天日]|每日|按天|日(?:的)?数据|trend|trends|chart|weekly|daily", str(query or ""), re.I))


def _qs_clean_metric_app_name(value: str) -> str:
    """清理列表里的 App 名，尽量保留冒号、连字符和 &。"""
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    text = re.sub(r"^[\s\d.、,，;；:：-]+", "", text)
    text = re.sub(r"(?:这(?:几|两|\d+|一个|二个|三个|四个|五个|六个|七个|八个|九个|十个)?个?|这些|上述|以上)?\s*(?:APP|App|app|应用|产品)\s*$", "", text).strip()
    text = re.sub(r"(?:过去|近|最近|上个?月|本月|今年|last\b|每周|每星期|每[一天日]|每日|按周|按天|weekly|daily).*$", "", text, flags=re.I).strip()
    text = re.sub(r"(?:的)?(?:RPD|收入|营收|下载|新增|数据|表现|趋势|走势).*$", "", text, flags=re.I).strip()
    return text.strip(" '\"“”‘’")


def _qs_extract_metric_trend_apps(query: str) -> list[str]:
    """从用户输入中提取明确列出的多个 App 名。"""
    text = re.sub(r"\s+", " ", str(query or "")).strip()
    if not text:
        return []
    # 优先截取“这几个 App / 这些 App”之前的显式列表。
    prefix = re.split(r"(?:这(?:几|两|\d+|一个|二个|三个|四个|五个|六个|七个|八个|九个|十个)?个?|这些|上述|以上)\s*(?:APP|App|app|应用|产品)", text, maxsplit=1)[0]
    if prefix == text:
        prefix = re.split(
            r"(?:过去|近|最近|上个?月|本月|今年|last\b|每周|每星期|每[一天日]|每日|按周|按天|weekly|daily|在(?:美国|日本|英国|加拿大|澳大利亚|巴西|菲律宾|德国|法国|韩国|印度|印尼|墨西哥)|"
            r"(?:美国|美区|日本|日区|英国|英区|加拿大|澳大利亚|巴西|菲律宾|德国|法国|韩国|印度|印尼|墨西哥)地区|"
            r"RPD|收入|营收|下载|新增|数据|表现|是多少)",
            text,
            maxsplit=1,
            flags=re.I,
        )[0]
    if not re.search(r"[、,，;；\n]", prefix):
        return []
    parts = re.split(r"\s*[、,，;；\n]\s*", prefix)
    names = [
        _qs_clean_metric_app_name(part)
        for part in parts
    ]
    names = [
        name
        for name in names
        if len(name) >= 3 and re.search(r"[A-Za-z0-9]", name)
    ]
    return _qs_dedupe_texts(names, limit=10)


def _qs_extract_single_metric_trend_app(query: str) -> str:
    """从“App 名 + 时间/趋势词”里兜底截取单个 App 名。"""
    text = re.sub(r"\s+", " ", str(query or "")).strip()
    prefix = re.split(
        r"(?:(?:20\d{2}\s*年)?\s*\d{1,2}\s*月(?:份)?|过去|近|最近|上个?月|本月|今年|last\b|每周|每星期|每[一天日]|每日|按周|按天|"
        r"RPD|收入|营收|下载|新增|数据|表现|趋势|走势|trend|trends|weekly|daily)",
        text,
        maxsplit=1,
        flags=re.I,
    )[0]
    cleaned = _qs_clean_metric_app_name(prefix)
    if len(cleaned) >= 3 and re.search(r"[A-Za-z0-9]", cleaned):
        return cleaned[:80]
    return ""


def _qs_metric_trend_request(query: str) -> dict[str, Any] | None:
    """识别“多个指定 App + 国家/周期 + 收入/新增/RPD”查询。"""
    text = str(query or "")
    metric_hit = re.search(r"RPD|收入|营收|下载|新增|数据|download|downloads|revenue|trend|trends|表现|趋势|走势", text, re.I)
    if not metric_hit:
        return None
    apps = _qs_extract_metric_trend_apps(text)
    include_timeseries = _qs_is_timeseries_query(text)
    if len(apps) < 2 and include_timeseries:
        direct_app = st_direct_app_query_name(text)
        if not direct_app:
            direct_app = _qs_extract_single_metric_trend_app(text)
        if direct_app:
            apps = [direct_app]
    if len(apps) < 2 and not include_timeseries:
        return None
    if not apps:
        return None
    period = _qs_parse_metric_trend_period(text)
    granularity = _qs_metric_trend_granularity(text)
    metrics = _qs_metric_trend_metrics(text)
    if granularity == "week" and isinstance(period.get("start_date"), date) and isinstance(period.get("end_date"), date):
        start = period["start_date"]
        end = period["end_date"]
        period["start_date"] = start - timedelta(days=start.weekday())
        period["end_date"] = end + timedelta(days=6 - end.weekday())
    regions = _qs_extract_metric_trend_regions(text)
    return {
        "apps": apps,
        "regions": regions,
        "include_timeseries": include_timeseries,
        "granularity": granularity,
        "metrics": metrics,
        **period,
    }


_QS_APP_REVIEW_STOPWORDS = {
    "review", "reviews", "rating", "ratings", "comment", "comments", "feedback",
    "bad", "negative", "recent", "latest", "app", "apps", "store",
    "complaint", "complaints", "complain", "complains", "complaining",
    "issue", "issues", "problem", "problems", "about", "related",
    "privacy", "data", "permission", "permissions", "security", "trust",
    "subscription", "billing", "charge", "charges", "refund", "price", "pricing",
    "crash", "crashes", "bug", "bugs", "login", "account", "ads", "notification",
}

_QS_APP_REVIEW_COUNTRY_LABELS: dict[str, str] = {
    "US": "美国",
    "CN": "中国",
    "JP": "日本",
    "GB": "英国",
    "CA": "加拿大",
    "AU": "澳大利亚",
    "DE": "德国",
    "FR": "法国",
    "KR": "韩国",
    "IN": "印度",
    "BR": "巴西",
    "PH": "菲律宾",
    "MX": "墨西哥",
    "TW": "台湾",
    "HK": "香港",
    "SG": "新加坡",
    "IT": "意大利",
    "ES": "西班牙",
}

_QS_APP_REVIEW_COUNTRY_PATTERNS: tuple[tuple[re.Pattern, str], ...] = (
    (re.compile(r"美国|美区|\bUS\b|United States", re.I), "US"),
    (re.compile(r"中国|中区|国区|大陆|\bCN\b|China", re.I), "CN"),
    (re.compile(r"日本|日区|\bJP\b|Japan", re.I), "JP"),
    (re.compile(r"英国|英区|\bUK\b|\bGB\b|United Kingdom", re.I), "GB"),
    (re.compile(r"加拿大|加区|\bCA\b|Canada", re.I), "CA"),
    (re.compile(r"澳大利亚|澳洲|\bAU\b|Australia", re.I), "AU"),
    (re.compile(r"德国|德区|\bDE\b|Germany", re.I), "DE"),
    (re.compile(r"法国|法区|\bFR\b|France", re.I), "FR"),
    (re.compile(r"韩国|韩区|\bKR\b|Korea", re.I), "KR"),
    (re.compile(r"印度|\bIN\b|India", re.I), "IN"),
    (re.compile(r"巴西|巴区|\bBR\b|Brazil", re.I), "BR"),
    (re.compile(r"菲律宾|\bPH\b|Philippines", re.I), "PH"),
    (re.compile(r"墨西哥|\bMX\b|Mexico", re.I), "MX"),
    (re.compile(r"台湾|台区|\bTW\b|Taiwan", re.I), "TW"),
    (re.compile(r"香港|港区|\bHK\b|Hong Kong", re.I), "HK"),
    (re.compile(r"新加坡|\bSG\b|Singapore", re.I), "SG"),
    (re.compile(r"意大利|意区|\bIT\b|Italy", re.I), "IT"),
    (re.compile(r"西班牙|西区|\bES\b|Spain", re.I), "ES"),
)

_QS_APP_REVIEW_GLOBAL_PATTERN = re.compile(r"全球|全部国家|所有国家|全地区|所有地区|worldwide|global|all countries", re.I)


def _qs_extract_review_countries(query: str) -> list[str]:
    """识别评论查询里的显式国家；没有国家时返回空列表，代表全球口径。"""
    text = str(query or "")
    if not text or _QS_APP_REVIEW_GLOBAL_PATTERN.search(text):
        return []
    countries: list[str] = []
    for pattern, code in _QS_APP_REVIEW_COUNTRY_PATTERNS:
        if pattern.search(text):
            countries.append(code)
    return _qs_dedupe_texts(countries, limit=6)


def _qs_strip_review_country_terms(query: str) -> str:
    """从 App 名抽取文本里去掉国家词，避免把“美区/中国”等误当 App 名。"""
    text = str(query or "")
    text = _QS_APP_REVIEW_GLOBAL_PATTERN.sub(" ", text)
    for pattern, _code in _QS_APP_REVIEW_COUNTRY_PATTERNS:
        text = pattern.sub(" ", text)
    return re.sub(r"\s+", " ", text).strip()


def _qs_review_country_label(code: str) -> str:
    value = re.sub(r"[^A-Za-z]", "", str(code or "")).upper()
    return _QS_APP_REVIEW_COUNTRY_LABELS.get(value, value or "未知地区")


_QS_REVIEW_TOPIC_PATTERNS: tuple[tuple[str, str, re.Pattern], ...] = (
    ("pricing", "收费/订阅", re.compile(r"\$|€|£|subscription|subscribe|subscribed|monthly|annual|yearly|trial|paywall|premium|paid|payment|monetization|billing|charg(?:e|ed|es|ing)|refund|cancel(?:ed|ling)?|price|pricing|expensive|cost|money|credit\s*card|card information|free|收费|订阅|付费|价格|试用|扣费|退款|取消订阅|会员|高级版|信用卡|免费", re.I)),
    ("bugs", "崩溃/卡顿", re.compile(r"bug|bugs|crash(?:ed|es|ing)?|freeze|frozen|lag|slow|stuck|glitch|broken|not working|doesn.?t work|error|崩溃|闪退|卡顿|很慢|加载|报错|打不开|无响应|故障|问题", re.I)),
    ("account", "登录/账号", re.compile(r"log ?in|login|sign ?in|account|password|email|restore purchase|subscription not restored|登录|登陆|账号|账户|密码|邮箱|恢复购买|无法登录", re.I)),
    ("content_quality", "内容质量/准确性", re.compile(r"inaccurate|wrong|incorrect|mistake|misleading|translation|translate|content|verse|scripture|answer.*wrong|不准确|错误|误导|翻译|内容|经文|答案.*错|质量", re.I)),
    ("ai_quality", "AI 回答/理解", re.compile(r"\bAI\b|chatbot|bot|answer|response|hallucinat|understand|personalized|人工智能|智能|机器人|回答|回复|理解|幻觉|个性化", re.I)),
    ("ux", "操作体验/界面", re.compile(r"interface|ui|ux|design|navigation|hard to use|confusing|layout|screen|button|界面|操作|导航|设计|按钮|布局|难用|不好用|混乱", re.I)),
    ("missing_features", "功能缺失", re.compile(r"missing|wish|feature request|please add|should add|lack(?:ing)?|缺少|希望|建议增加|功能缺失|不能使用|无法使用", re.I)),
    ("support", "客服/退款", re.compile(r"support|customer service|contact|help desk|refund|no response|客服|售后|联系|没人回复|退款|支持团队", re.I)),
    ("notification", "通知/打扰", re.compile(r"notification|reminder|alarm|push|提醒|通知|推送|打扰", re.I)),
    ("privacy", "隐私/信任", re.compile(r"privacy|data|permission|trust|scam|fraud|安全|隐私|数据|权限|信任|诈骗|欺骗", re.I)),
    ("ads", "广告干扰", re.compile(r"\bads?\b|advertis|广告", re.I)),
    ("helpful", "有帮助/价值", re.compile(r"helpful|useful|love|amazing|great|excellent|valuable|changed my life|helps me|有帮助|很棒|喜欢|有价值|改变|受益", re.I)),
    ("easy_use", "易用/顺手", re.compile(r"easy to use|simple|intuitive|convenient|smooth|user friendly|容易|简单|直观|方便|顺手|流畅", re.I)),
    ("inspiration", "陪伴/激励", re.compile(r"inspir|motivat|encourag|comfort|peace|pray|prayer|devotional|faith|spiritual|激励|鼓励|安慰|平静|祷告|灵修|信仰|陪伴", re.I)),
)
_QS_NEGATIVE_REVIEW_TOPIC_KEYS = {
    "pricing", "bugs", "account", "content_quality", "ai_quality", "ux",
    "missing_features", "support", "notification", "privacy", "ads",
}
_QS_POSITIVE_REVIEW_TOPIC_KEYS = {
    "helpful", "easy_use", "inspiration", "content_quality", "ai_quality", "ux", "pricing",
}


def _qs_requested_review_topic(query: str) -> dict[str, str] | None:
    """从用户问题里识别想看的差评类型，例如隐私、收费、崩溃、广告。"""
    text = str(query or "")
    for key, label, pattern in _QS_REVIEW_TOPIC_PATTERNS:
        if key in _QS_NEGATIVE_REVIEW_TOPIC_KEYS and pattern.search(text):
            return {"key": key, "label": label}
    return None


def _qs_app_review_request(query: str) -> dict[str, Any] | None:
    """识别“某个 App 近期差评/评论”查询。"""
    text = str(query or "").strip()
    if not text:
        return None
    if re.search(r"reddit|社区|帖子|subreddit", text, re.I):
        return None
    review_hit = re.search(r"差评|负面评论|低分|吐槽|抱怨|不满|评论|评价|评分|好评|正面|高分|reviews?|ratings?|feedback|complaints?", text, re.I)
    if not review_hit:
        return None

    requested_topic = _qs_requested_review_topic(text)
    sentiment = "all"
    if re.search(r"差评|负面|低分|吐槽|抱怨|不满|bad|negative|unhappy|complain", text, re.I):
        sentiment = "negative"
    elif re.search(r"好评|正面|高分|positive|happy", text, re.I):
        sentiment = "positive"
    elif requested_topic:
        sentiment = "negative"

    days = 365
    day_match = re.search(r"(?:过去|近|最近|recent|last)\s*(\d{1,3})\s*(?:天|days?|d\b)", text, re.I)
    if day_match:
        days = max(1, min(int(day_match.group(1)), 365))
    elif re.search(r"一周|近7天|最近7天|week", text, re.I):
        days = 7
    elif re.search(r"半年|6\s*(?:个)?月", text, re.I):
        days = 180
    elif re.search(r"上个?月|近30天|最近30天|month", text, re.I):
        days = 30
    elif re.search(r"近期|最近|recent|latest", text, re.I):
        days = 30
    elif re.search(r"全部|所有|全量|all", text, re.I):
        days = 365

    countries = _qs_extract_review_countries(text)
    app_text = _qs_strip_review_country_terms(text)
    tokens = re.findall(r"[A-Za-z][A-Za-z0-9&+.'’:-]*", app_text)
    cleaned_tokens = [
        token.strip(" .,:;!?()[]{}")
        for token in tokens
        if token.lower().strip(" .,:;!?()[]{}") not in _QS_APP_REVIEW_STOPWORDS
    ]
    app_query = " ".join(cleaned_tokens[:8]).strip()
    if not app_query:
        # 中文 App 名暂时直接去掉常见评论意图词，保留剩余部分交给 ST autocomplete。
        app_query = re.sub(
            r"(近期|最近|过去|有什么|哪些|的|在|app\s*store|App\s*Store|差评|负面评论|低分|吐槽|抱怨|不满|评论|评价|评分|好评)",
            " ",
            app_text,
            flags=re.I,
        )
        if requested_topic:
            app_query = re.sub(
                r"(相关|隐私|安全|数据|权限|信任|诈骗|收费|订阅|付费|价格|试用|扣费|退款|崩溃|闪退|卡顿|登录|账号|广告|通知|客服|功能缺失)",
                " ",
                app_query,
                flags=re.I,
            )
        app_query = re.sub(r"\s+", " ", app_query).strip()
    if len(app_query) < 2:
        return None
    review_req = {
        "app_query": app_query[:100],
        "days": days,
        "sentiment": sentiment,
        "countries": countries,
        "country_scope": "specific" if countries else "global",
        "country_labels": [_qs_review_country_label(code) for code in countries] if countries else ["全部国家"],
    }
    if requested_topic:
        review_req["requested_review_topic_key"] = requested_topic["key"]
        review_req["requested_review_topic_label"] = requested_topic["label"]
    return review_req


def _qs_dedupe_app_reviews(reviews: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """按评论 id/内容去重，避免多国家合并时同一条评论重复展示。"""
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for review in reviews:
        if not isinstance(review, dict):
            continue
        key = "|".join([
            str(review.get("id") or "").strip(),
            str(review.get("country") or "").strip().upper(),
            str(review.get("created_at") or "").strip(),
            str(review.get("rating") or "").strip(),
            str(review.get("content") or "").strip()[:120],
        ])
        if key in seen:
            continue
        seen.add(key)
        out.append(review)
    return out


def _qs_merge_country_review_results(results: list[dict[str, Any]], review_req: dict[str, Any]) -> dict[str, Any]:
    """合并多个国家的评论查询结果，保留总量和分页口径。"""
    available_results = [item for item in results if item.get("available")]
    if not available_results:
        first_error = next((item.get("error") for item in results if item.get("error")), "")
        return {
            "available": False,
            "review_search": True,
            "queries": [review_req.get("app_query") or ""],
            "reviews": [],
            "error": first_error or "Apple App Store 评论查询失败，请检查本地配置",
        }

    countries = [str(code or "").upper() for code in (review_req.get("countries") or []) if str(code or "").strip()]
    merged_reviews: list[dict[str, Any]] = []
    for result in available_results:
        merged_reviews.extend([review for review in (result.get("reviews") or []) if isinstance(review, dict)])
    merged_reviews = _qs_dedupe_app_reviews(merged_reviews)
    merged_reviews.sort(key=lambda item: str(item.get("created_at") or ""), reverse=True)

    sentiment = str(available_results[0].get("sentiment_filter") or review_req.get("sentiment") or "negative").lower()
    negative_total = len([review for review in merged_reviews if _qs_review_matches_sentiment(review, "negative")])
    positive_total = len([review for review in merged_reviews if _qs_review_matches_sentiment(review, "positive")])
    selected_total = len([review for review in merged_reviews if _qs_review_matches_sentiment(review, sentiment)])
    raw_total = sum(int(item.get("raw_total") or len(item.get("reviews") or [])) for item in available_results)
    source_total = sum(int(item.get("source_total") or item.get("raw_total") or len(item.get("reviews") or [])) for item in available_results)
    max_raw_capacity = sum(int(item.get("max_raw_capacity") or item.get("source_total") or item.get("raw_total") or len(item.get("reviews") or [])) for item in available_results)
    fetched_pages = sum(int(item.get("fetched_pages") or 0) for item in available_results) or None
    page_count = sum(int(item.get("page_count") or 0) for item in available_results) or None
    base = available_results[0]

    return {
        **base,
        "available": True,
        "review_search": True,
        "queries": [review_req.get("app_query") or ""],
        "reviews": merged_reviews[:5000],
        "total": selected_total,
        "all_total": len(merged_reviews),
        "negative_total": negative_total,
        "positive_total": positive_total,
        "raw_total": raw_total,
        "source_total": source_total,
        "max_raw_capacity": max_raw_capacity,
        "fetched_pages": fetched_pages,
        "page_count": page_count,
        "countries": countries,
        "country_scope": "specific",
        "country_labels": [_qs_review_country_label(code) for code in countries],
        "country_results": [
            {
                "country": country,
                "label": _qs_review_country_label(country),
                "available": result.get("available"),
                "raw_total": result.get("raw_total"),
                "all_total": result.get("all_total"),
                "negative_total": result.get("negative_total"),
                "positive_total": result.get("positive_total"),
                "error": result.get("error"),
            }
            for country, result in zip(countries, results)
        ],
    }


def _qs_pick_review_translation_candidates(
    reviews: list[dict[str, Any]],
    *,
    sentiment: str,
) -> list[dict[str, Any]]:
    """优先翻译两端排序首屏：最新和最早，避免切换时间排序后首屏全是英文。"""
    sentiment = str(sentiment or "negative").lower()

    def _dedupe(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        seen: set[int] = set()
        out: list[dict[str, Any]] = []
        for item in items:
            marker = id(item)
            if marker in seen:
                continue
            seen.add(marker)
            out.append(item)
        return out

    def _two_ends(items: list[dict[str, Any]], each_side: int) -> list[dict[str, Any]]:
        if not items:
            return []
        return _dedupe(items[:each_side] + list(reversed(items[-each_side:])))

    if sentiment in {"negative", "positive"}:
        primary = [item for item in reviews if _qs_review_matches_sentiment(item, sentiment)]
        secondary = [item for item in reviews if not _qs_review_matches_sentiment(item, sentiment)]
        return _dedupe(
            _two_ends(primary, 6)
            + _two_ends(secondary, 2)
        )[:16]
    return _two_ends(reviews, 8)[:16]


def _qs_build_app_review_signal(review_req: dict[str, Any]) -> dict[str, Any]:
    """执行 Apple RSS App 评论查询，并整理成雷达搜索可展示的市场信号。"""
    countries = [
        str(code or "").upper()
        for code in (review_req.get("countries") or [])
        if str(code or "").strip()
    ]
    if countries:
        country_results = [
            st_fetch_app_reviews(
                str(review_req.get("app_query") or ""),
                days=int(review_req.get("days") or 30),
                limit=5000,
                sentiment=str(review_req.get("sentiment") or "negative"),
                country=country,
            )
            for country in countries
        ]
        result = _qs_merge_country_review_results(country_results, review_req)
    else:
        result = st_fetch_app_reviews(
            str(review_req.get("app_query") or ""),
            days=int(review_req.get("days") or 30),
            limit=5000,
            sentiment=str(review_req.get("sentiment") or "negative"),
            country="global",
        )
        if not result.get("available"):
            result = st_fetch_app_reviews(
                str(review_req.get("app_query") or ""),
                days=int(review_req.get("days") or 30),
                limit=5000,
                sentiment=str(review_req.get("sentiment") or "negative"),
                country="US",
            )
            if result.get("available"):
                countries = ["US"]
                result["global_review_fallback_country"] = "US"
                result["country_scope"] = "specific"
                result["country_labels"] = ["美国"]
        else:
            result["country_scope"] = "global"
            result["country_labels"] = ["全部国家"]
    if not result.get("available"):
        return {
            "available": False,
            "review_search": True,
            "queries": [review_req.get("app_query") or ""],
            "reviews": [],
            "requested_review_topic_key": review_req.get("requested_review_topic_key") or "",
            "requested_review_topic_label": review_req.get("requested_review_topic_label") or "",
            "error": result.get("error") or "Apple App Store 评论查询失败，请检查本地配置",
        }
    reviews = result.get("reviews") or []
    sentiment = str(result.get("sentiment_filter") or review_req.get("sentiment") or "negative").lower()
    translation_candidates = _qs_pick_review_translation_candidates(reviews, sentiment=sentiment)
    _qs_translate_app_reviews(translation_candidates, max_reviews=len(translation_candidates))
    _qs_annotate_review_topics(reviews)
    result["review_distribution"] = _qs_build_review_distribution(reviews)
    return {
        "available": True,
        "review_search": True,
        "queries": [review_req.get("app_query") or ""],
        "countries": countries,
        **result,
        "countries": countries,
        "country_scope": result.get("country_scope") or ("specific" if countries else "global"),
        "country_labels": result.get("country_labels") or ([_qs_review_country_label(code) for code in countries] if countries else ["全部国家"]),
        "requested_review_topic_key": review_req.get("requested_review_topic_key") or "",
        "requested_review_topic_label": review_req.get("requested_review_topic_label") or "",
    }


def _qs_review_rating(review: dict[str, Any]) -> int:
    try:
        rating = int(float(review.get("rating") or 0))
    except Exception:
        rating = 0
    return max(0, min(5, rating))


def _qs_review_matches_sentiment(review: dict[str, Any], sentiment: str) -> bool:
    rating = _qs_review_rating(review)
    if sentiment == "negative":
        return 1 <= rating <= 3
    if sentiment == "positive":
        return 4 <= rating <= 5
    return rating > 0


def _qs_review_text(review: dict[str, Any]) -> str:
    tags = review.get("tags") or []
    tag_text = " ".join(str(tag or "") for tag in tags) if isinstance(tags, list) else str(tags or "")
    parts = [
        review.get("title"),
        review.get("content"),
        review.get("title_zh"),
        review.get("content_zh"),
        tag_text,
    ]
    return " ".join(str(part or "") for part in parts).strip()


def _qs_review_topic_matches(review: dict[str, Any], sentiment: str) -> list[str]:
    """按当前好/差评口径返回评论命中的主题 key，顺序跟配置保持一致。"""
    text = _qs_review_text(review)
    if not text:
        return []
    allowed_keys = _QS_NEGATIVE_REVIEW_TOPIC_KEYS if sentiment == "negative" else _QS_POSITIVE_REVIEW_TOPIC_KEYS
    matched: list[str] = []
    for key, _label, pattern in _QS_REVIEW_TOPIC_PATTERNS:
        if key not in allowed_keys:
            continue
        if pattern.search(text):
            matched.append(key)
    return matched


def _qs_annotate_review_topics(reviews: list[dict[str, Any]]) -> None:
    """给每条差评补充类型 key/label，供前端二级筛选。"""
    labels = {key: label for key, label, _pattern in _QS_REVIEW_TOPIC_PATTERNS}
    for review in reviews:
        if not _qs_review_matches_sentiment(review, "negative"):
            review.pop("negative_topic_keys", None)
            review.pop("negative_topics", None)
            continue
        keys = _qs_review_topic_matches(review, "negative")
        review["negative_topic_keys"] = keys
        review["negative_topics"] = [labels.get(key, key) for key in keys]


def _qs_review_topic_distribution(reviews: list[dict[str, Any]], sentiment: str) -> dict[str, Any]:
    scoped = [review for review in reviews if _qs_review_matches_sentiment(review, sentiment)]
    total = len(scoped)
    if total <= 0:
        return {"total": 0, "items": [], "summary": "暂无足够评论可分析内容分布。"}

    counts: Counter[str] = Counter()
    labels = {key: label for key, label, _pattern in _QS_REVIEW_TOPIC_PATTERNS}
    matched_count = 0
    for review in scoped:
        matched_keys = set(_qs_review_topic_matches(review, sentiment))
        if matched_keys:
            matched_count += 1
        for key in matched_keys:
            counts[key] += 1

    items = [
        {
            "key": key,
            "label": labels.get(key, key),
            "count": count,
            "percent": round(count * 100 / total, 1),
        }
        for key, count in counts.most_common()
        if count > 0
    ]
    other_count = max(0, total - matched_count)
    if other_count:
        items.append({
            "key": "other",
            "label": "其他/未归类",
            "count": other_count,
            "percent": round(other_count * 100 / total, 1),
        })
    top = items[:3]
    prefix = "差评" if sentiment == "negative" else "好评"
    summary = f"{prefix}共 {total} 条，暂无明显主题集中。"
    if top:
        summary = f"{prefix}共 {total} 条，主要集中在" + "、".join(
            f"{item['label']} {item['percent']}%" for item in top
        ) + "。"
    return {"total": total, "items": items, "summary": summary}


def _qs_build_review_distribution(reviews: list[dict[str, Any]]) -> dict[str, Any]:
    """基于评论文本做轻量主题分布统计，避免把全量评论交给模型导致慢和幻觉。"""
    return {
        "negative": _qs_review_topic_distribution(reviews, "negative"),
        "positive": _qs_review_topic_distribution(reviews, "positive"),
        "method": "keyword_rules",
        "note": "一条评论可命中多个主题，所以各主题占比可能合计超过 100%。",
    }


def _qs_translate_app_reviews(reviews: list[dict[str, Any]], *, max_reviews: int = 30) -> None:
    """给 App Store 评论补充中文标题和正文翻译；失败时保留原文。"""
    if not reviews:
        return
    items: list[dict[str, str]] = []
    for index, review in enumerate(reviews[:max_reviews]):
        title = str(review.get("title") or "").strip()
        content = str(review.get("content") or "").strip()
        if title:
            items.append({"id": f"t{index}", "text": title[:180]})
        if content:
            items.append({"id": f"c{index}", "text": content[:700]})
    if not items:
        return
    for start in range(0, len(items), 12):
        batch = items[start:start + 12]
        prompt = (
            "将以下 App Store 用户评论翻译成自然中文，保留产品名、宗教/功能术语和用户情绪。"
            "只输出 JSON 对象，key 使用原 id，value 是中文译文。\n\n"
            + json.dumps(batch, ensure_ascii=False)
        )
        translated = _qs_llm_translate_json(prompt, max_tokens=3500)
        for item in batch:
            value = translated.get(item["id"])
            if not value:
                continue
            match = re.match(r"([tc])(\d+)$", item["id"])
            if not match:
                continue
            kind, index_text = match.groups()
            try:
                review = reviews[int(index_text)]
            except Exception:
                continue
            if kind == "t":
                review["title_zh"] = value
            else:
                review["content_zh"] = value


def _qs_build_metric_trends_signal(query: str, trend_req: dict[str, Any]) -> dict[str, Any]:
    """执行多个 App 的 ST 指标趋势查询，并整理成雷达搜索市场信号。"""
    metrics = [
        metric for metric in (trend_req.get("metrics") or ["revenue", "downloads", "rpd"])
        if metric in {"revenue", "downloads", "rpd"}
    ] or ["revenue"]
    st_status = st_check_available()
    if not st_status.get("available"):
        return {
            "available": False,
            "metric_trends": True,
            "metrics": metrics,
            "market_region": "、".join(trend_req.get("regions") or ["US"]),
            "metrics_region": "、".join(trend_req.get("regions") or ["US"]),
            "queries": trend_req.get("apps") or [],
            "top_apps": [],
            "table_rows": [],
            "error": _QS_SENSOR_TOWER_UNAVAILABLE_HINT,
        }
    apps = [{"name": name} for name in (trend_req.get("apps") or [])]
    result = st_fetch_apps_country_platform_trends(
        apps,
        regions=trend_req.get("regions") or ["US"],
        days=int(trend_req.get("days") or 60),
        start_date=trend_req.get("start_date"),
        end_date=trend_req.get("end_date"),
        comparison_start_date=trend_req.get("comparison_start_date"),
        comparison_end_date=trend_req.get("comparison_end_date"),
        limit=min(10, max(1, len(apps))),
    )
    if not result.get("available"):
        return {
            "available": False,
            "metric_trends": True,
            "metrics": metrics,
            "market_region": "、".join(trend_req.get("regions") or ["US"]),
            "metrics_region": "、".join(trend_req.get("regions") or ["US"]),
            "queries": trend_req.get("apps") or [],
            "top_apps": [],
            "table_rows": [],
            "error": result.get("error") or _QS_SENSOR_TOWER_UNAVAILABLE_HINT,
        }

    time_series: dict[str, Any] | None = None
    if trend_req.get("include_timeseries"):
        time_series = st_fetch_apps_revenue_download_timeseries(
            apps,
            regions=trend_req.get("regions") or ["US"],
            days=int(trend_req.get("days") or 60),
            start_date=trend_req.get("start_date"),
            end_date=trend_req.get("end_date"),
            granularity=str(trend_req.get("granularity") or "week"),
            metrics=metrics,
            limit=min(10, max(1, len(apps))),
        )

    rows = []
    for row in result.get("table_rows") or []:
        if not isinstance(row, dict):
            continue
        next_row = dict(row)
        next_row["flags"] = _qs_filter_trend_flags_for_metrics(next_row.get("flags") or [], metrics)
        rows.append(next_row)
    all_rows = [row for row in rows if row.get("platform") == "all"]
    highlights = []
    for item in result.get("highlights") or []:
        flag = str((item or {}).get("flag") or "")
        if _qs_filter_trend_flags_for_metrics([flag], metrics):
            highlights.append(item)
    top_apps = [
        {
            "name": row.get("app"),
            "publisher": row.get("publisher") or "",
            "revenue": row.get("revenue"),
            "revenue_display": row.get("revenue_display"),
            "downloads": row.get("downloads"),
            "downloads_display": row.get("downloads_display"),
            "growth_pct": row.get("revenue_growth_pct"),
            "downloads_growth_pct": row.get("downloads_growth_pct"),
            "app_store_url": row.get("app_store_url") or "",
            "sensor_tower_url": row.get("sensor_tower_url") or "",
        }
        for row in all_rows
    ]
    regions = result.get("regions") or trend_req.get("regions") or ["US"]
    return {
        "available": True,
        "metric_trends": True,
        "metrics": metrics,
        "market_region": "、".join(regions),
        "candidate_region": "、".join(regions),
        "metrics_region": "、".join(regions),
        "metrics_time_period": str(trend_req.get("days") or result.get("date_range", {}).get("label") or ""),
        "sort_by": "metric_trend",
        "queries": trend_req.get("apps") or [],
        "date_range": result.get("date_range") or {},
        "comparison_range": result.get("comparison_range") or {},
        "regions": regions,
        "items": result.get("items") or [],
        "table_rows": rows,
        "highlights": highlights,
        "top_apps": top_apps,
        **({"time_series": time_series} if time_series else {}),
    }


def _qs_filter_relevant_posts_fast(posts: list[dict], topic: str) -> list[dict]:
    """用英文关键词快速过滤明显跑题结果，避免快速搜索卡在 LLM 相关性判断。"""
    posts = [post for post in posts if not _qs_is_political_or_current_affairs(post)]
    if len(posts) <= 3:
        return posts
    raw_terms = {
        t
        for t in re.findall(r"[a-z][a-z0-9-]{2,}", str(topic or "").lower())
        if t not in _QS_FAST_RELEVANCE_STOPWORDS and (len(t) >= 4 or t in {"gut", "ibs", "adhd", "ocd", "ptsd", "app", "ai"})
    }
    terms = {t for t in raw_terms if t not in _QS_FAST_RELEVANCE_GENERIC_TERMS}
    if not terms:
        terms = raw_terms
    if not terms:
        return posts
    target_subs = {
        s.lower()
        for s in re.findall(r"r/([a-zA-Z0-9_]+)", str(topic or ""))
    }
    kept: list[dict] = []
    for post in posts:
        source_sub = _qs_source_subreddit(post)
        target_sub = source_sub in target_subs
        if not target_sub and (
            source_sub in _QS_FAST_RELEVANCE_NOISE_SUBS
            or any(hint in source_sub for hint in _QS_FAST_RELEVANCE_NOISE_SOURCE_HINTS)
        ):
            continue
        haystack = " ".join([
            str(post.get("title") or ""),
            str(post.get("content") or ""),
            str(post.get("source") or ""),
        ]).lower()
        tokens = _qs_word_tokens(haystack)
        hits = terms & tokens
        hit_count = len(hits)
        source_hit = any(term in source_sub or source_sub in term for term in terms)
        domain_source = any(hint in source_sub for hint in _QS_FAST_RELEVANCE_DOMAIN_SOURCE_HINTS)
        if target_sub and (hit_count >= 1 or source_hit):
            kept.append(post)
        elif hit_count >= 2:
            kept.append(post)
        elif domain_source and hit_count >= 1:
            kept.append(post)
    if not kept:
        print("[QuickSearch] fast relevance kept 0, stop instead of showing off-topic posts")
        return []
    if len(kept) < max(5, len(posts) // 4):
        print(f"[QuickSearch] fast relevance kept few ({len(kept)}), use precise subset")
        return kept
    print(f"[QuickSearch] fast relevance {len(posts)} -> {len(kept)}")
    return kept


_QS_FEEDBACK_DISCOVERY_RE = re.compile(
    r"feedback|feature request|customer request|user request|user interview|support ticket|"
    r"customer insight|voice of customer|roadmap|需求|反馈|用户意见|功能请求|客户声音",
    re.I,
)
_QS_FEEDBACK_WORKFLOW_RE = re.compile(
    r"prioriti[sz]|rank|triage|consolidat|collect|categor|cluster|dedup|track|manage|"
    r"backlog|notify|close the loop|spreadsheet|notion|排序|优先级|归并|归类|去重|"
    r"收集|管理|跟踪|闭环|表格",
    re.I,
)


def _qs_apply_intent_guard(posts: list[dict], query: str) -> list[dict]:
    """Require both sides of feedback-workflow queries to appear in evidence.

    This deterministic last pass prevents generic product complaints from
    filling a result set merely because an LLM considered them adjacent.
    Other query types retain their existing relevance pipeline.
    """
    query_text = str(query or "")
    if not (_QS_FEEDBACK_DISCOVERY_RE.search(query_text) and _QS_FEEDBACK_WORKFLOW_RE.search(query_text)):
        return posts
    kept: list[dict] = []
    for post in posts:
        comments = post.get("comments") or []
        comment_text = " ".join(
            str(item.get("body") or item.get("text") or "") if isinstance(item, dict) else str(item)
            for item in comments[:8]
        )
        evidence_text = " ".join([
            str(post.get("title") or ""),
            str(post.get("content") or ""),
            comment_text,
        ])
        if _QS_FEEDBACK_DISCOVERY_RE.search(evidence_text) and _QS_FEEDBACK_WORKFLOW_RE.search(evidence_text):
            kept.append(post)
    print(f"[QuickSearch] intent guard {len(posts)} -> {len(kept)}")
    return kept


def _qs_comment_heat_threshold(min_score: int) -> int:
    """把赞数门槛折算成评论共鸣门槛，避免低赞高讨论的求助帖被误删。"""
    if min_score <= 0:
        return 0
    if min_score <= 10:
        return 5
    return max(8, min_score // 2)


def _qs_post_meets_heat(post: dict, min_score: int) -> bool:
    """社区热度允许赞数或评论数任一达标。"""
    if min_score <= 0:
        return True
    try:
        score = int(post.get("score") or 0)
    except (TypeError, ValueError):
        score = 0
    try:
        comments = int(post.get("num_comments") or 0)
    except (TypeError, ValueError):
        comments = 0
    return score >= min_score or comments >= _qs_comment_heat_threshold(min_score)


def _qs_post_rank_score(post: dict) -> float:
    """综合产品机会、赞数和评论量排序，评论量代表问题是否有共鸣。"""
    try:
        score = float(post.get("score") or 0)
    except (TypeError, ValueError):
        score = 0.0
    try:
        comments = float(post.get("num_comments") or 0)
    except (TypeError, ValueError):
        comments = 0.0
    try:
        opportunity = float(post.get("opportunity_score") or 0)
    except (TypeError, ValueError):
        opportunity = 0.0
    signal_counts = post.get("signal_counts") if isinstance(post.get("signal_counts"), dict) else {}
    signal_bonus = 0.0
    for key, weight in (
        ("pain", 16.0),
        ("workaround", 14.0),
        ("switching", 14.0),
        ("payment", 18.0),
        ("trust", 8.0),
        ("software", 8.0),
        ("comment", 5.0),
    ):
        try:
            signal_bonus += min(3, int(signal_counts.get(key) or 0)) * weight
        except (TypeError, ValueError):
            continue
    return score + comments * 0.65 + opportunity * 55 + signal_bonus


_QS_LOW_VALUE_POST_PATTERNS = re.compile(
    r"\b(news|breaking|announcement|press release|rumou?r|drama|meme|shitpost|"
    r"satire|politics|election|war|government|celebrity|gossip)\b|"
    r"新闻|公告|发布会|八卦|梗图|时政|政治|战争",
    re.I,
)


def _qs_apply_business_signal_filter(posts: list[dict]) -> list[dict]:
    """用产品机会信号参与快速搜索排序；候选充足时剔除明显低价值材料。"""
    if not posts:
        return posts
    annotate_posts_with_opportunity(posts)
    if len(posts) < 8:
        return posts

    high_signal: list[dict] = []
    low_value: list[dict] = []
    for post in posts:
        text = " ".join([
            str(post.get("title") or ""),
            str(post.get("content") or "")[:260],
            str(post.get("source") or ""),
        ])
        try:
            opportunity = float(post.get("opportunity_score") or 0)
        except (TypeError, ValueError):
            opportunity = 0.0
        signal_counts = post.get("signal_counts") if isinstance(post.get("signal_counts"), dict) else {}
        has_business_signal = False
        for key in ("pain", "workaround", "switching", "payment", "trust", "software"):
            try:
                if int(signal_counts.get(key) or 0) > 0:
                    has_business_signal = True
                    break
            except (TypeError, ValueError):
                continue
        if _QS_LOW_VALUE_POST_PATTERNS.search(text) and not has_business_signal:
            low_value.append(post)
            continue
        if opportunity >= 2.45 or has_business_signal:
            high_signal.append(post)

    if len(high_signal) >= 4:
        print(f"[QuickSearch] business signal filter {len(posts)} -> {len(high_signal)} (low_value={len(low_value)})")
        return high_signal
    return posts


def _qs_posts_for_client(posts: list[dict]) -> list[dict[str, Any]]:
    """把内部 Reddit 帖子转换成快速搜索前端需要的轻量结构。"""
    return [
        {
            "title": p.get("title", ""),
            "title_zh": p.get("title_zh", ""),
            "content": (p.get("content") or "")[:500],
            "content_zh": p.get("content_zh", ""),
            "url": p.get("url", ""),
            "score": p.get("score", 0),
            "num_comments": p.get("num_comments", 0),
            "source": p.get("source", ""),
            "created_utc": p.get("created_utc", 0),
            "process_dimensions": p.get("_process_dimensions") or [],
            "process_actions": p.get("_process_actions") or [],
            "process_scopes": p.get("_process_scopes") or [],
            "comments": [
                _qs_norm_comment(c)
                for c in (p.get("comments") or [])[:5]
            ],
        }
        for p in posts
    ]


def _qs_build_topic_overview(posts: list[dict], query: str) -> str:
    """归纳主题分布，供总结 prompt 使用。"""
    if not posts:
        return "（无帖子）"
    lines = []
    for i, p in enumerate(posts[:40]):
        title = p.get("title_zh") or p.get("title", "")
        lines.append(f"{i}: [{p.get('score', 0)}赞] {title}")
    prompt = QUICK_SEARCH_TOPIC_PROMPT.format(
        query=query,
        titles_list="\n".join(lines),
    )
    try:
        resp = call_llm(
            [{"role": "system", "content": "只输出 JSON。"}, {"role": "user", "content": prompt}],
            max_tokens=1200,
        )
        parsed = _parse_json_from_text(resp)
        if not parsed or not isinstance(parsed, dict):
            return "（主题归纳失败）"
        parts = [f"突出主题：{parsed.get('dominant_theme', '未知')}"]
        themes = parsed.get("themes", [])
        if isinstance(themes, list):
            for th in themes[:6]:
                if not isinstance(th, dict):
                    continue
                name = th.get("name", "")
                indices = th.get("post_indices", [])
                n = len(indices) if isinstance(indices, list) else 0
                signal = th.get("signal", "")
                if name:
                    parts.append(f"- {name}（约 {n} 帖）：{signal}")
        note = parsed.get("coverage_note", "")
        if note:
            parts.append(f"覆盖说明：{note}")
        return "\n".join(parts)
    except Exception as e:
        print(f"[QuickSearch] topic overview failed: {e}")
        return "（主题归纳失败）"


def _qs_build_topic_overview_fast(posts: list[dict], query: str, language: Any = UI_LANGUAGE_ZH) -> str:
    """用非 LLM 方式生成快速主题概览，避免社区搜索在聚类阶段卡住。"""
    if not posts:
        return _ui_text(language, "（无帖子）", "(No posts)")
    source_counts: dict[str, int] = {}
    for post in posts:
        source = str(post.get("source") or "reddit/unknown")
        source_counts[source] = source_counts.get(source, 0) + 1
    top_sources = sorted(source_counts.items(), key=lambda item: item[1], reverse=True)[:5]
    if _is_ui_en(language):
        top_titles = [
            f"[{p.get('score', 0)} upvotes/{p.get('num_comments', 0)} comments] {p.get('title', '')}"
            for p in posts[:5]
        ]
        lines = [
            f"Quick overview: kept {len(posts)} high-signal posts around \"{query}\".",
            "Main sources: " + ", ".join(f"{source.replace('reddit/', 'r/')} x{count}" for source, count in top_sources),
            "High-heat evidence: " + "; ".join(top_titles),
        ]
    else:
        top_titles = [
            f"[{p.get('score', 0)}赞/{p.get('num_comments', 0)}评] {p.get('title_zh') or p.get('title', '')}"
            for p in posts[:5]
        ]
        lines = [
            f"快速概览：围绕「{query}」保留 {len(posts)} 条高信号帖子。",
            "主要来源：" + "、".join(f"{source.replace('reddit/', 'r/')}×{count}" for source, count in top_sources),
            "高热证据：" + "；".join(top_titles),
        ]
    return "\n".join(lines)


def _qs_fetch_market_signal(
    query: str,
    plan: dict,
    reddit_queries: list[str],
    *,
    metrics_time_period: str = "30d",
    progress_callback=None,
) -> dict[str, Any]:
    """查询 SensorTower 市场信号；失败时返回可展示的弱错误，不中断 Reddit 搜索。"""
    plan_meta = {
        "planning_source": str((plan or {}).get("planning_source") or ""),
        "planning_confidence": (plan or {}).get("planning_confidence"),
        "planning_issue": str((plan or {}).get("planning_issue") or ""),
    }
    st_status = st_check_available()
    if not st_status.get("available"):
        return {
            "available": False,
            "candidate_region": "US",
            "metrics_region": "全球",
            "metrics_time_period": metrics_time_period,
            "market_region": "全球",
            "queries": [],
            "top_apps": [],
            "error": _QS_SENSOR_TOWER_UNAVAILABLE_HINT,
            **plan_meta,
        }
    if str((plan or {}).get("market_intent") or "").lower() == "direct_app":
        direct_queries = plan.get("market_queries") or []
        if isinstance(direct_queries, str):
            direct_queries = [direct_queries]
        market_queries = _qs_dedupe_texts([str(q) for q in direct_queries if str(q).strip()], limit=3)
    else:
        market_queries = _qs_market_search_queries(plan, query, reddit_queries)
    if not market_queries:
        return {
            "available": False,
            "candidate_region": "US",
            "metrics_region": "全球",
            "metrics_time_period": metrics_time_period,
            "market_region": "全球",
            "queries": [],
            "top_apps": [],
            "error": "没有生成适合 SensorTower 的 App 搜索词",
            **plan_meta,
        }
    try:
        result = search_market_apps(
            query,
            search_queries=market_queries,
            top_n=24,
            result_limit=8,
            market_region="US",
            sort_by=_qs_market_sort_by(query, plan),
            metrics_time_period=metrics_time_period,
            progress_callback=progress_callback,
        )
        if not result:
            return {
                "available": False,
                "candidate_region": "US",
                "metrics_region": "全球",
                "metrics_time_period": metrics_time_period,
                "market_region": "全球",
                "queries": market_queries,
                "top_apps": [],
                "error": "SensorTower 未匹配到相关 App",
                **plan_meta,
            }
        result.update({k: v for k, v in plan_meta.items() if v not in ("", None)})
        return result
    except Exception as e:
        print(f"[QuickSearch] SensorTower search failed: {e}")
        return {
            "available": False,
            "candidate_region": "US",
            "metrics_region": "全球",
            "metrics_time_period": metrics_time_period,
            "market_region": "全球",
            "queries": market_queries,
            "top_apps": [],
            "error": _QS_SENSOR_TOWER_UNAVAILABLE_HINT,
            **plan_meta,
        }


def _qs_format_market_context(market_signal: dict[str, Any] | None, language: Any = UI_LANGUAGE_ZH) -> str:
    """把 SensorTower 结果压缩为总结 prompt 可用的市场上下文。"""
    if not market_signal:
        return _ui_text(language, "（未查询 SensorTower）", "(Sensor Tower was not queried)")
    if not market_signal.get("available"):
        return _ui_text(
            language,
            f"（SensorTower 暂无可用市场信号：{market_signal.get('error', '未知原因')}）",
            f"(No usable Sensor Tower market signal: {market_signal.get('error', 'unknown reason')})",
        )
    apps = market_signal.get("top_apps") or []
    if not apps:
        return _ui_text(language, "（SensorTower 未匹配到相关 App）", "(Sensor Tower did not match related Apps)")
    candidate_region = market_signal.get("candidate_region") or "US"
    metrics_region = market_signal.get("metrics_region") or market_signal.get("market_region") or "全球"
    date_range = market_signal.get("date_range") or {}
    date_label = date_range.get("label") or ""
    sort_label_map = (
        {
            "growth": "sorted by revenue growth",
            "downloads": "sorted by downloads",
            "revenue": "sorted by revenue scale",
            "scale": "sorted by overall scale",
        }
        if _is_ui_en(language)
        else {
            "growth": "按收入增长排序",
            "downloads": "按下载量排序",
            "revenue": "按收入规模排序",
            "scale": "按综合规模排序",
        }
    )
    sort_label = sort_label_map.get(str(market_signal.get("sort_by") or "revenue"), _ui_text(language, "按收入规模排序", "sorted by revenue scale"))
    if _is_ui_en(language):
        lines = [f"Candidate competitors: {candidate_region}; revenue/downloads: {metrics_region}; window: {date_label or 'previous full calendar month'}; sorting: {sort_label}"]
    else:
        lines = [f"候选竞品：{candidate_region}；收入/下载：{metrics_region}；口径：{date_label or '上一个完整自然月'}；排序：{sort_label}"]
    queries = market_signal.get("queries") or []
    if queries:
        lines.append(_ui_text(language, "查询词：", "Queries: ") + (_ui_text(language, "、", ", ")).join(str(q) for q in queries[:6]))
    for i, app in enumerate(apps[:8], start=1):
        name = app.get("name") or "Unknown"
        publisher = app.get("publisher") or ""
        revenue = app.get("revenue_display") or "-"
        downloads = app.get("downloads_display") or "-"
        growth = app.get("growth_pct")
        if _is_ui_en(language):
            growth_text = f", revenue growth {growth}%" if isinstance(growth, (int, float)) else ""
            lines.append(f"{i}. {name}{f' ({publisher})' if publisher else ''}: revenue {revenue}, downloads {downloads}{growth_text}")
        else:
            growth_text = f"，收入增长 {growth}%" if isinstance(growth, (int, float)) else ""
            lines.append(f"{i}. {name}{f'（{publisher}）' if publisher else ''}：收入 {revenue}，下载 {downloads}{growth_text}")
    return "\n".join(lines)


def _qs_build_market_summary(query: str, market_signal: dict[str, Any] | None, language: Any = UI_LANGUAGE_ZH) -> str:
    """不依赖 LLM 的竞品搜索摘要，避免纯市场查询被 GPT 连接状态卡住。"""
    if _is_ui_en(language):
        if not market_signal or not market_signal.get("available"):
            error = (market_signal or {}).get("error") or "Sensor Tower did not return usable results"
            return (
                "## Conclusion\n"
                f"No stable Sensor Tower competitor signal was retrieved yet: {error}.\n\n"
                "## Data Limitations\n"
                "Candidate competitors are searched primarily in the US App market, while revenue/download metrics use a global scope. If the category or product name is ambiguous, try adding an English App name or category term."
            )
        if market_signal.get("review_search"):
            app = market_signal.get("app") or {}
            reviews = market_signal.get("reviews") or []
            date_range = market_signal.get("date_range") or {}
            date_text = f"{date_range.get('start', '')} ~ {date_range.get('end', '')}".strip(" ~") or date_range.get("label") or "recent period"
            source = market_signal.get("source") or "App Store"
            negative_total = market_signal.get("negative_total")
            positive_total = market_signal.get("positive_total")
            return (
                "## Conclusion\n"
                f"Queried **{app.get('name') or (market_signal.get('queries') or ['target App'])[0]}** {source} reviews for {date_text}. "
                f"Fetched {market_signal.get('raw_total') or len(reviews)} raw reviews, including {negative_total if negative_total is not None else '-'} negative and {positive_total if positive_total is not None else '-'} positive reviews.\n\n"
                "## Data Scope\n"
                "- Negative reviews are 1-3 stars; positive reviews are 4-5 stars.\n"
                "- The structured review list and filters are shown in the result panel above."
            )
        if market_signal.get("metric_trends"):
            metrics = [
                metric for metric in (market_signal.get("metrics") or ["revenue", "downloads", "rpd"])
                if metric in {"revenue", "downloads", "rpd"}
            ] or ["revenue"]
            regions = ", ".join(str(r) for r in (market_signal.get("regions") or [])) or market_signal.get("metrics_region") or "US"
            return (
                "## Conclusion\n"
                f"Queried {', '.join(_qs_metric_trend_labels_for_language(metrics, language))} trends for {len(market_signal.get('queries') or [])} App(s) in {regions}.\n\n"
                "## Data Scope\n"
                "- Sensor Tower estimated data is shown in the structured trend table and charts above.\n"
                "- Trend changes compare the current period with the previous period of the same length."
            )
        apps = market_signal.get("top_apps") or []
        if not apps:
            return (
                "## Conclusion\n"
                "Sensor Tower did not match stable related competitors yet.\n\n"
                "## Data Limitations\n"
                "Try a clearer English App name, product type, or category term."
            )
        leader = apps[0]
        return (
            "## Conclusion\n"
            f"The most prominent related product currently matched is **{leader.get('name') or 'the leading product'}**, with revenue around {leader.get('revenue_display') or '-'} and downloads around {leader.get('downloads_display') or '-'}.\n\n"
            "## Data Scope\n"
            "This reflects App-market competitor and commercialization signals, not Reddit discussion heat."
        )
    if not market_signal or not market_signal.get("available"):
        error = (market_signal or {}).get("error") or "Sensor Tower 未返回可用结果"
        return (
            "## 结论\n"
            f"暂时没有拿到稳定的 Sensor Tower 竞品信号：{error}。\n\n"
            "## 数据局限\n"
            "当前候选竞品优先按美区 App 市场检索，收入/下载按全球口径拉取；如果赛道关键词较中文化或产品不在 App Store / Google Play，可能需要补充英文竞品名。"
        )

    if market_signal.get("review_search"):
        app = market_signal.get("app") or {}
        reviews = market_signal.get("reviews") or []
        date_range = market_signal.get("date_range") or {}
        date_text = f"{date_range.get('start', '')} ~ {date_range.get('end', '')}".strip(" ~") or date_range.get("label") or "近期"
        source = market_signal.get("source") or "App Store"
        sentiment = str(market_signal.get("sentiment_filter") or "negative")
        sentiment_label = {"negative": "差评", "positive": "好评", "all": "评论"}.get(sentiment, "评论")
        requested_topic_key = str(market_signal.get("requested_review_topic_key") or "").strip()
        requested_topic_label = str(market_signal.get("requested_review_topic_label") or "").strip()
        countries = [str(code or "").upper() for code in (market_signal.get("countries") or []) if str(code or "").strip()]
        country_text = "、".join(_qs_review_country_label(code) for code in countries) if countries else "全部国家"
        selected_reviews = [review for review in reviews if _qs_review_matches_sentiment(review, sentiment)]
        if requested_topic_key and sentiment == "negative":
            selected_reviews = [
                review for review in selected_reviews
                if requested_topic_key in (review.get("negative_topic_keys") or [])
            ]
        negative_total = market_signal.get("negative_total")
        positive_total = market_signal.get("positive_total")
        all_total = market_signal.get("all_total") or len(reviews)
        distribution = market_signal.get("review_distribution") or {}
        negative_dist = (distribution.get("negative") or {}) if isinstance(distribution, dict) else {}
        positive_dist = (distribution.get("positive") or {}) if isinstance(distribution, dict) else {}

        def _table_cell(value: Any) -> str:
            text = str(value if value is not None and value != "" else "-")
            return text.replace("|", "\\|").replace("\n", " ").strip() or "-"

        lines = [
            "## 结论",
            f"已查询 **{app.get('name') or (market_signal.get('queries') or ['目标 App'])[0]}** 在 {source} 的{country_text} {date_text}评论，抓取 {market_signal.get('raw_total') or len(reviews)} 条原始评论，差评 {negative_total if negative_total is not None else '-'} 条，好评 {positive_total if positive_total is not None else '-'} 条，当前可筛选查看 {all_total} 条。{f'本次默认展示{requested_topic_label}相关差评。' if requested_topic_label else ''}",
            "",
            "## 内容分布",
            f"- {negative_dist.get('summary') or '差评暂无足够内容分布。'}",
            f"- {positive_dist.get('summary') or '好评暂无足够内容分布。'}",
            "",
            "## 用户评论",
            "| 评分 | 日期 | 标题 | 中文内容 | 原文 |",
            "| ---: | --- | --- | --- | --- |",
        ]
        display_reviews = selected_reviews if requested_topic_key else (selected_reviews or reviews)
        for review in display_reviews[:12]:
            content_zh = review.get("content_zh") or review.get("content") or "-"
            title = review.get("title_zh") or review.get("title") or "-"
            lines.append(
                "| "
                + " | ".join([
                    _table_cell(review.get("rating")),
                    _table_cell(str(review.get("created_at") or "")[:10]),
                    _table_cell(title),
                    _table_cell(content_zh),
                    _table_cell(review.get("content")),
                ])
                + " |"
            )
        if market_signal.get("fallback") == "sensor_tower":
            capacity = market_signal.get("source_total") or market_signal.get("max_raw_capacity") or market_signal.get("raw_total") or len(reviews)
            fetched_pages = market_signal.get("fetched_pages") or "-"
            page_count = market_signal.get("page_count") or "-"
            rss_state = "没有返回评论" if market_signal.get("apple_rss_empty") else "返回不完整"
            fetch_note = f"- Apple RSS 本次{rss_state}，已通过补充抓取通道获取 App Store 评论；当前窗口可返回约 {capacity} 条，已抓取 {fetched_pages}/{page_count} 页。"
        else:
            fetch_note = "- Apple RSS 默认最多抓取 10 页、每页 50 条；如 App Store RSS 分区返回不完整，结果可能低于全量评论。"
        lines.extend([
            "",
            "## 数据口径",
            f"- 查询来源：{source} 评论",
            f"- 国家/地区：{country_text}",
            f"- 时间：{date_text}",
            f"- 默认筛选：{sentiment_label}",
            *( [f"- 默认差评类型：{requested_topic_label}"] if requested_topic_label else [] ),
            "- 差评定义为 1-3 星，好评定义为 4-5 星。",
            fetch_note,
        ])
        return "\n".join(lines)

    if market_signal.get("metric_trends"):
        rows = market_signal.get("table_rows") or []
        all_rows = [row for row in rows if row.get("platform") == "all"] or rows
        date_range = market_signal.get("date_range") or {}
        comparison_range = market_signal.get("comparison_range") or {}
        date_text = f"{date_range.get('start', '')} ~ {date_range.get('end', '')}".strip(" ~") or date_range.get("label") or "当前周期"
        comparison_text = f"{comparison_range.get('start', '')} ~ {comparison_range.get('end', '')}".strip(" ~") or comparison_range.get("label") or "对比周期"
        regions = "、".join(str(r) for r in (market_signal.get("regions") or [])) or market_signal.get("metrics_region") or "US"
        highlights = market_signal.get("highlights") or []
        metrics = [
            metric for metric in (market_signal.get("metrics") or ["revenue", "downloads", "rpd"])
            if metric in {"revenue", "downloads", "rpd"}
        ] or ["revenue"]
        metric_text = "、".join(_qs_metric_trend_labels(metrics))

        def _table_cell(value: Any) -> str:
            text = str(value if value is not None and value != "" else "-")
            return text.replace("|", "\\|").replace("\n", " ").strip() or "-"

        if highlights:
            lead = highlights[0]
            conclusion = (
                f"已查询 {len(market_signal.get('queries') or [])} 个 App 在 {regions} 的指标趋势；"
                f"其中 **{lead.get('app') or '某个 App'}** 出现「{lead.get('flag') or '显著变化'}」。"
            )
        else:
            conclusion = f"已查询 {len(market_signal.get('queries') or [])} 个 App 在 {regions} 的{metric_text}趋势，暂未发现达到阈值的大幅变化。"

        table_headers = ["产品", "国家", "平台"]
        if "revenue" in metrics:
            table_headers.extend(["当前收入", "收入变化"])
        if "downloads" in metrics:
            table_headers.extend(["当前新增", "新增变化"])
        if "rpd" in metrics:
            table_headers.append("RPD")
        if "revenue" in metrics or "downloads" in metrics:
            table_headers.append("趋势")

        lines = [
            "## 结论",
            conclusion,
            "",
            "## 指标趋势表",
            "| " + " | ".join(table_headers) + " |",
            "| " + " | ".join(["---"] * len(table_headers)) + " |",
        ]
        for row in all_rows[:30]:
            revenue_growth = row.get("revenue_growth_pct")
            downloads_growth = row.get("downloads_growth_pct")
            revenue_growth_text = f"{revenue_growth:+.1f}%" if isinstance(revenue_growth, (int, float)) else "-"
            downloads_growth_text = f"{downloads_growth:+.1f}%" if isinstance(downloads_growth, (int, float)) else "-"
            flags = "、".join(str(flag) for flag in (row.get("flags") or [])) or "-"
            row_cells = [
                f"**{_table_cell(row.get('app'))}**",
                _table_cell(row.get("region")),
                _table_cell(row.get("platform")),
            ]
            if "revenue" in metrics:
                row_cells.extend([_table_cell(row.get("revenue_display")), _table_cell(revenue_growth_text)])
            if "downloads" in metrics:
                row_cells.extend([_table_cell(row.get("downloads_display")), _table_cell(downloads_growth_text)])
            if "rpd" in metrics:
                row_cells.append(_table_cell(row.get("rpd_display") or row.get("rpd_60d_display")))
            if "revenue" in metrics or "downloads" in metrics:
                row_cells.append(_table_cell(flags))
            lines.append(
                "| "
                + " | ".join(row_cells)
                + " |"
            )
        lines.extend([
            "",
            "## 数据口径",
            f"- 查询来源：Sensor Tower",
            f"- 当前周期：{date_text}",
            f"- 对比周期：{comparison_text}",
            f"- 区域：{regions}",
            f"- 本次展示指标：{metric_text}",
            *(['- 新增 = downloads；RPD = 当前周期收入 / 当前周期新增下载'] if "rpd" in metrics else []),
            "- 趋势标记阈值：变化率 >= 30%，且收入变化 >= $10K 或新增变化 >= 10K",
        ])
        return "\n".join(lines)

    apps = market_signal.get("top_apps") or []
    sort_by = str(market_signal.get("sort_by") or "revenue")
    sort_label = {"growth": "收入增长", "downloads": "下载量", "revenue": "收入规模", "scale": "综合规模"}.get(sort_by, "收入规模")
    candidate_region = market_signal.get("candidate_region") or "US"
    metrics_region = market_signal.get("metrics_region") or market_signal.get("market_region") or "全球"
    date_range = market_signal.get("date_range") or {}
    date_text = f"{date_range.get('start', '')} ~ {date_range.get('end', '')}".strip(" ~") or date_range.get("label") or "上一个完整自然月"

    if not apps:
        return (
            "## 结论\n"
            "Sensor Tower 暂未匹配到相关竞品。\n\n"
            "## 数据局限\n"
            f"已按候选竞品 {candidate_region}、收入/下载 {metrics_region}、{date_text} 口径查询，但当前关键词可能过宽或过窄。"
        )

    if market_signal.get("direct_app_competitors"):
        target = market_signal.get("target_app") if isinstance(market_signal.get("target_app"), dict) else None
        target = target or next((app for app in apps if app.get("is_target_app")), None)
        peers = [app for app in apps if not app.get("is_target_app")]
        target_name = (target or {}).get("name") or "目标 App"
        if peers:
            lead = peers[0]
            lead_text = f"其中最值得先对照的是 **{lead.get('name') or '首位竞品'}**，收入约 {lead.get('revenue_display') or '-'}，下载约 {lead.get('downloads_display') or '-'}。"
        else:
            lead_text = "暂时没有拿到足够稳定的同类竞品，只能先展示目标 App 自身数据。"

        def _table_cell(value: Any) -> str:
            text = str(value if value is not None and value != "" else "-")
            text = text.replace("|", "\\|").replace("\n", " ")
            return text.strip() or "-"

        lines = [
            "## 结论",
            f"已先定位 **{target_name}**，再按直接相关性和 {metrics_region} 收入/下载规模筛选同类 App；{lead_text}",
            "",
            "## 目标 App 与相关竞品",
            "| 类型 | 产品 | 发行商 | 收入 | 下载 | 场景匹配 |",
            "| --- | --- | --- | ---: | ---: | --- |",
        ]
        for app in apps[:8]:
            row_type = "目标 App" if app.get("is_target_app") else "竞品"
            matched = " / ".join(str(q) for q in (app.get("matched_queries") or [])[:2]) or "-"
            lines.append(
                "| "
                + " | ".join([
                    _table_cell(row_type),
                    f"**{_table_cell(app.get('name') or 'Unknown')}**",
                    _table_cell(app.get("publisher") or ""),
                    _table_cell(app.get("revenue_display") or "-"),
                    _table_cell(app.get("downloads_display") or "-"),
                    _table_cell(matched),
                ])
                + " |"
            )
        queries = market_signal.get("queries") or []
        lines.extend([
            "",
            "## 数据口径",
            f"- 查询来源：Sensor Tower",
            f"- 候选竞品区域：{candidate_region}",
            f"- 收入/下载区域：{metrics_region}",
            f"- 时间：{date_text}",
            "- 排序：先直接相关性，再看收入/下载规模",
        ])
        if queries:
            lines.append(f"- 查询词：{'、'.join(str(q) for q in queries[:8])}")
        lines.extend([
            "",
            "## 数据局限",
            "App 锚点竞品依赖 Sensor Tower 搜索召回和名称匹配；同名 App 或跨品类大盘产品仍需要人工核对。",
        ])
        return "\n".join(lines)

    if market_signal.get("direct_app"):
        app = apps[0]
        name = app.get("name") or "目标 App"
        publisher = app.get("publisher") or ""
        revenue = app.get("revenue_display") or "-"
        downloads = app.get("downloads_display") or "-"
        dau = app.get("dau_display") or "-"
        return (
            "## 结论\n"
            f"按 Sensor Tower {metrics_region} 口径，**{name}**{f'（{publisher}）' if publisher else ''}在 {date_text} 的收入约 **{revenue}**，下载约 **{downloads}**。\n\n"
            "## 数据明细\n"
            "| 产品 | 发行商 | 收入 | 下载 | MAU |\n"
            "| --- | --- | ---: | ---: | ---: |\n"
            f"| **{name}** | {publisher or '-'} | {revenue} | {downloads} | {dau} |\n\n"
            "## 数据局限\n"
            "该数据来自 Sensor Tower 估算，收入和下载按全球口径展示；如果同名 App 较多，需要用完整 App 名或 App Store 链接复查。"
        )

    leader = apps[0]
    leader_name = leader.get("name") or "首位产品"
    leader_revenue = leader.get("revenue_display") or "-"
    leader_downloads = leader.get("downloads_display") or "-"
    leader_growth = leader.get("growth_pct")
    growth_text = f"，收入增长 {leader_growth}%" if isinstance(leader_growth, (int, float)) else ""

    def _table_cell(value: Any) -> str:
        text = str(value if value is not None and value != "" else "-")
        text = text.replace("|", "\\|").replace("\n", " ")
        return text.strip() or "-"

    lines = [
        "## 结论",
        f"按 Sensor Tower 候选竞品 {candidate_region}、收入/下载 {metrics_region}、{date_text} 的 {sort_label} 口径，当前最突出的相关产品是 **{leader_name}**，收入约 {leader_revenue}，下载约 {leader_downloads}{growth_text}。",
        "",
        "## 相关竞品",
        "| 排名 | 产品 | 发行商 | 收入 | 下载 | 收入增长 | DAU |",
        "| ---: | --- | --- | ---: | ---: | ---: | ---: |",
    ]
    for index, app in enumerate(apps[:8], start=1):
        name = app.get("name") or "Unknown"
        publisher = app.get("publisher") or ""
        revenue = app.get("revenue_display") or "-"
        downloads = app.get("downloads_display") or "-"
        growth = app.get("growth_pct")
        growth_text = f"{growth:+.1f}%" if isinstance(growth, (int, float)) else "-"
        dau = app.get("dau_display") or "-"
        lines.append(
            "| "
            + " | ".join([
                _table_cell(index),
                f"**{_table_cell(name)}**",
                _table_cell(publisher),
                _table_cell(revenue),
                _table_cell(downloads),
                _table_cell(growth_text),
                _table_cell(dau),
            ])
            + " |"
        )

    queries = market_signal.get("queries") or []
    lines.extend([
        "",
        "## 数据口径",
        f"- 查询来源：Sensor Tower",
        f"- 候选竞品区域：{candidate_region}",
        f"- 收入/下载区域：{metrics_region}",
        f"- 时间：{date_text}",
        f"- 排序：{sort_label}",
    ])
    if queries:
        lines.append(f"- 查询词：{'、'.join(str(q) for q in queries[:8])}")
    lines.extend([
        "",
        "## 数据局限",
        "这代表 App 市场里的竞品和商业化信号，不代表社区讨论热度；如果要看用户为什么需要这类产品，需要再切到社区讨论策略。",
    ])
    return "\n".join(lines)


@router.get("/quick-search/history")
def quick_search_history(request: Request):
    """读取当前 session 的雷达搜索历史。"""
    _ensure_quick_search_enabled()
    ctx = _get_session(request)
    return {"items": _qs_read_history(ctx)}


@router.post("/quick-search/history")
def save_quick_search_history(req: QuickSearchHistorySaveRequest, request: Request):
    """保存当前 session 的雷达搜索历史，作为前端 localStorage 的后端兜底。"""
    _ensure_quick_search_enabled()
    ctx = _get_session(request)
    items = _qs_sanitize_history_items(req.items)
    with ctx.lock:
        _safe_json_write(_qs_history_file(ctx), {"items": items}, indent=2)
    return {"ok": True, "items": items}


@router.post("/quick-search/reviews/translate")
def translate_quick_search_reviews(req: QuickSearchReviewTranslateRequest, request: Request):
    """按需翻译当前可见的 App Store 评论，避免初次搜索阻塞全量翻译。"""
    _ensure_quick_search_enabled()
    ctx = _get_session(request)
    set_thread_session(ctx)
    try:
        cleaned: list[dict[str, Any]] = []
        for raw in (req.reviews or [])[:20]:
            if not isinstance(raw, dict):
                continue
            title = str(raw.get("title") or "")[:220].strip()
            content = str(raw.get("content") or "")[:900].strip()
            if not title and not content:
                continue
            cleaned.append({
                "id": str(raw.get("id") or "")[:600],
                "title": title,
                "content": content,
            })
        if not cleaned:
            return {"ok": True, "reviews": []}
        _qs_translate_app_reviews(cleaned, max_reviews=len(cleaned))
        return {
            "ok": True,
            "reviews": [
                {
                    "id": item.get("id") or "",
                    "title_zh": item.get("title_zh") or "",
                    "content_zh": item.get("content_zh") or "",
                }
                for item in cleaned
            ],
        }
    finally:
        clear_thread_session()


@router.post("/quick-search")
def quick_search(req: QuickSearchRequest, request: Request):
    """快速搜索：输入方向 → rdt 搜索 → 帖子列表 + AI 总结（SSE）"""
    _ensure_quick_search_enabled()
    ctx = _get_session(request)
    ui_language = _normalize_ui_language(req.language)

    def _generate() -> Generator[str, None, None]:
        import asyncio
        set_thread_session(ctx)
        loop = None
        ui_en = _is_ui_en(ui_language)

        def _t(zh: str, en: str) -> str:
            return en if ui_en else zh

        try:
            gate = _qs_query_gate(req.query, req.strategy)
            if not gate["ok"]:
                yield _sse("error", {
                    "message": _qs_gate_message_for_language(gate, ui_language),
                    "placement": "composer",
                    "kind": gate.get("status", "blocked"),
                })
                return

            def _fetch_market_signal_with_progress(
                plan: dict,
                reddit_queries: list[str],
                *,
                start: int,
                cap: int,
                message: str,
            ):
                import concurrent.futures
                import queue as _queue

                stage_events: _queue.Queue[dict[str, Any]] = _queue.Queue()

                def _push_stage(event: dict[str, Any]):
                    stage_events.put(event)

                def _stage_progress(event: dict[str, Any]) -> tuple[int, str]:
                    span = max(1, cap - start)
                    phase = str(event.get("phase") or "")
                    if phase == "autocomplete":
                        total = max(1, int(event.get("total") or 1))
                        current = max(1, min(total, int(event.get("current") or 1)))
                        progress_value = start + int(span * (0.08 + 0.54 * current / total))
                        return min(cap, progress_value), _t("正在匹配 Sensor Tower 查询词...", "Matching Sensor Tower query terms...")
                    if phase == "direct_app":
                        app_query = str(event.get("query") or "").strip()
                        progress_msg = (
                            _t(f"正在查询 {app_query} 的收入/下载数据...", f"Querying revenue/download data for {app_query}...")
                            if app_query
                            else _t("正在查询单个 App 的收入/下载数据...", "Querying revenue/download data for a single App...")
                        )
                        return min(cap, start + int(span * 0.54)), progress_msg
                    if phase == "facets":
                        return min(cap, start + int(span * 0.74)), _t("正在拉取候选 App 的收入/下载数据...", "Fetching candidate App revenue/download data...")
                    if phase == "entities":
                        return min(cap, start + int(span * 0.88)), _t("正在补全 App 名称和图标...", "Completing App names and icons...")
                    if phase == "done":
                        return cap, _t("Sensor Tower 数据返回，正在整理...", "Sensor Tower data returned. Organizing results...")
                    return start, message

                with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                    future = executor.submit(
                        _qs_fetch_market_signal,
                        req.query,
                        plan,
                        reddit_queries,
                        metrics_time_period=req.market_time_period,
                        progress_callback=_push_stage,
                    )
                    started_at = _time.time()
                    last_progress = start
                    last_message = message
                    phase_cap = start + max(1, int((cap - start) * 0.12))
                    tick = 0
                    while not future.done():
                        saw_stage_event = False
                        while True:
                            try:
                                event = stage_events.get_nowait()
                            except _queue.Empty:
                                break
                            saw_stage_event = True
                            next_progress, next_message = _stage_progress(event)
                            if str(event.get("phase") or "") == "autocomplete":
                                phase_cap = min(
                                    start + int((cap - start) * 0.64),
                                    next_progress + max(1, int((cap - start) * 0.04)),
                                )
                            elif str(event.get("phase") or "") == "facets":
                                phase_cap = min(
                                    start + int((cap - start) * 0.82),
                                    next_progress + max(1, int((cap - start) * 0.08)),
                                )
                            elif str(event.get("phase") or "") == "entities":
                                phase_cap = min(
                                    start + int((cap - start) * 0.94),
                                    next_progress + max(1, int((cap - start) * 0.08)),
                                )
                            else:
                                phase_cap = cap
                            last_progress = max(last_progress, min(cap, next_progress))
                            last_message = next_message or last_message
                            yield _sse("qs_progress", {
                                "message": last_message,
                                "progress": last_progress,
                            })
                        if not saw_stage_event:
                            elapsed = _time.time() - started_at
                            # 没有新阶段事件时只做轻微推进，避免长期停住，同时不越过当前真实阶段上限。
                            smooth = min(
                                phase_cap,
                                max(
                                    last_progress,
                                    start + int((phase_cap - start) * (1 - pow(0.90, max(1, tick + int(elapsed // 4))))),
                                ),
                            )
                            if smooth > last_progress or tick == 0:
                                last_progress = smooth
                                yield _sse("qs_progress", {
                                    "message": last_message,
                                    "progress": last_progress,
                                })
                        tick += 1
                        _time.sleep(0.8)
                    while True:
                        try:
                            event = stage_events.get_nowait()
                        except _queue.Empty:
                            break
                        next_progress, next_message = _stage_progress(event)
                        last_progress = max(last_progress, min(cap, next_progress))
                        last_message = next_message or last_message
                        yield _sse("qs_progress", {
                            "message": last_message,
                            "progress": last_progress,
                        })
                    return future.result()

            def _fetch_metric_trends_with_progress(
                trend_req: dict[str, Any],
                *,
                start: int,
                cap: int,
            ):
                import concurrent.futures

                with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                    future = executor.submit(_qs_build_metric_trends_signal, req.query, trend_req)
                    started_at = _time.time()
                    last_progress = start
                    metric_text = (
                        ("、" if not ui_en else ", ").join(_qs_metric_trend_labels_for_language(trend_req.get("metrics") or [], ui_language))
                        or _t("指标", "metrics")
                    )
                    messages = [
                        _t("正在匹配 App 列表...", "Matching App list..."),
                        _t("正在解析国家和时间周期...", "Parsing countries and time range..."),
                        _t(f"正在拉取 Sensor Tower {metric_text}趋势...", f"Fetching Sensor Tower {metric_text} trends..."),
                        _t(f"正在标记{metric_text}变化趋势...", f"Flagging {metric_text} trend changes..."),
                    ]
                    tick = 0
                    while not future.done():
                        elapsed = _time.time() - started_at
                        phase = min(len(messages) - 1, int(elapsed // 5))
                        target = min(
                            cap - 1,
                            start + int((cap - start) * (1 - pow(0.88, max(1, tick + int(elapsed // 3))))),
                        )
                        if target > last_progress or tick == 0:
                            last_progress = target
                            yield _sse("qs_progress", {
                                "message": messages[phase],
                                "progress": last_progress,
                            })
                        tick += 1
                        _time.sleep(0.8)
                    return future.result()

            def _fetch_review_signal_with_progress(
                review_req: dict[str, Any],
                *,
                start: int,
                cap: int,
            ):
                import concurrent.futures

                with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                    future = executor.submit(_qs_build_app_review_signal, review_req)
                    started_at = _time.time()
                    last_progress = start
                    app_query = str(review_req.get("app_query") or "").strip()
                    messages = [
                        _t(f"正在定位 {app_query} 的 App Store 应用...", f"Locating {app_query} on the App Store...") if app_query else _t("正在定位 App Store 应用...", "Locating the App Store app..."),
                        _t("正在拉取可读取的评论分页...", "Fetching readable review pages..."),
                        _t("正在统计好评、差评和国家分布...", "Counting positive/negative reviews and country distribution..."),
                        _t("正在补充首屏评论翻译...", "Preparing first-screen review translations..."),
                    ]
                    tick = 0
                    while not future.done():
                        elapsed = _time.time() - started_at
                        phase = min(len(messages) - 1, int(elapsed // 8))
                        target = min(
                            cap - 1,
                            start + int((cap - start) * (1 - pow(0.90, max(1, tick + int(elapsed // 4))))),
                        )
                        if target > last_progress or tick == 0:
                            last_progress = target
                            yield _sse("qs_progress", {
                                "message": messages[phase],
                                "progress": last_progress,
                            })
                        tick += 1
                        _time.sleep(0.8)
                    return future.result()

            strategy = {
                "mode": gate.get("strategy") or _qs_detect_strategy(req.query, req.strategy)["mode"],
                "reason": gate.get("reason") or _t("AI 已完成搜索意图判断", "AI search intent check completed"),
            }
            review_req = _qs_app_review_request(req.query)
            if review_req:
                app_query = str(review_req.get("app_query") or "").strip()
                yield _sse("qs_progress", {
                    "message": _t(f"识别为 App 评论查询：{app_query}", f"Detected App review query: {app_query}"),
                    "progress": 8,
                    "plan": {
                        "queries": [app_query],
                        "subreddits": [],
                        "reasoning": _t("用户询问某个 App 的近期评论/差评，直接调用 Apple RSS App Store 评论接口。", "The user is asking about recent reviews or negative reviews for an App, so Lumon calls the Apple RSS App Store review endpoint directly."),
                    },
                })
                yield _sse("qs_progress", {"message": _t("正在查询 Apple RSS App Store 近期评论...", "Querying recent Apple RSS App Store reviews..."), "progress": 24})
                market_signal = yield from _fetch_review_signal_with_progress(
                    review_req,
                    start=24,
                    cap=82,
                )
                yield _sse("qs_market", market_signal)
                yield _sse("qs_posts", {"posts": [], "total": 0, "total_searched": 0})
                yield _sse("qs_progress", {"message": _t("评论查询完成，正在整理结果...", "Review query finished. Organizing results..."), "progress": 88})
                yield _sse("qs_summary_chunk", {"text": _qs_build_market_summary(req.query, market_signal, ui_language)})
                yield _sse("qs_progress", {"message": _t("完成", "Done"), "progress": 100})
                yield _sse("done", {})
                return
            if strategy["mode"] == "competitor":
                yield _sse("qs_progress", {
                    "message": _t(f"识别为竞品/市场问题：{strategy['reason']}", f"Detected competitor/market question: {strategy['reason']}"),
                    "progress": 6,
                })
                trend_req = _qs_metric_trend_request(req.query)
                if trend_req:
                    apps = trend_req.get("apps") or []
                    regions = trend_req.get("regions") or ["US"]
                    period_label = (
                        _t(f"{trend_req.get('days')} 天", f"{trend_req.get('days')} days")
                        if trend_req.get("days")
                        else _t("自定义周期", "custom period")
                    )
                    trend_label = _t("指定 App 指标趋势查询", "single-App metric trend query") if len(apps) <= 1 else _t("多 App 指标趋势查询", "multi-App metric trend query")
                    yield _sse("qs_progress", {
                        "message": _t(
                            f"识别为{trend_label}：{len(apps)} 个 App，{ '、'.join(regions) }，{period_label}",
                            f"Detected {trend_label}: {len(apps)} App(s), {', '.join(regions)}, {period_label}",
                        ),
                        "progress": 14,
                        "plan": {
                            "queries": apps,
                            "subreddits": [],
                            "reasoning": _t("用户列出了多个 App，并询问 RPD、收入或新增，直接调用 Sensor Tower 指标趋势查询。", "The user listed App names and asked for RPD, revenue, or new downloads, so Lumon calls Sensor Tower metric trend data directly."),
                        },
                    })
                    yield _sse("qs_progress", {"message": _t("正在查询 Sensor Tower 指标趋势，通常需要 30-120 秒...", "Querying Sensor Tower metric trends. This usually takes 30-120 seconds..."), "progress": 24})
                    market_signal = yield from _fetch_metric_trends_with_progress(
                        trend_req,
                        start=24,
                        cap=86,
                    )
                    yield _sse("qs_market", market_signal)
                    yield _sse("qs_posts", {"posts": [], "total": 0, "total_searched": 0})
                    yield _sse("qs_progress", {"message": _t("指标趋势查询完成，正在整理结果...", "Metric trend query finished. Organizing results..."), "progress": 90})
                    yield _sse("qs_summary_chunk", {"text": _qs_build_market_summary(req.query, market_signal, ui_language)})
                    yield _sse("qs_progress", {"message": _t("完成", "Done"), "progress": 100})
                    yield _sse("done", {})
                    return

                app_competitor_query = st_app_competitor_query_name(req.query)
                direct_app_query = st_direct_app_query_name(req.query)
                if app_competitor_query:
                    yield _sse("qs_progress", {
                        "message": _t(f"识别为 App 竞品查询：{app_competitor_query}", f"Detected App competitor query: {app_competitor_query}"),
                        "progress": 14,
                    })
                    market_plan = {
                        "market_queries": [app_competitor_query],
                        "known_competitors": [app_competitor_query],
                        "market_intent": "app_competitors",
                        "market_reasoning": _t("识别为某个 App 的竞品查询，将先定位目标 App，再扩展同类竞品。", "Detected an App competitor query. Lumon will locate the target App first, then expand to similar competitors."),
                    }
                elif direct_app_query:
                    yield _sse("qs_progress", {
                        "message": _t(f"识别为单个 App 指标查询：{direct_app_query}", f"Detected single-App metric query: {direct_app_query}"),
                        "progress": 14,
                    })
                    market_plan = {
                        "market_queries": [direct_app_query],
                        "market_intent": "direct_app",
                        "market_reasoning": _t("识别为单个 App 的收入/下载查询，直接调用 Sensor Tower 单产品接口。", "Detected a single-App revenue/download query, so Lumon calls the Sensor Tower single-product endpoint directly."),
                    }
                else:
                    yield _sse("qs_progress", {"message": _t("正在规划 Sensor Tower 搜索词...", "Planning Sensor Tower search terms..."), "progress": 14})
                    market_plan = _qs_market_plan(req.query)
                market_ok, market_issue, market_queries = _qs_validate_market_plan_for_search(
                    market_plan,
                    req.query,
                    [req.query],
                )
                if not market_ok:
                    yield _sse("error", {
                        "message": _t(
                            f"{market_issue}。请把问题改成更明确的赛道、产品类型或 App 名称后重试。",
                            "Market search planning was not stable enough. Please retry with a clearer category, product type, or App name.",
                        ),
                        "placement": "composer",
                        "kind": "planning_failed",
                    })
                    return
                market_plan["market_queries"] = market_queries
                plan_reason = market_plan.get("market_reasoning") or strategy["reason"]
                market_intent = str(market_plan.get("market_intent") or "").lower()
                strategy_message = (
                    _t("App 锚点策略就绪：先定位目标 App，再自动扩展同类竞品查询词", "App anchor strategy ready: locate the target App first, then expand similar competitor queries")
                    if market_intent == "app_competitors"
                    else _t(f"市场搜索策略就绪：{len(market_queries)} 个查询词", f"Market search strategy ready: {len(market_queries)} query terms")
                )
                yield _sse("qs_progress", {
                    "message": strategy_message,
                    "progress": 22,
                    "plan": {
                        "queries": market_queries,
                        "subreddits": [],
                        "reasoning": plan_reason,
                    },
                })
                yield _sse("qs_progress", {"message": _t("正在查询 Sensor Tower 竞品信号，通常需要 30-90 秒...", "Querying Sensor Tower competitor signals. This usually takes 30-90 seconds..."), "progress": 28})
                market_signal = yield from _fetch_market_signal_with_progress(
                    market_plan,
                    [req.query],
                    start=28,
                    cap=82,
                    message=_t("正在查询 Sensor Tower 竞品信号...", "Querying Sensor Tower competitor signals..."),
                )
                yield _sse("qs_market", market_signal)
                yield _sse("qs_posts", {"posts": [], "total": 0, "total_searched": 0})
                yield _sse("qs_progress", {"message": _t("竞品搜索完成，正在整理结果...", "Competitor search finished. Organizing results..."), "progress": 88})
                yield _sse("qs_summary_chunk", {"text": _qs_build_market_summary(req.query, market_signal, ui_language)})
                yield _sse("qs_progress", {"message": _t("完成", "Done"), "progress": 100})
                yield _sse("done", {})
                return

            yield _sse("qs_progress", {"message": _t("正在规划搜索策略...", "Planning search strategy..."), "progress": 5})

            # 1) LLM 规划搜索词
            research_type = _qs_research_type(req.query, gate)
            intent_summary = str(gate.get("intent_summary") or gate.get("topic") or req.query).strip()
            requested_dimensions = gate.get("requested_dimensions") or []
            plan_messages = [
                {"role": "system", "content": "你是搜索规划助手，输出 JSON。"},
                {"role": "user", "content": QUICK_SEARCH_PLANNING_PROMPT.format(
                    query=req.query,
                    research_type=research_type,
                    intent_summary=intent_summary,
                    requested_dimensions=", ".join(str(item) for item in requested_dimensions) or "not specified",
                )},
            ]
            try:
                plan_raw = call_llm(plan_messages, max_tokens=1500)
                plan = _parse_json_from_text(plan_raw)
                if not isinstance(plan, dict):
                    raise ValueError("plan is not a dict")
                plan_ok, plan_issue = _qs_validate_community_plan(plan, req.query, research_type)
                if not plan_ok:
                    raise ValueError(f"unsafe or unusable plan: {plan_issue}")
            except Exception as e:
                print(f"[QuickSearch] community plan failed, stop search: {e}")
                yield _sse("error", {
                    "message": _qs_planning_error_message(e, ui_language),
                    "placement": "composer",
                    "kind": "planning_failed",
                })
                return

            queries = _qs_flatten_plan_queries(plan, req.query)
            raw_subs = plan.get("subreddits", [])
            if isinstance(raw_subs, str):
                raw_subs = [raw_subs]
            raw_subs = raw_subs[:10] if isinstance(raw_subs, list) else []
            subreddits = _qs_sanitize_subreddits(raw_subs, req.query)
            subreddits = _qs_prioritize_process_subreddits(subreddits, req.query, research_type)
            reasoning = plan.get("reasoning", "")
            topic_anchor = plan.get("topic_anchor", "")
            if isinstance(topic_anchor, str) and topic_anchor.strip():
                reasoning = (
                    f"{reasoning}{_t('（焦点：', ' (focus: ')}{topic_anchor.strip()}{_t('）', ')')}"
                    if reasoning
                    else f"{_t('焦点：', 'Focus: ')}{topic_anchor.strip()}"
                )
            if strategy.get("reason"):
                reasoning = f"{reasoning}；{strategy['reason']}" if reasoning else strategy["reason"]
            filter_parts = [req.query]
            if topic_anchor:
                filter_parts.append(str(topic_anchor))
            if queries:
                filter_parts.append(_t("英文搜索词：", "English search terms: ") + " / ".join(queries[:8]))
            if subreddits:
                filter_parts.append(_t("目标社区：", "Target communities: ") + " / ".join(f"r/{s}" for s in subreddits[:6]))
            filter_topic = (_t("。", ". ")).join(part for part in filter_parts if str(part).strip())

            yield _sse("qs_progress", {
                "message": _t(
                    f"搜索策略就绪：{len(queries)} 个搜索词，{len(subreddits)} 个社区",
                    f"Search strategy ready: {len(queries)} query terms, {len(subreddits)} communities",
                ),
                "progress": 15,
                "plan": {"queries": queries, "subreddits": subreddits, "reasoning": reasoning},
            })

            # 2) rdt-cli 并发搜索
            fetcher = get_reddit_fetcher()
            if fetcher and fetcher.engine_name == "unknown":
                yield _sse("qs_progress", {"message": _t("正在连接 Reddit 本地引擎...", "Connecting to the local Reddit engine..."), "progress": 18})
                init_loop = asyncio.new_event_loop()
                try:
                    init_loop.run_until_complete(init_reddit_fetcher())
                finally:
                    init_loop.close()
                fetcher = get_reddit_fetcher()
            if not fetcher or fetcher.engine_name == "none":
                yield _sse("error", {"message": _t("Reddit 本地引擎暂不可用，请检查本地配置", "The local Reddit engine is unavailable. Please check your local settings.")})
                return

            time_map = {"week": "week", "month": "month", "3months": "year", "6months": "year"}
            rdt_time = time_map.get(req.time_period, "year")

            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            all_posts: list[dict] = []
            seen_ids: set[str] = set()

            yield _sse("qs_progress", {"message": _t("正在搜索 Reddit：先抓全局相关结果...", "Searching Reddit: collecting global relevant results first..."), "progress": 20})

            jobs: list[tuple[str, str, int, str]] = []
            global_limit = max(6, min(9, req.limit // max(1, len(queries))))
            subreddit_limit = max(5, min(7, req.limit // 9))
            for q in queries[:6]:
                jobs.append((q, "", global_limit, "relevance"))
            for q in queries[:2]:
                jobs.append((q, "", max(4, global_limit // 2), "top"))
            for q in queries[:4]:
                for sub in subreddits[:3]:
                    jobs.append((q, sub, subreddit_limit, "relevance"))
            if re.search(r"最多|近期|最近|趋势|热议|讨论|hot|trending|recent|most discussed", req.query, re.I):
                for sub in subreddits[:4]:
                    jobs.append(("", sub, 6, "top"))

            async def _run_search_batch(batch_jobs: list[tuple[str, str, int, str]]):
                coros = [
                    fetcher.search(
                        query=q,
                        subreddit=sub,
                        sort=sort,
                        time_filter=rdt_time,
                        limit=lim,
                    )
                    for q, sub, lim, sort in batch_jobs
                ]
                return await asyncio.gather(*coros, return_exceptions=True)

            chunk_size = 8
            total_batches = max(1, (len(jobs) + chunk_size - 1) // chunk_size)
            for batch_index, i in enumerate(range(0, len(jobs), chunk_size), start=1):
                batch_jobs = jobs[i:i + chunk_size]
                batch_progress = min(42, 22 + int(20 * (batch_index - 1) / total_batches))
                yield _sse("qs_progress", {
                    "message": _t(
                        f"正在搜索 Reddit：第 {batch_index}/{total_batches} 组，已收集 {len(all_posts)} 条候选...",
                        f"Searching Reddit: batch {batch_index}/{total_batches}, collected {len(all_posts)} candidates...",
                    ),
                    "progress": batch_progress,
                })
                results = loop.run_until_complete(_run_search_batch(batch_jobs))
                for (job_query, _job_subreddit, _job_limit, _job_sort), result in zip(batch_jobs, results):
                    if isinstance(result, Exception) or not result:
                        continue
                    job_dimension = _qs_process_query_dimension(job_query, plan) if research_type == "process_workflow" else ""
                    for p in result:
                        if not isinstance(p, dict):
                            continue
                        pid = p.get("_post_id") or p.get("url", "")
                        if pid and pid not in seen_ids:
                            seen_ids.add(pid)
                            if job_dimension:
                                p["_process_retrieval_dimensions"] = [job_dimension]
                            all_posts.append(p)
                        elif pid and job_dimension:
                            existing_post = next((item for item in all_posts if (item.get("_post_id") or item.get("url", "")) == pid), None)
                            if existing_post is not None:
                                dimensions = set(existing_post.get("_process_retrieval_dimensions") or [])
                                dimensions.add(job_dimension)
                                existing_post["_process_retrieval_dimensions"] = sorted(dimensions)
                yield _sse("qs_progress", {
                    "message": _t(
                        f"Reddit 候选池扩大到 {len(all_posts)} 条，继续交叉验证...",
                        f"Reddit candidate pool expanded to {len(all_posts)} posts. Continuing cross-checks...",
                    ),
                    "progress": min(42, 22 + int(20 * batch_index / total_batches)),
                })

            # 3) 热度/时间过滤 → 相关性过滤 → 排序截断
            all_posts = [p for p in all_posts if not _qs_is_political_or_current_affairs(p)]
            filtered = [p for p in all_posts if _qs_post_meets_heat(p, req.min_score)]

            from datetime import timezone
            if req.time_period != "all":
                period_days = {"week": 7, "month": 30, "3months": 90, "6months": 180}.get(req.time_period, 90)
                cutoff = datetime.now(timezone.utc).timestamp() - period_days * 86400
                filtered = [p for p in filtered if p.get("created_utc", 0) > cutoff]

            if not filtered and all_posts and req.min_score > 0:
                relaxed_score = max(0, req.min_score // 2)
                yield _sse("qs_progress", {
                    "message": _t(
                        f"严格热度结果较少，自动放宽到 {relaxed_score}+ 赞或评论共鸣...",
                        f"Strict popularity filtering found too few results. Relaxing to {relaxed_score}+ upvotes or comment resonance...",
                    ),
                    "progress": 44,
                })
                filtered = [p for p in all_posts if _qs_post_meets_heat(p, relaxed_score)]
                if req.time_period != "all":
                    filtered = [p for p in filtered if p.get("created_utc", 0) > cutoff]

            filtered = _qs_apply_business_signal_filter(filtered)
            filtered = _qs_apply_intent_guard(filtered, req.query)
            filtered.sort(key=_qs_post_rank_score, reverse=True)
            pre_filter_count = len(filtered)
            candidates = filtered[: min(len(filtered), max(12, min(req.limit, 18)))]

            yield _sse("qs_progress", {"message": _t("正在快速校验主题相关性...", "Quickly checking topic relevance..."), "progress": 48})
            filtered = _qs_filter_relevant_posts_fast(candidates, filter_topic)
            if filtered:
                yield _sse("qs_progress", {"message": _t("正在精筛跑题证据，保留真正相关的社区讨论...", "Refining off-topic evidence and keeping truly relevant community discussions..."), "progress": 51})
                filtered = _qs_filter_relevant_posts(
                    filtered,
                    filter_topic,
                    fallback_to_original=False,
                    min_keep=2,
                )
            filtered = _qs_filter_process_evidence(filtered, req.query, plan)
            filtered = _qs_apply_business_signal_filter(filtered)
            filtered.sort(key=_qs_post_rank_score, reverse=True)
            filtered = _qs_diversify_process_posts(filtered, plan, req.limit)

            if research_type == "process_workflow":
                issue_zh, issue_en = _qs_process_evidence_issue(filtered, req.query)
                if issue_zh:
                    yield _sse("qs_progress", {
                        "message": _t(f"{issue_zh}，停止生成流程。", f"{issue_en}. Workflow synthesis was stopped."),
                        "progress": 100,
                    })
                    yield _sse("qs_posts", {
                        "posts": _qs_posts_for_client(filtered),
                        "total": len(filtered),
                        "total_searched": len(all_posts),
                    })
                    yield _sse("qs_summary_chunk", {
                        "text": _t(
                            f"## 流程证据不足\n\n{issue_zh}，目前只能作为局部线索，不能生成完整流程。请缩小地区或申请体系后重试，例如分别搜索“美国 Common App 文书流程”与“英国 UCAS Personal Statement 流程”。",
                            f"## Workflow Evidence Insufficient\n\n{issue_en}. The current results are only partial leads and cannot support a complete workflow. Narrow the region or application system, such as US Common App essays or UK UCAS personal statements.",
                        ),
                    })
                    yield _sse("done", {})
                    return

            # 少于 4 条严格相关证据时，不生成看似完整的综合结论。
            # 小样本可以继续作为线索查看，但不足以支撑跨用户的产品判断。
            if len(filtered) < 4:
                yield _sse("qs_progress", {
                    "message": _t(
                        f"仅找到 {len(filtered)} 条严格相关证据，暂不生成综合结论。",
                        f"Only {len(filtered)} strictly relevant evidence items were found. No synthesis was generated.",
                    ),
                    "progress": 100,
                })
                yield _sse("qs_posts", {
                    "posts": _qs_posts_for_client(filtered),
                    "total": len(filtered),
                    "total_searched": len(all_posts),
                })
                yield _sse("qs_summary_chunk", {
                    "text": _t(
                        "## 证据不足\n\n本次没有保留足够多的直接相关社区讨论，因此不生成综合结论。请收窄问题范围、补充社区或放宽时间范围后重试。",
                        "## Insufficient evidence\n\nThere were not enough directly relevant community discussions to produce a synthesis. Narrow the question, add communities, or widen the time range and try again.",
                    ),
                })
                yield _sse("done", {})
                return

            yield _sse("qs_progress", {
                "message": _t(
                    f"找到 {len(filtered)} 条相关帖子（检索 {len(all_posts)} 条，相关性 {pre_filter_count}→{len(filtered)}）",
                    f"Found {len(filtered)} relevant posts (searched {len(all_posts)} candidates, relevance {pre_filter_count}->{len(filtered)})",
                ),
                "progress": 55,
            })
            yield _sse("qs_posts", {
                "posts": _qs_posts_for_client(filtered),
                "total": len(filtered),
                "total_searched": len(all_posts),
            })

            # 4) 评论充实（前 4 条高信号帖子）
            if req.fetch_comments and filtered:
                top_for_comments = [p for p in filtered[:4] if p.get("_post_id") and len(p.get("comments", [])) < 3]
                total_comment_batches = max(1, (len(top_for_comments) + 1) // 2)
                for batch_index, i in enumerate(range(0, len(top_for_comments), 2), start=1):
                    batch = top_for_comments[i:i + 2]
                    yield _sse("qs_progress", {
                        "message": _t(
                            f"正在补充高信号评论：第 {batch_index}/{total_comment_batches} 组...",
                            f"Enriching high-signal comments: batch {batch_index}/{total_comment_batches}...",
                        ),
                        "progress": min(66, 58 + batch_index * 3),
                    })

                    async def _do_enrich_batch(batch_posts: list[dict]):
                        tasks = [fetcher.read_post(p["_post_id"]) for p in batch_posts]
                        results = await asyncio.gather(*tasks, return_exceptions=True)
                        for p, result in zip(batch_posts, results):
                            if isinstance(result, dict):
                                p["comments"] = result.get("comments", [])
                                if result.get("content"):
                                    p["content"] = result["content"]

                    loop.run_until_complete(_do_enrich_batch(batch))

            if filtered:
                if ui_en:
                    yield _sse("qs_progress", {"message": _t("已保留 Reddit 原文，AI 将直接用英文归纳...", "Keeping Reddit originals. AI will summarize in English..."), "progress": 68})
                else:
                    yield _sse("qs_progress", {"message": "正在翻译证据帖标题和高信号评论...", "progress": 68})
                    translation_limit = min(8, len(filtered)) if research_type == "process_workflow" else min(req.limit, 18)
                    trans = _qs_batch_translate(filtered, max_posts=translation_limit)
                    _qs_apply_translations(filtered, trans)

            yield _sse("qs_progress", {"message": _t("已保留 Reddit 原文，AI 将直接用中文归纳...", "Reddit originals are ready. AI will summarize directly in English..."), "progress": 70})
            topic_overview = _qs_build_topic_overview_fast(filtered, req.query, ui_language)
            yield _sse("qs_progress", {"message": _t("主题概览已整理，准备生成 AI 分析...", "Topic overview prepared. Getting ready to generate AI analysis..."), "progress": 74})

            market_signal: dict[str, Any] | None = None
            if req.market_search and strategy["mode"] == "hybrid":
                yield _sse("qs_progress", {"message": _t("正在规划 Sensor Tower 搜索词...", "Planning Sensor Tower search terms..."), "progress": 75})
                market_plan = _qs_market_plan(req.query)
                market_ok, market_issue, market_queries = _qs_validate_market_plan_for_search(
                    market_plan,
                    req.query,
                    queries,
                )
                if not market_ok:
                    yield _sse("qs_market", {
                        "available": False,
                        "candidate_region": "US",
                        "metrics_region": "全球",
                        "metrics_time_period": req.market_time_period,
                        "market_region": "全球",
                        "queries": market_queries,
                        "top_apps": [],
                        "error": _t(f"{market_issue}，本次只展示 Reddit 社区讨论结果。", "Market search planning was unstable, so this result only shows Reddit community discussions."),
                    })
                    market_signal = None
                    yield _sse("qs_progress", {"message": _t("市场搜索规划不稳定，已跳过 Sensor Tower 查询...", "Market search planning was unstable. Skipping Sensor Tower query..."), "progress": 78})
                else:
                    market_plan["market_queries"] = market_queries
                    plan = _qs_merge_market_plan(plan, market_plan)
                    yield _sse("qs_progress", {"message": _t("正在查询 Sensor Tower 市场信号，通常需要 30-90 秒...", "Querying Sensor Tower market signals. This usually takes 30-90 seconds..."), "progress": 76})
                    market_signal = yield from _fetch_market_signal_with_progress(
                        plan,
                        queries,
                        start=76,
                        cap=83,
                        message=_t("正在查询 Sensor Tower 市场信号...", "Querying Sensor Tower market signals..."),
                    )
                    yield _sse("qs_market", market_signal)

            yield _sse("qs_progress", {"message": _t("搜索完成，正在整理帖子证据...", "Search finished. Organizing post evidence..."), "progress": 78})

            # 5) 推送评论补全后的帖子列表
            yield _sse("qs_posts", {
                "posts": _qs_posts_for_client(filtered),
                "total": len(filtered),
                "total_searched": len(all_posts),
            })

            # 6) LLM 流式总结
            if not filtered:
                yield _sse("qs_summary_chunk", {"text": _t("未找到符合条件的帖子，请尝试调整搜索条件或放宽筛选。", "No matching posts were found. Try adjusting the query or relaxing filters.")})
                yield _sse("done", {})
                return

            posts_text_parts = []
            for i, p in enumerate(filtered[:8]):
                title = p.get("title", "")
                title_zh = p.get("title_zh", "")
                if ui_en:
                    entry = f"### Post {i + 1} [{p.get('score', 0)} upvotes] {title}\n"
                    entry += f"Source: {p.get('source', '')} | Comments: {p.get('num_comments', 0)}\n"
                else:
                    entry = f"### 帖子{i + 1} [{p.get('score', 0)}赞] {title}\n"
                    if title_zh and title_zh != title:
                        entry += f"标题译: {title_zh}\n"
                    entry += f"来源: {p.get('source', '')} | 评论数: {p.get('num_comments', 0)}\n"
                process_dimensions = p.get("_process_dimensions") or []
                if process_dimensions:
                    entry += f"{_t('检索维度', 'Retrieval dimensions')}: {', '.join(process_dimensions)}\n"
                content = p.get("content", "")
                content_zh = p.get("content_zh", "")
                if content:
                    entry += f"{_t('正文', 'Body')}: {content[:120]}\n"
                    if content_zh and not ui_en:
                        entry += f"正文译: {content_zh[:120]}\n"
                top_comments = sorted(
                    [_qs_norm_comment(c) for c in p.get("comments", [])],
                    key=lambda c: c["score"],
                    reverse=True,
                )[:1]
                if top_comments:
                    entry += _t("高赞评论:\n", "Top comment:\n")
                    for c in top_comments:
                        score_tag = _t(f"[{c['score']}赞] ", f"[{c['score']} upvotes] ") if c["score"] else ""
                        entry += f"  - {score_tag}{c['body'][:100]}\n"
                        if c.get("body_zh") and not ui_en:
                            entry += f"    译: {c['body_zh'][:100]}\n"
                posts_text_parts.append(entry)

            posts_data = "\n".join(posts_text_parts)
            if research_type == "process_workflow":
                summary_prompt = QUICK_SEARCH_PROCESS_SUMMARY_PROMPT_EN if ui_en else QUICK_SEARCH_PROCESS_SUMMARY_PROMPT
            else:
                summary_prompt = QUICK_SEARCH_SUMMARY_PROMPT_EN if ui_en else QUICK_SEARCH_SUMMARY_PROMPT
            summary_messages = [
                {"role": "system", "content": _t("你是产品研究分析师，擅长从社区讨论中提炼洞察。输出 Markdown。", "You are a product research analyst who extracts insights from community discussions. Output Markdown.")},
                {"role": "user", "content": summary_prompt.format(
                    query=req.query,
                    post_count=len(filtered),
                    topic_overview=topic_overview,
                    market_context=_qs_format_market_context(market_signal, ui_language),
                    posts_data=posts_data,
                )},
            ]

            yield _sse("qs_progress", {"message": _t("AI 正在提炼结论、热点和数据局限...", "AI is extracting conclusions, hotspots, and limitations..."), "progress": 85})

            summary_started = False
            summary_chunks: list[str] = []
            try:
                for chunk in call_llm_stream(summary_messages, max_tokens=650):
                    if chunk:
                        summary_started = True
                        summary_chunks.append(chunk)
                        if research_type != "process_workflow":
                            yield _sse("qs_summary_chunk", {"text": chunk})
                if research_type == "process_workflow" and summary_started:
                    process_summary = _qs_sanitize_process_summary("".join(summary_chunks), filtered[:8], ui_language)
                    if process_summary:
                        yield _sse("qs_summary_chunk", {"text": process_summary})
                    else:
                        yield _sse("qs_summary_chunk", {"text": _t(
                            "## 证据不足\n\n生成的流程阶段没有通过阶段与证据一致性校验，因此不展示可能由常识补全的流程。请收窄对象、补充目标社区或放宽时间范围后重试。",
                            "## Insufficient Evidence\n\nThe generated workflow did not pass stage-to-evidence validation, so potentially inferred stages are not shown. Narrow the subject, add target communities, or widen the time range and try again.",
                        )})
            except Exception as e:
                print(f"[QuickSearch] summary stream failed, keep evidence results: {e}")
                if research_type == "process_workflow":
                    yield _sse("qs_summary_chunk", {
                        "text": _t(
                            "## 证据不足\n\n流程总结未完整生成或未通过阶段与证据一致性校验，因此不展示可能不可靠的流程。请稍后重试。",
                            "## Insufficient Evidence\n\nThe workflow synthesis was incomplete or did not pass stage-to-evidence validation, so a potentially unreliable workflow is not shown. Please try again later.",
                        )
                    })
                elif not summary_started:
                    yield _sse("qs_summary_chunk", {
                        "text": _t(
                            "## 结论\n"
                            "AI 总结服务暂不可用，已先展示下方高信号证据帖。\n\n"
                            "## 数据局限\n"
                            "当前结果已完成社区搜索和安全过滤，但还没有生成自动归纳；可以稍后重试 AI 分析。",
                            "## Conclusion\n"
                            "AI summarization is temporarily unavailable, but high-signal evidence posts are shown below.\n\n"
                            "## Data Limitations\n"
                            "The result completed community search and safety filtering, but automatic synthesis has not been generated yet. Try AI analysis again later.",
                        )
                    })

            yield _sse("qs_progress", {"message": _t("完成", "Done"), "progress": 100})
            yield _sse("done", {})

        except Exception as e:
            print(f"[QuickSearch] runtime failed: {e}")
            yield _sse("error", {"message": _qs_runtime_error_message(e, ui_language)})
        finally:
            if loop is not None:
                try:
                    loop.close()
                except Exception:
                    pass
            clear_thread_session()

    return StreamingResponse(_generate(), media_type="text/event-stream")


# ---- 在线统计 ----


class AnalyticsEventRequest(BaseModel):
    event: str
    properties: dict[str, Any] = Field(default_factory=dict)


@router.post("/analytics/event")
def analytics_event(req: AnalyticsEventRequest, request: Request):
    """记录一次前端产品埋点事件。"""
    ctx = _get_session(request)
    return _record_analytics_event(
        ctx.session_id,
        req.event,
        req.properties,
        env=_analytics_environment(request),
    )


@router.get("/analytics/summary")
def analytics_summary(request: Request):
    """返回产品埋点聚合统计，不包含用户哈希明细。"""
    if not _is_local_analytics_request(request):
        raise HTTPException(status_code=404, detail="not found")
    env = str(request.query_params.get("env") or "production").lower()
    return _analytics_summary_public(env)


# ============================================================
# 用户画像建模
# ============================================================

class PersonaRequest(BaseModel):
    need_index: int
    language: str = UI_LANGUAGE_ZH

@router.post("/generate-personas")
def generate_personas(req: PersonaRequest, request: Request):
    """基于需求主题下的真实帖子，用 LLM 两步法建模 2-4 个典型用户画像。SSE 流式返回。"""
    ctx = _get_session(request)
    persona_language = _normalize_ui_language(req.language)
    persona_is_en = _is_ui_en(persona_language)
    def _pt(zh: str, en: str) -> str:
        return en if persona_is_en else zh

    needs_data = get_needs(request)["needs"]
    if req.need_index < 0 or req.need_index >= len(needs_data):
        raise HTTPException(status_code=400, detail=_pt("无效的需求索引", "Invalid demand index"))

    need = needs_data[req.need_index]

    # 检查 LLM 可用性
    set_thread_session(ctx)
    try:
        llm_ok, llm_err = check_llm_available()
    finally:
        clear_thread_session()
    if not llm_ok:
        model_name = "GPT" if ctx._general_model == "gpt" else "Claude"
        safe_error = _friendly_error_for_language(persona_language, llm_err) if llm_err else _pt(
            f"{model_name} 模型不可用，请检查本地配置",
            f"{model_name} model is unavailable. Please check your local settings.",
        )
        def _err():
            yield _sse("persona_error", {"message": safe_error})
        return StreamingResponse(_err(), media_type="text/event-stream")

    def _format_posts_for_persona(need_data: dict) -> str:
        """将帖子格式化为画像建模的上下文素材。"""
        lines = []
        for i, post in enumerate(need_data.get("posts", []), 1):
            lines.append(f"### {_pt('帖子', 'Post')} {i}: {post.get('title', '')}")
            lines.append(f"- {_pt('来源', 'Source')}: {post.get('source', 'unknown')}")
            lines.append(f"- {_pt('赞数', 'Score')}: {post.get('score', 0)} | {_pt('评论数', 'Comments')}: {post.get('num_comments', 0)}")
            content = post.get("content", "")
            if content:
                lines.append(f"- {_pt('内容', 'Content')}: {content[:1200]}")
            comments = post.get("comments", [])
            if comments:
                lines.append(f"- {_pt('用户评论', 'User comments')}:")
                for c in comments[:10]:
                    lines.append(f"  > {c[:400]}")
            lines.append("")
        return "\n".join(lines)

    posts_text = _format_posts_for_persona(need)

    # 初始化 persona_job
    with ctx.persona_lock:
        ctx.persona_job = ctx._empty_persona_job()
        ctx.persona_job["active"] = True
        ctx.persona_job["need_index"] = req.need_index

    def _update_progress(progress: int, message: str):
        with ctx.persona_lock:
            ctx.persona_job["progress"] = progress
            ctx.persona_job["message"] = message

    def _run_persona_bg():
        set_thread_session(ctx)
        try:
            # ===== Step 1: 聚类分析 — 识别用户群体 =====
            _update_progress(10, _pt("正在分析用户发言，识别行为模式...", "Analyzing user statements and identifying behavior patterns..."))

            step1_prompt = f"""你是一位资深的用户研究专家。以下是围绕「{need.get('need_title', '')}」这一需求主题收集的真实用户帖子和评论。

## 需求描述
{need.get('need_description', '')}

## 真实帖子数据
{posts_text}

## 任务
请仔细阅读以上所有帖子和评论，识别出 2-4 个行为模式、动机、背景明显不同的用户群体。

要求：
1. 每个群体必须有明确不同的特征（不要只是年龄不同，要在动机、行为、痛点上有质的差异）
2. 群体划分必须有帖子/评论中的真实证据支撑
3. 为每个群体提供一个简短标签和核心特征关键词

请以 JSON 格式输出：
```json
{{
  "groups": [
    {{
      "label": "群体简短标签",
      "core_traits": ["特征1", "特征2", "特征3"],
      "motivation": "核心动机描述",
      "evidence_posts": [1, 3, 5]
    }}
  ]
}}
```"""

            if persona_is_en:
                step1_prompt = f"""You are a senior user research expert. Below are real user posts and comments collected around the demand topic "{need.get('need_title', '')}".

## Demand Description
{need.get('need_description', '')}

## Real Post Data
{posts_text}

## Task
Read the posts and comments carefully. Identify 2-4 user groups with clearly different behavior patterns, motivations, backgrounds, and pain points.

Requirements:
1. Each group must be meaningfully different; do not split only by age.
2. Each group must be supported by evidence from the posts/comments.
3. Provide a short label and core trait keywords for each group.

Return JSON only:
```json
{{
  "groups": [
    {{
      "label": "Short group label",
      "core_traits": ["trait 1", "trait 2", "trait 3"],
      "motivation": "core motivation",
      "evidence_posts": [1, 3, 5]
    }}
  ]
}}
```"""

            step1_result = call_llm([
                {"role": "system", "content": _pt(
                    "你是用户研究专家，擅长从定性数据中识别用户群体。严格输出 JSON。",
                    "You are a user research expert who identifies user segments from qualitative data. Output strict JSON only.",
                )},
                {"role": "user", "content": step1_prompt},
            ], max_tokens=1800, timeout_seconds=60, max_attempts=2)

            _update_progress(30, _pt("聚类分析完成，开始建模画像...", "Segmentation completed. Starting persona modeling..."))

            groups_data = _parse_json_from_text(step1_result)
            if not groups_data or "groups" not in groups_data:
                # 降级：直接生成画像，不依赖聚类结果
                groups_data = {"groups": (
                    [
                        {"label": "Core users", "core_traits": ["frequent users"], "motivation": "solve the core pain"},
                        {"label": "Potential users", "core_traits": ["need-aware but inactive"], "motivation": "find a better solution"},
                    ]
                    if persona_is_en else
                    [
                        {"label": "核心用户", "core_traits": ["高频使用者"], "motivation": "解决核心痛点"},
                        {"label": "潜在用户", "core_traits": ["有需求但未行动"], "motivation": "寻找解决方案"},
                    ]
                )}

            # 每个画像都需要完整引用和日常场景；过多分组会把单次响应推到
            # 模型或中转站的长度上限，优先稳定产出有证据支撑的 2-3 个画像。
            groups = groups_data["groups"][:3]
            if not groups:
                groups = [{
                    "label": _pt("核心用户", "Core users"),
                    "core_traits": [_pt("高频遇到该问题", "frequent exposure to the problem")],
                    "motivation": _pt("解决核心痛点", "solve the core pain"),
                }]

            # ===== Step 2: 为每个群体生成完整画像 =====
            _update_progress(40, _pt(
                f"正在为 {len(groups)} 个用户群体建模详细画像...",
                f"Building detailed personas for {len(groups)} user groups...",
            ))

            # 将详细画像拆成单群体请求，避免一次请求同时携带全部帖子和多个长画像。
            # 每个请求只返回一个 JSON 对象，后端再合并并执行统一证据校验。
            def _group_evidence_posts(group: dict) -> list[dict]:
                raw_indices = group.get("evidence_posts") or []
                selected: list[dict] = []
                for raw_index in raw_indices:
                    try:
                        index = int(raw_index) - 1  # 提示词中的帖子编号从 1 开始
                    except (TypeError, ValueError):
                        continue
                    if 0 <= index < len(need.get("posts", [])):
                        selected.append(need["posts"][index])
                if not selected:
                    selected = list(need.get("posts", []))[:3]
                return selected[:4]

            def _group_posts(group: dict) -> str:
                lines: list[str] = []
                for index, post in enumerate(_group_evidence_posts(group), 1):
                    lines.append(
                        f"[{index}] {post.get('title', '')}\n"
                        f"URL: {post.get('url', '')}\n"
                        f"Content: {(post.get('content') or '')[:700]}"
                    )
                    comments = post.get("comments") or []
                    for comment in comments[:3]:
                        body = comment.get("body", "") if isinstance(comment, dict) else comment
                        if body:
                            lines.append(f"Comment: {str(body)[:300]}")
                return "\n\n".join(lines)

            def _persona_validation_errors(item: Any, allowed_urls: set[str]) -> list[str]:
                """Normalize harmless shape differences and report evidence/schema gaps."""
                if not isinstance(item, dict):
                    return ["response is not a JSON object"]

                day_in_life = item.get("day_in_life")
                if isinstance(day_in_life, list):
                    timeline: list[str] = []
                    for entry in day_in_life:
                        if isinstance(entry, dict):
                            stage = str(entry.get("time") or entry.get("stage") or entry.get("period") or "").strip()
                            activity = str(entry.get("content") or entry.get("activity") or entry.get("description") or "").strip()
                            if stage or activity:
                                timeline.append(f"{stage}: {activity}".strip(": "))
                        elif str(entry).strip():
                            timeline.append(str(entry).strip())
                    item["day_in_life"] = "\n".join(timeline)
                elif isinstance(day_in_life, dict):
                    item["day_in_life"] = "\n".join(
                        f"{str(key).strip()}: {str(value).strip()}".strip(": ")
                        for key, value in day_in_life.items()
                        if str(value).strip()
                    )

                quotes = item.get("quotes")
                if isinstance(quotes, list):
                    normalized_quotes: list[dict] = []
                    for quote in quotes:
                        if isinstance(quote, dict):
                            source_url = str(quote.get("source_url") or quote.get("url") or "").strip()
                            if source_url in allowed_urls:
                                quote["source_url"] = source_url
                            normalized_quotes.append(quote)
                        elif isinstance(quote, str) and quote.strip() and len(allowed_urls) == 1:
                            # Safe only when the group has exactly one possible source.
                            normalized_quotes.append({"text": quote.strip(), "source_url": next(iter(allowed_urls))})
                    item["quotes"] = normalized_quotes

                errors: list[str] = []
                for field in ("name", "bio", "tagline"):
                    if not isinstance(item.get(field), str) or not item[field].strip():
                        errors.append(f"missing {field}")
                if len(str(item.get("day_in_life") or "").splitlines()) < 4:
                    errors.append("day_in_life needs at least 4 lines")
                for field in ("goals", "frustrations", "quotes"):
                    if not isinstance(item.get(field), list) or not item[field]:
                        errors.append(f"missing {field}")
                if isinstance(item.get("quotes"), list) and not any(
                    isinstance(quote, dict)
                    and str(quote.get("text") or "").strip()
                    and str(quote.get("source_url") or "").strip() in allowed_urls
                    for quote in item["quotes"]
                ):
                    errors.append("quotes lack an allowed source_url")
                return errors

            def _persona_prompt(group: dict) -> str:
                group_label = str(group.get("label") or "用户群体")
                traits = ", ".join(str(item) for item in (group.get("core_traits") or [])[:4])
                motivation = str(group.get("motivation") or "")
                evidence = _group_posts(group)
                if persona_is_en:
                    return f'''Build exactly one persona for this user group. Return one JSON object only, no Markdown.

Demand: {need.get("need_title_en") or need.get("need_title", "")}
Group: {group_label}; traits: {traits}; motivation: {motivation}
Evidence posts (use only these; quote source_url exactly):
{evidence}

Rules: Do not invent facts, metrics, willingness to pay, or quotes. Use natural English except quote.text, which must preserve the original. Include 2 goals, 2 frustrations, 2 behaviors, 1-2 quotes with source_url, and a 6-line day_in_life timeline. Keep every field concise.
Required JSON keys: name, avatar_seed, gender, avatar_hint, tagline, bio, demographics, goals, frustrations, behaviors, tools_used, willingness_to_pay, quotes, day_in_life, priority_rank, switching_trigger, deal_breaker.'''
                return f'''请只为下方一个用户群体生成一个画像，输出单个 JSON 对象，不要 Markdown。

需求：{need.get("need_title", "")}
群体：{group_label}；特征：{traits}；动机：{motivation}
证据帖子（只能使用这些内容，source_url 必须原样保留）：
{evidence}

要求：不要编造事实、数字、付费意愿或引用。除 quote.text 保留原文外，面向用户的字段都用中文；包含 2 个目标、2 个痛点、2 个行为、1-2 条带 source_url 的原文引用，以及 6 行简短 day_in_life 时间线。所有字段保持具体但简洁。
必须包含字段：name, avatar_seed, gender, avatar_hint, tagline, bio, demographics, goals, frustrations, behaviors, tools_used, willingness_to_pay, quotes, day_in_life, priority_rank, switching_trigger, deal_breaker。'''

            def _generate_one_persona(group: dict) -> dict:
                set_thread_session(ctx)
                try:
                    messages = [
                        {"role": "system", "content": _pt(
                            "你是用户研究专家，只输出严格 JSON，不要编造。",
                            "You are a user research expert. Output strict JSON only and do not fabricate.",
                        )},
                        {"role": "user", "content": _persona_prompt(group)},
                    ]
                    try:
                        result = call_llm(messages, max_tokens=2600, timeout_seconds=90, max_attempts=1)
                    except Exception as first_error:
                        # 中转站偶发截断时用更短响应重试一次；不复制已返回的半成品。
                        print(f"[Persona] group retry after first failure: {type(first_error).__name__}")
                        retry_messages = [
                            messages[0],
                            {"role": "user", "content": _persona_prompt(group) + "\n只保留必要字段，控制在 1200 tokens 内。"},
                        ]
                        result = call_llm(retry_messages, max_tokens=1800, timeout_seconds=60, max_attempts=1)
                    parsed = _parse_json_from_text(result)
                    if isinstance(parsed, dict) and isinstance(parsed.get("personas"), list):
                        parsed = parsed["personas"][0] if parsed["personas"] else None
                    if not isinstance(parsed, dict):
                        raise ValueError("persona response is not a JSON object")
                    allowed_urls = {
                        str(post.get("url") or "").strip()
                        for post in _group_evidence_posts(group)
                        if str(post.get("url") or "").strip()
                    }
                    validation_errors = _persona_validation_errors(parsed, allowed_urls)
                    if validation_errors:
                        print(f"[Persona] repairing group schema: {', '.join(validation_errors)}")
                        repair_result = call_llm([
                            {"role": "system", "content": _pt(
                                "你是 JSON 数据修复器。只修复缺失字段和格式，不添加证据中没有的事实。只输出单个 JSON 对象。",
                                "You repair JSON data. Fix only missing fields and shape; do not add facts absent from the evidence. Return one JSON object only.",
                            )},
                            {"role": "user", "content": _pt(
                                "下方画像校验失败。错误："
                                + "; ".join(validation_errors)
                                + "\n允许的 source_url："
                                + json.dumps(sorted(allowed_urls), ensure_ascii=False)
                                + "\n请基于原画像修复；引用只能使用允许的 URL，不能编造原话。\n原画像：\n"
                                + json.dumps(parsed, ensure_ascii=False),
                                "The persona below failed validation. Errors: "
                                + "; ".join(validation_errors)
                                + "\nAllowed source_url values: "
                                + json.dumps(sorted(allowed_urls))
                                + "\nRepair the existing persona only. Quotes may use only allowed URLs and must not be invented.\nOriginal persona:\n"
                                + json.dumps(parsed, ensure_ascii=False),
                            )},
                        ], max_tokens=2000, timeout_seconds=60, max_attempts=1)
                        repaired = _parse_json_from_text(repair_result)
                        if isinstance(repaired, dict) and isinstance(repaired.get("personas"), list):
                            repaired = repaired["personas"][0] if repaired["personas"] else None
                        if not isinstance(repaired, dict):
                            raise ValueError("persona repair is not a JSON object")
                        parsed = repaired
                        validation_errors = _persona_validation_errors(parsed, allowed_urls)
                    if validation_errors:
                        raise ValueError("persona schema invalid: " + "; ".join(validation_errors))
                    return parsed
                finally:
                    clear_thread_session()

            personas: list[dict] = []
            persona_errors: list[str] = []
            import concurrent.futures as _persona_futures
            # 中转站对同一模型的并发长响应不稳定，画像请求串行执行以降低截断概率。
            with _persona_futures.ThreadPoolExecutor(max_workers=1) as executor:
                pending = [executor.submit(_generate_one_persona, group) for group in groups]
                for completed, future in enumerate(_persona_futures.as_completed(pending), 1):
                    try:
                        personas.append(future.result())
                    except Exception as persona_error:
                        persona_errors.append(type(persona_error).__name__)
                        print(f"[Persona] one group failed ({type(persona_error).__name__}); continue with other groups")
                    _update_progress(50 + int(35 * completed / max(len(pending), 1)), _pt(
                        f"已完成 {len(personas)}/{len(pending)} 个画像，继续整理证据...",
                        f"Generated {len(personas)}/{len(pending)} personas. Organizing evidence...",
                    ))

            if len(personas) < 2:
                raise RuntimeError("画像分段生成失败，未达到最少 2 个有效画像")
            if persona_errors:
                _update_progress(85, _pt(
                    f"已生成 {len(personas)} 个有效画像，跳过 {len(persona_errors)} 个失败分组...",
                    f"Generated {len(personas)} valid personas; skipped {len(persona_errors)} failed group...",
                ))
            _update_progress(85, _pt("画像生成完成，正在解析结果...", "Personas generated. Parsing results..."))

            # 后续的统一校验和持久化逻辑复用原实现；当前分支已在下方完成保存后返回。
            # 旧的单次请求模板保留在此处仅作兼容参考，不会再执行。
            step2_result = ""
            if personas:
                step2_result = json.dumps(personas, ensure_ascii=False)
            else:
                raise ValueError("no personas generated")

            # 解析结果统一走下方原有校验逻辑。

            groups_desc = "\n".join([
                f"- {_pt('群体', 'Group')}{i+1}「{g.get('label', '')}」: {', '.join(g.get('core_traits', []))} — {g.get('motivation', '')}"
                for i, g in enumerate(groups)
            ])

            step2_prompt = f"""你是一位资深用户研究专家，现在需要为以下用户群体建模详细的用户画像。

## 需求主题
标题：{need.get('need_title', '')}
描述：{need.get('need_description', '')}

## 识别到的用户群体
{groups_desc}

## 真实帖子数据（作为画像素材）
{posts_text}

## 任务
为每个群体生成一个鲜活、具体的用户画像（Persona）。每个画像必须像一个真实的人，让产品经理读完后能在脑海里浮现这个人的形象。

## 核心原则
- 画像必须符合其所在地区的真实生活习惯（如北美用户的作息、通勤方式、社交习惯与中国用户截然不同）
- 性别必须明确，所有描述、人设、行为都要与性别一致
- 不同画像之间要有明显差异，覆盖不同的用户类型

要求：
1. name：必须用英文名（Western name），禁止使用中文名！格式为 "英文名, 年龄, 职业"（如 "Alex, 28, 前端工程师"、"Emily, 34, 产品经理"），名字要符合性别和种族特征
2. gender：明确指定 "male" 或 "female"，画像群体中男女应合理分布
3. avatar_hint：用英文描述此人的外貌特征，方便匹配头像（如 "young white male, brown hair, glasses" 或 "middle-aged asian female, professional"）
4. tagline：一句话中文人设标签，要有画面感（如 "被照片淹没的记录强迫症患者"）
5. bio：一句中文描述这个人是什么样的人，所有代词和描述要与性别一致
6. demographics：中文人口特征（age_range/occupation/location_hint/tech_savviness）
7. goals/frustrations：**必须用中文**，不要输出英文！基于真实帖子内容概括成中文痛点和目标，不要直接复制英文原文
8. quotes：从帖子中提取 2-3 条最能代表此画像的原文（text 保留英文原文），同时提供 text_zh（准确的中文翻译，不要机翻味），如果帖子数据中有 URL 则提供 source_url
9. day_in_life：中文，以第一人称写，用时间线格式（每个时间段换行，格式为 "HH:MM - 内容"），覆盖从早到晚 6-8 个时间节点，每个节点 1-2 句话。要求：
   - 深度结合需求主题，每个时间点都要体现这个需求/痛点在用户日常中的具体表现
   - 当叙事中出现与需求主题直接相关的关键短语时，用 **双星号** 将其加粗（如"我总想**把信息放进一个能随时搜到的地方**"）
   - 符合当地的生活习惯（如北美用户开车通勤、用 Slack 沟通、吃三明治午餐等，不要出现与当地文化不符的细节）
   - 描写情绪变化和心理活动，让读者能感同身受
   - 嵌入 2-3 条来自帖子的真实引用，自然融入叙事中
10. switching_trigger/deal_breaker：中文

请以 JSON 数组格式输出所有画像：
```json
[
  {{
    "name": "Alex, 28, 前端工程师",
    "avatar_seed": "alex-28-engineer",
    "gender": "male",
    "avatar_hint": "young white male, brown hair, casual",
    "tagline": "一句话人设标签",
    "bio": "一句话描述这个人是什么样的人",
    "demographics": {{
      "age_range": "25-32",
      "occupation": "前端工程师",
      "location_hint": "北美",
      "tech_savviness": "high"
    }},
    "goals": ["中文目标1", "中文目标2"],
    "frustrations": ["中文痛点1", "中文痛点2"],
    "behaviors": ["行为1", "行为2"],
    "tools_used": ["工具1", "工具2"],
    "willingness_to_pay": "付费意愿描述",
    "quotes": [
      {{"text": "Original English quote from post", "text_zh": "准确的中文翻译", "source_url": "https://reddit.com/r/..."}}
    ],
    "day_in_life": "07:00 - 闹钟响了，我从床上爬起来...\n07:30 - 洗漱完毕，打开笔记本，我总想**把信息放进一个能随时搜到的地方**...\n09:00 - 开车到公司...\n10:30 - 晨会结束后...\n12:30 - 午餐时间...\n14:00 - 下午第一个会议...\n16:00 - 又遇到了老问题...\n18:00 - 收拾东西准备下班...\n19:30 - 到家后...\n21:00 - 坐在沙发上...\n23:00 - 睡前刷手机...",
    "priority_rank": ["需求1", "需求2", "需求3"],
    "switching_trigger": "什么会让 TA 换产品",
    "deal_breaker": "绝对不能接受什么"
  }}
]
```"""

            if persona_is_en:
                step2_prompt = f"""You are a senior user research expert. Build detailed personas for the following user groups.

## Demand Topic
Title: {need.get('need_title', '')}
Description: {need.get('need_description', '')}

## Identified User Groups
{groups_desc}

## Real Post Data
{posts_text}

## Task
Create vivid, specific personas based on the real posts. Each persona should feel like a real person a product manager can picture.

Core rules:
- Personas must reflect realistic local life habits for their likely region.
- Gender must be explicit, and pronouns/details must match gender.
- Personas must be meaningfully different from one another.
- All user-facing fields must be in natural English.
- Keep original quotes in their original language. Set text_zh to an empty string in English mode.

Return a JSON array only:
```json
[
  {{
    "name": "Alex, 28, Frontend Engineer",
    "avatar_seed": "alex-28-engineer",
    "gender": "male",
    "avatar_hint": "young white male, brown hair, casual",
    "tagline": "A visual one-line persona label",
    "bio": "One sentence describing who this person is.",
    "demographics": {{
      "age_range": "25-32",
      "occupation": "Frontend Engineer",
      "location_hint": "North America",
      "tech_savviness": "high"
    }},
    "goals": ["goal 1", "goal 2"],
    "frustrations": ["frustration 1", "frustration 2"],
    "behaviors": ["behavior 1", "behavior 2"],
    "tools_used": ["tool 1", "tool 2"],
    "willingness_to_pay": "Short willingness-to-pay description",
    "quotes": [
      {{"text": "Original quote from post", "text_zh": "", "source_url": "https://reddit.com/r/..."}}
    ],
    "day_in_life": "07:00 - I wake up and...\n08:30 - On the way to work...\n10:00 - During the first focused work block...\n12:30 - At lunch...\n15:00 - The problem shows up again...\n18:00 - After work...\n21:30 - Before bed...",
    "priority_rank": ["need 1", "need 2", "need 3"],
    "switching_trigger": "What would make this person switch products",
    "deal_breaker": "What this person absolutely would not accept"
  }}
]
```"""

            _update_progress(50, _pt("正在深度建模用户画像，预计约 1-2 分钟...", "Modeling detailed personas. Estimated time: 1-2 minutes..."))

            step2_result = json.dumps(personas, ensure_ascii=False) if personas else call_llm([
                {"role": "system", "content": _pt(
                    "你是用户研究专家，擅长建模鲜活的用户画像。基于真实数据，不要编造。严格输出 JSON 数组。name 字段必须使用英文名（如 Alex、Emily、Marcus），严禁中文名！goals、frustrations、tagline、bio、day_in_life、demographics 等所有字段必须用中文，绝对不能出现英文！唯一例外：quotes 中的 text 保留英文原文并附 text_zh 中文翻译，avatar_hint 用英文。day_in_life 中与需求相关的关键短语请用 **双星号** 加粗。",
                    "You are a user research expert who creates vivid personas from real evidence. Do not fabricate. Output a strict JSON array. All user-facing persona fields must be natural English. Names must use Western-style English names. Keep quotes.text in the original language, set quotes.text_zh to an empty string, and keep avatar_hint in English.",
                )},
                {"role": "user", "content": step2_prompt},
            ], max_tokens=6000, timeout_seconds=240, max_attempts=1)

            _update_progress(85, _pt("画像生成完成，正在解析结果...", "Personas generated. Parsing results..."))

            personas = _parse_json_from_text(step2_result)
            if personas is None:
                with ctx.persona_lock:
                    ctx.persona_job["error"] = _pt("画像生成结果解析失败，请重试", "Persona result parsing failed. Please retry.")
                    ctx.persona_job["active"] = False
                return

            # 兼容两种格式：直接数组或包在对象里
            if isinstance(personas, dict):
                personas = personas.get("personas", [])
            post_urls = {
                str(post.get("url") or "").strip()
                for post in need.get("posts", [])
                if str(post.get("url") or "").strip()
            }

            def _valid_persona(item: Any) -> bool:
                return not _persona_validation_errors(item, post_urls)

            if not isinstance(personas, list) or not personas or not all(_valid_persona(item) for item in personas):
                with ctx.persona_lock:
                    ctx.persona_job["error"] = _pt("画像缺少可回溯证据或必要字段，未保存结果，请重试", "Personas lacked traceable evidence or required fields. Nothing was saved; please retry.")
                    ctx.persona_job["active"] = False
                return

            _update_progress(95, _pt("整理画像数据...", "Organizing persona data..."))

            # 持久化到 session 目录
            persona_file = ctx.data_dir / f"personas_{req.need_index}_{int(_time.time())}.json"
            _safe_json_write(persona_file, {
                "need_index": req.need_index,
                "need_title": need.get("need_title", ""),
                "language": persona_language,
                "personas": personas,
                "created_at": datetime.now().isoformat(),
            })

            with ctx.persona_lock:
                ctx.persona_job["personas"] = personas
                ctx.persona_job["progress"] = 100
                ctx.persona_job["message"] = _pt("画像建模完成！", "Persona modeling completed.")
                ctx.persona_job["done"] = True
                ctx.persona_job["active"] = False

        except Exception as e:
            with ctx.persona_lock:
                ctx.persona_job["error"] = _pt(f"画像生成失败：{_friendly_error(e)}", f"Persona generation failed: {_friendly_error_for_language(persona_language, e)}")
                ctx.persona_job["active"] = False

    t = threading.Thread(target=_run_persona_bg, daemon=True)
    t.start()

    # SSE 流：从 persona_job 读取状态
    def event_stream() -> Generator[str, None, None]:
        _last_progress = -1
        while True:
            with ctx.persona_lock:
                job = ctx.persona_job
                progress = job["progress"]
                message = job["message"]
                error = job["error"]
                done = job["done"]
                personas = job["personas"]

            if error:
                yield _sse("persona_error", {"message": error})
                return

            if progress != _last_progress and message:
                yield _sse("persona_progress", {"progress": progress, "message": message})
                _last_progress = progress

            if done and personas is not None:
                yield _sse("persona_done", {"personas": personas})
                yield "\n"
                return

            if not job["active"] and not done:
                return

            _time.sleep(0.5)

    return StreamingResponse(event_stream(), media_type="text/event-stream")
