#!/usr/bin/env python3
"""
pace.py — universal solo-developer pace heuristic CLI.

A self-training estimator: it reads each developer's own git + Claude
Code session history, derives their personal pace tiers (slow / median /
fast), and learns bias correction from past forecast outcomes so future
estimates converge on reality over time.

Commands
--------
  calibrate    Measure pace tiers from this repo's git history + Claude
               session transcripts. Writes calibration.json.
  start        Lock a forecast for an upcoming epic. Writes forecast.json.
  status       Compare current state to the active forecast.
  complete     Close the active forecast, record actual outcome to history,
               update the global profile so future estimates are bias-corrected.
  cancel       Discard the active forecast without recording an outcome.
  history      List past forecasts (per-repo or --global).
  profile      Show cross-repo accuracy summary + current bias factors.

Storage
-------
  <repo>/.pace/data/
    calibration.json     per-repo measurement
    forecast.json        active forecast
    history.jsonl        completed forecasts on this repo

  ~/.claude/skills/pace/data/
    profile.json         cross-repo accuracy summary (auto-rebuilt)
    history.jsonl        completed forecasts across all repos

EP signals
----------
  commits        weighted by lines-changed (0.4 / 1.0 / 2.0 / 3.0)
  markdown       1.0 EP per 100 lines added in window
  sub-agents     weighted by transcript duration (0.1 / 0.3 / 0.6)

Pace tiers (personal, derived from your own active weeks)
---------
  slow           25th percentile of EP/active-day
  median         50th percentile
  fast           75th percentile

There are no hard-coded "anchors" — your tiers come from your own data.
"""

from __future__ import annotations
import argparse
import datetime as dt
import glob
import json
import os
import re
import statistics
import subprocess
import sys
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

GAP_BOUNDARY = dt.timedelta(minutes=30)

# Commit-size buckets (lines changed = additions + deletions).
COMMIT_BUCKETS = [
    (20, 0.4),    # typo / tweak
    (200, 1.0),   # typical commit
    (800, 2.0),   # substantial change
    (10**9, 3.0), # refactor / scaffold
]

# Sub-agent duration buckets (seconds → EP).
SUBAGENT_BUCKETS = [
    (5 * 60, 0.1),    # quick lookup
    (20 * 60, 0.3),   # medium exploration
    (10**9, 0.6),     # heavy multi-step
]

# Fallback tiers used only when the user has no calibration yet.
# Conservative — assumes flat 1 EP/commit at typical sizes.
FALLBACK_TIERS = {"slow": 3.0, "median": 6.0, "fast": 10.0}


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

def repo_root() -> Path:
    """Return git repo root (falls back to cwd if not a git repo)."""
    try:
        out = subprocess.check_output(
            ["git", "rev-parse", "--show-toplevel"], text=True, stderr=subprocess.DEVNULL
        ).strip()
        return Path(out)
    except subprocess.CalledProcessError:
        return Path.cwd()


def repo_data_dir() -> Path:
    d = repo_root() / ".pace" / "data"
    d.mkdir(parents=True, exist_ok=True)
    return d


def global_data_dir() -> Path:
    d = Path.home() / ".claude" / "skills" / "pace" / "data"
    d.mkdir(parents=True, exist_ok=True)
    return d


def calibration_path() -> Path: return repo_data_dir() / "calibration.json"
def forecast_path() -> Path:    return repo_data_dir() / "forecast.json"
def history_path() -> Path:     return repo_data_dir() / "history.jsonl"
def global_profile_path() -> Path: return global_data_dir() / "profile.json"
def global_history_path() -> Path: return global_data_dir() / "history.jsonl"


def claude_project_dir(repo: Path) -> Path | None:
    """Derive the Claude project directory from the repo path."""
    encoded = str(repo).replace("/", "-").replace(" ", "-")
    candidate = Path.home() / ".claude" / "projects" / encoded
    return candidate if candidate.exists() else None


# ---------------------------------------------------------------------------
# Generic helpers
# ---------------------------------------------------------------------------

def _parse_iso(s: str) -> dt.datetime:
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))


def _bucket_value(value: float, buckets: list[tuple[float, float]]) -> float:
    for ceiling, weight in buckets:
        if value <= ceiling:
            return weight
    return buckets[-1][1]


def _git(*args: str, repo: Path | None = None) -> str:
    cmd = ["git"]
    if repo:
        cmd += ["-C", str(repo)]
    cmd += list(args)
    try:
        return subprocess.check_output(cmd, text=True, stderr=subprocess.DEVNULL)
    except subprocess.CalledProcessError:
        return ""


def git_default_author(repo: Path | None = None) -> str:
    """The repo's configured git user.name — the default 'who am I' for author filtering.
    On a SHARED repo, pace must count only your own commits, not the whole team's."""
    return _git("config", "user.name", repo=repo).strip()


