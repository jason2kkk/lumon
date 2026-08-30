"""
feishu_client.py — 飞书开放平台 API 封装

功能：将 Markdown 报告一键创建为飞书在线文档。
流程：获取 token → 创建空文档 → Markdown 转 blocks → 插入 blocks。
"""

import os
import time
import hashlib
import httpx
import concurrent.futures as _cf

_FEISHU_HOST = "https://open.feishu.cn"
_token_cache: dict[str, dict[str, float | str]] = {}
_http_client: httpx.Client | None = None


def _get_client() -> httpx.Client:
    global _http_client
    if _http_client is None or _http_client.is_closed:
        _http_client = httpx.Client(
            base_url=_FEISHU_HOST,
            timeout=30,
            limits=httpx.Limits(max_connections=10, max_keepalive_connections=5),
        )
    return _http_client


def _get_app_credentials(app_id: str | None = None, app_secret: str | None = None) -> tuple[str, str]:
    if app_id is not None or app_secret is not None:
        return app_id or "", app_secret or ""
    app_id = os.getenv("FEISHU_APP_ID", "")
    app_secret = os.getenv("FEISHU_APP_SECRET", "")
    return app_id, app_secret


def is_feishu_configured(app_id: str | None = None, app_secret: str | None = None) -> bool:
    app_id, app_secret = _get_app_credentials(app_id, app_secret)
    return bool(app_id and app_secret)


def _get_tenant_token(app_id: str | None = None, app_secret: str | None = None) -> str:
    now = time.time()
    app_id, app_secret = _get_app_credentials(app_id, app_secret)
    if not app_id or not app_secret:
        raise ValueError("飞书未配置：请在设置中填写 FEISHU_APP_ID 和 FEISHU_APP_SECRET")
    cache_key = hashlib.sha256(f"{app_id}\0{app_secret}".encode("utf-8")).hexdigest()
    cached = _token_cache.get(cache_key, {})
    if cached.get("token") and float(cached.get("expires_at", 0)) > now + 60:
        return str(cached["token"])

    client = _get_client()
    resp = client.post(
        "/open-apis/auth/v3/tenant_access_token/internal",
        json={"app_id": app_id, "app_secret": app_secret},
        timeout=10,
    )
    data = resp.json()
    if data.get("code") != 0:
        raise RuntimeError(f"获取飞书 token 失败: {data.get('msg', '未知错误')}")

    token = data["tenant_access_token"]
    expire = data.get("expire", 7200)
    _token_cache[cache_key] = {"token": token, "expires_at": now + expire}
    return token


def _headers(app_id: str | None = None, app_secret: str | None = None) -> dict:
    return {
        "Authorization": f"Bearer {_get_tenant_token(app_id, app_secret)}",
        "Content-Type": "application/json; charset=utf-8",
    }


def _api_post(path: str, body: dict, h: dict, timeout: int = 15) -> dict:
    """带重试的 API 调用，处理频率限制。"""
    client = _get_client()
    for attempt in range(3):
        resp = client.post(path, headers=h, json=body, timeout=timeout)
        if resp.status_code == 429 or (resp.text and "frequency limit" in resp.text):
            time.sleep(0.5 + attempt * 0.5)
            continue
        try:
            return resp.json()
        except Exception:
            if attempt < 2:
                time.sleep(0.3)
                continue
            raise RuntimeError(f"飞书 API 返回异常: status={resp.status_code}")
    raise RuntimeError("飞书 API 频率限制，请稍后重试")


_STRIP_KEYS = {"block_id", "parent_id", "merge_info"}


def _clean_block(block: dict) -> dict:
    """清理 block 的临时/只读字段，保留 block_type 和内容。"""
    cleaned = {}
    for k, v in block.items():
        if k in _STRIP_KEYS or k == "children":
            continue
        cleaned[k] = v
    return cleaned


def _prepare_blocks(blocks: list, first_level_ids: list) -> tuple[list, dict]:
    """
    将 convert API 返回的扁平列表分成：
    - simple_blocks: 可直接插入的非 table 一级块
    - table_info: {table_block_id: {prop, cells_data}} 表格数据

    返回 (ordered_items, block_map)
    ordered_items: [("block", cleaned_block) | ("table", table_meta)] 保持原始顺序
    """
    block_map: dict[str, dict] = {}
    for b in blocks:
        bid = b.get("block_id", "")
        if bid:
            block_map[bid] = b

    def _resolve_children(block_id: str) -> list[dict]:
        raw = block_map.get(block_id)
        if not raw:
            return []
        node = _clean_block(raw)
        child_ids = raw.get("children", [])
        if child_ids:
            nested = []
            for cid in child_ids:
                child_nodes = _resolve_children(cid)
                if child_nodes:
                    nested.extend(child_nodes) if len(child_nodes) > 1 else nested.append(child_nodes[0])
            if nested:
                node["children"] = nested
        return [node]

    ordered_items: list[tuple[str, dict]] = []

    for bid in first_level_ids:
        raw = block_map.get(bid)
        if not raw:
            continue

        if raw.get("block_type") == 31:
            # 表格：提取结构信息
            table_data = raw.get("table", {})
            prop = table_data.get("property", {})
            col_size = prop.get("column_size", 1)
            row_size = prop.get("row_size", 1)
            flat_cell_ids = table_data.get("cells", [])

            # 收集每个 cell 的内容 blocks
            cells_content: list[list[dict]] = []
            for cell_id in flat_cell_ids:
                cell_raw = block_map.get(cell_id)
                if not cell_raw:
                    cells_content.append([])
                    continue
                child_ids = cell_raw.get("children", [])
                content_blocks = []
                for cid in child_ids:
                    resolved = _resolve_children(cid)
                    content_blocks.extend(resolved)
                cells_content.append(content_blocks)

            ordered_items.append(("table", {
                "row_size": row_size,
                "col_size": col_size,
                "cells_content": cells_content,
            }))
        else:
            resolved = _resolve_children(bid)
            if resolved:
                ordered_items.append(("block", resolved[0]))

    return ordered_items, block_map


