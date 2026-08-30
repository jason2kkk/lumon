"""
st_client.py — SensorTower CLI (st-cli) 封装

通过调用本机安装的 st-cli 命令行工具获取竞品数据，
包括月收入、月下载、月活、市占率、增长率、App Store 评论等。
同时支持品类级别的 Top Apps 和市场数据查询（用于热度监控模块）。
"""

import json
import hashlib
import math
import re
import subprocess
import sys
import tempfile
import time
from defusedxml import ElementTree as ET
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import Request as UrlRequest, urlopen

# ---------------------------------------------------------------------------
# st-cli 内部模块动态导入（用于品类级 API 调用）
# ---------------------------------------------------------------------------
_ST_CLI_SITE_PKGS: str | None = None
_st_api_mod: Any = None
_st_auth_mod: Any = None
_st_client_mod: Any = None
_st_constants_mod: Any = None
_LAST_NICHE_ERROR = ""


def _ensure_st_cli_imports() -> bool:
    """延迟导入 st-cli 内部模块，加入 sys.path（仅一次）。"""
    global _ST_CLI_SITE_PKGS, _st_api_mod, _st_auth_mod, _st_client_mod, _st_constants_mod
    if _st_api_mod is not None:
        return True
    try:
        import importlib
        st_bin = subprocess.run(
            ["which", "st"], capture_output=True, text=True, timeout=5
        ).stdout.strip()
        if not st_bin:
            return False
        st_script = Path(st_bin).resolve()
        # uv 安装路径: .../sensortower-st-cli/bin/python → .../lib/python3.*/site-packages/
        venv_root = st_script.parent.parent
        site_dirs = list(venv_root.glob("lib/python*/site-packages"))
        if not site_dirs:
            return False
        sp = str(site_dirs[0])
        if sp not in sys.path:
            sys.path.insert(0, sp)
        _ST_CLI_SITE_PKGS = sp
        _st_api_mod = importlib.import_module("st_cli.st_api")
        _st_auth_mod = importlib.import_module("st_cli.auth")
        _st_client_mod = importlib.import_module("st_cli.st_client")
        _st_constants_mod = importlib.import_module("st_cli.constants")
        return True
    except Exception as e:
        print(f"[st_client] failed to import st-cli internals: {e}")
        return False


def _get_st_http_client():
    """获取带 cookie 认证的 SensorTower httpx 客户端。"""
    if not _ensure_st_cli_imports():
        return None
    cred = _st_auth_mod.get_credential()
    if not cred or not cred.is_valid:
        print("[st_client] no valid ST credential")
        return None
    return _st_client_mod.create_st_client(cred.cookies)


def _entity_app_store_url(entity: dict[str, Any]) -> str:
    """从 ST entity/autocomplete 结果里尽量提取 iOS App Store 链接。"""
    for key in ("app_store_url", "store_url", "itunes_url", "url"):
        url = str(entity.get(key) or "").strip()
        if "apps.apple.com" in url or "itunes.apple.com" in url:
            return url
    for sub in entity.get("ios_apps") or []:
        if not isinstance(sub, dict):
            continue
        for key in ("app_store_url", "store_url", "itunes_url", "url"):
            url = str(sub.get(key) or "").strip()
            if "apps.apple.com" in url or "itunes.apple.com" in url:
                return url
        app_id = sub.get("id") or sub.get("app_id")
        if app_id:
            return f"https://apps.apple.com/us/app/id{app_id}"
    return ""


def _extract_ios_app_id(text: str) -> str:
    """从 App Store 链接里提取 iOS app id。"""
    match = re.search(r"/id(\d+)", str(text or ""))
    if match:
        return match.group(1)
    return ""


def _sensor_tower_search_url(name: str) -> str:
    """生成可点击的 ST 搜索入口；不猜测内部 app 详情页路径。"""
    text = str(name or "").strip()
    if not text:
        return ""
    origin = "https://app.sensortower.com"
    if _st_constants_mod is None:
        _ensure_st_cli_imports()
    if _st_constants_mod is not None:
        origin = getattr(_st_constants_mod, "ST_ORIGIN", origin) or origin
    return f"{origin.rstrip('/')}/search?search_term={quote(text)}"


def sensor_tower_search_url(name: str) -> str:
    """供报告/前端接口复用的 ST 搜索入口。"""
    return _sensor_tower_search_url(name)


def _st_is_transient_error(e: Exception) -> bool:
    """判断 ST 请求是否是网络/协议层临时错误，可以重试。"""
    msg = str(e).lower()
    return any(token in msg for token in (
        "unexpected_eof", "eof occurred", "ssl", "tls", "timeout", "timed out",
        "connection reset", "connection aborted", "remote protocol", "network",
        "502", "503", "504", "429",
    ))


def _st_friendly_error(e: Exception) -> str:
    """把 st-cli / SensorTower 底层异常转成可展示的业务提示。"""
    msg = str(e).lower()
    if _st_is_transient_error(e):
        return "SensorTower 连接临时中断，请检查本机网络后重试"
    if "login" in msg or "auth" in msg or "credential" in msg or "cookie" in msg or "未登录" in msg or "认证" in msg:
        return "SensorTower 登录状态失效，请在本机重新登录 st-cli"
    if "not found" in msg or "no such file" in msg:
        return "st-cli 不可用，请在运行 Lumon 的本机安装"
    return "SensorTower 状态检测失败，请检查本机 st-cli 状态"


def _st_call_with_retry(fn, label: str, *, attempts: int = 3):
    """对 st-cli 内部 HTTP 调用做轻量重试，避免一次 SSL EOF 让整次搜索失败。"""
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            return fn()
        except Exception as e:
            last_error = e
            if attempt >= attempts or not _st_is_transient_error(e):
                raise
            delay = min(0.8 * attempt, 2.4)
            print(f"[st_client] {label} transient error {attempt}/{attempts}: {e}; retry in {delay:.1f}s")
            time.sleep(delay)
    if last_error:
        raise last_error


def fetch_category_market_data(
    category_id: int,
    *,
    top_n: int = 30,
) -> dict[str, Any] | None:
    """查询指定 SensorTower 品类的市场聚合数据。

    返回: {product_count, revenue_sum, revenue_avg, downloads_sum,
           revenue_growth_pct, top_apps: [{name, revenue, downloads}, ...]}
    """
    if not _ensure_st_cli_imports():
        return None
    client = _get_st_http_client()
    if not client:
        return None

    try:
        today = date.today()
        month_start = date(today.year, today.month, 1)
        if today.month == 1:
            prev_start = date(today.year - 1, 12, 1)
        else:
            prev_start = date(today.year, today.month - 1, 1)
        prev_end = month_start - timedelta(days=1)

        csrf = _st_api_mod.get_csrf_token_for_top_apps_page(client)

        regions = ["US"]

        # 获取 top_apps 原始数据（含 unified_app_id + sub_app_ids）
        top_apps_params = {
            "os": "unified",
            "filters": {
                "measure": "revenue",
                "comparison_attribute": "absolute",
                "category": category_id,
                "devices": ["iphone", "ipad", "android"],
                "regions": regions,
                "start_date": prev_start.strftime("%Y-%m-%d"),
                "end_date": prev_end.strftime("%Y-%m-%d"),
                "time_range": "day",
            },
            "pagination": {"limit": top_n, "offset": 0},
            "data_model": _st_api_mod.DEFAULT_DATA_MODEL,
        }
        headers: dict[str, str] = dict(_st_api_mod.POST_JSON_HEADERS)
        if csrf:
            headers["x-csrf-token"] = csrf
        r = client.post("/api/unified/top_apps", json=top_apps_params, headers=headers)
        top_raw = r.json()
        items = top_raw.get("data", {}).get("apps_ids", []) if isinstance(top_raw, dict) else []

        # 提取 sub_app_ids 和 unified_app_ids 的映射
        app_ids: list[int | str] = []
        unified_to_sub: dict[str, list] = {}
        unified_ids: list[str] = []
        for it in items:
            if not isinstance(it, dict):
                continue
            uid = it.get("unified_app_id", "")
            subs = it.get("sub_app_ids") or []
            if uid:
                unified_ids.append(uid)
                unified_to_sub[uid] = subs
            for sid in subs:
                if sid is not None:
                    app_ids.append(sid)
        if not app_ids:
            return None

        orig_regions = _st_api_mod.DEFAULT_FACET_REGIONS
        _st_api_mod.DEFAULT_FACET_REGIONS = ["US"]
        try:
            facet_rows = _st_api_mod.apps_facets_v2_month_slice(
                client,
                app_ids=app_ids,
                month_start=prev_start,
                month_end=prev_end,
                comparison_start=prev_start - timedelta(days=30),
                comparison_end=prev_start - timedelta(days=1),
                csrf_token=csrf,
                limit=top_n + 5,
            )
        finally:
            _st_api_mod.DEFAULT_FACET_REGIONS = orig_regions

        revenue_sum = 0.0
        downloads_sum = 0.0
        growth_vals: list[float] = []
        product_count = 0
        top_apps: list[dict] = []

        for row in facet_rows:
            if row.get("appId") is not None:
                continue
            product_count += 1

            rev_raw = row.get("revenueAbsolute")
            rev = 0.0
            if rev_raw is not None and rev_raw != "":
                try:
                    rev = float(rev_raw) / 100.0
                except (ValueError, TypeError):
                    pass
            revenue_sum += rev

            dl_raw = row.get("downloadsAbsolute")
            dl = 0
            if dl_raw is not None and dl_raw != "":
                try:
                    dl = int(float(dl_raw))
                except (ValueError, TypeError):
                    pass
            downloads_sum += dl

            g = row.get("revenueGrowthPercent")
            g_pct = None
            if g is not None and g != "":
                try:
                    g_pct = float(g) * 100
                    growth_vals.append(g_pct)
                except (ValueError, TypeError):
                    pass

            dl_g = row.get("downloadsGrowthPercent")
            dl_g_pct = None
            if dl_g is not None and dl_g != "":
                try:
                    dl_g_pct = round(float(dl_g) * 100, 1)
                except (ValueError, TypeError):
                    pass

            dau_raw = row.get("activeUsersDAUAbsolute")
            dau = 0
            if dau_raw is not None and dau_raw != "":
                try:
                    dau = int(float(dau_raw))
                except (ValueError, TypeError):
                    pass

            top_apps.append({
                "name": "",
                "icon_url": "",
                "publisher": "",
                "_unified_id": row.get("unifiedAppId", ""),
                "revenue": round(rev, 2),
                "revenue_display": _format_currency(rev) if rev else "-",
                "downloads": dl,
                "downloads_display": _format_number(dl) if dl else "-",
                "growth_pct": round(g_pct, 1) if g_pct is not None else None,
                "downloads_growth_pct": dl_g_pct,
                "dau": dau,
                "dau_display": _format_number(dau) if dau else "-",
            })

        top_apps.sort(key=lambda x: x["revenue"], reverse=True)

        # 获取产品名称、icon（通过 internal_entities）
        if unified_ids:
            try:
                entities = _st_api_mod.internal_entities(
                    client, unified_ids[:top_n], csrf_token=csrf
                )
                uid_info: dict[str, dict] = {}
                for ent in entities:
                    eid = ent.get("id") or ent.get("app_id") or ""
                    uid_info[eid] = {
                        "name": ent.get("name") or ent.get("humanized_name") or "",
                        "publisher": ent.get("publisher_name") or "",
                        "icon_url": ent.get("icon_url") or "",
                    }
                # facet_rows 按 unifiedAppId 关联
                for app in top_apps:
                    uid = app.pop("_unified_id", "")
                    if uid and uid in uid_info:
                        info = uid_info[uid]
                        app["name"] = info["name"]
                        app["publisher"] = info["publisher"]
                        app["icon_url"] = info["icon_url"]
            except Exception as e:
                print(f"[st_client] internal_entities error: {e}")

        revenue_avg = revenue_sum / product_count if product_count else 0
        avg_growth = sum(growth_vals) / len(growth_vals) if growth_vals else 0

        final_top = []
        for a in top_apps[:5]:
            a.pop("_unified_id", None)
            final_top.append(a)

        return {
            "product_count": product_count,
            "revenue_sum": round(revenue_sum, 2),
            "revenue_avg": round(revenue_avg, 2),
            "downloads_sum": round(downloads_sum),
            "revenue_growth_pct": round(avg_growth, 1),
            "top_apps": final_top,
        }
    except Exception as e:
        print(f"[st_client] category market data error for {category_id}: {e}")
        return None
    finally:
        try:
            client.close()
        except Exception:
            pass


def _global_facet_regions() -> list[str]:
    """读取 st-cli 内置全球 region 列表，用于收入和下载指标口径。"""
    if _ensure_st_cli_imports() and _st_constants_mod is not None:
        regions = getattr(_st_constants_mod, "GLOBAL_FACET_REGIONS", None)
        if isinstance(regions, (list, tuple)) and regions:
            return list(regions)
    return list(getattr(_st_api_mod, "DEFAULT_FACET_REGIONS", []) or ["US"])


def _normalize_market_region(market_region: str = "") -> tuple[str, str, list[str]]:
    """竞品候选按指定市场理解，收入/下载指标固定查全球。"""
    candidate_label = str(market_region or "US").strip().upper() or "US"
    return candidate_label, "全球", _global_facet_regions()


def _shift_month_start(base: date, months: int) -> date:
    """返回 base 所在月份偏移 months 后的月初日期。"""
    month_index = base.year * 12 + (base.month - 1) + months
    year = month_index // 12
    month = month_index % 12 + 1
    return date(year, month, 1)


def _market_metrics_window(metrics_time_period: str = "30d") -> tuple[date, date, date, date, str, str]:
    """把前端时间口径转为 ST facets 的主窗口和对比窗口。"""
    today = date.today()
    current_month_start = date(today.year, today.month, 1)
    prev_end = current_month_start - timedelta(days=1)
    period = str(metrics_time_period or "30d").strip().lower()

    if period in {"6months", "6m", "half_year"}:
        window_start = _shift_month_start(current_month_start, -6)
        label = "过去 6 个月"
        normalized = "6months"
    elif period in {"all_time", "all", "lifetime", "cumulative"}:
        window_start = date(2012, 1, 1)
        label = "全部累计"
        normalized = "all_time"
    else:
        window_start = _shift_month_start(current_month_start, -1)
        label = "过去 30 天"
        normalized = "30d"

    window_days = max(30, (prev_end - window_start).days + 1)
    comparison_end = window_start - timedelta(days=1)
    comparison_days = 30 if normalized == "all_time" else window_days
    comparison_start = comparison_end - timedelta(days=comparison_days - 1)
    return window_start, prev_end, comparison_start, comparison_end, label, normalized


def _market_rolling_30d_window() -> tuple[date, date]:
    """返回与 Sensor Tower Summary「Last 30 Days」更接近的滚动 30 天窗口。"""
    # ST 网页 Summary 的 Last 30 Days 使用已完整入库的数据日；
    # 在本机亚洲时区下通常需要避开最近 1 个未稳定日。
    end = date.today() - timedelta(days=2)
    start = end - timedelta(days=29)
    return start, end


def _market_metric(app: dict, key: str) -> float:
    """读取 ST 指标数值，缺失或异常时按 0 处理。"""
    try:
        return float(app.get(key) or 0)
    except (TypeError, ValueError):
        return 0.0


def _market_has_size_signal(app: dict) -> bool:
    """有收入或下载估算，才适合作为赛道头部竞品展示。"""
    return _market_metric(app, "revenue") > 0 or _market_metric(app, "downloads") > 0


def _market_is_head_player(app: dict) -> bool:
    """赛道竞品默认只展示有一定规模的玩家，避免长尾小应用占位。"""
    return _market_metric(app, "revenue") >= 1_000 or _market_metric(app, "downloads") >= 500


def _market_size_score(app: dict) -> float:
    """综合收入和下载估算赛道规模，收入权重略高于下载。"""
    revenue = _market_metric(app, "revenue")
    downloads = _market_metric(app, "downloads")
    return math.log1p(revenue) * 1.45 + math.log1p(downloads)


