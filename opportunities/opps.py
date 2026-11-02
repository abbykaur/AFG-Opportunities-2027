"""Opportunities bot: a few program picks a week for #opportunities, curated by mods.

  Monday    up to 4 programs are drafted into #opps-queue (mods only): ones whose
            usual deadline is this month or next, plus programs SimplifyJobs
            lists as open right now, which go first.
  Any run   ✅ drafts post to #opportunities right away (deadlines don't wait for
            Friday) and ❌ drafts close. Mods reply to a draft to edit it, or post
            `more`, `add: …` or `remove: …` in the queue.
  Daily     the pinned board message in #opportunities is refreshed.

Same bot, token, and run-once design as news-bot/news.py. The program list is
programs.json, which also renders the repo's README board (board.py); this
folder's state.json tracks drafts and is committed back by the workflow.
"""
import os
import re
import json
import asyncio
import logging
import urllib.request
from pathlib import Path
from datetime import datetime, timedelta, timezone

import discord
from dotenv import load_dotenv

import board
from programs import WHO, MONTHS, key, problems

load_dotenv(Path(__file__).with_name(".env"))

TOKEN = os.getenv("NEWS_DISCORD_TOKEN")   # the same AFG bot as the news feed
QUEUE_CHANNEL_ID = int(os.getenv("OPPS_QUEUE_CHANNEL_ID", "0"))
CHANNEL_ID = int(os.getenv("OPPS_CHANNEL_ID", "0"))
# Manual runs can force Monday's drafting on any day: "draft".
FORCE = os.getenv("OPPS_FORCE", "").strip().lower()

STATE_PATH = Path(__file__).with_name("state.json")
BOARD_URL = f"https://github.com/{os.getenv('GITHUB_REPOSITORY', 'abbykaur/job-scraping-bot')}#readme"
# SimplifyJobs' internship list for next summer also carries fellowships and
# underclassmen programs. It rolls over to the next summer in August.
SIMPLIFY_URL = ("https://raw.githubusercontent.com/SimplifyJobs/Summer{year}-Internships/dev/"
                ".github/scripts/listings.json")

TZ = board.TZ
DRAFT_DAYS = {0, 1, 2, 3}       # Monday=0; later days only if Monday's run failed
BOARD_HOUR = 6                  # refresh the pinned board on the first run after 6 AM PT
WEEKLY_CANDIDATES = 4
MAX_OPEN_DRAFTS = 6             # Monday never leaves more than this many open
DRAFTS_PER_REQUEST = 3
MAX_PER_TYPE = 2                # variety: at most 2 of one type per batch
DUE_WINDOW = 2                  # "due soon" = this month or next
REDRAFT_AFTER = timedelta(days=300)   # a program comes back next cycle, not sooner
CARRY_AFTER = timedelta(days=6)       # unreviewed by next Monday: carried once, then closed
HISTORY_WEEKS = 8

# Program-like titles on SimplifyJobs, and the research/medical roles to skip.
# "Insight" and "discovery" only count as program names ("Early Insight Program",
# "Discovery Day"), not as ordinary words ("Strategic Insight and Analytics").
PROGRAM_RE = re.compile(r"\b(fellows?(hip)?|sophomore|freshman|first[- ]year|early insights?|"
                        r"insights? (program|series|day|week)|discovery (program|day|series)|"
                        r"explore|summit|launch program|early career program|scholars? program)\b", re.I)
SKIP_RE = re.compile(r"\b(co-?op|post-?doc\w*|pre-?doctoral|ph\.?d|ms/phd|clinical|medical|"
                     r"medicine|surgical|visiting scholar|research fellow in)\b", re.I)

APPROVE, SKIP = "✅", "❌"
COLOR_PENDING, COLOR_IN, COLOR_OUT, COLOR_POST = 0xF1C40F, 0x2ECC71, 0x95A5A6, 0x9B59B6
HOW_TO = (f"{APPROVE} post to #opportunities · {SKIP} skip · "
          "reply with note: / title: to edit")

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("oppsbot")


def load_state() -> dict:
    try:
        state = json.loads(STATE_PATH.read_text())
    except FileNotFoundError:
        state = {}
    state.setdefault("drafts", {})           # queue message id -> draft fields
    state.setdefault("drafted", {})          # program key -> ISO time last drafted
    state.setdefault("last_message_id", None)
    state.setdefault("drafted_week", None)
    state.setdefault("board_message_id", None)
    state.setdefault("board_day", None)
    # How each closed draft went, by type only (the repo is public: no names).
    state.setdefault("history", [])
    return state


