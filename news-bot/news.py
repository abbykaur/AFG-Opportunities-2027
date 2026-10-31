"""AI news bot: a weekly batch of stories for the #ai-news forum, curated by mods.

The weekly cycle (Pacific time):
  Monday   the bot drafts the week's top stories into #news-queue (mods only):
           mostly articles, plus a couple of YouTube videos.
  Mon-Fri  mods react ✅ / ❌ on drafts, reply to a draft to edit it, or post
           "more: <topic>" for extra drafts.
  Friday   from 10 AM, every ✅ draft becomes its own post in the #ai-news
           forum, all at once, so the forum gets new stories once a week.

It runs once per invocation (GitHub Actions, every 2 hours on weekdays), like
the jobs bot, and works out from the day what to do. There are no buttons:
buttons need a bot that's online when they're clicked, and this one only runs
for a minute at a time. Reactions and replies stay on the message, so the next
run can still read them.

State (stories already drafted, open drafts, which weeks are done) lives in
state.json, which the workflow commits back after each run.
"""
import os
import re
import html
import json
import random
import asyncio
import logging
import urllib.parse
from pathlib import Path
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import discord
import feedparser
from dotenv import load_dotenv

load_dotenv(Path(__file__).with_name(".env"))

TOKEN = os.getenv("NEWS_DISCORD_TOKEN")
QUEUE_CHANNEL_ID = int(os.getenv("NEWS_QUEUE_CHANNEL_ID", "0"))
FORUM_CHANNEL_ID = int(os.getenv("NEWS_FORUM_CHANNEL_ID", "0"))
# Manual runs can force a step regardless of the day: "draft" or "roundup".
FORCE = os.getenv("NEWS_FORCE", "").strip().lower()

STATE_PATH = Path(__file__).with_name("state.json")

FEEDS = {
    "TechCrunch": "https://techcrunch.com/category/artificial-intelligence/feed/",
    "The Verge": "https://www.theverge.com/rss/ai-artificial-intelligence/index.xml",
    "MIT Technology Review": "https://www.technologyreview.com/topic/artificial-intelligence/feed",
    "Ars Technica": "https://arstechnica.com/ai/feed/",
    "OpenAI": "https://openai.com/news/rss.xml",
    "Google DeepMind": "https://deepmind.google/blog/rss.xml",
}
# "more: <topic>" searches Google News for the past week.
TOPIC_SEARCH_URL = "https://news.google.com/rss/search?q={q}+when:7d&hl=en-US&gl=US&ceid=US:en"
FEED_AGENT = "Mozilla/5.0 (compatible; afg-news-bot)"

# YouTube channels for the video slots, grouped into categories. Every channel
# has a free RSS feed. To add one, open the channel page, view the page source,
# and search for "channelId" to find its ID.
VIDEO_CHANNELS = {
    "Paper breakdowns": {"Two Minute Papers": "UCbfYPyITQ-7l4upoX8nvctg",
                         "Yannic Kilcher": "UCZHmQk67mSJgfCCTn7xBfew"},
    "News & analysis":  {"AI Explained": "UCNJ1Ymd5yFuUPtn21xtRbbw",
                         "Matthew Berman": "UCawZsQWqfGSbCI5yjkdVkTA"},
    "Official labs":    {"OpenAI": "UCXZCJLdBC09xxGZ6gcdrc6A",
                         "Google DeepMind": "UCP7jMXSY2xbc3KCAE0MHQ-A",
                         "Anthropic": "UCrDwWp7EBBv4NwvScIpBDOA"},
    "Learning":         {"3Blue1Brown": "UCYO_jab_esuFRV4b17AJtAw",
                         "Andrej Karpathy": "UCXUPKJO5MZQN11PqgIvyuvQ"},
}
VIDEO_FEED_URL = "https://www.youtube.com/feeds/videos.xml?channel_id={}"
# Feeds mix in promos for their own events ("5 days left: save at TechCrunch
# Founder Summit"). Titles matching this are dropped.
PROMO_RE = re.compile(r"\btechcrunch\b|\btickets?\b|\bdays left\b|\blast chance\b", re.IGNORECASE)

