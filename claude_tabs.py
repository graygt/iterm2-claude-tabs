#!/usr/bin/env python3
"""iterm2-claude-tabs: an iTerm2 Toolbelt panel for juggling many Claude Code sessions.

Lists every Claude Code session across iTerm2 windows with its live state (needs
you / done / working / idle). Sessions can be grouped, dragged and reordered,
starred, colour-tagged (the iTerm2 tab gets the same colour), renamed, annotated,
postponed, forked and reopened after they were closed; a click focuses the tab.

State comes from claude-tabs-hook (a Claude Code hook) through
~/.config/claude-tabs/state/; marks and view settings live in
~/.config/claude-tabs/marks.json. The panel is served on 127.0.0.1 with a random
per-run token and never talks to the network.

Show it with View > Toolbelt > Claude Tabs (Cmd-Shift-B toggles the toolbelt).
https://github.com/graygt/iterm2-claude-tabs  (MIT licence)
"""
import asyncio
import glob
import json
import os
import re
import secrets
import shlex
import time

import iterm2

BASE = os.path.expanduser("~/.config/claude-tabs")
STATE_DIR = os.path.join(BASE, "state")
MARKS_PATH = os.path.join(BASE, "marks.json")
CLOSED_DIR = os.path.join(BASE, "closed")
RENAME_DIR = os.path.join(BASE, "rename")  # panel renames waiting for the session's next prompt
CLOSED_KEEP_SECONDS = 7 * 24 * 3600  # "Recently closed" lists everything from the last week
CLOSED_KEEP_MAX = 500      # safety bound on the number of records on disk
VANISH_GRACE_SECONDS = 5   # a tab must be missing this long before it counts as closed
HOME = os.path.expanduser("~")
# Optional per-session name files (e.g. written by a custom status line); kept in step
# with panel renames so nothing that reads them re-applies an old name.
CLAUDE_DIR = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~/.claude")
NAMES_DIR = os.path.join(CLAUDE_DIR, "session-names")

# ---- Claude's own session title, read from the transcript ----------------------------
# The panel shows what Claude Code shows: the last /rename (custom-title), else Claude's
# automatic title (ai-title). Transcripts only grow, so each one is read incrementally.
_TITLES = {}      # transcript path -> {"off": bytes read, "custom": str, "ai": str}
_TRANSCRIPT = {}  # claude session id -> transcript path (for state files without one)


def transcript_path(st):
    if st.get("transcript"):
        return st["transcript"]
    csid = st.get("claude_sid")
    if not csid:
        return None
    if csid not in _TRANSCRIPT:
        hits = glob.glob(os.path.join(CLAUDE_DIR, "projects", "*", glob.escape(csid) + ".jsonl"))
        _TRANSCRIPT[csid] = hits[0] if hits else None
    return _TRANSCRIPT[csid]


def claude_title(st):
    path = transcript_path(st)
    if not path:
        return None
    try:
        size = os.path.getsize(path)
    except OSError:
        return None
    c = _TITLES.get(path)
    if c is None or size < c["off"]:
        c = _TITLES[path] = {"off": 0, "custom": None, "ai": None}
    if size > c["off"]:
        with open(path, "rb") as f:
            f.seek(c["off"])
            data = f.read(size - c["off"])
        end = data.rfind(b"\n") + 1  # leave a half-written last line for next time
        for line in data[:end].splitlines():
            if b'"custom-title"' in line or b'"ai-title"' in line:
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if rec.get("type") == "custom-title" and rec.get("customTitle"):
                    c["custom"] = rec["customTitle"]
                elif rec.get("type") == "ai-title" and rec.get("aiTitle"):
                    c["ai"] = rec["aiTitle"]
        c["off"] += end
    return c["custom"] or c["ai"]


def pending_rename(sid, csid=None):
    """Title waiting to be handed to Claude; only if it is for the session now in the tab."""
    try:
        with open(os.path.join(RENAME_DIR, os.path.basename(sid))) as f:
            pending = json.load(f)
    except (OSError, ValueError):
        return None
    if csid is not None and pending.get("csid") != csid:
        return None
    return pending.get("title") or None


def set_pending_rename(sid, title):
    """Queue a rename; the hook hands it to Claude Code on the session's next prompt."""
    path = os.path.join(RENAME_DIR, os.path.basename(sid))
    csid = (load_json(os.path.join(STATE_DIR, sid + ".json"), {}) or {}).get("claude_sid")
    if not title or not csid:
        try:
            os.remove(path)
        except OSError:
            pass
        return
    os.makedirs(RENAME_DIR, exist_ok=True)
    with open(path, "w") as f:
        json.dump({"csid": csid, "title": title}, f)
    write_name_file(sid, title)


def migrate_panel_titles():
    """Earlier versions kept panel-only titles; turn them into renames of the session."""
    marks = load_marks()
    changed = False
    for sid, m in marks["sessions"].items():
        if m.get("title"):
            if not pending_rename(sid):
                set_pending_rename(sid, m["title"])
            m.pop("title")
            changed = True
    if changed:
        save_marks(marks)
TOKEN = secrets.token_hex(16)

COLORS = {
    "red": (229, 72, 77), "orange": (247, 107, 21), "yellow": (240, 190, 40),
    "green": (48, 164, 108), "blue": (0, 144, 255), "purple": (142, 78, 198),
}

APP = None
SERVER = None
FOCUS = {"app_active": True, "session": None}
SEEN = {}  # iTerm session id -> epoch seconds the user last looked at it
MISSING = {}  # iTerm session id -> when its tab was first found missing


def load_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default


def load_marks():
    d = load_json(MARKS_PATH, {})
    if "sessions" not in d:  # first version stored sessions at top level
        d = {"sessions": {k: v for k, v in d.items() if isinstance(v, dict)}}
    d.setdefault("groups", [])
    return d


def save_marks(marks):
    tmp = MARKS_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(marks, f, indent=1, ensure_ascii=False)
    os.replace(tmp, MARKS_PATH)


_project_cache = {}


def project_of(cwd):
    if not cwd:
        return "—"
    if cwd in _project_cache:
        return _project_cache[cwd]
    name = None
    p = cwd
    while True:
        g = os.path.join(p, ".git")
        if os.path.isdir(g):
            name = os.path.basename(p)
            break
        if os.path.isfile(g):
            # Worktree or submodule: fold worktrees into their main repo.
            try:
                with open(g) as f:
                    line = f.read().strip()
            except OSError:
                line = ""
            name = os.path.basename(p)
            if line.startswith("gitdir:"):
                gd = line[len("gitdir:"):].strip()
                if not os.path.isabs(gd):
                    gd = os.path.normpath(os.path.join(p, gd))
                i = gd.find(os.sep + ".git" + os.sep + "worktrees" + os.sep)
                if i >= 0:
                    name = os.path.basename(gd[:i])
            break
        parent = os.path.dirname(p)
        if parent == p:
            break
        p = parent
    if name is None:
        name = "~" if cwd.rstrip("/") == HOME else (os.path.basename(cwd.rstrip("/")) or cwd)
    _project_cache[cwd] = name
    return name


_SPINNER = re.compile(r"^[^\w(\[]+")
_SUFFIX = re.compile(r"\s*\((claude|node)\)\s*$")


def clean_title(name):
    name = _SUFFIX.sub("", _SPINNER.sub("", name or "")).strip()
    return name or "Claude"