def fetch_niche_market_data(
    queries: list[str],
    *,
    top_n: int = 20,
    market_region: str = "US",
    result_limit: int = 5,
    sort_by: str = "revenue",
    metrics_time_period: str = "30d",
    progress_callback: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any] | None:
    """用多个搜索关键词查找细分赛道的头部产品，然后聚合市场数据。

    流程：autocomplete_search 多词搜索 → 去重 → facets 月度数据 → 聚合 + Top5。
    返回格式与 fetch_category_market_data 一致。
    """
    if not _ensure_st_cli_imports():
        return None
    client = _get_st_http_client()
    if not client:
        return None

    global _LAST_NICHE_ERROR
    _LAST_NICHE_ERROR = ""
    transient_errors: list[str] = []

    try:
        window_start, window_end, comparison_start, comparison_end, period_label, normalized_period = _market_metrics_window(metrics_time_period)

        csrf = _st_call_with_retry(
            lambda: _st_api_mod.get_csrf_token_for_top_apps_page(client),
            "csrf",
        )

        # 1. 多关键词搜索，按 unified_app_id 去重
        seen_uids: set[str] = set()
        uid_matched_queries: dict[str, set[str]] = {}
        app_entries: list[dict] = []
        context_terms = _market_context_terms(queries)
        for q_index, q in enumerate(queries):
            try:
                if progress_callback:
                    progress_callback({
                        "phase": "autocomplete",
                        "current": q_index + 1,
                        "total": len(queries),
                        "query": q,
                    })
                results = _st_call_with_retry(
                    lambda q=q: _st_api_mod.autocomplete_search(client, q, limit=8),
                    f"autocomplete '{q}'",
                )
                filtered_names: list[str] = []
                for ent in results:
                    if not _is_st_autocomplete_match(q, ent):
                        filtered_names.append(str(ent.get("name") or ent.get("humanized_name") or ""))
                        continue
                    if context_terms and _market_needs_context_guard(q):
                        entity_terms = _market_match_terms(_st_entity_text(ent))
                        if not entity_terms & context_terms:
                            filtered_names.append(str(ent.get("name") or ent.get("humanized_name") or ""))
                            continue
                    if _market_generic_query_needs_context_guard(q, context_terms):
                        entity_terms = _market_match_terms(_st_entity_text(ent))
                        if not entity_terms & context_terms:
                            filtered_names.append(str(ent.get("name") or ent.get("humanized_name") or ""))
                            continue
                    uid = str(ent.get("id") or ent.get("app_id") or "")
                    if not uid or uid in seen_uids:
                        if uid:
                            uid_matched_queries.setdefault(uid, set()).add(q)
                        continue
                    seen_uids.add(uid)
                    uid_matched_queries.setdefault(uid, set()).add(q)
                    app_entries.append(ent)
                if filtered_names:
                    preview = ", ".join(name for name in filtered_names[:3] if name)
                    if len(filtered_names) > 3:
                        preview += " 等"
                    print(f"[st_client] autocomplete filtered query='{q}' count={len(filtered_names)} apps={preview}")
            except Exception as e:
                print(f"[st_client] autocomplete '{q}' error: {e}")
                if _st_is_transient_error(e):
                    transient_errors.append(str(e)[:200])
        if not app_entries:
            if transient_errors:
                _LAST_NICHE_ERROR = transient_errors[-1]
            return None

        # 2. 提取 sub_app_ids
        sub_app_ids: list[int | str] = []
        unified_ids: list[str] = []
        for ent in app_entries:
            uid = str(ent.get("id") or ent.get("app_id") or "")
            if uid:
                unified_ids.append(uid)
            for sub in ent.get("ios_apps", []) + ent.get("android_apps", []):
                sid = sub.get("id") or sub.get("app_id")
                if sid is not None:
                    sub_app_ids.append(sid)
        if not sub_app_ids:
            return None

        # 3. facets 月度数据：候选竞品按美区语境，收入/下载使用全球口径。
        candidate_region_label, metrics_region_label, facet_regions = _normalize_market_region(market_region)
        orig_regions = _st_api_mod.DEFAULT_FACET_REGIONS
        _st_api_mod.DEFAULT_FACET_REGIONS = facet_regions
        try:
            if progress_callback:
                progress_callback({
                    "phase": "facets",
                    "app_count": len(sub_app_ids),
                })
            facet_rows = _st_call_with_retry(
                lambda: _st_api_mod.apps_facets_v2_month_slice(
                    client,
                    app_ids=sub_app_ids[:60],
                    month_start=window_start,
                    month_end=window_end,
                    comparison_start=comparison_start,
                    comparison_end=comparison_end,
                    csrf_token=csrf,
                    limit=len(sub_app_ids) + 10,
                ),
                "apps facets",
            )
        finally:
            _st_api_mod.DEFAULT_FACET_REGIONS = orig_regions

        # 4. 只取汇总行 (appId=None) 聚合
        revenue_sum = 0.0
        downloads_sum = 0.0
        growth_vals: list[float] = []
        product_count = 0
        top_apps: list[dict] = []

        for row in facet_rows:
            if row.get("appId") is not None:
                continue
            product_count += 1

            rev_raw = row.get("revenueAbsolute")
            rev = 0.0
            if rev_raw is not None and rev_raw != "":
                try:
                    rev = float(rev_raw) / 100.0
                except (ValueError, TypeError):
                    pass
            revenue_sum += rev

            dl_raw = row.get("downloadsAbsolute")
            dl = 0
            if dl_raw is not None and dl_raw != "":
                try:
                    dl = int(float(dl_raw))
                except (ValueError, TypeError):
                    pass
            downloads_sum += dl

            g = row.get("revenueGrowthPercent")
            g_pct = None
            if g is not None and g != "":
                try:
                    g_pct = float(g) * 100
                    growth_vals.append(g_pct)
                except (ValueError, TypeError):
                    pass

            dl_g = row.get("downloadsGrowthPercent")
            dl_g_pct = None
            if dl_g is not None and dl_g != "":
                try:
                    dl_g_pct = round(float(dl_g) * 100, 1)
                except (ValueError, TypeError):
                    pass

            dau_raw = row.get("activeUsersDAUAbsolute")
            dau = 0
            if dau_raw is not None and dau_raw != "":
                try:
                    dau = int(float(dau_raw))
                except (ValueError, TypeError):
                    pass

            top_apps.append({
                "name": "",
                "icon_url": "",
                "publisher": "",
                "_unified_id": row.get("unifiedAppId", ""),
                "matched_queries": sorted(uid_matched_queries.get(str(row.get("unifiedAppId", "")), set()))[:4],
                "revenue": round(rev, 2),
                "revenue_display": _format_currency(rev) if rev else "-",
                "downloads": dl,
                "downloads_display": _format_number(dl) if dl else "-",
                "growth_pct": round(g_pct, 1) if g_pct is not None else None,
                "downloads_growth_pct": dl_g_pct,
                "dau": dau,
                "dau_display": _format_number(dau) if dau else "-",
            })

        if sort_by == "growth":
            top_apps.sort(
                key=lambda x: (
                    _market_metric(x, "revenue") >= 30_000 or _market_metric(x, "downloads") >= 10_000,
                    x.get("growth_pct") is not None,
                    float(x.get("growth_pct") or -999999),
                    _market_size_score(x),
                ),
                reverse=True,
            )
        elif sort_by == "downloads":
            top_apps.sort(
                key=lambda x: (
                    _market_has_size_signal(x),
                    _market_metric(x, "downloads"),
                    _market_metric(x, "revenue"),
                ),
                reverse=True,
            )
        elif sort_by == "scale":
            top_apps.sort(
                key=lambda x: (
                    _market_has_size_signal(x),
                    _market_size_score(x),
                    _market_metric(x, "revenue"),
                    _market_metric(x, "downloads"),
                ),
                reverse=True,
            )
        else:
            top_apps.sort(
                key=lambda x: (
                    _market_metric(x, "revenue"),
                    _market_metric(x, "downloads"),
                ),
                reverse=True,
            )

        # 5. 用 internal_entities 获取产品名称/icon
        #    先从 autocomplete 原始数据建 fallback 名称映射
        name_fallback: dict[str, dict] = {}
        for ent in app_entries:
            uid = str(ent.get("id") or ent.get("app_id") or "")
            fb_name = ent.get("name") or ent.get("humanized_name") or ""
            fb_icon = ent.get("icon_url") or ""
            fb_pub = ent.get("publisher_name") or ""
            if uid and fb_name:
                name_fallback[uid] = {
                    "name": fb_name,
                    "icon_url": fb_icon,
                    "publisher": fb_pub,
                    "app_store_url": _entity_app_store_url(ent),
                }

        facet_uids = list({a["_unified_id"] for a in top_apps if a.get("_unified_id")})
        if facet_uids:
            try:
                if progress_callback:
                    progress_callback({
                        "phase": "entities",
                        "app_count": len(facet_uids),
                    })
                entities = _st_call_with_retry(
                    lambda: _st_api_mod.internal_entities(
                        client, facet_uids[:30], csrf_token=csrf
                    ),
                    "internal entities",
                    attempts=2,
                )
                uid_info: dict[str, dict] = {}
                for ent in entities:
                    eid = str(ent.get("id") or ent.get("app_id") or "")
                    uid_info[eid] = {
                        "name": ent.get("name") or ent.get("humanized_name") or "",
                        "publisher": ent.get("publisher_name") or "",
                        "icon_url": ent.get("icon_url") or "",
                        "app_store_url": _entity_app_store_url(ent),
                    }
                for app in top_apps:
                    uid = app.get("_unified_id", "")
                    if uid and uid in uid_info:
                        info = uid_info[uid]
                        app["name"] = info["name"]
                        app["publisher"] = info["publisher"]
                        app["icon_url"] = info["icon_url"]
                        app["app_store_url"] = info["app_store_url"]
            except Exception as e:
                print(f"[st_client] internal_entities error: {e}")

        for app in top_apps:
            if not app.get("name"):
                uid = app.get("_unified_id", "")
                if uid and uid in name_fallback:
                    fb = name_fallback[uid]
                    app["name"] = fb["name"]
                    if not app.get("icon_url"):
                        app["icon_url"] = fb["icon_url"]
                    if not app.get("publisher"):
                        app["publisher"] = fb["publisher"]
                    if not app.get("app_store_url"):
                        app["app_store_url"] = fb.get("app_store_url", "")

            app["store_url"] = app.get("app_store_url", "")
            app["sensor_tower_url"] = _sensor_tower_search_url(app.get("name", ""))

        revenue_avg = revenue_sum / product_count if product_count else 0
        avg_growth = sum(growth_vals) / len(growth_vals) if growth_vals else 0

        measured_apps = [app for app in top_apps if _market_has_size_signal(app)]
        head_apps = [app for app in measured_apps if _market_is_head_player(app)]
        display_apps = head_apps or measured_apps or top_apps
        final_top = []
        for a in display_apps[:max(1, result_limit)]:
            a.pop("_unified_id", None)
            final_top.append(a)

        if progress_callback:
            progress_callback({"phase": "done"})

        return {
            "product_count": product_count,
            "revenue_sum": round(revenue_sum, 2),
            "revenue_avg": round(revenue_avg, 2),
            "downloads_sum": round(downloads_sum),
            "revenue_growth_pct": round(avg_growth, 1),
            "top_apps": final_top,
            "measured_product_count": len(measured_apps),
            "candidate_region": candidate_region_label,
            "metrics_region": metrics_region_label,
            "metrics_time_period": normalized_period,
            "market_region": metrics_region_label,
            "sort_by": sort_by,
            "date_range": {
                "start": window_start.isoformat(),
                "end": window_end.isoformat(),
                "label": period_label,
            },
        }
    except Exception as e:
        _LAST_NICHE_ERROR = str(e)[:200]
        print(f"[st_client] niche market data error: {e}")
        return None
    finally:
        try:
            client.close()
        except Exception:
            pass


_MARKET_DIRECT_METRIC_TERMS = (
    "收入", "营收", "下载", "月收入", "上个月", "上月", "last month", "revenue",
    "downloads", "download", "sales", "income", "how much",
)

_MARKET_DIRECT_LIST_TERMS = (
    "赛道", "竞品", "竞争", "对手", "有哪些", "排行", "排名", "榜单", "增长最快",
    "头部", "top", "ranking", "competitor", "competitors", "fastest",
)

_MARKET_DIRECT_APP_STOPWORDS = {
    "revenue", "revenues", "download", "downloads", "sales", "income", "last", "month",
    "monthly", "how", "much", "what", "was", "were", "is", "the", "for", "of", "in",
    "app", "apps", "application", "estimate", "estimated",
}

_MARKET_DIRECT_APP_BROAD_SINGLE_TERMS = {
    "bible", "christian", "prayer", "devotional", "religion", "religious",
    "fitness", "workout", "running", "runner", "health", "sleep", "meditation",
    "budget", "budgeting", "expense", "finance", "sobriety", "sober",
    "notes", "note", "journal", "todo", "productivity",
}


def _market_direct_app_query_name(query: str) -> str:
    """识别“某个 App 上个月收入/下载是多少”这类单 App 指标问题。"""
    text = str(query or "").strip()
    lower = text.lower()
    if not any(term in lower or term in text for term in _MARKET_DIRECT_METRIC_TERMS):
        return ""
    if any(term in lower or term in text for term in _MARKET_DIRECT_LIST_TERMS):
        return ""
    tokens = re.findall(r"[A-Za-z][A-Za-z0-9&+.'’:-]*", text)
    cleaned_tokens = [
        token.strip(" .,:;!?()[]{}")
        for token in tokens
        if token.lower().strip(" .,:;!?()[]{}") not in _MARKET_DIRECT_APP_STOPWORDS
    ]
    if not cleaned_tokens:
        return ""
    if len(cleaned_tokens) == 1:
        single = cleaned_tokens[0]
        if single.islower() and single.lower() in _MARKET_DIRECT_APP_BROAD_SINGLE_TERMS:
            return ""
    app_name = " ".join(cleaned_tokens[:7]).strip()
    if not app_name or app_name.lower() in {"app", "apps", "application"}:
        return ""
    return app_name[:80]


def _compact_name(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(name or "").lower())


def _market_ordered_terms(text: str) -> list[str]:
    """按原文顺序提取可用于查询扩展的英文词。"""
    seen: set[str] = set()
    terms: list[str] = []
    for term in re.findall(r"[a-zA-Z0-9]+", str(text or "").lower()):
        if term in _MARKET_QUERY_STOP_TERMS or len(term) < 3:
            continue
        normalized = term[:-1] if term.endswith("s") and len(term) > 4 else term
        if normalized in seen:
            continue
        seen.add(normalized)
        terms.append(normalized)
    return terms


_MARKET_APP_CATEGORY_LABELS = {
    6000: "business",
    6001: "weather",
    6002: "utilities",
    6003: "travel",
    6004: "sports",
    6005: "social networking",
    6006: "reference",
    6007: "productivity",
    6008: "photo video",
    6009: "news",
    6010: "navigation",
    6011: "music",
    6012: "lifestyle",
    6013: "health fitness",
    6014: "games",
    6015: "finance",
    6016: "entertainment",
    6017: "education",
    6018: "books",
    6020: "medical",
    6021: "magazines newspapers",
    6022: "catalogs",
    6023: "food drink",
    6024: "shopping",
    6026: "developer tools",
    6027: "graphics design",
}


def _market_category_terms_from_value(value: Any) -> list[str]:
    """把 ST 的 iOS 数字类目或 Android 字符串类目转为可搜索文本。"""
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        terms: list[str] = []
        for item in value:
            terms.extend(_market_category_terms_from_value(item))
        return terms
    if isinstance(value, dict):
        terms = []
        for key in ("name", "humanized_name", "slug", "category", "category_name", "id", "category_id", "categoryId"):
            terms.extend(_market_category_terms_from_value(value.get(key)))
        return terms
    try:
        category_id = int(value)
        if category_id in _MARKET_APP_CATEGORY_LABELS:
            return [_MARKET_APP_CATEGORY_LABELS[category_id]]
    except (TypeError, ValueError):
        pass
    text = re.sub(r"[_-]+", " ", str(value or "")).strip().lower()
    return [text] if text else []


def _market_entity_category_terms(entity: dict | None) -> list[str]:
    """从 ST autocomplete/fetch 实体里提取 App Store / Google Play 类目信号。"""
    if not isinstance(entity, dict):
        return []
    terms: list[str] = []
    for key in ("categories", "category", "primary_category", "game_category"):
        terms.extend(_market_category_terms_from_value(entity.get(key)))
    for sub in (entity.get("ios_apps") or []) + (entity.get("android_apps") or []):
        if isinstance(sub, dict):
            for key in ("categories", "category", "primary_category", "game_category"):
                terms.extend(_market_category_terms_from_value(sub.get(key)))
    return _dedupe_texts(terms, limit=10)


def _market_entity_app_names(entity: dict | None) -> list[str]:
    """从统一 App 的 iOS/Android 子包里提取产品名称，避免只看统一主名称。"""
    if not isinstance(entity, dict):
        return []
    names: list[str] = []
    for sub in (entity.get("ios_apps") or []) + (entity.get("android_apps") or []):
        if not isinstance(sub, dict):
            continue
        for key in ("name", "humanized_name"):
            name = re.sub(r"\s+", " ", str(sub.get(key) or "")).strip()
            if name:
                names.append(name)
    return _dedupe_texts(names, limit=8)


def _market_app_competitor_query_name(query: str) -> str:
    """识别“某个 App 的竞品/替代品有哪些”这类 App 锚点竞品问题。"""
    text = str(query or "").strip()
    lower = text.lower()
    if not any(term in lower or term in text for term in _MARKET_DIRECT_LIST_TERMS):
        return ""
    tokens = re.findall(r"[A-Za-z][A-Za-z0-9&+.'’:-]*", text)
    cleaned_tokens = [
        token.strip(" .,:;!?()[]{}")
        for token in tokens
        if token.lower().strip(" .,:;!?()[]{}") not in _MARKET_DIRECT_APP_STOPWORDS
        and token.lower().strip(" .,:;!?()[]{}") not in {
            "top", "ranking", "rank", "competitor", "competitors", "alternative", "alternatives",
            "similar", "like", "market", "category", "segment",
        }
    ]
    if not cleaned_tokens:
        return ""
    if len(cleaned_tokens) == 1:
        single = cleaned_tokens[0]
        if single.lower() in _MARKET_DIRECT_APP_BROAD_SINGLE_TERMS:
            return ""
    app_name = " ".join(cleaned_tokens[:7]).strip()
    if not app_name or app_name.lower() in {"app", "apps", "application"}:
        return ""
    return app_name[:80]


def _monthly_estimate_value(items: list[dict], month_key: str, value_key: str) -> float | None:
    for item in items or []:
        if not isinstance(item, dict):
            continue
        if str(item.get("month") or "") != month_key:
            continue
        try:
            return float(item.get(value_key) or 0)
        except (TypeError, ValueError):
            return None
    return None