def save_state(state: dict) -> None:
    STATE_PATH.write_text(json.dumps(state, indent=2, ensure_ascii=False) + "\n")


def save_programs(programs: list[dict]) -> None:
    board.PROGRAMS_PATH.write_text(json.dumps(programs, indent=2, ensure_ascii=False) + "\n")
    board.README_PATH.write_text(board.render(programs, datetime.now(TZ)))


def fetch_simplify(now: datetime) -> list[dict]:
    """Active program-like listings on SimplifyJobs (fellowships, sophomore programs…)."""
    year = now.year + 1 if now.month >= 8 else now.year
    req = urllib.request.Request(SIMPLIFY_URL.format(year=year), headers={"User-Agent": "afg-opps-bot"})
    listings = json.load(urllib.request.urlopen(req, timeout=60))
    return [{"company": j.get("company_name", ""), "title": j.get("title", ""), "url": j.get("url", "")}
            for j in listings
            if j.get("active") and j.get("is_visible", True) and j.get("url")
            and PROGRAM_RE.search(j.get("title", "")) and not SKIP_RE.search(j.get("title", ""))]


def match_program(listing: dict, programs: list[dict]) -> dict | None:
    """The known program a listing belongs to: same company, and the program's
    name shares a distinctive word with the listing's title (e.g. "Sophomore")."""
    company = key(listing["company"])
    if len(company) < 3:
        return None
    title_words = set(re.findall(r"[a-z]{4,}", listing["title"].lower())) - {"intern", "internship", "program", "software", "engineer"}
    for p in programs:
        name = p["name"].lower()
        if company in key(p["name"]) and (not title_words or any(w in name for w in title_words)):
            return p
    return None


def guess_type(title: str) -> str:
    t = title.lower()
    if re.search(r"sophomore|freshman|first[- ]year", t):
        return "Underclassmen program"
    if re.search(r"insight|discovery|explore|summit", t):
        return "Insight / exploratory"
    return "Fellowship"


def guess_who(title: str) -> str:
    t = title.lower()
    found = [w for w, pat in (("Freshman", r"freshman|first[- ]year"), ("Sophomore", r"sophomore"),
                              ("Junior", r"junior"))
             if re.search(pat, t)]
    return ", ".join(found) or "All students"


def type_weights(state: dict) -> dict[str, float]:
    """How strongly to favor each program type, from mods' recent decisions.

    Same smoothing as the news bot's video_weights: (approved + 1) / (shown + 2),
    with an unreviewed draft counting half, squared so a type mods keep
    approving clearly wins while the others still come up.
    """
    weeks = sorted({h["week"] for h in state["history"]})[-HISTORY_WEEKS:]
    recent = [h for h in state["history"] if h["week"] in weeks]
    weights = {}
    for t in board.SECTIONS:
        shown = [h for h in recent if h["type"] == t]
        approved = sum(h["decision"] == "approved" for h in shown)
        rejected = sum(h["decision"] == "rejected" for h in shown)
        ignored = sum(h["decision"] == "ignored" for h in shown)
        weights[t] = ((approved + 1) / (approved + rejected + ignored / 2 + 2)) ** 2
    return weights


def candidates(state: dict, programs: list[dict], listings: list[dict], now: datetime) -> list[dict]:
    """Programs worth drafting now, best first.

    Known programs qualify when they're due this month or next, or SimplifyJobs
    lists them as open. Unknown SimplifyJobs programs qualify as "new". Each is
    scored by type weight, with open-now and women-focused programs boosted and
    later deadlines discounted. A program drafted in the last REDRAFT_AFTER days
    (or open in the queue) is skipped.
    """
    recent = {k for k, t in state["drafted"].items()
              if now - datetime.fromisoformat(t) < REDRAFT_AFTER}
    recent |= {key(d["name"]) for d in state["drafts"].values()}
    confirmed, new = {}, {}
    for listing in listings:
        p = match_program(listing, programs)
        if p:
            confirmed.setdefault(key(p["name"]), listing["url"])
        else:
            title, company = listing["title"], listing["company"]
            # "Anthropic Fellows Program", not "Anthropic Anthropic Fellows Program".
            name = (title if key(company) in key(title) else f"{company} {title}").strip()[:100]
            new.setdefault(key(name), {
                "name": name, "url": listing["url"], "type": guess_type(listing["title"]),
                "who": guess_who(listing["title"]), "deadline_month": None,
                "women": bool(re.search(r"\bwomen\b", listing["title"], re.I)),
                "source": "SimplifyJobs", "new": True, "open_url": listing["url"],
            })

    pool = []
    for p in programs:
        months = board.months_until(p["deadline_month"], now)
        if key(p["name"]) in confirmed or months < DUE_WINDOW:
            pool.append({**p, "open_url": confirmed.get(key(p["name"]))})
    pool += new.values()

    weights = type_weights(state)
    def score(p):
        # Open right now counts as due now. Programs not on the board yet are
        # half-weighted, since SimplifyJobs' program titles are noisier.
        months = 0 if p.get("open_url") else min(board.months_until(p["deadline_month"], now), 3)
        return (weights.get(p["type"], 0.25) * (2 if p.get("open_url") else 1)
                * (0.5 if p.get("new") else 1) * (1.5 if p.get("women") else 1) / (1 + months))
    pool = [p for p in pool if key(p["name"]) not in recent]
    return sorted(pool, key=score, reverse=True)


