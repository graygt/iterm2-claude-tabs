#!/bin/bash
# Installs iterm2-claude-tabs: an iTerm2 Toolbelt panel for juggling many Claude Code sessions.
#
#   ./install.sh          copy the files into place
#   ./install.sh --link   symlink them to this checkout instead (for development)
#
# Safe to re-run: it updates the files and never duplicates the Claude Code hooks.
set -euo pipefail

here="$(cd "$(dirname "$0")" && pwd)"
mode=copy
[ "${1:-}" = "--link" ] && mode=link

base="$HOME/.config/claude-tabs"
hook="$base/claude-tabs-hook"
scripts="$HOME/Library/Application Support/iTerm2/Scripts/AutoLaunch"
panel="$scripts/claude_tabs.py"
config="${CLAUDE_CONFIG_DIR:-$HOME/.claude}"
settings="$config/settings.json"

if [ "$(uname)" != Darwin ]; then
  echo "claude-tabs needs macOS and iTerm2." >&2
  exit 1
fi
if ! /usr/bin/python3 -c pass 2>/dev/null; then
  echo "The hook runs with /usr/bin/python3, which needs Apple's command line tools." >&2
  echo "Run:  xcode-select --install   then re-run this installer." >&2
  exit 1
fi
if [ ! -d "/Applications/iTerm.app" ] && [ ! -d "/Applications/iTerm 2.app" ]; then
  echo "warning: iTerm2 was not found in /Applications (https://iterm2.com)." >&2
fi

put() {  # put <source> <destination> <mode>
  rm -f "$2"
  if [ "$mode" = link ]; then ln -s "$1" "$2"; else install -m "$3" "$1" "$2"; fi
}
mkdir -p "$base/state" "$scripts" "$config"
put "$here/claude-tabs-hook" "$hook" 755
put "$here/claude_tabs.py" "$panel" 644
echo "installed panel  -> $panel"
echo "installed hook   -> $hook"

# Register the hook for every Claude Code event the panel listens to.
[ -s "$settings" ] || echo '{}' > "$settings"
cp "$settings" "$base/settings.json.bak.$(date +%Y%m%d-%H%M%S)"
/usr/bin/python3 - "$settings" "$hook" <<'PY'
import json, os, sys
path, cmd = sys.argv[1], sys.argv[2]
raw = open(path).read()
d = json.loads(raw or "{}")
hooks = d.setdefault("hooks", {})
events = ["SessionStart", "SessionEnd", "UserPromptSubmit", "PreToolUse", "PostToolUse",
          "PermissionRequest", "Notification", "Stop", "StopFailure", "SubagentStart", "SubagentStop"]
# Drop earlier claude-tabs entries (any path) so re-running never duplicates them.
for ev in list(hooks):
    groups = []
    for g in hooks[ev]:
        g["hooks"] = [h for h in g.get("hooks", [])
                      if os.path.basename(str(h.get("command", ""))) != "claude-tabs-hook"]
        if g["hooks"]:
            groups.append(g)
    hooks[ev] = groups
for ev in events:
    entry = {"hooks": [{"type": "command", "command": cmd}]}
    if ev == "UserPromptSubmit":
        # First in line: when hooks return different session titles, Claude Code keeps
        # the first one, and ours only returns one for a rename made in the panel.
        hooks.setdefault(ev, []).insert(0, entry)
    else:
        hooks.setdefault(ev, []).append(entry)
for ev in [e for e, g in hooks.items() if not g]:
    del hooks[ev]
open(path, "w").write(json.dumps(d, indent=2, ensure_ascii=False) + "\n")
print("registered hooks in", path)
PY

# The panel talks to iTerm2 through its Python API.
defaults write com.googlecode.iterm2 EnableAPIServer -bool true

# If iTerm2 is running, (re)start the panel now; otherwise it starts with iTerm2.
if [ "$(osascript -e 'application id "com.googlecode.iterm2" is running' 2>/dev/null)" = true ]; then
  pkill -f 'AutoLaunch/claude_tabs.py' 2>/dev/null || true
  started=no
  for _ in 1 2 3 4 5 6 7 8; do  # iTerm2 needs a moment to notice a new script file
    if osascript -e 'tell application "iTerm2" to launch API script named "claude_tabs"' >/dev/null 2>&1; then
      started=yes; break
    fi
    sleep 1
  done
  if [ "$started" = yes ]; then echo "panel started"; else echo "could not start the panel yet (see step 1 below)"; fi
fi

cat <<'TXT'

Done. In iTerm2:
  1. If iTerm2 asks to download its Python runtime, accept
     (or: Scripts > Manage > Install Python Runtime, then restart iTerm2).
  2. View > Toolbelt > tick "Claude Tabs".  Cmd-Shift-B shows/hides the toolbelt.
Claude Code sessions appear after their next prompt or tool call.
TXT
