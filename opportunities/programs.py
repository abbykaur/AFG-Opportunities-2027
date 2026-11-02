"""Check, add, and update entries in programs.json.

    python opportunities/programs.py check [--base OLD.json]
        Validate programs.json. With --base, also check that links added since
        OLD.json still load. Used by the pull request check.
    python opportunities/programs.py add-issue EVENT.json
        Add the program from an approved "Suggest a program" issue.
    python opportunities/programs.py report-issue EVENT.json
        Apply an approved "Report closed / wrong info" issue.

EVENT.json is the GitHub event payload. Each prints problems and exits 1 if
the list or the issue isn't valid.

Status: every program is "open" (accepting applications, checked), "closed"
(not accepting right now), or "unconfirmed" (only its usual timing is known).
refresh() keeps that honest over time: a passed deadline closes a program, a
closed program comes back as unconfirmed when its usual month comes around
again, and an "open" nobody has re-checked in a while goes back to unconfirmed.
"""
import calendar
import json
import re
import sys
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path

from board import COMING_UP_MONTHS, PROGRAMS_PATH, SECTIONS, TZ, months_until

WHO = ["Freshman", "Sophomore", "Junior", "All students"]
MONTHS = {calendar.month_name[i]: i for i in range(1, 13)}
STATUSES = ("open", "closed", "unconfirmed")
# A closed program comes back as unconfirmed once its usual month is close
# again (and it closed at least REOPEN_MIN ago); with no month, after REOPEN_AFTER.
REOPEN_MIN = timedelta(days=60)
REOPEN_AFTER = timedelta(days=300)
OPEN_STALE_AFTER = timedelta(days=45)  # "open" with no deadline needs a re-check by then
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/124.0 Safari/537.36"


def key(name: str) -> str:
    """Names compared without case, spaces, or punctuation, to catch duplicates."""
    return re.sub(r"[^a-z0-9]", "", name.lower())


def today() -> date:
    return datetime.now(TZ).date()


def parse_date(text: str, ref: date) -> date | None:
    """'2026-12-15', 'Dec 15', 'December 15, 2026', or '12/15'. Without a year,
    the next Dec 15 on or after ref."""
    text = text.strip().rstrip(".")
    if m := re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", text):
        y, mo, d = map(int, m.groups())
    elif m := re.fullmatch(r"(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?", text):
        mo, d, y = int(m[1]), int(m[2]), m[3] and int(m[3])
        y = y + 2000 if y and y < 100 else y
    elif m := re.fullmatch(r"([A-Za-z]{3,9})\.? (\d{1,2})(?:st|nd|rd|th)?,? ?(\d{4})?", text):
        mo = next((n for name, n in MONTHS.items() if name.lower().startswith(m[1].lower()[:3])), None)
        d, y = int(m[2]), m[3] and int(m[3])
        if mo is None:
            return None
    else:
        return None
    try:
        if y:
            return date(y, mo, d)
        found = date(ref.year, mo, d)
        return found if found >= ref else date(ref.year + 1, mo, d)
    except ValueError:
        return None


def new_program(**fields) -> dict:
    """A program with every field the board expects, defaults filled in."""
    p = {"name": "", "url": "", "type": "Fellowship", "who": "All students",
         "deadline_month": None, "women": False, "source": "",
         "status": "unconfirmed", "deadline": None, "checked": None,
         "added": today().isoformat()}
    p.update(fields)
    return p


def set_status(p: dict, status: str, deadline: date | None = None, via: str | None = None,
               on: date | None = None, apply_url: str | None = None) -> None:
    """Record a check: the program is open/closed as of `on` (today).
    apply_url is this cycle's application posting, kept only while open."""
    p["status"] = status
    p["checked"] = (on or today()).isoformat()
    if status != "open":
        p.pop("apply_url", None)
    elif apply_url:
        p["apply_url"] = apply_url
    if deadline:
        p["deadline"] = deadline.isoformat()
        p["deadline_month"] = deadline.month
    if via:
        p["via"] = via


def refresh(programs: list[dict], on: date | None = None) -> bool:
    """Age statuses (see the module docstring). Returns whether anything changed."""
    on = on or today()
    changed = False
    for p in programs:
        checked = date.fromisoformat(p["checked"]) if p.get("checked") else None
        deadline = date.fromisoformat(p["deadline"]) if p.get("deadline") else None
        before = (p["status"], p.get("deadline"))
        if p["status"] != "closed" and deadline and deadline < on:
            p["status"] = "closed"
        elif p["status"] == "closed" and checked and (
                on - checked > REOPEN_AFTER
                or (p["deadline_month"] and on - checked > REOPEN_MIN
                    and months_until(p["deadline_month"], on) < COMING_UP_MONTHS)):
            p["status"], p["deadline"] = "unconfirmed", None
        elif (p["status"] == "open" and not deadline and checked
              and on - checked > OPEN_STALE_AFTER):
            p["status"] = "unconfirmed"
        if p["status"] != "open":
            p.pop("apply_url", None)
        changed |= (p["status"], p.get("deadline")) != before
    return changed


