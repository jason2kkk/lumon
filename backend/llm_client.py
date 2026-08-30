"""
llm_client.py — LLM 统一调用层

职责：通过 OpenAI SDK 适配中转站 base_url，为 Claude 和 GPT 各维护一个客户端实例
      支持角色级别的模型选择（默认全用 Claude，可选 GPT）
注意：当配置变更后，需调用 reset_clients() 使缓存失效

多用户支持：通过 _thread_session_ctx (threading.local) 实现。
当 api_routes 设置了线程本地的 session context 后，所有 LLM 调用
（包括 debate.py / web_search.py / quote_extractor.py 中的）都会自动
路由到对应 session 的客户端实例，无需修改这些模块。
"""

import os
import json
import threading
from pathlib import Path
from openai import OpenAI
from dotenv import load_dotenv

load_dotenv()

_thread_local = threading.local()


def set_thread_session(ctx):
    """设置当前线程的 session context。由 api_routes 在请求处理前调用。"""
    _thread_local.session_ctx = ctx


def get_thread_session():
    """获取当前线程的 session context，没有则返回 None（回退到全局）。"""
    return getattr(_thread_local, "session_ctx", None)


def clear_thread_session():
    """清除当前线程的 session context。"""
    _thread_local.session_ctx = None

ROOT = Path(__file__).resolve().parent.parent
_ROLE_MODEL_FILE = ROOT / "data" / "cache" / "role_models.json"
_GENERAL_MODEL_FILE = ROOT / "data" / "cache" / "general_model.json"
_TOKEN_STATS_FILE = ROOT / "data" / "cache" / "token_stats.json"

_claude_client: OpenAI | None = None
_gpt_client: OpenAI | None = None
_claude_config_hash: str = ""
_gpt_config_hash: str = ""

_runtime_config: dict = {}

# Token 使用量统计
_token_lock = threading.Lock()
_token_stats: dict[str, dict[str, int]] = {
    "claude": {"input": 0, "output": 0, "calls": 0},
    "gpt": {"input": 0, "output": 0, "calls": 0},
}


class LLMResponseError(RuntimeError):
    """模型网关返回了无法安全用于产品流程的响应。"""


def parse_chat_completion_response(response) -> tuple[str, object]:
    """校验并提取 Chat Completions 响应，拒绝 HTML 或非标准结构。"""
    if isinstance(response, str):
        normalized = response.lstrip().lower()
        if normalized.startswith("<!doctype html") or "<html" in normalized[:500]:
            raise LLMResponseError(
                "模型接口返回了网页内容，请检查 Base URL 是否包含 /v1"
            )
        raise LLMResponseError("模型接口返回了非标准文本响应，请确认中转站兼容 Chat Completions")

    if isinstance(response, dict):
        choices = response.get("choices")
        usage = response.get("usage")
    else:
        choices = getattr(response, "choices", None)
        usage = getattr(response, "usage", None)
    if not choices:
        raise LLMResponseError("模型接口返回中缺少 choices，无法继续处理")

    first = choices[0]
    if isinstance(first, dict):
        message = first.get("message") or {}
        content = message.get("content") if isinstance(message, dict) else None
    else:
        message = getattr(first, "message", None)
        content = getattr(message, "content", None)
    if not isinstance(content, str) or not content.strip():
        raise LLMResponseError("模型接口返回了空内容，无法继续处理")
    return content, usage


def connection_test_error_message(error: Exception) -> str:
    """Return a useful settings-page error without exposing request details."""
    if isinstance(error, LLMResponseError):
        return str(error)
    message = str(error).lower()
    status_code = getattr(error, "status_code", None)
    if status_code in (401, 403) or "unauthorized" in message or "forbidden" in message:
        return "认证失败，请检查 API Key 和模型访问权限"
    if status_code == 429 or "rate limit" in message or "too many requests" in message:
        return "请求频率受限，请稍后再测试"
    if "timeout" in type(error).__name__.lower() or "timeout" in message or "timed out" in message:
        return "连接检测超时，请检查中转站状态或稍后重试"
    if "connection" in type(error).__name__.lower() or any(
        marker in message for marker in ("connection", "connect", "network", "dns", "ssl", "eof")
    ):
        return "无法连接模型服务，请检查 Base URL、中转站状态和本机网络"
    if isinstance(status_code, int) and status_code >= 500:
        return "模型服务暂时异常，请稍后再测试"
    return "连接失败，请检查 Base URL、API Key 和模型名称"


