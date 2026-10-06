---
name: event-log-review
description: Download a day's Salesforce Event Monitoring logs (EventLogFile) from an org and produce a short summary of security, access and stability concerns. Use when asked to review, check or summarise event logs, or for the daily scheduled event-log run. Optional arguments, either order - an org alias (default $EVENT_LOG_ORG, else the sf CLI target-org) and a log date YYYY-MM-DD (default yesterday, UTC).
argument-hint: "[org-alias] [YYYY-MM-DD]"
allowed-tools: Bash(*/event-log-review/scripts/run.sh) Bash(*/event-log-review/scripts/run.sh *) Bash(jq *) Read Grep Glob Write(~/.local/share/sf-event-logs/**) Edit(~/.local/share/sf-event-logs/**)
---

# Event log review

Review one day of Event Monitoring logs for one org and report concerns.
Arguments (either order, both optional): an org alias — default `$EVENT_LOG_ORG`,
else the `sf` CLI's `target-org` — and a log date `YYYY-MM-DD` — default yesterday (UTC). Below, `<org>` and `<date>`
mean the resolved values; the digest header shows both.
This runs unattended on a daily schedule, so: do not ask questions, do not change
anything in the org, and always finish with the summary — even if a step failed.

## 1. Collect

Run the pipeline, passing the arguments through unchanged (it works from any
working directory):

```bash
"${CLAUDE_SKILL_DIR}/scripts/run.sh" $ARGUMENTS
```

Data lives outside the skill, in `$EVENT_LOG_HOME` (default `~/.local/share/sf-event-logs`):
`logs/<org>/<date>/` holds the CSVs, `digest.md` and your `review.md`;
`known/<org>.md` holds confirmed-normal patterns.

- Exit code 0: it prints two lines, `digest=<path to digest.md>` and
  `known=<path to known/<org>.md>`. Below, `<day dir>` is the folder holding the digest.
- Exit code 2: Salesforce has not generated that day's files yet (they normally
  appear ~02:30–03:00 UTC the next day). Report "logs not ready" and stop.
- Any other failure (no org given, auth expired, network): report the error text and the likely
  fix (e.g. `sf org login web --alias <org>`) and stop.

Do not run your own `sf data query` calls against the org; the scripts already
fetch everything needed, including `users.json` for resolving User Ids.

## 2. Read

1. Read the digest in full. It already compares against up to 7 prior days
   stored alongside it.
2. Read the `known` file — patterns the owner has confirmed as expected for that
   org (`run.sh` creates it from `known/_template.md` on first use). Do not raise
   these as concerns unless they changed materially (volume, user, scope).
3. When a digest item needs detail, query the raw CSVs in `<day dir>` with
   `python3`/`csv` or `jq` (one CSV per event type; `users.json` maps User Ids).
   Keep this targeted: confirm or dismiss specific items; don't dump whole files.

## 3. Judge

Rate each finding. Prioritise in this order:

**High (🔴)** — likely needs action today
- Failed-login bursts, logins for inactive users, logins from a new country,
  successful login right after repeated failures
- Grants of high-privilege permissions (ModifyAllData, ViewAllData, ManageUsers,
  AuthorApex, CustomizeApplication, AssignPermissionSets, modifyAllRecords on
  sensitive objects) that aren't explained by a known deploy pipeline
- Large data extraction: report exports, bulk API, unusually high rows processed
  by a person rather than an integration
- Encryption key management actions (destroy/export/rotate)
- A new integration / connected app calling the API

**Medium (🟡)** — worth knowing
- New IPs for known users, out-of-hours interactive logins
- New Apex exception signatures, or a recurring one that jumped sharply
- Flow errors, failed queued jobs, failed package installs
- Metadata deploys by someone other than the usual pipeline user
- Big volume spikes/drops vs baseline (a drop to near-zero can mean an
  integration silently stopped)

**Low / info** — mention in one line at most
- Recurring known exceptions at normal levels, routine pipeline activity

Account for the weekday shown in the digest header: weekends normally have no
logins, API or metadata activity, so their absence is not a concern — but
human activity on a weekend is worth a mention.

Check whether the org is a sandbox (the `sf` username ends in `.<sandboxname>`,
as in `users.json`). In a sandbox, weigh developer activity accordingly, but
privilege grants, data extraction and unknown access still matter — sandboxes
often hold copies of production data. Never compare one org's activity with
another's; each org has its own baseline.

Be honest about uncertainty: say "unusual, probably X" rather than asserting
intent. Never invent data that is not in the digest or CSVs. Note coverage gaps
(e.g. baseline shorter than 7 days, event types absent today).

## 4. Report

Write the full review to `<day dir>/review.md`:

```markdown
# Event log review — <org> — <date>
**Overall:** 🔴/🟡/🟢 <one-line verdict>

## Concerns
1. 🔴 <title> — <what happened, who, when, how it compares to baseline>
   Evidence: <counts / users / IPs / ids>  ·  Suggested check: <concrete next step>
...

## Noted, not concerning
- ...

## Coverage
- Event types present, baseline days used, anything missing
```

Then finish your response with a **short summary for the reader** (it is what
the scheduler captures): the overall verdict line, the concerns as one line each
(highest first, max ~8), and the path to `review.md`. If nothing is concerning,
say so plainly in two or three lines.
