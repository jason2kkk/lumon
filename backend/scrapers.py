"""
scrapers.py — 海外社区数据采集器

职责：从 HackerNews / Reddit 抓取帖子和评论，输出统一格式的 dict 列表
支持三种挖掘模式：一句话搜索、关键词搜索、开放式浏览
"""

import asyncio
import os
import httpx
from dotenv import load_dotenv

load_dotenv()

HN_API_BASE = "https://hacker-news.firebaseio.com/v0"
HN_ALGOLIA_BASE = "https://hn.algolia.com/api/v1"

TRACK_CATEGORIES = {
    "email": {
        "label": "办公-邮箱",
        "label_en": "Email",
        "st_queries": ["email", "mail app", "gmail", "outlook", "inbox"],
        "subreddits": ["email", "Gmail", "Outlook", "ProtonMail"],
        "hn_tags": ["email", "gmail", "inbox", "SMTP"],
    },
    "chatbot": {
        "label": "Chatbot",
        "label_en": "AI Chatbot",
        "st_queries": ["chatbot AI", "AI assistant", "ChatGPT", "Claude AI", "AI chat"],
        "subreddits": ["ChatGPT", "ClaudeAI", "LocalLLaMA", "ArtificialIntelligence"],
        "hn_tags": ["ChatGPT", "LLM", "AI assistant", "chatbot"],
    },
    "calorie_nutrition": {
        "label": "健康-卡路里/营养",
        "label_en": "Calorie & Nutrition",
        "st_queries": ["calorie tracker", "nutrition tracker", "diet tracker", "meal tracker", "food diary"],
        "subreddits": ["CICO", "LoseIt", "Nutrition", "MealPrepSunday"],
        "hn_tags": ["calorie", "nutrition", "diet tracker", "food tracking"],
    },
    "meditation_sleep": {
        "label": "健康-冥想/睡眠",
        "label_en": "Meditation & Sleep",
        "st_queries": ["meditation", "sleep tracker", "white noise", "sleep sounds", "mindfulness"],
        "subreddits": ["Meditation", "Sleep", "Mindfulness", "Insomnia"],
        "hn_tags": ["meditation", "sleep", "mindfulness", "white noise"],
    },
    "womens_health": {
        "label": "健康-女性生理",
        "label_en": "Women's Health",
        "st_queries": ["period tracker", "fertility tracker", "women health", "ovulation", "menstrual cycle"],
        "subreddits": ["TwoXChromosomes", "TryingForABaby", "Periods", "WomensHealth"],
        "hn_tags": ["women health", "period tracker", "fertility", "femtech"],
    },
    "dating_coach": {
        "label": "恋爱约会-情感教练",
        "label_en": "Dating Coach",
        "st_queries": ["relationship coach", "dating advice", "love coach", "breakup recovery", "dating tips"],
        "subreddits": ["RelationshipAdvice", "Dating", "DatingApps", "BreakUps"],
        "hn_tags": ["dating", "relationship", "dating app"],
    },
    "dating_couple": {
        "label": "恋爱约会-情侣互动",
        "label_en": "Couple App",
        "st_queries": ["couple app", "relationship app", "love app", "couples game", "date night"],
        "subreddits": ["LongDistance", "Relationships", "Marriage"],
        "hn_tags": ["couple app", "relationship", "long distance"],
    },
    "language_learning": {
        "label": "语言学习",
        "label_en": "Language Learning",
        "st_queries": ["language learning", "vocabulary", "duolingo", "learn english", "flashcard language"],
        "subreddits": ["LanguageLearning", "LearnJapanese", "LearnSpanish", "Duolingo"],
        "hn_tags": ["language learning", "duolingo", "vocabulary", "spaced repetition"],
    },
    "tutoring": {
        "label": "学习辅导",
        "label_en": "Tutoring",
        "st_queries": ["tutoring", "homework help", "study app", "math solver", "AI tutor"],
        "subreddits": ["HomeworkHelp", "GetStudying", "LearnMath", "StudyTips"],
        "hn_tags": ["tutoring", "homework", "edtech", "study"],
    },
    "knowledge": {
        "label": "知识学习",
        "label_en": "Knowledge Learning",
        "st_queries": ["Quizlet", "Brilliant", "Khan Academy", "Curiosity", "Brainly", "quiz trivia"],
        "subreddits": ["IWantToLearn", "TodayILearned", "ExplainLikeImFive"],
        "hn_tags": ["knowledge", "learning", "quiz", "education"],
    },
    "cooking": {
        "label": "饮食烹饪",
        "label_en": "Cooking & Recipes",
        "st_queries": ["cooking", "recipe app", "meal planner", "food recipe", "kitchen"],
        "subreddits": ["Cooking", "Recipes", "MealPrepSunday", "EatCheapAndHealthy"],
        "hn_tags": ["cooking", "recipe", "meal planning", "food tech"],
    },
    "calendar": {
        "label": "效率-日程日历",
        "label_en": "Calendar & Schedule",
        "st_queries": ["calendar app", "schedule planner", "daily planner", "to-do list", "task manager"],
        "subreddits": ["Productivity", "GetDisciplined", "BulletJournal", "GTD"],
        "hn_tags": ["calendar", "todo", "task manager", "productivity"],
    },
    "image_edit": {
        "label": "图像编辑/生成",
        "label_en": "Image Edit & Generate",
        "st_queries": ["photo editor", "AI image generator", "image editing", "AI art", "photo filter"],
        "subreddits": ["PhotoEditing", "StableDiffusion", "MidJourney", "GraphicDesign"],
        "hn_tags": ["image generation", "AI art", "photo editing", "stable diffusion"],
    },
    "translation": {
        "label": "翻译",
        "label_en": "Translation",
        "st_queries": ["translate", "translator app", "translation", "language translator", "dictionary"],
        "subreddits": ["TranslationStudies", "LanguageLearning", "Translator"],
        "hn_tags": ["translation", "translator", "machine translation", "NLP"],
    },
    "drawing": {
        "label": "绘画",
        "label_en": "Drawing & Painting",
        "st_queries": ["drawing app", "painting app", "sketch", "coloring book", "digital art"],
        "subreddits": ["DigitalArt", "Drawing", "ProCreate", "LearnToDraw"],
        "hn_tags": ["drawing", "digital art", "procreate", "sketch"],
    },
    "recording": {
        "label": "录音录像",
        "label_en": "Recording",
        "st_queries": ["voice recorder", "screen recorder", "audio recorder", "video recorder", "dictaphone"],
        "subreddits": ["ScreenRecording", "VoiceActing", "Podcasting"],
        "hn_tags": ["screen recording", "voice recorder", "transcription", "audio"],
    },
    "job_search": {
        "label": "求职",
        "label_en": "Job Search",
        "st_queries": ["job search", "resume builder", "career app", "LinkedIn", "job finder"],
        "subreddits": ["Jobs", "Resumes", "CareerAdvice", "CSCareerQuestions"],
        "hn_tags": ["job search", "hiring", "resume", "career"],
    },
    "scanner": {
        "label": "通用扫描",
        "label_en": "Scanner",
        "st_queries": ["scanner app", "document scan", "OCR", "PDF scanner", "cam scanner"],
        "subreddits": ["DataHoarder", "Productivity", "Paperless"],
        "hn_tags": ["OCR", "document scanner", "PDF", "digitize"],
    },
    "recognition": {
        "label": "识别",
        "label_en": "Recognition",
        "st_queries": ["identify app", "plant identifier", "bird identifier", "object recognition", "music recognition"],
        "subreddits": ["WhatsThisPlant", "WhatsThisBird", "WhatsThisBug"],
        "hn_tags": ["image recognition", "plant identifier", "AI recognition", "Shazam"],
    },
    "measurement": {
        "label": "其他工具-空间测量",
        "label_en": "Measurement",
        "st_queries": ["measure app", "ruler app", "distance measure", "AR measure", "level tool"],
        "subreddits": ["HomeImprovement", "DIY", "Tools"],
        "hn_tags": ["AR measure", "measurement", "LiDAR", "spatial"],
    },
    "device_locator": {
        "label": "设备/联系人定位",
        "label_en": "Device Locator",
        "st_queries": ["find my device", "location tracker", "GPS tracker", "family locator", "phone tracker"],
        "subreddits": ["Privacy", "Parenting", "GPS"],
        "hn_tags": ["location tracking", "GPS", "find my", "AirTag"],
    },
    "medical": {
        "label": "医疗知识/咨询",
        "label_en": "Medical",
        "st_queries": ["medical app", "health consultation", "symptom checker", "doctor app", "telehealth"],
        "subreddits": ["HealthIT", "Medicine", "AskDocs"],
        "hn_tags": ["telehealth", "medical", "health tech", "symptom checker"],
    },
    "stock_invest": {
        "label": "财务-投资股票",
        "label_en": "Stock & Investment",
        "st_queries": ["stock trading", "investing app", "stock market", "trading platform", "portfolio tracker"],
        "subreddits": ["Investing", "Stocks", "WallStreetBets", "FinancialIndependence"],
        "hn_tags": ["stock trading", "investing", "fintech", "portfolio"],
    },
    "budgeting": {
        "label": "财务-个人记账",
        "label_en": "Budgeting",
        "st_queries": ["expense tracker", "budget app", "bookkeeping", "money manager", "personal finance"],
        "subreddits": ["PersonalFinance", "Budgeting", "YNAB", "Frugal"],
        "hn_tags": ["budgeting", "expense tracker", "personal finance", "YNAB"],
    },
    "religion": {
        "label": "宗教与灵性",
        "label_en": "Religion & Spirituality",
        "st_queries": ["bible app", "prayer app", "quran", "religious", "devotional"],
        "subreddits": ["Christianity", "Islam", "Buddhism", "Spirituality"],
        "hn_tags": ["religion", "bible", "prayer", "spiritual"],
    },
    "hobby_skill": {
        "label": "兴趣/技能",
        "label_en": "Hobby & Skill",
        "st_queries": ["hobby app", "DIY craft", "skill learning", "guitar tuner", "knitting"],
        "subreddits": ["Hobbies", "DIY", "LearnANewSkill", "Guitar", "Knitting"],
        "hn_tags": ["hobby", "DIY", "skill learning", "craft"],
    },
}

