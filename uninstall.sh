#!/bin/bash
# Removes iterm2-claude-tabs.
#
#   ./uninstall.sh           remove the panel and the Claude Code hooks, keep your groups/marks
#   ./uninstall.sh --purge   also delete ~/.config/claude-tabs (groups, marks, notes, state)
set -euo pipefail

base="$HOME/.config/claude-tabs"
panel="$HOME/Library/Application Support/iTerm2/Scripts/AutoLaunch/claude_tabs.py"
config="${CLAUDE_CONFIG_DIR:-$HOME/.claude}"
settings="$config/settings.json"

if [ -s "$settings" ]; then
  mkdir -p "$base"
  cp "$settings" "$base/settings.json.bak.$(date +%Y%m%d-%H%M%S)"
  /usr/bin/python3 - "$settings" <<'PY'
import json, os, sys
path = sys.argv[1]
d = json.load(open(path))
hooks = d.get("hooks", {})
for ev in list(hooks):
    groups = []
    for g in hooks[ev]:
        g["hooks"] = [h for h in g.get("hooks", [])
                      if os.path.basename(str(h.get("command", ""))) != "claude-tabs-hook"]
        if g["hooks"]:
            groups.append(g)
    if groups:
        hooks[ev] = groups
    else:
        del hooks[ev]
if not hooks:
    d.pop("hooks", None)
open(path, "w").write(json.dumps(d, indent=2, ensure_ascii=False) + "\n")
print("removed hooks from", path)
PY
fi

pkill -f 'AutoLaunch/claude_tabs.py' 2>/dev/null || true
rm -f "$panel" "$base/claude-tabs-hook" "$base/url"
echo "removed the panel (untick View > Toolbelt > Claude Tabs if it is still shown)"

if [ "${1:-}" = "--purge" ]; then
  rm -rf "$base"
  echo "deleted $base"
else
  echo "kept your groups and marks in $base (use --purge to delete them)"
fi
echo "Tab colours set from the panel stay until you change them in iTerm2."
