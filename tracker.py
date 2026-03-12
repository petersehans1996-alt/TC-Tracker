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
import time
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
    ("Sifted",              "https://sifted.eu/feed/?post_type=article"),
    ("FT Technology",       "https://www.ft.com/technology?format=rss"),
    ("FT Climate Capital",  "https://www.ft.com/climate-capital?format=rss"),
    ("FT Companies",        "https://www.ft.com/companies/technology?format=rss"),
]

# Keywords to filter FT articles — only include if title/snippet matches at least one
FT_RELEVANT_KEYWORDS = [
    "startup", "venture", "vc ", "funding", "investment", "raise", "series",
    "ai ", "artificial intelligence", "climate", "energy", "fintech", "saas",
    "europe", "european", "ipo", "acquisition", "tech", "software", "deeptech",
]

DAILY_MAX_ARTICLES  = 40
WEEKLY_MAX_ARTICLES = 120  # broader net over 7 days
DAILY_LOOKBACK_H    = 26
WEEKLY_LOOKBACK_H   = 7 * 24 + 4  # 7 days + buffer

# Notion block chunk size — API enforces max 100 children per request
NOTION_CHUNK_SIZE = 100
# Brief pause between consecutive Notion PATCH calls to avoid rate-limiting
NOTION_CHUNK_DELAY = 0.35  # seconds

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

                title_text = entry.get("title", "").strip()
                combined   = (title_text + " " + text).lower()

                # For FT feeds, only include topically relevant articles
                if source.startswith("FT"):
                    if not any(kw in combined for kw in FT_RELEVANT_KEYWORDS):
                        continue

                articles.append({
                    "title":     title_text,
                    "url":       link,
                    "snippet":   text,
                    "category":  category,
                    "published": published.isoformat() if published else "",
                    "source":    source,
                })
        except Exception as e:
            print(f"[WARN] Failed to fetch {source}: {e}")

    unique = list({a["url"]: a for a in articles}.values())
    # Sort newest first so most recent articles are prioritised if we cap
    unique.sort(key=lambda a: a["published"], reverse=True)
    print(f"[INFO] Fetched {len(unique)} unique articles (capping at {max_articles})")
    return unique[:max_articles]


# ── Prompts ───────────────────────────────────────────────────────────────────
DAILY_PROMPT = """You are a sharp tech analyst with strong opinions. You will receive today's articles from TechCrunch, Sifted, and the Financial Times.

Write a daily briefing. Be direct, opinionated, and willing to call out hype or contrarian signals. Sound like a smart colleague, not a press release.

When referencing a specific story, briefly attribute the source inline — e.g. "TechCrunch reports...", "According to the FT...", "Sifted notes...". Don't do this for every sentence, only when it adds useful context or when sources diverge.

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

WEEKLY_PROMPT = """You are a sharp, opinionated tech analyst writing a weekly column called "This Week in Tech." Your reader is a VC/startup professional who wants signal over noise — balanced across venture, technology, and macro/policy, but with no patience for fluff. You're willing to be contrarian, call out hype, and say what others won't.

Articles come from three sources: TechCrunch (global tech news), Sifted (European startup focus), and the Financial Times (macro, policy, markets). Weigh all three, but note when a story is distinctly European vs. global, and when the FT's macro angle adds context that TechCrunch misses.

When referencing a specific story, briefly attribute the source inline — e.g. "TechCrunch reports...", "The FT notes...", "Sifted's coverage suggests...". Don't over-attribute — only do it when the source perspective is meaningful or when sources diverge on the same topic.

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
        pub   = a["published"][:10] if a["published"] else "?"
        badge = SOURCE_BADGE.get(a.get("source", ""), "?")
        formatted.append(
            f"{i}. [{badge}] [{a['category']}] [{pub}] {a['title']}\n   {a['snippet'][:350]}"
        )

    if mode == "weekly":
        prompt     = WEEKLY_PROMPT.replace("{week_range}", week_range) + "\n\n".join(formatted)
        max_tokens = 8000   # increased from 4000 to avoid mid-response cutoff
    else:
        prompt     = DAILY_PROMPT + "\n\n".join(formatted)
        max_tokens = 4000   # increased from 2000 to avoid mid-response cutoff

    print(f"[INFO] Sending {len(articles)} articles to Claude ({mode} mode)...")
    response = client.messages.create(
        model="claude-opus-4-5",
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": prompt}],
    )

    # Warn if Claude hit the token ceiling mid-response
    stop_reason = response.stop_reason
    if stop_reason == "max_tokens":
        print("[WARN] Claude hit max_tokens — output may be truncated. Consider raising max_tokens further.")

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


