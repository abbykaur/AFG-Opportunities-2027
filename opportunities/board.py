"""Render the public opportunities board from programs.json.

    python opportunities/board.py

Writes README.md (open and upcoming programs: women's programs first, then by
category) and CLOSED.md (programs not accepting applications until their next
cycle). First it ages statuses (programs.refresh: passed deadlines close,
closed programs come back for their next cycle), so the daily workflow keeps
both pages honest.
"""
import calendar
import json
import os
import re
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import quote
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
PROGRAMS_PATH = ROOT / "opportunities" / "programs.json"
README_PATH = ROOT / "README.md"
CLOSED_PATH = ROOT / "CLOSED.md"
TZ = ZoneInfo("America/Los_Angeles")

REPO = os.getenv("GITHUB_REPOSITORY", "abbykaur/AFG-Opportunities-2027")
INVITE_URL = "https://discord.gg/6BBkjMJURt"
# Relative links, so they survive a repo rename.
SUGGEST_URL = "../../issues/new?template=suggest-program.yml"
REPORT_URL = "../../issues/new?template=report-program.yml"
# How many months ahead "Coming up" looks, counting the current month.
COMING_UP_MONTHS = 2
NEW_FOR = timedelta(days=14)         # 🆕 marker
CLOSING_SOON = timedelta(days=7)     # 🔥 marker on open programs
WOMEN_LABEL = {"only": "💜 Women only", "focused": "💗 Women-focused"}
# Section heading for each program type, in board order. Types missing here
# are added at the end under their own name.
SECTIONS = {
    "Underclassmen program": "🎓 Underclassmen programs",
    "Fellowship": "🌱 Fellowships",
    "Insight / exploratory": "🔍 Insight & exploratory programs",
    "Scholarship": "💰 Scholarships",
    "Conference scholarship": "🎤 Conference scholarships",
    "Research program": "🔬 Research programs",
    "Mentorship": "🤝 Mentorship",
}
SOURCES = {
    "LuisaE/opportunities": "https://github.com/LuisaE/opportunities",
    "zapplyjobs/underclassmen-internships": "https://github.com/zapplyjobs/underclassmen-internships",
}


def months_until(month: int | None, now: datetime | date) -> int:
    """0 for this month, 11 for last month; programs with no month sort last."""
    return 99 if month is None else (month - now.month) % 12


def when(month: int | None) -> str:
    return f"~{calendar.month_abbr[month]}" if month else "Check site"


def short(iso: str | None) -> str:
    """'2026-12-15' -> 'Dec 15'."""
    if not iso:
        return ""
    d = date.fromisoformat(iso)
    return f"{calendar.month_abbr[d.month]} {d.day}"


def deadline(p: dict) -> str:
    """An exact deadline when known, else the usual month."""
    return f"**{short(p['deadline'])}**" if p.get("deadline") else when(p["deadline_month"]).replace("Check site", "—")


def who(p: dict) -> str:
    """Who can apply, with the women label first so it's easy to spot (and Ctrl+F)."""
    label = WOMEN_LABEL.get(p.get("women"))
    return f"{label} · {p['who']}" if label else p["who"]


def report_link(p: dict) -> str:
    """The report form with this program's name already filled in."""
    return f"{REPORT_URL}&name={quote(p['name'], safe='')}"


def closing_soon(p: dict, today: date) -> bool:
    return bool(p["status"] == "open" and p.get("deadline")
                and date.fromisoformat(p["deadline"]) - today <= CLOSING_SOON)


def status(p: dict, today: date) -> str:
    if p["status"] == "open":
        icon = "🔥" if closing_soon(p, today) else "✅"
        return f"{icon} [Apply]({p['apply_url']})" if p.get("apply_url") else f"{icon} Open"
    if p["status"] == "upcoming":
        return "⏳ Not open yet"
    if p["status"] == "closed":
        return "🔒 Closed"
    return f"❓ [Update]({report_link(p)})"


def name_cell(p: dict, today: date) -> str:
    new = (p.get("added") and p.get("source") not in SOURCES
           and today - date.fromisoformat(p["added"]) <= NEW_FOR)
    return f"[{p['name']}]({p['url']})" + (" 🆕" if new else "")


def anchor(heading: str) -> str:
    """GitHub's heading anchor: lowercase, punctuation and emoji dropped, spaces to hyphens."""
    return "#" + re.sub(r"[^\w\- ]", "", heading.lower()).replace(" ", "-")


def table(headers: list[str], rows: list[list[str]]) -> list[str]:
    return (["| " + " | ".join(headers) + " |", "|" + "---|" * len(headers)]
            + ["| " + " | ".join(r) + " |" for r in rows])


STATUS_ORDER = {"open": 0, "upcoming": 1, "unconfirmed": 2, "closed": 3}


def sort_key(p: dict, today: date):
    """Open first, then not-open-yet, then by exact deadline, usual month, and name."""
    exact = date.fromisoformat(p["deadline"]) if p.get("deadline") else None
    return (STATUS_ORDER[p["status"]], exact or date.max, months_until(p["deadline_month"], today),
            p["name"].lower())


