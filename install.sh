#!/usr/bin/env bash
# install.sh — install the pace skill into the current user's Claude Code skills directory.
#
# Usage:
#   bash install.sh           # copy pace.py + SKILL.md into ~/.claude/skills/pace
#   bash install.sh --link    # symlink them instead (repo stays the single source of truth)
#
# Idempotent — safe to re-run to upgrade. Only writes pace.py and SKILL.md;
# user-generated data under <repo>/.pace/data/ and ~/.claude/skills/pace/data/
# is never touched.
#
# --link is recommended for development: it makes ~/.claude/skills/pace/{pace.py,SKILL.md}
# point straight at this repo, so editing the repo updates the running skill instantly and
# the two can never drift out of sync.

set -euo pipefail

LINK_MODE=false
for arg in "$@"; do
  case "$arg" in
    --link) LINK_MODE=true ;;
    *) echo "Unknown argument: $arg (supported: --link)" >&2; exit 2 ;;
  esac
done

SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TARGET_DIR="$HOME/.claude/skills/pace"
DATA_DIR="$TARGET_DIR/data"

echo "Installing pace skill"
echo "  source: $SOURCE_DIR"
echo "  target: $TARGET_DIR"
echo

# Refuse to install if source and target are the same (already installed).
if [ "$SOURCE_DIR" = "$TARGET_DIR" ]; then
  echo "Source and target are the same path — pace is already installed at $TARGET_DIR."
  echo "Re-running install would have no effect. Aborting."
  exit 0
fi

# Sanity-check that the source has the two files we expect.
for f in pace.py SKILL.md; do
  if [ ! -f "$SOURCE_DIR/$f" ]; then
    echo "ERROR: $SOURCE_DIR/$f is missing. This installer must run from the source kit." >&2
    exit 1
  fi
done

# Verify python3 is available.
if ! command -v python3 >/dev/null 2>&1; then
  echo "ERROR: python3 not found on PATH. pace requires Python 3.10+." >&2
  exit 1
fi
PY_VER="$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
echo "  python3: $PY_VER"

mkdir -p "$TARGET_DIR" "$DATA_DIR"

# Back up existing files before overwriting (so an upgrade is reversible).
ts="$(date +%Y%m%d-%H%M%S)"
for f in pace.py SKILL.md; do
  if [ -f "$TARGET_DIR/$f" ]; then
    cp "$TARGET_DIR/$f" "$TARGET_DIR/${f}.backup-${ts}"
    echo "  backed up old $f → ${f}.backup-${ts}"
  fi
done

if [ "$LINK_MODE" = true ]; then
  ln -sf "$SOURCE_DIR/pace.py" "$TARGET_DIR/pace.py"
  ln -sf "$SOURCE_DIR/SKILL.md" "$TARGET_DIR/SKILL.md"
  echo "  linked pace.py  → $SOURCE_DIR/pace.py"
  echo "  linked SKILL.md → $SOURCE_DIR/SKILL.md"
else
  cp "$SOURCE_DIR/pace.py" "$TARGET_DIR/pace.py"
  cp "$SOURCE_DIR/SKILL.md" "$TARGET_DIR/SKILL.md"
  chmod +x "$TARGET_DIR/pace.py"
fi

echo
if [ "$LINK_MODE" = true ]; then
  echo "✓ pace linked into $TARGET_DIR (source of truth: $SOURCE_DIR)"
else
  echo "✓ pace installed at $TARGET_DIR"
fi
echo
echo "Quick start:"
echo "  cd <any-git-repo>"
echo "  python3 $TARGET_DIR/pace.py calibrate     # measure your pace from history"
echo
echo "Or, in a Claude Code session inside that repo:"
echo "  /pace calibrate"
echo "  /pace analyze     (paste a PRD)"
echo "  /pace start ..."
echo "  /pace complete    (when shipped — this is what trains it)"
echo
echo "Run \`python3 $TARGET_DIR/pace.py --help\` for the full command list."