SOURCE_BADGE = {
    "TechCrunch Main":     "TC",
    "TechCrunch AI":       "TC",
    "TechCrunch Startups": "TC",
    "TechCrunch Venture":  "TC",
    "TechCrunch Climate":  "TC",
    "Sifted":              "Sifted",
    "FT Technology":       "FT",
    "FT Climate Capital":  "FT",
    "FT Companies":        "FT",
}

# Special sections that get their own colored callout block in Notion
SPECIAL_SECTIONS = {
    "CLIMATE TECH CORNER":    ("🌱", "green_background"),
    "EUROPE WATCH":           ("🇪🇺", "blue_background"),
    "THE CONTRARIAN TAKE":    ("⚡", "yellow_background"),
    "TL;DR":                  ("💡", "purple_background"),
    "ONE TO WATCH NEXT WEEK": ("👀", "orange_background"),
    "ONE TO WATCH":           ("👀", "orange_background"),
}


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


def toggle_block(heading: str, children: list[dict]) -> dict:
    """A collapsible toggle block with nested children."""
    return {
        "object": "block",
        "type": "toggle",
        "toggle": {
            "rich_text": [{"type": "text", "text": {"content": heading},
                           "annotations": {"bold": True}}],
            "children": children,
        },
    }


def parse_analysis_to_blocks(analysis: str) -> list[dict]:
    """
    Convert markdown-style analysis text into Notion blocks with:
    - H2 for all section headings (scannable)
    - Dividers between every section
    - Colored callout blocks for special sections (TL;DR, Climate, Europe, Contrarian)
    - Plain paragraphs for body text
    """
    blocks       = []
    lines        = analysis.split("\n")
    i            = 0
    in_special   = None   # tracks if we're collecting body for a special section
    special_body = []

    def flush_special():
        """Emit a colored callout for the accumulated special section body."""
        nonlocal in_special, special_body
        if in_special and special_body:
            emoji, color = SPECIAL_SECTIONS[in_special]
            body_text    = " ".join(special_body).strip()
            if body_text:
                blocks.append(callout_block(body_text, emoji=emoji, color=color))
            blocks.append(divider_block())
        in_special   = None
        special_body = []

    while i < len(lines):
        line = lines[i].strip()
        i   += 1

        if not line:
            continue

        # Detect a heading line: **HEADING** or **HEADING TEXT**
        heading_match = re.match(r"^\*\*([^*]+)\*\*$", line)
        if heading_match:
            clean = heading_match.group(1).strip()
            upper = clean.upper()

            # Flush any in-progress special section
            flush_special()

            # Check if this is a special section
            matched_special = next(
                (k for k in SPECIAL_SECTIONS if k in upper), None
            )

            if matched_special:
                in_special   = matched_special
                special_body = []
                # Don't emit a heading block — the callout carries it
            else:
                # Normal section: H2 heading + divider above
                if blocks and blocks[-1].get("type") != "divider":
                    blocks.append(divider_block())
                blocks.append(heading_block(clean, level=2))
        else:
            # Body text
            if in_special is not None:
                special_body.append(line)
            else:
                blocks.append(text_block(line))

    flush_special()
    return blocks


def append_blocks_chunked(page_id: str, blocks: list[dict], label: str = "blocks") -> None:
    """
    Append an arbitrary number of blocks to a Notion page by splitting into
    NOTION_CHUNK_SIZE batches. A short delay between calls prevents rate-limiting.
    """
    total = len(blocks)
    if total == 0:
        return

    for start in range(0, total, NOTION_CHUNK_SIZE):
        chunk = blocks[start:start + NOTION_CHUNK_SIZE]
        try:
            notion_request("PATCH", f"/blocks/{page_id}/children", {"children": chunk})
            end = min(start + NOTION_CHUNK_SIZE, total)
            print(f"[INFO] Appended {label} blocks {start + 1}–{end} of {total}.")
        except Exception as e:
            print(f"[WARN] Could not append {label} chunk starting at {start}: {e}")
        if start + NOTION_CHUNK_SIZE < total:
            time.sleep(NOTION_CHUNK_DELAY)


