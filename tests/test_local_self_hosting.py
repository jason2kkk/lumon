"""本地自托管的访问边界与用户凭据回归测试。"""

import importlib
import json
import os
import shutil
import stat
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

import backend.session_context as session_context
from backend.llm_client import clear_thread_session, get_thread_session, set_thread_session


class LocalAccessTests(unittest.TestCase):
    session_id = "local-self-hosting-test"

    def tearDown(self):
        shutil.rmtree(Path("data/sessions") / self.session_id, ignore_errors=True)

    def _client(self, *, env: dict[str, str], host: str, client_host: str) -> TestClient:
        keys = {
            "LUMON_LOCAL_ONLY": "1",
            "LUMON_TRUST_LOCAL_PROXY": "0",
            "LUMON_ACCESS_TOKEN": "",
            "LUMON_ALLOWED_ORIGINS": "http://localhost:5173,http://127.0.0.1:5173",
            "LUMON_MAX_REQUEST_BYTES": "8388608",
            **env,
        }
        with patch.dict(os.environ, keys, clear=False):
            import server

            module = importlib.reload(server)
        return TestClient(
            module.app,
            base_url=f"http://{host}",
            client=(client_host, 50000),
            headers={"X-Session-Id": self.session_id},
        )

    def test_local_request_is_allowed(self):
        client = self._client(env={}, host="127.0.0.1", client_host="127.0.0.1")
        self.assertEqual(client.get("/api/config/status").status_code, 200)

    def test_non_loopback_host_is_rejected(self):
        client = self._client(env={}, host="example.com", client_host="127.0.0.1")
        self.assertEqual(client.get("/api/config/status").status_code, 403)

    def test_container_proxy_requires_explicit_trust(self):
        blocked = self._client(env={}, host="127.0.0.1", client_host="172.18.0.1")
        self.assertEqual(blocked.get("/api/config/status").status_code, 403)

        allowed = self._client(
            env={"LUMON_TRUST_LOCAL_PROXY": "1"},
            host="127.0.0.1",
            client_host="172.18.0.1",
        )
        self.assertEqual(allowed.get("/api/config/status").status_code, 200)

    def test_local_proxy_rejects_remote_browser_origin(self):
        client = self._client(
            env={"LUMON_TRUST_LOCAL_PROXY": "1"},
            host="127.0.0.1",
            client_host="172.18.0.1",
        )
        response = client.get(
            "/api/config/status",
            headers={"Origin": "https://public-tunnel.example.com"},
        )
        self.assertEqual(response.status_code, 403)

    def test_remote_mode_requires_matching_token(self):
        missing = self._client(
            env={"LUMON_LOCAL_ONLY": "0"},
            host="lumon.example.com",
            client_host="203.0.113.10",
        )
        self.assertEqual(missing.get("/api/config/status").status_code, 503)

        protected = self._client(
            env={
                "LUMON_LOCAL_ONLY": "0",
                "LUMON_ACCESS_TOKEN": "test-token",
                "LUMON_ALLOWED_ORIGINS": "https://lumon.example.com",
            },
            host="lumon.example.com",
            client_host="203.0.113.10",
        )
        self.assertEqual(protected.get("/healthz").status_code, 200)
        self.assertEqual(protected.get("/api/config/status").status_code, 401)
        self.assertEqual(
            protected.get(
                "/api/config/status",
                headers={
                    "Origin": "https://lumon.example.com",
                    "X-Lumon-Access-Token": "test-token",
                },
            ).status_code,
            200,
        )

    def test_request_size_limit(self):
        client = self._client(
            env={"LUMON_MAX_REQUEST_BYTES": "65536"},
            host="127.0.0.1",
            client_host="127.0.0.1",
        )
        response = client.post("/api/config", content=b"x" * 65537)
        self.assertEqual(response.status_code, 413)


class CredentialTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_sessions_dir = session_context.SESSIONS_DIR
        session_context.SESSIONS_DIR = Path(self.temp_dir.name)
        self.ctx = session_context.SessionContext("credential-test")

    def tearDown(self):
        clear_thread_session()
        session_context.SESSIONS_DIR = self.original_sessions_dir
        self.temp_dir.cleanup()

    def test_config_is_private_and_key_can_be_cleared(self):
        self.ctx.save_config({
            "GPT_BASE_URL": "https://one.example/v1",
            "GPT_API_KEY": "first-key",
            "GPT_MODEL": "test-model",
        })
        self.assertEqual(stat.S_IMODE(self.ctx.config_file.stat().st_mode), 0o600)

        self.ctx.save_config({
            "GPT_BASE_URL": "https://one.example/v1",
            "GPT_API_KEY": "",
            "GPT_MODEL": "test-model",
            "CLEAR_FIELDS": ["GPT_API_KEY"],
        })
        self.assertNotIn("GPT_API_KEY", self.ctx._runtime_config)

    def test_base_url_change_requires_a_new_key(self):
        self.ctx.save_config({
            "GPT_BASE_URL": "https://one.example/v1",
            "GPT_API_KEY": "first-key",
            "GPT_MODEL": "test-model",
        })
        with self.assertRaises(ValueError):
            self.ctx.save_config({
                "GPT_BASE_URL": "https://two.example/v1",
                "GPT_API_KEY": "",
                "GPT_MODEL": "test-model",
            })

    def test_unsafe_provider_urls_are_rejected(self):
        for url in ("file:///tmp/model", "http://example.com/v1", "https://10.0.0.1/v1"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                self.ctx.save_config({"GPT_BASE_URL": url})

    def test_tavily_client_uses_session_key(self):
        captured: dict[str, str] = {}

        class FakeTavilyClient:
            def __init__(self, api_key: str):
                captured["api_key"] = api_key

        fake_module = types.SimpleNamespace(TavilyClient=FakeTavilyClient)
        self.ctx._runtime_config["TAVILY_API_KEY"] = "session-tavily-key"
        set_thread_session(self.ctx)
        with patch.dict(sys.modules, {"tavily": fake_module}):
            from backend.web_search import _get_tavily_client

            _get_tavily_client()
        self.assertEqual(captured["api_key"], "session-tavily-key")


class SyntheticDemoDataTests(unittest.TestCase):
    def test_demo_data_contains_only_declared_synthetic_sources(self):
        demo_dir = Path("data/demo")
        needs = json.loads((demo_dir / "demo_needs.json").read_text(encoding="utf-8"))
        self.assertEqual(len(needs), 3)
        for need in needs:
            self.assertIn("合成演示数据", need["original_topic"])
            for post in need["posts"]:
                self.assertEqual(post["source"], "synthetic/lumon-demo")
                self.assertEqual(post["_engine"], "synthetic-demo")
                self.assertTrue(post["url"].startswith("https://example.com/lumon-demo/"))

        combined = "\n".join(path.read_text(encoding="utf-8") for path in demo_dir.glob("*.json")).lower()
        for forbidden in ("reddit.com", "news.ycombinator.com"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, combined)


class DebateStreamingTests(unittest.TestCase):
    session_id = "debate-streaming-test"

    def setUp(self):
        self.session_dir = Path("data/sessions") / self.session_id
        self.session_dir.mkdir(parents=True, exist_ok=True)
        (self.session_dir / "fetched_needs.json").write_text(
            json.dumps([
                {
                    "need_title": "测试需求",
                    "need_description": "用于验证讨论首条消息",
                    "posts": [],
                },
            ], ensure_ascii=False),
            encoding="utf-8",
        )

        with patch.dict(os.environ, {
            "LUMON_LOCAL_ONLY": "1",
            "LUMON_TRUST_LOCAL_PROXY": "0",
            "LUMON_ACCESS_TOKEN": "",
            "LUMON_ALLOWED_ORIGINS": "http://127.0.0.1:5173",
        }, clear=False):
            import server

            module = importlib.reload(server)
        self.client = TestClient(
            module.app,
            base_url="http://127.0.0.1",
            client=("127.0.0.1", 50000),
            headers={"X-Session-Id": self.session_id},
        )

    def tearDown(self):
        with session_context._sessions_lock:
            session_context._sessions.pop(self.session_id, None)
        shutil.rmtree(self.session_dir, ignore_errors=True)

    def test_opening_is_streamed_before_role_preflight_error(self):
        with (
            patch("backend.api_routes.check_role_models_available", return_value=(False, "test failure")),
            patch("backend.api_routes._provider_for_role", return_value="gpt"),
            patch("time.sleep", return_value=None),
        ):
            response = self.client.post(
                "/api/debate/start",
                json={"need_index": 0, "max_rounds": 1, "language": "zh-CN"},
            )

        body = response.text
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers.get("x-accel-buffering"), "no")
        self.assertIn("no-transform", response.headers.get("cache-control", ""))
        self.assertLess(body.index("event: message_start"), body.index("event: error"))
        self.assertIn("测试需求", body)

    def test_debate_role_calls_use_bounded_single_requests(self):
        from backend import api_routes

        with patch("backend.api_routes.call_for_role_stream", return_value=iter(["ok"])) as mocked:
            self.assertEqual(list(api_routes._stream_role("director", [])), ["ok"])

        mocked.assert_called_once_with(
            "director",
            [],
            None,
            timeout_seconds=90,
            max_attempts=1,
        )

    def test_debate_role_reconnects_once_before_first_chunk(self):
        from backend import api_routes

        def disconnected():
            raise ConnectionError("connection reset")
            yield "unreachable"

        with (
            patch(
                "backend.api_routes.call_for_role_stream",
                side_effect=[disconnected(), iter(["ok"])],
            ) as mocked,
            patch("time.sleep", return_value=None),
        ):
            self.assertEqual(list(api_routes._stream_role("analyst", [])), ["ok"])

        self.assertEqual(mocked.call_count, 2)

    def test_debate_role_does_not_retry_after_partial_output(self):
        from backend import api_routes

        def interrupted():
            yield "partial"
            raise ConnectionError("connection reset")

        with patch(
            "backend.api_routes.call_for_role_stream",
            return_value=interrupted(),
        ) as mocked:
            stream = api_routes._stream_role("critic", [])
            self.assertEqual(next(stream), "partial")
            with self.assertRaises(ConnectionError):
                next(stream)

        mocked.assert_called_once()