def _market_result_from_direct_app(
    app: dict,
    *,
    direct_app_name: str,
    metrics_time_period: str = "30d",
) -> dict[str, Any]:
    """把 st fetch 单 App 结果转换成雷达搜索市场结果结构。"""
    snapshot_range = app.get("date_range") if isinstance(app.get("date_range"), dict) else None
    if snapshot_range:
        period_label = str(snapshot_range.get("label") or "过去 30 天")
        normalized_period = str(metrics_time_period or "30d")
        window_start_text = str(snapshot_range.get("start") or "")
        window_end_text = str(snapshot_range.get("end") or "")
    else:
        window_start, window_end, _, _, period_label, normalized_period = _market_metrics_window(metrics_time_period)
        window_start_text = window_start.isoformat()
        window_end_text = window_end.isoformat()
    if snapshot_range:
        revenue = app.get("revenue_snapshot")
        downloads = app.get("downloads_snapshot")
        mau = app.get("mau_snapshot")
    else:
        month_key = window_start.strftime("%Y-%m")
        revenue = _monthly_estimate_value(
            app.get("revenue_monthly_estimates") or [],
            month_key,
            "revenue_absolute_usd",
        )
        downloads = _monthly_estimate_value(
            app.get("downloads_monthly_estimates") or [],
            month_key,
            "downloads_absolute",
        )
        mau = _monthly_estimate_value(
            app.get("mau_monthly_estimates") or [],
            month_key,
            "mau_absolute",
        )
    if revenue is None:
        revenue = _market_metric(app, "revenue_last_month")
    if downloads is None:
        downloads = _market_metric(app, "downloads_last_month")
    top_app = {
        "name": app.get("name") or direct_app_name,
        "publisher": app.get("publisher") or "",
        "icon_url": app.get("icon_url") or "",
        "store_url": app.get("store_url") or app.get("app_store_url") or "",
        "app_store_url": app.get("app_store_url") or app.get("store_url") or "",
        "sensor_tower_url": app.get("sensor_tower_url") or _sensor_tower_search_url(app.get("name") or direct_app_name),
        "revenue": round(float(revenue or 0), 2),
        "revenue_display": _format_currency(float(revenue or 0)) if revenue else app.get("revenue_display", "-"),
        "downloads": int(float(downloads or 0)),
        "downloads_display": _format_number(float(downloads or 0)) if downloads else app.get("downloads_display", "-"),
        "growth_pct": app.get("growth_pct"),
        "downloads_growth_pct": app.get("downloads_growth_pct"),
        "dau": int(float(mau or 0)),
        "dau_display": _format_number(float(mau or 0)) if mau else "-",
        "release_date": app.get("release_date") or app.get("first_release_date_us") or "",
        "metric_source": app.get("_metric_source") or ("snapshot" if snapshot_range else "fetch"),
    }
    return {
        "available": True,
        "direct_app": True,
        "queries": [direct_app_name],
        "candidate_region": "US",
        "metrics_region": "全球",
        "metrics_time_period": normalized_period,
        "market_region": "全球",
        "sort_by": "revenue",
        "date_range": {
            "start": window_start_text,
            "end": window_end_text,
            "label": period_label,
        },
        "product_count": 1,
        "measured_product_count": 1 if _market_has_size_signal(top_app) else 0,
        "revenue_sum": top_app["revenue"],
        "revenue_avg": top_app["revenue"],
        "downloads_sum": top_app["downloads"],
        "revenue_growth_pct": None,
        "top_apps": [top_app],
    }


def _market_target_app_metric_row(app: dict, *, direct_app_name: str, metrics_time_period: str = "30d") -> dict[str, Any]:
    result = _market_result_from_direct_app(
        app,
        direct_app_name=direct_app_name,
        metrics_time_period=metrics_time_period,
    )
    target = dict((result.get("top_apps") or [{}])[0])
    target["is_target_app"] = True
    target["matched_queries"] = ["目标 App"]
    return target


def _market_app_competitor_terms(query: str, target_name: str, app: dict | None = None) -> tuple[set[str], set[str]]:
    """提取 App 锚点查询的核心词和更细的场景词，用于竞品重排。"""
    text = " ".join([
        str(query or ""),
        str(target_name or ""),
        str((app or {}).get("name") or ""),
        str((app or {}).get("publisher") or ""),
        " ".join(str(v) for v in (app or {}).get("category_terms") or []),
        " ".join(str(v) for v in (app or {}).get("platform_app_names") or []),
    ]).lower()
    terms = _market_match_terms(text)
    broad = {
        "bible", "christian", "prayer", "devotional", "religion", "religious",
        "fitness", "workout", "running", "health", "sleep", "meditation",
        "budget", "expense", "finance", "sobriety", "sober",
        "notes", "note", "journal", "todo", "productivity",
        "family", "safety", "location", "locator", "tracking", "tracker", "parental",
    }
    fine_terms = {
        term for term in terms
        if term not in broad and term not in _MARKET_QUERY_STOP_TERMS
    }
    if {"note", "notes", "journal", "sermon"} & terms:
        fine_terms.update({"note", "notes", "journal", "sermon"})
    if {"chat", "ai"} & terms:
        fine_terms.add("chat")
    if {"run", "running", "runner", "bike", "cycling", "walk", "marathon"} & terms:
        fine_terms.update({"run", "running", "runner", "bike", "cycling", "walk", "marathon", "training"})
    if {"study", "homework", "math", "solver", "tutor", "education", "learning", "quiz"} & terms:
        fine_terms.update({"study", "homework", "math", "solver", "tutor", "education", "learning", "quiz", "student"})
    if {"family", "safety", "location", "locator", "gps", "tracking", "tracker", "parental"} & terms:
        fine_terms.update({"family", "safety", "location", "locator", "gps", "tracking", "tracker", "parental"})
    return terms, fine_terms


def _market_app_competitor_queries(
    query: str,
    target_name: str,
    app: dict | None,
    search_queries: list[str] | None,
    *,
    limit: int = 14,
) -> list[str]:
    """围绕目标 App 生成同类竞品查询词，优先直接场景，其次成熟大盘。"""
    candidates: list[str] = []
    target_key = _compact_name(target_name)
    for raw in search_queries or []:
        text = re.sub(r"\s+", " ", str(raw or "")).strip()
        if not text:
            continue
        compact_text = _compact_name(text)
        if target_key and (compact_text == target_key or compact_text in target_key or target_key in compact_text):
            continue
        if re.search(r"[\u4e00-\u9fff]", text):
            continue
        candidates.append(text)

    name = str((app or {}).get("name") or target_name or "").strip()
    metadata_text = " ".join([
        str((app or {}).get("publisher") or ""),
        " ".join(str(v) for v in (app or {}).get("category_terms") or []),
        " ".join(str(v) for v in (app or {}).get("platform_app_names") or []),
    ])
    source = " ".join([str(query or ""), name, metadata_text]).lower()
    name_terms = _market_match_terms(" ".join([name, metadata_text]))

    if re.search(
        r"life360|family safety|family locator|location sharing|family tracking|"
        r"parental control|findmykids|geozilla|child location|children location|"
        r"phone tracker|family.*gps|gps.*family",
        source,
    ):
        candidates.extend([
            "iSharing GPS Location Tracker",
            "GeoZilla Phone Location Finder",
            "Find My Kids GPS Tracker",
            "Family 360 GPS Phone Tracker",
            "Google Family Link",
            "family locator app",
            "family safety app",
            "location sharing app",
            "GPS family tracker",
            "family tracking app",
            "phone tracker app",
            "child location tracker",
            "parental control app",
        ])
    elif "bible" in name_terms or re.search(r"圣经|基督|bible|christian|sermon|prayer|devotional", source):
        if re.search(r"笔记|note|notes|journal|sermon", source):
            candidates.extend([
                "Bible note app",
                "Bible journal app",
                "Bible study notes",
                "sermon notes app",
                "Christian notes app",
                "prayer journal app",
                "devotional journal app",
            ])
        candidates.extend([
            "Bible study app",
            "Bible app",
            "devotional app",
            "prayer app",
            "Christian app",
            "Bible Chat",
            "YouVersion Bible",
            "Glorify",
            "Hallow",
        ])
    elif "run" in name_terms or "running" in name_terms or re.search(r"跑步|running|runner|marathon", source):
        candidates.extend([
            "running app",
            "run tracker",
            "marathon training",
            "5K training app",
            "Strava",
            "Runna",
            "Nike Run Club",
            "Runkeeper",
        ])
    elif re.search(
        r"学习|作业|数学|拍照搜题|解题|教育|study|homework|math|solver|"
        r"ai study|ai tutor|education|learning|student|school|quiz|question",
        source,
    ):
        candidates.extend([
            "Gauth AI Study Companion",
            "Question AI",
            "Answer AI",
            "Photomath",
            "Mathway",
            "Symbolab",
            "Chegg Study",
            "Quizlet",
            "AI homework helper",
            "math solver app",
            "homework help app",
            "AI study app",
            "AI tutor app",
            "study tools app",
        ])
    elif "fitness" in name_terms or "workout" in name_terms or re.search(r"健身|fitness|workout|gym", source):
        candidates.extend([
            "fitness app",
            "workout app",
            "gym tracker",
            "strength training app",
            "Fitbod",
            "Nike Training Club",
            "MyFitnessPal",
        ])
    elif "budget" in name_terms or "expense" in name_terms or re.search(r"记账|预算|expense|budget|finance", source):
        candidates.extend([
            "budgeting app",
            "expense tracker",
            "personal finance app",
            "receipt scanner",
            "Monarch Money",
            "YNAB",
            "Rocket Money",
        ])
    else:
        words = [
            term for term in _market_ordered_terms(name)
            if term not in _MARKET_QUERY_STOP_TERMS and term not in {"free", "pro", "plus"}
        ]
        if words:
            phrase = " ".join(words[:3])
            candidates.extend([phrase, f"{phrase} app"])

    fine_markers = (
        "note", "notes", "journal", "sermon", "tracker", "training",
        "locator", "location", "gps", "family", "safety", "parental", "sharing",
        "run", "running", "runner", "bike", "cycling", "walk", "marathon",
        "study", "homework", "math", "solver", "tutor", "education", "learning", "quiz",
    )

    def _priority(item: tuple[int, str]) -> tuple[int, int]:
        index, text = item
        lower = text.lower()
        compact = _compact_name(text)
        generic_note_query = lower in {"note app", "notes app", "journal app", "writing app"}
        if target_key and (compact == target_key or target_key in compact or compact in target_key):
            return (0, index)
        if any(marker in lower for marker in fine_markers) and not generic_note_query:
            return (0, index)
        if any(marker in lower for marker in ("study", "chat", "devotional", "prayer")):
            return (1, index)
        return (2, index)

    ordered = [
        text
        for _, text in sorted(enumerate(candidates), key=_priority)
        if not (
            target_key
            and (compact := _compact_name(text))
            and (compact == target_key or compact in target_key or target_key in compact)
        )
    ]
    return _dedupe_texts(ordered, limit=limit)


def _market_compact_app_key(app: dict) -> str:
    return _compact_name(str(app.get("name") or ""))


def _market_rank_app_competitors(
    apps: list[dict],
    *,
    query: str,
    target_name: str,
    target_app: dict,
) -> list[dict]:
    """App 锚点竞品按“直接相关性优先、规模其次”排序，避免宽泛大盘产品淹没直接竞品。"""
    target_key = _compact_name(target_name) or _market_compact_app_key(target_app)
    _, fine_terms = _market_app_competitor_terms(query, target_name, target_app)

    def _score(app: dict) -> tuple[float, float, float]:
        app_key = _market_compact_app_key(app)
        if app_key and app_key == target_key:
            return (1000.0, _market_size_score(app), _market_metric(app, "revenue"))
        matched_text = " ".join(str(q or "") for q in app.get("matched_queries") or [])
        entity_text = " ".join([
            str(app.get("name") or ""),
            str(app.get("publisher") or ""),
            matched_text,
        ]).lower()
        entity_terms = _market_match_terms(entity_text)
        fine_hits = len(entity_terms & fine_terms)
        relevance = 0.0
        if fine_hits:
            relevance += 120.0 + fine_hits * 35.0
        if matched_text:
            relevance += 20.0
        if any(term in matched_text.lower() for term in ("note", "journal", "sermon", "tracker", "training")):
            relevance += 35.0
        matched_compact = _compact_name(matched_text)
        direct_query_markers = (
            "runna", "runkeeper", "nikerunclub",
            "geozilla", "isharing", "findmykids", "family360", "googlefamilylink",
            "biblechat", "youversion", "hallow", "glorify",
            "gauth", "questionai", "answerai", "photomath", "mathway", "symbolab", "cheggstudy", "quizlet",
            "monarchmoney", "ynab", "rocketmoney",
        )
        size_score = _market_size_score(app)
        direct_query_hit = any(marker in matched_compact for marker in direct_query_markers)
        if direct_query_hit:
            relevance += 105.0 + min(150.0, size_score * 3.5)
        else:
            relevance += min(35.0, size_score)
        return (relevance, size_score, _market_metric(app, "revenue"))

    deduped: list[dict] = []
    seen: set[str] = set()
    for app in apps:
        key = _market_compact_app_key(app)
        if key and key in seen:
            continue
        if key:
            seen.add(key)
        deduped.append(app)
    return sorted(deduped, key=_score, reverse=True)


def _market_result_from_app_competitors(
    query: str,
    *,
    direct_app_name: str,
    search_queries: list[str] | None,
    top_n: int,
    result_limit: int,
    market_region: str,
    metrics_time_period: str,
    progress_callback: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any] | None:
    """查询目标 App 自身数据，并围绕它扩展直接竞品。"""
    if progress_callback:
        progress_callback({"phase": "direct_app", "query": direct_app_name})
    target_app = None
    if str(metrics_time_period or "30d").strip().lower() in {"30d", "30days", "last30"}:
        start_date, end_date = _market_rolling_30d_window()
        target_app = fetch_app_snapshot(direct_app_name, start_date=start_date, end_date=end_date)
    if not target_app:
        target_app = fetch_app(direct_app_name)
    if not target_app:
        return None

    target_name = str(target_app.get("name") or direct_app_name).strip()
    target_row = _market_target_app_metric_row(
        target_app,
        direct_app_name=direct_app_name,
        metrics_time_period=metrics_time_period,
    )
    peer_queries = _market_app_competitor_queries(
        query,
        target_name,
        target_app,
        search_queries,
        limit=14,
    )
    if not peer_queries:
        return _market_result_from_direct_app(
            target_app,
            direct_app_name=direct_app_name,
            metrics_time_period=metrics_time_period,
        )

    market = fetch_niche_market_data(
        peer_queries,
        top_n=max(top_n, 30),
        market_region=market_region,
        result_limit=max(result_limit + 8, 16),
        sort_by="scale",
        metrics_time_period=metrics_time_period,
        progress_callback=progress_callback,
    )
    if not market:
        result = _market_result_from_direct_app(
            target_app,
            direct_app_name=direct_app_name,
            metrics_time_period=metrics_time_period,
        )
        result["direct_app_competitors"] = True
        result["target_app"] = target_row
        result["queries"] = peer_queries
        result["sort_by"] = "app_competitor"
        result["top_apps"] = [target_row]
        return result

    peer_apps = [
        app for app in (market.get("top_apps") or [])
        if _market_compact_app_key(app) != _compact_name(target_name)
    ]
    ranked_peers = _market_rank_app_competitors(
        peer_apps,
        query=query,
        target_name=target_name,
        target_app=target_app,
    )
    top_apps = [target_row] + ranked_peers[: max(0, result_limit - 1)]
    revenue_values = [float(app.get("revenue") or 0) for app in top_apps]
    downloads_values = [float(app.get("downloads") or 0) for app in top_apps]
    return {
        "available": True,
        "direct_app_competitors": True,
        "target_app": target_row,
        "queries": peer_queries,
        "candidate_region": market.get("candidate_region", "US"),
        "metrics_region": market.get("metrics_region", "全球"),
        "metrics_time_period": market.get("metrics_time_period", metrics_time_period),
        "market_region": market.get("market_region", market.get("metrics_region", "全球")),
        "sort_by": "app_competitor",
        "date_range": market.get("date_range") or target_app.get("date_range") or {},
        "product_count": market.get("product_count", len(top_apps)),
        "measured_product_count": len([app for app in top_apps if _market_has_size_signal(app)]),
        "revenue_sum": round(sum(revenue_values), 2),
        "revenue_avg": round(sum(revenue_values) / len(revenue_values), 2) if revenue_values else 0,
        "downloads_sum": round(sum(downloads_values)),
        "revenue_growth_pct": market.get("revenue_growth_pct"),
        "top_apps": top_apps,
    }


def search_market_apps(
    query: str,
    *,
    search_queries: list[str] | None = None,
    top_n: int = 20,
    result_limit: int = 8,
    market_region: str = "US",
    sort_by: str | None = None,
    metrics_time_period: str = "30d",
    progress_callback: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any] | None:
    """面向搜索引擎的 SensorTower 市场搜索，返回相关 App 与月度商业信号。"""
    topic = str(query or "").strip()
    if not topic:
        return None
    app_competitor_name = _market_app_competitor_query_name(topic)
    if app_competitor_name:
        result = _market_result_from_app_competitors(
            topic,
            direct_app_name=app_competitor_name,
            search_queries=search_queries or [],
            top_n=top_n,
            result_limit=result_limit,
            market_region=market_region,
            metrics_time_period=metrics_time_period,
            progress_callback=progress_callback,
        )
        if result:
            return result
    direct_app_name = _market_direct_app_query_name(topic)
    if direct_app_name:
        if progress_callback:
            progress_callback({"phase": "direct_app", "query": direct_app_name})
        app = None
        if str(metrics_time_period or "30d").strip().lower() in {"30d", "30days", "last30"}:
            start_date, end_date = _market_rolling_30d_window()
            app = fetch_app_snapshot(direct_app_name, start_date=start_date, end_date=end_date)
        elif not app:
            app = fetch_app(direct_app_name)
        if app:
            return _market_result_from_direct_app(
                app,
                direct_app_name=direct_app_name,
                metrics_time_period=metrics_time_period,
            )
        return None
    queries = _market_validation_queries(
        {"need_title": topic, "need_description": topic, "posts": []},
        topic=topic,
        search_queries=search_queries or [],
        max_queries=12,
    )
    if not queries:
        return None
    lower_query = topic.lower()
    if sort_by in {"revenue", "growth", "downloads", "scale"}:
        sort_by = sort_by
    elif any(token in lower_query for token in ("增长", "增速", "窜榜", "上升", "fastest", "growth", "growing", "rising")):
        sort_by = "growth"
    elif any(token in lower_query for token in ("下载", "download", "downloads", "install", "installs")):
        sort_by = "downloads"
    else:
        sort_by = "revenue"
    market = fetch_niche_market_data(
        queries,
        top_n=top_n,
        market_region=market_region,
        result_limit=result_limit,
        sort_by=sort_by,
        metrics_time_period=metrics_time_period,
        progress_callback=progress_callback,
    )
    if not market:
        error = "SensorTower 未匹配到相关 App"
        if _LAST_NICHE_ERROR:
            if _st_is_transient_error(Exception(_LAST_NICHE_ERROR)):
                error = "SensorTower 连接临时中断，请检查本机网络后重试"
            else:
                error = "SensorTower 查询失败，请检查本机 st-cli 登录状态"
        return {
            "available": False,
            "queries": queries,
            "top_apps": [],
            "candidate_region": "US",
            "metrics_region": "全球",
            "metrics_time_period": str(metrics_time_period or "30d"),
            "market_region": "全球",
            "error": error,
        }
    return {
        "available": True,
        "queries": queries,
        "candidate_region": market.get("candidate_region", "US"),
        "metrics_region": market.get("metrics_region", "全球"),
        "metrics_time_period": market.get("metrics_time_period", metrics_time_period),
        "market_region": market.get("market_region", market.get("metrics_region", "全球")),
        "sort_by": market.get("sort_by", sort_by),
        "date_range": market.get("date_range") or {},
        "product_count": market.get("product_count", 0),
        "measured_product_count": market.get("measured_product_count", 0),
        "revenue_sum": market.get("revenue_sum", 0),
        "revenue_avg": market.get("revenue_avg", 0),
        "downloads_sum": market.get("downloads_sum", 0),
        "revenue_growth_pct": market.get("revenue_growth_pct"),
        "top_apps": market.get("top_apps") or [],
    }


