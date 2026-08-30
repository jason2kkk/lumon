"""Web Search 能力检测的结果分类与重试测试。"""

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from backend.api_routes import (
    _fallback_needs,
    _qs_apply_intent_guard,
    _qs_diversify_process_posts,
    _qs_filter_process_evidence,
    _qs_flatten_plan_queries,
    _qs_is_process_research_query,
    _qs_normalize_process_queries,
    _qs_prioritize_process_subreddits,
    _qs_process_evidence_issue,
    _qs_sanitize_process_summary,
    _qs_sanitize_history_items,
    _qs_validate_community_plan,
    _report_repair_reddit_links,
    _web_search_probe_message,
)
from backend.llm_client import (
    LLMResponseError,
    connection_test_error_message,
    parse_chat_completion_response,
)
from backend.web_search import (
    _classify_web_search_probe_error,
    _probe_responses_web_search,
)


class FakeResponses:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = 0

    def create(self, **_kwargs):
        outcome = self.outcomes[self.calls]
        self.calls += 1
        if isinstance(outcome, Exception):
            raise outcome
        return SimpleNamespace(output_text=outcome, usage=None)


class FakeClient:
    def __init__(self, outcomes):
        self.responses = FakeResponses(outcomes)


class HttpError(Exception):
    def __init__(self, status_code: int, message: str):
        super().__init__(message)
        self.status_code = status_code


