"""Render the repo's README.md (the public opportunities board) from programs.json.

Run from the repo root: python opportunities/board.py
The daily workflow (.github/workflows/opportunities.yml) runs this and commits
the README when it changes, so "Coming up" always reflects the current month.
"""
import calendar
import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
PROGRAMS_PATH = ROOT / "opportunities" / "programs.json"
README_PATH = ROOT / "README.md"
TZ = ZoneInfo("America/Los_Angeles")

INVITE_URL = "https://discord.gg/6BBkjMJURt"
SUGGEST_URL = "../../issues/new?template=suggest-program.yml"
# How many months ahead "Coming up" looks, counting the current month.
COMING_UP_MONTHS = 2
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


def months_until(month: int | None, now: datetime) -> int:
    """0 for this month, 11 for last month; programs with no month sort last."""
    return 99 if month is None else (month - now.month) % 12


def when(month: int | None) -> str:
    return f"~{calendar.month_abbr[month]}" if month else "Check site"


def row(p: dict, with_type: bool = False) -> str:
    name = f"[{p['name']}]({p['url']})" + (" 💜" if p.get("women") else "")
    cells = [name, p["who"]] + ([p["type"]] if with_type else []) + [when(p["deadline_month"])]
    return "| " + " | ".join(cells) + " |"


def render(programs: list[dict], now: datetime) -> str:
    by_date = sorted(programs, key=lambda p: (months_until(p["deadline_month"], now), p["name"].lower()))
    soon = [p for p in by_date if months_until(p["deadline_month"], now) < COMING_UP_MONTHS]
    types = list(SECTIONS) + sorted({p["type"] for p in programs} - set(SECTIONS))

    out = [
        "# 📋 AFG Opportunities",
        "",
        "Fellowships, insight programs, and early-career programs for students in tech, "
        f"curated by **[AI First Girls]({INVITE_URL})**.",
        "",
        # Month only, so the daily run doesn't commit a new README every day.
        f"**{len(programs)} programs** · updated {now:%B %Y} · "
        "💜 = for women · dates are when a program usually opens or closes, so check its page.",
        "",
        f"**[➕ Suggest a program]({SUGGEST_URL})**",
        "",
        f"## 🔥 Coming up ({calendar.month_name[now.month]}"
        f"{'' if COMING_UP_MONTHS == 1 else ' – ' + calendar.month_name[(now.month + COMING_UP_MONTHS - 2) % 12 + 1]})",
        "",
    ]
    if soon:
        out += ["| Program | For | Type | When |", "|---|---|---|---|"] + [row(p, with_type=True) for p in soon]
    else:
        out.append("Nothing due in the next couple of months. Check the sections below.")
    for t in types:
        group = [p for p in by_date if p["type"] == t]
        if not group:
            continue
        out += ["", f"## {SECTIONS.get(t, t)} ({len(group)})", "",
                "| Program | For | When |", "|---|---|---|"] + [row(p) for p in group]
    out += [
        "",
        "## Suggest a program",
        "",
        f"Fill out the **[Suggest a program]({SUGGEST_URL})** form (needs a free GitHub account). "
        "A maintainer reviews it, and once approved it shows up here automatically. "
        f"You can also post it in **#opportunities** on the [AI First Girls Discord]({INVITE_URL}).",
        "",
        "## Credits",
        "",
        "The list started from " + " and ".join(f"[{name}]({url})" for name, url in SOURCES.items())
        + " (data by Zapply). Thank you!",
        "",
        "---",
        "",
        "<sub>This board updates itself daily. The repo also runs AFG's Discord bots: "
        "[jobs bot setup](DEPLOY.md) · [news bot](news-bot/README.md).</sub>",
        "",
    ]
    return "\n".join(out)


if __name__ == "__main__":
    programs = json.loads(PROGRAMS_PATH.read_text())
    README_PATH.write_text(render(programs, datetime.now(TZ)))
    print(f"Wrote {README_PATH.name} with {len(programs)} programs.")