def check_available() -> dict:
    """检测 st-cli 是否安装且已认证。"""
    try:
        result = subprocess.run(
            ["st", "status", "--json"],
            capture_output=True, text=True, timeout=15,
        )
        data = json.loads(result.stdout.strip()) if result.stdout.strip() else {}
        ok = data.get("ok", False)
        inner = data.get("data", {}) or {}
        err_info = data.get("error", {}) or {}
        err_details = err_info.get("details", {}) or {}
        raw_error = str(err_info.get("message", "") or "")
        return {
            "installed": True,
            "available": ok,
            "api_ok": inner.get("api_ok", False) or err_details.get("api_ok", False),
            "credential_source": inner.get("credential_source", "") or err_details.get("credential_source", ""),
            "error": (_st_friendly_error(Exception(raw_error)) if raw_error else "") if not ok else "",
        }
    except FileNotFoundError:
        return {"installed": False, "available": False, "api_ok": False, "error": "st-cli 不可用，请在运行 Lumon 的本机安装"}
    except Exception as e:
        return {"installed": False, "available": False, "api_ok": False, "error": _st_friendly_error(e)}


def fetch_app(query: str) -> dict | None:
    """查询单个 App 的 SensorTower 数据。

    query 可以是 App 名称或 App Store URL。
    返回归一化后的 dict 或 None。
    """
    last_error = ""
    for attempt in range(1, 3):
        try:
            result = subprocess.run(
                ["st", "fetch", query, "--json"],
                capture_output=True, text=True, timeout=75,
            )
        except subprocess.TimeoutExpired as e:
            last_error = f"timeout: {e}"
            print(f"[st-cli] fetch timeout attempt={attempt}: {query}")
            if attempt < 2:
                time.sleep(1.2)
                continue
            return None
        except Exception as e:
            last_error = str(e)
            print(f"[st-cli] fetch error: {e}")
            if attempt < 2 and _st_is_transient_error(e):
                time.sleep(1.2)
                continue
            return None

        if result.returncode != 0:
            last_error = result.stderr[:300]
            print(f"[st-cli] fetch failed attempt={attempt}: {result.stderr[:200]}")
            if attempt < 2 and _st_is_transient_error(Exception(result.stderr)):
                time.sleep(1.2)
                continue
            return None

        data = json.loads(result.stdout.strip())
        if not data.get("ok"):
            return None

        inner = data.get("data", {})

        if inner.get("needs_disambiguation"):
            candidates = inner.get("candidates", [])
            if candidates:
                picked = _fetch_app_with_pick(query, pick=1)
                if picked:
                    return picked
                app = _normalize_app(candidates[0])
                app["_metric_source"] = "autocomplete_disambiguation_fallback"
                return app
            return None

        selected = inner.get("selected")
        if selected:
            app = _normalize_app(selected)
            app["first_release_date_us"] = inner.get("first_release_date_us", "")
            app["revenue_monthly_estimates"] = (inner.get("revenue") or {}).get("monthly_estimates") or []
            app["downloads_monthly_estimates"] = (inner.get("downloads") or {}).get("monthly_estimates") or []
            app["mau_monthly_estimates"] = (inner.get("mau") or {}).get("monthly_estimates") or []
            return app

        return None
    print(f"[st-cli] fetch failed after retry: {last_error[:200]}")
    return None


def _fetch_app_with_pick(query: str, *, pick: int = 1) -> dict | None:
    """在 st fetch 返回歧义候选时，指定候选序号再拉一次详细指标。"""
    try:
        result = subprocess.run(
            ["st", "fetch", query, "--pick", str(pick), "--json"],
            capture_output=True, text=True, timeout=75,
        )
    except Exception as e:
        print(f"[st-cli] fetch pick error: {e}")
        return None
    if result.returncode != 0:
        print(f"[st-cli] fetch pick failed: {result.stderr[:200]}")
        return None
    try:
        data = json.loads(result.stdout.strip())
    except Exception:
        return None
    if not data.get("ok"):
        return None
    inner = data.get("data", {}) or {}
    selected = inner.get("selected")
    if not selected:
        return None
    app = _normalize_app(selected)
    app["first_release_date_us"] = inner.get("first_release_date_us", "")
    app["revenue_monthly_estimates"] = (inner.get("revenue") or {}).get("monthly_estimates") or []
    app["downloads_monthly_estimates"] = (inner.get("downloads") or {}).get("monthly_estimates") or []
    app["mau_monthly_estimates"] = (inner.get("mau") or {}).get("monthly_estimates") or []
    app["_metric_source"] = f"fetch_pick_{pick}"
    return app


def fetch_app_snapshot(query: str, *, start_date: date, end_date: date, app_store_url: str = "") -> dict | None:
    """查询单个 App 在指定时间窗口内的精确收入/下载快照。

    st-cli 的 snapshot/fetch 目前只会从候选里取第一个平台 id；这里直接用
    autocomplete 候选里的 iOS + Android sub app ids，保持和 ST 官网 Unified Summary 一致。
    """
    if not _ensure_st_cli_imports():
        return None
    client = _get_st_http_client()
    if not client:
        return None
    try:
        candidates = _st_call_with_retry(
            lambda: _st_api_mod.autocomplete_search(client, query, limit=8),
            f"snapshot autocomplete '{query}'",
            attempts=2,
        )
        if not candidates:
            return None
        ios_app_id = _extract_ios_app_id(app_store_url or query)
        selected = None
        if ios_app_id:
            for candidate in candidates:
                if not isinstance(candidate, dict):
                    continue
                for sub in candidate.get("ios_apps") or []:
                    if not isinstance(sub, dict):
                        continue
                    sid = str(sub.get("id") or sub.get("app_id") or "")
                    if sid == ios_app_id:
                        selected = candidate
                        break
                if selected:
                    break
        if not selected:
            selected = next(
                (candidate for candidate in candidates if isinstance(candidate, dict) and _is_st_direct_app_match(query, candidate)),
                None,
            )
        if not selected:
            print(f"[st_client] direct app autocomplete has no strict match: query={query!r}")
            return None
        sub_app_ids: list[int | str] = []
        for sub in (selected.get("ios_apps") or []) + (selected.get("android_apps") or []):
            if not isinstance(sub, dict):
                continue
            sid = sub.get("id") or sub.get("app_id")
            if sid is not None and sid not in sub_app_ids:
                sub_app_ids.append(sid)
        if not sub_app_ids:
            fallback = selected.get("app_id") or selected.get("id")
            if fallback is not None:
                sub_app_ids.append(fallback)
        if not sub_app_ids:
            return None

        csrf = _st_call_with_retry(
            lambda: _st_api_mod.get_csrf_token_for_top_apps_page(client),
            "snapshot csrf",
            attempts=2,
        )
        comparison_end = start_date - timedelta(days=1)
        comparison_start = comparison_end - timedelta(days=max(1, (end_date - start_date).days + 1) - 1)
        orig_regions = _st_api_mod.DEFAULT_FACET_REGIONS
        _st_api_mod.DEFAULT_FACET_REGIONS = _global_facet_regions()
        try:
            rows = _st_call_with_retry(
                lambda: _st_api_mod.apps_facets_v2_month_slice(
                    client,
                    app_ids=sub_app_ids,
                    month_start=start_date,
                    month_end=end_date,
                    comparison_start=comparison_start,
                    comparison_end=comparison_end,
                    csrf_token=csrf,
                    limit=len(sub_app_ids) + 5,
                ),
                "snapshot facets",
            )
        finally:
            _st_api_mod.DEFAULT_FACET_REGIONS = orig_regions

        revenue = _st_api_mod.extract_revenue_absolute_from_facets_v2_rows(rows)
        downloads = _st_api_mod.extract_downloads_absolute_from_facets_v2_rows(rows)
        mau = _st_api_mod.extract_mau_absolute_from_facets_v2_rows(rows)
        growth_pct = None
        downloads_growth_pct = None
        first_release = ""
        for row in rows:
            if row.get("appId") is not None:
                continue
            first_release = _st_api_mod.extract_first_release_date_us_from_facets_v2_rows(rows) or ""
            g = row.get("revenueGrowthPercent")
            if g is not None and g != "":
                try:
                    growth_pct = round(float(g) * 100, 1)
                except (TypeError, ValueError):
                    pass
            dg = row.get("downloadsGrowthPercent")
            if dg is not None and dg != "":
                try:
                    downloads_growth_pct = round(float(dg) * 100, 1)
                except (TypeError, ValueError):
                    pass
            break

        return {
            "name": selected.get("name") or selected.get("humanized_name") or query,
            "publisher": selected.get("publisher_name", ""),
            "revenue_snapshot": revenue,
            "downloads_snapshot": downloads,
            "mau_snapshot": mau,
            "growth_pct": growth_pct,
            "downloads_growth_pct": downloads_growth_pct,
            "icon_url": selected.get("icon_url", ""),
            "app_store_url": _entity_app_store_url(selected),
            "store_url": _entity_app_store_url(selected),
            "sensor_tower_url": _sensor_tower_search_url(selected.get("name") or selected.get("humanized_name") or query),
            "release_date": selected.get("release_date", ""),
            "first_release_date_us": first_release,
            "category_terms": _market_entity_category_terms(selected),
            "platform_app_names": _market_entity_app_names(selected),
            "date_range": {
                "start": start_date.isoformat(),
                "end": end_date.isoformat(),
                "label": "过去 30 天",
            },
            "_metric_source": "unified_sub_apps_snapshot",
        }
    except Exception as e:
        print(f"[st_client] unified snapshot error: {e}")
        return None
    finally:
        try:
            client.close()
        except Exception:
            pass


def _format_rpd(value: float | None) -> str:
    """格式化 RPD（Revenue Per Download）为美元小数。"""
    if value is None:
        return "-"
    if value >= 10:
        return f"${value:.1f}"
    if value >= 1:
        return f"${value:.2f}"
    return f"${value:.3f}"


def fetch_apps_rpd(
    apps: list[dict],
    *,
    start_date: date | None = None,
    end_date: date | None = None,
    limit: int = 8,
) -> dict[str, Any]:
    """批量查询 App 过去 30 天收入/下载并计算 RPD。

    apps: [{"name": "Bible Chat", "url": "https://..."}, ...]
    返回每个 App 的收入、下载量、RPD 和基础跳转链接。
    """
    if end_date is None:
        end_date = date.today() - timedelta(days=1)
    if start_date is None:
        start_date = end_date - timedelta(days=29)

    entries = [app for app in apps[:limit] if isinstance(app, dict) and (app.get("name") or app.get("query") or app.get("url") or app.get("app_store_url"))]
    results: list[dict[str, Any]] = []
    for item in entries:
        query = str(item.get("query") or item.get("name") or item.get("url") or item.get("app_store_url") or "").strip()
        url = str(item.get("url") or item.get("app_store_url") or "").strip()
        if not query:
            continue
        snapshot = fetch_app_snapshot(query, start_date=start_date, end_date=end_date, app_store_url=url)
        if not snapshot:
            results.append({
                "query": query,
                "name": item.get("name") or query,
                "ok": False,
                "error": "未匹配到稳定的 SensorTower App 数据",
            })
            continue

        revenue = snapshot.get("revenue_snapshot")
        downloads = snapshot.get("downloads_snapshot")
        rpd = None
        try:
            revenue_num = float(revenue or 0)
            downloads_num = float(downloads or 0)
            if downloads_num > 0:
                rpd = revenue_num / downloads_num
        except (TypeError, ValueError):
            rpd = None

        results.append({
            "query": query,
            "ok": True,
            "name": snapshot.get("name") or item.get("name") or query,
            "publisher": snapshot.get("publisher") or "",
            "revenue": round(float(revenue or 0), 2),
            "revenue_display": _format_currency(float(revenue or 0)) if revenue else "-",
            "downloads": int(float(downloads or 0)),
            "downloads_display": _format_number(float(downloads or 0)) if downloads else "-",
            "rpd": round(rpd, 4) if rpd is not None else None,
            "rpd_display": _format_rpd(rpd),
            "mau": int(float(snapshot.get("mau_snapshot") or 0)),
            "mau_display": _format_number(float(snapshot.get("mau_snapshot") or 0)) if snapshot.get("mau_snapshot") else "-",
            "revenue_growth_pct": snapshot.get("growth_pct"),
            "downloads_growth_pct": snapshot.get("downloads_growth_pct"),
            "app_store_url": snapshot.get("app_store_url") or snapshot.get("store_url") or "",
            "sensor_tower_url": snapshot.get("sensor_tower_url") or _sensor_tower_search_url(snapshot.get("name") or query),
            "date_range": snapshot.get("date_range") or {
                "start": start_date.isoformat(),
                "end": end_date.isoformat(),
                "label": "过去 30 天",
            },
            "_metric_source": snapshot.get("_metric_source") or "snapshot",
        })

    return {
        "date_range": {
            "start": start_date.isoformat(),
            "end": end_date.isoformat(),
            "label": "过去 30 天",
        },
        "metric": "RPD",
        "definition": "Revenue Per Download = 过去 30 天收入 / 过去 30 天下载量",
        "items": results,
    }


def _facet_money(row: dict[str, Any], key: str) -> float:
    value = row.get(key)
    if value is None or value == "":
        return 0.0
    try:
        return float(value) / 100.0
    except (TypeError, ValueError):
        return 0.0


def _facet_number(row: dict[str, Any], key: str) -> float:
    value = row.get(key)
    if value is None or value == "":
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _facet_growth_pct(row: dict[str, Any], key: str) -> float | None:
    value = row.get(key)
    if value is None or value == "":
        return None
    try:
        return round(float(value) * 100, 1)
    except (TypeError, ValueError):
        return None


def _platform_from_sub_app_id(app_id: Any) -> str:
    text = str(app_id or "")
    return "ios" if text.isdigit() else "android"


def _trend_flags(row: dict[str, Any]) -> list[str]:
    """基于当前窗口 vs 对比窗口，为收入/新增的大幅变化打标。"""
    flags: list[str] = []
    revenue_delta = _facet_money(row, "revenueDelta")
    downloads_delta = _facet_number(row, "downloadsDelta")
    revenue_growth = _facet_growth_pct(row, "revenueGrowthPercent")
    downloads_growth = _facet_growth_pct(row, "downloadsGrowthPercent")

    if revenue_growth is not None:
        if revenue_growth >= 30 and revenue_delta >= 10_000:
            flags.append("收入显著增长")
        elif revenue_growth <= -30 and abs(revenue_delta) >= 10_000:
            flags.append("收入显著下滑")
    if downloads_growth is not None:
        if downloads_growth >= 30 and downloads_delta >= 10_000:
            flags.append("新增显著增长")
        elif downloads_growth <= -30 and abs(downloads_delta) >= 10_000:
            flags.append("新增显著下滑")
    return flags


def _normalize_facet_segment(
    row: dict[str, Any],
    *,
    app_name: str,
    region: str,
    platform: str,
) -> dict[str, Any]:
    revenue = _facet_money(row, "revenueAbsolute")
    revenue_previous = _facet_money(row, "revenueAbsolutePrevious")
    revenue_delta = _facet_money(row, "revenueDelta")
    downloads = _facet_number(row, "downloadsAbsolute")
    downloads_previous = _facet_number(row, "downloadsAbsolutePrevious")
    downloads_delta = _facet_number(row, "downloadsDelta")
    dau = _facet_number(row, "activeUsersDAUAbsolute")
    mau = _facet_number(row, "activeUsersMAUAbsolute")
    rpd = revenue / downloads if downloads > 0 else None
    return {
        "app": app_name,
        "region": region,
        "platform": platform,
        "revenue": round(revenue, 2),
        "revenue_display": _format_currency(revenue) if revenue else "-",
        "revenue_previous": round(revenue_previous, 2),
        "revenue_previous_display": _format_currency(revenue_previous) if revenue_previous else "-",
        "revenue_delta": round(revenue_delta, 2),
        "revenue_delta_display": _format_currency(abs(revenue_delta)) if revenue_delta else "-",
        "revenue_growth_pct": _facet_growth_pct(row, "revenueGrowthPercent"),
        "downloads": int(downloads),
        "downloads_display": _format_number(downloads) if downloads else "-",
        "downloads_previous": int(downloads_previous),
        "downloads_previous_display": _format_number(downloads_previous) if downloads_previous else "-",
        "downloads_delta": int(downloads_delta),
        "downloads_delta_display": _format_number(abs(downloads_delta)) if downloads_delta else "-",
        "downloads_growth_pct": _facet_growth_pct(row, "downloadsGrowthPercent"),
        "rpd": round(rpd, 4) if rpd is not None else None,
        "rpd_display": _format_rpd(rpd),
        "rpd_60d": round(rpd, 4) if rpd is not None else None,
        "rpd_60d_display": _format_rpd(rpd),
        "rpd_all_time_us": str(row.get("rpd") or "").strip() or "-",
        "dau": int(dau),
        "dau_display": _format_number(dau) if dau else "-",
        "mau": int(mau),
        "mau_display": _format_number(mau) if mau else "-",
        "flags": _trend_flags(row),
        "website_url": str(row.get("websiteUrl") or "").strip(),
    }