# Schedule, in Pacific time. Drafting can happen Mon-Thu (in case Monday's run
# fails), but only once a week; the roundup goes out once, Friday from 10 AM.
TZ = ZoneInfo("America/Los_Angeles")
DRAFT_DAYS = {0, 1, 2, 3}          # Monday=0
ROUNDUP_DAY, ROUNDUP_HOUR = 4, 10  # Friday, 10 AM
# Every story the bot posts gets this forum tag, next to its topic tag, so it
# lists all mod-picked stories. It's a mod-only tag, which the bot can apply
# because it has Manage Threads.
ROUNDUP_TAG = "Weekly Roundup"
# Video posts also get this tag, if the forum has one by this name.
VIDEO_TAG = "Video"

WEEKLY_CANDIDATES = 8
# Monday tops the queue up to at most this many open drafts, counting ones
# carried over from last week, so mods never face more than 11 at once.
# "more" requests add on top: a mod asked for them.
MAX_OPEN_DRAFTS = 11
# How many of the weekly drafts are videos, each from a different category.
# Categories mods keep approving get picked more often (see video_weights).
VIDEO_SLOTS = 2
DRAFTS_PER_REQUEST = 3
MAX_STORY_AGE = timedelta(days=7)
MAX_VIDEO_AGE = timedelta(days=14)  # video channels post less often than news sites
# Decisions from the last HISTORY_WEEKS weeks steer the video picks.
HISTORY_WEEKS = 8
SEEN_TTL = timedelta(days=30)
BLURB_CHARS = 300

APPROVE, SKIP = "✅", "❌"
COLOR_PENDING, COLOR_IN, COLOR_OUT = 0xF1C40F, 0x2ECC71, 0x95A5A6
HOW_TO = (f"{APPROVE} post Friday · {SKIP} leave out · "
          "reply with title: / blurb: / tag: to edit")

# Forum tag for each story, matched against the title and blurb (lowercased).
# Keywords match whole words, plural included ("model" hits "models", "ban"
# doesn't hit "bank"). The first tag with a match wins, so the more specific
# tags come first. Names must match the #ai-news forum's tags.
TAG_KEYWORDS = {
    "Policy & Safety":    ["regulation", "regulator", "law", "lawmaker", "senate", "congress", "government", "eu ai act",
                           "safety", "security", "privacy", "lawsuit", "court", "copyright", "fair use", "scraping",
                           "opt out", "ban", "alignment", "election", "misinformation", "deepfake", "fraud",
                           "backdoor", "hack", "national security", "doj", "ftc", "protest", "ethic"],
    "Startups & Funding": ["raises", "funding", "series a", "series b", "valuation", "acquires", "acquisition", "ipo",
                           "startup", "invest", "investor", "stock", "stake", "revenue", "profit", "spending",
                           "data center", "shutting down", "shuts down", "cosigner", "deal", "roi"],
    "Careers":            ["hiring", "layoffs", "job", "internship", "workforce", "talent", "at work", "workplace",
                           "worker", "employee", "career"],
    "Research":           ["paper", "study", "research", "researchers", "scientist", "benchmark", "arxiv", "breakthrough"],
    "Models & Releases":  ["launches", "releases", "announces", "unveils", "gpt", "chatgpt", "claude", "gemini",
                           "llama", "model", "open source"],
    "Tools":              ["app", "tool", "api", "plugin", "feature", "agent", "copilot", "assistant", "extension",
                           "chatbot", "search", "robot", "autonomy", "enterprise"],
}

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("newsbot")


