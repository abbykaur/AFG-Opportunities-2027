# Opportunities bot

Picks a few programs a week for **#opportunities**, reviewed by mods in **#opps-queue**. It's the same AFG bot as the news feed, and it reads the same list as this repo's [README board](../README.md): `programs.json`.

| When (Pacific) | What happens |
|---|---|
| Monday | Up to 4 picks go to #opps-queue: programs confirmed open go first, then ones whose usual deadline is this month or next. Programs for women get a boost. At most 2 of one type, and at most 6 drafts open at once. Then up to 5 **🔎 Is this open?** checks (see below). |
| Any time | ✅ on a draft posts it to #opportunities within about 2 hours. ❌ closes it. |
| Next Monday | A draft nobody reacted to is marked "Carried over". If it's still unreviewed the Monday after, it closes. |
| Daily, ~6 AM | The bot checks the [SimplifyJobs](https://github.com/SimplifyJobs/Summer2027-Internships) and [vanshb03](https://github.com/vanshb03/Summer2027-Internships) internship lists for programs with an open posting (and marks ones whose posting is gone as closed), then refreshes the pinned **📌 Opportunities board** message in #opportunities. |

The bot checks every 2 hours, so replies and commands take up to 2 hours to show up. It reacts ✏️, 👍, 🗑️ or 👀 when it picks one up.

## For mods

- **Post / skip:** react ✅ or ❌ on a draft. If both, ❌ wins.
- **Edit a draft:** reply to it with `note: …` (a line that posts with it, e.g. "Great for sophomores, apply early!") or `title: …`. A plain reply becomes the note.
- **More picks:** post `more` for 3 extra.
- **Add a program to the board:** `add: Name | https://link | Oct | Fellowship`. Month and type are optional. Types: Underclassmen program, Fellowship, Insight / exploratory, Scholarship, Conference scholarship, Research program, Mentorship (the start of the name is enough, e.g. `insight`).
- **Remove one:** `remove: Name`.
- **Mark one open:** `open: Name | Dec 15 | https://apply-link` (deadline and apply link optional). It shows as ✅ on the board, with a bold deadline if you gave one.
- **Mark one closed:** `closed: Name`. It moves to CLOSED.md and comes back when its usual month comes around again.
- **Mark one not open yet:** `upcoming: Name | Jan 15` (date optional). It shows as ⏳ on the board.
- **Women label:** `women: Name | only` (only women, or women and gender minorities, can apply), `women: Name | focused` (open to everyone, built to support women), or `women: Name | none`. Check the program's eligibility wording first: a wrong "women only" label sends people away from programs they could apply to. `add:` takes it as a fifth part too: `add: Name | link | Oct | Fellowship | only`.

## 🔎 Is this open? checks

Each Monday the bot posts up to 5 checks for programs due this month or next that nobody has confirmed in 2 weeks. Open the program's page and react once: ✅ open, 🔒 closed this cycle, or ⏳ not open yet. If you have the deadline or the application link, reply to the check with it (`Dec 15`, a link, or both) before or after reacting. The board updates within 2 hours. If mods react differently, the most cautious answer wins (🔒, then ⏳, then ✅). A check nobody answers closes after a week and comes back another Monday.

For any command, the name can be the full name or a unique part of it (`open: Dropbox | Dec 15`).

## Statuses

Every program is ✅ open (someone checked), ⏳ not open yet (checked), 🔒 closed, or ❓ unconfirmed (only its usual timing is known). Checks come from open postings on SimplifyJobs and vanshb03 (matched by each program's `match` phrase in programs.json, like "First Play"), the 🔎 checks, your `open:` / `closed:` / `upcoming:` commands, and approved reports on GitHub. A ✅ on a draft posts it but doesn't change its status, so a program that's coming up but not open yet can still be announced. A passed deadline closes a program automatically, and a status nobody re-checks goes back to ❓ (45 days for open, 30 for not open yet).
- **New programs:** a pick marked **New** came from SimplifyJobs and isn't on the board yet. Approving it posts it and adds it to the board.

People without Discord access can suggest programs through the repo's [Suggest a program](../../../issues/new?template=suggest-program.yml) form.

## Feedback loop

Each closed draft is logged by type (approved, skipped, or no reaction; no names). Types mods keep approving get picked more often over the last 8 weeks, the same way the news bot weights video categories. Women-focused programs get a boost.

## Setup

1. In #opportunities, give the bot View Channel, Send Messages, Embed Links, Read Message History, and **Pin Messages** (or Manage Messages) so it can pin the board. In #opps-queue: View Channel, Send Messages, Embed Links, Add Reactions, Read Message History.
2. Repo secrets (the token is already set for the news bot):
   ```
   gh secret set OPPS_QUEUE_CHANNEL_ID --body <#opps-queue id>
   gh secret set OPPS_CHANNEL_ID --body <#opportunities id>
   ```
3. **Run it now:** Actions → **opportunities** → Run workflow, with `force` = `draft` to post this week's picks.

The workflow runs every 2 hours. Until both channel secrets exist, it only re-renders the README board.
