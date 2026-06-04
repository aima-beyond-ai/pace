---
name: pace
description: Pace estimator and journal for any developer. Two modes: forecast (PRD-driven — estimate, lock, learn from outcome) and track (discovery-driven — just observe what shipped). Reads the user's git + Claude Code history to derive personal pace tiers and learns bias correction from forecast outcomes. Commands: calibrate, analyze, start, track, status, complete/ship, cancel, history, profile, report.
user-invocable: true
allowed-tools:
  - Read
  - Write
  - Bash(python3 *)
  - Bash(git log *)
  - Bash(git rev-parse *)
  - Bash(ls *)
  - Bash(cat *)
  - Bash(test *)
  - Bash(stat *)
---

# /pace — universal pace tool for any developer

Help the user understand their pace and (optionally) forecast their work. Two modes share the same data plumbing:

| Mode | When | Trigger |
|---|---|---|
| **forecast** | A real spec exists; user wants a target | `/pace start --ep N --pace median` |
| **track** | Discovery-driven; no upfront estimate | `/pace track <name>` |

The kit lives at `~/.claude/skills/pace/pace.py`. Always invoke via Python so the user can audit.

Arguments: `$ARGUMENTS`

---

## Pick the right mode

Most developers shift between modes day-to-day. Don't force them into forecast when they don't have a spec.

**Use forecast when**:
- The user has a written PRD, ticket, or detailed brief
- They explicitly ask "how long will this take?" with a clear scope
- A client engagement requires a delivery commitment
- They've completed ≥3 forecasts and want bias correction to improve

**Use track when**:
- The user describes work in vague or evolving terms ("let me see what's needed")
- They mention discovery, exploration, or experimentation
- They're starting a feature whose shape will emerge as they build
- They say things like "starting on X now" without an estimate
- Most solo product work — when in doubt, default to track

---

## Step 1 — Detect intent

Look at `$ARGUMENTS` first. If empty, look at the surrounding conversation.

| Words | Operation |
|---|---|
| "calibrate", "measure", "set up" | calibrate |
| "analyze", "estimate", "size", "how big", a pasted PRD | analyze |
| "forecast", "start", "lock", "predict", explicit estimate | start (forecast mode) |
| "track", "starting", "begin", "I'm working on", no estimate | track |
| "status", "progress", "burn-down" | status |
| "complete", "ship", "shipped", "done", "finished" | complete (ship is an alias) |
| "cancel", "abandon", "drop" | cancel |
| "history", "past forecasts", "track record" | history |
| "profile", "bias", "accuracy" | profile |
| "report", "retro", "summary", "what did I do" | report |
| Nothing clear | Show both modes and ask |

If the user pastes a PRD or talks about scope: this is **forecast mode**. Run `analyze` first, present the EP breakdown, then `start`.

If the user says "I'm starting on the X feature": this is **track mode**. Run `track <name>` directly. No estimate needed.

---

## Step 2 — Verify repo

```bash
git -C "$PWD" rev-parse --show-toplevel
```

If not in a git repo, tell them to `cd` into one.

---

## Step 3 — calibrate

```bash
python3 ~/.claude/skills/pace/pace.py calibrate
```

The output reports dedication time, active days, EP delivered (weighted), personal tiers (`slow`/`median`/`fast`), and the cross-repo profile if any.

Report a one-line interpretation of the personal tiers. **Don't run calibrate as part of any other command** — it's user-initiated.

### Step 3b — Auto-sync the calibration to AIMA (if connected)

If an MCP tool named `record_my_pace` is available in the current session (AIMA workspace MCP server connected), call it immediately after `calibrate` succeeds. This avoids the user having to paste `calibration.json` into the profile page by hand.

Procedure:

1. Read `<repo>/.pace/data/calibration.json` (the file the calibrate command just wrote).
2. Call `mcp__<server-prefix>__record_my_pace` with the JSON contents as the arguments. The required fields are `personal_tiers`, `dedication_hours`, `natural_days`, `calibrated_at`; the extra fields (`ep`, `pace_observed`, `repo`, etc.) are accepted and ignored by the server.
3. On success, add a single line to the calibration summary: *"Sincronizado con AIMA (workspace: {orgId})."*
4. On failure (no MCP server, no API key, etc.), say nothing — degrade silently and let the user paste manually if they want.