def fetch_apps_country_platform_trends(
    apps: list[dict],
    *,
    regions: list[str] | None = None,
    days: int = 60,
    start_date: date | None = None,
    end_date: date | None = None,
    comparison_start_date: date | None = None,
    comparison_end_date: date | None = None,
    limit: int = 6,
) -> dict[str, Any]:
    """批量查看 App 在不同国家和平台的收入/新增趋势。

    口径：当前周期 vs 对比周期；新增使用 downloads。
    """
    if not _ensure_st_cli_imports():
        return {"available": False, "error": "st-cli 内部模块不可用", "items": []}
    client = _get_st_http_client()
    if not client:
        return {"available": False, "error": "SensorTower 登录状态不可用", "items": []}

    region_list = [
        re.sub(r"[^A-Za-z]", "", str(region or "")).upper()
        for region in (regions or ["US", "PH", "BR"])
    ]
    region_list = [region for region in _dedupe_texts(region_list, limit=8) if len(region) == 2]
    if not region_list:
        region_list = ["US"]
    if end_date is None:
        end_date = date.today() - timedelta(days=1)
    if start_date is None:
        days = max(7, min(int(days or 60), 365))
        start_date = end_date - timedelta(days=days - 1)
    if start_date > end_date:
        return {"available": False, "error": "开始日期不能晚于结束日期", "items": []}
    period_days = (end_date - start_date).days + 1
    if period_days < 7 or period_days > 365:
        return {"available": False, "error": "查询周期范围需为 7-365 天", "items": []}
    comparison_end = comparison_end_date or (start_date - timedelta(days=1))
    comparison_start = comparison_start_date or (comparison_end - timedelta(days=period_days - 1))
    if comparison_start > comparison_end:
        return {"available": False, "error": "对比周期开始日期不能晚于结束日期", "items": []}

    try:
        app_records: list[dict[str, Any]] = []
        unified_to_app: dict[str, dict[str, Any]] = {}
        sub_id_to_platform: dict[str, tuple[str, str]] = {}
        all_sub_ids: list[int | str] = []

        for item in apps[:limit]:
            query = str(item.get("query") or item.get("name") or item.get("url") or item.get("app_store_url") or "").strip()
            url = str(item.get("url") or item.get("app_store_url") or "").strip()
            if not query:
                continue
            candidates = _st_call_with_retry(
                lambda q=query: _st_api_mod.autocomplete_search(client, q, limit=8),
                f"trends autocomplete '{query}'",
                attempts=2,
            )
            selected = None
            ios_app_id = _extract_ios_app_id(url or query)
            if ios_app_id:
                for candidate in candidates or []:
                    if not isinstance(candidate, dict):
                        continue
                    for sub in candidate.get("ios_apps") or []:
                        sid = str(sub.get("id") or sub.get("app_id") or "")
                        if sid == ios_app_id:
                            selected = candidate
                            break
                    if selected:
                        break
            if not selected:
                selected = next(
                    (candidate for candidate in candidates or [] if isinstance(candidate, dict) and _is_st_direct_app_match(query, candidate)),
                    None,
                )
            if not selected and candidates:
                selected = next((candidate for candidate in candidates if isinstance(candidate, dict)), None)
            if not selected:
                app_records.append({"query": query, "ok": False, "error": "未匹配到 App"})
                continue

            unified_id = str(selected.get("id") or selected.get("app_id") or "")
            app_name = selected.get("name") or selected.get("humanized_name") or query
            record = {
                "query": query,
                "ok": True,
                "_input_order": len(app_records),
                "name": app_name,
                "publisher": selected.get("publisher_name") or "",
                "app_store_url": _entity_app_store_url(selected),
                "sensor_tower_url": _sensor_tower_search_url(app_name),
                "segments": [],
            }
            app_records.append(record)
            if unified_id:
                unified_to_app[unified_id] = record

            for sub in (selected.get("ios_apps") or []) + (selected.get("android_apps") or []):
                if not isinstance(sub, dict):
                    continue
                sid = sub.get("id") or sub.get("app_id")
                if sid is None:
                    continue
                platform = str(sub.get("os") or _platform_from_sub_app_id(sid)).lower()
                sid_key = str(sid)
                sub_id_to_platform[sid_key] = (unified_id, "ios" if platform == "ios" else "android")
                if sid not in all_sub_ids:
                    all_sub_ids.append(sid)

        if not all_sub_ids:
            return {
                "available": True,
                "date_range": {"start": start_date.isoformat(), "end": end_date.isoformat(), "label": f"{period_days} 天"},
                "comparison_range": {"start": comparison_start.isoformat(), "end": comparison_end.isoformat(), "label": f"对比 {period_days} 天"},
                "regions": region_list,
                "items": app_records,
                "table_rows": [],
                "highlights": [],
            }

        csrf = _st_call_with_retry(lambda: _st_api_mod.get_csrf_token_for_top_apps_page(client), "trends csrf", attempts=2)
        highlights: list[dict[str, Any]] = []
        for region in region_list:
            orig_regions = _st_api_mod.DEFAULT_FACET_REGIONS
            _st_api_mod.DEFAULT_FACET_REGIONS = [region]
            try:
                rows = _st_call_with_retry(
                    lambda r=region: _st_api_mod.apps_facets_v2_month_slice(
                        client,
                        app_ids=all_sub_ids,
                        month_start=start_date,
                        month_end=end_date,
                        comparison_start=comparison_start,
                        comparison_end=comparison_end,
                        csrf_token=csrf,
                        limit=len(all_sub_ids) + len(app_records) + 10,
                    ),
                    f"trends facets {region}",
                    attempts=2,
                )
            finally:
                _st_api_mod.DEFAULT_FACET_REGIONS = orig_regions

            for row in rows or []:
                unified_id = str(row.get("unifiedAppId") or "")
                app_id = row.get("appId")
                if app_id is None:
                    record = unified_to_app.get(unified_id)
                    platform = "all"
                else:
                    mapped = sub_id_to_platform.get(str(app_id))
                    if not mapped:
                        continue
                    record = unified_to_app.get(mapped[0])
                    platform = mapped[1]
                if not record:
                    continue
                segment = _normalize_facet_segment(row, app_name=record["name"], region=region, platform=platform)
                record["segments"].append(segment)
                for flag in segment["flags"]:
                    highlights.append({
                        "app": record["name"],
                        "region": region,
                        "platform": platform,
                        "flag": flag,
                        "revenue_growth_pct": segment["revenue_growth_pct"],
                        "downloads_growth_pct": segment["downloads_growth_pct"],
                        "revenue_delta_display": segment["revenue_delta_display"],
                        "downloads_delta_display": segment["downloads_delta_display"],
                    })

        platform_order = {"all": 0, "ios": 1, "android": 2}
        table_rows: list[dict[str, Any]] = []
        for record in app_records:
            if not record.get("ok"):
                continue
            for segment in record.get("segments", []):
                table_rows.append({
                    "app_order": record.get("_input_order", 0),
                    "app": record.get("name") or segment.get("app"),
                    "publisher": record.get("publisher") or "",
                    "region": segment.get("region"),
                    "platform": segment.get("platform"),
                    "revenue": segment.get("revenue"),
                    "revenue_display": segment.get("revenue_display"),
                    "revenue_previous": segment.get("revenue_previous"),
                    "revenue_previous_display": segment.get("revenue_previous_display"),
                    "revenue_growth_pct": segment.get("revenue_growth_pct"),
                    "downloads": segment.get("downloads"),
                    "downloads_display": segment.get("downloads_display"),
                    "downloads_previous": segment.get("downloads_previous"),
                    "downloads_previous_display": segment.get("downloads_previous_display"),
                    "downloads_growth_pct": segment.get("downloads_growth_pct"),
                    "rpd": segment.get("rpd"),
                    "rpd_display": segment.get("rpd_display"),
                    "rpd_60d": segment.get("rpd_60d"),
                    "rpd_60d_display": segment.get("rpd_60d_display"),
                    "flags": segment.get("flags") or [],
                    "app_store_url": record.get("app_store_url") or "",
                    "sensor_tower_url": record.get("sensor_tower_url") or "",
                })
        table_rows.sort(key=lambda row: (
            int(row.get("app_order") or 0),
            str(row.get("region") or ""),
            platform_order.get(str(row.get("platform") or ""), 9),
        ))

        return {
            "available": True,
            "date_range": {"start": start_date.isoformat(), "end": end_date.isoformat(), "label": f"{period_days} 天"},
            "comparison_range": {"start": comparison_start.isoformat(), "end": comparison_end.isoformat(), "label": f"对比 {period_days} 天"},
            "regions": region_list,
            "items": app_records,
            "table_rows": table_rows,
            "highlights": highlights,
        }
    except Exception as e:
        print(f"[st_client] country/platform trends error: {e}")
        return {"available": False, "error": _st_friendly_error(e), "items": []}
    finally:
        try:
            client.close()
        except Exception:
            pass


def fetch_apps_revenue_download_timeseries(
    apps: list[dict],
    *,
    regions: list[str] | None = None,
    days: int = 60,
    start_date: date | None = None,
    end_date: date | None = None,
    granularity: str = "week",
    metrics: list[str] | None = None,
    limit: int = 6,
) -> dict[str, Any]:
    """批量查看 App 按日/周拆分的收入和新增下载时间序列。"""
    if not _ensure_st_cli_imports():
        return {"available": False, "error": "st-cli 内部模块不可用", "series": []}
    client = _get_st_http_client()
    if not client:
        return {"available": False, "error": "SensorTower 登录状态不可用", "series": []}

    granularity = str(granularity or "week").lower()
    if granularity not in {"day", "week", "month"}:
        granularity = "week"
    max_days = 120 if granularity == "day" else 365
    metric_set = {
        str(metric or "").lower()
        for metric in (metrics or ["revenue", "downloads", "rpd"])
        if str(metric or "").lower() in {"revenue", "downloads", "rpd"}
    }
    if not metric_set:
        metric_set = {"revenue"}
    needs_revenue = "revenue" in metric_set or "rpd" in metric_set
    needs_downloads = "downloads" in metric_set or "rpd" in metric_set

    if end_date is None:
        end_date = date.today() - timedelta(days=1)
    if start_date is None:
        days = max(7, min(int(days or 60), max_days))
        start_date = end_date - timedelta(days=days - 1)
    if start_date > end_date:
        return {"available": False, "error": "开始日期不能晚于结束日期", "series": []}
    period_days = (end_date - start_date).days + 1
    if period_days < 7 or period_days > max_days:
        return {"available": False, "error": f"{granularity} 粒度查询周期范围需为 7-{max_days} 天", "series": []}

    region_list = [
        re.sub(r"[^A-Za-z]", "", str(region or "")).upper()
        for region in (regions or ["US"])
    ]
    region_list = [region for region in _dedupe_texts(region_list, limit=6) if len(region) == 2]
    if not region_list:
        region_list = ["US"]

    try:
        app_records: list[dict[str, Any]] = []
        unified_to_app: dict[str, dict[str, Any]] = {}
        sub_id_to_platform: dict[str, tuple[str, str]] = {}
        all_sub_ids: list[int | str] = []

        for item in apps[:limit]:
            query = str(item.get("query") or item.get("name") or item.get("url") or item.get("app_store_url") or "").strip()
            url = str(item.get("url") or item.get("app_store_url") or "").strip()
            if not query:
                continue
            candidates = _st_call_with_retry(
                lambda q=query: _st_api_mod.autocomplete_search(client, q, limit=8),
                f"timeseries autocomplete '{query}'",
                attempts=2,
            )
            selected = None
            ios_app_id = _extract_ios_app_id(url or query)
            if ios_app_id:
                for candidate in candidates or []:
                    if not isinstance(candidate, dict):
                        continue
                    for sub in candidate.get("ios_apps") or []:
                        sid = str(sub.get("id") or sub.get("app_id") or "")
                        if sid == ios_app_id:
                            selected = candidate
                            break
                    if selected:
                        break
            if not selected:
                selected = next(
                    (candidate for candidate in candidates or [] if isinstance(candidate, dict) and _is_st_direct_app_match(query, candidate)),
                    None,
                )
            if not selected and candidates:
                selected = next((candidate for candidate in candidates if isinstance(candidate, dict)), None)
            if not selected:
                app_records.append({"query": query, "ok": False, "error": "未匹配到 App"})
                continue

            unified_id = str(selected.get("id") or selected.get("app_id") or "")
            app_name = selected.get("name") or selected.get("humanized_name") or query
            record = {
                "query": query,
                "ok": True,
                "_input_order": len(app_records),
                "name": app_name,
                "publisher": selected.get("publisher_name") or "",
                "app_store_url": _entity_app_store_url(selected),
                "sensor_tower_url": _sensor_tower_search_url(app_name),
            }
            app_records.append(record)
            if unified_id:
                unified_to_app[unified_id] = record

            for sub in (selected.get("ios_apps") or []) + (selected.get("android_apps") or []):
                if not isinstance(sub, dict):
                    continue
                sid = sub.get("id") or sub.get("app_id")
                if sid is None:
                    continue
                platform = str(sub.get("os") or _platform_from_sub_app_id(sid)).lower()
                sid_key = str(sid)
                sub_id_to_platform[sid_key] = (unified_id, "ios" if platform == "ios" else "android")
                if sid not in all_sub_ids:
                    all_sub_ids.append(sid)

        if not all_sub_ids:
            return {
                "available": True,
                "date_range": {"start": start_date.isoformat(), "end": end_date.isoformat(), "label": f"{period_days} 天"},
                "granularity": granularity,
                "regions": region_list,
                "items": app_records,
                "rows": [],
                "series": [],
            }

        csrf = _st_call_with_retry(lambda: _st_api_mod.get_csrf_token_for_top_apps_page(client), "timeseries csrf", attempts=2)
        rows_out: list[dict[str, Any]] = []

        for region in region_list:
            metric_facets = []
            if needs_downloads:
                metric_facets.append({"facet": "downloads", "measure": "absolute", "alias": "downloadsAbsolute"})
            if needs_revenue:
                metric_facets.append({"facet": "revenue", "measure": "absolute", "alias": "revenueAbsolute"})
            body = {
                "facets": [
                    *metric_facets,
                    {"facet": "unified_app_id", "alias": "unifiedAppId"},
                    {"facet": "app_id", "alias": "appId"},
                    {"facet": "date", "granularity": granularity, "alias": "date"},
                ],
                "filters": {
                    "start_date": start_date.isoformat(),
                    "end_date": end_date.isoformat(),
                    "devices": ["iphone", "ipad", "android"],
                    "regions": [region],
                    "app_ids": all_sub_ids,
                },
                "breakdowns": [["date", "unifiedAppId"], ["date", "unifiedAppId", "appId"]],
                "data_model": getattr(_st_api_mod, "DEFAULT_DATA_MODEL", "DM_V1"),
            }
            headers = dict(_st_api_mod.POST_JSON_HEADERS)
            if csrf:
                headers["x-csrf-token"] = csrf
            response = client.post(
                "/api/v2/apps/facets?query_identifier=TopAppsData",
                json=body,
                headers=headers,
            )
            rows = _st_api_mod._parse_json_response(response).get("data", [])
            for row in rows or []:
                unified_id = str(row.get("unifiedAppId") or "")
                app_id = row.get("appId")
                if app_id is None:
                    record = unified_to_app.get(unified_id)
                    platform = "all"
                else:
                    mapped = sub_id_to_platform.get(str(app_id))
                    if not mapped:
                        continue
                    record = unified_to_app.get(mapped[0])
                    platform = mapped[1]
                if not record:
                    continue
                revenue = _facet_money(row, "revenueAbsolute") if needs_revenue else 0.0
                downloads = _facet_number(row, "downloadsAbsolute") if needs_downloads else 0.0
                rpd = revenue / downloads if downloads else None
                normalized_row = {
                    "app_order": record.get("_input_order", 0),
                    "app": record.get("name") or "",
                    "publisher": record.get("publisher") or "",
                    "region": region,
                    "platform": platform,
                    "date": str(row.get("date") or ""),
                    "app_store_url": record.get("app_store_url") or "",
                    "sensor_tower_url": record.get("sensor_tower_url") or "",
                }
                if "revenue" in metric_set:
                    normalized_row.update({
                        "revenue": round(revenue, 2),
                        "revenue_display": _format_currency(revenue) if revenue else "-",
                    })
                if "downloads" in metric_set:
                    normalized_row.update({
                        "downloads": int(downloads),
                        "downloads_display": _format_number(downloads) if downloads else "-",
                    })
                if "rpd" in metric_set:
                    normalized_row.update({
                        "rpd": round(rpd, 4) if rpd is not None else None,
                        "rpd_display": _format_rpd(rpd),
                    })
                rows_out.append(normalized_row)

        platform_order = {"all": 0, "ios": 1, "android": 2}
        rows_out.sort(key=lambda row: (
            int(row.get("app_order") or 0),
            str(row.get("region") or ""),
            platform_order.get(str(row.get("platform") or ""), 9),
            str(row.get("date") or ""),
        ))

        grouped: dict[tuple[int, str, str, str], dict[str, Any]] = {}
        for row in rows_out:
            key = (
                int(row.get("app_order") or 0),
                str(row.get("app") or ""),
                str(row.get("region") or ""),
                str(row.get("platform") or ""),
            )
            if key not in grouped:
                grouped[key] = {
                    "key": "|".join(str(part) for part in key),
                    "app": row.get("app"),
                    "publisher": row.get("publisher"),
                    "region": row.get("region"),
                    "platform": row.get("platform"),
                    "label": f"{row.get('app')} · {str(row.get('region') or '').upper()} · {str(row.get('platform') or '').upper()}",
                    "app_store_url": row.get("app_store_url") or "",
                    "sensor_tower_url": row.get("sensor_tower_url") or "",
                    "points": [],
                }
            point = {"date": row.get("date")}
            if "revenue" in metric_set:
                point["revenue"] = row.get("revenue")
            if "downloads" in metric_set:
                point["downloads"] = row.get("downloads")
            if "rpd" in metric_set:
                point["rpd"] = row.get("rpd")
            grouped[key]["points"].append(point)

        return {
            "available": True,
            "date_range": {"start": start_date.isoformat(), "end": end_date.isoformat(), "label": f"{period_days} 天"},
            "granularity": granularity,
            "metrics": list(metric for metric in ["revenue", "downloads", "rpd"] if metric in metric_set),
            "regions": region_list,
            "items": app_records,
            "rows": rows_out,
            "series": list(grouped.values()),
        }
    except Exception as e:
        print(f"[st_client] timeseries error: {e}")
        return {"available": False, "error": _st_friendly_error(e), "series": []}
    finally:
        try:
            client.close()
        except Exception:
            pass


def _validated_itunes_url(url: str) -> str:
    """只允许访问 Apple 官方 HTTPS API，避免跟随外部数据中的任意 URL。"""
    parsed = urlsplit(str(url or "").strip())
    if (
        parsed.scheme != "https"
        or (parsed.hostname or "").lower() != "itunes.apple.com"
        or parsed.username
        or parsed.password
        or parsed.fragment
    ):
        raise ValueError("Apple 数据地址无效")
    return parsed.geturl()