def load_state() -> dict:
    try:
        state = json.loads(STATE_PATH.read_text())
    except FileNotFoundError:
        state = {}
    state.setdefault("seen", {})            # story url -> ISO time first drafted
    state.setdefault("drafts", {})          # queue message id -> draft fields
    state.setdefault("last_message_id", None)
    state.setdefault("drafted_week", None)  # ISO week ("2026-W44") last drafted
    state.setdefault("roundup_week", None)  # ISO week last rounded up
    # How each closed draft went, to steer future picks. Kind, category, source,
    # tag and outcome only: no names or comments, since the repo is public.
    state.setdefault("history", [])
    return state


def save_state(state: dict) -> None:
    cutoff = datetime.now(timezone.utc) - SEEN_TTL
    state["seen"] = {u: t for u, t in state["seen"].items()
                     if datetime.fromisoformat(t) > cutoff}
    STATE_PATH.write_text(json.dumps(state, indent=2, ensure_ascii=False) + "\n")


def clean_text(raw: str, limit: int = BLURB_CHARS) -> str:
    """RSS summaries are HTML; strip tags and trim to a blurb at a word break."""
    text = html.unescape(re.sub(r"<[^>]+>", " ", raw or ""))
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= limit:
        return text
    return text[:limit].rsplit(" ", 1)[0].rstrip(",;:") + "…"


def tag_key(name: str) -> str:
    """Forum tag names compared without emoji, case or extra spaces, so
    "🧪 Research" in the forum still matches "Research" here."""
    return " ".join(re.findall(r"[a-z0-9&]+", name.lower()))


def pick_tag(title: str, blurb: str) -> str | None:
    # The headline says what the story is about; the blurb only breaks ties
    # when the headline has none of the keywords.
    for text in (title.lower(), blurb.lower()):
        for tag, words in TAG_KEYWORDS.items():
            if any(re.search(r"\b" + re.escape(w) + r"s?\b", text) for w in words):
                return tag
    return None


def fetch_stories(url: str, source: str | None = None) -> list[dict]:
    """Stories from the past week in one feed, newest first."""
    parsed = feedparser.parse(url, agent=FEED_AGENT)
    cutoff = datetime.now(timezone.utc) - MAX_STORY_AGE
    stories = []
    for e in parsed.entries:
        when = e.get("published_parsed") or e.get("updated_parsed")
        published = datetime(*when[:6], tzinfo=timezone.utc) if when else None
        if published and published < cutoff:
            continue
        title = clean_text(e.get("title", ""), limit=100)
        name = source
        if name is None:
            # Google News titles end in " - Publisher"; use that as the source.
            name = (e.get("source") or {}).get("title") or "Google News"
            title = title.removesuffix(f" - {name}")
        if not title or not e.get("link") or PROMO_RE.search(title):
            continue
        blurb = clean_text(e.get("summary", ""))
        if blurb.startswith(title):     # some feeds repeat the title as the summary
            blurb = blurb[len(title):].strip(" -–—:")
        stories.append({
            "title": title,
            "blurb": blurb,
            "url": e.link,
            "source": name,
            "published": published.isoformat() if published else None,
            "tag": pick_tag(title, blurb),
        })
    stories.sort(key=lambda s: s["published"] or "", reverse=True)
    return stories


def weekly_candidates(state: dict, count: int) -> list[dict]:
    """Newest new stories, taken one source at a time so no outlet dominates."""
    per_source = []
    for source, url in FEEDS.items():
        try:
            fresh = [s for s in fetch_stories(url, source) if s["url"] not in state["seen"]]
        except Exception as e:
            log.error("Feed '%s' failed: %s", source, e)
            continue
        if fresh:
            per_source.append(fresh)
    picked = []
    while per_source and len(picked) < count:
        for stories in list(per_source):
            picked.append(stories.pop(0))
            if not stories:
                per_source.remove(stories)
            if len(picked) == count:
                break
    return picked