async def snapshot():
    marks = load_marks()
    sessions = marks["sessions"]
    now = time.time()
    if FOCUS["app_active"] and FOCUS["session"]:
        SEEN[FOCUS["session"]] = now

    live = set()
    rows = []
    for wi, window in enumerate(APP.terminal_windows):
        for ti, tab in enumerate(window.tabs):
            for session in tab.sessions:
                sid = session.session_id
                live.add(sid)
                st = load_json(os.path.join(STATE_DIR, sid + ".json"), None)
                if not st:
                    continue
                m = sessions.get(sid, {})
                pending = pending_rename(sid, st.get("claude_sid"))
                title = pending or claude_title(st) or st.get("title")
                if not title:
                    try:
                        title = clean_title(await session.async_get_variable("name"))
                    except Exception:
                        title = "Claude"
                state = st.get("state", "idle")
                since = st.get("since") or st.get("updated") or now
                auto = project_of(st.get("cwd"))
                rows.append({
                    "id": sid,
                    "title": title,
                    "rename_pending": bool(pending),
                    "state": state,
                    "unread": state in ("done", "error") and SEEN.get(sid, 0) < since,
                    "since": since,
                    "detail": st.get("detail") or "",
                    "prompt": st.get("prompt") or "",
                    "cwd": (st.get("cwd") or "").replace(HOME, "~", 1),
                    "project": m.get("project") or "",
                    "auto_project": auto,
                    "star": bool(m.get("star")),
                    "postponed": bool(m.get("postponed")),
                    "color": m.get("color") or "",
                    "note": m.get("note") or "",
                    "order": m.get("order"),
                    "where": "w%d · tab %d" % (wi + 1, ti + 1),
                    "can_fork": bool(st.get("claude_sid")),
                    "csid": st.get("claude_sid") or "",
                    "parent": m.get("parent") or "",
                    "tasks": [{"type": t.get("type") or "task", "desc": t.get("desc") or ""}
                              for t in (st.get("tasks") or [])][:20],
                    "crons": int(st.get("crons") or 0),
                    "active": sid == FOCUS["session"],
                })

    # A fork whose parent was closed and reopened (new tab id) finds it again.
    by_csid = {r["csid"]: r["id"] for r in rows if r["csid"]}
    row_ids = {r["id"] for r in rows}
    relinked = False
    for r in rows:
        m = sessions.get(r["id"]) or {}
        found = by_csid.get(m.get("parent_csid"))
        if found and found != r["id"] and r["parent"] not in row_ids:
            m["parent"] = r["parent"] = found
            relinked = True
    if relinked:
        save_marks(marks)

    # Tabs that are gone: list them under Recently closed, then forget them.
    try:
        changed = False
        live |= {s.session_id for s in getattr(APP, "buried_sessions", None) or []}
        for fn in os.listdir(STATE_DIR):
            sid = fn[:-5]
            if not fn.endswith(".json"):
                continue
            if sid in live:
                MISSING.pop(sid, None)
                continue
            first = MISSING.setdefault(sid, now)
            if now - first < VANISH_GRACE_SECONDS:
                continue
            path = os.path.join(STATE_DIR, fn)
            remember_closed(sid, load_json(path, {}) or {}, sessions.get(sid, {}), "tab closed")
            os.remove(path)
            MISSING.pop(sid, None)
            changed = sessions.pop(sid, None) is not None or changed
        if changed:
            save_marks(marks)
    except OSError:
        pass
    live_csids = set()
    for fn in os.listdir(STATE_DIR):
        st = load_json(os.path.join(STATE_DIR, fn), {}) or {}
        if st.get("claude_sid"):
            live_csids.add(st["claude_sid"])
    return {"now": now, "rows": rows, "groups": marks["groups"], "ui": marks.get("ui"),
            "closed": closed_list(live_csids, now)}


async def apply_tab_color(sid, color):
    session = APP.get_session_by_id(sid)
    if not session:
        return
    p = iterm2.LocalWriteOnlyProfile()
    on = color in COLORS
    if on:
        c = iterm2.Color(*COLORS[color])
        p.set_tab_color(c)
        p.set_tab_color_light(c)
        p.set_tab_color_dark(c)
    p.set_use_tab_color(on)
    p.set_use_tab_color_light(on)
    p.set_use_tab_color_dark(on)
    await session.async_set_profile_properties(p)


def parent_csid_of(m):
    """Claude session id of a fork's parent. Tab ids change when a session is reopened;
    the Claude id doesn't, so it is what links a fork back to its parent."""
    if m.get("parent_csid"):
        return m["parent_csid"]
    if m.get("parent"):
        return (load_json(os.path.join(STATE_DIR, m["parent"] + ".json"), {}) or {}).get("claude_sid")
    return None


def live_session_for(csid):
    """iTerm2 session id of the open tab running this Claude session, if any."""
    if not csid:
        return None
    for window in APP.terminal_windows:
        for tab in window.tabs:
            for session in tab.sessions:
                st = load_json(os.path.join(STATE_DIR, session.session_id + ".json"), {}) or {}
                if st.get("claude_sid") == csid:
                    return session.session_id
    return None


def is_descendant(sessions, sid, ancestor):
    """True if sid sits (at any depth) under ancestor in the fork hierarchy."""
    seen = set()
    p = (sessions.get(sid) or {}).get("parent")
    while p and p not in seen:
        if p == ancestor:
            return True
        seen.add(p)
        p = (sessions.get(p) or {}).get("parent")
    return False


def keep_families(sessions, moved):
    """After cards in `moved` changed group or Postponed state (as one batch):
    their forks move with them, and a fork that now sits apart from its parent
    becomes a regular card (forks only nest inside their parent's group)."""
    moved = [sid for sid in moved if sid in sessions]
    for sid in moved:
        m = sessions[sid]
        for other, om in sessions.items():
            if other not in moved and is_descendant(sessions, other, sid):
                for key in ("project", "postponed"):
                    if m.get(key):
                        om[key] = m[key]
                    else:
                        om.pop(key, None)
    for sid in moved:
        m = sessions[sid]
        pm = sessions.get(m.get("parent") or "")
        if m.get("parent") and (pm is None or pm.get("project") != m.get("project")
                                or bool(pm.get("postponed")) != bool(m.get("postponed"))):
            m.pop("parent", None)
            m.pop("parent_csid", None)


def remember_closed(sid, st, m, reason):
    """Record a session for the Recently closed list (same format the hook writes)."""
    csid = st.get("claude_sid")
    if not csid or not (st.get("prompt") or st.get("title")):
        return
    set_pending_rename(sid, None)
    rec = {
        "claude_sid": csid, "cwd": st.get("cwd"), "title": claude_title(st) or st.get("title"),
        "prompt": st.get("prompt"), "closed_at": time.time(), "reason": reason,
        "marks": {k: m[k] for k in ("project", "color", "note", "star") if m.get(k)},
    }
    if parent_csid_of(m):
        rec["marks"]["parent_csid"] = parent_csid_of(m)
    os.makedirs(CLOSED_DIR, exist_ok=True)
    out = os.path.join(CLOSED_DIR, csid + ".json")
    tmp = out + ".tmp"
    with open(tmp, "w") as f:
        json.dump(rec, f)
    os.replace(tmp, out)


def closed_list(live_csids, now):
    items = []
    try:
        names = os.listdir(CLOSED_DIR)
    except OSError:
        return items
    for fn in names:
        if not fn.endswith(".json"):
            continue
        path = os.path.join(CLOSED_DIR, fn)
        rec = load_json(path, None)
        if not rec or rec.get("claude_sid") in live_csids:
            continue
        if now - rec.get("closed_at", 0) > CLOSED_KEEP_SECONDS:
            try:
                os.remove(path)
            except OSError:
                pass
            continue
        items.append(rec)
    items.sort(key=lambda r: -r.get("closed_at", 0))
    for rec in items[CLOSED_KEEP_MAX:]:  # bound the directory
        try:
            os.remove(os.path.join(CLOSED_DIR, rec["claude_sid"] + ".json"))
        except OSError:
            pass
    return [{
        "csid": r["claude_sid"],
        "title": r.get("title") or (r.get("prompt") or "")[:70] or "Claude session",
        "project": (r.get("marks") or {}).get("project") or project_of(r.get("cwd")),
        "cwd": (r.get("cwd") or "").replace(HOME, "~", 1),
        "prompt": r.get("prompt") or "",
        "closed_at": r.get("closed_at", now),
        "color": (r.get("marks") or {}).get("color") or "",
    } for r in items]


async def reopen_session(csid):
    path = os.path.join(CLOSED_DIR, os.path.basename(csid) + ".json")
    rec = load_json(path, None)
    if not rec:
        return {"ok": False, "error": "not in the closed list"}
    m = dict(rec.get("marks") or {})
    m.pop("title", None)  # the resumed session keeps Claude's own title
    m.pop("parent", None)
    # A fork whose parent is still open goes back under it, in a tab right after it.
    parent = live_session_for(m.get("parent_csid"))
    window, idx = APP.current_terminal_window, None
    if parent:
        pwin, ptab = APP.get_tab_and_window_for_session(APP.get_session_by_id(parent))
        if pwin and ptab in pwin.tabs:
            window, idx = pwin, pwin.tabs.index(ptab) + 1
    if window is None:
        window = await iterm2.Window.async_create(APP.connection)
        new = window.current_tab.current_session
    else:
        new = (await window.async_create_tab(index=idx)).current_session
    cwd = rec.get("cwd") or HOME
    await new.async_send_text("cd %s && claude -r %s\n" % (shlex.quote(cwd), shlex.quote(rec["claude_sid"])))
    marks = load_marks()
    if parent:
        pm = marks["sessions"].get(parent, {})
        m["parent"] = parent
        if pm.get("project"):  # forks nest only inside their parent's group
            m["project"] = pm["project"]
        else:
            m.pop("project", None)
        m.pop("postponed", None)
        if pm.get("order") is not None:
            m["order"] = pm["order"] + 0.5
    if m:
        marks["sessions"][new.session_id] = m
        if m.get("project") and m["project"] not in marks["groups"]:
            marks["groups"].append(m["project"])
        save_marks(marks)
    if m.get("color"):
        await apply_tab_color(new.session_id, m["color"])
    try:
        os.remove(path)
    except OSError:
        pass
    return {"ok": True}