class WebSearchProbeTests(unittest.TestCase):
    def test_html_response_is_rejected_with_base_url_hint(self):
        with self.assertRaisesRegex(LLMResponseError, "Base URL.*v1"):
            parse_chat_completion_response("<!doctype html><html><title>Gateway</title>")

    def test_standard_chat_response_is_extracted(self):
        response = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="OK"))],
            usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1),
        )
        content, usage = parse_chat_completion_response(response)
        self.assertEqual(content, "OK")
        self.assertIsNotNone(usage)

    def test_connection_test_errors_distinguish_timeout_and_auth(self):
        self.assertIn("超时", connection_test_error_message(TimeoutError("request timed out")))
        self.assertIn("认证失败", connection_test_error_message(HttpError(401, "unauthorized")))

    def test_connection_classifier_retries_transport_errors_only(self):
        from backend.llm_client import is_transient_connection_error

        self.assertTrue(is_transient_connection_error(ConnectionError("connection reset")))
        self.assertFalse(is_transient_connection_error(TimeoutError("timed out")))
        self.assertFalse(is_transient_connection_error(RuntimeError("429 rate limit")))

    def test_successful_search_is_available(self):
        client = FakeClient(["67234"])
        result = _probe_responses_web_search(client, "test-model")
        self.assertTrue(result.ok)
        self.assertEqual(result.status, "available")

    def test_transient_timeout_is_retried(self):
        client = FakeClient([TimeoutError("request timed out"), "67234"])
        result = _probe_responses_web_search(client, "test-model", attempts=2)
        self.assertTrue(result.ok)
        self.assertEqual(client.responses.calls, 2)

    def test_explicit_tool_rejection_is_not_retried(self):
        client = FakeClient([HttpError(400, "web_search tool is not supported")])
        result = _probe_responses_web_search(client, "test-model", attempts=2)
        self.assertFalse(result.ok)
        self.assertEqual(result.status, "unsupported")
        self.assertFalse(result.retryable)
        self.assertEqual(client.responses.calls, 1)

    def test_generic_invalid_request_is_not_reported_as_unsupported(self):
        result = _classify_web_search_probe_error(ValueError("invalid request payload"))
        self.assertEqual(result.status, "request_failed")
        self.assertTrue(result.retryable)

    def test_common_http_failures_have_distinct_statuses(self):
        cases = (
            (HttpError(401, "unauthorized"), "authentication_failed"),
            (HttpError(429, "rate limit"), "rate_limited"),
            (HttpError(502, "bad gateway"), "upstream_error"),
            (HttpError(404, "not found"), "responses_api_unavailable"),
        )
        for error, expected in cases:
            with self.subTest(expected=expected):
                self.assertEqual(_classify_web_search_probe_error(error).status, expected)

    def test_timeout_message_does_not_claim_unsupported(self):
        message = _web_search_probe_message("GPT", "timeout", "test-model")
        self.assertIn("检测超时", message)
        self.assertNotIn("不支持", message)

    def test_failed_fallback_does_not_create_heat_group_cards(self):
        with patch("backend.api_routes.call_llm", side_effect=RuntimeError("bad gateway")):
            self.assertEqual(_fallback_needs([{"title": "A post"}]), [])

    def test_report_reddit_links_must_match_source_posts(self):
        need = {"posts": [{"title": "Manage feature requests", "url": "https://reddit.com/r/SaaS/comments/abc/manage/"}]}
        report = (
            "[Manage feature requests](https://reddit.com/r/SaaS/comments/wrong/slug/) "
            "[External](https://example.com/source)"
        )
        repaired = _report_repair_reddit_links(report, need)
        self.assertIn("https://reddit.com/r/SaaS/comments/abc/manage/", repaired)
        self.assertIn("[External](https://example.com/source)", repaired)
        self.assertNotIn("comments/wrong/", repaired)

    def test_report_bare_reddit_urls_must_match_source_posts(self):
        known = "https://reddit.com/r/SaaS/comments/abc/manage/"
        need = {"posts": [{"title": "Manage feature requests", "url": known}]}
        report = (
            f"Known source: {known}\n"
            "Invented source: https://reddit.com/r/SaaS/comments/wrong/slug/.\n"
            "External source: https://example.com/source"
        )
        repaired = _report_repair_reddit_links(report, need)
        self.assertIn(known, repaired)
        self.assertNotIn("comments/wrong/", repaired)
        self.assertIn("https://example.com/source", repaired)

    def test_feedback_intent_guard_removes_generic_adjacent_posts(self):
        posts = [
            {"title": "How to manage feature requests", "content": "Collect feedback and prioritize the roadmap."},
            {"title": "Game rating rant", "content": "I played for 2000 hours and rate it 1/10."},
        ]
        kept = _qs_apply_intent_guard(posts, "How should founders collect and prioritize user feedback?")
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0]["title"], "How to manage feature requests")

    def test_process_intent_detection_is_conservative(self):
        self.assertTrue(_qs_is_process_research_query("大学申请文书的完整流程和参与角色"))
        self.assertTrue(_qs_is_process_research_query("What steps do founders follow to launch an app?"))
        self.assertFalse(_qs_is_process_research_query("独立开发者最大的用户反馈痛点是什么？"))

    def test_process_queries_keep_dimensions_and_anchor(self):
        plan = {
            "topic_anchor": "college application essay",
            "search_anchors": ["college essay"],
            "stage_queries": ["timeline"],
            "task_queries": ["brainstorm workload"],
            "role_queries": ["counselor feedback"],
            "tool_queries": ["version tracking"],
            "subreddits": ["ApplyingToCollege", "CollegeEssays"],
        }
        _qs_normalize_process_queries(plan, "process_workflow")
        queries = _qs_flatten_plan_queries(plan, "fallback")
        self.assertEqual(len(queries), 4)
        self.assertTrue(all("college essay" in query.lower() for query in queries))
        ok, issue = _qs_validate_community_plan(plan, "college essay workflow", "process_workflow")
        self.assertTrue(ok, issue)

    def test_process_queries_execute_both_pathways_early(self):
        plan = {
            "research_type": "process_workflow",
            "stage_queries": ["Common App essay timeline", "UCAS statement timeline"],
            "task_queries": ["Common App draft revise", "UCAS draft revise"],
            "role_queries": ["essay counselor role"],
            "tool_queries": ["essay deadline tracker"],
        }
        self.assertEqual(
            _qs_flatten_plan_queries(plan, "")[:4],
            ["Common App essay timeline", "Common App draft revise", "UCAS statement timeline", "UCAS draft revise"],
        )

    def test_process_subreddits_prioritize_us_and_europe(self):
        values = ["ApplyingToCollege", "CollegeEssays", "CommonApp", "6thForm", "UniUK", "UCAS"]
        ordered = _qs_prioritize_process_subreddits(values, "欧美高中生申请大学流程", "process_workflow")
        self.assertIn("ApplyingToCollege", ordered[:3])
        self.assertTrue(any(item in ordered[:3] for item in ("6thForm", "UniUK", "UCAS")))

    def test_process_evidence_removes_rants_and_single_essay_requests(self):
        plan = {"research_type": "process_workflow", "search_anchors": ["college essay"]}
        posts = [
            {"title": "Have you ever read a horrible College essay?", "content": "This applicant seems horrible."},
            {"title": "Brutally critique my college essay", "content": "Here is my single essay draft."},
            {"title": "Tips for incoming seniors", "content": "First start the Common App essay in June, then draft supplemental essays before deadlines."},
            {"title": "My UCAS process", "content": "I brainstormed, wrote a first draft, asked my teacher for feedback, revised it, and submitted through UCAS."},
        ]
        kept = _qs_filter_process_evidence(posts, "欧美高中生申请大学流程", plan)
        self.assertEqual([item["title"] for item in kept], ["Tips for incoming seniors", "My UCAS process"])

    def test_multi_scope_process_requires_both_pathways(self):
        us_posts = [
            {
                "_process_dimensions": ["stages", "tasks"],
                "_process_actions": ["brainstorm", "draft", "revise"],
                "_process_scopes": ["us"],
            }
            for _ in range(4)
        ]
        issue_zh, _ = _qs_process_evidence_issue(us_posts, "欧美高中生申请大学流程")
        self.assertIn("英国/欧洲路径", issue_zh)
        mixed = us_posts[:2] + [
            {
                "_process_dimensions": ["stages", "tasks"],
                "_process_actions": ["draft", "feedback", "submit"],
                "_process_scopes": ["europe"],
            }
            for _ in range(2)
        ]
        self.assertEqual(_qs_process_evidence_issue(mixed, "欧美高中生申请大学流程"), ("", ""))

    def test_quick_search_history_keeps_process_evidence_metadata(self):
        items = _qs_sanitize_history_items([{
            "id": "workflow",
            "query": "college essay workflow",
            "timestamp": 1,
            "summary": "## Workflow Stages",
            "posts": [{
                "title": "Timeline",
                "process_dimensions": ["stages", "tasks"],
                "process_actions": ["draft", "revise"],
                "process_scopes": ["us"],
            }],
        }])
        post = items[0]["posts"][0]
        self.assertEqual(post["process_actions"], ["draft", "revise"])
        self.assertEqual(post["process_scopes"], ["us"])

    def test_process_diversification_uses_existing_candidates_only(self):
        plan = {
            "research_type": "process_workflow",
            "search_anchors": ["college essay"],
            "stage_queries": ["college essay timeline"],
            "task_queries": ["college essay brainstorm"],
            "role_queries": ["college essay counselor"],
            "tool_queries": ["college essay tracker"],
        }
        posts = [
            {"title": "College essay timeline from junior spring", "content": "deadline calendar"},
            {"title": "College essay timeline advice", "content": "when to start"},
            {"title": "College essay counselor review", "content": "teacher feedback"},
            {"title": "How I brainstorm a college essay", "content": "drafting task"},
        ]
        selected = _qs_diversify_process_posts(posts, plan, 3)
        self.assertEqual(len(selected), 3)
        self.assertEqual({id(item) for item in selected}.issubset({id(item) for item in posts}), True)
        dimensions = {dimension for item in selected for dimension in item.get("_process_dimensions", [])}
        self.assertGreaterEqual(len(dimensions), 3)

    def test_process_summary_rejects_invalid_or_reused_evidence(self):
        valid = """## 结论\n证据支持局部流程。\n\n## 流程阶段\n### 准备\n- 工作：整理素材。\n- 参与者：学生。\n- 证据：帖子 1\n### 修改\n- 工作：获取反馈。\n- 参与者：顾问。\n- 证据：帖子 2\n\n## 证据边界\n时间信息不足。"""
        self.assertEqual(_qs_sanitize_process_summary(valid, 2), valid)
        reused = valid.replace("帖子 2", "帖子 1")
        self.assertEqual(_qs_sanitize_process_summary(reused, 2), "")
        out_of_range = valid.replace("帖子 2", "帖子 3")
        self.assertEqual(_qs_sanitize_process_summary(out_of_range, 2), "")

    def test_process_summary_requires_stage_or_task_evidence(self):
        summary = """## Conclusion\nPartial workflow.\n\n## Workflow Stages\n### Prepare\n- Work: Gather material.\n- Participants: Student.\n- Evidence: Post 1\n### Review\n- Work: Review a draft.\n- Participants: Counselor.\n- Evidence: Post 2\n\n## Evidence Limits\nTiming is incomplete."""
        posts = [
            {"_process_dimensions": ["stages"]},
            {"_process_dimensions": ["roles"]},
        ]
        self.assertEqual(_qs_sanitize_process_summary(summary, posts, "en-US"), "")
        posts[1]["_process_dimensions"] = ["tasks", "roles"]
        self.assertEqual(_qs_sanitize_process_summary(summary, posts, "en-US"), summary)


if __name__ == "__main__":
    unittest.main()