def post_to_notion(analysis: str, articles: list[dict], mode: str, week_range: str = "") -> str:
    today = datetime.date.today()

    if mode == "weekly":
        title        = f"This Week in Tech — {week_range}"
        callout_text = f"📰 Weekly briefing · {len(articles)} articles · TechCrunch + Sifted + FT · {week_range}"
        emoji        = "🗞️"
    else:
        date_str     = today.strftime("%d %b %Y")
        title        = f"Tech Briefing — {date_str}"
        callout_text = f"📰 {len(articles)} articles analyzed · TechCrunch + Sifted + FT · {today.strftime('%A, %d %B %Y')}"
        emoji        = "📰"

    # Build the full list of analysis blocks — no slicing here
    all_analysis_blocks = [
        callout_block(callout_text, emoji=emoji),
        divider_block(),
        *parse_analysis_to_blocks(analysis),
        divider_block(),
    ]

    # Build article toggle children (Notion toggle children are capped at 100 inline;
    # we stay under that limit by capping article list at 95)
    article_blocks = []
    for a in articles:
        pub   = a["published"][:10] if a["published"] else ""
        badge = SOURCE_BADGE.get(a.get("source", ""), "?")
        label = f"[{badge}] {pub} — {a['title']}"
        article_blocks.append({
            "object": "block",
            "type": "bulleted_list_item",
            "bulleted_list_item": {
                "rich_text": [{
                    "type": "text",
                    "text": {"content": label, "link": {"url": a["url"]}},
                }]
            },
        })

    article_toggle = toggle_block(
        f"📋 {len(articles)} articles analyzed", article_blocks[:95]
    )

    # ── Step 1: Create the page with the first chunk of analysis blocks ────────
    first_chunk = all_analysis_blocks[:NOTION_CHUNK_SIZE]
    remaining   = all_analysis_blocks[NOTION_CHUNK_SIZE:]

    page_payload = {
        "parent": {"page_id": NOTION_PARENT_PAGE},
        "icon":   {"type": "emoji", "emoji": emoji},
        "properties": {
            "title": {"title": [{"type": "text", "text": {"content": title}}]}
        },
        "children": first_chunk,
    }

    print("[INFO] Creating Notion page...")
    result   = notion_request("POST", "/pages", page_payload)
    page_url = result.get("url", "")
    page_id  = result.get("id", "")
    print(f"[INFO] Notion page created: {page_url}")

    if not page_id:
        print("[ERROR] No page_id returned — cannot append remaining blocks.")
        return page_url

    # ── Step 2: Append any remaining analysis blocks in chunks ─────────────────
    if remaining:
        time.sleep(NOTION_CHUNK_DELAY)
        append_blocks_chunked(page_id, remaining, label="analysis")

    # ── Step 3: Append the article toggle last ─────────────────────────────────
    time.sleep(NOTION_CHUNK_DELAY)
    try:
        notion_request("PATCH", f"/blocks/{page_id}/children", {"children": [article_toggle]})
        print("[INFO] Article toggle appended.")
    except Exception as e:
        print(f"[WARN] Could not append article toggle: {e}")

    if mode == "weekly":
        update_parent_page_description(week_range, len(articles))

    return page_url


def update_parent_page_description(week_range: str, article_count: int) -> None:
    """
    Place a description callout at the very top of the parent Notion page.
    Deletes any existing callout block that was previously placed there,
    then inserts a fresh one above all other content.
    """
    today       = datetime.date.today().strftime("%A, %d %B %Y")
    description = (
        f"Daily and weekly tech briefings analyzed by Claude. "
        f"Sources: TechCrunch · Sifted · Financial Times. "
        f"Topics: AI, venture capital, climate tech, European startups, fintech, macro & policy. "
        f"Last weekly briefing: week of {week_range}, posted {today} · {article_count} articles analyzed."
    )

    try:
        # 1. Fetch the first block on the parent page
        result = notion_request("GET", f"/blocks/{NOTION_PARENT_PAGE}/children?page_size=10")
        blocks = result.get("results", [])

        # 2. If the first block is already our description callout, delete it
        if blocks and blocks[0].get("type") == "callout":
            old_block_id = blocks[0]["id"]
            try:
                notion_request("DELETE", f"/blocks/{old_block_id}")
                print("[INFO] Removed old description callout.")
            except Exception as e:
                print(f"[WARN] Could not delete old callout: {e}")

        new_callout = {
            "object": "block",
            "type": "callout",
            "callout": {
                "rich_text": [{"type": "text", "text": {"content": description}}],
                "icon": {"type": "emoji", "emoji": "🗞️"},
                "color": "blue_background",
            },
        }

        # Re-fetch first block after potential deletion
        result2  = notion_request("GET", f"/blocks/{NOTION_PARENT_PAGE}/children?page_size=1")
        blocks2  = result2.get("results", [])
        first_id = blocks2[0]["id"] if blocks2 else None

        if first_id:
            body = {
                "children": [new_callout],
                "after": "",  # empty string = prepend to top
            }
        else:
            body = {"children": [new_callout]}

        notion_request("PATCH", f"/blocks/{NOTION_PARENT_PAGE}/children", body)
        print("[INFO] Parent page description updated at top.")

    except Exception as e:
        print(f"[WARN] Could not update parent page description: {e}")


# ── Main ──────────────────────────────────────────────────────────────────────
def get_week_range() -> str:
    today    = datetime.date.today()
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