def write_name_file(sid, title):
    """Claude Code itself is renamed by the hook on the session's next prompt."""
    csid = (load_json(os.path.join(STATE_DIR, sid + ".json"), {}) or {}).get("claude_sid")
    path = os.path.join(NAMES_DIR, os.path.basename(csid or ""))
    if csid and os.path.isfile(path):
        try:
            with open(path, "w") as f:
                f.write(title)
        except OSError:
            pass


def text(v, n=120):
    return " ".join(str(v or "").split())[:n]


def set_project(m, group):
    group = text(group, 60)
    if group:
        m["project"] = group
    else:
        m.pop("project", None)


async def fork_session(sid):
    """Open `claude -r <id> --fork-session` in a new tab right after the source tab."""
    st = load_json(os.path.join(STATE_DIR, sid + ".json"), {}) or {}
    csid = st.get("claude_sid")
    src = APP.get_session_by_id(sid)
    if not csid or not src:
        return {"ok": False, "error": "Claude session id not known yet"}
    try:
        profile = (await src.async_get_profile()).name
    except Exception:
        profile = None
    window, tab = APP.get_tab_and_window_for_session(src)
    idx = window.tabs.index(tab) + 1 if window and tab in window.tabs else None

    async def open_with(prof):
        return (await window.async_create_tab(profile=prof, index=idx)).current_session

    try:
        new = await open_with(profile)
    except Exception:
        # A tab can run a session-local copy of a profile that has no usable name.
        new = await open_with(None)
    cwd = st.get("cwd") or HOME
    await new.async_send_text("cd %s && claude -r %s --fork-session\n" % (shlex.quote(cwd), shlex.quote(csid)))

    # The fork starts in the same group, right below the original.
    marks = load_marks()
    m = marks["sessions"].get(sid, {})
    nm = {"parent": sid, "parent_csid": csid}  # shown nested under the original until detached
    if m.get("project"):
        nm["project"] = m["project"]
    if m.get("order") is not None:
        nm["order"] = m["order"] + 0.5
    marks["sessions"][new.session_id] = nm
    save_marks(marks)
    return {"ok": True}


async def act(req):
    op = req.get("op")
    if op == "fork":
        return await fork_session(req.get("id", ""))
    if op == "reopen":
        return await reopen_session(req.get("csid", ""))
    if op == "forget":
        try:
            os.remove(os.path.join(CLOSED_DIR, os.path.basename(req.get("csid", "")) + ".json"))
        except OSError:
            pass
        return {"ok": True}
    if op == "close":
        sid = req.get("id", "")
        session = APP.get_session_by_id(sid)
        if not session:
            return {"ok": False, "error": "session not found"}
        marks = load_marks()
        st = load_json(os.path.join(STATE_DIR, sid + ".json"), {}) or {}
        remember_closed(sid, st, marks["sessions"].get(sid, {}), "closed from panel")
        await session.async_close(force=True)  # the panel already asked for confirmation
        try:
            os.remove(os.path.join(STATE_DIR, sid + ".json"))
        except OSError:
            pass
        if marks["sessions"].pop(sid, None) is not None:
            save_marks(marks)
        return {"ok": True}
    if op == "focus":
        sid = req.get("id", "")
        session = APP.get_session_by_id(sid)
        if session:
            await session.async_activate(select_tab=True, order_window_front=True)
            SEEN[sid] = time.time()
        return {"ok": bool(session)}

    marks = load_marks()
    sessions = marks["sessions"]
    touched = set()

    if op == "set":
        key, val = req.get("key"), req.get("value")
        for sid in req.get("ids", []):
            m = sessions.setdefault(sid, {})
            touched.add(sid)
            if key in ("star", "postponed"):
                if val:
                    m[key] = True
                else:
                    m.pop(key, None)
            elif key == "title":
                set_pending_rename(sid, text(val))
            elif key == "note":
                v = text(val)
                if v:
                    m[key] = v
                else:
                    m.pop(key, None)
            elif key == "color":
                if val in COLORS:
                    m["color"] = val
                else:
                    m.pop("color", None)
                await apply_tab_color(sid, val)
            elif key == "project":
                set_project(m, val)
                g = text(val, 60)
                if g and g not in marks["groups"]:
                    marks["groups"].append(g)
            elif key == "parent":
                m.pop("parent", None)  # only detaching is supported
                m.pop("parent_csid", None)
            else:
                return {"ok": False, "error": "bad key"}
        if key in ("project", "postponed"):
            keep_families(sessions, list(req.get("ids", [])))
    elif op == "place":
        sid = req.get("id", "")
        parent = req.get("parent") or ""
        if parent and (parent == sid or not os.path.exists(os.path.join(STATE_DIR, parent + ".json"))
                       or is_descendant(sessions, parent, sid)):
            return {"ok": False, "error": "can't nest a session under itself or its own fork"}
        m = sessions.setdefault(sid, {})
        if req.get("post"):
            m["postponed"] = True
        else:
            m.pop("postponed", None)
            set_project(m, req.get("group"))
        if parent:
            # Nested under a card: it follows that card's group and Postponed state,
            # and keeps the link by Claude session id like a real fork.
            pm = sessions.get(parent, {})
            m["parent"] = parent
            pc = (load_json(os.path.join(STATE_DIR, parent + ".json"), {}) or {}).get("claude_sid")
            if pc:
                m["parent_csid"] = pc
            else:
                m.pop("parent_csid", None)
            set_project(m, pm.get("project"))
            if pm.get("postponed"):
                m["postponed"] = True
            else:
                m.pop("postponed", None)
        else:
            m.pop("parent", None)  # dropped at top level: a regular card
            m.pop("parent_csid", None)
        keep_families(sessions, [sid])  # its forks (any depth) move with it
        for i, other in enumerate(req.get("ids", [])):
            sessions.setdefault(other, {})["order"] = i
            touched.add(other)
        touched.add(sid)
    elif op == "ui":
        ui = req.get("ui") or {}
        marks["ui"] = {
            "view": ui.get("view") if ui.get("view") in ("project", "status") else "project",
            "sort": ui.get("sort") if ui.get("sort") in ("urgency", "manual") else "urgency",
            "fold": {str(k): True for k, v in (ui.get("fold") or {}).items() if v},
        }
    elif op == "groups":
        marks["groups"] = [text(g, 60) for g in req.get("names", []) if text(g, 60)]
    elif op == "reset":
        sid = req.get("id", "")
        if sessions.pop(sid, {}).get("color"):
            await apply_tab_color(sid, None)
    else:
        return {"ok": False, "error": "bad op"}

    for sid in touched:
        if not sessions.get(sid):
            sessions.pop(sid, None)
    save_marks(marks)
    return {"ok": True}


async def route(method, path, body):
    prefix = "/" + TOKEN + "/"
    if not path.startswith(prefix):
        return 404, "text/plain", b"not found"
    sub = path[len(prefix):].split("?", 1)[0]
    if method == "GET" and sub == "":
        return 200, "text/html; charset=utf-8", PAGE.encode("utf-8")
    if method == "GET" and sub == "state":
        return 200, "application/json", json.dumps(await snapshot()).encode("utf-8")
    if method == "POST" and sub == "action":
        res = await act(json.loads(body.decode("utf-8") or "{}"))
        return 200, "application/json", json.dumps(res).encode("utf-8")
    return 404, "text/plain", b"not found"


