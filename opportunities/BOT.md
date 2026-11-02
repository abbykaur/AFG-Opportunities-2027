# Opportunities bot

Picks a few programs a week for **#opportunities**, reviewed by mods in **#opps-queue**. It's the same AFG bot as the news feed, and it reads the same list as this repo's [README board](../README.md): `programs.json`.

| When (Pacific) | What happens |
|---|---|
| Monday | Up to 4 picks go to #opps-queue: programs whose usual deadline is this month or next, and programs [SimplifyJobs](https://github.com/SimplifyJobs) lists as open right now (those go first). At most 2 of one type, and at most 6 drafts open at once. |
| Any time | ✅ on a draft posts it to #opportunities within about 2 hours. ❌ closes it. |
| Next Monday | A draft nobody reacted to is marked "Carried over". If it's still unreviewed the Monday after, it closes. |
| Daily, ~6 AM | The pinned **📌 Opportunities board** message in #opportunities is refreshed with what's coming up. |

The bot checks every 2 hours, so replies and commands take up to 2 hours to show up. It reacts ✏️, 👍, 🗑️ or 👀 when it picks one up.

## For mods

- **Post / skip:** react ✅ or ❌ on a draft. If both, ❌ wins.
- **Edit a draft:** reply to it with `note: …` (a line that posts with it, e.g. "Great for sophomores, apply early!") or `title: …`. A plain reply becomes the note.
- **More picks:** post `more` for 3 extra.
- **Add a program to the board:** `add: Name | https://link | Oct | Fellowship`. Month and type are optional. Types: Underclassmen program, Fellowship, Insight / exploratory, Scholarship, Conference scholarship, Research program, Mentorship (the start of the name is enough, e.g. `insight`).
- **Remove one:** `remove: Name`.
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
