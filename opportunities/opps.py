"""Opportunities bot: a few program picks a week for #opportunities, curated by mods.

  Monday    up to 4 programs are drafted into #opps-queue (mods only): ones
            confirmed open go first, then ones whose usual deadline is this
            month or next.
  Any run   ✅ drafts post to #opportunities right away (deadlines don't wait for
            Friday) and ❌ drafts close. Mods reply to a draft to edit it, or post
            `more`, `add:`, `remove:`, `open:` or `closed:` in the queue.
  Monday    also up to 5 "Is this open?" checks for programs due soon that
            nobody has confirmed: one reaction (✅ open / 🔒 closed / ⏳ not yet)
            updates the board.
  Daily     SimplifyJobs and vanshb03 postings are checked for programs that are
            open right now (and ones no longer listed), and the pinned board
            message in #opportunities is refreshed.

Same bot, token, and run-once design as news-bot/news.py. The program list is
programs.json, which also renders the repo's README board and CLOSED.md
(board.py); this folder's state.json tracks drafts. The workflow commits both.
"""
import os
import re
import json
import asyncio
import logging
import urllib.parse
import urllib.request
from pathlib import Path
from datetime import date, datetime, timedelta, timezone

import discord
from dotenv import load_dotenv

import board
import programs as plist
from programs import key, problems

load_dotenv(Path(__file__).with_name(".env"))

TOKEN = os.getenv("NEWS_DISCORD_TOKEN")   # the same AFG bot as the news feed
QUEUE_CHANNEL_ID = int(os.getenv("OPPS_QUEUE_CHANNEL_ID", "0"))
CHANNEL_ID = int(os.getenv("OPPS_CHANNEL_ID", "0"))
# Manual runs can force Monday's drafting on any day: "draft".
FORCE = os.getenv("OPPS_FORCE", "").strip().lower()

STATE_PATH = Path(__file__).with_name("state.json")
BOARD_URL = f"https://github.com/{os.getenv('GITHUB_REPOSITORY', 'abbykaur/AFG-Opportunities-2027')}#readme"
# Internship lists in the SimplifyJobs format. Next summer's lists also carry
# fellowships and underclassmen programs, and roll over to the next summer in
# August. A program's posting showing up as active here means it's open.
POSTING_SOURCES = {
    "SimplifyJobs": "https://raw.githubusercontent.com/SimplifyJobs/Summer{year}-Internships/dev/.github/scripts/listings.json",
    "vanshb03": "https://raw.githubusercontent.com/vanshb03/Summer{year}-Internships/dev/.github/scripts/listings.json",
}

TZ = board.TZ
DRAFT_DAYS = {0, 1, 2, 3}       # Monday=0; later days only if Monday's run failed
DAILY_HOUR = 6                  # SimplifyJobs check + pinned board, first run after 6 AM PT
WEEKLY_CANDIDATES = 4
MAX_OPEN_DRAFTS = 6             # Monday never leaves more than this many open
DRAFTS_PER_REQUEST = 3
MAX_PER_TYPE = 2                # variety: at most 2 of one type per batch
DUE_WINDOW = board.COMING_UP_MONTHS   # "due soon" = this month or next
REDRAFT_AFTER = timedelta(days=300)   # a program comes back next cycle, not sooner
CARRY_AFTER = timedelta(days=6)       # unreviewed by next Monday: carried once, then closed
RECHECK_AFTER = timedelta(days=14)    # re-stamp a still-open program's check date this often
HISTORY_WEEKS = 8
VERIFY_PER_WEEK = 5                   # Monday "Is this open?" checks
VERIFY_EXPIRE = timedelta(days=7)     # an unanswered check closes after this
RECHECK_STATUS_AFTER = timedelta(days=14)  # don't ask about a program checked more recently
WOMEN_BOOST = {"only": 1.5, "focused": 1.25}
WOMEN_LABEL = {"only": "💜 Women only", "focused": "💗 Women-focused"}
CHECK_OPEN, CHECK_CLOSED, CHECK_NOT_YET = "✅", "🔒", "⏳"
STATUS_TEXT = {"open": "✅ Open", "upcoming": "⏳ Not open yet", "unconfirmed": "❓ Not confirmed", "closed": "🔒 Closed"}
DATE_IN_TEXT = re.compile(r"\d{4}-\d{1,2}-\d{1,2}|\d{1,2}/\d{1,2}(?:/\d{2,4})?|"
                          r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.? \d{1,2}(?:st|nd|rd|th)?(?:,? \d{4})?",
                          re.IGNORECASE)

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
COMMANDS = ("`more` · `add: Name | link | month | type | women` · `remove: Name` · "
            "`open: Name | deadline | apply link` · `closed: Name` · `upcoming: Name` · "
            "`women: Name | only/focused/none`")

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
    state.setdefault("board_day", None)      # last day the daily step ran
    state.setdefault("checks", {})           # verify message id -> {"name", "url", "created", ...}
    # How each closed draft went, by type only (the repo is public: no names).
    state.setdefault("history", [])
    return state


