# pace

A pace tool for solo developers that **fits how you actually work**. Two modes share the same plumbing:

- **forecast** — when you have a real spec, lock an estimate and let the system learn from outcomes
- **track** — when you're in discovery mode, just record what shipped without inventing numbers

No central service. No telemetry. Everything stays on your machine.

## Why

Most estimation tools assume you have a written PRD before you start. Most solo product work doesn't. `pace` lets you toggle between modes per feature — formal forecasting when it helps, passive tracking when it doesn't — and reads your actual git + Claude Code activity to derive your personal pace.

- **Repo-agnostic** — works on any git repo. No assumptions about your stack.
- **Personal tiers** — `slow`/`median`/`fast` are computed from your own active days, not generic anchors.
- **Bias correction** — every shipped forecast feeds back into a profile that auto-corrects future estimates.
- **Discovery-friendly** — `track` mode adds zero overhead and produces a faithful retro via `report`.

## Install

Requires Python 3.10+ and Claude Code (for session-time tracking). Git is required.

```bash
git clone https://github.com/<your-fork>/pace ~/pace-skill
bash ~/pace-skill/install.sh
```

Or copy `pace.py` and `SKILL.md` into `~/.claude/skills/pace/` manually.

## Usage

### Forecast mode (PRD-driven)
```
/pace calibrate          # measure your pace from history
/pace analyze            # paste a PRD, get an EP breakdown
/pace start              # lock the forecast (auto-applies bias correction)
/pace status             # check progress
/pace ship               # close the loop, train the bias correction
```

### Track mode (discovery-driven)
```
/pace track auth-flow    # start tracking, no estimate needed
/pace status             # see EP delivered so far
/pace ship               # close, joins history without bias math
```

### Either mode
```
/pace history            # past completed units, both modes
/pace report --since 7d  # markdown retro across both modes
/pace profile            # bias correction summary (forecast units only)
/pace cancel             # discard active unit without recording
```

Or directly via the CLI:
```bash
python3 ~/.claude/skills/pace/pace.py calibrate
python3 ~/.claude/skills/pace/pace.py track my-feature
python3 ~/.claude/skills/pace/pace.py ship
python3 ~/.claude/skills/pace/pace.py report --since 30d
```

## How EP is measured

EP (Effort Points) is a unit derived from observable artifacts:

| Signal | Weight |
|---|---|
| Commit ≤ 20 lines changed | 0.4 EP |
| Commit ≤ 200 lines | 1.0 EP |
| Commit ≤ 800 lines | 2.0 EP |
| Commit > 800 lines | 3.0 EP |
| Markdown lines added | 1.0 EP per 100 lines |
| Sub-agent run < 5 min | 0.1 EP |
| Sub-agent run < 20 min | 0.3 EP |
| Sub-agent run ≥ 20 min | 0.6 EP |

These weights are deterministic. The intelligence comes from:
1. How they roll up into your **personal tiers** (your actual active-day percentiles)
2. How forecast outcomes feed back into your **bias profile** to correct future estimates

## Storage

```
<repo>/.pace/data/
  calibration.json    per-repo pace measurement
  forecast.json       active unit (forecast or track mode)
  history.jsonl       completed units on this repo

~/.claude/skills/pace/data/
  profile.json        cross-repo accuracy summary
  history.jsonl       completed units across all repos
```

The per-repo files belong in your git history (commit them — that's how you lock predictions in time). The global files in `~/.claude/skills/pace/data/` are private to you.

## When to use which mode

**Forecast** when:
- A real spec or detailed ticket exists
- You're delivering against a commitment (client, deadline, sprint goal)
- You want to learn whether your estimates are getting better

**Track** when:
- The work is exploratory or evolving
- You don't have a written spec and don't want to invent one
- You want a passive journal — what was shipped, when, by you
- You're in discovery mode (most solo product work)

When in doubt: track. Forcing forecasts on undefined work poisons the bias data and adds paperwork without value.

## License

MIT.
