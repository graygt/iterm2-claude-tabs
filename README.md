# iterm2-claude-tabs

**A side panel for iTerm2 that keeps ten-plus Claude Code sessions under control.**

If you run Claude Code in many tabs at once (some working, some waiting for an answer, some parked for later, spread over several projects), the tab bar stops telling you anything. iterm2-claude-tabs adds a panel to the right of the iTerm2 window that lists every Claude session across all windows, shows what each one is doing, and lets you group, order and mark them the way you think about your work.

<!-- Screenshot: docs/panel.png -->

## Features

- **Live status for every session**, across all iTerm2 windows:
  - 🟠 **needs you**: a permission request or a question is waiting
  - 🟢 **done**: finished, and you haven't looked at it yet
  - 🔵 **working**: shows the current tool and how long it has been running
  - 🔴 **error**: the turn failed
  - grey **idle**: done and already seen
- **Click a row to jump to its tab**, even in another window. The row of the tab you're in is outlined.
- **Groups.** Sessions start ungrouped at the top. Create groups (right-click empty space → New group…), drag sessions into them, drag whole groups to reorder them, collapse them, rename them.
- **Two sorts:** *Urgency* puts "needs you" first; *Manual* keeps your drag order.
- **Two views:** by *Project* (your groups) or by *Status*.
- **Marks:** star a session, give it a colour (the real iTerm2 tab gets the same colour), rename it, attach a note.
- **Postponed:** drag sessions you'll come back to into a folded section at the bottom. It opens by itself when you switch to one of its tabs.
- **Fork session:** right-click → *Fork session* opens `claude -r <id> --fork-session` in a new tab right next to the original, in the same folder. No more `/status` and copy-pasting IDs.
- **Recently closed:** sessions whose tab you closed are listed for two weeks; click one to resume it in a new tab with its group, colour and note.
- **Filter box** across titles, groups, notes, folders and last prompts.
- **Same view in every window:** groups, order, marks and view settings are shared by the panels of all iTerm2 windows.

## Requirements

- macOS with [iTerm2](https://iterm2.com) 3.5 or newer and its Python API (the installer turns it on)
- [Claude Code](https://claude.com/claude-code)
- Apple's command line tools (`xcode-select --install`), for `/usr/bin/python3`

## Install

```bash
git clone https://github.com/graygt/iterm2-claude-tabs.git
cd iterm2-claude-tabs
./install.sh
```

Then in iTerm2:

1. If iTerm2 offers to download its Python runtime, accept (or use **Scripts → Manage → Install Python Runtime** and restart iTerm2).
2. **View → Toolbelt → Claude Tabs.** ⌘⇧B shows and hides the toolbelt.

Sessions that were already running appear after their next prompt or tool call.

To update later: `git pull && ./install.sh`.

## Using it

| Do this | To |
|---|---|
| Click a row | jump to that tab |
| Hover a row | see its folder, window/tab and last prompt; ★ and ⋯ buttons appear |
| Right-click a row (or ⋯) | fork, star, colour, move to group, rename, add a note, close |
| Drag a row | move it into another group, into Postponed, or to the top (no group) |
| Drag a group header | reorder groups |
| Right-click a group header | move up/down, sort A–Z, rename, ungroup all, postpone all, delete |
| Right-click empty space | create a new group |
| Type in the filter box | filter everything; Enter jumps to the first match, Esc clears |

## How it works

```
Claude Code ──hook──▶ claude-tabs-hook ──▶ ~/.config/claude-tabs/state/<iTerm session>.json
                                                          │
iTerm2 Toolbelt ◀── web view ◀── claude_tabs.py (iTerm2 Python API, AutoLaunch)
```

- `claude-tabs-hook` is registered as a [Claude Code hook](https://docs.claude.com/en/docs/claude-code/hooks) for session start/end, prompts, tool use, permission requests, notifications and stop events. It records each session's state in a small JSON file named after the iTerm2 session (`$ITERM_SESSION_ID`). It takes about 20 ms and swallows every error, so it can never break Claude Code.
- `claude_tabs.py` runs inside iTerm2 as an AutoLaunch script. It watches windows and tabs through the iTerm2 Python API, serves the panel to a Toolbelt web view and performs actions such as focusing tabs, colouring tabs and opening forks.
- Your groups, marks and view settings live in `~/.config/claude-tabs/marks.json`.

It works next to iTerm2's own Claude Code integration (the `cc-status` hook and tab-bar status dots); neither interferes with the other.

### Privacy

Nothing leaves your Mac. The panel is served on `127.0.0.1` on a random port with a random per-run token in the URL, and the scripts make no network requests. The state files hold session IDs, folders, titles and your last prompt (first 200 characters), all under `~/.config/claude-tabs`.

## Limitations

- Pressing Esc to interrupt Claude fires no hook, so the session shows *working* for up to a minute until Claude's idle notification corrects it.
- Claude Code running on another machine over ssh doesn't appear, because its hooks run on that machine.
- "Not seen yet" tracking is kept in memory and resets when iTerm2 restarts.
- Claude Code's hook events change between versions now and then; if states look wrong, see Troubleshooting.

## Troubleshooting

- **Panel is blank or outdated:** untick and re-tick **View → Toolbelt → Claude Tabs**.
- **Panel missing from the Toolbelt menu:** check **Scripts → Manage → Console** for errors from `claude_tabs.py`, or start it with **Scripts → AutoLaunch → claude_tabs.py**.
- **A session shows the wrong state:** `touch ~/.config/claude-tabs/debug` turns on an event log in `~/.config/claude-tabs/events.log`. Reproduce, attach the relevant lines to an issue, then remove the `debug` file.

## Uninstall

```bash
./uninstall.sh            # removes the panel and the hooks, keeps your groups and marks
./uninstall.sh --purge    # also deletes ~/.config/claude-tabs
```

Both installer and uninstaller back up your Claude Code `settings.json` to `~/.config/claude-tabs/` before changing it.

## License

[MIT](LICENSE)

iterm2-claude-tabs is an independent project and isn't affiliated with Anthropic or iTerm2.