def save_state(state: dict) -> None:
    STATE_PATH.write_text(json.dumps(state, indent=2, ensure_ascii=False) + "\n")


def save_programs(programs: list[dict], now: datetime) -> None:
    """Write the list and re-render both board pages."""
    plist.refresh(programs, now.date())
    plist.save(programs)
    board.README_PATH.write_text(board.render(programs, now))
    board.CLOSED_PATH.write_text(board.render_closed(programs, now))


def fetch_postings(now: datetime) -> list[dict]:
    """Active listings from every POSTING_SOURCES list, deduplicated by link.
    A source that fails is skipped, so one outage doesn't stop the others."""
    year = now.year + 1 if now.month >= 8 else now.year
    out, seen = [], set()
    for source, url in POSTING_SOURCES.items():
        try:
            req = urllib.request.Request(url.format(year=year), headers={"User-Agent": "afg-opps-bot"})
            listings = json.load(urllib.request.urlopen(req, timeout=60))
        except Exception as e:
            log.error("%s postings fetch failed: %s", source, e)
            continue
        for j in listings:
            if j.get("active") and j.get("is_visible", True) and j.get("url") and j["url"] not in seen:
                seen.add(j["url"])
                out.append({"company": j.get("company_name", ""), "title": j.get("title", ""), "url": j["url"]})
    return out


# Lists whose links go through their own shortener; the board stores the
# company's real application link instead.
SHORTLINK_HOSTS = {"go.vansh.nyc"}


def resolve_link(url: str) -> str:
    """The final application link behind a list's shortener (unchanged otherwise)."""
    if urllib.parse.urlparse(url).hostname not in SHORTLINK_HOSTS:
        return url
    for method in ("HEAD", "GET"):
        try:
            req = urllib.request.Request(url, method=method, headers={"User-Agent": "Mozilla/5.0"})
            return urllib.request.urlopen(req, timeout=20).geturl()
        except Exception:
            continue
    return url


def is_program_like(listing: dict) -> bool:
    return bool(PROGRAM_RE.search(listing["title"]) and not SKIP_RE.search(listing["title"]))


def same_company(listing: dict, p: dict) -> bool:
    company = key(listing["company"])
    if p.get("company"):
        return key(p["company"]) in company
    return len(company) >= 3 and company in key(p["name"])


def match_program(listing: dict, programs: list[dict]) -> dict | None:
    """The known program a listing is a posting for.

    A program with a `match` phrase ("First Play", "Ignite") matches its
    company's listings whose title has that phrase: precise, and works for any
    title. Without one, only program-like titles are considered, and the
    program's name must share a distinctive word with the title.
    """
    title = listing["title"]
    for p in programs:
        if p.get("match") and same_company(listing, p) and any(
                re.search(r"\b" + re.escape(phrase.strip()).replace(r"\ ", r"[\s-]+") + r"\b", title, re.I)
                for phrase in p["match"].split("|")):
            return p
    if not is_program_like(listing):
        return None
    title_words = set(re.findall(r"[a-z]{4,}", title.lower())) - {"intern", "internship", "program", "software", "engineer"}
    for p in programs:
        if not p.get("match") and same_company(listing, p) and (
                not title_words or any(w in p["name"].lower() for w in title_words)):
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