def is_transient_connection_error(error: BaseException) -> bool:
    """Identify transport setup failures without treating timeouts or API errors as reconnectable."""
    error_chain: list[str] = []
    current: BaseException | None = error
    while current is not None and len(error_chain) < 4:
        error_chain.append(f"{type(current).__name__}: {current}".lower())
        current = current.__cause__ or current.__context__
    error_text = " | ".join(error_chain)
    if "timeout" in error_text or "timed out" in error_text:
        return False
    return any(marker in error_text for marker in (
        "apiconnectionerror",
        "connecterror",
        "connection error",
        "connection reset",
        "unexpected_eof",
        "unexpected eof",
    ))


def _load_token_stats():
    global _token_stats
    if _TOKEN_STATS_FILE.exists():
        try:
            data = json.loads(_TOKEN_STATS_FILE.read_text(encoding="utf-8"))
            for k in ("claude", "gpt"):
                if k in data and isinstance(data[k], dict):
                    _token_stats[k] = {
                        "input": int(data[k].get("input", 0)),
                        "output": int(data[k].get("output", 0)),
                        "calls": int(data[k].get("calls", 0)),
                    }
        except Exception:
            pass


def _save_token_stats():
    try:
        _TOKEN_STATS_FILE.parent.mkdir(parents=True, exist_ok=True)
        _TOKEN_STATS_FILE.write_text(
            json.dumps(_token_stats, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception:
        pass


def record_usage(provider: str, usage):
    """从 OpenAI response.usage 记录 token 使用量。"""
    if not usage:
        return
    ctx = get_thread_session()
    if ctx:
        ctx.record_usage(provider, usage)
        return
    inp = getattr(usage, "prompt_tokens", 0) or 0
    out = getattr(usage, "completion_tokens", 0) or 0
    if inp == 0 and out == 0:
        return
    with _token_lock:
        _token_stats[provider]["input"] += inp
        _token_stats[provider]["output"] += out
        _token_stats[provider]["calls"] += 1
        _save_token_stats()


def get_token_stats() -> dict:
    with _token_lock:
        return {k: dict(v) for k, v in _token_stats.items()}


def reset_token_stats():
    global _token_stats
    with _token_lock:
        _token_stats = {
            "claude": {"input": 0, "output": 0, "calls": 0},
            "gpt": {"input": 0, "output": 0, "calls": 0},
        }
        _save_token_stats()


_load_token_stats()

_DEFAULT_CONFIG = {
    "CLAUDE_BASE_URL": "",
    "CLAUDE_API_KEY": "",
    "CLAUDE_MODEL": "claude-sonnet-4",
    "GPT_BASE_URL": "",
    "GPT_API_KEY": "",
    "GPT_MODEL": "gpt-5.4-mini",
}

# role → model provider mapping: "claude" or "gpt"
_role_model_map: dict[str, str] = {
    "director": "gpt",
    "analyst": "gpt",
    "critic": "gpt",
    "investor": "gpt",
}


def set_runtime_config(config: dict):
    """由 server.py 调用，设置运行时模型配置（优先于 .env）。"""
    global _runtime_config
    _runtime_config = config


def _load_role_model_config():
    """Load persisted role model config from disk."""
    global _role_model_map
    if _ROLE_MODEL_FILE.exists():
        try:
            data = json.loads(_ROLE_MODEL_FILE.read_text(encoding="utf-8"))
            for role in ("director", "analyst", "critic", "investor"):
                if role in data and data[role] in ("claude", "gpt"):
                    _role_model_map[role] = data[role]
        except Exception:
            pass


def get_role_model_config() -> dict:
    ctx = get_thread_session()
    if ctx:
        return ctx.get_role_model_config()
    return dict(_role_model_map)


def set_role_model_config(mapping: dict):
    global _role_model_map
    for role in ("director", "analyst", "critic", "investor"):
        if role in mapping and mapping[role] in ("claude", "gpt"):
            _role_model_map[role] = mapping[role]
    _ROLE_MODEL_FILE.parent.mkdir(parents=True, exist_ok=True)
    _ROLE_MODEL_FILE.write_text(json.dumps(_role_model_map, ensure_ascii=False), encoding="utf-8")


_load_role_model_config()


# 全局模型偏好：控制讨论角色和 WebSearch 以外的所有 LLM 调用
_general_model: str = "gpt"


def _load_general_model():
    global _general_model
    if _GENERAL_MODEL_FILE.exists():
        try:
            data = json.loads(_GENERAL_MODEL_FILE.read_text(encoding="utf-8"))
            if data.get("model") in ("claude", "gpt"):
                _general_model = data["model"]
        except Exception:
            pass


def get_general_model() -> str:
    return _general_model


def set_general_model(model: str):
    global _general_model
    if model in ("claude", "gpt"):
        _general_model = model
        _GENERAL_MODEL_FILE.parent.mkdir(parents=True, exist_ok=True)
        _GENERAL_MODEL_FILE.write_text(json.dumps({"model": model}), encoding="utf-8")


_load_general_model()


def get_provider_config(prefix: str) -> dict:
    """公开接口：返回指定 provider 的 base_url / api_key / model。"""
    ctx = get_thread_session()
    if ctx:
        return ctx.get_config(prefix)
    return _get_config(prefix)


def _get_config(prefix: str) -> dict:
    """
    读取模型配置。base_url / api_key / model 三者作为一组从同一来源获取：
    - 运行时 url+key 都有 → 用运行时全套（含 model）
    - 否则用 .env 全套
    防止部分覆盖导致配置错乱。
    """
    rt_url = _runtime_config.get(f"{prefix}_BASE_URL", "")
    rt_key = _runtime_config.get(f"{prefix}_API_KEY", "")
    rt_model = _runtime_config.get(f"{prefix}_MODEL", "")

    env_url = os.getenv(f"{prefix}_BASE_URL", "")
    env_key = os.getenv(f"{prefix}_API_KEY", "")
    env_model = os.getenv(f"{prefix}_MODEL", "")

    if rt_url and rt_key:
        base_url, api_key = rt_url, rt_key
        model = rt_model or env_model or _DEFAULT_CONFIG.get(f"{prefix}_MODEL", "")
    elif env_url and env_key:
        base_url, api_key = env_url, env_key
        model = env_model or _DEFAULT_CONFIG.get(f"{prefix}_MODEL", "")
    else:
        base_url = rt_url or env_url or _DEFAULT_CONFIG.get(f"{prefix}_BASE_URL", "")
        api_key = rt_key or env_key or _DEFAULT_CONFIG.get(f"{prefix}_API_KEY", "")
        model = rt_model or env_model or _DEFAULT_CONFIG.get(f"{prefix}_MODEL", "")

    return {"base_url": base_url, "api_key": api_key, "model": model}


def _config_hash(cfg: dict) -> str:
    return f"{cfg['base_url']}|{cfg['api_key'][:8] if cfg['api_key'] else ''}|{cfg['model']}"


_LLM_TIMEOUT = 180
_PREFLIGHT_TIMEOUT = 20

def _get_claude_client() -> OpenAI:
    global _claude_client, _claude_config_hash
    cfg = _get_config("CLAUDE")
    h = _config_hash(cfg)
    if _claude_client is None or h != _claude_config_hash:
        _claude_client = OpenAI(base_url=cfg["base_url"], api_key=cfg["api_key"], timeout=_LLM_TIMEOUT)
        _claude_config_hash = h
    return _claude_client


def _get_gpt_client() -> OpenAI:
    global _gpt_client, _gpt_config_hash
    cfg = _get_config("GPT")
    h = _config_hash(cfg)
    if _gpt_client is None or h != _gpt_config_hash:
        _gpt_client = OpenAI(base_url=cfg["base_url"], api_key=cfg["api_key"], timeout=_LLM_TIMEOUT)
        _gpt_config_hash = h
    return _gpt_client


def reset_clients():
    global _claude_client, _gpt_client, _claude_config_hash, _gpt_config_hash
    _claude_client = None
    _gpt_client = None
    _claude_config_hash = ""
    _gpt_config_hash = ""


def call_claude(
    messages: list[dict],
    max_tokens: int | None = None,
    *,
    timeout_seconds: float | None = None,
    max_attempts: int = 3,
) -> str:
    ctx = get_thread_session()
    if ctx:
        return ctx.call_claude(messages, max_tokens, timeout_seconds=timeout_seconds, max_attempts=max_attempts)
    client = _get_claude_client()
    cfg = _get_config("CLAUDE")
    kwargs: dict = {
        "model": cfg["model"],
        "messages": messages,
        "temperature": 0.7,
    }
    if max_tokens is not None:
        kwargs["max_tokens"] = max_tokens
    response = client.chat.completions.create(**kwargs)
    content, usage = parse_chat_completion_response(response)
    record_usage("claude", usage)
    return content


def call_gpt(
    messages: list[dict],
    max_tokens: int | None = None,
    *,
    timeout_seconds: float | None = None,
    max_attempts: int = 5,
) -> str:
    ctx = get_thread_session()
    if ctx:
        return ctx.call_gpt(messages, max_tokens, timeout_seconds=timeout_seconds, max_attempts=max_attempts)
    client = _get_gpt_client()
    cfg = _get_config("GPT")
    kwargs: dict = {
        "model": cfg["model"],
        "messages": messages,
        "temperature": 0.7,
    }
    if max_tokens is not None:
        kwargs["max_tokens"] = max_tokens
    response = client.chat.completions.create(**kwargs)
    content, usage = parse_chat_completion_response(response)
    record_usage("gpt", usage)
    return content


def check_llm_available() -> tuple[bool, str]:
    """轻量级检测当前配置的 LLM 是否可用。返回 (ok, error_message)。"""
    ctx = get_thread_session()
    if ctx:
        return ctx.check_llm_available()
    provider = _general_model
    if provider == "gpt":
        cfg = _get_config("GPT")
        if not cfg["api_key"]:
            return False, "GPT 模型不可用，请检查本地模型配置"
        try:
            client = _get_gpt_client().with_options(timeout=_PREFLIGHT_TIMEOUT, max_retries=0)
            response = client.chat.completions.create(
                model=cfg["model"],
                messages=[{"role": "user", "content": "Hi"}],
                max_tokens=5,
            )
            parse_chat_completion_response(response)
            return True, ""
        except Exception as e:
            return False, str(e) if isinstance(e, LLMResponseError) else "GPT 模型不可用，请检查本地模型配置"
    else:
        cfg = _get_config("CLAUDE")
        if not cfg["api_key"]:
            return False, "Claude 模型不可用，请检查本地模型配置"
        try:
            client = _get_claude_client().with_options(timeout=_PREFLIGHT_TIMEOUT, max_retries=0)
            response = client.chat.completions.create(
                model=cfg["model"],
                messages=[{"role": "user", "content": "Hi"}],
                max_tokens=5,
            )
            parse_chat_completion_response(response)
            return True, ""
        except Exception as e:
            return False, str(e) if isinstance(e, LLMResponseError) else "Claude 模型不可用，请检查本地模型配置"


def check_role_models_available() -> tuple[bool, str]:
    """检测角色分配使用的模型是否可用。"""
    ctx = get_thread_session()
    if ctx:
        return ctx.check_role_models_available()
    providers_needed = set(_role_model_map.values())
    for provider in providers_needed:
        if provider == "gpt":
            cfg = _get_config("GPT")
            if not cfg["api_key"]:
                return False, "角色模型不可用，请检查本地模型配置"
            for attempt in range(2):
                try:
                    client = _get_gpt_client().with_options(timeout=_PREFLIGHT_TIMEOUT, max_retries=0)
                    response = client.chat.completions.create(
                        model=cfg["model"], messages=[{"role": "user", "content": "Hi"}], max_tokens=5)
                    parse_chat_completion_response(response)
                    break
                except Exception as e:
                    if attempt == 0 and is_transient_connection_error(e):
                        import time as _t
                        _t.sleep(1)
                        continue
                    return False, connection_test_error_message(e)
        else:
            cfg = _get_config("CLAUDE")
            if not cfg["api_key"]:
                return False, "角色模型不可用，请检查本地模型配置"
            for attempt in range(2):
                try:
                    client = _get_claude_client().with_options(timeout=_PREFLIGHT_TIMEOUT, max_retries=0)
                    response = client.chat.completions.create(
                        model=cfg["model"], messages=[{"role": "user", "content": "Hi"}], max_tokens=5)
                    parse_chat_completion_response(response)
                    break
                except Exception as e:
                    if attempt == 0 and is_transient_connection_error(e):
                        import time as _t
                        _t.sleep(1)
                        continue
                    return False, connection_test_error_message(e)
    return True, ""


def call_llm(
    messages: list[dict],
    max_tokens: int | None = None,
    *,
    timeout_seconds: float | None = None,
    max_attempts: int | None = None,
) -> str:
    """根据全局模型偏好调用 LLM（非讨论、非 WebSearch 场景）。"""
    ctx = get_thread_session()
    if ctx:
        return ctx.call_llm(
            messages,
            max_tokens,
            timeout_seconds=timeout_seconds,
            max_attempts=max_attempts,
        )
    if _general_model == "gpt":
        cfg = _get_config("GPT")
        if cfg["api_key"]:
            return call_gpt(
                messages,
                max_tokens,
                timeout_seconds=timeout_seconds,
                max_attempts=max_attempts if max_attempts is not None else 5,
            )
        raise RuntimeError("GPT 模型不可用，请检查本地模型配置")
    cfg_c = _get_config("CLAUDE")
    if not cfg_c["api_key"]:
        raise RuntimeError("Claude 模型不可用，请检查本地模型配置")
    return call_claude(
        messages,
        max_tokens,
        timeout_seconds=timeout_seconds,
        max_attempts=max_attempts if max_attempts is not None else 3,
    )


def call_llm_stream(messages: list[dict], max_tokens: int | None = None):
    """流式调用 LLM，根据用户设置的 general_model 选择 GPT 或 Claude。"""
    ctx = get_thread_session()
    if ctx:
        yield from ctx.call_llm_stream(messages, max_tokens)
        return
    if _general_model == "gpt":
        cfg = _get_config("GPT")
        if cfg["api_key"]:
            yield from call_gpt_stream(messages, max_tokens)
            return
        raise RuntimeError("GPT 模型不可用，请检查本地模型配置")
    cfg_c = _get_config("CLAUDE")
    if not cfg_c["api_key"]:
        raise RuntimeError("Claude 模型不可用，请检查本地模型配置")
    yield from call_claude_stream(messages, max_tokens)


def call_claude_stream(
    messages: list[dict],
    max_tokens: int | None = None,
    *,
    timeout_seconds: float | None = None,
    max_attempts: int = 3,
):
    ctx = get_thread_session()
    if ctx:
        yield from ctx.call_claude_stream(
            messages,
            max_tokens,
            timeout_seconds=timeout_seconds,
            max_attempts=max_attempts,
        )
        return
    client = _get_claude_client()
    request_client = client.with_options(timeout=timeout_seconds, max_retries=0) if timeout_seconds else client
    cfg = _get_config("CLAUDE")

    for attempt in range(max_attempts):
        had_content = False
        try:
            kwargs: dict = {
                "model": cfg["model"],
                "messages": messages,
                "temperature": 0.7,
                "stream": True,
                "stream_options": {"include_usage": True},
            }
            if max_tokens is not None:
                kwargs["max_tokens"] = max_tokens
            stream = request_client.chat.completions.create(**kwargs)
            for chunk in stream:
                if chunk.usage:
                    record_usage("claude", chunk.usage)
                if chunk.choices and chunk.choices[0].delta.content:
                    had_content = True
                    yield chunk.choices[0].delta.content
            return
        except Exception as e:
            err_msg = str(e).lower()
            print(f"[Claude] stream attempt {attempt+1}/{max_attempts} failed (had_content={had_content}): {e}")
            if had_content:
                raise RuntimeError("模型输出中断，请重试并检查模型服务状态") from e
            retryable = ("connection" in err_msg or "timeout" in err_msg or "timed out" in err_msg
                         or "500" in err_msg or "server" in err_msg or "stream" in err_msg
                         or "one_api" in err_msg or ("401" in err_msg and "令牌" in err_msg))
            if attempt < max_attempts - 1 and retryable:
                import time
                time.sleep(2 ** attempt)
                continue
            raise


def call_gpt_stream(
    messages: list[dict],
    max_tokens: int | None = None,
    *,
    timeout_seconds: float | None = None,
    max_attempts: int = 5,
):
    ctx = get_thread_session()
    if ctx:
        yield from ctx.call_gpt_stream(
            messages,
            max_tokens,
            timeout_seconds=timeout_seconds,
            max_attempts=max_attempts,
        )
        return
    client = _get_gpt_client()
    request_client = client.with_options(timeout=timeout_seconds, max_retries=0) if timeout_seconds else client
    cfg = _get_config("GPT")
    model = cfg["model"]

    for attempt in range(max_attempts):
        had_content = False
        try:
            kwargs: dict = {
                "model": model,
                "messages": messages,
                "temperature": 0.7,
                "stream": True,
            }
            if attempt == 0:
                kwargs["stream_options"] = {"include_usage": True}
            if max_tokens is not None:
                kwargs["max_tokens"] = max_tokens

            stream = request_client.chat.completions.create(**kwargs)
            for chunk in stream:
                if hasattr(chunk, 'usage') and chunk.usage:
                    record_usage("gpt", chunk.usage)
                if chunk.choices and chunk.choices[0].delta.content:
                    had_content = True
                    yield chunk.choices[0].delta.content
            return
        except Exception as e:
            err_msg = str(e).lower()
            print(f"[GPT] stream attempt {attempt+1}/{max_attempts} failed (had_content={had_content}): {e}")
            if had_content:
                raise RuntimeError("模型输出中断，请重试并检查模型服务状态") from e
            retryable = ("connection" in err_msg or "timeout" in err_msg or "timed out" in err_msg
                         or "stream" in err_msg or "codex" in err_msg or "500" in err_msg
                         or "server" in err_msg or "one_api" in err_msg
                         or ("401" in err_msg and "令牌" in err_msg)
                         or "rate" in err_msg or "429" in err_msg)
            if attempt < max_attempts - 1 and retryable:
                import time
                _delay = min(3 * (2 ** attempt), 30)
                time.sleep(_delay)
                continue
            raise


def test_connection(prefix: str, override: dict | None = None) -> tuple[bool, str]:
    cfg = _get_config(prefix)
    if override:
        for k, v in override.items():
            if v:
                cfg[k] = v
    if not cfg["base_url"] or not cfg["api_key"]:
        return False, "Missing base_url or api_key"
    try:
        client = OpenAI(
            base_url=cfg["base_url"], api_key=cfg["api_key"],
            timeout=_PREFLIGHT_TIMEOUT, max_retries=0,
        )
        resp = client.chat.completions.create(
            model=cfg["model"],
            messages=[{"role": "user", "content": "Say OK"}],
            max_tokens=10,
        )
        content, _usage = parse_chat_completion_response(resp)
        return True, f"Connected. Response: {content[:50]}"
    except Exception as e:
        return False, connection_test_error_message(e)


def call_for_role(
    role: str,
    messages: list[dict],
    max_tokens: int | None = None,
    *,
    timeout_seconds: float | None = None,
    max_attempts: int | None = None,
) -> str:
    """Non-streaming LLM call for a given role, using the configured model provider."""
    ctx = get_thread_session()
    if ctx:
        return ctx.call_for_role(
            role,
            messages,
            max_tokens,
            timeout_seconds=timeout_seconds,
            max_attempts=max_attempts,
        )
    print(f"[WARN] call_for_role({role}): no thread session! global _role_model_map={_role_model_map}")
    provider = _role_model_map.get(role, "gpt")
    if provider == "gpt":
        cfg_g = _get_config("GPT")
        if not cfg_g["api_key"]:
            raise RuntimeError("角色模型不可用，请检查本地模型配置")
        return call_gpt(
            messages,
            max_tokens,
            timeout_seconds=timeout_seconds,
            max_attempts=max_attempts if max_attempts is not None else 5,
        )
    cfg_g_fallback = _get_config("GPT")
    if cfg_g_fallback["api_key"]:
        print(f"[WARN] role={role} mapped to claude but Claude unavailable, using GPT")
        return call_gpt(
            messages,
            max_tokens,
            timeout_seconds=timeout_seconds,
            max_attempts=max_attempts if max_attempts is not None else 5,
        )
    cfg_c = _get_config("CLAUDE")
    if not cfg_c["api_key"]:
        raise RuntimeError("角色模型不可用，请检查本地模型配置")
    return call_claude(
        messages,
        max_tokens,
        timeout_seconds=timeout_seconds,
        max_attempts=max_attempts if max_attempts is not None else 3,
    )


def call_for_role_stream(
    role: str,
    messages: list[dict],
    max_tokens: int | None = None,
    *,
    timeout_seconds: float | None = None,
    max_attempts: int | None = None,
):
    """Stream LLM output for a given role, using the configured model provider."""
    ctx = get_thread_session()
    if ctx:
        return ctx.call_for_role_stream(
            role,
            messages,
            max_tokens,
            timeout_seconds=timeout_seconds,
            max_attempts=max_attempts,
        )
    print(f"[WARN] call_for_role_stream({role}): no thread session! global _role_model_map={_role_model_map}")
    provider = _role_model_map.get(role, "gpt")
    if provider == "gpt":
        cfg_g = _get_config("GPT")
        if not cfg_g["api_key"]:
            raise RuntimeError("角色模型不可用，请检查本地模型配置")
        return call_gpt_stream(
            messages,
            max_tokens,
            timeout_seconds=timeout_seconds,
            max_attempts=max_attempts if max_attempts is not None else 5,
        )
    cfg_g_fallback = _get_config("GPT")
    if cfg_g_fallback["api_key"]:
        print(f"[WARN] role={role} mapped to claude but Claude unavailable, using GPT")
        return call_gpt_stream(
            messages,
            max_tokens,
            timeout_seconds=timeout_seconds,
            max_attempts=max_attempts if max_attempts is not None else 5,
        )
    cfg_c = _get_config("CLAUDE")
    if not cfg_c["api_key"]:
        raise RuntimeError("角色模型不可用，请检查本地模型配置")
    return call_claude_stream(
        messages,
        max_tokens,
        timeout_seconds=timeout_seconds,
        max_attempts=max_attempts if max_attempts is not None else 3,
    )


def estimate_tokens(text: str) -> int:
    return max(1, len(text) // 3)


def estimate_debate_cost(post_text: str, num_comments: int, max_rounds: int) -> dict:
    base_input = estimate_tokens(post_text)
    comment_tokens = num_comments * 150
    output_per_step = 1500
    steps = 2 + (max_rounds * 2) + 1

    context_growth = 0
    for _ in range(steps):
        context_growth += output_per_step * 0.6

    total_input = int((base_input + comment_tokens) * steps + context_growth)
    total_output = int(output_per_step * steps)
    return {"input_tokens": total_input, "output_tokens": total_output, "total": total_input + total_output}


def check_config() -> dict:
    errors = []
    claude_ok = True
    gpt_ok = True
    cfg_c = _get_config("CLAUDE")
    cfg_g = _get_config("GPT")

    if not cfg_c["base_url"]:
        errors.append("Missing CLAUDE_BASE_URL")
        claude_ok = False
    if not cfg_c["api_key"]:
        errors.append("Missing CLAUDE_API_KEY")
        claude_ok = False
    if not cfg_g["base_url"]:
        gpt_ok = False
    if not cfg_g["api_key"]:
        gpt_ok = False

    return {"ready": claude_ok, "claude_ok": claude_ok, "gpt_ok": gpt_ok, "errors": errors}


def _mask_key(key: str) -> str:
    """将密钥掩码为 sk-...xxxx 格式，避免明文暴露到前端。"""
    if not key or len(key) < 8:
        return ""
    return key[:3] + "..." + key[-4:]


def get_config_values() -> dict:
    """Return current config values for the local frontend (keys are masked)."""
    cfg_c = _get_config("CLAUDE")
    cfg_g = _get_config("GPT")

    tavily_key = os.getenv("TAVILY_API_KEY", "")
    feishu_id = os.getenv("FEISHU_APP_ID", "")
    feishu_secret = os.getenv("FEISHU_APP_SECRET", "")
    return {
        "CLAUDE_BASE_URL": cfg_c["base_url"],
        "CLAUDE_API_KEY": _mask_key(cfg_c["api_key"]),
        "CLAUDE_API_KEY_SET": bool(cfg_c["api_key"]),
        "CLAUDE_MODEL": cfg_c["model"],
        "GPT_BASE_URL": cfg_g["base_url"],
        "GPT_API_KEY": _mask_key(cfg_g["api_key"]),
        "GPT_API_KEY_SET": bool(cfg_g["api_key"]),
        "GPT_MODEL": cfg_g["model"],
        "TAVILY_API_KEY": _mask_key(tavily_key),
        "FEISHU_APP_ID": feishu_id,
        "FEISHU_APP_SECRET": _mask_key(feishu_secret),
        "role_models": get_role_model_config(),
    }