def pick(pool: list[dict], count: int) -> list[dict]:
    """Top of the pool, with at most MAX_PER_TYPE of any one type."""
    picked, per_type = [], {}
    for p in pool:
        if len(picked) == count:
            break
        if per_type.get(p["type"], 0) < MAX_PER_TYPE:
            picked.append(p)
            per_type[p["type"]] = per_type.get(p["type"], 0) + 1
    return picked


def when(d: dict) -> str:
    return board.when(d.get("deadline_month")).replace("Check site", "check the page")


def draft_embed(d: dict, status: str = "pending", by: str | None = None) -> discord.Embed:
    if status == "pending" and d.get("carried"):
        status = "carried"
    color = {"pending": COLOR_PENDING, "carried": COLOR_PENDING, "included": COLOR_IN}.get(status, COLOR_OUT)
    embed = discord.Embed(title=d["name"][:256], url=d["url"], color=color,
                          description=d.get("note") or "*(reply with note: to add a line that posts with it)*")
    embed.add_field(name="Type", value=d["type"])
    embed.add_field(name="For", value=d["who"])
    embed.add_field(name="Usually due", value=when(d))
    if d.get("open_url"):
        embed.add_field(name="Open now", value=f"[Listed on SimplifyJobs]({d['open_url']})", inline=False)
    else:
        # The board's months are rough (from community lists), so a pick that
        # isn't confirmed open may be closed or not open yet.
        embed.add_field(name="⚠️ Not confirmed open",
                        value=f"Usual timing only. Check [the page]({d['url']}) before {APPROVE}.", inline=False)
    if d.get("new"):
        embed.add_field(name="New", value="Not on the board yet; approving adds it.", inline=False)
    if d.get("women"):
        embed.add_field(name="💜", value="For women")
    if d.get("requested"):
        embed.add_field(name="Requested", value=d["requested"])
    footer = {"pending": HOW_TO,
              "carried": f"Carried over: closes next Monday. {HOW_TO}",
              "included": f"Posted to #opportunities (approved by {by})",
              "left_out": "Not posted"}[status]
    embed.set_footer(text=footer)
    return embed


def post_embed(d: dict) -> discord.Embed:
    """What members see in #opportunities."""
    lines = (["💜 **For women**"] if d.get("women") else []) + ([d["note"]] if d.get("note") else [])
    lines.append(f"[📋 All opportunities]({BOARD_URL})")
    embed = discord.Embed(title=d["name"][:256], url=d["url"], color=COLOR_POST,
                          description="\n\n".join(lines))
    embed.add_field(name="For", value=d["who"])
    embed.add_field(name="Type", value=d["type"])
    embed.add_field(name="Usually due", value=when(d))
    return embed


def board_embed(programs: list[dict], now: datetime) -> discord.Embed:
    """The pinned summary: what's coming up, with a link to the full board."""
    soon = sorted((p for p in programs if board.months_until(p["deadline_month"], now) < DUE_WINDOW),
                  key=lambda p: (board.months_until(p["deadline_month"], now), p["name"].lower()))
    lines = [f"• [{p['name']}]({p['url']}) · {p['who']} · {board.when(p['deadline_month'])}"
             + (" 💜" if p.get("women") else "") for p in soon]
    footer = f"\n\n[📋 See all {len(programs)} programs →]({BOARD_URL})"
    text = "**🔥 Coming up**\n"
    for line in lines:
        if len(text) + len(line) + len(footer) > 3900:
            text += "• …and more on the full board\n"
            break
        text += line + "\n"
    embed = discord.Embed(title="📌 Opportunities board", url=BOARD_URL, color=COLOR_POST,
                          description=text + footer)
    embed.set_footer(text="💜 = for women · dates are approximate, check each page · updated daily")
    embed.timestamp = now
    return embed