def append_jsonl(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        f.write(json.dumps(record) + "\n")


def read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


# ---------------------------------------------------------------------------
# Sessions from Claude transcripts
# ---------------------------------------------------------------------------

def gather_timestamps(claude_dir: Path) -> list[dt.datetime]:
    ts = []
    for p in glob.glob(str(claude_dir / "**" / "*.jsonl"), recursive=True):
        try:
            with open(p) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        d = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if d.get("timestamp"):
                        try:
                            ts.append(_parse_iso(d["timestamp"]))
                        except ValueError:
                            pass
        except OSError:
            continue
    ts.sort()
    return ts


def compute_sessions(timestamps: list[dt.datetime], gap: dt.timedelta = GAP_BOUNDARY):
    """Return list of (start, end, duration_seconds)."""
    if not timestamps:
        return []
    sessions = []
    ss = se = timestamps[0]
    for t in timestamps[1:]:
        if t - se > gap:
            sessions.append((ss, se, (se - ss).total_seconds()))
            ss = t
        se = t
    sessions.append((ss, se, (se - ss).total_seconds()))
    return sessions


def filter_sessions(sessions, since: dt.datetime | None = None, until: dt.datetime | None = None):
    out = []
    for ss, se, d in sessions:
        if since and ss < since:
            continue
        if until and ss > until:
            continue
        out.append((ss, se, d))
    return out


# ---------------------------------------------------------------------------
# EP signals (weighted, repo-agnostic)
# ---------------------------------------------------------------------------

def commit_records(repo: Path, since: str | None = None, until: str | None = None,
                   pattern: str | None = None, branch: str | None = None,
                   author: str | None = None) -> list[dict]:
    """Return [{sha, message, lines, ep, day}] for non-merge commits in window.
    When `author` is set, only that author's commits are counted (shared-repo safety)."""
    args = ["log", "--no-merges", "--numstat", "--pretty=format:COMMIT %H|%ad|%s",
            "--date=iso-strict"]
    if author: args.append(f"--author={author}")
    if since: args.append(f"--since={since}")
    if until: args.append(f"--until={until}")
    if branch: args.append(branch)
    out = _git(*args, repo=repo)
    if not out.strip():
        return []
    records = []
    cur = None
    for line in out.splitlines():
        if line.startswith("COMMIT "):
            if cur:
                records.append(cur)
            try:
                _, payload = line.split(" ", 1)
                sha, date_s, msg = payload.split("|", 2)
            except ValueError:
                continue
            cur = {"sha": sha, "date": date_s, "message": msg, "lines": 0, "files": 0}
        elif cur and line.strip():
            parts = line.split("\t")
            if len(parts) >= 2:
                add_s, del_s = parts[0], parts[1]
                add = int(add_s) if add_s.isdigit() else 0
                rem = int(del_s) if del_s.isdigit() else 0
                cur["lines"] += add + rem
                cur["files"] += 1
    if cur:
        records.append(cur)

    if pattern:
        rx = re.compile(pattern, re.IGNORECASE)
        records = [r for r in records if rx.search(r["message"])]

    for r in records:
        r["ep"] = _bucket_value(r["lines"], COMMIT_BUCKETS)
        try:
            r["day"] = _parse_iso(r["date"]).astimezone().strftime("%Y-%m-%d")
        except ValueError:
            r["day"] = r["date"][:10]
    return records


def markdown_lines_added(repo: Path, since: str | None = None, until: str | None = None,
                         author: str | None = None) -> int:
    """Lines added to *.md files in window (deletions don't count).
    When `author` is set, only that author's doc lines are counted."""
    args = ["log", "--no-merges", "--numstat", "--pretty=format:"]
    if author: args.append(f"--author={author}")
    if since: args.append(f"--since={since}")
    if until: args.append(f"--until={until}")
    args += ["--", "*.md"]
    out = _git(*args, repo=repo)
    total = 0
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split("\t")
        if parts and parts[0].isdigit():
            total += int(parts[0])
    return total


def subagent_records(claude_dir: Path | None,
                     since: dt.datetime | None = None,
                     until: dt.datetime | None = None) -> list[dict]:
    """Return [{path, duration_sec, ep, ts}] for sub-agent transcripts in window."""
    if not claude_dir:
        return []
    out = []
    for p in glob.glob(str(claude_dir / "**" / "subagents" / "agent-*.jsonl"), recursive=True):
        mtime = dt.datetime.fromtimestamp(os.path.getmtime(p), dt.timezone.utc)
        if since and mtime < since: continue
        if until and mtime > until: continue
        # Duration: first to last timestamp in transcript
        first = last = None
        try:
            with open(p) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        d = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    ts = d.get("timestamp")
                    if not ts:
                        continue
                    try:
                        t = _parse_iso(ts)
                    except ValueError:
                        continue
                    if first is None: first = t
                    last = t
        except OSError:
            continue
        dur = (last - first).total_seconds() if first and last else 0.0
        out.append({
            "path": p,
            "duration_sec": dur,
            "ep": _bucket_value(dur, SUBAGENT_BUCKETS),
            "ts": mtime,
        })
    return out


def total_ep(commits: list[dict], md_lines: int, subagents: list[dict]) -> dict:
    commits_ep = sum(r["ep"] for r in commits)
    md_ep = md_lines / 100.0
    sub_ep = sum(r["ep"] for r in subagents)
    return {
        "total": round(commits_ep + md_ep + sub_ep, 2),
        "commits_ep": round(commits_ep, 2),
        "doc_ep": round(md_ep, 2),
        "subagents_ep": round(sub_ep, 2),
        "raw": {
            "commits": len(commits),
            "doc_lines": md_lines,
            "subagent_runs": len(subagents),
        },
    }


# ---------------------------------------------------------------------------
# Personal pace tiers
# ---------------------------------------------------------------------------

def derive_personal_tiers(commits: list[dict], md_lines_per_day: dict[str, int],
                          subagents: list[dict]) -> dict:
    """Compute slow/median/fast tiers from per-active-day EP totals."""
    by_day: dict[str, float] = {}
    for r in commits:
        by_day[r["day"]] = by_day.get(r["day"], 0.0) + r["ep"]
    for day, lines in md_lines_per_day.items():
        by_day[day] = by_day.get(day, 0.0) + lines / 100.0
    for r in subagents:
        day = r["ts"].astimezone().strftime("%Y-%m-%d")
        by_day[day] = by_day.get(day, 0.0) + r["ep"]
    if not by_day:
        return dict(FALLBACK_TIERS, samples=0, source="fallback")
    values = sorted(by_day.values())
    if len(values) < 3:
        # Too few days for percentiles — collapse to a single observed pace.
        m = statistics.median(values)
        return {"slow": round(m, 2), "median": round(m, 2), "fast": round(m, 2),
                "samples": len(values), "source": "single"}
    return {
        "slow": round(statistics.quantiles(values, n=4)[0], 2),
        "median": round(statistics.median(values), 2),
        "fast": round(statistics.quantiles(values, n=4)[2], 2),
        "samples": len(values),
        "source": "personal",
    }


def md_lines_by_day(repo: Path, since: str | None = None, until: str | None = None,
                    author: str | None = None) -> dict[str, int]:
    args = ["log", "--no-merges", "--numstat", "--pretty=format:DAY %ad",
            "--date=short"]
    if author: args.append(f"--author={author}")
    if since: args.append(f"--since={since}")
    if until: args.append(f"--until={until}")
    args += ["--", "*.md"]
    out = _git(*args, repo=repo)
    by_day: dict[str, int] = {}
    cur_day = None
    for line in out.splitlines():
        if line.startswith("DAY "):
            cur_day = line[4:].strip()
        elif cur_day and line.strip():
            parts = line.split("\t")
            if parts and parts[0].isdigit():
                by_day[cur_day] = by_day.get(cur_day, 0) + int(parts[0])
    return by_day


# ---------------------------------------------------------------------------
# Cross-repo profile (bias correction)
# ---------------------------------------------------------------------------

def rebuild_profile() -> dict:
    """Rebuild profile.json from global history.jsonl.

    Bias correction is computed only over forecast-mode units (those with a
    real ex-ante estimate). Track-mode units feed the personal-tier history
    via `report` but never bias-correct an estimate that didn't exist.
    """
    history = read_jsonl(global_history_path())
    forecast_units = [h for h in history
                      if h.get("ep_actual") is not None
                      and h.get("mode", "forecast") == "forecast"
                      and h.get("ep_estimate")]
    track_units = [h for h in history
                   if h.get("ep_actual") is not None
                   and h.get("mode") == "track"]
    profile = {
        "forecast_samples": len(forecast_units),
        "track_samples": len(track_units),
        # Back-compat alias for callers that read `samples`.
        "samples": len(forecast_units),
        "ep_bias_median": 1.0,
        "ep_bias_iqr": [1.0, 1.0],
        "days_bias_median": 1.0,
        "days_bias_iqr": [1.0, 1.0],
        "last_updated": dt.datetime.now(dt.timezone.utc).isoformat(),
        "anchor_usage": {},
    }
    if forecast_units:
        ep_ratios = [h["ep_actual"] / h["ep_estimate"] for h in forecast_units]
        day_ratios = [h["days_actual"] / h["days_central"] for h in forecast_units
                      if h.get("days_central")]
        if ep_ratios:
            profile["ep_bias_median"] = round(statistics.median(ep_ratios), 3)
            if len(ep_ratios) >= 4:
                qs = statistics.quantiles(ep_ratios, n=4)
                profile["ep_bias_iqr"] = [round(qs[0], 3), round(qs[2], 3)]
        if day_ratios:
            profile["days_bias_median"] = round(statistics.median(day_ratios), 3)
            if len(day_ratios) >= 4:
                qs = statistics.quantiles(day_ratios, n=4)
                profile["days_bias_iqr"] = [round(qs[0], 3), round(qs[2], 3)]
        for h in forecast_units:
            a = h.get("anchor_used", "unknown")
            profile["anchor_usage"][a] = profile["anchor_usage"].get(a, 0) + 1
    global_profile_path().write_text(json.dumps(profile, indent=2))
    return profile


def load_profile() -> dict:
    p = global_profile_path()
    if not p.exists():
        return rebuild_profile()
    try:
        return json.loads(p.read_text())
    except json.JSONDecodeError:
        return rebuild_profile()


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def cmd_calibrate(args) -> None:
    repo = repo_root()
    claude = claude_project_dir(repo)
    # Author filtering (shared-repo safety): default to this repo's git user, override with --author,
    # or pass --all-authors to count the whole repo (the original whole-repo behavior).
    author = None if getattr(args, "all_authors", False) else (getattr(args, "author", None) or git_default_author(repo))
    print(f"Repo:           {repo}")
    print(f"Claude project: {claude or '(none — running with git signals only)'}")
    print(f"Author filter:  {author or '(all authors — whole repo)'}")

    timestamps = gather_timestamps(claude) if claude else []
    sessions = compute_sessions(timestamps)
    dedication_sec = sum(s[2] for s in sessions)
    natural_days = len({s[0].astimezone().strftime("%Y-%m-%d") for s in sessions})

    commits = commit_records(repo, author=author)
    md_total = markdown_lines_added(repo, author=author)
    md_by_day = md_lines_by_day(repo, author=author)
    subs = subagent_records(claude)
    ep = total_ep(commits, md_total, subs)

    tiers = derive_personal_tiers(commits, md_by_day, subs)
    ep_per_natural = ep["total"] / natural_days if natural_days else 0
    ep_per_hour = ep["total"] / (dedication_sec / 3600) if dedication_sec else 0

    out = {
        "repo": str(repo),
        "author": author,
        "calibrated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "first_event": timestamps[0].isoformat() if timestamps else None,
        "last_event": timestamps[-1].isoformat() if timestamps else None,
        "dedication_hours": round(dedication_sec / 3600, 2),
        "natural_days": natural_days,
        "session_count": len(sessions),
        "ep": ep,
        "pace_observed": {
            "ep_per_natural_day": round(ep_per_natural, 2),
            "ep_per_dedication_hour": round(ep_per_hour, 2),
        },
        "personal_tiers": tiers,
    }
    calibration_path().write_text(json.dumps(out, indent=2))

    profile = load_profile()

    print()
    print(f"Sessions:           {len(sessions)}")
    print(f"Dedication time:    {dedication_sec/3600:.1f} hours")
    print(f"Natural days:       {natural_days}")
    print(f"EP delivered:       {ep['total']:.1f}  (commits {ep['commits_ep']}, "
          f"docs {ep['doc_ep']}, sub-agents {ep['subagents_ep']})")
    print(f"  raw: {ep['raw']['commits']} commits / "
          f"{ep['raw']['doc_lines']} md lines / "
          f"{ep['raw']['subagent_runs']} sub-agent runs")
    print(f"EP / natural day:   {ep_per_natural:.2f}")
    print(f"EP / dedication h:  {ep_per_hour:.2f}")
    print()
    print(f"Personal pace tiers (EP/active-day, source: {tiers['source']}):")
    print(f"  slow:    {tiers['slow']}")
    print(f"  median:  {tiers['median']}")
    print(f"  fast:    {tiers['fast']}")
    print()
    if profile["samples"] > 0:
        print(f"Cross-repo profile: {profile['samples']} completed forecast(s) tracked")
        print(f"  EP bias median:   {profile['ep_bias_median']:.2f}× "
              f"(IQR {profile['ep_bias_iqr'][0]:.2f}–{profile['ep_bias_iqr'][1]:.2f})")
        print(f"  Days bias median: {profile['days_bias_median']:.2f}×")
    else:
        print("Cross-repo profile: no completed forecasts yet — bias correction "
              "starts after the first /pace complete.")
    print()
    print(f"Wrote {calibration_path().relative_to(repo)}")


def _select_pace(args, cal: dict) -> tuple[float, str]:
    if args.pace_value is not None:
        return args.pace_value, f"custom={args.pace_value}"
    tiers = cal.get("personal_tiers", FALLBACK_TIERS)
    if args.pace in ("slow", "median", "fast"):
        return tiers.get(args.pace, FALLBACK_TIERS[args.pace]), args.pace
    # Default: median tier
    return tiers.get("median", FALLBACK_TIERS["median"]), "median"


def cmd_start(args) -> None:
    repo = repo_root()
    cp = calibration_path()
    if not cp.exists():
        print("ERROR: run `pace.py calibrate` first.", file=sys.stderr)
        sys.exit(1)

    cal = json.loads(cp.read_text())
    profile = load_profile()
    pace_value, pace_label = _select_pace(args, cal)

    raw_ep = args.ep
    bias = profile.get("ep_bias_median", 1.0)
    biased_ep = raw_ep * bias if args.bias else raw_ep
    natural_days = biased_ep / pace_value
    band_low = natural_days * 0.75
    band_high = natural_days * 1.25

    branch = _git("rev-parse", "--abbrev-ref", "HEAD", repo=repo).strip() or None

    forecast = {
        "name": args.name,
        "mode": "forecast",
        "created_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "ep_estimate_raw": raw_ep,
        "ep_estimate": round(biased_ep, 2),
        "ep_bias_applied": round(bias, 3) if args.bias else 1.0,
        "ep_breakdown": json.loads(args.breakdown) if args.breakdown else None,
        "anchor_used": pace_label,
        "pace_value": pace_value,
        "natural_days_central": round(natural_days, 1),
        "natural_days_band": [round(band_low, 1), round(band_high, 1)],
        "commit_filter": args.filter,
        "branch": branch,
        "calibration_at_start": cal.get("personal_tiers", {}),
        "profile_at_start": {
            "samples": profile["samples"],
            "ep_bias_median": profile["ep_bias_median"],
            "days_bias_median": profile["days_bias_median"],
        },
    }
    forecast_path().write_text(json.dumps(forecast, indent=2))

    print(f"Forecast locked: {args.name}")
    print(f"  EP estimate:      {raw_ep}", end="")
    if args.bias and abs(bias - 1.0) > 0.01:
        print(f" → biased {bias:.2f}× → {biased_ep:.1f}  "
              f"(based on {profile['samples']} past forecast(s))")
    else:
        print()
    print(f"  Pace tier:        {pace_label} ({pace_value} EP/active-day)")
    print(f"  Natural days:     {natural_days:.1f}  "
          f"(band {band_low:.1f}–{band_high:.1f})")
    if branch:
        print(f"  Branch:           {branch}")
    if args.filter:
        print(f"  Commit filter:    /{args.filter}/")
    print()
    print(f"Wrote {forecast_path().relative_to(repo)}")
    print("Commit this file to git to lock the prediction.")


def cmd_status(args) -> None:
    repo = repo_root()
    fp = forecast_path()
    if not fp.exists():
        print("ERROR: no active unit. Run `pace.py track <name>` (discovery) "
              "or `pace.py start ...` (forecast) first.", file=sys.stderr)
        sys.exit(1)

    unit = json.loads(fp.read_text())
    mode = unit.get("mode", "forecast")
    started = _parse_iso(unit["created_at"])
    now = dt.datetime.now(dt.timezone.utc)

    claude = claude_project_dir(repo)
    timestamps = gather_timestamps(claude) if claude else []
    all_sessions = compute_sessions(timestamps)
    window_sessions = filter_sessions(all_sessions, since=started, until=now)
    dedication_sec = sum(s[2] for s in window_sessions)
    natural_days = len({s[0].astimezone().strftime("%Y-%m-%d") for s in window_sessions})

    since_str = started.strftime("%Y-%m-%d %H:%M")
    commits = commit_records(repo, since=since_str,
                             pattern=unit.get("commit_filter"),
                             branch=unit.get("branch"))
    md_total = markdown_lines_added(repo, since=since_str)
    subs = subagent_records(claude, since=started, until=now)
    ep = total_ep(commits, md_total, subs)

    delivered = ep["total"]
    days_elapsed = (now - started).total_seconds() / 86400

    print(f"Mode:            {mode}")
    print(f"{'Forecast' if mode == 'forecast' else 'Tracking'}:        {unit['name']}")
    print(f"Started:         {started.strftime('%Y-%m-%d %H:%M %Z')}")
    print(f"Now:             {now.strftime('%Y-%m-%d %H:%M %Z')}")
    print(f"Calendar elapsed: {days_elapsed:.1f} days")
    print(f"Active days worked: {natural_days}")
    print(f"Dedication time:  {dedication_sec/3600:.1f} hours")
    print()
    print(f"EP Delivered:")
    print(f"  Total:          {delivered:.1f}")
    print(f"  Breakdown:      commits {ep['raw']['commits']} ({ep['commits_ep']} EP), "
          f"docs {ep['doc_ep']} EP, sub-agents {ep['raw']['subagent_runs']} ({ep['subagents_ep']} EP)")
    print()

    if mode == "track":
        # Discovery mode: no forecast to compare against. Just observation.
        observed_pace = delivered / natural_days if natural_days else 0
        print(f"Pace:")
        print(f"  Observed:       {observed_pace:.2f} EP/active-day")
        print()
        print(f"Run `pace ship` when done to close the unit and add it to history.")
        return

    # Forecast mode: full burn-down report.
    target = unit["ep_estimate"]
    remaining = max(0.0, target - delivered)
    pct_done = 100 * delivered / target if target else 0
    forecast_pace = unit["pace_value"]
    observed_pace = delivered / natural_days if natural_days else 0
    pace_ratio = observed_pace / forecast_pace if forecast_pace else 0
    days_remaining_at_forecast = remaining / forecast_pace if forecast_pace else float("inf")
    days_remaining_at_observed = remaining / observed_pace if observed_pace > 0 else float("inf")

    if pct_done >= 100:
        verdict = "✅ COMPLETE — run `pace ship` to record outcome"
    elif observed_pace == 0:
        verdict = "⚠ no measurable progress yet"
    elif pace_ratio >= 0.85:
        verdict = "✅ on track"
    elif pace_ratio >= 0.5:
        verdict = "⚠ behind forecast pace"
    else:
        verdict = "❌ significantly behind"

    print(f"Progress:        {delivered:.1f} / {target}  ({pct_done:.1f}%)")
    print(f"Remaining:       {remaining:.1f} EP")
    print()
    print(f"Pace:")
    print(f"  Forecast:       {forecast_pace} EP/active-day  ({unit['anchor_used']})")
    print(f"  Observed:       {observed_pace:.2f} EP/active-day  ({pace_ratio*100:.0f}% of forecast)")
    print()
    print(f"Time-to-finish:")
    print(f"  At forecast pace:  {days_remaining_at_forecast:.1f} more active days")
    if observed_pace > 0:
        print(f"  At observed pace:  {days_remaining_at_observed:.1f} more active days")
    print(f"  Original target:   {unit['natural_days_central']} active days "
          f"(band {unit['natural_days_band'][0]}–{unit['natural_days_band'][1]})")
    print()
    print(f"Verdict: {verdict}")


def cmd_track(args) -> None:
    """Start tracking a discovery-mode unit — no estimate required."""
    repo = repo_root()
    if forecast_path().exists():
        existing = json.loads(forecast_path().read_text())
        print(f"ERROR: already tracking '{existing['name']}' "
              f"(mode={existing.get('mode', 'forecast')}). "
              f"Run `pace ship` or `pace cancel` first.", file=sys.stderr)
        sys.exit(1)

    branch = _git("rev-parse", "--abbrev-ref", "HEAD", repo=repo).strip() or None
    unit = {
        "name": args.name,
        "mode": "track",
        "created_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "ep_estimate_raw": None,
        "ep_estimate": None,
        "anchor_used": None,
        "pace_value": None,
        "natural_days_central": None,
        "natural_days_band": None,
        "commit_filter": args.filter,
        "branch": branch,
    }
    forecast_path().write_text(json.dumps(unit, indent=2))
    print(f"Tracking: {args.name}")
    if branch:
        print(f"  Branch: {branch}")
    if args.filter:
        print(f"  Commit filter: /{args.filter}/")
    print()
    print(f"No estimate — discovery mode. Run `pace status` to see EP delivered, "
          f"`pace ship` when done.")


def cmd_complete(args) -> None:
    """Close the active unit, append outcome to history, refresh profile.

    Works for both forecast units (records bias data) and track units (records
    EP delivered without bias math). Aliased as `pace ship`.
    """
    repo = repo_root()
    fp = forecast_path()
    if not fp.exists():
        print("ERROR: no active unit to close.", file=sys.stderr)
        sys.exit(1)

    unit = json.loads(fp.read_text())
    mode = unit.get("mode", "forecast")
    started = _parse_iso(unit["created_at"])
    now = dt.datetime.now(dt.timezone.utc)

    claude = claude_project_dir(repo)
    since_str = started.strftime("%Y-%m-%d %H:%M")
    commits = commit_records(repo, since=since_str,
                             pattern=unit.get("commit_filter"),
                             branch=unit.get("branch"))
    md_total = markdown_lines_added(repo, since=since_str)
    subs = subagent_records(claude, since=started, until=now)
    measured_ep = total_ep(commits, md_total, subs)["total"]
    ep_actual = args.ep_actual if args.ep_actual is not None else measured_ep

    timestamps = gather_timestamps(claude) if claude else []
    sessions = filter_sessions(compute_sessions(timestamps), since=started, until=now)
    days_actual = len({s[0].astimezone().strftime("%Y-%m-%d") for s in sessions}) \
                  or max(1, int((now - started).total_seconds() / 86400))

    record = {
        "repo": str(repo),
        "name": unit["name"],
        "mode": mode,
        "started_at": unit["created_at"],
        "completed_at": now.isoformat(),
        "ep_estimate": unit.get("ep_estimate_raw"),
        "ep_estimate_biased": unit.get("ep_estimate"),
        "ep_actual": round(ep_actual, 2),
        "ep_measured": round(measured_ep, 2),
        "days_central": unit.get("natural_days_central"),
        "days_actual": days_actual,
        "anchor_used": unit.get("anchor_used"),
        "pace_value": unit.get("pace_value"),
        "commits": len(commits),
        "notes": args.notes,
    }
    append_jsonl(history_path(), record)
    append_jsonl(global_history_path(), record)
    fp.unlink()
    profile = rebuild_profile()

    if mode == "track":
        print(f"Shipped (track mode): {unit['name']}")
        print(f"  Active days:   {days_actual}")
        print(f"  EP delivered:  {ep_actual:.1f}  ({len(commits)} commits)")
        print()
        print(f"Logged to history. {profile['track_samples']} track unit(s), "
              f"{profile['forecast_samples']} forecast unit(s) total.")
        return

    # Forecast mode
    ep_ratio = record["ep_actual"] / record["ep_estimate"] if record["ep_estimate"] else 1.0
    days_ratio = record["days_actual"] / record["days_central"] if record["days_central"] else 1.0

    print(f"Forecast '{unit['name']}' completed.")
    print(f"  Estimated:   {record['ep_estimate']} EP / {record['days_central']} days")
    print(f"  Actual:      {record['ep_actual']} EP / {record['days_actual']} days")
    print(f"  EP ratio:    {ep_ratio:.2f}×  ({'over' if ep_ratio > 1 else 'under'} estimate)")
    print(f"  Days ratio:  {days_ratio:.2f}×")
    print()
    print(f"Profile updated. Now tracking {profile['forecast_samples']} forecast unit(s) "
          f"({profile['track_samples']} track-only).")
    print(f"  Median EP bias:    {profile['ep_bias_median']:.2f}×")
    print(f"  Median days bias:  {profile['days_bias_median']:.2f}×")
    if profile["forecast_samples"] >= 3:
        print(f"  Future forecasts will be auto-biased (use --no-bias to opt out).")


def cmd_cancel(args) -> None:
    fp = forecast_path()
    if not fp.exists():
        print("No active unit.")
        return
    unit = json.loads(fp.read_text())
    fp.unlink()
    mode = unit.get("mode", "forecast")
    print(f"{mode.capitalize()} '{unit['name']}' cancelled. No outcome recorded.")


def cmd_report(args) -> None:
    """Markdown retro across history within a window. Works in both modes."""
    repo = repo_root()
    path = global_history_path() if args.global_scope else history_path()
    rows = read_jsonl(path)
    if not rows:
        scope = "any repo" if args.global_scope else "this repo"
        print(f"No completed units in {scope} yet.")
        return

    since = None
    if args.since:
        try:
            # Accept "YYYY-MM-DD" or relative like "7d" / "30d"
            if args.since.endswith("d") and args.since[:-1].isdigit():
                days = int(args.since[:-1])
                since = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)
            else:
                since = _parse_iso(args.since + "T00:00:00+00:00")
        except (ValueError, TypeError):
            print(f"WARNING: couldn't parse --since '{args.since}', showing all.",
                  file=sys.stderr)

    if since:
        rows = [r for r in rows
                if "completed_at" in r and _parse_iso(r["completed_at"]) >= since]

    if not rows:
        print(f"No units completed since {args.since}.")
        return

    scope_label = "all repos" if args.global_scope else "this repo"
    period_label = f"since {args.since}" if args.since else "all time"

    print(f"# Pace report — {scope_label}, {period_label}")
    print()
    total_ep = sum(r.get("ep_actual", 0) for r in rows)
    total_days = sum(r.get("days_actual", 0) for r in rows)
    forecast_count = sum(1 for r in rows if r.get("mode", "forecast") == "forecast")
    track_count = sum(1 for r in rows if r.get("mode") == "track")

    print(f"## Summary")
    print()
    print(f"- **Units shipped**: {len(rows)} ({forecast_count} forecast, {track_count} track)")
    print(f"- **Total EP delivered**: {total_ep:.1f}")
    print(f"- **Total active days**: {total_days}")
    if total_days:
        print(f"- **Average pace**: {total_ep/total_days:.1f} EP/active-day")
    print()

    print(f"## Units")
    print()
    print(f"| name | mode | started | days | EP | est | ratio |")
    print(f"|---|---|---|---:|---:|---:|---:|")
    for r in sorted(rows, key=lambda x: x.get("started_at", ""), reverse=True):
        name = (r.get("name") or "")[:40]
        mode = r.get("mode", "forecast")
        started = r.get("started_at", "")[:10]
        days = r.get("days_actual", 0)
        ep_a = r.get("ep_actual", 0)
        ep_e = r.get("ep_estimate")
        if mode == "forecast" and ep_e:
            ratio = f"{ep_a/ep_e:.2f}×"
            est_s = f"{ep_e}"
        else:
            ratio = "—"
            est_s = "—"
        print(f"| {name} | {mode} | {started} | {days} | {ep_a:.1f} | {est_s} | {ratio} |")
    print()

    if forecast_count > 0:
        profile = load_profile()
        print(f"## Forecast accuracy")
        print()
        print(f"- **Median EP bias**: {profile['ep_bias_median']:.2f}× "
              f"(IQR {profile['ep_bias_iqr'][0]:.2f}–{profile['ep_bias_iqr'][1]:.2f})")
        print(f"- **Median days bias**: {profile['days_bias_median']:.2f}×")
        if profile["ep_bias_median"] > 1.10:
            print(f"- You tend to **underestimate** by {(profile['ep_bias_median']-1)*100:.0f}%.")
        elif profile["ep_bias_median"] < 0.90:
            print(f"- You tend to **overestimate** by {(1-profile['ep_bias_median'])*100:.0f}%.")
        else:
            print(f"- Forecast accuracy is **well-calibrated**.")
        print()