async def handle(reader, writer):
    try:
        head = await reader.readuntil(b"\r\n\r\n")
        lines = head.decode("latin-1").split("\r\n")
        method, path, _ = lines[0].split(" ", 2)
        headers = {}
        for line in lines[1:]:
            if ":" in line:
                k, v = line.split(":", 1)
                headers[k.strip().lower()] = v.strip()
        n = int(headers.get("content-length") or 0)
        body = await reader.readexactly(n) if n else b""
        status, ctype, data = await route(method, path, body)
    except Exception as e:  # keep serving whatever happens
        status, ctype, data = 500, "text/plain", repr(e).encode("utf-8")
    try:
        writer.write(
            ("HTTP/1.1 %d OK\r\nContent-Type: %s\r\nContent-Length: %d\r\n"
             "Cache-Control: no-store\r\nConnection: close\r\n\r\n"
             % (status, ctype, len(data))).encode("latin-1") + data)
        await writer.drain()
    finally:
        writer.close()


async def watch_focus(connection):
    async with iterm2.FocusMonitor(connection) as mon:
        while True:
            update = await mon.async_get_next_update()
            if update.application_active is not None:
                FOCUS["app_active"] = update.application_active.application_active
            if update.active_session_changed is not None:
                FOCUS["session"] = update.active_session_changed.session_id
            elif update.selected_tab_changed is not None or update.window_changed is not None:
                try:
                    FOCUS["session"] = APP.current_terminal_window.current_tab.current_session.session_id
                except AttributeError:
                    pass