def parse_edit(text: str) -> dict:
    """'title: …' / 'note: …' lines, or a plain reply as the note."""
    edits = {}
    for line in text.splitlines():
        m = re.match(r"\s*(title|note)\s*:\s*(.*)", line, re.IGNORECASE)
        if m:
            edits[m.group(1).lower()] = m.group(2).strip()
    if not edits and text.strip():
        edits["note"] = text.strip()
    if "title" in edits:
        edits["name"] = edits.pop("title")[:100]
    if "note" in edits:
        edits["note"] = edits["note"][:1000]
    return edits


def parse_add(text: str) -> dict:
    """`add: Name | link | month | type` (month and type optional)."""
    parts = [p.strip() for p in text.split("|")]
    name, url = (parts + ["", ""])[:2]
    month = None
    if len(parts) > 2 and parts[2]:
        month = next((n for m, n in MONTHS.items() if m.lower().startswith(parts[2].lower()[:3])), None)
    ptype = next((t for t in board.SECTIONS if len(parts) > 3 and t.lower().startswith(parts[3].lower())),
                 "Fellowship")
    return {"name": name, "url": url, "type": ptype, "who": "All students",
            "deadline_month": month, "women": False, "source": "discord"}


async def read_queue(queue: discord.TextChannel, state: dict, programs: list[dict]) -> tuple[int, bool]:
    """Apply edits and commands. Returns (`more` requests, whether the list changed)."""
    more, changed = 0, False
    if state["last_message_id"] is None:
        # First run: start from now rather than replaying the channel's history.
        last = [m async for m in queue.history(limit=1)]
        state["last_message_id"] = last[0].id if last else 0
        return more, changed

    after = discord.Object(id=state["last_message_id"]) if state["last_message_id"] else None
    async for msg in queue.history(after=after, limit=200, oldest_first=True):
        state["last_message_id"] = msg.id
        if msg.author.bot:
            continue
        if not msg.content:
            log.warning("Message %s has no text: turn on Message Content Intent.", msg.id)
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
        text = msg.content.strip()
        if m := re.match(r"add\s*:\s*(.+)", text, re.IGNORECASE | re.DOTALL):
            program = parse_add(m.group(1))
            errors = problems(programs + [program])
            if errors:
                await msg.reply("Couldn't add that: " + "; ".join(e.split(": ", 1)[-1] for e in errors)
                                + "\nFormat: `add: Name | https://link | Oct | Fellowship` (month and type optional).")
            else:
                programs.append(program)
                changed = True
                await msg.add_reaction("👍")
        elif m := re.match(r"remove\s*:\s*(.+)", text, re.IGNORECASE | re.DOTALL):
            gone = [p for p in programs if key(p["name"]) == key(m.group(1))]
            if gone:
                programs.remove(gone[0])
                changed = True
                await msg.add_reaction("🗑️")
            else:
                await msg.reply(f"No program named **{m.group(1).strip()}** on the board.")
        elif re.match(r"more\b", text, re.IGNORECASE):
            more += 1
            await msg.add_reaction("👀")
    return more, changed


async def decided_by(msg: discord.Message, emoji: str) -> str | None:
    """Display name of the first person (not a bot) who reacted with emoji."""
    for reaction in msg.reactions:
        if str(reaction.emoji) == emoji:
            async for user in reaction.users():
                if not user.bot:
                    return user.display_name
    return None


async def close_decided(queue, channel, state: dict, programs: list[dict], monday: bool) -> bool:
    """Post ✅ drafts, close ❌ ones; on Monday, carry or close unreviewed ones.

    A draft is closed only after its post succeeds, so a refused post is retried
    next run. Returns whether an approved new program was added to the list.
    """
    changed = False
    now = datetime.now(timezone.utc)
    for msg_id, d in list(state["drafts"].items()):
        try:
            msg = await queue.fetch_message(int(msg_id))
        except discord.NotFound:
            del state["drafts"][msg_id]
            continue
        skipped = await decided_by(msg, SKIP)
        who = None if skipped else await decided_by(msg, APPROVE)
        if not who and not skipped:
            if not monday or now - datetime.fromisoformat(d["created"]) < CARRY_AFTER:
                continue
            if not d.get("carried"):
                d["carried"] = True
                await msg.edit(embed=draft_embed(d))
                continue
        if who:
            try:
                await channel.send(embed=post_embed(d))
            except discord.HTTPException as e:
                await queue.send(f"Couldn't post to {channel.mention} ({e.status}: {e.text}). "
                                 "The draft stays open; the next run will try again.")
                raise
            if d.get("new"):
                program = {k: d[k] for k in ("name", "url", "type", "who", "deadline_month", "women")}
                program["source"] = d["source"]
                if not problems(programs + [program]):
                    programs.append(program)
                    changed = True
        await msg.edit(embed=draft_embed(d, "included" if who else "left_out", who))
        state["history"].append({
            "week": datetime.now(TZ).strftime("%G-W%V"), "type": d["type"],
            "new": bool(d.get("new")), "open": bool(d.get("open_url")),
            "decision": "approved" if who else "rejected" if skipped else "ignored",
        })
        del state["drafts"][msg_id]
    return changed