REDDIT_CATEGORIES = TRACK_CATEGORIES

NEED_SIGNAL_WORDS = [
    # Pain / frustration
    "need", "wish", "hate", "frustrated", "annoying", "painful",
    "struggle", "problem", "broken", "impossible", "terrible",
    "sick of", "tired of", "fed up", "gave up", "given up",
    "I literally can't", "destroying my", "driving me crazy",
    # Solution seeking
    "looking for", "help me", "how do i", "is there a", "why can't",
    "alternative", "switched from", "moved to", "replaced",
    "recommend", "suggestion", "best app",
    # Workaround / DIY
    "workaround", "hack", "I built", "I made", "my workflow",
    "I ended up", "temporary solution", "jury-rigged",
    # Willingness to pay
    "I would pay", "worth paying", "shut up and take my money",
    "I'd pay for", "pricing", "too expensive", "free alternative",
]


def has_need_signals(text: str) -> bool:
    lower = text.lower()
    return any(word in lower for word in NEED_SIGNAL_WORDS)


def hard_filter(post: dict) -> bool:
    """Pre-LLM quality gate: reject posts that are obviously low-value.
    Returns True if the post passes the minimum quality bar."""
    source = post.get("source", "")
    score = post.get("score", 0)
    num_comments = post.get("num_comments", 0)
    body = post.get("content", "") or ""

    if source.startswith("reddit"):
        return score >= 1 and len(body) > 30
    else:
        return score >= 1 and num_comments >= 1


