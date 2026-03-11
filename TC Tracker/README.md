# 📰 TechCrunch Sentiment & Theme Tracker

Daily and weekly briefings from TechCrunch, analyzed by Claude, delivered to Notion.

## What you get

**Daily** (Tue–Sun, 08:00 CET): A quick snapshot of the day's tech news — sentiment, top themes, notable narratives, and what to watch.

**Weekly** (Monday, 08:00 CET): "This Week on TechCrunch" — a full weekly column recapping the previous Mon–Sun with:
- The week in one paragraph
- 3–5 big themes (analytical, not descriptive)
- Deals & money
- Climate Tech Corner *(only if climate content appeared)*
- Europe Watch *(only if European stories appeared)*
- The Contrarian Take
- One to Watch Next Week

Tone: sharp, opinionated, willing to push back on consensus.

---

## Setup

### 1. Create a GitHub repo and add the files
```
tracker.py
requirements.txt
.github/workflows/briefings.yml
```

### 2. Create a Notion integration
1. Go to https://www.notion.so/my-integrations
2. Click **+ New integration** → name it "TC Tracker" → copy the **Internal Integration Secret**

### 3. Create your master Notion page
1. Create a page in Notion, e.g. "📰 TechCrunch Briefings"
2. Click `•••` → **Connect to** → select "TC Tracker"
3. Copy the page ID from the URL (32-char string at the end)

### 4. Add GitHub Secrets
**Settings → Secrets → Actions → New repository secret**

| Secret | Value |
|--------|-------|
| `ANTHROPIC_API_KEY` | Your key from console.anthropic.com |
| `NOTION_TOKEN` | Integration secret from step 2 |
| `NOTION_PARENT_PAGE_ID` | Page ID from step 3 |

### 5. Test manually
Go to **Actions → TechCrunch Briefings → Run workflow** and pick `daily` or `weekly`.

---

## Schedule
| Run | When | Cron |
|-----|------|------|
| Daily briefing | Tue–Sun at 08:00 CET | `0 7 * * 2-7` |
| Weekly briefing | Monday at 08:00 CET | `0 7 * * 1` |

Adjust the cron expressions in `briefings.yml` to change timing.

## Local usage
```bash
pip install -r requirements.txt

export ANTHROPIC_API_KEY="sk-ant-..."
export NOTION_TOKEN="secret_..."
export NOTION_PARENT_PAGE_ID="your-page-id"

python tracker.py          # auto-detects (weekly on Monday)
python tracker.py --weekly # force weekly
python tracker.py --daily  # force daily
```

## Cost
- Daily: ~40 articles × ~600 tokens ≈ **~$0.01/day**
- Weekly: ~120 articles × ~1000 tokens ≈ **~$0.05/week**
- GitHub Actions & Notion API: **free**