def sync_postings(programs: list[dict], listings: list[dict], now: datetime) -> tuple[bool, list[dict]]:
    """Record which programs have an open posting, and collect program-like
    postings the board doesn't have yet. Returns (whether the list changed, new programs).

    A known program with an active posting is marked open (with its
    application link). One the bot marked open from postings before, but whose
    posting is gone, is marked closed. Statuses mods or reports set aren't
    touched by the second rule, since the posting lists only cover some programs.
    """
    today = now.date()
    changed, listed, new = False, set(), {}
    for listing in listings:
        p = match_program(listing, programs)
        if p:
            if key(p["name"]) in listed:
                continue   # a second posting for the same program (e.g. another city)
            listed.add(key(p["name"]))
            apply_url = resolve_link(listing["url"])
            checked = date.fromisoformat(p["checked"]) if p.get("checked") else None
            if (p["status"] != "open" or p.get("apply_url") != apply_url
                    or not checked or today - checked > RECHECK_AFTER):
                plist.set_status(p, "open", via="postings", on=today, apply_url=apply_url)
                changed = True
            continue
        if not is_program_like(listing):
            continue
        title, company = listing["title"], listing["company"]
        # "Anthropic Fellows Program", not "Anthropic Anthropic Fellows Program".
        name = (title if key(company) in key(title) else f"{company} {title}").strip()[:100]
        if key(name) in new:
            continue
        apply_url = resolve_link(listing["url"])
        new[key(name)] = (plist.new_program(
            name=name, url=apply_url, type=guess_type(title), who=guess_who(title),
            women="focused" if re.search(r"\bwomen\b", title, re.I) else None, source="SimplifyJobs",
            status="open", checked=today.isoformat(), via="postings", apply_url=apply_url, new=True))
    if listings:   # an empty fetch says nothing about what closed
        for p in programs:
            if p["status"] == "open" and p.get("via") in ("postings", "simplify") and key(p["name"]) not in listed:
                plist.set_status(p, "closed", via="postings", on=today)
                changed = True
    return changed, list(new.values())


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


def candidates(state: dict, programs: list[dict], new: list[dict], now: datetime) -> list[dict]:
    """Programs worth drafting now, best first.

    Known programs qualify when they're open, or due this month or next and not
    closed. Programs SimplifyJobs lists that the board doesn't have qualify as
    "new". Each is scored by type weight, with open and women-focused programs
    boosted and later deadlines discounted. A program drafted in the last
    REDRAFT_AFTER days (or open in the queue) is skipped.
    """
    recent = {k for k, t in state["drafted"].items()
              if now - datetime.fromisoformat(t) < REDRAFT_AFTER}
    recent |= {key(d["name"]) for d in state["drafts"].values()}
    pool = [p for p in programs if p["status"] == "open" or (
        p["status"] != "closed" and board.months_until(p["deadline_month"], now) < DUE_WINDOW)]
    pool = [p for p in pool + new if key(p["name"]) not in recent]

    weights = type_weights(state)
    def score(p):
        # Open counts as due now. Programs not on the board yet are half-weighted,
        # since SimplifyJobs' program titles are noisier.
        is_open = p["status"] == "open"
        months = 0 if is_open else min(board.months_until(p["deadline_month"], now), 3)
        return (weights.get(p["type"], 0.25) * (2 if is_open else 1) * (0.5 if p.get("new") else 1)
                * WOMEN_BOOST.get(p.get("women"), 1) / (1 + months))
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


def due(d: dict) -> str:
    """'Dec 15' for a known deadline, else the usual month."""
    if d.get("deadline"):
        return board.short(d["deadline"])
    return board.when(d.get("deadline_month")).replace("Check site", "check the page")


