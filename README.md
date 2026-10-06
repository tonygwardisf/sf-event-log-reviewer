# Salesforce Event Log Review — Claude Code plugin

Downloads one day of Salesforce Event Monitoring logs (`EventLogFile`) for an org,
condenses them into a digest compared against the previous 7 days, and has Claude
write a short review of security, access and stability concerns: failed-login
bursts, new countries/IPs, privilege grants, data extraction, metadata deploys,
new Apex exceptions, volume spikes and drops.

It is read-only against the org: it queries `EventLogFile`, `User`, `LoginHistory`
and `Organization`, and never changes anything.

## Prerequisites

- [Salesforce CLI](https://developer.salesforce.com/tools/salesforcecli) (`sf`), logged in
  to the org: `sf org login web --alias <alias>`
- The org user needs **API Enabled** and **View Event Log Files**. Without the Event
  Monitoring add-on (Shield), Salesforce provides only a few event types, so the
  review covers less.
- `python3` (3.8+), `jq`, and GNU `date` (Linux/WSL; on macOS `brew install coreutils`
  and put `gnubin` on `PATH`)

## Install

In Claude Code:

```
/plugin marketplace add tonygwardisf/sf-event-log-reviewer
/plugin install sf-event-logs@sf-event-log-reviewer
```

or from a shell: `claude plugin marketplace add tonygwardisf/sf-event-log-reviewer`
then `claude plugin install sf-event-logs@sf-event-log-reviewer`.

## Use

```
/sf-event-logs:event-log-review                       # default org, yesterday (UTC)
/sf-event-logs:event-log-review my-org                # another org
/sf-event-logs:event-log-review my-org 2026-10-05     # a specific day (either order)
```

Asking Claude to "review yesterday's event logs" also triggers it.

The default org is `$EVENT_LOG_ORG`, else the `sf` CLI's `target-org`. Daily log files
appear around 03:00 UTC the next day; before then the review reports "logs not ready".
The first run for an org also downloads the previous 7 days as a baseline, so it
takes a few minutes. Later runs fetch one day.

## Where things are stored

Everything is under `$EVENT_LOG_HOME` (default `~/.local/share/sf-event-logs`),
outside the plugin, so updating or reinstalling the plugin keeps it:

```
logs/<org>/<date>/   one CSV per event type, users.json, digest.md, review.md
known/<org>.md       patterns you have confirmed as normal for that org
```

Downloads older than 30 days are pruned (Salesforce keeps them for 30 days too).
The CSVs contain usernames, IP addresses and activity detail, so treat the folder as
sensitive.

### Teaching it what's normal

When the review flags something that turns out to be expected, add a line to
`known/<org>.md` under the right heading, e.g.

```
- copadopipeline@… deploys metadata daily via Copado — CI pipeline user (added 2026-10-06)
```

Later reviews won't raise it again unless it changes materially (volume, user or scope).

## Run it daily

Example crontab entry (04:00 UTC, after the files are generated). Cron has a minimal
`PATH`, so include the directories holding `sf`, `node`, `jq` and `claude`:

```cron
0 4 * * * PATH=/home/me/.nvm/versions/node/v24.21.0/bin:/home/me/.local/bin:/usr/bin:/bin EVENT_LOG_ORG=my-org claude -p "/sf-event-logs:event-log-review" >> ~/.local/share/sf-event-logs/cron.log 2>&1
```

The skill pre-approves the tools it needs: its own `run.sh`, `jq`, reading files, and
writing under `~/.local/share/sf-event-logs`. The Python commands it sometimes uses to
dig into CSVs are not pre-approved, because that would allow any code; in a headless
run they are skipped unless you add `--allowedTools "Bash(python3 *)"` or use
`--permission-mode auto`. If you set a custom `EVENT_LOG_HOME`, add a matching
`--allowedTools "Write(<path>/**)"` so the review can be saved.

## Development

```
claude --plugin-dir .                                   # try local changes without installing
claude plugin validate . --strict                       # check the manifests
skills/event-log-review/scripts/run.sh my-org           # run the pipeline on its own
```

Layout:

```
.claude-plugin/plugin.json        plugin manifest
.claude-plugin/marketplace.json   this repo as a one-plugin marketplace
skills/event-log-review/
  SKILL.md                        how Claude reviews and rates findings
  scripts/run.sh                  pipeline: download, backfill baseline, prune, digest
  scripts/download.sh             fetch one day's EventLogFiles + users/login geo/org info
  scripts/digest.py               condense CSVs into digest.md vs the baseline
  known/_template.md              starting point for known/<org>.md
```

Bump `version` in both manifests when releasing, so installed copies pick up the update.