def _itunes_json(url: str) -> dict[str, Any]:
    """读取 Apple iTunes JSON 接口。"""
    url = _validated_itunes_url(url)
    req = UrlRequest(
        url,
        headers={
            "User-Agent": "Lumon/1.0 (+https://lumon.local)",
            "Accept": "application/json,text/javascript,*/*",
        },
    )
    with urlopen(req, timeout=20) as response:  # nosec B310
        return json.loads(response.read().decode("utf-8", "replace"))


def _itunes_rss_reviews_page(app_id: str, *, country: str, page: int | None = None, url: str | None = None) -> tuple[list[dict[str, Any]], str]:
    """读取 Apple RSS XML 评论页；JSON 版本偶发空 feed，XML + cc 参数更稳定。"""
    page_url = _validated_itunes_url(url or f"https://itunes.apple.com/rss/customerreviews/page={page or 2}/id={app_id}/sortby=mostrecent/xml?{urlencode({'cc': country})}")
    with urlopen(page_url, timeout=20) as response:  # nosec B310
        text = response.read().decode("utf-8", "replace")
    root = ET.fromstring(text)
    ns = {"a": "http://www.w3.org/2005/Atom", "im": "http://itunes.apple.com/rss"}
    next_url = ""
    for link_node in root.findall("a:link", ns):
        attrs = link_node.attrib or {}
        if attrs.get("rel") == "next" and attrs.get("href"):
            next_url = attrs.get("href", "")
            break
    out: list[dict[str, Any]] = []
    for entry in root.findall("a:entry", ns):
        rating_text = entry.findtext("im:rating", default="", namespaces=ns)
        if not rating_text:
            continue
        link = ""
        for link_node in entry.findall("a:link", ns):
            attrs = link_node.attrib or {}
            if attrs.get("href"):
                link = attrs.get("href", "")
                break
        author = entry.find("a:author", ns)
        username = ""
        if author is not None:
            username = author.findtext("a:name", default="", namespaces=ns) or ""
        out.append({
            "id": entry.findtext("a:id", default="", namespaces=ns) or "",
            "title": entry.findtext("a:title", default="", namespaces=ns) or "",
            "content": entry.findtext("a:content", default="", namespaces=ns) or "",
            "created_at": entry.findtext("a:updated", default="", namespaces=ns) or "",
            "rating": rating_text,
            "version": entry.findtext("im:version", default="", namespaces=ns) or "",
            "vote_sum": entry.findtext("im:voteSum", default="0", namespaces=ns) or "0",
            "vote_count": entry.findtext("im:voteCount", default="0", namespaces=ns) or "0",
            "username": username,
            "review_url": link,
        })
    return out, next_url


def _itunes_label(node: Any, default: str = "") -> str:
    if isinstance(node, dict):
        return str(node.get("label") or default)
    return default


def _itunes_review_date(value: str) -> date | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except Exception:
        try:
            return date.fromisoformat(text[:10])
        except Exception:
            return None


def _review_rating(value: Any) -> int:
    """把不同来源的评分统一转成 0-5 的整数，无法识别时返回 0。"""
    try:
        rating = int(float(value or 0))
    except Exception:
        rating = 0
    return max(0, min(5, rating))


def _review_sentiment_from_rating(value: Any) -> str:
    rating = _review_rating(value)
    if 1 <= rating <= 3:
        return "negative"
    if 4 <= rating <= 5:
        return "positive"
    return ""


def _filter_reviews_by_sentiment(reviews: list[dict[str, Any]], sentiment: str) -> list[dict[str, Any]]:
    """按产品口径过滤评论：1-3 星算差评，4-5 星算好评。"""
    mode = str(sentiment or "all").lower()
    filtered: list[dict[str, Any]] = []
    for review in reviews:
        rating = _review_rating(review.get("rating"))
        if mode == "negative" and not (1 <= rating <= 3):
            continue
        if mode == "positive" and not (4 <= rating <= 5):
            continue
        if mode == "all" and rating <= 0:
            continue
        filtered.append(review)
    return filtered


_ST_ANDROID_REVIEW_LANGUAGES = [
    "AR", "AZ", "BG", "CS", "DA", "DE", "EL", "EN", "ES", "ET", "FI", "FR",
    "HE", "HI", "HR", "HU", "ID", "IT", "JA", "KK", "KO", "LO", "LT", "LV",
    "MS", "MY", "NL", "NO", "PL", "PT", "RO", "RU", "SK", "SL", "SR", "SV",
    "SW", "TH", "TR", "UK", "VI", "ZH",
]


def _st_review_page_params(
    *,
    platform: str,
    app_id: str,
    start_date: date,
    end_date: date,
    page: int,
    limit: int,
    country: str,
) -> dict[str, Any]:
    """构造 SensorTower 评论分页请求；接口单页最大 200。"""
    params: dict[str, Any] = {
        "app_id": app_id,
        "start_date": _st_api_mod.format_date(start_date),
        "end_date": _st_api_mod.format_date(end_date),
        "rating_filters": [1, 2, 3, 4, 5],
        "search_terms": [],
        "content_keywords": [],
        "tags": [],
        "versions": [],
        "sentiments": ["happy", "mixed", "neutral", "unhappy"],
        "sort_by": "date",
        "sort_order": "desc",
        "limit": max(1, min(int(limit or 200), 200)),
        "page": max(1, int(page or 1)),
        "exclude_rating_breakdown": "true",
    }
    if platform == "ios":
        normalized_country = str(country or "").strip().lower()
        if normalized_country not in {"", "all", "global", "worldwide", "ww"}:
            params["countries"] = [normalized_country.upper()]
    else:
        params["languages"] = _ST_ANDROID_REVIEW_LANGUAGES
    return params


def _st_fetch_app_review_pages(
    client: Any,
    *,
    platform: str,
    app_id: str,
    start_date: date,
    end_date: date,
    csrf_token: str | None,
    country: str,
    max_total: int = 5000,
) -> tuple[list[dict[str, Any]], int, int, int]:
    """分页抓取 SensorTower 当前窗口内可返回的评论，返回 raw/page_count/total_count/pages。"""
    headers: dict[str, str] = dict(_st_api_mod.POST_JSON_HEADERS)
    if csrf_token:
        headers["x-csrf-token"] = csrf_token
    per_page = 200
    endpoint = f"/api/{platform}/review/get_reviews"
    raw_reviews: list[dict[str, Any]] = []
    page_count = 1
    total_count = 0
    fetched_pages = 0
    page = 1
    while page <= max(1, page_count):
        params = _st_review_page_params(
            platform=platform,
            app_id=app_id,
            start_date=start_date,
            end_date=end_date,
            page=page,
            limit=per_page,
            country=country,
        )
        data = _st_call_with_retry(
            lambda params=params: _st_api_mod._parse_json_response(
                client.post(endpoint, json=params, headers=headers)
            ),
            f"review fallback comments page {page}",
            attempts=2,
        )
        feedbacks = data.get("feedback", []) if isinstance(data, dict) else []
        if not isinstance(feedbacks, list):
            feedbacks = []
        fetched_pages += 1
        if isinstance(data, dict):
            try:
                page_count = max(1, int(data.get("page_count") or page_count or 1))
            except Exception:
                page_count = max(1, page_count)
            try:
                total_count = max(total_count, int(data.get("total_count") or 0))
            except Exception:
                total_count = max(total_count, len(raw_reviews) + len(feedbacks))
        raw_reviews.extend(feedbacks)
        if len(raw_reviews) >= max_total:
            raw_reviews = raw_reviews[:max_total]
            break
        if not feedbacks or len(feedbacks) < per_page:
            break
        page += 1
    return raw_reviews, page_count, total_count or len(raw_reviews), fetched_pages


def _itunes_resolve_app(query: str, *, country: str = "us") -> dict[str, Any] | None:
    """用 Apple Search/Lookup API 定位 App Store App。"""
    text = str(query or "").strip()
    ios_app_id = _extract_ios_app_id(text) or (text if re.fullmatch(r"\d{6,}", text) else "")
    if ios_app_id:
        lookup_url = f"https://itunes.apple.com/lookup?{urlencode({'id': ios_app_id, 'country': country})}"
        data = _itunes_json(lookup_url)
        results = data.get("results") or []
        return results[0] if results else None

    search_url = "https://itunes.apple.com/search?" + urlencode({
        "term": text,
        "country": country,
        "entity": "software",
        "limit": 8,
    })
    data = _itunes_json(search_url)
    results = data.get("results") or []
    if not results:
        return None

    compact_query = _compact_name(text)
    direct = next(
        (
            item for item in results
            if compact_query and compact_query in _compact_name(str(item.get("trackName") or ""))
        ),
        None,
    )
    return direct or results[0]


def _st_fetch_app_review_sample(
    query: str,
    *,
    app_hint: dict[str, Any] | None,
    days: int,
    limit: int,
    sentiment: str,
    country: str,
) -> dict[str, Any]:
    """Apple RSS 为空时，回退到 st-cli 内部评论接口拿小样本。"""
    if not _ensure_st_cli_imports():
        return {"available": False, "error": "st-cli 内部模块不可用", "reviews": []}
    client = _get_st_http_client()
    if not client:
        return {"available": False, "error": "SensorTower 登录状态不可用", "reviews": []}
    end_date = date.today()
    window_days = max(1, min(int(days or 30), 365))
    start_date = end_date - timedelta(days=window_days - 1)
    try:
        candidates = _st_call_with_retry(
            lambda: _st_api_mod.autocomplete_search(client, query, limit=8),
            f"review fallback autocomplete '{query}'",
            attempts=2,
        )
        selected = None
        app_id_hint = str((app_hint or {}).get("trackId") or "")
        if app_id_hint:
            for candidate in candidates or []:
                if not isinstance(candidate, dict):
                    continue
                for sub in candidate.get("ios_apps") or []:
                    sid = str(sub.get("id") or sub.get("app_id") or "")
                    if sid == app_id_hint:
                        selected = candidate
                        break
                if selected:
                    break
        if not selected:
            selected = next(
                (candidate for candidate in candidates or [] if isinstance(candidate, dict) and _is_st_direct_app_match(query, candidate)),
                None,
            )
        if not selected and candidates:
            selected = next((candidate for candidate in candidates if isinstance(candidate, dict)), None)
        if not selected:
            return {"available": False, "error": "SensorTower 未匹配到该 App", "reviews": []}

        ios_apps = [sub for sub in (selected.get("ios_apps") or []) if isinstance(sub, dict)]
        android_apps = [sub for sub in (selected.get("android_apps") or []) if isinstance(sub, dict)]
        ios_app_id = (ios_apps[0].get("app_id") or ios_apps[0].get("id")) if ios_apps else None
        android_app_id = (android_apps[0].get("app_id") or android_apps[0].get("id")) if android_apps else None
        platform = "ios" if ios_app_id else ("android" if android_app_id else "")
        if not platform:
            return {"available": False, "error": "未找到可查询评论的 App ID", "reviews": []}

        csrf = _st_call_with_retry(lambda: _st_api_mod.get_csrf_token_for_top_apps_page(client), "review fallback csrf", attempts=2)
        raw_reviews, page_count, total_count, fetched_pages = _st_fetch_app_review_pages(
            client,
            platform=platform,
            app_id=str(ios_app_id if platform == "ios" else android_app_id),
            start_date=start_date,
            end_date=end_date,
            csrf_token=csrf,
            country=country,
        )

        country_label = "" if str(country or "").strip().lower() in {"", "all", "global", "worldwide", "ww"} else country.upper()
        reviews = []
        for review in (raw_reviews or []):
            rating = _review_rating(review.get("rating"))
            if rating <= 0:
                continue
            reviews.append({
                "id": review.get("id") or "",
                "app_id": review.get("app_id") or "",
                "platform": "App Store" if platform == "ios" else "Google Play",
                "title": str(review.get("title") or "").strip(),
                "username": str(review.get("username") or "").strip(),
                "country": str(review.get("country") or "").strip() or country_label,
                "sentiment": _review_sentiment_from_rating(rating),
                "rating": rating,
                "tags": review.get("tags") or [],
                "content": str(review.get("content") or "").strip(),
                "created_at": review.get("date") or review.get("created_at") or "",
                "version": str(review.get("version") or "").strip(),
            })
        reviews.sort(key=lambda item: str(item.get("created_at") or ""), reverse=True)
        sentiment = str(sentiment or "negative").lower()
        selected_reviews = _filter_reviews_by_sentiment(reviews, sentiment)
        negative_total = len(_filter_reviews_by_sentiment(reviews, "negative"))
        positive_total = len(_filter_reviews_by_sentiment(reviews, "positive"))
        app_name = selected.get("name") or selected.get("humanized_name") or (app_hint or {}).get("trackName") or query
        return {
            "available": True,
            "review_search": True,
            "sentiment_filter": sentiment,
            "source": "App Store",
            "source_channel": "SensorTower 补充抓取",
            "date_range": {"start": start_date.isoformat(), "end": end_date.isoformat(), "label": f"近 {window_days} 天"},
            "app": {
                "name": app_name,
                "publisher": selected.get("publisher_name") or (app_hint or {}).get("sellerName") or "",
                "icon_url": selected.get("icon_url") or (app_hint or {}).get("artworkUrl100") or "",
                "app_store_url": _entity_app_store_url(selected) or (app_hint or {}).get("trackViewUrl") or "",
                "sensor_tower_url": _sensor_tower_search_url(app_name),
            },
            "reviews": reviews[:limit],
            "total": len(selected_reviews),
            "all_total": len(reviews),
            "negative_total": negative_total,
            "positive_total": positive_total,
            "raw_total": len(raw_reviews or []),
            "source_total": total_count,
            "fetched_pages": fetched_pages,
            "page_count": page_count,
            "max_raw_capacity": total_count,
            "fallback": "sensor_tower",
        }
    except Exception as e:
        print(f"[st_client] review fallback error: {e}")
        return {
            "available": False,
            "error": "Apple App Store 暂无可读取评论，SensorTower 补充通道也暂时不可用，请稍后重试",
            "reviews": [],
            "fallback_error": _st_friendly_error(e),
        }
    finally:
        try:
            client.close()
        except Exception:
            pass


def fetch_app_reviews(
    query: str,
    *,
    days: int = 30,
    limit: int = 12,
    sentiment: str = "negative",
    country: str = "global",
    max_pages: int = 10,
) -> dict[str, Any]:
    """使用 Apple iTunes RSS 查询单个 App 的近期 App Store 用户评论。"""
    text = str(query or "").strip()
    if not text:
        return {"available": False, "error": "请输入 App 名称", "reviews": []}
    country = re.sub(r"[^A-Za-z]", "", str(country or "global")).lower() or "global"
    is_global_country = country in {"all", "global", "worldwide", "ww"}
    review_country = "global" if is_global_country else country
    resolve_country = "us" if is_global_country else country
    end_date = date.today()
    window_days = max(1, min(int(days or 30), 365))
    start_date = end_date - timedelta(days=window_days - 1)
    max_pages = max(1, min(int(max_pages or 10), 10))
    requested_limit = max(3, min(int(limit or 12), 5000))
    apple_limit = min(requested_limit, max_pages * 50)

    try:
        app = _itunes_resolve_app(text, country=resolve_country)
        if not app:
            fallback = _st_fetch_app_review_sample(
                text,
                app_hint=None,
                days=window_days,
                limit=requested_limit,
                sentiment=sentiment,
                country=review_country,
            )
            if fallback.get("available"):
                fallback["apple_rss_empty"] = True
                fallback["apple_max_raw_capacity"] = max_pages * 50
                return fallback
            return {"available": False, "error": fallback.get("error") or "Apple App Store 未匹配到该 App", "reviews": []}
        app_id = str(app.get("trackId") or "")
        if not app_id:
            return {"available": False, "error": "Apple App Store 未返回 App ID", "reviews": []}

        if is_global_country:
            fallback = _st_fetch_app_review_sample(
                text,
                app_hint=app,
                days=window_days,
                limit=requested_limit,
                sentiment=sentiment,
                country="global",
            )
            if fallback.get("available"):
                fallback["apple_rss_empty"] = True
                fallback["apple_max_raw_capacity"] = max_pages * 50
            return fallback

        raw_reviews: list[dict[str, Any]] = []
        stop_after_page = False
        next_url = ""
        for page in range(1, max_pages + 1):
            try:
                entries, next_url = _itunes_rss_reviews_page(
                    app_id,
                    country=country,
                    page=2 if page == 1 else None,
                    url=next_url if page > 1 else None,
                )
            except Exception as e:
                if page == 1:
                    raise
                print(f"[st_client] iTunes reviews page {page} stopped: {e}")
                break
            if not isinstance(entries, list) or not entries:
                break
            page_reviews: list[dict[str, Any]] = []
            for entry in entries:
                if not isinstance(entry, dict) or not entry.get("rating"):
                    continue
                created_at = str(entry.get("created_at") or "")
                review_date = _itunes_review_date(created_at)
                if review_date and review_date < start_date:
                    stop_after_page = True
                    continue
                try:
                    rating = _review_rating(entry.get("rating"))
                except Exception:
                    rating = 0
                sentiment_value = _review_sentiment_from_rating(rating)
                page_reviews.append({
                    "id": str(entry.get("id") or ""),
                    "app_id": app_id,
                    "platform": "App Store",
                    "title": str(entry.get("title") or "").strip(),
                    "username": str(entry.get("username") or "").strip(),
                    "country": review_country.upper(),
                    "sentiment": sentiment_value,
                    "rating": rating,
                    "version": str(entry.get("version") or "").strip(),
                    "vote_sum": str(entry.get("vote_sum") or "0"),
                    "vote_count": str(entry.get("vote_count") or "0"),
                    "tags": [],
                    "content": str(entry.get("content") or "").strip(),
                    "created_at": created_at,
                    "review_url": str(entry.get("review_url") or "").strip(),
                })
            raw_reviews.extend(page_reviews)
            if stop_after_page:
                break
            if not next_url:
                break

        if not raw_reviews:
            fallback = _st_fetch_app_review_sample(
                text,
                app_hint=app,
                days=window_days,
                limit=requested_limit,
                sentiment=sentiment,
                country=review_country,
            )
            if fallback.get("available"):
                fallback["apple_rss_empty"] = True
                fallback["apple_max_raw_capacity"] = max_pages * 50
            return fallback

        sentiment = str(sentiment or "negative").lower()
        all_reviews = _filter_reviews_by_sentiment(raw_reviews, "all")
        all_reviews.sort(key=lambda item: str(item.get("created_at") or ""), reverse=True)
        selected_reviews = _filter_reviews_by_sentiment(all_reviews, sentiment)
        negative_total = len(_filter_reviews_by_sentiment(all_reviews, "negative"))
        positive_total = len(_filter_reviews_by_sentiment(all_reviews, "positive"))

        app_name = str(app.get("trackName") or text)
        apple_result = {
            "available": True,
            "review_search": True,
            "sentiment_filter": sentiment,
            "source": "Apple RSS",
            "date_range": {"start": start_date.isoformat(), "end": end_date.isoformat(), "label": f"近 {window_days} 天"},
            "app": {
                "name": app_name,
                "publisher": app.get("sellerName") or app.get("artistName") or "",
                "icon_url": app.get("artworkUrl100") or app.get("artworkUrl512") or "",
                "app_store_url": app.get("trackViewUrl") or f"https://apps.apple.com/{resolve_country}/app/id{app_id}",
                "sensor_tower_url": _sensor_tower_search_url(app_name),
            },
            "reviews": all_reviews[:apple_limit],
            "total": len(selected_reviews),
            "all_total": len(all_reviews),
            "negative_total": negative_total,
            "positive_total": positive_total,
            "raw_total": len(raw_reviews),
            "fetched_pages": max_pages,
            "max_raw_capacity": max_pages * 50,
        }
        if requested_limit > len(all_reviews):
            fallback = _st_fetch_app_review_sample(
                text,
                app_hint=app,
                days=window_days,
                limit=requested_limit,
                sentiment=sentiment,
                country=review_country,
            )
            fallback_reviews = fallback.get("reviews") or []
            if fallback.get("available") and len(fallback_reviews) > len(all_reviews):
                fallback["apple_rss_partial"] = True
                fallback["apple_reviews_total"] = len(all_reviews)
                fallback["apple_max_raw_capacity"] = max_pages * 50
                return fallback
        return apple_result
    except Exception as e:
        print(f"[st_client] apple rss reviews error: {e}")
        if _st_is_transient_error(e):
            return {"available": False, "error": "Apple App Store 评论接口临时不可用，请稍后重试", "reviews": []}
        return {"available": False, "error": "Apple App Store 评论查询失败，请检查本机网络后重试", "reviews": []}