# ============================================================
# HackerNews
# ============================================================

async def _fetch_item(client: httpx.AsyncClient, item_id: int) -> dict | None:
    try:
        resp = await client.get(f"{HN_API_BASE}/item/{item_id}.json")
        resp.raise_for_status()
        return resp.json()
    except Exception:
        return None


async def _fetch_comments(
    client: httpx.AsyncClient,
    comment_ids: list[int],
    max_comments: int = 10,
) -> list[str]:
    if not comment_ids:
        return []
    ids_to_fetch = comment_ids[:max_comments * 2]
    tasks = [_fetch_item(client, cid) for cid in ids_to_fetch]
    results = await asyncio.gather(*tasks)

    comments = []
    for item in results:
        if item and item.get("type") == "comment" and not item.get("deleted"):
            text = item.get("text", "")
            if text:
                comments.append(text[:500])
                if len(comments) >= max_comments:
                    break
    return comments


async def _fetch_hn_posts(
    category: str = "top",
    limit: int = 30,
    min_score: int = 5,
    min_comments: int = 2,
) -> list[dict]:
    endpoint_map = {
        "top": "topstories",
        "new": "newstories",
        "ask": "askstories",
        "show": "showstories",
    }
    endpoint = endpoint_map.get(category, "topstories")

    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.get(f"{HN_API_BASE}/{endpoint}.json")
        resp.raise_for_status()
        story_ids = resp.json()[:limit * 3]

        tasks = [_fetch_item(client, sid) for sid in story_ids]
        stories = await asyncio.gather(*tasks)

        posts = []
        for story in stories:
            if not story or story.get("type") != "story":
                continue
            if story.get("score", 0) < min_score:
                continue
            if len(story.get("kids", [])) < min_comments:
                continue

            comment_texts = await _fetch_comments(client, story.get("kids", []))

            title = story.get("title", "")
            content = story.get("text", "") or ""
            url = story.get("url", "")
            hn_url = f"https://news.ycombinator.com/item?id={story['id']}"

            post = {
                "source": "hackernews",
                "title": title,
                "content": content,
                "comments": comment_texts,
                "url": url or hn_url,
                "hn_url": hn_url,
                "score": story.get("score", 0),
                "num_comments": len(story.get("kids", [])),
                "has_need_signals": has_need_signals(
                    title + " " + content + " " + " ".join(comment_texts)
                ),
            }
            posts.append(post)
            if len(posts) >= limit:
                break

        posts.sort(key=lambda p: p["score"], reverse=True)
        return posts