def cmd_history(args) -> None:
    path = global_history_path() if args.global_scope else history_path()
    scope = "global" if args.global_scope else "this repo"
    rows = read_jsonl(path)
    if not rows:
        print(f"No completed units ({scope}).")
        return
    print(f"Completed units ({scope}, {len(rows)} total):")
    print()
    print(f"  {'NAME':<26} {'MODE':<9} {'EP est→act':>12} {'days':>6} {'anchor':>10}")
    print(f"  {'-'*26} {'-'*9} {'-'*12} {'-'*6} {'-'*10}")
    for r in rows[-20:]:
        name = (r.get("name") or "")[:26]
        mode = r.get("mode", "forecast")[:9]
        ep_e = r.get("ep_estimate")
        ep_a = r.get("ep_actual", 0)
        d_a = r.get("days_actual", 0)
        anchor = (r.get("anchor_used") or "—")[:10]
        ep_str = f"{ep_e:>4}→{ep_a:<4.0f}" if ep_e else f"  —→{ep_a:<4.0f}"
        print(f"  {name:<26} {mode:<9} {ep_str:>12} {d_a:>6} {anchor:>10}")


def cmd_profile(args) -> None:
    profile = load_profile()
    print(f"Pace profile (~/.claude/skills/pace/data/profile.json):")
    print()
    print(f"  Forecast units:   {profile.get('forecast_samples', profile['samples'])}")
    print(f"  Track units:      {profile.get('track_samples', 0)}")
    if profile.get("forecast_samples", profile["samples"]) == 0:
        print()
        if profile.get("track_samples", 0) > 0:
            print("  Track-only history exists, but bias correction needs forecast units.")
            print("  Lock a forecast with `/pace start ...` to start training the bias.")
        else:
            print("  No data yet. Either:")
            print("    /pace track <name>     — start a discovery-mode unit")
            print("    /pace start ...        — lock a forecast")
        return
    print(f"  EP bias median:   {profile['ep_bias_median']:.2f}×  "
          f"(IQR {profile['ep_bias_iqr'][0]:.2f}–{profile['ep_bias_iqr'][1]:.2f})")
    print(f"  Days bias median: {profile['days_bias_median']:.2f}×")
    print(f"  Last updated:     {profile['last_updated']}")
    print()
    print(f"  Anchor usage:")
    for anchor, n in sorted(profile.get("anchor_usage", {}).items(), key=lambda x: -x[1]):
        print(f"    {anchor:<14} {n}")
    print()
    if profile["ep_bias_median"] > 1.10:
        print(f"  → You tend to UNDERESTIMATE by {(profile['ep_bias_median']-1)*100:.0f}%. "
              f"Future estimates auto-bias up unless you pass --no-bias.")
    elif profile["ep_bias_median"] < 0.90:
        print(f"  → You tend to OVERESTIMATE by {(1-profile['ep_bias_median'])*100:.0f}%. "
              f"Future estimates auto-bias down unless you pass --no-bias.")
    else:
        print(f"  → Your estimates are well-calibrated. Bias correction has minimal effect.")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    p = argparse.ArgumentParser(prog="pace", description=__doc__.split("\n\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)

    sub_cal = sub.add_parser("calibrate", help="Measure pace tiers from this repo.")
    sub_cal.add_argument("--author", default=None,
                         help="Count only this author's commits/docs (git --author match). "
                              "Defaults to the repo's git user.name. Use on shared repos.")
    sub_cal.add_argument("--all-authors", action="store_true", default=False,
                         help="Count the whole repo, every contributor (legacy behavior).")
    sub_cal.set_defaults(func=cmd_calibrate)

    sub_start = sub.add_parser("start", help="Lock in a forecast for an upcoming PRD.")
    sub_start.add_argument("--name", required=True)
    sub_start.add_argument("--ep", type=float, required=True)
    sub_start.add_argument("--pace", default="median",
                           choices=["slow", "median", "fast"],
                           help="Personal pace tier (default: median)")
    sub_start.add_argument("--pace-value", type=float, default=None,
                           help="Custom pace (EP/active-day) — overrides --pace")
    sub_start.add_argument("--filter", default=None,
                           help="Regex to match commit messages (e.g. '\\[Migration\\]')")
    sub_start.add_argument("--breakdown", default=None,
                           help="Optional JSON string of EP breakdown by task")
    sub_start.add_argument("--bias", action="store_true", default=True,
                           help="Apply bias correction from past forecasts (default on)")
    sub_start.add_argument("--no-bias", dest="bias", action="store_false",
                           help="Disable bias correction")
    sub_start.set_defaults(func=cmd_start)

    sub_track = sub.add_parser("track",
        help="Start a discovery-mode unit (no estimate, just observe).")
    sub_track.add_argument("name", help="Short name, e.g. 'mcp-server'")
    sub_track.add_argument("--filter", default=None,
                           help="Regex to match commit messages")
    sub_track.set_defaults(func=cmd_track)

    sub_status = sub.add_parser("status",
        help="Show progress on the active unit (forecast or track mode).")
    sub_status.set_defaults(func=cmd_status)

    # `complete` and `ship` are aliases — same handler.
    for cmd_name, cmd_help in [
        ("complete", "Close active unit and record outcome."),
        ("ship", "Alias of `complete` — close the active unit."),
    ]:
        sub_cmd = sub.add_parser(cmd_name, help=cmd_help)
        sub_cmd.add_argument("--ep-actual", type=float, default=None,
                              help="Actual EP delivered (default: measure from git/Claude)")
        sub_cmd.add_argument("--notes", default=None,
                              help="Optional retro note attached to the history entry")
        sub_cmd.set_defaults(func=cmd_complete)

    sub_cancel = sub.add_parser("cancel", help="Discard active unit without recording.")
    sub_cancel.set_defaults(func=cmd_cancel)

    sub_history = sub.add_parser("history", help="Show past completed units.")
    sub_history.add_argument("--global", dest="global_scope", action="store_true",
                             help="Show across all repos (default: this repo only)")
    sub_history.set_defaults(func=cmd_history)

    sub_profile = sub.add_parser("profile", help="Show cross-repo accuracy summary.")
    sub_profile.set_defaults(func=cmd_profile)

    sub_report = sub.add_parser("report",
        help="Markdown retro across history within a window.")
    sub_report.add_argument("--since", default=None,
        help="Filter to units completed since this date "
             "(YYYY-MM-DD or relative like '7d', '30d')")
    sub_report.add_argument("--global", dest="global_scope", action="store_true",
        help="Report across all repos (default: this repo only)")
    sub_report.set_defaults(func=cmd_report)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