FAQ = [
    ("Is this list only for women?",
     "No. Most programs here are open to everyone, and the list is built for anyone starting out in tech and AI. "
     "We put women first: programs made for women are marked 💜 (women only, or women and gender minorities) "
     "or 💗 (open to everyone, but built to support women), and they're collected in the "
     "[💜 For women](#-for-women) section."),
    ("What's on this list?",
     "Programs for students early in college: underclassmen internships and programs, fellowships, "
     "insight days, scholarships, conference scholarships, research programs, and mentorship. "
     "Regular internships and new-grad jobs aren't here; for those, see "
     "[SimplifyJobs](https://github.com/SimplifyJobs)."),
    ("How do you know what's open?",
     "✅ and ⏳ mean someone checked recently: an AFG mod, a community report, or the program's posting showing up "
     "as open on [SimplifyJobs](https://github.com/SimplifyJobs/Summer2027-Internships) or "
     "[vanshb03's list](https://github.com/vanshb03/Summer2027-Internships). The **Checked** column shows when. "
     "Deadlines that pass close automatically, and a status nobody re-checks goes back to ❓ (after 45 days "
     "for ✅, 30 for ⏳). ❓ means we only know the usual timing from past years, so check the program's page, "
     "and if you find out, click **Update**."),
    ("Where did the closed programs go?",
     "To [CLOSED.md](CLOSED.md). Most programs run every year, so they come back here when their next "
     "cycle gets close."),
    ("How can I help?",
     f"[Suggest a program]({SUGGEST_URL}) we're missing, or [report one]({REPORT_URL}) that's open, closed, "
     "not open yet, or has the wrong date. Both are short forms (a free GitHub account is needed). A maintainer "
     "reviews each one, and approved changes show up here automatically. Starring the repo helps other students "
     "find it."),
    ("Who runs this?",
     f"[AI First Girls]({INVITE_URL}), a community for women in AI and tech. Come say hi on Discord, where "
     "mods share picks from this list in #opportunities every week."),
]