def video_blurb(description: str) -> str:
    """First paragraph of a video description that isn't sponsor links or chapters."""
    for para in re.split(r"\n\s*\n", description or ""):
        para = para.strip()
        # Sponsor and link lines usually start with an emoji, or contain a URL.
        if (para and para[0].isalnum() and "http" not in para
                and not re.search(r"\b\d{1,2}:\d{2}\b", para)):
            return clean_text(para)
    return ""


def fetch_videos(state: dict) -> dict[str, list[dict]]:
    """Recent videos per category, most-viewed first. Shorts are skipped."""
    cutoff = datetime.now(timezone.utc) - MAX_VIDEO_AGE
    by_category = {}
    for category, channels in VIDEO_CHANNELS.items():
        videos = []
        for name, channel_id in channels.items():
            parsed = feedparser.parse(VIDEO_FEED_URL.format(channel_id), agent=FEED_AGENT)
            for e in parsed.entries:
                link = e.get("link", "")
                when = e.get("published_parsed")
                if not link or "/shorts/" in link or link in state["seen"] or not when:
                    continue
                published = datetime(*when[:6], tzinfo=timezone.utc)
                if published < cutoff:
                    continue
                title = clean_text(e.get("title", ""), limit=100)
                blurb = video_blurb(e.get("summary", ""))
                videos.append({
                    "title": title, "blurb": blurb, "url": link, "source": name,
                    "published": published.isoformat(), "tag": pick_tag(title, blurb),
                    "kind": "video", "category": category,
                    "views": int((e.get("media_statistics") or {}).get("views") or 0),
                })
        if videos:
            by_category[category] = sorted(videos, key=lambda v: v["views"], reverse=True)
    return by_category


def video_weights(state: dict) -> dict[str, float]:
    """How strongly to favor each video category, from mods' recent decisions.

    Each category's approval rate over the last HISTORY_WEEKS weeks, smoothed
    as (approved + 1) / (shown + 2) so a new or rarely shown category starts at
    0.5, not 0 or 1. A ❌ counts fully against a category, but a draft nobody
    reacted to counts half, since that often means nobody had time. Squaring
    the rate means a category mods keep approving clearly wins, while the
    others still come up now and then.
    """
    weeks = sorted({h["week"] for h in state["history"]})[-HISTORY_WEEKS:]
    recent = [h for h in state["history"] if h["week"] in weeks and h.get("kind") == "video"]
    weights = {}
    for category in VIDEO_CHANNELS:
        shown = [h for h in recent if h.get("category") == category]
        approved = sum(h["decision"] == "approved" for h in shown)
        rejected = sum(h["decision"] == "rejected" for h in shown)
        ignored = sum(h["decision"] == "ignored" for h in shown)
        weights[category] = ((approved + 1) / (approved + rejected + ignored / 2 + 2)) ** 2
    return weights


def pick_videos(state: dict, count: int) -> list[dict]:
    """The most-viewed recent video from `count` different categories.

    Categories are drawn at random, weighted by video_weights, so the mix
    leans toward what mods approve without ever settling on one category.
    """
    by_category = fetch_videos(state)
    weights = video_weights(state)
    picked = []
    while by_category and len(picked) < count:
        categories = list(by_category)
        category = random.choices(categories, weights=[weights[c] for c in categories])[0]
        picked.append(by_category.pop(category)[0])
    return picked


def video_summary(state: dict) -> str:
    """One line of video approvals per category, e.g. 'Official labs 2/3'."""
    weeks = sorted({h["week"] for h in state["history"]})[-HISTORY_WEEKS:]
    recent = [h for h in state["history"] if h["week"] in weeks and h.get("kind") == "video"]
    parts = []
    for category in VIDEO_CHANNELS:
        shown = [h for h in recent if h.get("category") == category]
        if shown:
            approved = sum(h["decision"] == "approved" for h in shown)
            parts.append(f"{category} {approved}/{len(shown)}")
    return " · ".join(parts)


