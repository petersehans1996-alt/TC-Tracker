#!/usr/bin/env python3
"""
TechCrunch Sentiment & Theme Tracker
- Daily briefing: posted every morning
- Weekly briefing: posted every Monday morning (recaps last 7 days)

Usage:
  python tracker.py           # auto-detects day (weekly on Monday, daily otherwise)
  python tracker.py --weekly  # force weekly mode
  python tracker.py --daily   # force daily mode
"""

import os
import re
import sys
import json
import hashlib
import datetime
import feedparser
import urllib.request
import urllib.error
from anthropic import Anthropic

# ── Config ────────────────────────────────────────────────────────────────────
ANTHROPIC_API_KEY  = os.environ["ANTHROPIC_API_KEY"]
NOTION_TOKEN       = os.environ["NOTION_TOKEN"]
NOTION_PARENT_PAGE = os.environ["NOTION_PARENT_PAGE_ID"]

RSS_FEEDS = [
    ("TechCrunch Main",     "https://techcrunch.com/feed/"),
    ("TechCrunch AI",       "https://techcrunch.com/category/artificial-intelligence/feed/"),
    ("TechCrunch Startups", "https://techcrunch.com/category/startups/feed/"),
    ("TechCrunch Venture",  "https://techcrunch.com/category/venture/feed/"),
    ("TechCrunch Climate",  "https://techcrunch.com/category/climate/feed/"),
]

DAILY_MAX_ARTICLES  = 40
WEEKLY_MAX_ARTICLES = 120  # broader net over 7 days
DAILY_LOOKBACK_H    = 26
WEEKLY_LOOKBACK_H   = 7 * 24 + 4  # 7 days + buffer

client = Anthropic(api_key=ANTHROPIC_API_KEY)

NOTION_HEADERS = {
    "Authorization": f"Bearer {NOTION_TOKEN}",
    "Notion-Version": "2022-06-28",
    "Content-Type": "application/json",
}


# ── RSS Fetching ──────────────────────────────────────────────────────────────
def fetch_articles(lookback_hours: int, max_articles: int) -> list[dict]:
    cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=lookback_hours)
    articles, seen = [], set()

    for source, url in RSS_FEEDS:
        try:
            feed = feedparser.parse(url)
            for entry in feed.entries:
                published = None
                if hasattr(entry, "published_parsed") and entry.published_parsed:
                    published = datetime.datetime(
                        *entry.published_parsed[:6], tzinfo=datetime.timezone.utc
                    )
                if published and published < cutoff:
                    continue

                link = entry.get("link", "")
                uid  = hashlib.md5(link.encode()).hexdigest()
                if uid in seen:
                    continue
                seen.add(uid)

                summary = entry.get("summary", "") or entry.get("description", "")
                text    = re.sub(r"<[^>]+>", " ", summary).strip()[:800]

                category = source.replace("TechCrunch ", "") if "TechCrunch " in source else "General"
                if hasattr(entry, "tags") and entry.tags:
                    category = entry.tags[0].get("term", category)

                articles.append({
                    "title":     entry.get("title", "").strip(),
                    "url":       link,
                    "snippet":   text,
                    "category":  category,
                    "published": published.isoformat() if published else "",
                })
        except Exception as e:
            print(f"[WARN] Failed to fetch {source}: {e}")

    unique = list({a["url"]: a for a in articles}.values())
    # Sort newest first so most recent articles are prioritised if we cap
    unique.sort(key=lambda a: a["published"], reverse=True)
    print(f"[INFO] Fetched {len(unique)} unique articles (capping at {max_articles})")
    return unique[:max_articles]


# ── Prompts ───────────────────────────────────────────────────────────────────
DAILY_PROMPT = """You are a sharp tech analyst with strong opinions. You will receive today's TechCrunch articles.

Write a daily briefing. Be direct, opinionated, and willing to call out hype or contrarian signals. Sound like a smart colleague, not a press release.

Structure:

**TL;DR**
Two sentences maximum. What is the single most important thing happening in tech today, and what's the mood? This should read like the first line of a memo — crisp, specific, no filler.

**1. OVERALL SENTIMENT**
One paragraph on the mood of today's tech news. Reference specific stories. Don't be bland.

**2. TOP THEMES**
3–5 dominant themes. For each: 2–3 sentences on what's happening and what it actually signals. Be analytical — go beyond describing the story.

**3. NOTABLE NARRATIVES**
2–3 standout stories and a sharp take on why each matters or what it reveals. No summaries — give your read on it.

**4. WHAT TO WATCH**
One short paragraph on threads worth following in the coming days.

Use prose throughout — no bullet lists for sections 1, 3, or 4.

Articles:
"""