def fetch_landscape(competitors: list[dict], limit: int = 5) -> list[dict]:
    """批量查询竞品的 SensorTower 数据。

    competitors: [{"name": "Duolingo", "url": "https://apps.apple.com/app/id570060128"}, ...]
    返回归一化后的竞品数据列表。
    """
    if not competitors:
        return []

    entries = competitors[:limit]

    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".txt", delete=False, encoding="utf-8",
    ) as f:
        for c in entries:
            url = c.get("url") or c.get("app_store_url") or ""
            name = c.get("name", "")
            if url:
                f.write(f"{name}\t{url}\n")
            else:
                f.write(f"{name}\n")
        tmp_path = f.name

    try:
        result = subprocess.run(
            [
                "st", "landscape",
                "--competitors-file", tmp_path,
                "--limit", str(limit),
                "--json",
            ],
            capture_output=True, text=True, timeout=180,
        )

        Path(tmp_path).unlink(missing_ok=True)

        if result.returncode != 0:
            print(f"[st-cli] landscape failed: {result.stderr[:300]}")
            return []

        data = json.loads(result.stdout.strip())
        if not data.get("ok"):
            print(f"[st-cli] landscape not ok: {data.get('error', {})}")
            return []

        raw_competitors = data.get("data", {}).get("competitors", [])
        results = []
        for rc in raw_competitors:
            normalized = _normalize_competitor(rc)
            if normalized:
                results.append(normalized)

        return results

    except subprocess.TimeoutExpired:
        print("[st-cli] landscape timeout (180s)")
        Path(tmp_path).unlink(missing_ok=True)
        return []
    except Exception as e:
        print(f"[st-cli] landscape error: {e}")
        Path(tmp_path).unlink(missing_ok=True)
        return []


def _normalize_app(app: dict) -> dict:
    """从 st fetch 的 autocomplete 结果中提取关键字段。"""
    rev = app.get("humanized_worldwide_last_month_revenue", {})
    dl = app.get("humanized_worldwide_last_month_downloads", {})
    return {
        "name": app.get("name", ""),
        "publisher": app.get("publisher_name", ""),
        "revenue_last_month": rev.get("revenue"),
        "revenue_display": rev.get("string", "-"),
        "downloads_last_month": dl.get("downloads"),
        "downloads_display": dl.get("string", "-"),
        "icon_url": app.get("icon_url", ""),
        "release_date": app.get("release_date", ""),
        "category_terms": _market_entity_category_terms(app),
        "platform_app_names": _market_entity_app_names(app),
    }


def _normalize_competitor(rc: dict) -> dict | None:
    """从 st landscape 的竞品数据中提取报告所需的归一化字段。"""
    st = rc.get("st")
    if not st:
        return {
            "name": rc.get("name", ""),
            "store_url": rc.get("store_url", ""),
            "error": rc.get("error", "SensorTower 未匹配到"),
            "has_st_data": False,
        }

    selected = st.get("selected", {})
    rev_humanized = selected.get("humanized_worldwide_last_month_revenue", {})
    dl_humanized = selected.get("humanized_worldwide_last_month_downloads", {})

    revenue_last = st.get("revenue_last_month_usd") or st.get("revenue_as_of_last_month_usd")
    downloads_last = (st.get("downloads_as_of_last_month") or {}).get("downloads_absolute")
    mau = (st.get("mau_as_of_last_month") or {}).get("mau_absolute")
    market_share = (st.get("market_share_as_of_last_month") or {}).get("share_percent")
    growth_6m = st.get("growth_vs_6m_percent")
    first_release = st.get("first_release_date_us", "")

    # App Store 评论（优先选负面/mixed 的）
    raw_comments = st.get("comments", [])
    negative_comments = [
        c for c in raw_comments
        if c.get("sentiment") in ("unhappy", "mixed") or (c.get("rating") or 5) <= 3
    ]
    if not negative_comments:
        negative_comments = raw_comments[:3]
    comments = [
        {
            "rating": c.get("rating"),
            "title": c.get("title", ""),
            "content": c.get("content", "")[:300],
            "sentiment": c.get("sentiment", ""),
            "tags": c.get("tags", []),
        }
        for c in negative_comments[:3]
    ]

    return {
        "name": rc.get("name") or selected.get("name", ""),
        "store_url": rc.get("store_url", ""),
        "has_st_data": True,
        "revenue_last_month": revenue_last,
        "revenue_display": _format_currency(revenue_last) if revenue_last else rev_humanized.get("string", "-"),
        "downloads_last_month": downloads_last,
        "downloads_display": _format_number(downloads_last) if downloads_last else dl_humanized.get("string", "-"),
        "mau": mau,
        "mau_display": _format_number(mau) if mau else "-",
        "market_share_percent": round(market_share, 2) if market_share else None,
        "market_share_display": f"{market_share:.1f}%" if market_share else "-",
        "growth_6m_percent": round(growth_6m, 1) if growth_6m is not None else None,
        "growth_6m_display": f"+{growth_6m:.1f}%" if growth_6m and growth_6m >= 0 else (f"{growth_6m:.1f}%" if growth_6m else "-"),
        "first_release": first_release,
        "release_year": first_release[:4] if first_release else "-",
        "ai_label": rc.get("ai_label", "-"),
        "segment": rc.get("segment", "-"),
        "strengths": rc.get("strengths", []),
        "weaknesses": rc.get("weaknesses", []),
        "comments": comments,
        "publisher": selected.get("publisher_name", ""),
        "icon_url": selected.get("icon_url", ""),
    }


def _format_currency(value: float | None) -> str:
    """将美元金额格式化为可读字符串。"""
    if value is None:
        return "-"
    if value >= 1_000_000:
        return f"${value / 1_000_000:.1f}M"
    if value >= 100_000:
        return f"${value / 1_000:.1f}K".replace(".0K", "K")
    if value >= 1_000:
        return f"${value / 1_000:.0f}K"
    return f"${value:.0f}"


def _format_number(value: float | None) -> str:
    """将数字格式化为可读字符串。"""
    if value is None:
        return "-"
    if value >= 1_000_000:
        return f"{value / 1_000_000:.1f}M"
    if value >= 1_000:
        return f"{value / 1_000:.0f}K"
    return f"{value:.0f}"


def fetch_product_with_peers(
    product_name: str,
    category_queries: list[str],
    *,
    peer_count: int = 8,
) -> dict[str, Any] | None:
    """获取自家产品数据 + 收入规模相近的竞品列表。

    1. autocomplete 搜索自家产品，获取图标/收入/下载
    2. 用 category_queries 搜索该赛道所有产品
    3. 按收入与自家产品的差距排序，取最接近的 peer_count 个作为竞品
    """
    if not _ensure_st_cli_imports():
        return None
    client = _get_st_http_client()
    if not client:
        return None

    try:
        today = date.today()
        month_start = date(today.year, today.month, 1)
        prev_start = date(today.year - 1, 12, 1) if today.month == 1 else date(today.year, today.month - 1, 1)
        prev_end = month_start - timedelta(days=1)
        csrf = _st_api_mod.get_csrf_token_for_top_apps_page(client)

        # -- 1. 搜索自家产品 --
        my_results = _st_api_mod.autocomplete_search(client, product_name, limit=5)
        if not my_results:
            return None
        my_app = my_results[0]
        my_uid = str(my_app.get("id") or my_app.get("app_id") or "")
        my_info = {
            "name": my_app.get("name") or my_app.get("humanized_name") or product_name,
            "icon_url": my_app.get("icon_url") or "",
            "publisher": my_app.get("publisher_name") or "",
        }

        my_sub_ids = []
        for sub in my_app.get("ios_apps", []) + my_app.get("android_apps", []):
            sid = sub.get("id") or sub.get("app_id")
            if sid is not None:
                my_sub_ids.append(sid)

        my_revenue = 0.0
        my_downloads = 0
        my_dau = 0
        my_growth = None
        my_dl_growth = None
        if my_sub_ids:
            orig_regions = _st_api_mod.DEFAULT_FACET_REGIONS
            _st_api_mod.DEFAULT_FACET_REGIONS = ["US"]
            try:
                my_facets = _st_api_mod.apps_facets_v2_month_slice(
                    client, app_ids=my_sub_ids, month_start=prev_start, month_end=prev_end,
                    comparison_start=prev_start - timedelta(days=30), comparison_end=prev_start - timedelta(days=1),
                    csrf_token=csrf, limit=20,
                )
            finally:
                _st_api_mod.DEFAULT_FACET_REGIONS = orig_regions
            for row in my_facets:
                if row.get("appId") is not None:
                    continue
                rev_raw = row.get("revenueAbsolute")
                if rev_raw:
                    try: my_revenue = float(rev_raw) / 100.0
                    except: pass
                dl_raw = row.get("downloadsAbsolute")
                if dl_raw:
                    try: my_downloads = int(float(dl_raw))
                    except: pass
                dau_raw = row.get("activeUsersDAUAbsolute")
                if dau_raw:
                    try: my_dau = int(float(dau_raw))
                    except: pass
                g = row.get("revenueGrowthPercent")
                my_growth = None
                if g is not None and g != "":
                    try: my_growth = round(float(g) * 100, 1)
                    except: pass
                dl_g = row.get("downloadsGrowthPercent")
                my_dl_growth = None
                if dl_g is not None and dl_g != "":
                    try: my_dl_growth = round(float(dl_g) * 100, 1)
                    except: pass

        product_data = {
            **my_info,
            "revenue": round(my_revenue, 2),
            "revenue_display": _format_currency(my_revenue) if my_revenue else "-",
            "downloads": my_downloads,
            "downloads_display": _format_number(my_downloads) if my_downloads else "-",
            "dau": my_dau,
            "dau_display": _format_number(my_dau) if my_dau else "-",
            "growth_pct": my_growth,
            "downloads_growth_pct": my_dl_growth,
        }

        # -- 2. 搜索赛道内所有产品 --
        seen_uids: set[str] = set()
        if my_uid:
            seen_uids.add(my_uid)
        all_entries: list[dict] = []
        for q in category_queries:
            try:
                results = _st_api_mod.autocomplete_search(client, q, limit=10)
                for ent in results:
                    uid = str(ent.get("id") or ent.get("app_id") or "")
                    if not uid or uid in seen_uids:
                        continue
                    seen_uids.add(uid)
                    all_entries.append(ent)
            except Exception as e:
                print(f"[st_client] peer search '{q}' error: {e}")

        if not all_entries:
            return {"product": product_data, "peers": []}

        # 提取所有 sub_app_ids
        peer_sub_ids: list[int | str] = []
        for ent in all_entries:
            for sub in ent.get("ios_apps", []) + ent.get("android_apps", []):
                sid = sub.get("id") or sub.get("app_id")
                if sid is not None:
                    peer_sub_ids.append(sid)

        if not peer_sub_ids:
            return {"product": product_data, "peers": []}

        # -- 3. 获取所有候选竞品的 facets 数据 --
        orig_regions = _st_api_mod.DEFAULT_FACET_REGIONS
        _st_api_mod.DEFAULT_FACET_REGIONS = ["US"]
        try:
            peer_facets = _st_api_mod.apps_facets_v2_month_slice(
                client, app_ids=peer_sub_ids[:80], month_start=prev_start, month_end=prev_end,
                comparison_start=prev_start - timedelta(days=30), comparison_end=prev_start - timedelta(days=1),
                csrf_token=csrf, limit=len(peer_sub_ids) + 10,
            )
        finally:
            _st_api_mod.DEFAULT_FACET_REGIONS = orig_regions

        peer_apps: list[dict] = []
        for row in peer_facets:
            if row.get("appId") is not None:
                continue
            rev_raw = row.get("revenueAbsolute")
            rev = 0.0
            if rev_raw:
                try: rev = float(rev_raw) / 100.0
                except: pass
            dl_raw = row.get("downloadsAbsolute")
            dl = 0
            if dl_raw:
                try: dl = int(float(dl_raw))
                except: pass
            g = row.get("revenueGrowthPercent")
            g_pct = None
            if g is not None and g != "":
                try: g_pct = round(float(g) * 100, 1)
                except: pass
            dl_g = row.get("downloadsGrowthPercent")
            dl_g_pct = None
            if dl_g is not None and dl_g != "":
                try: dl_g_pct = round(float(dl_g) * 100, 1)
                except: pass
            dau_raw = row.get("activeUsersDAUAbsolute")
            dau = 0
            if dau_raw:
                try: dau = int(float(dau_raw))
                except: pass

            peer_apps.append({
                "name": "",
                "icon_url": "",
                "publisher": "",
                "_unified_id": row.get("unifiedAppId", ""),
                "revenue": round(rev, 2),
                "revenue_display": _format_currency(rev) if rev else "-",
                "downloads": dl,
                "downloads_display": _format_number(dl) if dl else "-",
                "growth_pct": g_pct,
                "downloads_growth_pct": dl_g_pct,
                "dau": dau,
                "dau_display": _format_number(dau) if dau else "-",
                "_rev_distance": abs(rev - my_revenue),
            })

        # -- 4. 把自家产品插入列表，按收入降序排，取周围各5个（总共10个含自己）--
        my_entry = {
            **product_data,
            "_unified_id": my_uid,
            "_is_ours": True,
            "_rev_distance": 0,
        }
        peer_apps.append(my_entry)
        peer_apps.sort(key=lambda x: x["revenue"], reverse=True)

        my_idx = next((i for i, a in enumerate(peer_apps) if a.get("_is_ours")), 0)
        half = (peer_count - 1) // 2
        start = max(0, my_idx - half)
        end = start + peer_count
        if end > len(peer_apps):
            end = len(peer_apps)
            start = max(0, end - peer_count)
        selected_peers = peer_apps[start:end]

        # 补充名称/图标
        name_fb: dict[str, dict] = {}
        for ent in all_entries:
            uid = str(ent.get("id") or ent.get("app_id") or "")
            n = ent.get("name") or ent.get("humanized_name") or ""
            ic = ent.get("icon_url") or ""
            pub = ent.get("publisher_name") or ""
            if uid and n:
                name_fb[uid] = {"name": n, "icon_url": ic, "publisher": pub}

        non_ours = [a for a in selected_peers if not a.get("_is_ours")]
        facet_uids = list({a["_unified_id"] for a in non_ours if a.get("_unified_id")})
        if facet_uids:
            try:
                entities = _st_api_mod.internal_entities(client, facet_uids[:30], csrf_token=csrf)
                uid_info: dict[str, dict] = {}
                for ent in entities:
                    eid = str(ent.get("id") or ent.get("app_id") or "")
                    uid_info[eid] = {
                        "name": ent.get("name") or ent.get("humanized_name") or "",
                        "publisher": ent.get("publisher_name") or "",
                        "icon_url": ent.get("icon_url") or "",
                    }
                for app in non_ours:
                    uid = app.get("_unified_id", "")
                    if uid and uid in uid_info:
                        info = uid_info[uid]
                        app["name"] = info["name"]
                        app["publisher"] = info["publisher"]
                        app["icon_url"] = info["icon_url"]
            except Exception as e:
                print(f"[st_client] peer entities error: {e}")

        for app in non_ours:
            if not app.get("name"):
                uid = app.get("_unified_id", "")
                if uid and uid in name_fb:
                    fb = name_fb[uid]
                    app["name"] = fb["name"]
                    if not app.get("icon_url"):
                        app["icon_url"] = fb["icon_url"]
                    if not app.get("publisher"):
                        app["publisher"] = fb["publisher"]

        # 计算全局排名（1-based）
        global_rank_offset = start
        final_peers = []
        for i, a in enumerate(selected_peers):
            a["rank"] = global_rank_offset + i + 1
            is_ours = bool(a.pop("_is_ours", False))
            a["is_ours"] = is_ours
            a.pop("_unified_id", None)
            a.pop("_rev_distance", None)
            final_peers.append(a)

        return {"product": product_data, "peers": final_peers}

    except Exception as e:
        print(f"[st_client] fetch_product_with_peers error: {e}")
        return None
    finally:
        try:
            client.close()
        except Exception:
            pass