def topic_stories(topic: str, state: dict) -> list[dict]:
    url = TOPIC_SEARCH_URL.format(q=urllib.parse.quote(topic))
    return [s for s in fetch_stories(url) if s["url"] not in state["seen"]]


def draft_embed(d: dict, status: str = "pending", by: str | None = None) -> discord.Embed:
    if status == "pending" and d.get("carried"):
        status = "carried"
    color = {"pending": COLOR_PENDING, "carried": COLOR_PENDING, "included": COLOR_IN}.get(status, COLOR_OUT)
    title = f"🎥 {d['title']}" if d.get("kind") == "video" else d["title"]
    embed = discord.Embed(title=title[:256], url=d["url"],
                          description=d["blurb"] or "*(no blurb: reply with blurb: to add one)*",
                          color=color)
    embed.add_field(name="Tag", value=d["tag"] or "none")
    embed.add_field(name="Source", value=d["source"])
    if d.get("kind") == "video":
        embed.add_field(name="Video type", value=d["category"])
    if d.get("requested"):
        embed.add_field(name="Requested", value=d["requested"])
    footer = {"pending": HOW_TO,
              "carried": f"Carried over: last chance this Friday. {HOW_TO}",
              "included": f"Posted to #ai-news (approved by {by})",
              "left_out": "Not posted this week"}[status]
    embed.set_footer(text=footer)
    return embed


def parse_edit(text: str) -> dict:
    """'title: …' / 'blurb: …' / 'tag: …' lines, or a plain reply as a new blurb."""
    edits = {}
    for line in text.splitlines():
        m = re.match(r"\s*(title|blurb|tag)\s*:\s*(.*)", line, re.IGNORECASE)
        if m:
            edits[m.group(1).lower()] = m.group(2).strip()
    if not edits and text.strip():
        edits["blurb"] = text.strip()
    if "title" in edits:
        edits["title"] = edits["title"][:100]
    if "blurb" in edits:
        edits["blurb"] = edits["blurb"][:1500]  # leaves room for the link in a 2000-char post
    if edits.get("tag", "").lower() == "none":
        edits["tag"] = None
    return edits


async def read_queue_messages(queue: discord.TextChannel, state: dict) -> list[str | None]:
    """Apply edit replies to drafts; return requested topics (None = more of the feeds)."""
    requests = []
    if state["last_message_id"] is None:
        # First run: start from now rather than replaying the channel's history.
        last = [m async for m in queue.history(limit=1)]
        state["last_message_id"] = last[0].id if last else 0
        return requests

    after = discord.Object(id=state["last_message_id"]) if state["last_message_id"] else None
    async for msg in queue.history(after=after, limit=200, oldest_first=True):
        state["last_message_id"] = msg.id
        if msg.author.bot:
            continue
        if not msg.content and not msg.attachments:
            # Discord blanks message text for bots without the privileged intent.
            log.warning("Message %s from %s has no text: turn on Message Content "
                        "Intent for this bot in the Developer Portal.", msg.id, msg.author)
            continue
        ref = msg.reference.message_id if msg.reference else None
        if ref and str(ref) in state["drafts"]:
            draft = state["drafts"][str(ref)]
            draft.update(parse_edit(msg.content))
            try:
                original = await queue.fetch_message(ref)
                await original.edit(embed=draft_embed(draft))
                await msg.add_reaction("✏️")
            except discord.NotFound:
                pass
            continue
        m = re.match(r"\s*more\b\s*:?\s*(.*)", msg.content, re.IGNORECASE | re.DOTALL)
        if m:
            requests.append(m.group(1).strip() or None)
            await msg.add_reaction("👀")
    return requests


async def decided_by(msg: discord.Message, emoji: str) -> str | None:
    """Display name of the first person (not a bot) who reacted with emoji."""
    for reaction in msg.reactions:
        if str(reaction.emoji) == emoji:
            async for user in reaction.users():
                if not user.bot:
                    return user.display_name
    return None