def render(programs: list[dict], now: datetime) -> str:
    today = now.date() if isinstance(now, datetime) else now
    active = sorted((p for p in programs if p["status"] != "closed"), key=lambda p: sort_key(p, today))
    closed = [p for p in programs if p["status"] == "closed"]
    open_now = [p for p in active if p["status"] == "open"]
    women = [p for p in active if p.get("women")]
    soon = [p for p in active if p["status"] != "open"
            and months_until(p["deadline_month"], today) < COMING_UP_MONTHS]
    checked_recently = sum(1 for p in programs if p.get("checked")
                           and today - date.fromisoformat(p["checked"]) <= timedelta(days=30))
    types = [t for t in list(SECTIONS) + sorted({p["type"] for p in programs} - set(SECTIONS))
             if any(p["type"] == t for p in active)]
    end_month = calendar.month_name[(today.month + COMING_UP_MONTHS - 2) % 12 + 1]
    badge = lambda label, value, color, link: (
        f"[![{label}](https://img.shields.io/badge/{quote(label.replace(' ', '_'))}-{value}-{color})]({link})")

    out = [
        '<div align="center">',
        "",
        "# 💜 AFG Opportunities 2027",
        "",
        "### Fellowships, freshman & sophomore programs, and scholarships for women in tech & AI",
        "",
        f"[![Stars](https://img.shields.io/github/stars/{REPO}?style=flat&label=Stars&color=8b5cf6)]"
        f"(https://github.com/{REPO}/stargazers) "
        f"[![Updated](https://img.shields.io/github/last-commit/{REPO}?label=Updated&color=22c55e)]"
        f"(https://github.com/{REPO}/commits) "
        + badge("Open now", len(open_now), "22c55e", "#-open-now") + " "
        + badge("For women", len(women), "a855f7", "#-for-women") + " "
        f"[![Discord](https://img.shields.io/badge/Discord-AI_First_Girls-5865F2?logo=discord&logoColor=white)]({INVITE_URL})",
        "",
        "</div>",
        "",
        '<img src="opportunities/assets/banner.svg" alt="AFG Opportunities 2027: fellowships, freshman and sophomore '
        'programs, and scholarships for women in tech and AI" width="100%">',
        "",
        "> [!NOTE]",
        f"> **Built by [AI First Girls]({INVITE_URL})**, a community for women in AI and tech. We track early-career "
        "opportunities for students starting out (fellowships, underclassmen programs, insight days, scholarships) "
        "and mark the ones made for women, so you can find them fast. Most are open to everyone.",
        "",
        f"**Contribute in 60 seconds:** [➕ Suggest a program]({SUGGEST_URL}) · "
        f"[🔁 Report one that's open, closed, or wrong]({REPORT_URL}) · or post it in **#opportunities** "
        f"on [Discord]({INVITE_URL}).",
        "",
        f"**{len(open_now)} open now** · **{len(women)} for women** · {checked_recently} checked in the last "
        f"30 days · updated {today:%B %Y}",
        "",
        "---",
        "",
        f"### Browse {len(active)} programs",
        "",
        f"💜 **[For women](#-for-women)** ({len(women)})",
        "",
    ]
    for t in types:
        group = [p for p in active if p["type"] == t]
        n_open = sum(p["status"] == "open" for p in group)
        heading = SECTIONS.get(t, t)
        icon, label = heading.split(" ", 1) if " " in heading else ("", heading)
        out += [f"{icon} **[{label}]({anchor(heading)})** "
                f"({len(group)}{f' · ✅ {n_open} open' if n_open else ''})", ""]
    out += [
        "---",
        "",
        "## Legend",
        "",
        "- ✅ Open, checked recently (**Apply** goes straight to the application) · 🔥 Open, closes within 7 days",
        "- ⏳ Checked: not open yet · ❓ Not confirmed: if you know, click **Update**",
        "- 💜 Women only (or women and gender minorities) · 💗 Women-focused: open to everyone, built to support women",
        "- 🆕 Added in the last 2 weeks · **Bold dates** are confirmed deadlines; `~Nov` means it usually opens "
        "or closes around then",
        "",
        f"> 🔒 Programs that are closed until their next cycle are in **[CLOSED.md](CLOSED.md)** ({len(closed)}).",
        "",
        "---",
        "",
        "## ✅ Open now",
        "",
    ]
    if open_now:
        out += table(["Program", "For", "Type", "Apply", "Deadline"],
                     [[name_cell(p, today), who(p), p["type"], status(p, today), deadline(p)] for p in open_now])
    else:
        out.append(f"Nothing confirmed open right now. Check **Coming up** below, or [report one]({REPORT_URL}) that's open.")
    out += ["", "## 💜 For women", "",
            "Programs made for women. Everything else on this board is open to women too.", ""]
    out += (table(["Program", "For", "Type", "Status", "Deadline"],
                  [[name_cell(p, today), who(p), p["type"], status(p, today), deadline(p)] for p in women])
            if women else [f"None right now. Know one? [Suggest it]({SUGGEST_URL})."])
    out += ["", f"## 🔥 Coming up ({calendar.month_name[today.month]}"
            f"{'' if COMING_UP_MONTHS == 1 else ' – ' + end_month})", "",
            "These usually open or close around now. Unless marked ⏳, nobody has confirmed them yet, "
            "so check each page.", ""]
    out += (table(["Program", "For", "Type", "Status", "Usually"],
                  [[name_cell(p, today), who(p), p["type"], status(p, today), deadline(p)] for p in soon])
            if soon else ["Nothing else due in the next couple of months."])
    for t in types:
        group = [p for p in active if p["type"] == t]
        out += ["", f"## {SECTIONS.get(t, t)}", ""]
        out += table(["Program", "For", "Status", "Deadline", "Checked"],
                     [[name_cell(p, today), who(p), status(p, today), deadline(p), short(p.get("checked")) or "—"]
                      for p in group])
    out += ["", "## FAQ", ""]
    for q, a in FAQ:
        out += ["<details>", f"<summary><strong>{q}</strong></summary>", "", a, "", "</details>", ""]
    out += [
        "## Credits",
        "",
        "The list started from " + " and ".join(f"[{name}]({url})" for name, url in SOURCES.items())
        + " (data by Zapply). Open-status checks use postings from "
        "[SimplifyJobs](https://github.com/SimplifyJobs/Summer2027-Internships) and "
        "[vanshb03](https://github.com/vanshb03/Summer2027-Internships). Thank you!",
        "",
        "---",
        "",
        # Month only, so the daily run doesn't commit a new README every day.
        f"<sub>{len(programs)} programs · updated {today:%B %Y}. This board updates itself daily. "
        "The repo also runs AFG's Discord bots: [jobs bot setup](DEPLOY.md) · [news bot](news-bot/README.md) · "
        "[opportunities bot](opportunities/BOT.md).</sub>",
        "",
    ]
    return "\n".join(out)


def render_closed(programs: list[dict], now: datetime) -> str:
    today = now.date() if isinstance(now, datetime) else now
    closed = sorted((p for p in programs if p["status"] == "closed"),
                    key=lambda p: (months_until(p["deadline_month"], today), p["name"].lower()))
    out = [
        "# 🔒 Closed programs",
        "",
        "These aren't accepting applications right now. Most run every year, so each one moves back to the "
        f"[main board](README.md) when its next cycle gets close. Reopened early? [Report it]({REPORT_URL}).",
        "",
    ]
    out += (table(["Program", "For", "Type", "Usually", "Closed as of"],
                  [[name_cell(p, today), who(p), p["type"], when(p["deadline_month"]).replace("Check site", "—"),
                    short(p.get("checked")) or "—"] for p in closed])
            if closed else ["Nothing closed right now."])
    return "\n".join(out) + "\n"


if __name__ == "__main__":
    import programs as program_list   # imports board, so only here

    programs = json.loads(PROGRAMS_PATH.read_text())
    now = datetime.now(TZ)
    if program_list.refresh(programs, now.date()):
        program_list.save(programs)
    README_PATH.write_text(render(programs, now))
    CLOSED_PATH.write_text(render_closed(programs, now))
    print(f"Wrote {README_PATH.name} and {CLOSED_PATH.name} with {len(programs)} programs.")