def draft_embed(d: dict, status: str = "pending", by: str | None = None) -> discord.Embed:
    if status == "pending" and d.get("carried"):
        status = "carried"
    color = {"pending": COLOR_PENDING, "carried": COLOR_PENDING, "included": COLOR_IN}.get(status, COLOR_OUT)
    embed = discord.Embed(title=d["name"][:256], url=d["url"], color=color,
                          description=d.get("note") or "*(reply with note: to add a line that posts with it)*")
    embed.add_field(name="Type", value=d["type"])
    embed.add_field(name="For", value=d["who"])
    embed.add_field(name="Deadline" if d.get("deadline") else "Usually due", value=due(d))
    if d.get("status") == "open":
        how = f"[apply here]({d['apply_url']})" if d.get("apply_url") else f"checked {board.short(d['checked'])}"
        embed.add_field(name="✅ Open now", value=how, inline=False)
    else:
        # The board's months are rough (from community lists), so a pick that
        # isn't confirmed open may be closed or not open yet.
        embed.add_field(name="⚠️ Not confirmed open",
                        value=f"Usual timing only. Check [the page]({d['url']}) before {APPROVE}. "
                              "If it's open, `open: Name | deadline` marks it on the board.", inline=False)
    if d.get("new"):
        embed.add_field(name="New", value="Not on the board yet; approving adds it.", inline=False)
    if d.get("women"):
        embed.add_field(name="Who can apply", value=WOMEN_LABEL[d["women"]])
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
    lines = ([f"**{WOMEN_LABEL[d['women']]}**"] if d.get("women") else []) + ([d["note"]] if d.get("note") else [])
    if d.get("status") == "open" and d.get("apply_url"):
        lines.append(f"**[Apply here]({d['apply_url']})**")
    lines.append(f"[📋 All opportunities]({BOARD_URL})")
    embed = discord.Embed(title=d["name"][:256], url=d["url"], color=COLOR_POST,
                          description="\n\n".join(lines))
    embed.add_field(name="For", value=d["who"])
    embed.add_field(name="Type", value=d["type"])
    embed.add_field(name="Deadline" if d.get("deadline") else "Usually due", value=due(d))
    return embed


def board_embed(programs: list[dict], now: datetime) -> discord.Embed:
    """The pinned summary: what's open and coming up, with a link to the full board."""
    today = now.date()
    active = sorted((p for p in programs if p["status"] != "closed"), key=lambda p: board.sort_key(p, today))
    open_now = [p for p in active if p["status"] == "open"]
    soon = [p for p in active if p["status"] != "open"
            and board.months_until(p["deadline_month"], today) < DUE_WINDOW]
    line = lambda p: (f"• [{p['name']}]({p.get('apply_url') or p['url']}) · {p['who']} · {due(p)}"
                      + (f" · {WOMEN_LABEL[p['women']]}" if p.get("women") else ""))
    footer = f"\n[📋 See all {len(active)} programs →]({BOARD_URL})"
    text = ""
    for title, group in (("**✅ Open now**", open_now), ("**🔥 Coming up** (not confirmed, check each page)", soon)):
        if not group:
            continue
        text += f"{title}\n"
        for p in group:
            if len(text) + len(line(p)) + len(footer) > 3900:
                text += "• …and more on the full board\n"
                break
            text += line(p) + "\n"
        text += "\n"
    embed = discord.Embed(title="📌 Opportunities board", url=BOARD_URL, color=COLOR_POST,
                          description=(text or "Nothing open or coming up right now.\n") + footer)
    embed.set_footer(text="💜 women only · 💗 women-focused · updated daily")
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


def parse_women(text: str) -> tuple[str | None, bool]:
    """'only' / 'women only' / 'focused' / 'none' -> (label, whether it was understood)."""
    t = text.strip().lower()
    if t in ("", "none", "no", "everyone"):
        return None, True
    if "only" in t:
        return "only", True
    if "focus" in t:
        return "focused", True
    return None, False


def parse_add(text: str) -> dict:
    """`add: Name | link | month | type | women` (month, type, and women optional)."""
    parts = [p.strip() for p in text.split("|")]
    name, url = (parts + ["", ""])[:2]
    month = None
    if len(parts) > 2 and parts[2]:
        month = next((n for m, n in plist.MONTHS.items() if m.lower().startswith(parts[2].lower()[:3])), None)
    ptype = next((t for t in board.SECTIONS if len(parts) > 3 and t.lower().startswith(parts[3].lower())),
                 "Fellowship")
    women = parse_women(parts[4])[0] if len(parts) > 4 else None
    return plist.new_program(name=name, url=url, type=ptype, deadline_month=month, women=women, source="discord")