def _dedupe_texts(values: list[str], *, limit: int) -> list[str]:
    """按小写去重文本，并保留原始顺序。"""
    seen: set[str] = set()
    result: list[str] = []
    for raw in values:
        value = re.sub(r"\s+", " ", str(raw or "")).strip()
        if not value:
            continue
        key = value.lower()
        if key in seen:
            continue
        seen.add(key)
        result.append(value[:80])
        if len(result) >= limit:
            break
    return result


_MARKET_QUERY_STOP_TERMS = {
    "app", "apps", "tool", "tools", "software", "best", "top", "alternative",
    "alternatives", "for", "with", "and", "the", "ai",
}


_MARKET_CONTEXT_RULES: tuple[tuple[re.Pattern, set[str]], ...] = (
    (
        re.compile(r"戒饮|戒酒|酒精|戒断|戒瘾|少喝酒|quit drinking|stop drinking|sobriety|sober|alcohol", re.I),
        {"sober", "sobriety", "alcohol", "drink", "drinking", "recovery", "addiction"},
    ),
    (
        re.compile(r"宗教|基督|圣经|祷告|讲道|教会|bible|christian|sermon|church|prayer|devotional|worship", re.I),
        {"bible", "christian", "prayer", "pray", "church", "sermon", "devotional", "worship", "faith"},
    ),
    (
        re.compile(r"戒烟|烟草|尼古丁|quit smoking|stop smoking|nicotine", re.I),
        {"smoke", "smoking", "nicotine", "quit", "tobacco"},
    ),
    (
        re.compile(r"跑步|跑者|马拉松|running|runner|run tracker|marathon|jogging|strava|runkeeper|runna", re.I),
        {"run", "running", "runner", "runners", "marathon", "jogging", "strava", "runkeeper", "runna"},
    ),
    (
        re.compile(r"家庭定位|家人定位|家庭安全|定位共享|儿童定位|family safety|family locator|location sharing|phone tracker|parental control|life360|findmykids|geozilla", re.I),
        {"family", "safety", "location", "locator", "tracking", "tracker", "gps", "parental", "child", "children", "kids"},
    ),
    (
        re.compile(r"攀岩|抱石|climbing|climber|climbers|bouldering|crag", re.I),
        {"climb", "climbing", "climber", "climbers", "boulder", "bouldering", "crag"},
    ),
    (
        re.compile(r"女性健康|经期|月经|排卵|备孕|怀孕|women health|female health|period|menstrual|fertility|ovulation|pregnancy|menopause", re.I),
        {"women", "female", "period", "menstrual", "fertility", "ovulation", "pregnancy", "menopause", "cycle"},
    ),
)


_MARKET_AMBIGUOUS_SHORT_QUERIES = {
    "glow",
    "kaya",
    "nomo",
    "rev",
}

_MARKET_GENERIC_CATEGORY_TERMS = {
    "fitness", "workout", "training", "tracker", "health", "wellness", "coach",
    "coaching", "lifestyle", "habit", "activity",
    "safety", "family", "locator", "location", "tracking", "parental", "child",
    "children", "phone",
}


def _market_match_terms(text: str) -> set[str]:
    """提取 ST 查询和产品名的可比对词，避免 autocomplete 泛召回污染商业化信号。"""
    terms: set[str] = set()
    for term in re.findall(r"[a-zA-Z0-9]+", str(text or "").lower()):
        if term in _MARKET_QUERY_STOP_TERMS or len(term) < 3:
            continue
        terms.add(term)
        if term.endswith("s") and len(term) > 4:
            terms.add(term[:-1])
    return terms


def _market_context_terms(queries: list[str]) -> set[str]:
    """从整组查询词里提取赛道上下文，过滤短词误召回。"""
    text = " ".join(str(q or "") for q in queries)
    terms: set[str] = set()
    for pattern, pattern_terms in _MARKET_CONTEXT_RULES:
        if pattern.search(text):
            terms.update(pattern_terms)
    return terms


def _st_entity_text(entity: dict) -> str:
    return " ".join([
        str(entity.get("name") or entity.get("humanized_name") or ""),
        str(entity.get("publisher_name") or ""),
    ])


def _market_needs_context_guard(query: str) -> bool:
    """少数短词 App 名容易撞名，需再看是否贴合当前赛道上下文。"""
    terms = _market_match_terms(query)
    if len(terms) != 1:
        return False
    term = next(iter(terms))
    return term in _MARKET_AMBIGUOUS_SHORT_QUERIES


def _market_generic_query_needs_context_guard(query: str, context_terms: set[str]) -> bool:
    """上下文明确时，过宽类目词召回的 App 也必须贴合赛道。"""
    if not context_terms:
        return False
    query_terms = _market_match_terms(query)
    if not query_terms or query_terms & context_terms:
        return False
    return bool(query_terms & _MARKET_GENERIC_CATEGORY_TERMS)


def _is_st_autocomplete_match(query: str, entity: dict) -> bool:
    """判断 ST autocomplete 结果是否真的匹配当前查询词。

    SensorTower autocomplete 对短词很宽松，例如 `Rev` 会返回 VPN、Revolut；
    这里必须要求查询词和产品名/开发者存在明确词项命中。
    """
    query_terms = _market_match_terms(query)
    if not query_terms:
        return False

    name_text = _st_entity_text(entity)
    name_terms = _market_match_terms(name_text)
    if not name_terms:
        return False

    query_compact = _compact_name(query)
    name_compact = _compact_name(name_text)
    if len(query_terms) >= 2 and len(query_compact) >= 6 and query_compact in name_compact:
        return True

    # 单词查询最容易跑偏：要求完整词命中，不接受 Hallow -> Halloween / Rev -> Revolut 这类前缀泛化。
    if len(query_terms) == 1:
        return next(iter(query_terms)) in name_terms

    overlap = query_terms & name_terms
    if len(overlap) >= 2:
        return True
    # 多词查询允许核心首词单独命中，例如 "Bible app" / "prayer app"。
    first_term = next(iter(_market_match_terms(str(query).split()[0] if str(query).split() else "")), "")
    return bool(first_term and first_term in name_terms)


def _is_st_direct_app_match(query: str, entity: dict) -> bool:
    """单 App 指标查询比赛道搜索更严格，避免把泛类目错选成第一名。"""
    query_terms = _market_match_terms(query)
    if not query_terms:
        return False
    name_terms = _market_match_terms(_st_entity_text(entity))
    if not name_terms:
        return False
    if len(query_terms) == 1:
        term = next(iter(query_terms))
        return term in name_terms and term not in _MARKET_DIRECT_APP_BROAD_SINGLE_TERMS
    required = min(len(query_terms), 2)
    return len(query_terms & name_terms) >= required


def _market_validation_queries(
    need: dict,
    *,
    topic: str = "",
    known_competitors: list[str] | None = None,
    search_queries: list[str] | None = None,
    max_queries: int = 8,
) -> list[str]:
    """为 ST 赛道校验生成查询词：优先竞品，其次成熟邻近市场，最后用帖子标题兜底。"""
    candidates: list[str] = []
    search_query_candidates: list[str] = []
    for competitor in known_competitors or []:
        comp_text = str(competitor or "").strip()
        if not comp_text:
            continue
        compact = re.sub(r"[^a-zA-Z0-9]", "", comp_text)
        if len(compact) <= 3:
            continue
        candidates.append(comp_text)

    for q in (search_queries or [])[:10]:
        q_text = str(q or "").strip()
        if not q_text:
            continue
        lower = q_text.lower()
        term_count = len(re.findall(r"[a-zA-Z0-9]+", q_text))
        has_english = bool(re.search(r"[a-zA-Z]", q_text))
        is_short_market_query = has_english and 1 <= term_count <= 5
        has_app_marker = any(token in lower for token in (" app", "apps", "tool", "software", "alternative", "best "))
        if is_short_market_query or has_app_marker:
            search_query_candidates.append(q_text)

    posts = need.get("posts") or []
    post_titles = " ".join(str(p.get("title") or "") for p in posts[:8] if isinstance(p, dict))
    text = " ".join([
        str(topic or ""),
        str(need.get("need_title") or ""),
        str(need.get("need_description") or ""),
        post_titles,
        " ".join(candidates),
        " ".join(search_query_candidates),
    ]).lower()

    # 上游 LLM 已经把用户问题翻译成英文市场查询词时，优先信任这些词；
    # 下方中文赛道映射只作为兜底补位，避免搜索引擎看起来像写死几个赛道。
    candidates.extend(search_query_candidates)

    # 这些不是写死结论，而是给 ST autocomplete 一个成熟产品簇入口；
    # 否则“宗教笔记/讲道笔记”容易只匹配到极小众 notes app。
    if re.search(r"宗教|基督|圣经|祷告|讲道|教会|bible|christian|sermon|church|prayer|devotional|worship", text):
        candidates.extend([
            "YouVersion Bible",
            "Hallow",
            "Pray.com",
            "Bible Chat",
            "Glorify",
            "Abide",
            "Bible app",
            "Bible study app",
            "prayer app",
            "Christian app",
            "devotional app",
        ])
        if re.search(r"笔记|note|notes|journal|transcri|record", text):
            candidates.extend([
                "Bible journal app",
                "sermon notes app",
                "Christian notes",
                "prayer journal",
            ])

    if re.search(r"戒饮|戒酒|酒精|戒断|戒瘾|少喝酒|quit drinking|stop drinking|sobriety|sober|alcohol", text):
        candidates.extend([
            "I Am Sober",
            "Reframe",
            "Sunnyside",
            "Nomo",
            "Sober Time",
            "sobriety app",
            "quit drinking app",
            "alcohol tracker",
        ])

    if re.search(r"戒烟|烟草|尼古丁|quit smoking|stop smoking|nicotine", text):
        candidates.extend([
            "Smoke Free",
            "QuitNow",
            "Kwit",
            "quit smoking app",
            "nicotine tracker",
        ])

    # 从英文标题里提取少量短语兜底，避免纯中文输入没有 ST 查询词。
    stop_words = {
        "with", "from", "this", "that", "there", "have", "need", "what",
        "best", "anyone", "tried", "looking", "recommendation", "advice",
        "setup", "help", "about", "would", "could", "should",
    }
    words = [
        w for w in re.findall(r"[a-zA-Z][a-zA-Z0-9+.-]{2,}", post_titles)
        if w.lower() not in stop_words
    ]
    for idx in range(0, min(len(words), 10), 3):
        phrase = " ".join(words[idx:idx + 3])
        if phrase:
            candidates.append(f"{phrase} app")

    return _dedupe_texts(candidates, limit=max_queries)


def _market_level(max_revenue: float, total_revenue: float, competitor_count: int, downloads_sum: float) -> tuple[str, str]:
    """根据 ST 收入和下载规模给出商业化强度。"""
    if max_revenue >= 100_000 or total_revenue >= 300_000:
        return "strong", "商业信号强"
    if max_revenue >= 30_000 or total_revenue >= 100_000:
        return "medium", "商业信号中"
    return "weak", "商业信号弱"


def _market_source_id(*parts: Any, prefix: str = "st") -> str:
    """给 ST 市场信号生成稳定 source_id，供反幻觉追溯。"""
    raw = "|".join(str(part or "") for part in parts)
    return prefix + "_" + hashlib.sha1(raw.encode("utf-8", errors="ignore"), usedforsecurity=False).hexdigest()[:12]


def validate_market_for_need(
    need: dict,
    *,
    topic: str = "",
    known_competitors: list[str] | None = None,
    search_queries: list[str] | None = None,
    market_region: str = "US",
    max_queries: int = 8,
) -> dict[str, Any] | None:
    """用 SensorTower 细分赛道数据为需求卡片生成商业化信号。"""
    min_competitor_revenue = 100_000.0
    queries = _market_validation_queries(
        need,
        topic=topic,
        known_competitors=known_competitors,
        search_queries=search_queries,
        max_queries=max_queries,
    )
    if not queries:
        return None

    market = fetch_niche_market_data(queries, top_n=20, market_region=market_region)
    if not market:
        source_id = _market_source_id("no_match", topic, ",".join(queries), date.today().isoformat(), prefix="st_market")
        return {
            "level": "weak",
            "label": "商业信号弱",
            "source_id": source_id,
            "source_type": "sensor_tower_market",
            "competitor_count": 0,
            "top_competitors": [],
            "queries": queries,
            "risk_note": "SensorTower 未匹配到稳定竞品商业化信号（候选美区，收入/下载全球口径）。",
            "checked_at": date.today().isoformat(),
            "candidate_region": "US",
            "metrics_region": "全球",
            "market_region": "全球",
            "minimum_competitor_revenue": min_competitor_revenue,
        }

    top_apps = market.get("top_apps") or []
    signal_revenues = [float(app.get("revenue") or 0) for app in top_apps]
    signal_downloads = [float(app.get("downloads") or 0) for app in top_apps]
    signal_max_revenue = max(signal_revenues or [0.0])
    signal_total_revenue = sum(signal_revenues)
    signal_downloads_sum = sum(signal_downloads)
    signal_competitor_count = len(top_apps)
    competitors: list[dict[str, Any]] = []
    major_apps = [
        app for app in top_apps
        if float(app.get("revenue") or 0) >= min_competitor_revenue
    ]
    for app in major_apps[:5]:
        revenue = float(app.get("revenue") or 0)
        downloads = float(app.get("downloads") or 0)
        competitors.append({
            "source_id": _market_source_id(app.get("name"), app.get("publisher"), market.get("date_range"), prefix="st_app"),
            "source_type": "sensor_tower_app",
            "name": app.get("name") or "",
            "publisher": app.get("publisher") or "",
            "store_url": app.get("store_url") or app.get("app_store_url") or "",
            "app_store_url": app.get("app_store_url") or app.get("store_url") or "",
            "sensor_tower_url": app.get("sensor_tower_url") or _sensor_tower_search_url(app.get("name", "")),
            "revenue": revenue,
            "revenue_display": app.get("revenue_display") or _format_currency(revenue),
            "downloads": downloads,
            "downloads_display": app.get("downloads_display") or _format_number(downloads),
            "growth_pct": app.get("growth_pct"),
        })

    max_revenue = max([float(c.get("revenue") or 0) for c in competitors] or [0.0])
    total_revenue = sum(float(c.get("revenue") or 0) for c in competitors)
    downloads_sum = sum(float(c.get("downloads") or 0) for c in competitors)
    competitor_count = len(competitors)
    level, label = _market_level(
        signal_max_revenue,
        signal_total_revenue,
        signal_competitor_count,
        signal_downloads_sum,
    )

    growth_pct = market.get("revenue_growth_pct")
    growth_signal = "unknown"
    if isinstance(growth_pct, (int, float)):
        if growth_pct >= 5:
            growth_signal = "positive"
        elif growth_pct <= -5:
            growth_signal = "negative"
        else:
            growth_signal = "flat"

    if level == "strong":
        risk_note = "SensorTower 找到收入可观的同类或邻近应用（候选美区，收入/下载全球口径），商业化已被市场验证。"
    elif level == "medium":
        risk_note = "SensorTower 找到一定商业化信号（候选美区，收入/下载全球口径），但尚未达到强验证标准。"
    else:
        risk_note = "SensorTower 未匹配到全球月收入 $100K 以上的稳定竞品。"

    market_source_id = _market_source_id(topic, ",".join(queries), market.get("date_range"), signal_max_revenue, signal_total_revenue, prefix="st_market")
    return {
        "level": level,
        "label": label,
        "source_id": market_source_id,
        "source_type": "sensor_tower_market",
        "max_monthly_revenue": round(max_revenue, 2),
        "max_monthly_revenue_display": _format_currency(max_revenue) if max_revenue else "",
        "total_peer_revenue": round(total_revenue, 2),
        "total_peer_revenue_display": _format_currency(total_revenue) if total_revenue else "",
        "competitor_count": competitor_count,
        "growth_signal": growth_signal,
        "top_competitors": competitors,
        "queries": queries,
        "risk_note": risk_note,
        "checked_at": date.today().isoformat(),
        "candidate_region": market.get("candidate_region", "US"),
        "metrics_region": market.get("metrics_region", "全球"),
        "market_region": market.get("market_region", "全球"),
        "date_range": market.get("date_range") or {},
        "minimum_competitor_revenue": min_competitor_revenue,
        "signal_max_monthly_revenue": round(signal_max_revenue, 2),
        "signal_total_peer_revenue": round(signal_total_revenue, 2),
        "signal_competitor_count": signal_competitor_count,
    }


def format_for_report(competitors: list[dict]) -> str:
    """将竞品数据格式化为注入 prompt 的文本。"""
    if not competitors:
        return "（SensorTower 竞品数据未获取）"

    lines = ["### SensorTower 竞品数据（真实数据，报告中必须使用这些数字）\n"]

    for i, c in enumerate(competitors, 1):
        lines.append(f"#### {i}. {c['name']}")

        if c.get("has_st_data"):
            lines.append(f"- 月收入: {c.get('revenue_display', '-')}")
            lines.append(f"- 月下载: {c.get('downloads_display', '-')}")
            lines.append(f"- 月活跃: {c.get('mau_display', '-')}")
            lines.append(f"- 市占率: {c.get('market_share_display', '-')}")
            lines.append(f"- 6M增长: {c.get('growth_6m_display', '-')}")
            lines.append(f"- 上线时间: {c.get('first_release', '-')}")
            lines.append(f"- AI: {c.get('ai_label', '-')}")
            lines.append(f"- 链接: {c.get('store_url', '-')}")

            strengths = c.get("strengths", [])
            weaknesses = c.get("weaknesses", [])
            if strengths:
                lines.append(f"- 核心优势: {'; '.join(strengths[:3])}")
            if weaknesses:
                lines.append(f"- 核心劣势: {'; '.join(weaknesses[:3])}")

            comments = c.get("comments", [])
            if comments:
                lines.append("- App Store 用户评论:")
                for cm in comments:
                    stars = f"{'★' * (cm.get('rating') or 0)}{'☆' * (5 - (cm.get('rating') or 0))}"
                    lines.append(f'  - {stars} "{cm["content"][:200]}"')
        else:
            lines.append(f"- SensorTower 未匹配: {c.get('error', '未知原因')}")
            lines.append(f"- 链接: {c.get('store_url', '-')}")

        lines.append("")

    lines.append("---")
    lines.append("⚠️ 以上数据来自 SensorTower，报告中竞品概览表的数字列必须直接使用这些数据，不要编造或修改。")

    return "\n".join(lines)