WEEKLY_PROMPT = """You are a sharp, opinionated tech analyst writing a weekly column called "This Week on TechCrunch." Your reader is a VC/startup professional who wants signal over noise — balanced across venture, technology, and macro/policy, but with no patience for fluff. You're willing to be contrarian, call out hype, and say what others won't.

Two topics always get a dedicated callout if they appeared this week: Climate Tech and European Startups.

Write the weekly briefing with the following structure:

**THIS WEEK ON TECHCRUNCH**
Week of {week_range}

**TL;DR**
Two sentences maximum. What defined this week in tech, and what does it signal? Write it like the opening line of an investor memo — specific, opinionated, no throat-clearing.

**THE WEEK IN ONE PARAGRAPH**
A crisp, opinionated paragraph that captures the dominant energy of the week. What was the overarching story? Was it a good week for tech, a bad one, or complicated? Reference the biggest signals.

**THE BIG THEMES**
3–5 themes that defined the week. For each, write a proper analytical paragraph (3–4 sentences): what happened, what it means, and — where warranted — where the consensus might be wrong. Don't just describe; interpret.

**DEALS & MONEY**
A paragraph on the most interesting venture activity of the week. What rounds got announced? Any notable exits, IPO moves, or dry powder signals? What does the deal flow say about investor appetite right now?

**CLIMATE TECH CORNER**
(Include this section only if climate tech appeared in the week's articles. If nothing appeared, omit this section entirely.)
A focused paragraph on what happened in climate tech this week and what it signals for the space.

**EUROPE WATCH**
(Include this section only if European startups or companies appeared in the week's articles. If nothing appeared, omit this section entirely.)
A focused paragraph on European startup/tech news this week. Is Europe catching up, falling behind, or just doing its own thing?

**THE CONTRARIAN TAKE**
Pick one consensus narrative from the week and push back on it. What is everyone saying, and why might they be wrong or missing something? Be direct.

**ONE TO WATCH NEXT WEEK**
One thread — a company, a trend, a regulatory development — worth keeping an eye on in the coming week and why.

Tone: sharp, direct, confident. Write like you have a strong point of view and aren't afraid to share it. Avoid bullet points in paragraphs; use prose throughout.

Articles from this week:
"""

def analyze_articles(articles: list[dict], mode: str, week_range: str = "") -> str:
    formatted = []
    for i, a in enumerate(articles, 1):
        pub = a["published"][:10] if a["published"] else "?"
        formatted.append(
            f"{i}. [{a['category']}] [{pub}] {a['title']}\n   {a['snippet'][:350]}"
        )

    if mode == "weekly":
        prompt = WEEKLY_PROMPT.replace("{week_range}", week_range) + "\n\n".join(formatted)
        max_tokens = 2200
    else:
        prompt = DAILY_PROMPT + "\n\n".join(formatted)
        max_tokens = 1500

    print(f"[INFO] Sending {len(articles)} articles to Claude ({mode} mode)...")
    response = client.messages.create(
        model="claude-opus-4-5",
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": prompt}],
    )
    return response.content[0].text


# ── Notion Helpers ────────────────────────────────────────────────────────────
def notion_request(method: str, path: str, body: dict = None):
    url  = f"https://api.notion.com/v1{path}"
    data = json.dumps(body).encode() if body else None
    req  = urllib.request.Request(url, data=data, headers=NOTION_HEADERS, method=method)
    try:
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        print(f"[ERROR] Notion {method} {path}: {e.code} {e.read().decode()}")
        raise


def text_block(content: str) -> dict:
    return {
        "object": "block", "type": "paragraph",
        "paragraph": {"rich_text": [{"type": "text", "text": {"content": content}}]},
    }


def heading_block(content: str, level: int = 2) -> dict:
    key = f"heading_{level}"
    return {
        "object": "block", "type": key,
        key: {"rich_text": [{"type": "text", "text": {"content": content}}]},
    }


def divider_block() -> dict:
    return {"object": "block", "type": "divider", "divider": {}}