class GeneratedArtifactFlowTests(unittest.TestCase):
    session_id = "generated-artifact-flow-test"

    def setUp(self):
        self.session_dir = Path("data/sessions") / self.session_id
        self.session_dir.mkdir(parents=True, exist_ok=True)
        demo_needs = json.loads(Path("data/demo/demo_needs.json").read_text(encoding="utf-8"))
        (self.session_dir / "fetched_needs.json").write_text(
            json.dumps(demo_needs, ensure_ascii=False),
            encoding="utf-8",
        )
        with patch.dict(os.environ, {
            "LUMON_LOCAL_ONLY": "1",
            "LUMON_TRUST_LOCAL_PROXY": "0",
            "LUMON_ACCESS_TOKEN": "",
            "LUMON_ALLOWED_ORIGINS": "http://127.0.0.1:5173",
        }, clear=False):
            import server

            module = importlib.reload(server)
        self.client = TestClient(
            module.app,
            base_url="http://127.0.0.1",
            client=("127.0.0.1", 50000),
            headers={"X-Session-Id": self.session_id},
        )

    def tearDown(self):
        with session_context._sessions_lock:
            session_context._sessions.pop(self.session_id, None)
        shutil.rmtree(self.session_dir, ignore_errors=True)

    def test_persona_preflight_failure_uses_persona_error_event(self):
        with patch("backend.api_routes.check_llm_available", return_value=(False, "test failure")):
            response = self.client.post(
                "/api/generate-personas",
                json={"need_index": 0, "language": "zh-CN"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertIn("event: persona_error", response.text)
        self.assertNotIn("event: error\n", response.text)

    def test_demo_report_is_saved_and_listed_for_the_session(self):
        with patch("time.sleep", return_value=None):
            response = self.client.post(
                "/api/generate-report",
                json={"need_index": 0, "demo": True, "language": "zh-CN"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertIn('"filename": "demo_report.json"', response.text)
        reports = self.client.get("/api/reports").json()["reports"]
        self.assertTrue(any(report["filename"] == "demo_report.json" for report in reports))
        report = self.client.get("/api/reports/demo_report.json")
        self.assertEqual(report.status_code, 200)
        self.assertIn("合成演示内容", report.json()["final_report"])


class PublicBoundaryTests(unittest.TestCase):
    def test_fetch_depth_values_do_not_select_a_model(self):
        from backend import api_routes

        self.assertEqual(api_routes._normalize_fetch_strategy("default"), "fast")
        self.assertEqual(api_routes._normalize_fetch_strategy("fast"), "fast")
        self.assertEqual(api_routes._normalize_fetch_strategy("deep"), "deep")
        self.assertEqual(api_routes._normalize_fetch_strategy("review:legacy-model"), "deep")
        self.assertEqual(api_routes._normalize_fetch_strategy("full:legacy-model"), "deep")

    def test_fetch_job_keeps_the_session_general_model(self):
        from backend import api_routes

        ctx = session_context.SessionContext("fetch-model-selection-test")
        ctx._general_model = "claude"
        ctx._runtime_config.update({
            "CLAUDE_BASE_URL": "https://claude.example/v1",
            "CLAUDE_API_KEY": "session-claude-key",
            "CLAUDE_MODEL": "user-selected-claude",
        })
        captured: dict[str, str] = {}

        def check_selected_model():
            captured["provider"] = ctx.get_general_model()
            captured["model"] = ctx.get_config("CLAUDE")["model"]
            return False, "stop after model selection"

        with (
            patch.dict(os.environ, {
                "GPT_BASE_URL": "https://environment-gpt.example/v1",
                "GPT_API_KEY": "environment-gpt-key",
                "GPT_MODEL": "environment-gpt-model",
            }, clear=False),
            patch("backend.api_routes.check_llm_available", side_effect=check_selected_model),
        ):
            api_routes._run_fetch_job(ctx, {
                "mode": "sentence",
                "query": "test demand",
                "sources": ["reddit"],
                "fetch_model": "deep",
            })

        self.assertEqual(captured, {"provider": "claude", "model": "user-selected-claude"})
        self.assertEqual(ctx.get_general_model(), "claude")
        with session_context._sessions_lock:
            session_context._sessions.pop(ctx.session_id, None)
        shutil.rmtree(ctx.data_dir, ignore_errors=True)

    def test_quick_search_keeps_the_session_general_model(self):
        from backend import api_routes

        ctx = session_context.SessionContext("quick-search-model-selection-test")
        ctx._general_model = "claude"
        ctx._runtime_config.update({
            "CLAUDE_BASE_URL": "https://claude.example/v1",
            "CLAUDE_API_KEY": "session-claude-key",
            "CLAUDE_MODEL": "user-selected-claude",
        })
        captured: dict[str, str] = {}

        def capture_translation_model(*_args, **_kwargs):
            active_ctx = get_thread_session()
            captured["provider"] = active_ctx.get_general_model()
            captured["model"] = active_ctx.get_config("CLAUDE")["model"]

        try:
            with (
                patch("backend.api_routes._get_session", return_value=ctx),
                patch("backend.api_routes._qs_translate_app_reviews", side_effect=capture_translation_model),
            ):
                response = api_routes.translate_quick_search_reviews(
                    api_routes.QuickSearchReviewTranslateRequest(reviews=[{
                        "id": "review-1",
                        "title": "A review",
                        "content": "Needs translation",
                    }]),
                    object(),
                )

            self.assertTrue(response["ok"])
            self.assertEqual(captured, {"provider": "claude", "model": "user-selected-claude"})
            self.assertIsNone(get_thread_session())
        finally:
            clear_thread_session()
            with session_context._sessions_lock:
                session_context._sessions.pop(ctx.session_id, None)
            shutil.rmtree(ctx.data_dir, ignore_errors=True)

    def test_private_integration_markers_are_absent(self):
        source_paths = [
            Path(".env.example"),
            Path("README.md"),
            Path("backend/api_routes.py"),
            Path("frontend/src/App.tsx"),
            Path("frontend/src/components/FetchView.tsx"),
            Path("frontend/src/i18n.tsx"),
            Path("frontend/package.json"),
            Path("frontend/vite.config.ts"),
            Path("prompts/poc_eval.py"),
        ]
        combined = "\n".join(path.read_text(encoding="utf-8") for path in source_paths)
        forbidden_markers = (
            "红" + "毛丹",
            "Hong" + "maodan",
            "准" + "入",
            "UR-" + "poc-evaluator",
            "5" + " Gate",
            "review:" + "gpt-5.5",
            "CLI_" + "API_KEY",
            "/cli" + "/st",
            "auto" + "start",
            "cloudflared" + " tunnel",
            "VITE_DEV_" + "TUNNEL_HOST",
        )
        for forbidden in forbidden_markers:
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, combined)

        self.assertFalse(Path("docs/ST-cli-api.md").exists())
        self.assertFalse(Path("docs/lumon-url-prefill-api.md").exists())


if __name__ == "__main__":
    unittest.main()