Do NOT call `record_my_pace` from any other pace command. Only after a fresh `calibrate`.

---

## Step 4 — analyze (PRD → EP)

This is the LLM step. Used **only in forecast mode**.

### 4a — Load context first

Read these files if they exist:
1. `<repo>/.pace/data/calibration.json` → use `personal_tiers` to recommend a pace tier
2. `~/.claude/skills/pace/data/profile.json` → use `ep_bias_median` to surface a bias warning

### 4b — Honest sizing

Decompose the PRD into 3–8 epics/tasks. For each, classify by type and estimate size in commit-equivalents (one logical change touching 5–25 files).

| Type | Weight | Anchor example |
|---|---:|---|
| foundation | 0.6 / commit-eq | scaffold, init, simple config |
| strategic_refactor | 1.0 / commit-eq | replace one library with another |
| ui_redesign | 1.5 / commit-eq | iterative visual feedback work |
| diagnosis_cycle | 3.0 / cycle | analyze → fix → re-run → write up |
| doc_strategy | 1.0 / page | 100 lines of strategic markdown |
| infrastructure | 1.2 / commit-eq | cross-cutting plumbing |
| external_integration | 1.8 / commit-eq | vendor SDK, auth, deploy, env |

**Important caveat**: PRDs systematically under-describe the work that ships. Real software shows ~30–50% of work emerging during execution (debugging, polish, edge cases, UX iteration). When the PRD is a vision document or sketch, multiply the central estimate by 1.5–2.0. When it's a detailed ticket with acceptance criteria, multiply by 1.0–1.2. Surface this multiplier in the breakdown so the user sees the reasoning.

### 4c — Output a markdown breakdown

```markdown
## Task Breakdown

| Epic / Task | Type | Size | Weight | EP |
|---|---|---:|---:|---:|
| E1: ... | external_integration | 4 commit-eq | 1.8 | 7.2 |
| **Subtotal** | | | | **N** |
| Scope multiplier (PRD specificity) | | | × 1.5 | N×1.5 |
| **Total raw estimate** | | | | **M** |

## Confidence Band
- Low (every task at 0.5×): X EP
- Central: M EP
- High (every task at 1.5×): Z EP

## Risks
- [...]

## Recommended pace tier
[slow | median | fast] — based on the kind of work + your personal tiers.

## Bias correction (only if profile.samples ≥ 3)
Your historical bias is X.XX×. Biased EP: M × X.XX = ... EP.

## Forecast at recommended tier
EP ÷ tier_value = D active days  (band L – H)

## Next step
Run: python3 ~/.claude/skills/pace/pace.py start --name "<short>" --ep M --pace <tier>
```

Get user confirmation, then run `start`.

---

## Step 5 — start (forecast mode)

```bash
python3 ~/.claude/skills/pace/pace.py start --name "<name>" --ep <ep> --pace <tier> [--filter '<regex>'] [--no-bias]
```

The CLI auto-applies bias correction if profile has ≥3 samples. Output shows raw vs. biased EP so the user can see what happened.

After it succeeds, remind the user:
- **Commit `.pace/data/forecast.json` to git** to lock the prediction.
- **Run `/pace ship` (or `complete`) when done** — this trains the bias correction.

---

## Step 6 — track (discovery mode)

```bash
python3 ~/.claude/skills/pace/pace.py track <name> [--filter '<regex>']
```

No estimate, no target. Just records "I'm starting work on X now." Works without prior calibration.

After running, tell the user:
- "Tracking '<name>'. Run `/pace status` to see EP delivered as you go, `/pace ship` when done."
- Don't pretend there's a forecast to compare against.

---

## Step 7 — status

```bash
python3 ~/.claude/skills/pace/pace.py status
```

The output adapts to the mode:
- **Forecast mode**: full burn-down report with verdict (✅ on track / ⚠ behind / ❌ significantly behind)
- **Track mode**: just observation — elapsed time, active days, EP delivered, observed pace. No verdict.

