#!/usr/bin/env python3
"""Condense one day of downloaded EventLogFile CSVs into a markdown digest.

Usage: scripts/digest.py [YYYY-MM-DD] [--org ALIAS] [--baseline-days N]

Reads $EVENT_LOG_HOME/logs/<org>/<date>/*.csv (from download.sh) and compares against up to N
earlier days already present under logs/<org>/ (default 7). Prints markdown to stdout.
The digest states facts and flags deviations; judging what matters is left
to the reader (the event-log-review skill).
"""
import argparse
import csv
import json
import os
import statistics
import sys
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

DATA = Path(os.environ.get("EVENT_LOG_HOME")
            or Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share") / "sf-event-logs")
LOGS = DATA / "logs"
csv.field_size_limit(sys.maxsize)

# Working hours (UTC) used to flag out-of-hours interactive logins.
WORK_START, WORK_END = 6, 20

# Login statuses that are a normal step of a successful sign-in, not a failure.
OK_LOGIN = {"LOGIN_NO_ERROR", ""}
CHALLENGE_LOGIN = {"LOGIN_TWOFACTOR_REQ", "LOGIN_CHALLENGE_ISSUED", "LOGIN_CHALLENGE_PENDING",
                   "LOGIN_LIGHTNING_LOGIN_REQ"}


def login_failed(row):
    return row.get("LOGIN_STATUS") not in OK_LOGIN | CHALLENGE_LOGIN


def load_day(day):
    d = LOGS / day
    data = {}
    for f in sorted(d.glob("*.csv")):
        etype = f.stem.split("_")[0]
        with f.open(newline="") as fh:
            data.setdefault(etype, []).extend(csv.DictReader(fh))
    return data


def load_users(day):
    f = LOGS / day / "users.json"
    users = {}
    if f.exists():
        for u in json.loads(f.read_text()):
            users[u["Id"]] = u
            users[u["Id"][:15]] = u
    return users


def load_geo(day):
    """IP -> country from LoginHistory (download.sh); None if it wasn't fetched."""
    f = LOGS / day / "login_geo.json"
    if not f.exists():
        return None
    return {g["SourceIp"]: g.get("Country") or "?" for g in json.loads(f.read_text()) if g.get("SourceIp")}


def num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def hour(row):
    ts = row.get("TIMESTAMP_DERIVED") or ""
    return int(ts[11:13]) if len(ts) >= 13 else None


class Digest:
    def __init__(self, day, baseline_days):
        self.day = day
        self.today = load_day(day)
        self.users = load_users(day)
        self.geo = load_geo(day)
        start = date.fromisoformat(day)
        self.base_days = [
            d for d in ((start - timedelta(days=i)).isoformat() for i in range(1, baseline_days + 1))
            if (LOGS / d).is_dir()
        ]
        self.base = {d: load_day(d) for d in self.base_days}
        self.base_geo = {d: load_geo(d) or {} for d in self.base_days}
        self.out = []

    # ---------- helpers ----------
    def who(self, uid):
        u = self.users.get(uid or "")
        if not u:
            return uid or "(none)"
        flag = "" if u.get("IsActive") else " [inactive]"
        return f"{u['Username']} ({u.get('Profile') or u.get('UserType')}){flag}"

    def uid(self, row):
        return row.get("USER_ID_DERIVED") or row.get("USER_ID") or ""

    def rows(self, etype):
        return self.today.get(etype, [])

    def base_rows(self, etype):
        for d in self.base_days:
            yield from self.base[d].get(etype, [])

    def base_set(self, etype, key):
        return {key(r) for r in self.base_rows(etype)}

    def p(self, line=""):
        self.out.append(line)

    def table(self, headers, rows, limit=10):
        rows = list(rows)
        if not rows:
            return
        if self.out and self.out[-1] != "":
            self.p()
        self.p("| " + " | ".join(headers) + " |")
        self.p("|" + "---|" * len(headers))
        for r in rows[:limit]:
            self.p("| " + " | ".join(str(c).replace("|", "/").replace("\n", " ")[:160] for c in r) + " |")
        if len(rows) > limit:
            self.p(f"| … {len(rows) - limit} more | " + " | " * (len(headers) - 1))
        self.p()

    # ---------- sections ----------
    def volume(self):
        self.p("## Volume by event type (rows vs baseline)")
        self.p()
        types = sorted(set(self.today) | {t for d in self.base.values() for t in d})
        out = []
        for t in types:
            n = len(self.today.get(t, []))
            hist = [len(self.base[d].get(t, [])) for d in self.base_days]
            mean = statistics.median(hist) if hist else 0  # median: one outlier day shouldn't skew it
            note = ""
            if t not in self.today:
                note = "absent today"
            elif hist and not any(hist):
                note = "NEW (not seen in baseline)"
            elif mean and n > 3 * mean and n - mean > 20:
                note = f"SPIKE x{n / mean:.1f}"
            elif mean and n < mean / 3 and mean - n > 20:
                note = f"DROP x{n / mean:.2f}"
            out.append((t, n, f"{mean:.0f}", note))
        self.table(["Event type", "Today", "Baseline median", "Note"], out, limit=100)

    def logins(self):
        rows = self.rows("Login")
        if not rows:
            return
        self.p("## Logins")
        status = Counter(r.get("LOGIN_STATUS") for r in rows)
        self.p(f"- {len(rows)} logins; status: {dict(status)}")
        failed = [r for r in rows if login_failed(r)]
        if failed:
            self.p("- **Failed logins** by user / status / IP:")
            c = Counter((r.get("USER_NAME"), r.get("LOGIN_STATUS"), r.get("SOURCE_IP") or r.get("CLIENT_IP")) for r in failed)
            self.table(["User", "Status", "IP", "Count"], [(*k, v) for k, v in c.most_common()])
        ip_of = lambda r: r.get("SOURCE_IP") or r.get("CLIENT_IP")
        seen_ip = self.base_set("Login", lambda r: (r.get("USER_NAME"), ip_of(r)))
        seen_cc = {(r.get("USER_NAME"), self.country(r, self.base_geo[d]))
                   for d in self.base_days for r in self.base[d].get("Login", [])}
        seen_users = self.base_set("Login", lambda r: r.get("USER_NAME"))
        new = []
        for r in rows:
            u, ip, cc = r.get("USER_NAME"), ip_of(r), self.country(r, self.geo)
            notes = []
            usr = self.users.get(self.uid(r))
            if usr and not usr.get("IsActive"):
                notes.append("INACTIVE user")
            if u not in seen_users:
                notes.append("user not seen in baseline")
            elif cc and (u, cc) not in seen_cc:
                notes.append(f"new country {cc}")
            elif (u, ip) not in seen_ip:
                notes.append("new IP")
            h = hour(r)
            if h is not None and not (WORK_START <= h < WORK_END) and r.get("LOGIN_TYPE") in ("i", "Application"):
                notes.append(f"out of hours ({h:02d}:xx UTC)")
            if notes:
                new.append((u, cc, ip, r.get("LOGIN_TYPE"), r.get("TIMESTAMP_DERIVED", "")[:19], "; ".join(notes)))
        if new:
            self.p(f"- Logins worth a look (baseline: {len(self.base_days)} days):")
            self.table(["User", "Country", "IP", "Type", "Time UTC", "Why"], new, limit=25)
        # Success after repeated failures for the same user (possible guessed password)
        fails, burst = Counter(), []
        for r in sorted(rows, key=lambda r: r.get("TIMESTAMP_DERIVED", "")):
            u = r.get("USER_NAME")
            if login_failed(r):
                fails[u] += 1
            elif r.get("LOGIN_STATUS") in OK_LOGIN:
                if fails[u] >= 3:
                    burst.append((u, fails[u], ip_of(r), r.get("TIMESTAMP_DERIVED", "")[:19]))
                fails[u] = 0
        if burst:
            self.p("- **Successful login after 3+ consecutive failures:**")
            self.table(["User", "Failures before", "IP", "Success time UTC"], burst)
        if self.geo is None:
            self.p("- Countries: unavailable (LoginHistory not fetched for this day)")
        else:
            self.p(f"- Countries: {dict(Counter(self.country(r, self.geo) or '?' for r in rows))}")
        self.p()

    def country(self, row, geo):
        ip = row.get("SOURCE_IP") or row.get("CLIENT_IP")
        return row.get("COUNTRY_CODE") or (geo or {}).get(ip)

    def api(self):
        rows = self.rows("ApiTotalUsage")
        if not rows:
            return
        self.p("## API usage (ApiTotalUsage)")
        self.p(f"- {len(rows)} calls; status codes: {dict(Counter(r.get('STATUS_CODE') for r in rows))}")
        c = Counter((r.get("USER_NAME"), r.get("CONNECTED_APP_NAME") or r.get("CLIENT_NAME"), r.get("API_FAMILY")) for r in rows)
        base_c = Counter((r.get("USER_NAME"), r.get("CONNECTED_APP_NAME") or r.get("CLIENT_NAME"), r.get("API_FAMILY")) for r in self.base_rows("ApiTotalUsage"))
        nb = max(len(self.base_days), 1)
        out = []
        for k, v in c.most_common():
            avg = base_c.get(k, 0) / nb
            note = "NEW caller" if k not in base_c and self.base_days else (f"x{v / avg:.1f}" if avg and v > 3 * avg and v > 50 else "")
            out.append((*k, v, f"{avg:.0f}", note))
        self.table(["User", "App/Client", "Family", "Calls", "Baseline avg", "Note"], out, limit=15)
        # download.sh itself queries/downloads EventLogFile; label it so it isn't read as a spike.
        own = Counter(r.get("USER_NAME") for r in rows if r.get("ENTITY_NAME") == "EventLogFile"
                      or "/EventLogFile/" in (r.get("API_RESOURCE") or ""))
        if own:
            self.p(f"- EventLogFile calls (likely this review pipeline downloading logs): {dict(own)}")
        errs = Counter((r.get("USER_NAME"), r.get("STATUS_CODE"), r.get("API_RESOURCE")) for r in rows if r.get("STATUS_CODE", "")[:1] in ("4", "5"))
        if errs:
            self.p("- Error responses:")
            self.table(["User", "Status", "Resource", "Count"], [(*k, v) for k, v in errs.most_common()], limit=10)
        ips = self.base_set("ApiTotalUsage", lambda r: (r.get("USER_NAME"), r.get("CLIENT_IP")))
        new_ips = Counter((r.get("USER_NAME"), r.get("CLIENT_IP")) for r in rows if (r.get("USER_NAME"), r.get("CLIENT_IP")) not in ips)
        if new_ips and self.base_days:
            self.p("- API calls from IPs not seen for that user in baseline:")
            self.table(["User", "IP", "Calls"], [(*k, v) for k, v in new_ips.most_common()], limit=10)

    def data_volume(self):
        out = []
        for etype in ("API", "RestApi", "BulkApi", "BulkApi2", "ReportExport", "Report"):
            rows = self.rows(etype)
            if not rows:
                continue
            per_user = defaultdict(lambda: [0, 0.0, Counter()])
            for r in rows:
                rec = per_user[self.uid(r)]
                rec[0] += 1
                rec[1] += num(r.get("ROWS_PROCESSED") or r.get("ROW_COUNT"))
                rec[2][r.get("ENTITY_NAME") or r.get("REPORT_ID_DERIVED") or r.get("METHOD_NAME") or ""] += 1
            for u, (n, rp, ents) in sorted(per_user.items(), key=lambda kv: -kv[1][1]):
                out.append((etype, self.who(u), n, int(rp), ", ".join(f"{e}:{c}" for e, c in ents.most_common(4))))
        if out:
            self.p("## Data access volume (rows processed per user)")
            self.p()
            self.table(["Event", "User", "Requests", "Rows processed", "Top entities"], out, limit=15)
        exports = self.rows("ReportExport")
        if exports:
            self.p(f"- **{len(exports)} report export(s)**:")
            self.table(["User", "Report", "Time"], [(self.who(self.uid(r)), r.get("REPORT_ID_DERIVED") or r.get("REPORT_ID"), r.get("TIMESTAMP_DERIVED", "")[:19]) for r in exports], limit=20)

    def permissions(self):
        rows = self.rows("PermissionUpdate")
        if not rows:
            return
        self.p("## Permission changes (PermissionUpdate)")
        c = Counter((self.who(self.uid(r)), r.get("PERMISSION_TYPE"), r.get("UPDATE_TYPE"), r.get("FEATURE_ID")) for r in rows)
        self.p(f"- {len(rows)} changes")
        self.table(["Changed by", "Permission type", "Update", "Feature (profile/permset Id)", "Count"], [(*k, v) for k, v in c.most_common()], limit=15)
        risky_words = ("ModifyAllData", "ViewAllData", "ManageUsers", "AuthorApex", "CustomizeApplication",
                       "ModifyMetadata", "ViewSetup", "ManageProfilesPermissionsets", "AssignPermissionSets",
                       "ViewAllRecords", "ModifyAllRecords", "PasswordNeverExpires", "ApiEnabled", "ExportReport")
        risky = [r for r in rows if any(w.lower() in (r.get("DESCRIPTION") or "").lower() for w in risky_words)]
        if risky:
            self.p("- **Changes touching high-privilege permissions:**")
            self.table(["Changed by", "Description", "Time"], [(self.who(self.uid(r)), r.get("DESCRIPTION"), r.get("TIMESTAMP_DERIVED", "")[:19]) for r in risky], limit=25)
        self.p("- Sample descriptions:")
        for d in list(dict.fromkeys(r.get("DESCRIPTION") for r in rows))[:8]:
            self.p(f"  - {d}")
        self.p()

    def metadata(self):
        rows = self.rows("MetadataApiOperation")
        pkgs = self.rows("PackageInstall")
        if not rows and not pkgs:
            return
        self.p("## Metadata & packages")
        if rows:
            c = Counter((self.who(self.uid(r)), r.get("OPERATION")) for r in rows)
            self.table(["User", "Operation", "Count"], [(*k, v) for k, v in c.most_common()], limit=12)
            base_users = self.base_set("MetadataApiOperation", lambda r: self.uid(r))
            new = {self.uid(r) for r in rows} - base_users
            if new and self.base_days:
                self.p(f"- Users doing metadata operations not seen in baseline: {', '.join(self.who(u) for u in new)}")
        for r in pkgs:
            ok = "succeeded" if r.get("IS_SUCCESSFUL") == "1" else f"FAILED ({r.get('FAILURE_TYPE')})"
            self.p(f"- Package {r.get('OPERATION_TYPE')}: {r.get('PACKAGE_NAME') or '(unnamed)'} {ok}, push={r.get('IS_PUSH')}, by {self.who(self.uid(r))} at {r.get('TIMESTAMP_DERIVED', '')[:19]}")
        self.p()

    def exceptions(self):
        rows = self.rows("ApexUnexpectedException")
        if not rows:
            return
        self.p("## Apex unexpected exceptions")
        key = lambda r: (r.get("APEX_ENTITY_NAME"), r.get("EXCEPTION_CATEGORY") or r.get("EXCEPTION_TYPE"))
        c = Counter(key(r) for r in rows)
        base = Counter(key(r) for r in self.base_rows("ApexUnexpectedException"))
        nb = max(len(self.base_days), 1)
        sample = {key(r): (r.get("EXCEPTION_MESSAGE") or "").strip('"') for r in rows}
        out = []
        for k, v in c.most_common():
            avg = base.get(k, 0) / nb
            note = "NEW" if k not in base and self.base_days else ("recurring" if avg else "")
            out.append((*k, v, f"{avg:.1f}", note, sample[k][:140]))
        self.table(["Class/Trigger", "Category", "Today", "Baseline avg", "Note", "Sample message"], out, limit=15)

    def flows_and_jobs(self):
        flows = self.rows("FlowExecution")
        errs = [r for r in flows if num(r.get("NUMBER_OF_ERRORS"))]
        if flows:
            self.p("## Flows & async jobs")
            self.p(f"- FlowExecution: {len(flows)} rows, {int(sum(num(r.get('NUMBER_OF_INTERVIEWS')) for r in flows))} interviews, {int(sum(num(r.get('NUMBER_OF_ERRORS')) for r in errs))} errors")
            if errs:
                c = Counter((r.get("FLOW_VERSION_ID"), r.get("PROCESS_TYPE")) for r in errs)
                self.table(["Flow version Id", "Type", "Rows with errors"], [(*k, v) for k, v in c.most_common()])
        q = self.rows("QueuedExecution")
        if q:
            bad = Counter(r.get("ENTRY_POINT") for r in q if r.get("REQUEST_STATUS") not in ("", "S"))
            self.p(f"- QueuedExecution: {len(q)} jobs; top entry points: {dict(Counter(r.get('ENTRY_POINT') for r in q).most_common(5))}")
            if bad:
                self.p(f"- Non-success queued jobs: {dict(bad)}")
        apex = self.rows("ApexExecution")
        if apex:
            slow = sorted(apex, key=lambda r: -num(r.get("RUN_TIME") or r.get("EXEC_TIME")))[:5]
            longrun = sum(1 for r in apex if r.get("IS_LONG_RUNNING_REQUEST") == "1")
            self.p(f"- ApexExecution: {len(apex)} executions, {longrun} long-running. Slowest:")
            self.table(["Entry point", "Quiddity", "Run ms", "CPU ms", "SOQL"], [
                (r.get("ENTRY_POINT"), r.get("QUIDDITY"), int(num(r.get("RUN_TIME") or r.get("EXEC_TIME"))), int(num(r.get("CPU_TIME"))), r.get("NUMBER_SOQL_QUERIES")) for r in slow])
        self.p()

    def web_errors(self):
        out = []
        for etype in ("Sites", "VisualforceRequest", "URI", "LightningError", "RestApi", "API"):
            rows = self.rows(etype)
            bad = [r for r in rows if r.get("IS_ERROR") == "1" or r.get("REQUEST_STATUS") in ("F", "U", "A", "R", "N")
                   or (r.get("STATUS_CODE", "")[:1] in ("4", "5")) or r.get("EXCEPTION_MESSAGE")]
            if bad:
                c = Counter((r.get("PAGE_NAME") or r.get("URI") or r.get("ENTITY_NAME") or r.get("MESSAGE") or "",
                             r.get("REQUEST_STATUS") or r.get("STATUS_CODE") or "") for r in bad)
                out += [(etype, *k, v) for k, v in c.most_common(5)]
            if etype == "Sites":
                guests = sum(1 for r in rows if r.get("IS_GUEST") == "1")
                if guests:
                    out.append(("Sites", "guest-user requests", "", guests))
        if out:
            self.p("## Request errors & guest access")
            self.p()
            self.table(["Event", "Page/URI/Entity", "Status", "Count"], out, limit=20)

    def encryption(self):
        rows = self.rows("PlatformEncryption")
        if rows:
            self.p("## Platform encryption key activity")
            self.p(f"- {dict(Counter((r.get('ACTION'), r.get('KEY_TYPE')) for r in rows))}")
            mgmt = [r for r in rows if any(w in (r.get("ACTION") or "") for w in ("Destroy", "Export", "Import", "Rotate", "Generate", "Delete"))]
            if mgmt:
                self.p(f"- **Key management actions:** {[(r.get('ACTION'), self.who(self.uid(r)), r.get('TIMESTAMP_DERIVED', '')[:19]) for r in mgmt]}")
            self.p()

    def unknown_types(self):
        handled = {"Login", "Logout", "ApiTotalUsage", "API", "RestApi", "BulkApi", "BulkApi2", "ReportExport", "Report",
                   "PermissionUpdate", "MetadataApiOperation", "PackageInstall", "ApexUnexpectedException",
                   "FlowExecution", "QueuedExecution", "ApexExecution", "Sites", "VisualforceRequest", "URI",
                   "LightningError", "PlatformEncryption", "DatabaseSave", "UniqueQuery", "OneCommerceUsage"}
        other = [t for t in self.today if t not in handled]
        if other:
            self.p("## Other event types present (not specifically analysed)")
            for t in other:
                rows = self.rows(t)
                self.p(f"- {t}: {len(rows)} rows; top users: {[(self.who(u), n) for u, n in Counter(self.uid(r) for r in rows).most_common(3)]}")
            self.p()

    def dml(self):
        rows = self.rows("DatabaseSave")
        if not rows:
            return
        c = Counter()
        for r in rows:
            c[(self.who(self.uid(r)), r.get("DML_TYPE"), r.get("KEY_PREFIX"))] += int(num(r.get("NUM_ROWS")) * max(num(r.get("SAMPLE_FACTOR")), 1))
        deletes = [(k, v) for k, v in c.items() if (k[1] or "").lower() in ("delete", "d", "hard_delete")]
        self.p("## DML volume (DatabaseSave, sample-adjusted)")
        self.table(["User", "DML", "Key prefix", "≈Rows"], [(*k, v) for k, v in c.most_common()], limit=10)
        if deletes:
            self.p(f"- Deletes: {sorted([(*k, v) for k, v in deletes], key=lambda x: -x[-1])[:10]}")
            self.p()

    def render(self):
        mf = LOGS / self.day / "manifest.json"
        files = json.loads(mf.read_text()) if mf.exists() else {}
        info = files.get("orgInfo") or {}
        self.p(f"# Event log digest — {files.get('org', LOGS.name)} — {self.day} ({date.fromisoformat(self.day):%A})")
        self.p()
        if info:
            kind = "Sandbox" if info.get("IsSandbox") else f"Production ({info.get('OrganizationType')})"
            self.p(f"Org: {info.get('Name')} · {kind} · instance {info.get('InstanceName')} · Id {info.get('Id')}")
        if not files.get("files"):
            self.p("**No event log files for this day** — Salesforce generated none, i.e. no recorded activity.")
        self.p()
        self.p("Baseline days available locally: "
               + (", ".join(f"{d} ({date.fromisoformat(d):%a})" for d in self.base_days) or "none"))
        self.p()
        for section in (self.volume, self.logins, self.permissions, self.api, self.data_volume, self.metadata,
                        self.exceptions, self.flows_and_jobs, self.web_errors, self.encryption, self.dml, self.unknown_types):
            section()
        return "\n".join(self.out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("day", nargs="?", default=(datetime.now(timezone.utc).date() - timedelta(days=1)).isoformat())
    ap.add_argument("--org", required=True)
    ap.add_argument("--baseline-days", type=int, default=7)
    a = ap.parse_args()
    global LOGS
    LOGS = LOGS / a.org
    if not (LOGS / a.day).is_dir():
        sys.exit(f"No downloaded logs for {a.day} under {LOGS}. Run scripts/run.sh {a.day} {a.org} first.")
    print(Digest(a.day, a.baseline_days).render())


if __name__ == "__main__":
    main()
