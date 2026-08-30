"""生成不含真实用户内容、账号或第三方原文的 Lumon 合成演示数据。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent.parent
DEMO_DIR = ROOT / "data" / "demo"


def _post(index: int, *, title: str, title_zh: str, content: str, comments: list[str]) -> dict[str, Any]:
    """构造一条字段完整、来源明确的合成社区帖子。"""
    url = f"https://example.com/lumon-demo/source-{index}"
    return {
        "source": "synthetic/lumon-demo",
        "title": title,
        "title_zh": title_zh,
        "content": content,
        "comments": comments,
        "url": url,
        "hn_url": "",
        "score": 80 + index * 17,
        "num_comments": len(comments),
        "has_need_signals": True,
        "_engine": "synthetic-demo",
        "_full_body": content,
        "_discovery_source": "synthetic-demo",
    }


def _needs() -> list[dict[str, Any]]:
    """返回三个覆盖主要界面状态的合成需求主题。"""
    inbox_posts = [
        _post(
            1,
            title="My saved links keep turning into an unread second inbox",
            title_zh="收藏链接不断堆积成第二个未读收件箱",
            content=(
                "[Synthetic example] I save articles, videos, and product pages while working. "
                "A week later I cannot remember why I saved them, so the list keeps growing without helping me."
            ),
            comments=[
                "[Synthetic example] I need the original reason next to each saved item.",
                "[Synthetic example] A short weekly review would be more useful than another folder system.",
            ],
        ),
        _post(
            2,
            title="Bookmarks are easy to collect and hard to turn into decisions",
            title_zh="书签易于收集，却很难转化为行动",
            content=(
                "[Synthetic example] My research bookmarks mix buying decisions, learning material, and references. "
                "Search finds words, but it does not tell me which decision each page was meant to support."
            ),
            comments=[
                "[Synthetic example] I want reminders based on intent, not the date I saved something.",
                "[Synthetic example] Automatic summaries help only when I can verify the source quickly.",
            ],
        ),
    ]
    handoff_posts = [
        _post(
            3,
            title="Small project handoffs lose the reasoning behind decisions",
            title_zh="小项目交接时经常丢失决策背景",
            content=(
                "[Synthetic example] Our team records tasks, but the alternatives and tradeoffs stay in chat. "
                "When ownership changes, the next person repeats old discussions."
            ),
            comments=[
                "[Synthetic example] A decision log should take seconds, otherwise nobody maintains it.",
                "[Synthetic example] Linking each decision to evidence would prevent repeated debates.",
            ],
        ),
        _post(
            4,
            title="I can see what changed, but not why it changed",
            title_zh="我能看到改了什么，却看不到为什么改",
            content=(
                "[Synthetic example] Tickets describe the final implementation, while rejected options disappear. "
                "Months later the same constraints are rediscovered through trial and error."
            ),
            comments=[
                "[Synthetic example] Pull requests are too detailed for product context.",
                "[Synthetic example] The useful unit is one decision, its owner, and supporting evidence.",
            ],
        ),
    ]
    routine_posts = [
        _post(
            5,
            title="Shared household routines fail when exceptions pile up",
            title_zh="家庭共享日程在例外不断出现时容易失效",
            content=(
                "[Synthetic example] A repeating schedule works until travel, school events, or shift changes occur. "
                "Then everyone keeps a different mental version of the plan."
            ),
            comments=[
                "[Synthetic example] We need a clear confirmation when one week differs from the default.",
                "[Synthetic example] Calendar events do not explain who noticed or approved a change.",
            ],
        ),
        _post(
            6,
            title="Recurring chores need ownership without constant reminders",
            title_zh="重复家务需要明确责任，而不是反复催促",
            content=(
                "[Synthetic example] Checklists show unfinished work but do not handle swaps or temporary exceptions well. "
                "The coordination cost becomes larger than the task itself."
            ),
            comments=[
                "[Synthetic example] A swap should be visible and expire automatically after that week.",
                "[Synthetic example] Quiet status updates would reduce repeated messages.",
            ],
        ),
    ]
    return [
        {
            "need_title": "收藏内容失去保存意图后变成信息负担",
            "need_title_en": "Saved content becomes a burden after its original intent is lost",
            "need_description": "用户能轻松收藏内容，却无法保留保存原因、关联决策和复查时机，最终形成无人处理的第二收件箱。所有帖子和评论均为合成演示文本。",
            "need_description_en": "People can save content easily, but lose the reason, decision context, and review timing. All posts and comments are synthetic demo text.",
            "original_topic": "[合成演示数据] 个人知识与决策整理",
            "posts": inbox_posts,
            "total_score": sum(post["score"] for post in inbox_posts),
            "total_comments": sum(post["num_comments"] for post in inbox_posts),
        },
        {
            "need_title": "轻量项目交接缺少可追溯的决策背景",
            "need_title_en": "Lightweight project handoffs lose decision context",
            "need_description": "任务系统保留结果，却常遗漏备选方案、约束与证据，导致换人后重复讨论和试错。所有帖子和评论均为合成演示文本。",
            "need_description_en": "Task systems retain outcomes but often lose alternatives, constraints, and evidence. All posts and comments are synthetic demo text.",
            "original_topic": "[合成演示数据] 小团队协作",
            "posts": handoff_posts,
            "total_score": sum(post["score"] for post in handoff_posts),
            "total_comments": sum(post["num_comments"] for post in handoff_posts),
        },
        {
            "need_title": "家庭重复安排难以处理临时例外和责任交换",
            "need_title_en": "Household routines struggle with exceptions and ownership swaps",
            "need_description": "固定日程在旅行、轮班和临时事件出现后迅速失真，家庭成员需要低打扰地确认例外与责任变化。所有帖子和评论均为合成演示文本。",
            "need_description_en": "Recurring plans drift when travel, shifts, and exceptions occur. All posts and comments are synthetic demo text.",
            "original_topic": "[合成演示数据] 家庭协作",
            "posts": routine_posts,
            "total_score": sum(post["score"] for post in routine_posts),
            "total_comments": sum(post["num_comments"] for post in routine_posts),
        },
    ]


def _debate() -> list[dict[str, Any]]:
    """返回用于 SSE 回放的合成多角色讨论。"""
    return [
        {"event": "round_start", "data": {"round": 1}},
        {
            "event": "message",
            "role": "director",
            "label": "导演",
            "provider": "gpt",
            "content": "以下讨论仅使用合成演示数据。先验证用户是否真的需要保留保存意图，再讨论产品边界。",
        },
        {
            "event": "topic_start",
            "data": {"index": 0, "title": "保存意图是否值得单独管理", "total": 2},
        },
        {
            "event": "message",
            "role": "director",
            "label": "导演",
            "provider": "gpt",
            "content": "话题 1：用户缺的是更好的收藏夹，还是在做决定时恢复上下文的能力？",
        },
        {
            "event": "message",
            "role": "analyst",
            "label": "产品经理",
            "provider": "gpt",
            "content": (
                "<think>{\"data_notice\":\"synthetic\",\"confidence\":6}</think>\n\n"
                "两条合成样本都指向同一断点：收藏动作很轻，但保存原因没有进入记录。"
                "机会不在增加文件夹，而在收藏时用一句话记录意图，并在相关决策出现时主动召回。"
            ),
        },
        {
            "event": "message",
            "role": "critic",
            "label": "杠精",
            "provider": "gpt",
            "content": "样本只能证明上下文会丢失，还不能证明用户愿意多做一步。若记录意图超过几秒，产品会制造新的待办负担。",
        },
        {
            "event": "message",
            "role": "analyst",
            "label": "产品经理",
            "provider": "gpt",
            "content": "同意。因此首版应从浏览器分享入口自动带入标题和来源，只让用户选择意图类型，并允许一句可选备注。",
        },
        {
            "event": "message",
            "role": "director",
            "label": "导演",
            "provider": "gpt",
            "content": "阶段结论：先验证两秒内记录意图能否提升一周后的找回率，不做完整知识库。",
        },
        {"event": "round_start", "data": {"round": 2}},
        {
            "event": "topic_start",
            "data": {"index": 1, "title": "最小产品与验证指标", "total": 2},
        },
        {
            "event": "message",
            "role": "director",
            "label": "导演",
            "provider": "gpt",
            "content": "话题 2：怎样用最小产品区分真实需求和演示效果？",
        },
        {
            "event": "message",
            "role": "analyst",
            "label": "产品经理",
            "provider": "gpt",
            "content": "做一个本地优先的浏览器扩展：保存时选择研究、购买或稍后行动；七天后按意图生成复查队列。",
        },
        {
            "event": "message",
            "role": "critic",
            "label": "杠精",
            "provider": "gpt",
            "content": "核心指标不能是收藏数量，应看七天内被重新打开、完成决策或主动删除的比例。",
        },
        {
            "event": "message",
            "role": "investor",
            "label": "投资人",
            "provider": "gpt",
            "content": "在合成证据下只能给出待验证结论。优先验证高频研究者，避免过早扩展到团队知识管理。",
        },
        {
            "event": "message",
            "role": "director",
            "label": "导演",
            "provider": "gpt",
            "content": "最终判断：值得做一周原型实验，但证据强度有限。成功标准是用户能更快完成原本要做的决定，而不是保存更多内容。",
        },
    ]


def _report(primary_need: dict[str, Any]) -> dict[str, Any]:
    """构造包含常用 Markdown 元素的合成研究报告。"""
    report = """# 收藏内容失去保存意图后变成信息负担