For forecast mode, interpret the verdict per the usual playbook (probe blockers if behind, suggest cuts if significantly behind, etc.).

For track mode, just relay the data plainly. No "are you on track?" framing — there's no track to be on.

---

## Step 8 — complete / ship (close the unit)

```bash
python3 ~/.claude/skills/pace/pace.py ship           # alias of complete
# or
python3 ~/.claude/skills/pace/pace.py complete [--ep-actual N] [--notes "..."]
```

Behavior depends on mode:
- **Forecast**: records actual vs. estimated, updates bias profile, removes `forecast.json`.
- **Track**: records EP delivered + active days, no bias math, removes `forecast.json`.

Both modes append to `<repo>/.pace/data/history.jsonl` and `~/.claude/skills/pace/data/history.jsonl`.

After running, summarize plainly. For forecast mode, mention bias updates. For track mode, mention the EP/days observed and that it joined the journal.

---

## Step 9 — cancel

```bash
python3 ~/.claude/skills/pace/pace.py cancel
```

Discards the active unit (forecast or track) without recording. Use when work is abandoned, not when shipped.

---

## Step 10 — history

```bash
python3 ~/.claude/skills/pace/pace.py history          # this repo
python3 ~/.claude/skills/pace/pace.py history --global # all repos
```

Lists past completed units with the mode column. Highlight visible patterns if any.

---

## Step 11 — profile

```bash
python3 ~/.claude/skills/pace/pace.py profile
```

Shows cross-repo accuracy summary. **Bias correction only counts forecast units** — track units appear in the count but don't bias-correct an estimate that didn't exist.

Interpret the bias factor: > 1.10 means the user underestimates; < 0.90 means they overestimate.

---

## Step 12 — report

```bash
python3 ~/.claude/skills/pace/pace.py report --since 7d        # last 7 days
python3 ~/.claude/skills/pace/pace.py report --since 2026-04-01
python3 ~/.claude/skills/pace/pace.py report --global          # all repos
```

Markdown retrospective across both modes. The killer feature for discovery-driven workflows: a one-line command that produces a faithful summary of what shipped, no PRDs needed.

When the user wants a status update, weekly retro, or year-end review — `report` is the answer.

---

## What you must NOT do

- **Don't** force `start` (forecast mode) when the user has no real estimate. Use `track` instead.
- **Don't** modify weights, bucket thresholds, or fallback tiers in `pace.py` without the user's explicit consent.
- **Don't** invent calibration numbers. Run `calibrate` first if missing.
- **Don't** skip the "commit forecast.json" reminder after `start`. Skip it for `track` though — track units are private journal entries, not predictions.
- **Don't** auto-run `complete`/`ship` or `cancel`. Both should be explicit user choices.
- **Don't** treat any EP number as ground truth. It's a sizing aid; user judgment overrides.

---

## Storage layout

```
<repo>/.pace/data/
  calibration.json    per-repo measurement
  forecast.json       active unit (forecast or track)
  history.jsonl       completed units on this repo

~/.claude/skills/pace/data/
  profile.json        cross-repo accuracy summary (auto-rebuilt)
  history.jsonl       completed units across all repos
```

---

## End-to-end examples

### PRD-driven path
User: `/pace I have a PRD for a Firebase migration, how long?`

You:
1. Detect: analyze + start (forecast mode).
2. Read calibration + profile.
3. Ask for the PRD.
4. Produce EP breakdown with scope multiplier and bias correction.
5. User confirms.
6. Run `pace start --name firebase-migration --ep N --pace fast`.
7. Remind: commit forecast.json, run `/pace ship` when done.

### Discovery-driven path
User: `/pace starting on the new auth flow`

You:
1. Detect: track (no estimate mentioned).
2. Run `pace track auth-flow`.
3. Confirm: "Tracking 'auth-flow'. Run `/pace status` for progress, `/pace ship` when done."
4. (Days later, user runs `/pace ship` — outcome joins history without bias math.)

### Retro
User: `/pace what did I ship in the last month?`

You:
1. Detect: report.
2. Run `pace report --since 30d`.
3. Relay the markdown summary, highlight any patterns visible in the table.
