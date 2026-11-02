"""Check and add entries in programs.json.

    python opportunities/programs.py check [--base OLD.json]
        Validate programs.json. With --base, also check that links added since
        OLD.json still load. Used by the pull request check.
    python opportunities/programs.py add-issue EVENT.json
        Add the program from an approved "Suggest a program" issue (the GitHub
        event payload). Used by the issue approval workflow.

Both print problems and exit 1 if the list or the suggestion isn't valid.
"""
import calendar
import json
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

from board import PROGRAMS_PATH, SECTIONS

WHO = ["Freshman", "Sophomore", "Junior", "All students"]
MONTHS = {calendar.month_name[i]: i for i in range(1, 13)}
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/124.0 Safari/537.36"


def key(name: str) -> str:
    """Names compared without case, spaces, or punctuation, to catch duplicates."""
    return re.sub(r"[^a-z0-9]", "", name.lower())


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
        if p.get("type") not in SECTIONS:
            out.append(f"{label}: type must be one of {', '.join(SECTIONS)}")
        who = p.get("who", "").split(", ")
        if not who or any(w not in WHO for w in who):
            out.append(f"{label}: 'who' must be from {', '.join(WHO)}, comma-separated")
        if p.get("deadline_month") not in [None, *range(1, 13)]:
            out.append(f"{label}: deadline_month must be 1-12 or null")
        if not isinstance(p.get("women"), bool):
            out.append(f"{label}: women must be true or false")
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


def from_issue(body: str) -> dict:
    """Read the fields of a "Suggest a program" issue form."""
    sections = dict(re.findall(r"^### (.+?)\s*\n(.*?)(?=^### |\Z)", body, re.M | re.S))
    field = lambda label: sections.get(label, "").strip().replace("_No response_", "").strip()
    ticked = lambda label: re.findall(r"- \[[xX]\] (.+)", field(label))
    return {
        "name": field("Program name"),
        "url": field("Link"),
        "type": field("Type"),
        "who": ", ".join(w for w in WHO if w in ticked("Who it's for")) or "All students",
        "deadline_month": MONTHS.get(field("Usual deadline month")),
        "women": bool(ticked("For women?")),
        "source": "suggestion",
    }


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

    event = json.loads(Path(sys.argv[2]).read_text())
    program = from_issue(event["issue"]["body"] or "")
    errors = problems(programs + [program])
    if not errors and not link_works(program["url"]):
        errors.append("the link returns 404/410")
    if errors:
        print("\n".join(f"- {e}" for e in errors))
        return 1
    programs.append(program)
    PROGRAMS_PATH.write_text(json.dumps(programs, indent=2, ensure_ascii=False) + "\n")
    print(program["name"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