def _insert_blocks(document_id: str, parent_id: str, blocks: list[dict],
                   h: dict, start_index: int) -> int:
    """批量插入普通 blocks，返回下一个可用 index。"""
    BATCH_SIZE = 50
    idx = start_index
    for i in range(0, len(blocks), BATCH_SIZE):
        batch = blocks[i:i + BATCH_SIZE]
        result = _api_post(
            f"/open-apis/docx/v1/documents/{document_id}/blocks/{parent_id}/children",
            {"children": batch, "index": idx},
            h, timeout=30,
        )
        if result.get("code") == 0:
            idx += len(batch)
        else:
            for block in batch:
                time.sleep(0.1)
                single = _api_post(
                    f"/open-apis/docx/v1/documents/{document_id}/blocks/{parent_id}/children",
                    {"children": [block], "index": idx},
                    h,
                )
                if single.get("code") == 0:
                    idx += 1
                else:
                    print(f"[feishu] 跳过 block type={block.get('block_type')}: "
                          f"{single.get('msg', '')[:80]}")
    return idx


def _insert_table(document_id: str, parent_id: str, table_meta: dict,
                  h: dict, insert_index: int) -> int:
    """
    两步法插入表格：
    1. 创建空表格 → 获取 cell ID 列表
    2. 往每个 cell 填入内容
    """
    row_size = table_meta["row_size"]
    col_size = table_meta["col_size"]
    cells_content = table_meta["cells_content"]

    # Step 1: 创建空表格
    table_block = {
        "block_type": 31,
        "table": {
            "property": {
                "row_size": row_size,
                "column_size": col_size,
            }
        }
    }
    result = _api_post(
        f"/open-apis/docx/v1/documents/{document_id}/blocks/{parent_id}/children",
        {"children": [table_block], "index": insert_index},
        h, timeout=20,
    )
    if result.get("code") != 0:
        print(f"[feishu] 创建空表格失败: {result.get('msg', '')[:100]}")
        return insert_index

    # 从响应中拿到 cell ID 列表（从左到右、从上到下）
    created_children = result.get("data", {}).get("children", [])
    if not created_children:
        return insert_index + 1

    table_created = created_children[0]
    cell_ids = table_created.get("children", [])

    # Step 2: 并发往每个 cell 填入内容
    def _fill_cell(ci_cid):
        ci, cid = ci_cid
        if ci >= len(cells_content) or not cells_content[ci]:
            return
        _api_post(
            f"/open-apis/docx/v1/documents/{document_id}/blocks/{cid}/children",
            {"children": cells_content[ci], "index": 0},
            h,
        )

    tasks = [(ci, cid) for ci, cid in enumerate(cell_ids)
             if ci < len(cells_content) and cells_content[ci]]
    with _cf.ThreadPoolExecutor(max_workers=5) as pool:
        pool.map(_fill_cell, tasks)

    return insert_index + 1


def create_feishu_doc(
    title: str,
    markdown_content: str,
    folder_token: str = "",
    *,
    app_id: str | None = None,
    app_secret: str | None = None,
) -> dict:
    """
    创建飞书文档并写入 Markdown 内容。
    返回: {"url": "https://xxx.feishu.cn/docx/xxx", "document_id": "xxx"}
    """
    h = _headers(app_id, app_secret)

    # 1. 创建空文档
    create_body: dict = {"title": title}
    if folder_token:
        create_body["folder_token"] = folder_token
    create_data = _api_post("/open-apis/docx/v1/documents", create_body, h)
    if create_data.get("code") != 0:
        raise RuntimeError(f"创建飞书文档失败: {create_data.get('msg', '未知错误')}")

    doc_info = create_data["data"]["document"]
    document_id = doc_info["document_id"]
    doc_url = doc_info.get("url") or f"https://feishu.cn/docx/{document_id}"

    # 2. Markdown 转 blocks
    convert_data = _api_post(
        "/open-apis/docx/v1/documents/blocks/convert",
        {"content_type": "markdown", "content": markdown_content},
        h, timeout=30,
    )
    if convert_data.get("code") != 0:
        raise RuntimeError(f"Markdown 转换失败: {convert_data.get('msg', '未知错误')}")

    blocks = convert_data["data"].get("blocks", [])
    first_level_ids = convert_data["data"].get("first_level_block_ids", [])

    if not blocks or not first_level_ids:
        return {"url": doc_url, "document_id": document_id}

    # 3. 准备并插入内容
    ordered_items, _ = _prepare_blocks(blocks, first_level_ids)
    insert_index = 0

    # 将连续的普通 block 合并批量插入，遇到 table 则单独处理
    pending_blocks: list[dict] = []

    def flush_pending():
        nonlocal insert_index, pending_blocks
        if pending_blocks:
            insert_index = _insert_blocks(document_id, document_id, pending_blocks, h, insert_index)
            pending_blocks = []

    for item_type, item_data in ordered_items:
        if item_type == "block":
            pending_blocks.append(item_data)
        elif item_type == "table":
            flush_pending()
            insert_index = _insert_table(document_id, document_id, item_data, h, insert_index)

    flush_pending()

    return {"url": doc_url, "document_id": document_id}