def apply_status_command(programs: list[dict], verb: str, args: str, now: datetime) -> str | None:
    """`open: Name | deadline | apply link`, `upcoming: Name | deadline`, `closed: Name`,
    or `women: Name | only/focused/none`. Returns a problem, or None."""
    parts = [p.strip() for p in args.split("|")]
    p = plist.find(programs, parts[0])
    if not p:
        return f"No single program on the board matches **{parts[0]}**. Use its name as shown on the board."
    if verb == "women":
        label, ok = parse_women(parts[1] if len(parts) > 1 else "")
        if not ok:
            return "Use `women: Name | only`, `women: Name | focused`, or `women: Name | none`."
        p["women"] = label
        return None
    if verb == "closed":
        plist.set_status(p, "closed", via="mod", on=now.date())
        return None
    deadline = None
    if len(parts) > 1 and parts[1]:
        deadline = plist.parse_date(parts[1], now.date())
        if not deadline:
            return f"Couldn't read the deadline **{parts[1]}**. Try `Dec 15` or `2026-12-15`."
    apply_url = parts[2] if len(parts) > 2 and parts[2] else None
    plist.set_status(p, "open" if verb == "open" else "upcoming", deadline, via="mod", on=now.date(),
                     apply_url=apply_url)
    errors = problems(programs)
    return "; ".join(e.split(": ", 1)[-1] for e in errors) or None


def verify_candidates(state: dict, programs: list[dict], now: datetime) -> list[dict]:
    """Programs worth asking mods about this week, most urgent first.

    Ones due this month or next (or with a known deadline) that nobody has
    checked in RECHECK_STATUS_AFTER, plus stale "open" or "not open yet"
    statuses. Programs a posting already confirms open, closed ones, and ones
    already in the queue (as a draft or a check) are skipped.
    """
    today = now.date()
    asked = {c["name"] for c in state["checks"].values()} | {d["name"] for d in state["drafts"].values()}
    pool = []
    for p in programs:
        checked = date.fromisoformat(p["checked"]) if p.get("checked") else None
        if (p["status"] == "closed" or p["name"] in asked
                or (p["status"] == "open" and p.get("via") in ("postings", "simplify"))
                or (checked and today - checked < RECHECK_STATUS_AFTER)):
            continue
        if (p["status"] == "unconfirmed" and not p.get("deadline")
                and board.months_until(p["deadline_month"], today) >= DUE_WINDOW):
            continue
        pool.append(p)
    pool.sort(key=lambda p: (board.months_until(p["deadline_month"], today),
                             -WOMEN_BOOST.get(p.get("women"), 1), p["name"].lower()))
    return pool[:VERIFY_PER_WEEK]


def check_embed(p: dict, answer: str | None = None, by: str | None = None) -> discord.Embed:
    """An "Is this open?" check, or its answer."""
    if answer is None:
        embed = discord.Embed(
            title=f"🔎 Is this open? {p['name']}"[:256], url=p["url"], color=COLOR_PENDING,
            description=(f"Check [the program's page]({p['url']}) and react: {CHECK_OPEN} open · "
                         f"{CHECK_CLOSED} closed this cycle · {CHECK_NOT_YET} not open yet.\n"
                         "Reply with the deadline (e.g. `Dec 15`) or the application link if you have them."))
        embed.add_field(name="For", value=board.who(p))
        embed.add_field(name="Deadline" if p.get("deadline") else "Usually due", value=due(p))
        embed.add_field(name="Board says", value=STATUS_TEXT[p["status"]])
        embed.set_footer(text="One reaction updates the board. Unanswered checks close after a week.")
        return embed
    words = {"open": "open", "closed": "closed this cycle", "upcoming": "not open yet", "expired": None}
    embed = discord.Embed(title=f"🔎 {p['name']}"[:256], url=p["url"],
                          color=COLOR_OUT if answer == "expired" else COLOR_IN,
                          description=(f"Marked **{words[answer]}** by {by}. The board is updated."
                                       if words[answer] else "No answer this week, so it goes back in the pool for a future check."))
    return embed