async def main(connection):
    global APP, SERVER
    os.makedirs(STATE_DIR, exist_ok=True)
    APP = await iterm2.async_get_app(connection)
    migrate_panel_titles()
    try:
        FOCUS["session"] = APP.current_terminal_window.current_tab.current_session.session_id
    except AttributeError:
        pass
    asyncio.ensure_future(watch_focus(connection))
    SERVER = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = SERVER.sockets[0].getsockname()[1]
    url = "http://127.0.0.1:%d/%s/" % (port, TOKEN)
    print("claude-tabs serving", url, flush=True)
    fd = os.open(os.path.join(BASE, "url"), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(url + "\n")
    await iterm2.tool.async_register_web_view_tool(
        connection, "Claude Tabs", "claude-tabs", False, url)


PAGE = r"""<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<style>
:root{--bg:#f6f6f4;--fg:#1d1d1b;--mute:#76766f;--guide:#b9b8b1;--line:#e2e1dc;--row:#fff;--hover:#efeee9;--menu:#fff;
 --act:#2f6fdb;--wait:#e0702a;--done:#1f9d55;--work:#2f6fdb;--err:#d23c3c;--idle:#a6a59e;--star:#d9a300;
 --c-red:#e5484d;--c-orange:#f76b15;--c-yellow:#f0be28;--c-green:#30a46c;--c-blue:#0090ff;--c-purple:#8e4ec6}
@media (prefers-color-scheme:dark){:root{--bg:#1c1c1e;--fg:#e8e8e6;--mute:#8e8e89;--guide:#5a5a5e;--line:#333336;--menu:#2a2a2d;
 --row:#252527;--hover:#2f2f32;--act:#5b8ff0;--wait:#f08a45;--done:#3cc074;--work:#5b8ff0;--err:#f06060;--idle:#6b6b68;--star:#f0c030}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:12px/1.35 -apple-system,BlinkMacSystemFont,sans-serif;
 -webkit-user-select:none;user-select:none}
header{position:sticky;top:0;z-index:3;background:var(--bg);padding:8px 8px 6px;border-bottom:1px solid var(--line);display:grid;gap:6px}
.sum{display:flex;gap:8px;flex-wrap:wrap;color:var(--mute)}
.sum b{color:var(--fg);font-weight:600}
.bar{display:flex;gap:6px}
.seg{display:flex;flex:1;border:1px solid var(--line);border-radius:6px;overflow:hidden}
.seg button{flex:1;border:0;background:transparent;color:var(--mute);padding:3px 0;font:inherit;cursor:pointer}
.seg button.on{background:var(--row);color:var(--fg);font-weight:600}
input{font:inherit;padding:4px 6px;border:1px solid var(--line);border-radius:6px;background:var(--row);color:var(--fg);
 width:100%;-webkit-user-select:text;user-select:text;outline:none}
input:focus{border-color:var(--act)}
.grp{padding:10px 8px 3px;color:var(--mute);font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:.04em;
 display:flex;justify-content:space-between;align-items:center;cursor:pointer;border-radius:4px}
.grp .gn{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.grp .fa{display:inline-block;width:10px}
.grp.drop,.zone.drop{background:var(--hover);color:var(--fg)}
.zone{margin:4px 6px;padding:10px;border:1px dashed var(--mute);border-radius:6px;color:var(--mute);text-align:center;font-size:11px}
.pv .grp:not([data-g^="s:"]):not([data-g="::postponed"]){cursor:grab}
.grp.dragging,.row.gdragging{opacity:.3}
.grp.ghost{position:fixed;z-index:10;pointer-events:none;margin:0;background:var(--menu);color:var(--fg);
 box-shadow:0 6px 20px rgba(0,0,0,.25);padding:6px 8px;border-radius:6px}
.row{position:relative;margin:2px 6px;padding:6px 8px 6px 22px;background:var(--row);border-radius:6px;cursor:default;
 border:1px solid transparent;border-left:3px solid transparent;touch-action:none}
.row:hover{background:var(--hover)}
.row.active{border-color:var(--act)}
.row.post{opacity:.55}
.row.dragging{opacity:.3}
.row.ghost{position:fixed;z-index:10;pointer-events:none;margin:0;opacity:.95;box-shadow:0 6px 20px rgba(0,0,0,.25)}
.dot{position:absolute;left:7px;top:11px;width:8px;height:8px;border-radius:50%;background:var(--idle)}
.waiting .dot{background:var(--wait);box-shadow:0 0 0 3px color-mix(in srgb,var(--wait) 30%,transparent)}
.done.unread .dot{background:var(--done)} .error .dot{background:var(--err)}
.working .dot{background:var(--work);animation:pulse 1.4s ease-in-out infinite}
@keyframes pulse{50%{opacity:.3}}
.t{white-space:nowrap;overflow:hidden;text-overflow:ellipsis;padding-right:4px}
.unread .t,.waiting .t{font-weight:650}
.m{color:var(--mute);font-size:11px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.m .st{font-weight:600}
.waiting .m .st{color:var(--wait)} .done.unread .m .st{color:var(--done)} .error .m .st{color:var(--err)}
.working .m .st{color:var(--work)}
.note{font-size:11px;opacity:.85;margin-top:2px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.star{color:var(--star)}
.fk{color:var(--mute)}
.rp{color:var(--mute);font-weight:400}
.bgb{color:var(--work);font-weight:600}
.row.child[data-depth="1"]{margin-left:24px}
.row.child[data-depth="2"]{margin-left:42px}
.row.child[data-depth="3"]{margin-left:60px}
/* fork tree: │ runs through the 2px gaps between cards (-3px = gap + border) */
.tg{position:absolute;pointer-events:none;border:0 solid var(--guide)}
.tg.v{top:-3px;bottom:-3px;width:0;border-left-width:1px}
.tg.h{top:15px;width:9px;height:0;border-top-width:1px}
.tg.e{top:-3px;height:19px;width:9px;border-left-width:1px;border-bottom-width:1px;border-bottom-left-radius:5px}
.row.ghost{margin:0!important}
.ghost .tg{display:none}
.row.into{outline:2px dashed var(--act);outline-offset:1px}
.btns{position:absolute;right:4px;top:4px;display:none;gap:1px;background:var(--hover);border-radius:5px}
.row:hover .btns{display:flex}
.ghost .btns{display:none!important}
.btns button{border:0;background:transparent;color:var(--mute);font-size:13px;width:22px;height:22px;border-radius:4px;cursor:pointer;padding:0}
.btns button:hover{background:var(--line);color:var(--fg)}
.btns button.on{color:var(--star)}
.ins{position:fixed;z-index:9;height:2px;background:var(--act);border-radius:1px;pointer-events:none;display:none}
.menu{position:fixed;z-index:20;min-width:170px;max-width:calc(100vw - 8px);background:var(--menu);border:1px solid var(--line);
 border-radius:8px;box-shadow:0 8px 28px rgba(0,0,0,.25);padding:4px}
.mi{display:flex;gap:8px;align-items:center;padding:5px 8px;border-radius:5px;cursor:pointer;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.mi:hover{background:var(--act);color:#fff}
.mi .ic{width:14px;text-align:center;opacity:.8}
.mi.cur{font-weight:600}
.mh{padding:4px 8px 2px;color:var(--mute);font-size:11px}
.sep{height:1px;background:var(--line);margin:4px 2px}
.sw{display:flex;gap:6px;padding:5px 8px}
.sw span{width:16px;height:16px;border-radius:50%;cursor:pointer;border:2px solid transparent;display:grid;place-items:center;
 font-size:10px;color:var(--mute)}
.sw span.cur{border-color:var(--fg)}
.sw span.none{border:1px dashed var(--mute)}
.dlg{position:fixed;z-index:30;left:8px;right:8px;top:8px;background:var(--menu);border:1px solid var(--line);border-radius:8px;
 box-shadow:0 8px 28px rgba(0,0,0,.3);padding:10px;display:grid;gap:6px}
.dlg .hint{color:var(--mute);font-size:10px}
.dbtn{display:flex;gap:6px;justify-content:flex-end;margin-top:4px}
.dbtn button{font:inherit;padding:4px 12px;border-radius:6px;border:1px solid var(--line);background:var(--row);color:var(--fg);cursor:pointer}
.dbtn button:focus{outline:2px solid var(--act);outline-offset:1px}
.dbtn button.danger{background:var(--err);border-color:var(--err);color:#fff}
.mi.danger{color:var(--err)}
.mi.danger:hover{background:var(--err);color:#fff}
.empty{padding:24px 12px;color:var(--mute);text-align:center}
.closedh{margin-top:6px}
.crow{position:relative;margin:2px 6px;padding:5px 26px 5px 22px;border-radius:6px;cursor:pointer;opacity:.7;
 border:1px dashed var(--line);border-left:3px solid transparent}
.crow:hover{opacity:1;background:var(--hover)}
.crow .ri{position:absolute;left:7px;top:6px;color:var(--mute);font-size:11px}
.crow .fx{position:absolute;right:4px;top:5px;display:none;border:0;background:transparent;color:var(--mute);
 width:20px;height:20px;border-radius:4px;cursor:pointer;padding:0}
.crow:hover .fx{display:block}
.crow .fx:hover{background:var(--line);color:var(--fg)}
</style></head><body>
<header>
 <div class="sum" id="sum"></div>
 <input id="q" placeholder="Filter…  (title, project, note, folder)">
 <div class="bar">
  <div class="seg" id="view"><button data-v="project">Project</button><button data-v="status">Status</button></div>
  <div class="seg" id="sort"><button data-v="urgency" title="Needs-you first">Urgency</button><button data-v="manual" title="Your drag order">Manual</button></div>
 </div>
</header>
<main id="list"></main>
<div class="ins" id="ins"></div>
<script>
const $=s=>document.querySelector(s);
const COLORS=['red','orange','yellow','green','blue','purple'];
const RANK={waiting:0,unread:1,error:2,working:3,idle:4};
const LABEL={waiting:'needs you',unread:'done',error:'error',working:'working',idle:'idle'};
const SGROUP={waiting:'Needs you',unread:'Finished — not seen',error:'Errors',working:'Working',idle:'Idle'};
const POST='::postponed', TOP='::top', CLOSED='::closed';
let D={rows:[],now:0,groups:[]}, view='project', sort='urgency', q='', fold={}, layout={}, gorder=[], menu=null, dlg=null, drag=null;
const userGroups=()=>gorder.filter(n=>n!==POST&&n!==TOP);
// View settings are shared by the panels of all iTerm2 windows (stored server-side);
// localStorage only gives the first paint something before the first poll.
let uiChanged=0;
const store=(k,v)=>{try{localStorage.setItem(k,JSON.stringify(v))}catch(e){}
 uiChanged=Date.now();
 fetch('action',{method:'POST',body:JSON.stringify({op:'ui',ui:{view,sort,fold}})}).catch(()=>{})};
function applyUi(ui){
 if(!ui){store('view',view);return}            // first run: publish this window's settings
 if(Date.now()-uiChanged<2000)return;          // our own change may not be saved yet
 view=ui.view||'project';sort=ui.sort||'urgency';fold=ui.fold||{}}
const load=(k,d)=>{try{const v=localStorage.getItem(k);return v==null?d:JSON.parse(v)}catch(e){return d}};
view=load('view','project');sort=load('sort','urgency');fold=load('fold',{[POST]:true});
const busy=()=>menu||dlg||(drag&&drag.on);
const key=r=>r.unread&&r.state==='done'?'unread':r.unread&&r.state==='error'?'error':(r.state==='waiting'||r.state==='working')?r.state:'idle';
const rank=r=>RANK[key(r)];
const esc=s=>String(s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const byId=id=>D.rows.find(r=>r.id===id);
function ago(t){const s=Math.max(0,D.now-t);if(s<60)return 'now';if(s<3600)return Math.floor(s/60)+'m';
 if(s<86400)return Math.floor(s/3600)+'h';return Math.floor(s/86400)+'d'}
function cmp(a,b){
 if(sort==='manual'){const oa=a.order??1e9, ob=b.order??1e9;return (oa-ob)||(a.since-b.since)}
 return (b.star-a.star)||(rank(a)-rank(b))||(a.since-b.since)}
const match=r=>!q||[r.title,r.project,r.auto_project,r.note,r.cwd,r.prompt].join('\n').toLowerCase().includes(q);

function build(){
 const rows=D.rows.filter(match), act=rows.filter(r=>!r.postponed);
 let gs=[];
 if(view==='project'){
  const m=new Map();act.filter(r=>r.project).forEach(r=>{if(!m.has(r.project))m.set(r.project,[]);m.get(r.project).push(r)});
  if(!q)D.groups.forEach(n=>{if(!m.has(n))m.set(n,[])});
  gs=[...m].map(([n,rs])=>({g:n,name:n,rows:nest(rs.sort(cmp))}));
  const best=g=>g.rows.length?Math.min(...g.rows.map(rank)):99;
  if(sort==='manual'){const i=n=>{const k=D.groups.indexOf(n);return k<0?1e9:k};gs.sort((a,b)=>(i(a.g)-i(b.g))||a.g.localeCompare(b.g))}
  else gs.sort((a,b)=>(best(a)-best(b))||a.g.localeCompare(b.g));
  const top=nest(act.filter(r=>!r.project).sort(cmp));
  if(top.length||drag)gs.unshift({g:TOP,name:'',rows:top,top:true});
 }else{
  for(const k of Object.keys(RANK)){const rs=act.filter(r=>key(r)===k);if(rs.length)gs.push({g:'s:'+k,name:SGROUP[k],rows:nest(rs.sort(cmp))})}
 }
 const post=nest(rows.filter(r=>r.postponed).sort(cmp));
 if(post.length||drag)gs.push({g:POST,name:'Postponed',rows:post,post:true});
 return gs}

// Forks sit right under the session they came from (same group only), indented.
function nest(rows){
 const ids=new Set(rows.map(r=>r.id)),kids={},out=[],seen=new Set();
 const hasParent=r=>r.parent&&r.parent!==r.id&&ids.has(r.parent);
 rows.forEach(r=>{if(hasParent(r))(kids[r.parent]=kids[r.parent]||[]).push(r)});
 // guides: one entry per level for drawing the tree; 'mid' ├, 'end' └, 'pipe' │, '' nothing
 const walk=(r,d,pre,last)=>{if(seen.has(r.id))return;seen.add(r.id);r.depth=d;
  r.guides=d?[...pre,last?'end':'mid']:[];out.push(r);
  const ks=(kids[r.id]||[]).filter(k=>!seen.has(k.id)),next=d?[...pre,last?'':'pipe']:[];
  ks.forEach((k,i)=>walk(k,d+1,next,i===ks.length-1))};
 rows.forEach(r=>{if(!hasParent(r))walk(r,0,[],true)});
 rows.forEach(r=>walk(r,0,[],true));  // anything left (parent cycles) at top level
 return out}
function guidesHtml(r){
 const gs=(r.guides||[]).slice(-3);
 return gs.map((g,i)=>{const x=`left:${-13-18*(gs.length-1-i)}px`;
  return g==='pipe'?`<span class="tg v" style="${x}"></span>`
   :g==='mid'?`<span class="tg v" style="${x}"></span><span class="tg h" style="${x}"></span>`
   :g==='end'?`<span class="tg e" style="${x}"></span>`:''}).join('')}
function closedHtml(){
 const cs=(D.closed||[]).filter(c=>!q||[c.title,c.project,c.cwd,c.prompt].join('\n').toLowerCase().includes(q));
 if(!cs.length)return '';
 const f=!!fold[CLOSED];
 let h=`<div class="grp closedh" data-g="${CLOSED}"><span class="gn"><span class="fa">${f?'▸':'▾'}</span> Recently closed</span><span>${cs.length}</span></div>`;
 if(f)return h;
 for(const c of cs){
  const tip=['Click to reopen',c.cwd,c.prompt?'› '+c.prompt:''].filter(Boolean).join('\n');
  const cl=c.color?`style="border-left-color:var(--c-${c.color})"`:'';
  h+=`<div class="crow" data-csid="${esc(c.csid)}" title="${esc(tip)}" ${cl}><span class="ri">↺</span>
   <div class="t">${esc(c.title)}</div><div class="m">closed ${ago(c.closed_at)==='now'?'just now':ago(c.closed_at)+' ago'} · ${esc(c.project)}</div>
   <button class="fx" data-op="forget" title="Remove from list">✕</button></div>`}
 return h}
// Background work a session still has running (from Claude Code's own task list).
function bgBadge(r){
 const ts=r.tasks||[], n=t=>ts.filter(x=>x.type===t).length, other=ts.length-n('subagent')-n('shell');
 const parts=[], pl=(c,w)=>c+' '+w+(c===1?'':'s');
 if(n('subagent'))parts.push('⚙ '+pl(n('subagent'),'agent'));
 if(n('shell'))parts.push('▸ '+pl(n('shell'),'shell'));
 if(other)parts.push('◦ '+pl(other,'task'));
 if(r.crons)parts.push('⟳ '+pl(r.crons,'loop'));
 if(!parts.length)return '';
 const tip=ts.map(t=>(t.type==='subagent'?'⚙ ':t.type==='shell'?'▸ ':'◦ ')+t.desc).concat(r.crons?['⟳ '+pl(r.crons,'scheduled job')]:[]).join('\n');
 return `<span class="bgb" title="${esc('Running in the background:\n'+tip)}">${parts.join(' ')}</span>`}
function rowHtml(r,g){
 const k=key(r), meta=[`<span class="st">${LABEL[k]}</span> ${ago(r.since)}`];
 const bg=bgBadge(r);if(bg)meta.push(bg);
 if(r.detail&&(r.state==='waiting'||r.state==='working'||(r.state==='error'&&r.unread)))meta.push(esc(r.detail));
 if(view==='status'||!r.project)meta.push(esc(view==='status'&&r.project?r.project:r.auto_project));
 const tip=[r.cwd,r.where,r.prompt?'› '+r.prompt:''].filter(Boolean).join('\n');
 const cl=r.color?`style="border-left-color:var(--c-${r.color})"`:'';
 const par=r.parent&&byId(r.parent), dep=Math.min(r.depth||0,3);
 return `<div class="row ${r.state} ${r.unread?'unread':''} ${r.active?'active':''} ${r.postponed?'post':''} ${dep?'child':''}" ${cl}
 ${dep?`data-depth="${dep}"`:''}
 data-id="${r.id}" data-g="${esc(g)}" title="${esc(tip)}">${dep?guidesHtml(r):''}<span class="dot"></span>
 <div class="t">${dep?`<span class="fk" title="Fork of ${esc(par?par.title:'')}">⑂</span> `:''}${r.star?'<span class="star">★</span> ':''}${esc(r.title)}${r.rename_pending?' <span class="rp" title="Claude takes this name with your next prompt in this tab">↻</span>':''}</div><div class="m">${meta.join(' · ')}</div>
 ${r.note?`<div class="note">✎ ${esc(r.note)}</div>`:''}
 <div class="btns"><button data-op="star" class="${r.star?'on':''}" title="Star">★</button>
 <button data-op="menu" title="More…">⋯</button></div></div>`}

function render(force){
 if(busy()&&!force)return;
 const c={};D.rows.filter(r=>!r.postponed).forEach(r=>{const k=key(r);c[k]=(c[k]||0)+1});
 const parts=[['waiting','need you'],['unread','done'],['working','working']].filter(([k])=>c[k]).map(([k,l])=>`<span><b>${c[k]}</b> ${l}</span>`);
 parts.push(`<span><b>${D.rows.length}</b> sessions</span>`);
 $('#sum').innerHTML=parts.join('');
 document.querySelectorAll('#view button').forEach(b=>b.classList.toggle('on',b.dataset.v===view));
 document.querySelectorAll('#sort button').forEach(b=>b.classList.toggle('on',b.dataset.v===sort));
 document.body.classList.toggle('pv',view==='project');
 const gs=build();layout={};gorder=gs.map(g=>g.g);let h='';
 for(const g of gs){
  layout[g.g]=g.rows.map(r=>r.id);
  if(g.top){h+=g.rows.length?g.rows.map(r=>rowHtml(r,TOP)).join(''):`<div class="zone" data-g="${TOP}">Drop here — no group</div>`;continue}
  // A folded group opens while the focused tab is inside it, and folds back when you leave.
  const f=!!fold[g.g]&&!g.rows.some(r=>r.active);
  h+=`<div class="grp" data-g="${esc(g.g)}"><span class="gn"><span class="fa">${f?'▸':'▾'}</span> ${esc(g.name)}</span><span>${g.rows.length}</span></div>`;
  if(!f)h+=g.rows.length||g.post?g.rows.map(r=>rowHtml(r,g.g)).join(''):`<div class="zone" data-g="${esc(g.g)}">Drop sessions here</div>`;
 }
 if(!D.rows.length)h='<div class="empty">No Claude sessions yet.<br>Start <code>claude</code> in any tab.</div>';
 else if(!gs.length)h='<div class="empty">Nothing matches the filter.</div>';
 h+=closedHtml();
 $('#list').innerHTML=h;
 // Keep the focused tab's row in view when you switch tabs.
 const a=D.rows.find(r=>r.active),aid=a?a.id:null;
 if(aid&&aid!==lastActive){const el=document.querySelector(`.row[data-id="${aid}"]`);if(el)el.scrollIntoView({block:'nearest'})}
 lastActive=aid}
let lastActive=null;

async function poll(){try{D=await (await fetch('state')).json();if(!busy())applyUi(D.ui);render()}catch(e){}}
async function send(body){try{await fetch('action',{method:'POST',body:JSON.stringify(body)})}catch(e){}await poll()}
const setKey=(ids,k,v)=>send({op:'set',ids,key:k,value:v});

/* ---------- menus & dialogs ---------- */
function closeMenu(){if(menu){menu.remove();menu=null;render()}}
function openMenu(x,y,html,onClick){
 if(menu)menu.remove();
 menu=document.createElement('div');menu.className='menu';menu.innerHTML=html;document.body.appendChild(menu);
 const w=menu.offsetWidth,h=menu.offsetHeight;
 menu.style.left=Math.max(4,Math.min(x,innerWidth-w-4))+'px';menu.style.top=Math.max(4,Math.min(y,innerHeight-h-4))+'px';
 menu.onclick=e=>{const it=e.target.closest('[data-a]');if(it)onClick(it.dataset.a,it.dataset.v)}}
const mi=(a,ic,label,v='',cur=false)=>`<div class="mi ${cur?'cur':''}" data-a="${a}" data-v="${esc(v)}"><span class="ic">${ic}</span>${esc(label)}</div>`;

function rowMenu(id,x,y){
 const r=byId(id);if(!r)return;
 const sw=`<div class="sw">${COLORS.map(c=>`<span data-a="color" data-v="${c}" class="${r.color===c?'cur':''}" style="background:var(--c-${c})"></span>`).join('')}
  <span data-a="color" data-v="" class="none ${r.color?'':'cur'}" title="No colour">✕</span></div>`;
 openMenu(x,y,`<div class="mh">${esc(r.title)}</div>`+
  mi('focus','↗','Go to tab')+(r.can_fork?mi('fork','⑂','Fork session'):'')+mi('nest','↳','Nest under…')+(r.parent?mi('detach','⇤','Make regular tab'):'')+
  '<div class="sep"></div>'+mi('star','★',r.star?'Unstar':'Star')+
  '<div class="sep"></div>'+sw+'<div class="sep"></div>'+
  mi('move','➜','Move to group…')+mi('title','✎','Rename…')+mi('note','🗒',r.note?'Edit note…':'Add note…')+
  '<div class="sep"></div>'+mi('close','✕','Close session…').replace('class="mi ','class="mi danger '),
 (a,v)=>{
  if(a==='color'){closeMenu();setKey([id],'color',v);return}
  if(a==='move'){moveMenu([id],x,y);return}
  if(a==='nest'){nestMenu(id,x,y);return}
  closeMenu();
  if(a==='focus')send({op:'focus',id});
  else if(a==='fork')send({op:'fork',id});
  else if(a==='detach')setKey([id],'parent','');
  else if(a==='star')setKey([id],'star',!r.star);
  else if(a==='title')ask('Rename session','Renames the Claude session (like /rename) with your next prompt in that tab',r.title,v=>{if(v&&v!==r.title)setKey([id],'title',v)});
  else if(a==='note')ask('Note','Shown under the title; empty removes it',r.note,v=>setKey([id],'note',v));
  else if(a==='close')confirmClose(r);})}
function confirmClose(r){
 if(dlg)dlg.remove();
 const busyNote=r.state==='working'?' It is still working.':r.state==='waiting'?' It is waiting for your answer.':'';
 dlg=document.createElement('div');dlg.className='dlg';
 dlg.innerHTML=`<b>Close “${esc(r.title)}”?</b><div class="hint" style="font-size:11px">This closes the tab and ends its Claude session.${busyNote}</div>
  <div class="dbtn"><button data-c="0">Cancel</button><button data-c="1" class="danger">Close session</button></div>`;
 document.body.appendChild(dlg);
 const done=ok=>{dlg.remove();dlg=null;if(ok)send({op:'close',id:r.id});else render()};
 dlg.querySelector('[data-c="0"]').focus();
 dlg.onclick=e=>{const b=e.target.closest('button');if(b)done(b.dataset.c==='1')};
 dlg.onkeydown=e=>{if(e.key==='Escape')done(false)}}

function groupNames(extra){const s=new Set([...D.groups,...D.rows.map(r=>r.project),extra].filter(Boolean));return [...s].sort((a,b)=>a.localeCompare(b))}
// Is a (at any depth) a fork of anc?
function isDesc(a,anc){const seen=new Set();let r=byId(a);
 while(r&&r.parent&&!seen.has(r.id)){if(r.parent===anc)return true;seen.add(r.id);r=byId(r.parent)}return false}
const groupKey=r=>r.postponed?POST:(r.project||TOP);
// Place id under parent (or at top level when parent is ''), right after the parent's family.
function nestUnder(id,parent){
 const p=byId(parent),g=groupKey(p),ids=(layout[g]||[]).filter(x=>x!==id);
 let i=ids.indexOf(parent)+1;while(i<ids.length&&isDesc(ids[i],parent))i++;
 ids.splice(i,0,id);
 send(g===POST?{op:'place',id,post:true,parent,ids}:{op:'place',id,post:false,group:g===TOP?'':g,parent,ids})}
function nestMenu(id,x,y){
 const cands=D.rows.filter(c=>c.id!==id&&!isDesc(c.id,id)).sort((a,b)=>a.title.localeCompare(b.title));
 if(!cands.length){closeMenu();return}
 openMenu(x,y,`<div class="mh">Nest under…</div>`+cands.map(c=>mi('p','⑂',c.title+(c.project?'  ·  '+c.project:''),c.id,byId(id).parent===c.id)).join(''),
  (a,v)=>{closeMenu();if(a==='p')nestUnder(id,v)})}
function moveMenu(ids,x,y){
 const r=byId(ids[0]);
 openMenu(x,y,`<div class="mh">Move to group</div>`+groupNames(r&&r.auto_project).map(n=>mi('g','',n,n,r&&r.project===n)).join('')+
  '<div class="sep"></div>'+mi('new','＋','New group…')+(r&&r.project?mi('auto','⤒','No group (top)'):''),
 (a,v)=>{closeMenu();
  if(a==='g')setKey(ids,'project',v);
  else if(a==='auto')setKey(ids,'project','');
  else if(a==='new')ask('New group','Name of the group',"",v=>{if(v)setKey(ids,'project',v)})})}

function groupMenu(g,x,y){
 if(g===POST||g===CLOSED||g.startsWith('s:')){fold[g]=!fold[g];store('fold',fold);render(true);return}
 const ids=(layout[g]||[]);
 openMenu(x,y,`<div class="mh">${esc(g)}</div>`+mi('up','↑','Move group up')+mi('down','↓','Move group down')+
  mi('az','⇅','Sort all groups A–Z')+'<div class="sep"></div>'+
  mi('rename','✎','Rename group…')+mi('ungroup','⤒','Ungroup all (to top)')+mi('postall','⏸','Postpone all')+mi('fold',fold[g]?'▾':'▸',fold[g]?'Expand':'Collapse')+
  '<div class="sep"></div>'+mi('del','🗑','Delete group'+(ids.length?' (sessions go to top)':'')),
 (a)=>{closeMenu();
  if(a==='fold'){fold[g]=!fold[g];store('fold',fold);render(true)}
  else if(a==='az'){setSort('manual');send({op:'groups',names:userGroups().sort((p,q)=>p.localeCompare(q))})}
  else if(a==='postall')setKey(ids,'postponed',true);
  else if(a==='ungroup')setKey(ids,'project','');
  else if(a==='del'){const names=D.groups.filter(n=>n!==g);(ids.length?setKey(ids,'project',''):Promise.resolve()).then(()=>send({op:'groups',names}))}
  else if(a==='rename')ask('Rename group','Every session in it moves to the new name',g,v=>{if(v&&v!==g){
   send({op:'groups',names:userGroups().map(n=>n===g?v:n)}).then(()=>setKey(ids,'project',v))}});
  else{const names=userGroups(),i=names.indexOf(g),j=a==='up'?i-1:i+1;
   if(j<0||j>=names.length)return;[names[i],names[j]]=[names[j],names[i]];
   setSort('manual');send({op:'groups',names})}})}

function ask(title,hint,val,done,ph){
 if(dlg)dlg.remove();
 dlg=document.createElement('div');dlg.className='dlg';
 dlg.innerHTML=`<b>${esc(title)}</b><input value="${esc(val||'')}" placeholder="${esc(ph||'')}"><div class="hint">${esc(hint)} · Enter saves · Esc cancels</div>`;
 document.body.appendChild(dlg);const inp=dlg.querySelector('input');inp.focus();inp.select();
 inp.onkeydown=e=>{if(e.key==='Enter'){const v=inp.value.trim();dlg.remove();dlg=null;done(v)}
  if(e.key==='Escape'){dlg.remove();dlg=null;render()}}}

document.addEventListener('mousedown',e=>{
 if(menu&&!menu.contains(e.target))closeMenu();
 if(dlg&&!dlg.contains(e.target)){dlg.remove();dlg=null;render()}},true);
document.addEventListener('keydown',e=>{if(e.key==='Escape'&&menu)closeMenu()});
document.addEventListener('contextmenu',e=>{
 if(e.target.closest('input'))return;
 e.preventDefault();
 if(e.target.closest('.menu'))return;
 const row=e.target.closest('.row'),grp=e.target.closest('.grp'),cr=e.target.closest('.crow');
 if(cr){const csid=cr.dataset.csid;
  openMenu(e.clientX,e.clientY,mi('reopen','↺','Reopen in new tab')+mi('forget','✕','Remove from list'),
   a=>{closeMenu();send({op:a,csid})});return}
 if(row)rowMenu(row.dataset.id,e.clientX,e.clientY);
 else if(grp)groupMenu(grp.dataset.g,e.clientX,e.clientY);
 else newGroupMenu(e.clientX,e.clientY)});
function newGroupMenu(x,y){
 openMenu(x,y,mi('new','＋','New group…'),()=>{closeMenu();
  ask('New group','Name of the group',"",v=>{if(!v)return;
   if(view!=='project'){view='project';store('view',view)}
   send({op:'groups',names:[...D.groups.filter(n=>n!==v),v]})})})}

/* ---------- header controls ---------- */
function setSort(v){sort=v;store('sort',v);render(true)}
$('#view').onclick=e=>{const v=e.target.dataset.v;if(v){view=v;store('view',v);render(true)}};
$('#sort').onclick=e=>{const v=e.target.dataset.v;if(v)setSort(v)};
$('#q').oninput=e=>{q=e.target.value.trim().toLowerCase();render(true)};
$('#q').onkeydown=e=>{if(e.key==='Escape'){e.target.value='';q='';render(true)}
 if(e.key==='Enter'){const f=document.querySelector('.row');if(f)send({op:'focus',id:f.dataset.id})}};

/* ---------- click, buttons, drag & drop ---------- */
let dragEnded=0;
$('#list').addEventListener('click',e=>{
 if(Date.now()-dragEnded<300)return;
 const cr=e.target.closest('.crow');
 if(cr){const csid=cr.dataset.csid;
  if(e.target.closest('.fx'))send({op:'forget',csid});
  else{cr.style.opacity='.4';send({op:'reopen',csid})}
  return}
 const b=e.target.closest('button'),row=e.target.closest('.row'),grp=e.target.closest('.grp');
 if(b&&row){const id=row.dataset.id,r=byId(id),op=b.dataset.op;
  if(op==='star')setKey([id],'star',!r.star);
  else if(op==='menu'){const rc=b.getBoundingClientRect();rowMenu(id,rc.left,rc.bottom+2)}
  return}
 if(grp&&!row){const g=grp.dataset.g;fold[g]=!fold[g];store('fold',fold);render(true)}});

$('#list').addEventListener('pointerdown',e=>{
 if(e.button!==0||menu||dlg)return;
 const grp=e.target.closest('.grp');
 if(grp){const g=grp.dataset.g;
  if(view==='project'&&g!==POST&&g!==CLOSED)drag={kind:'group',g,x:e.clientX,y:e.clientY,on:false,moved:false,target:null};
  return}
 const row=e.target.closest('.row');
 if(!row||e.target.closest('button'))return;
 drag={kind:'row',row,id:row.dataset.id,from:row.dataset.g,x:e.clientX,y:e.clientY,on:false,moved:false,target:null}});
addEventListener('pointermove',e=>{
 if(!drag)return;
 if(!drag.on){if(Math.hypot(e.clientX-drag.x,e.clientY-drag.y)<5)return;drag.moved=true;
  if(drag.kind==='group')startGroupDrag();
  else if(view!=='project'&&drag.from!==POST)return;else startDrag(e)}
 if(drag.kind==='group')moveGroupDrag(e);else moveDrag(e)});
addEventListener('pointerup',e=>{
 if(!drag)return;const d=drag;
 if(d.on){if(d.kind==='group')finishGroupDrag();else finishDrag();dragEnded=Date.now()}
 else if(!d.moved&&d.kind==='row'){drag=null;send({op:'focus',id:d.id});return}
 drag=null;$('#ins').style.display='none';document.querySelectorAll('.drop,.into').forEach(x=>x.classList.remove('drop','into'))});

/* group drag: headers move whole groups; drop position is decided by group blocks */
const sel=g=>`[data-g="${CSS.escape(g)}"]`;
function blockRect(g){
 const els=[...document.querySelectorAll('#list '+sel(g))];if(!els.length)return null;
 return {top:els[0].getBoundingClientRect().top,bottom:els[els.length-1].getBoundingClientRect().bottom,
  left:els[0].getBoundingClientRect().left,width:document.getElementById('list').getBoundingClientRect().width}}
function startGroupDrag(){
 drag.on=true;closeMenu();
 const hd=document.querySelector('.grp'+sel(drag.g)),rc=hd.getBoundingClientRect();
 drag.dy=drag.y-rc.top;
 const gh=hd.cloneNode(true);gh.classList.add('ghost');gh.style.width=(rc.width-12)+'px';gh.style.left=(rc.left+6)+'px';
 gh.querySelector('.gn').insertAdjacentHTML('beforeend',` <span style="opacity:.6;text-transform:none;font-weight:400">· ${(layout[drag.g]||[]).length} sessions</span>`);
 document.body.appendChild(gh);drag.ghost=gh;
 document.querySelectorAll('#list '+sel(drag.g)).forEach(el=>el.classList.add(el.classList.contains('grp')?'dragging':'gdragging'))}
function moveGroupDrag(e){
 drag.ghost.style.top=(e.clientY-drag.dy)+'px';
 const hb=document.querySelector('header').getBoundingClientRect().bottom;
 if(e.clientY<hb+24)scrollBy(0,-10);else if(e.clientY>innerHeight-24)scrollBy(0,10);
 const others=userGroups().filter(n=>n!==drag.g);if(!others.length)return;
 let idx=others.length;
 for(let i=0;i<others.length;i++){const b=blockRect(others[i]);if(b&&e.clientY<(b.top+b.bottom)/2){idx=i;break}}
 const ref=idx<others.length?blockRect(others[idx]):blockRect(others[others.length-1]);
 const ins=$('#ins');ins.style.display='block';ins.style.left=(ref.left+6)+'px';ins.style.width=(ref.width-12)+'px';
 ins.style.top=(idx<others.length?ref.top-2:ref.bottom+2)+'px';
 drag.target=idx}
function finishGroupDrag(){
 drag.ghost.remove();
 document.querySelectorAll('.dragging,.gdragging').forEach(el=>el.classList.remove('dragging','gdragging'));
 const t=drag.target,g=drag.g;drag=null;
 if(t==null){render(true);return}
 const names=userGroups().filter(n=>n!==g);names.splice(t,0,g);
 setSort('manual');send({op:'groups',names})}

function startDrag(e){
 drag.on=true;render(true);  // re-render so an empty Postponed drop target exists
 const row=document.querySelector(`.row[data-id="${drag.id}"]`);const rc=row.getBoundingClientRect();
 drag.dy=drag.y-rc.top;
 const gh=row.cloneNode(true);gh.classList.add('ghost');gh.style.width=rc.width+'px';gh.style.left=rc.left+'px';
 document.body.appendChild(gh);drag.ghost=gh;row.classList.add('dragging')}
function moveDrag(e){
 drag.ghost.style.top=(e.clientY-drag.dy)+'px';
 const ins=$('#ins');document.querySelectorAll('.drop,.into').forEach(x=>x.classList.remove('drop','into'));
 const hb=document.querySelector('header').getBoundingClientRect().bottom;
 if(e.clientY<hb+24)scrollBy(0,-10);else if(e.clientY>innerHeight-24)scrollBy(0,10);
 const el=document.elementFromPoint(e.clientX,e.clientY);let t=el&&el.closest('.row:not(.dragging),.grp,.zone');
 if(!t&&e.clientY>hb)  // in the indent left of a card (or the margin): use the card at this height
  t=[...document.querySelectorAll('#list .row:not(.dragging)')].find(x=>{const b=x.getBoundingClientRect();return e.clientY>=b.top-2&&e.clientY<=b.bottom+2});
 if(!t){return}
 const g=t.dataset.g;
 if(view!=='project'&&g!==POST){ins.style.display='none';drag.target=null;return}
 if(!t.classList.contains('row')){t.classList.add('drop');ins.style.display='none';drag.target={g,index:0};return}
 const rc=t.getBoundingClientRect(),tr=byId(t.dataset.id),y=e.clientY,band=rc.height*0.3;
 const ids=(layout[g]||[]).filter(x=>x!==drag.id);
 if(tr&&y>rc.top+band&&y<rc.bottom-band&&!isDesc(tr.id,drag.id)){
  // middle of a card: becomes its fork, placed after its existing forks
  t.classList.add('into');ins.style.display='none';
  let i=ids.indexOf(tr.id)+1;while(i<ids.length&&isDesc(ids[i],tr.id))i++;
  drag.target={g,index:i,parent:tr.id,into:true};return}
 const before=y<rc.top+rc.height/2;
 // next to a fork, over its card: a sibling under the same parent; in the left gutter: top level
 let parent=(tr&&tr.depth&&e.clientX>=rc.left)?tr.parent:'';
 if(parent&&(parent===drag.id||isDesc(parent,drag.id)))parent='';
 const lb=$('#list').getBoundingClientRect().left+6,left=parent?rc.left:lb;
 ins.style.display='block';ins.style.left=(left+4)+'px';ins.style.width=(rc.right-left-8)+'px';
 ins.style.top=(before?rc.top-3:rc.bottom+1)+'px';
 let i=ids.indexOf(t.dataset.id);if(!before)i++;
 drag.target={g,index:i,parent}}
function finishDrag(){
 drag.ghost.remove();const t=drag.target;if(!t){render(true);return}
 const ids=(layout[t.g]||[]).filter(x=>x!==drag.id);ids.splice(t.index,0,drag.id);
 if(t.g===drag.from&&!t.into&&sort!=='manual')setSort('manual');
 if(t.g===POST&&fold[POST]){fold[POST]=false;store('fold',fold)}
 const id=drag.id;drag=null;
 const parent=t.parent||'';
 send(t.g===POST?{op:'place',id,post:true,parent,ids}:{op:'place',id,post:false,group:t.g===TOP?'':t.g,parent,ids})}

render(true);poll();setInterval(poll,1000);
</script></body></html>"""

iterm2.run_forever(main)