def _extract_hn_comments(
    children: list[dict],
    out: list[str],
    max_depth: int = 3,
    max_total: int = 20,
    depth: int = 0,
):
    """递归提取 HN 评论，支持多层嵌套（对标 Skill 的 2-3 层深度要求）。"""
    if depth >= max_depth or len(out) >= max_total:
        return
    for child in children:
        if len(out) >= max_total:
            return
        text = child.get("text", "")
        if text and len(text) > 20:
            out.append(text[:600])
        sub_children = child.get("children", [])
        if sub_children:
            _extract_hn_comments(sub_children, out, max_depth, max_total, depth + 1)


async def _search_hn_posts(query: str, limit: int = 30, time_period: str = "6months") -> list[dict]:
    """Search HackerNews using the Algolia API."""
    import time as _time
    _period_seconds = {"month": 30 * 86400, "3months": 90 * 86400, "6months": 183 * 86400, "9months": 270 * 86400}
    min_ts = int(_time.time()) - _period_seconds.get(time_period, 183 * 86400)

    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.get(
            f"{HN_ALGOLIA_BASE}/search",
            params={
                "query": query,
                "tags": "story",
                "hitsPerPage": min(limit * 2, 100),
                "numericFilters": f"created_at_i>{min_ts}",
            },
        )
        resp.raise_for_status()
        hits = resp.json().get("hits", [])

        posts = []
        for hit in hits:
            story_id = hit.get("objectID", "")
            title = hit.get("title", "")
            content = hit.get("story_text", "") or ""
            url = hit.get("url", "")
            score = hit.get("points", 0) or 0
            num_comments = hit.get("num_comments", 0) or 0

            if score < 3 or num_comments < 1:
                continue

            hn_url = f"https://news.ycombinator.com/item?id={story_id}"

            comment_texts: list[str] = []
            try:
                detail_resp = await client.get(
                    f"{HN_ALGOLIA_BASE}/items/{story_id}",
                )
                if detail_resp.status_code == 200:
                    children = detail_resp.json().get("children", [])
                    _extract_hn_comments(children, comment_texts, max_depth=3, max_total=20)
            except Exception:
                pass

            post = {
                "source": "hackernews",
                "title": title,
                "content": content,
                "comments": comment_texts,
                "url": url or hn_url,
                "hn_url": hn_url,
                "score": score,
                "num_comments": num_comments,
                "created_utc": hit.get("created_at_i", 0) or 0,
                "has_need_signals": has_need_signals(
                    title + " " + content + " " + " ".join(comment_texts)
                ),
            }
            posts.append(post)
            if len(posts) >= limit:
                break

        posts.sort(key=lambda p: p["score"], reverse=True)
        return posts