async def post_checks(queue, state: dict, programs: list[dict], now: datetime) -> None:
    batch = verify_candidates(state, programs, now)
    if not batch:
        return
    await queue.send(f"**Is this open?** {len(batch)} quick {'check' if len(batch) == 1 else 'checks'} "
                     "for programs due soon. One reaction each keeps the public board accurate.")
    for p in batch:
        msg = await queue.send(embed=check_embed(p))
        for emoji in (CHECK_OPEN, CHECK_CLOSED, CHECK_NOT_YET):
            await msg.add_reaction(emoji)
        state["checks"][str(msg.id)] = {"name": p["name"], "url": p["url"],
                                        "created": datetime.now(timezone.utc).isoformat()}


async def resolve_checks(queue, state: dict, programs: list[dict], now: datetime) -> bool:
    """Apply answered checks to the list; close ones unanswered for VERIFY_EXPIRE.
    Returns whether the list changed."""
    changed = False
    for msg_id, check in list(state["checks"].items()):
        try:
            msg = await queue.fetch_message(int(msg_id))
        except discord.NotFound:
            del state["checks"][msg_id]
            continue
        p = plist.find(programs, check["name"])
        # Most cautious answer wins if mods disagree.
        answer, by = None, None
        for emoji, status in ((CHECK_CLOSED, "closed"), (CHECK_NOT_YET, "upcoming"), (CHECK_OPEN, "open")):
            by = await decided_by(msg, emoji)
            if by:
                answer = status
                break
        if answer and p:
            deadline = date.fromisoformat(check["deadline"]) if check.get("deadline") else None
            plist.set_status(p, answer, deadline if answer != "closed" else None, via="mod",
                             on=now.date(), apply_url=check.get("apply_url"))
            changed = True
        elif not answer and now - datetime.fromisoformat(check["created"]) < VERIFY_EXPIRE:
            continue
        # The program may have been removed (`remove:`) while the check was open.
        await msg.edit(embed=check_embed(p or {"name": check["name"], "url": check.get("url")},
                                         answer or "expired", by))
        del state["checks"][msg_id]
    return changed


async def read_queue(queue: discord.TextChannel, state: dict, programs: list[dict],
                     now: datetime) -> tuple[int, bool]:
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
        if ref and str(ref) in state["checks"]:
            check = state["checks"][str(ref)]
            url = re.search(r"https?://[^\s|()<>]+", msg.content)
            when_ = next((d for m in DATE_IN_TEXT.finditer(re.sub(r"https?://\S+", "", msg.content))
                          if (d := plist.parse_date(m.group(0), now.date()))), None)
            if url:
                check["apply_url"] = url.group(0)
            if when_:
                check["deadline"] = when_.isoformat()
            await msg.add_reaction("✏️" if url or when_ else "❓")
            continue
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
            gone = plist.find(programs, m.group(1))
            if gone:
                programs.remove(gone)
                changed = True
                await msg.add_reaction("🗑️")
            else:
                await msg.reply(f"No single program on the board matches **{m.group(1).strip()}**.")
        elif m := re.match(r"(open|closed?|upcoming|women)\s*:\s*(.+)", text, re.IGNORECASE | re.DOTALL):
            verb = {"close": "closed"}.get(m.group(1).lower(), m.group(1).lower())
            problem = apply_status_command(programs, verb, m.group(2), now)
            if problem:
                await msg.reply(problem)
            else:
                changed = True
                await msg.add_reaction("👍")
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
            # Post with the program's latest status (e.g. a mod's `open:` since drafting).
            current = plist.find(programs, d["name"])
            if current:
                d.update({k: current.get(k) for k in ("status", "deadline", "apply_url", "checked")})
            try:
                await channel.send(embed=post_embed(d))
            except discord.HTTPException as e:
                await queue.send(f"Couldn't post to {channel.mention} ({e.status}: {e.text}). "
                                 "The draft stays open; the next run will try again.")
                raise
            if d.get("new") and not current:
                program = plist.new_program(**{k: d[k] for k in (
                    "name", "url", "type", "who", "deadline_month", "women", "source",
                    "status", "deadline", "checked", "apply_url", "via") if d.get(k) is not None})
                if not problems(programs + [program]):
                    programs.append(program)
                    changed = True
        await msg.edit(embed=draft_embed(d, "included" if who else "left_out", who))
        state["history"].append({
            "week": datetime.now(TZ).strftime("%G-W%V"), "type": d["type"],
            "new": bool(d.get("new")), "open": d.get("status") == "open",
            "decision": "approved" if who else "rejected" if skipped else "ignored",
        })
        del state["drafts"][msg_id]
    return changed