def callout_block(content: str, emoji: str = "📡", color: str = "gray_background") -> dict:
    return {
        "object": "block", "type": "callout",
        "callout": {
            "rich_text": [{"type": "text", "text": {"content": content}}],
            "icon": {"type": "emoji", "emoji": emoji},
            "color": color,
        },
    }


def parse_analysis_to_blocks(analysis: str) -> list[dict]:
    """Convert markdown-style analysis text into Notion blocks."""
    blocks = []
    for line in analysis.split("\n"):
        line = line.strip()
        if not line:
            continue
        # Match **HEADING** or **HEADING** with trailing text
        if re.match(r"^\*\*[^*]+\*\*$", line):
            clean = line.strip("*").strip()
            # Top-level title gets h2, sub-sections get h3
            level = 2 if line.isupper() or "THIS WEEK" in line else 3
            blocks.append(heading_block(clean, level=level))
        elif line.startswith("**") and "**" in line[2:]:
            clean = re.sub(r"\*\*", "", line).strip()
            blocks.append(heading_block(clean, level=3))
        else:
            blocks.append(text_block(line))
    return blocks


def build_notion_page(
    analysis: str,
    articles: list[dict],
    title: str,
    callout_text: str,
    emoji: str,
) -> dict:
    blocks = [
        callout_block(callout_text, emoji=emoji),
        divider_block(),
        *parse_analysis_to_blocks(analysis),
        divider_block(),
        heading_block("Articles Covered", level=3),
    ]

    for a in articles:
        pub = a["published"][:10] if a["published"] else ""
        blocks.append({
            "object": "block",
            "type": "bulleted_list_item",
            "bulleted_list_item": {
                "rich_text": [{
                    "type": "text",
                    "text": {
                        "content": f"[{a['category']}] {pub} — {a['title']}",
                        "link": {"url": a["url"]},
                    },
                }]
            },
        })

    return {
        "parent": {"page_id": NOTION_PARENT_PAGE},
        "icon": {"type": "emoji", "emoji": emoji},
        "properties": {
            "title": {"title": [{"type": "text", "text": {"content": title}}]}
        },
        "children": blocks[:100],
    }


def post_to_notion(analysis: str, articles: list[dict], mode: str, week_range: str = "") -> str:
    today = datetime.date.today()

    if mode == "weekly":
        title        = f"This Week on TechCrunch — {week_range}"
        callout_text = f"📰 Weekly briefing · {len(articles)} articles · {week_range}"
        emoji        = "🗞️"
    else:
        date_str     = today.strftime("%d %b %Y")
        title        = f"TechCrunch Briefing — {date_str}"
        callout_text = f"📰 {len(articles)} articles analyzed · {today.strftime('%A, %d %B %Y')}"
        emoji        = "📰"

    page_payload = build_notion_page(analysis, articles, title, callout_text, emoji)

    print("[INFO] Creating Notion page...")
    result   = notion_request("POST", "/pages", page_payload)
    page_url = result.get("url", "")
    print(f"[INFO] Notion page created: {page_url}")
    return page_url


# ── Main ──────────────────────────────────────────────────────────────────────
def get_week_range() -> str:
    today    = datetime.date.today()
    # Last Monday to last Sunday
    last_mon = today - datetime.timedelta(days=today.weekday() + 7)
    last_sun = last_mon + datetime.timedelta(days=6)
    return f"{last_mon.strftime('%-d %b')} – {last_sun.strftime('%-d %b %Y')}"


def main():
    args = sys.argv[1:]
    if "--weekly" in args:
        mode = "weekly"
    elif "--daily" in args:
        mode = "daily"
    else:
        # Auto-detect: weekly on Monday, daily otherwise
        mode = "weekly" if datetime.date.today().weekday() == 0 else "daily"

    print(f"[INFO] Mode: {mode} — {datetime.datetime.now().isoformat()}")

    lookback = WEEKLY_LOOKBACK_H if mode == "weekly" else DAILY_LOOKBACK_H
    max_art  = WEEKLY_MAX_ARTICLES if mode == "weekly" else DAILY_MAX_ARTICLES

    articles = fetch_articles(lookback, max_art)
    if not articles:
        print("[WARN] No articles fetched. Exiting.")
        return

    week_range = get_week_range() if mode == "weekly" else ""
    analysis   = analyze_articles(articles, mode, week_range)
    page_url   = post_to_notion(analysis, articles, mode, week_range)

    print(f"[DONE] Briefing posted → {page_url}")


if __name__ == "__main__":
    main()