def fetch_hackernews(
    category: str = "top",
    limit: int = 30,
    min_score: int = 5,
    min_comments: int = 2,
) -> list[dict]:
    return asyncio.run(
        _fetch_hn_posts(category, limit, min_score, min_comments)
    )


def search_hackernews(query: str, limit: int = 30, time_period: str = "6months") -> list[dict]:
    return asyncio.run(_search_hn_posts(query, limit, time_period))


# ============================================================
# Unified fetch
# ============================================================

def fetch_by_search(
    query: str,
    sources: list[str],
    limit: int = 30,
) -> list[dict]:
    """Search mode: fetch by query string across selected sources."""
    all_posts: list[dict] = []
    per_source = max(limit // len(sources), 10) if sources else limit

    if "hackernews" in sources:
        try:
            hn_posts = search_hackernews(query, per_source)
            all_posts.extend(hn_posts)
        except Exception as e:
            print(f"[Fetch] HN search error: {e}")

    all_posts.sort(key=lambda p: p["score"], reverse=True)
    return all_posts[:limit]


def fetch_by_keywords(
    keywords: list[str],
    sources: list[str],
    limit: int = 30,
) -> list[dict]:
    """Keywords mode: fetch by multiple keywords."""
    all_posts: list[dict] = []
    per_kw = max(limit // len(keywords), 5) if keywords else limit

    for kw in keywords:
        kw = kw.strip()
        if not kw:
            continue

        if "hackernews" in sources:
            try:
                all_posts.extend(search_hackernews(kw, per_kw))
            except Exception as e:
                print(f"[Fetch] HN '{kw}' error: {e}")

    seen_titles: set[str] = set()
    deduped: list[dict] = []
    for p in all_posts:
        key = p["title"].lower().strip()
        if key not in seen_titles:
            seen_titles.add(key)
            deduped.append(p)

    deduped.sort(key=lambda p: p["score"], reverse=True)
    return deduped[:limit]


def fetch_open(
    sources: list[str],
    category: str = "top",
    limit: int = 30,
) -> list[dict]:
    """Open mode: browse popular posts."""
    all_posts: list[dict] = []
    per_source = max(limit // len(sources), 10) if sources else limit

    if "hackernews" in sources:
        try:
            all_posts.extend(fetch_hackernews(category, per_source))
        except Exception as e:
            print(f"[Fetch] HN open error: {e}")

    all_posts.sort(key=lambda p: p["score"], reverse=True)
    return all_posts[:limit]