async def post_draft(queue, p: dict, state: dict, requested: str | None = None) -> None:
    d = {k: p.get(k) for k in ("name", "url", "type", "who", "deadline_month", "women", "source",
                               "status", "deadline", "checked", "apply_url", "via", "new")}
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


async def run_once() -> None:
    state = load_state()
    programs = json.loads(board.PROGRAMS_PATH.read_text())
    now = datetime.now(TZ)
    week = now.strftime("%G-W%V")
    loop = asyncio.get_running_loop()
    changed = plist.refresh(programs, now.date())
    try:
        client = discord.Client(intents=discord.Intents.default())
        async with client:
            await client.login(TOKEN)
            log.info("Logged in as %s.", client.user)
            queue = await client.fetch_channel(QUEUE_CHANNEL_ID)
            channel = await client.fetch_channel(CHANNEL_ID)

            more, edited = await read_queue(queue, state, programs, now)
            changed |= edited
            drafting = FORCE == "draft" or (now.weekday() in DRAFT_DAYS and state["drafted_week"] != week)
            daily = now.hour >= DAILY_HOUR and state["board_day"] != now.date().isoformat()
            changed |= await close_decided(queue, channel, state, programs, monday=drafting)
            changed |= await resolve_checks(queue, state, programs, now)

            new = []
            if drafting or more or daily:
                listings = await loop.run_in_executor(None, fetch_postings, now)
                synced, new = sync_postings(programs, listings, now)
                changed |= synced
            pool = candidates(state, programs, new, now) if drafting or more else []

            if drafting:
                open_now = len(state["drafts"])
                batch = pick(pool, max(0, min(WEEKLY_CANDIDATES, MAX_OPEN_DRAFTS - open_now)))
                carried = (f" {open_now} {'draft is' if open_now == 1 else 'drafts are'} "
                           "still open from last week (above)." if open_now else "")
                await queue.send(f"**This week's opportunity picks.**{carried} {HOW_TO}. "
                                 "Approved ones post to #opportunities within about 2 hours. "
                                 f"Commands: {COMMANDS}.")
                for p in batch:
                    await post_draft(queue, p, state)
                    pool.remove(p)
                await post_checks(queue, state, programs, now)
                state["drafted_week"] = week

            for _ in range(more):
                batch = pick(pool, DRAFTS_PER_REQUEST)
                if not batch:
                    await queue.send("No more programs open or due soon.")
                for p in batch:
                    await post_draft(queue, p, state, requested="more")
                    pool.remove(p)

            if changed or daily:
                await refresh_board(channel, queue, state, programs, now)
                state["board_day"] = now.date().isoformat() if daily else state["board_day"]
            log.info("Open drafts: %d.", len(state["drafts"]))
    finally:
        save_state(state)
        if changed:
            save_programs(programs, now)


if __name__ == "__main__":
    for name, value in (("NEWS_DISCORD_TOKEN", TOKEN),
                        ("OPPS_QUEUE_CHANNEL_ID", QUEUE_CHANNEL_ID),
                        ("OPPS_CHANNEL_ID", CHANNEL_ID)):
        if not value:
            raise SystemExit(f"{name} not set (opportunities/.env locally, repo secret in Actions)")
    asyncio.run(run_once())
