"""Render the public opportunities board from programs.json.

    python opportunities/board.py

Writes README.md (open and upcoming programs, by category) and CLOSED.md
(programs not accepting applications until their next cycle). First it ages
statuses (programs.refresh: passed deadlines close, closed programs come back
for their next cycle), so the daily workflow keeps both pages honest.
"""
import calendar
import json
import re
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
PROGRAMS_PATH = ROOT / "opportunities" / "programs.json"
README_PATH = ROOT / "README.md"
CLOSED_PATH = ROOT / "CLOSED.md"
TZ = ZoneInfo("America/Los_Angeles")

INVITE_URL = "https://discord.gg/6BBkjMJURt"
# Relative links, so they survive a repo rename.
SUGGEST_URL = "../../issues/new?template=suggest-program.yml"
REPORT_URL = "../../issues/new?template=report-program.yml"
# How many months ahead "Coming up" looks, counting the current month.
COMING_UP_MONTHS = 2
NEW_FOR = timedelta(days=14)    # 🆕 marker
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


def status(p: dict) -> str:
    if p["status"] == "open":
        return f"✅ [Apply]({p['apply_url']})" if p.get("apply_url") else "✅ Open"
    return "🔒" if p["status"] == "closed" else "❓"


def name_cell(p: dict, today: date) -> str:
    new = (p.get("added") and p.get("source") not in SOURCES
           and today - date.fromisoformat(p["added"]) <= NEW_FOR)
    return f"[{p['name']}]({p['url']})" + (" 🆕" if new else "") + (" 💜" if p.get("women") else "")


def anchor(heading: str) -> str:
    """GitHub's heading anchor: lowercase, punctuation and emoji dropped, spaces to hyphens."""
    return "#" + re.sub(r"[^\w\- ]", "", heading.lower()).replace(" ", "-")


def table(headers: list[str], rows: list[list[str]]) -> list[str]:
    return (["| " + " | ".join(headers) + " |", "|" + "---|" * len(headers)]
            + ["| " + " | ".join(r) + " |" for r in rows])


def sort_key(p: dict, today: date):
    """Open first, then by exact deadline, then by usual month, then name."""
    exact = date.fromisoformat(p["deadline"]) if p.get("deadline") else None
    return (p["status"] != "open", exact or date.max, months_until(p["deadline_month"], today),
            p["name"].lower())


FAQ = [
    ("What's on this list?",
     "Programs for students early in college: underclassmen internships and programs, fellowships, "
     "insight days, scholarships, conference scholarships, research programs, and mentorship. "
     "Regular internships and new-grad jobs aren't here; for those, see "
     "[SimplifyJobs](https://github.com/SimplifyJobs)."),
    ("How do you know what's open?",
     "✅ means someone checked recently: an AFG mod, a community report, or the program's posting showing up "
     "as open on [SimplifyJobs](https://github.com/SimplifyJobs). Each program's last check is in "
     "[programs.json](opportunities/programs.json). Deadlines that pass close automatically, and an "
     "\"open\" nobody re-checks in 45 days goes back to ❓. ❓ means we only know the usual timing from past "
     "years, so check the program's page."),
    ("Where did the closed programs go?",
     "To [CLOSED.md](CLOSED.md). Most programs run every year, so they come back here when their next "
     "cycle gets close."),
    ("How can I help?",
     f"[Suggest a program]({SUGGEST_URL}) we're missing, or [report one]({REPORT_URL}) that's closed, open, "
     "or has the wrong date. Both are short forms (a free GitHub account is needed). A maintainer reviews "
     "each one, and approved changes show up here automatically."),
    ("Who runs this?",
     f"[AI First Girls]({INVITE_URL}), a community for women in AI and tech. Come say hi on Discord, where "
     "mods share picks from this list in #opportunities every week."),
]


def render(programs: list[dict], now: datetime) -> str:
    today = now.date() if isinstance(now, datetime) else now
    active = sorted((p for p in programs if p["status"] != "closed"), key=lambda p: sort_key(p, today))
    closed = [p for p in programs if p["status"] == "closed"]
    open_now = [p for p in active if p["status"] == "open"]
    soon = [p for p in active if p["status"] != "open"
            and months_until(p["deadline_month"], today) < COMING_UP_MONTHS]
    types = [t for t in list(SECTIONS) + sorted({p["type"] for p in programs} - set(SECTIONS))
             if any(p["type"] == t for p in active)]
    end_month = calendar.month_name[(today.month + COMING_UP_MONTHS - 2) % 12 + 1]

    out = [
        "# 🎓 AFG Opportunities: Fellowships & Early-Career Programs",
        "",
        "Fellowships, sophomore and freshman programs, insight days, scholarships, and more for students "
        f"starting out in tech and AI. Kept up to date by **[AI First Girls]({INVITE_URL})** and the community.",
        "",
        f"🙏 **Know one we're missing? [Suggest a program]({SUGGEST_URL}). "
        f"Spot one that's closed or wrong? [Report it]({REPORT_URL}).** 🙏",
        "",
        "---",
        "",
        f"### Browse {len(active)} programs by category",
        "",
    ]
    for t in types:
        group = [p for p in active if p["type"] == t]
        n_open = sum(p["status"] == "open" for p in group)
        heading = SECTIONS.get(t, t)
        icon, label = heading.split(" ", 1) if " " in heading else ("", heading)
        out.append(f"{icon} **[{label}]({anchor(heading)})** "
                   f"({len(group)}{f' · ✅ {n_open} open' if n_open else ''})")
        out.append("")
    out += [
        "---",
        "",
        "## Legend",
        "",
        "- ✅ Open now, checked recently (**Apply** links straight to this cycle's application)",
        "- ❓ Not confirmed: we only know the usual timing, so check the program's page",
        "- 🆕 Added in the last 2 weeks",
        "- 💜 For women (or women and other groups underrepresented in tech)",
        "- **Bold dates** are confirmed deadlines; `~Nov` means it usually opens or closes around then",
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
                     [[name_cell(p, today), p["who"], p["type"], status(p), deadline(p)] for p in open_now])
    else:
        out.append(f"Nothing confirmed open right now. Check **Coming up** below, or [report one]({REPORT_URL}) that's open.")
    out += ["", f"## 🔥 Coming up ({calendar.month_name[today.month]}"
            f"{'' if COMING_UP_MONTHS == 1 else ' – ' + end_month})", "",
            "Not confirmed yet: these usually open or close around now, so check each page.", ""]
    if soon:
        out += table(["Program", "For", "Type", "Usually"],
                     [[name_cell(p, today), p["who"], p["type"], deadline(p)] for p in soon])
    else:
        out.append("Nothing else due in the next couple of months.")
    for t in types:
        group = [p for p in active if p["type"] == t]
        out += ["", f"## {SECTIONS.get(t, t)}", ""]
        out += table(["Program", "For", "Status", "Deadline", "Added"],
                     [[name_cell(p, today), p["who"], status(p), deadline(p), short(p.get("added"))] for p in group])
    out += ["", "## FAQ", ""]
    for q, a in FAQ:
        out += ["<details>", f"<summary><strong>{q}</strong></summary>", "", a, "", "</details>", ""]
    out += [
        "## Credits",
        "",
        "The list started from " + " and ".join(f"[{name}]({url})" for name, url in SOURCES.items())
        + " (data by Zapply). Thank you!",
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
                  [[name_cell(p, today), p["who"], p["type"], when(p["deadline_month"]).replace("Check site", "—"),
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