async def post_draft(queue, p: dict, state: dict, requested: str | None = None) -> None:
    d = {k: p.get(k) for k in ("name", "url", "type", "who", "deadline_month", "women",
                               "source", "new", "open_url")}
    d["created"] = datetime.now(timezone.utc).isoformat()
    if requested:
        d["requested"] = requested
    msg = await queue.send(embed=draft_embed(d))
    await msg.add_reaction(APPROVE)
    await msg.add_reaction(SKIP)
    state["drafts"][str(msg.id)] = d
    state["drafted"][key(d["name"])] = d["created"]


async def refresh_board(channel, queue, state: dict, programs: list[dict], now: datetime) -> None:
    embed = board_embed(programs, now)
    msg = None
    if state["board_message_id"]:
        try:
            msg = await channel.fetch_message(state["board_message_id"])
            await msg.edit(embed=embed)
        except discord.NotFound:
            msg = None
    if msg is None:
        msg = await channel.send(embed=embed)
        state["board_message_id"] = msg.id
        try:
            await msg.pin()
        except discord.Forbidden:
            await queue.send(f"Posted the opportunities board in {channel.mention} but couldn't pin it. "
                             "Pin it by hand, or give the bot Pin Messages there.")
    state["board_day"] = now.date().isoformat()


async def run_once() -> None:
    state = load_state()
    programs = json.loads(board.PROGRAMS_PATH.read_text())
    now = datetime.now(TZ)
    week = now.strftime("%G-W%V")
    loop = asyncio.get_running_loop()
    changed = False
    try:
        client = discord.Client(intents=discord.Intents.default())
        async with client:
            await client.login(TOKEN)
            log.info("Logged in as %s.", client.user)
            queue = await client.fetch_channel(QUEUE_CHANNEL_ID)
            channel = await client.fetch_channel(CHANNEL_ID)

            more, edited = await read_queue(queue, state, programs)
            drafting = FORCE == "draft" or (now.weekday() in DRAFT_DAYS and state["drafted_week"] != week)
            changed = await close_decided(queue, channel, state, programs, monday=drafting) or edited

            pool = None
            if drafting or more:
                try:
                    listings = await loop.run_in_executor(None, fetch_simplify, now)
                except Exception as e:
                    log.error("SimplifyJobs fetch failed: %s", e)
                    listings = []
                pool = candidates(state, programs, listings, now)

            if drafting:
                open_now = len(state["drafts"])
                batch = pick(pool, max(0, min(WEEKLY_CANDIDATES, MAX_OPEN_DRAFTS - open_now)))
                carried = (f" {open_now} {'draft is' if open_now == 1 else 'drafts are'} "
                           "still open from last week (above)." if open_now else "")
                await queue.send(f"**This week's opportunity picks.**{carried} {HOW_TO}. "
                                 "Approved ones post to #opportunities within about 2 hours. "
                                 "Post `more` for extra picks, `add: Name | link | month | type` "
                                 "or `remove: Name` to change the board.")
                for p in batch:
                    await post_draft(queue, p, state)
                    pool.remove(p)
                state["drafted_week"] = week

            for _ in range(more):
                batch = pick(pool, DRAFTS_PER_REQUEST)
                if not batch:
                    await queue.send("No more programs due soon or open right now.")
                for p in batch:
                    await post_draft(queue, p, state, requested="more")
                    pool.remove(p)

            if changed or (now.hour >= BOARD_HOUR and state["board_day"] != now.date().isoformat()):
                await refresh_board(channel, queue, state, programs, now)
            log.info("Open drafts: %d.", len(state["drafts"]))
    finally:
        save_state(state)
        if changed:
            save_programs(programs)


if __name__ == "__main__":
    for name, value in (("NEWS_DISCORD_TOKEN", TOKEN),
                        ("OPPS_QUEUE_CHANNEL_ID", QUEUE_CHANNEL_ID),
                        ("OPPS_CHANNEL_ID", CHANNEL_ID)):
        if not value:
            raise SystemExit(f"{name} not set (opportunities/.env locally, repo secret in Actions)")
    asyncio.run(run_once())