async def post_roundup(queue: discord.TextChannel, forum: discord.ForumChannel,
                       state: dict) -> None:
    """Friday: post every ✅ draft as its own forum post, and close out the rest.

    A draft nobody reacted to carries over to next week once, then closes, so
    the queue doesn't fill up with old stories.

    Each draft is closed only after its post succeeds, so if Discord refuses a
    post (e.g. missing permissions in the forum), the rest stay open and the
    next run retries them without re-posting the ones that went through.
    """
    if not state["drafts"]:
        return                             # nothing was drafted (e.g. first week)
    tags_by_name = {tag_key(t.name): t for t in forum.available_tags}
    posted = carried = 0
    missing = set()
    for msg_id, d in list(state["drafts"].items()):
        try:
            msg = await queue.fetch_message(int(msg_id))
        except discord.NotFound:
            del state["drafts"][msg_id]    # a mod deleted the draft
            continue
        # Skip wins if a draft has both, so one ❌ is enough to leave it out.
        skipped = await decided_by(msg, SKIP)
        who = None if skipped else await decided_by(msg, APPROVE)
        if not who and not skipped and not d.get("carried"):
            d["carried"] = True
            await msg.edit(embed=draft_embed(d))
            carried += 1
            continue
        if who:
            names = ([ROUNDUP_TAG] + ([VIDEO_TAG] if d.get("kind") == "video" else [])
                     + ([d["tag"]] if d["tag"] else []))
            tags = [tags_by_name[tag_key(n)] for n in names if tag_key(n) in tags_by_name]
            missing |= {n for n in names if tag_key(n) not in tags_by_name and n != VIDEO_TAG}
            # The bare link lets Discord show the article's preview (or the video
            # player) under the blurb.
            content = f"{d['blurb']}\n\n{d['url']}".strip()
            try:
                await forum.create_thread(name=d["title"], content=content[:2000], applied_tags=tags)
            except discord.HTTPException as e:
                await queue.send(f"Couldn't post to {forum.mention} ({e.status}: {e.text}). "
                                 "The remaining drafts stay open; the next run will try again.")
                raise
            posted += 1
        await msg.edit(embed=draft_embed(d, "included" if who else "left_out", who))
        state["history"].append({
            "week": datetime.now(TZ).strftime("%G-W%V"),
            "kind": d.get("kind", "article"), "category": d.get("category"),
            "source": d["source"], "tag": d["tag"],
            "decision": "approved" if who else "rejected" if skipped else "ignored",
        })
        del state["drafts"][msg_id]

    summary = (f"Posted this week's {posted} approved {'story' if posted == 1 else 'stories'} "
               f"to {forum.mention}." if posted else
               "No drafts were approved this week, so nothing was posted to #ai-news.")
    if carried:
        summary += (f" {carried} unreviewed {'draft carries' if carried == 1 else 'drafts carry'} "
                    f"over to next Friday, then {'closes' if carried == 1 else 'close'}.")
    if videos := video_summary(state):
        summary += f"\nVideos approved, last {HISTORY_WEEKS} weeks: {videos}."
    if missing:
        summary += (f"\n⚠️ {forum.mention} has no tag named {', '.join(sorted(missing))}, "
                    "so posts went out without it. Rename the forum tag or the one in TAG_KEYWORDS to match.")
    await queue.send(summary)
    log.info("Posted %d stories; carried over %d.", posted, carried)


async def post_draft(queue: discord.TextChannel, story: dict, state: dict,
                     requested: str | None = None) -> None:
    d = {k: story[k] for k in ("title", "blurb", "url", "source", "tag")}
    if story.get("kind") == "video":
        d["kind"], d["category"] = "video", story["category"]
    d["created"] = datetime.now(timezone.utc).isoformat()
    if requested:
        d["requested"] = requested
    msg = await queue.send(embed=draft_embed(d))
    await msg.add_reaction(APPROVE)
    await msg.add_reaction(SKIP)
    state["drafts"][str(msg.id)] = d
    state["seen"][story["url"]] = d["created"]