> 数据声明：本报告中的帖子、评论、分数、竞品和结论均为合成演示内容，不代表真实用户或市场事实。

## 一句话结论

用户的问题不是缺少收藏入口，而是保存原因和后续决策脱节。最小产品应验证“记录意图 + 定时召回”是否能提高内容被重新使用的比例。

## 核心发现

1. 收藏时的上下文没有进入现有书签记录。
2. 文件夹增加了整理成本，却没有回答“为什么保存”。
3. 自动摘要必须保留可核对的来源，才能支持后续决策。

## 证据边界

- 合成帖子数：2
- 合成评论数：4
- 外部事实验证：未执行
- 当前置信度：低，仅适合演示产品流程

## 痛点地图

### 1. 一周后无法恢复保存原因

**强度：中（合成信号）**

代表性合成引述：“I need the original reason next to each saved item.”

### 2. 收藏列表不能帮助完成决策

**强度：中（合成信号）**

代表性合成引述：“Bookmarks are easy to collect and hard to turn into decisions.”

## 最小产品建议

浏览器分享入口自动记录标题、URL 和时间，用户只需选择“研究 / 购买 / 稍后行动”之一；系统在七天后生成一次本地复查队列。

## 验证指标

| 指标 | 目标 | 失败信号 |
|---|---:|---|
| 保存操作中位耗时 | 小于 3 秒 | 用户频繁跳过意图选择 |
| 7 天内重新使用率 | 高于 30% | 内容继续堆积且无人处理 |
| 完成或删除比例 | 高于 20% | 用户只收藏、不做决定 |

## 合成竞品格局

| 竞品 | App Store | 近30天收入 | 近30天下载 | SensorTower |
|---|---|---:|---:|---|
| Demo Bookmark Tool | [示例来源](https://example.com/lumon-demo/competitor) | - | - | - |

## 下一步

招募 5 名需要持续做购买或研究决策的测试者，进行一周本地原型实验。发布任何市场判断前，必须重新收集有授权的真实证据。
"""
    return {
        "created_at": "2026-01-01T00:00:00+00:00",
        "debate_rounds": 2,
        "final_report": report,
        "need": primary_need,
        "report_format": "markdown",
    }


def _write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> None:
    """重建三份演示文件，输出内容保持确定性。"""
    DEMO_DIR.mkdir(parents=True, exist_ok=True)
    needs = _needs()
    _write_json(DEMO_DIR / "demo_needs.json", needs)
    _write_json(DEMO_DIR / "demo_debate.json", _debate())
    _write_json(DEMO_DIR / "demo_report.json", _report(needs[0]))


if __name__ == "__main__":
    main()
