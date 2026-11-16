#!/usr/bin/env python3
"""Render README screenshots of the panel from made-up demo sessions.

Serves the panel page from ../claude_tabs.py on 127.0.0.1 with fake data (no iTerm2,
no real sessions involved) and has headless Google Chrome screenshot it:

    python3 docs/screenshot.py        # writes docs/panel.png and docs/panel-menu.png
"""
import http.server
import json
import os
import re
import subprocess
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
NOW = time.time()
MIN, HOUR, DAY = 60, 3600, 86400


def page_html():
    src = open(os.path.join(HERE, "..", "claude_tabs.py")).read()
    return re.search(r'PAGE = r"""(.*?)"""', src, re.S).group(1)


def row(i, title, project, state, ago, **kw):
    repo = kw.pop("repo", project or "dotfiles")
    r = {
        "id": "demo-%d" % i, "title": title, "title_set": False, "state": state,
        "unread": state in ("done", "error"), "since": NOW - ago, "detail": "",
        "prompt": "", "cwd": "~/code/" + repo, "project": project, "auto_project": repo,
        "star": False, "postponed": False, "color": "", "note": "", "order": i,
        "where": "w1 · tab %d" % (i + 1), "can_fork": True, "parent": "", "active": False,
    }
    r.update(kw)
    return r


ROWS = [
    row(0, "Review PR #482: retry logic", "", "waiting", 1 * MIN, detail="allow Bash?",
        repo="api", prompt="review the retry/backoff changes in PR 482"),
    row(1, "Dark mode for the settings page", "web-app", "done", 3 * MIN, color="blue",
        prompt="add dark mode to the settings page using the theme tokens"),
    row(2, "Fix flaky checkout e2e test", "web-app", "working", 6 * MIN, detail="Bash",
        active=True, prompt="the checkout e2e test fails 1 in 10 runs, find out why"),
    row(3, "Fix flaky checkout e2e test: mock the clock", "web-app", "working", 2 * MIN,
        detail="Edit", parent="demo-2", note="fork: try fake timers instead"),
    row(4, "Add rate limiting to /login", "api", "working", 12 * MIN, detail="Edit",
        star=True, note="ship before Friday", prompt="add per-IP rate limiting to /login"),
    row(5, "Investigate 502s on /orders", "api", "error", 4 * MIN,
        detail="API error: overloaded"),
    row(6, "Migrate payments to Stripe v2", "api", "idle", 1 * HOUR, color="purple"),
    row(7, "Terraform: move staging to eu-west-1", "infra", "waiting", 30,
        detail="allow Bash?", color="orange"),
    row(8, "Upgrade to React 20", "web-app", "idle", 2 * DAY, postponed=True,
        note="after the release"),
    row(9, "Try Bun for build scripts", "", "idle", 5 * DAY, postponed=True, repo="tools"),
]
CLOSED = [
    {"csid": "c1", "title": "Release notes for v3.2", "project": "web-app",
     "cwd": "~/code/web-app", "prompt": "", "closed_at": NOW - 25 * MIN, "color": ""},
    {"csid": "c2", "title": "Benchmark the JSON parser", "project": "api",
     "cwd": "~/code/api", "prompt": "", "closed_at": NOW - 3 * HOUR, "color": ""},
]
STATE = {"now": NOW, "rows": ROWS, "groups": ["web-app", "api", "infra"],
         "ui": {"view": "project", "sort": "manual", "fold": {}}, "closed": CLOSED}

# Opens the row menu of the forked session once the list has rendered.
MENU_JS = """<script>setTimeout(()=>{const el=document.querySelector('.row[data-id="demo-4"]');
const r=el.getBoundingClientRect();rowMenu('demo-4',r.left+60,r.top+24)},1500)</script>"""


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def send(self, body, ctype):
        data = body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path.endswith("/state"):
            self.send(json.dumps(STATE), "application/json")
        else:
            html = page_html()
            if "/menu/" in self.path:
                html = html.replace("</body>", MENU_JS + "</body>")
            self.send(html, "text/html; charset=utf-8")

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        self.send('{"ok": true}', "application/json")


def shoot(url, out, width=520, height=880):
    with tempfile.TemporaryDirectory() as profile:
        subprocess.run([
            CHROME, "--headless=new", "--disable-gpu", "--hide-scrollbars",
            "--user-data-dir=" + profile, "--force-dark-mode",
            "--blink-settings=preferredColorScheme=0", "--force-device-scale-factor=2",
            "--window-size=%d,%d" % (width, height), "--virtual-time-budget=4000",
            "--screenshot=" + out, url,
        ], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    print("wrote", out)


def main():
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = "http://127.0.0.1:%d" % server.server_address[1]
    shoot(base + "/demo/", os.path.join(HERE, "panel.png"))
    shoot(base + "/menu/", os.path.join(HERE, "panel-menu.png"), height=700)
    server.shutdown()


if __name__ == "__main__":
    main()