def problems(programs: list[dict]) -> list[str]:
    out, names = [], {}
    for i, p in enumerate(programs, 1):
        label = f"#{i} ({p.get('name', 'no name')})"
        name, url = p.get("name", ""), p.get("url", "")
        # These characters would break the board's table or sneak in links.
        if not name or re.search(r"[|\[\]<>\n]", name):
            out.append(f"{label}: name is empty or has | [ ] < > or a line break")
        if not re.fullmatch(r"https?://[^\s|()<>]+", url):
            out.append(f"{label}: link must be a single http(s) URL")
        if p.get("apply_url") and not re.fullmatch(r"https?://[^\s|()<>]+", p["apply_url"]):
            out.append(f"{label}: apply_url must be a single http(s) URL")
        if p.get("type") not in SECTIONS:
            out.append(f"{label}: type must be one of {', '.join(SECTIONS)}")
        who = p.get("who", "").split(", ")
        if not who or any(w not in WHO for w in who):
            out.append(f"{label}: 'who' must be from {', '.join(WHO)}, comma-separated")
        if p.get("deadline_month") not in [None, *range(1, 13)]:
            out.append(f"{label}: deadline_month must be 1-12 or null")
        if not isinstance(p.get("women"), bool):
            out.append(f"{label}: women must be true or false")
        if p.get("status") not in STATUSES:
            out.append(f"{label}: status must be one of {', '.join(STATUSES)}")
        for field in ("deadline", "checked", "added"):
            value = p.get(field)
            if value is None and field != "added":
                continue
            try:
                date.fromisoformat(value)
            except (TypeError, ValueError):
                out.append(f"{label}: {field} must be a YYYY-MM-DD date" + ("" if field == "added" else " or null"))
        if key(name) in names:
            out.append(f"{label}: same name as #{names[key(name)]}")
        names.setdefault(key(name), i)
    return out


def link_works(url: str) -> bool:
    """False only for a page that's definitely gone; blocks and timeouts pass."""
    try:
        urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": UA}), timeout=15)
    except urllib.error.HTTPError as e:
        return e.code not in (404, 410)
    except Exception:
        pass
    return True


def issue_fields(body: str):
    """Field reader for a GitHub issue form body ("### Label" sections)."""
    sections = dict(re.findall(r"^### (.+?)\s*\n(.*?)(?=^### |\Z)", body, re.M | re.S))
    field = lambda label: sections.get(label, "").strip().replace("_No response_", "").strip()
    ticked = lambda label: re.findall(r"- \[[xX]\] (.+)", field(label))
    return field, ticked


def from_issue(body: str) -> tuple[dict, list[str]]:
    """The program in a "Suggest a program" issue, plus any problems reading it."""
    field, ticked = issue_fields(body)
    p = new_program(
        name=field("Program name"), url=field("Link"), type=field("Type"),
        who=", ".join(w for w in WHO if w in ticked("Who it's for")) or "All students",
        deadline_month=MONTHS.get(field("Usual deadline month")),
        women=bool(ticked("For women?")), source="suggestion",
    )
    errors = []
    deadline = None
    if field("Deadline (if you know it)"):
        deadline = parse_date(field("Deadline (if you know it)"), today())
        if not deadline:
            errors.append("couldn't read the deadline; use a date like Dec 15, 2026")
    if field("Is it open right now?").startswith("Yes"):
        set_status(p, "open", deadline, via="suggestion")
    elif deadline:
        p["deadline"], p["deadline_month"] = deadline.isoformat(), deadline.month
    return p, errors


def find(programs: list[dict], name: str) -> dict | None:
    """The program with this name, or the only one whose name contains it."""
    exact = [p for p in programs if key(p["name"]) == key(name)]
    if exact:
        return exact[0]
    partial = [p for p in programs if key(name) and key(name) in key(p["name"])]
    return partial[0] if len(partial) == 1 else None


def apply_report(programs: list[dict], body: str) -> tuple[dict | None, list[str]]:
    """Apply a "Report closed / wrong info" issue. Returns (program, problems)."""
    field, _ = issue_fields(body)
    p = find(programs, field("Program name"))
    if not p:
        return None, [f"no single program on the board matches \"{field('Program name')}\"; "
                      "use the name exactly as it appears on the board"]
    problem = field("What's wrong?")
    deadline = None
    if field("Correct deadline (if you know it)"):
        deadline = parse_date(field("Correct deadline (if you know it)"), today())
        if not deadline:
            return p, ["couldn't read the deadline; use a date like Dec 15, 2026"]
    if problem.startswith("It's closed"):
        set_status(p, "closed", via="report")
    elif problem.startswith("It's open"):
        set_status(p, "open", deadline, via="report")
    elif problem.startswith("Wrong deadline"):
        if not deadline:
            return p, ["add the correct deadline"]
        set_status(p, p["status"], deadline, via="report")
    elif problem.startswith("Link"):
        url = field("Correct link (if it moved)")
        if not url:
            return p, ["add the program's new link"]
        p["url"] = url
        if not link_works(url):
            return p, ["the new link returns 404/410"]
    else:
        return p, ["this kind of report needs a maintainer to edit programs.json by hand"]
    return p, problems(programs)


def save(programs: list[dict]) -> None:
    PROGRAMS_PATH.write_text(json.dumps(programs, indent=2, ensure_ascii=False) + "\n")


def main() -> int:
    programs = json.loads(PROGRAMS_PATH.read_text())
    if sys.argv[1] == "check":
        errors = problems(programs)
        if "--base" in sys.argv:
            old = {p["url"] for p in json.loads(Path(sys.argv[sys.argv.index("--base") + 1]).read_text())}
            errors += [f"{p['name']}: link returns 404/410" for p in programs
                       if p.get("url") not in old and not link_works(p["url"])]
        print("\n".join(errors) or f"programs.json looks good ({len(programs)} programs).")
        return 1 if errors else 0

    body = json.loads(Path(sys.argv[2]).read_text())["issue"]["body"] or ""
    if sys.argv[1] == "add-issue":
        program, errors = from_issue(body)
        programs.append(program)
        errors += problems(programs)
        if not errors and not link_works(program["url"]):
            errors.append("the link returns 404/410")
    else:
        program, errors = apply_report(programs, body)
    if errors:
        print("\n".join(f"- {e.split(': ', 1)[-1] if e.startswith('#') else e}" for e in errors))
        return 1
    save(programs)
    print(program["name"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