async def run_once() -> None:
    state = load_state()
    now = datetime.now(TZ)
    week = now.strftime("%G-W%V")
    loop = asyncio.get_running_loop()

    # Save even if a step fails partway, so what already happened (drafts
    # posted, stories published) isn't repeated by the next run.
    try:
        client = discord.Client(intents=discord.Intents.default())
        async with client:
            # REST only: everything below is a plain API call, so there's no need
            # to open a gateway connection and wait for on_ready.
            await client.login(TOKEN)
            log.info("Logged in as %s (application %s).", client.user, client.application_id)
            queue = await client.fetch_channel(QUEUE_CHANNEL_ID)
            forum = await client.fetch_channel(FORUM_CHANNEL_ID)
            log.info("Forum tags: %s", ", ".join(repr(t.name) for t in forum.available_tags) or "none")

            # Edits first, so a draft edited and approved in the same window goes
            # out with the edit.
            requests = await read_queue_messages(queue, state)

            # Post approved stories before making any new drafts, so drafts made in
            # this run (e.g. for a "more" request) stay open for mods to review.
            if FORCE == "roundup" or (now.weekday() == ROUNDUP_DAY and now.hour >= ROUNDUP_HOUR
                                      and state["roundup_week"] != week):
                await post_roundup(queue, forum, state)
                state["roundup_week"] = week

            if FORCE == "draft" or (now.weekday() in DRAFT_DAYS and state["drafted_week"] != week):
                open_now = len(state["drafts"])
                count = max(0, min(WEEKLY_CANDIDATES, MAX_OPEN_DRAFTS - open_now))
                # Keep the video share steady when Monday posts fewer than 8.
                n_videos = min(VIDEO_SLOTS, round(count * VIDEO_SLOTS / WEEKLY_CANDIDATES))
                videos = (await loop.run_in_executor(None, pick_videos, state, n_videos)
                          if n_videos else [])
                stories = (await loop.run_in_executor(None, weekly_candidates, state,
                                                      count - len(videos))
                           if count > len(videos) else []) + videos
                carried = (f" {open_now} {'draft is' if open_now == 1 else 'drafts are'} "
                           "still open from last week (above)." if open_now else "")
                await queue.send(f"**This week's AI news candidates.**{carried} {HOW_TO}. "
                                 f"Approved stories post to #ai-news Friday at {ROUNDUP_HOUR} AM PT. "
                                 "Post `more: <topic>` or `more videos` for extra drafts.")
                for story in stories:
                    await post_draft(queue, story, state)
                state["drafted_week"] = week

            for topic in requests:
                if topic and topic.lower() in ("video", "videos"):
                    stories = await loop.run_in_executor(None, pick_videos, state, DRAFTS_PER_REQUEST)
                elif topic:
                    stories = await loop.run_in_executor(None, topic_stories, topic, state)
                else:
                    stories = await loop.run_in_executor(None, weekly_candidates, state, DRAFTS_PER_REQUEST)
                if not stories:
                    await queue.send(f"Nothing new found for **{topic or 'the regular feeds'}**.")
                for story in stories[:DRAFTS_PER_REQUEST]:
                    await post_draft(queue, story, state, requested=f"more: {topic or 'feeds'}")

            log.info("Open drafts: %d.", len(state["drafts"]))
    finally:
        save_state(state)


if __name__ == "__main__":
    for name, value in (("NEWS_DISCORD_TOKEN", TOKEN),
                        ("NEWS_QUEUE_CHANNEL_ID", QUEUE_CHANNEL_ID),
                        ("NEWS_FORUM_CHANNEL_ID", FORUM_CHANNEL_ID)):
        if not value:
            raise SystemExit(f"{name} not set (news-bot/.env locally, repo secret in Actions)")
    asyncio.run(run_once())
