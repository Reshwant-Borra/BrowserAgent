# Using BrowserAgent

This is the normal way to use BrowserAgent — you don't need to know anything about tasks,
batches, workflows, or the CLI commands documented elsewhere. Type what you want in plain
English and BrowserAgent figures out how to run it.

## Start it

The recommended everyday path uses a **persistent browser**: a Chromium window BrowserAgent
attaches to instead of launching its own throwaway one. You log in to sites once, and that
session (cookies, etc.) survives even after you close and reopen BrowserAgent — see
"Persistent browser (recommended)" below.

For quick one-off testing, or if you don't want a separate persistent browser window,
BrowserAgent can also just launch and manage its own Chromium (`launch` mode, the default):

```powershell
browser-agent ui
```

This prints a URL (default `http://127.0.0.1:8765`) and opens it in your browser
automatically. The server only listens on your own machine (`127.0.0.1`) — nothing is
exposed to your network.

To pick a different port, or skip auto-opening a browser tab:

```powershell
browser-agent ui --port 9000 --no-browser
```

### Persistent browser (recommended)

Start a dedicated Chromium once, with remote debugging enabled and its own profile (never
your everyday Chrome profile):

```powershell
browser-agent browser start
```

Log in to any sites you'll need (school portal, email, etc.) in that window — normal manual
login, BrowserAgent never sees or stores your password. Then start the UI pointed at it:

```powershell
browser-agent ui --browser-mode cdp_attach --cdp-endpoint http://127.0.0.1:9222
```

Now BrowserAgent's UI page shows a small **Browser: Connected** indicator. Close the UI
(Ctrl+C) any time — the Chromium window, your open tabs, and your logged-in sessions stay
exactly as they were, since BrowserAgent only disconnects, it never closes them. Re-run the
same `browser-agent ui --browser-mode cdp_attach ...` command later to reattach to the same
browser and pick up where you left off. If the persistent browser isn't running when you
start the UI this way, the status indicator shows **Browser: Not connected** with the exact
`browser-agent browser start` command to fix it.

Remote debugging is bound to `127.0.0.1` only — never expose it to a LAN or the public
internet; anyone who can reach that port has full control of the browser, including any
logged-in sessions in it.

## Type a task

Type what you want in the text box and press **Run**. Some examples:

```text
Check these URLs and tell me which assignments I still have to do:
https://canvas.example.edu/course/101
https://canvas.example.edu/course/205
```

```text
Go to this site and find every assignment due this week.
https://portal.example.edu/dashboard
```

```text
Go to https://a.example.com and set the display mode to Compact.
Then go to https://b.example.com and enable the weekly summary.
Verify both.
```

```text
Research college application essays across a good number of strong sources
and give me an evidence-backed report.
```

You don't need to say "batch" or "single task" or anything about the internal
architecture — BrowserAgent decides on its own whether this is a single page, a sweep across
several pages, an ordered sequence of actions, or a research task.

### Talking about pages you haven't pasted a link for

In persistent-browser (`cdp_attach`) mode, BrowserAgent can also work from what you already
have open, or the page you're currently looking at — no need to copy-paste every URL:

```text
Check all my course pages and tell me what I still need to do this week.
```

```text
Tell me what I still need to do on this page.
```

```text
Look through everything I currently have open for school and tell me what's due.
```

```text
Check my course pages, find the assignment with the nearest deadline, and open it.
```

BrowserAgent reads these the same way a person would: it figures out you mean your currently
open tabs (or the tab you're on), looks at what's actually there, and only ever acts on real
pages it can see — it never invents a URL. If nothing you have open matches what you asked
for, it asks rather than failing silently — see "When BrowserAgent needs more information"
below. (Details: docs/SEMANTIC_PLANNER.md.)

## Watch progress

While a task runs you'll see a status line (Running / Waiting for approval / Waiting for
login / Completed / Failed / Stopped) and a short activity line — e.g. "Checking site 3 of
10" or "Step 2 of 3: enable notifications". A collapsible **Details** section has raw
task/batch/workflow IDs if you ever need them for debugging; you don't need to look at it
normally.

## Approving actions

If BrowserAgent needs to do something consequential — submit a form, send something, delete
something, make a purchase — it stops and asks first:

```text
Approval required

BrowserAgent wants to:
click "Submit Application" — reason: application is complete

[Approve]  [Deny]
```

Nothing happens until you click one of the buttons. Denying doesn't crash the task — it's
recorded and the task reports what it could and couldn't finish.

## Logging in

BrowserAgent never types your password for you. If a task hits a page that needs you to sign
in, it pauses and shows:

```text
Login required. Please complete login in the browser, then click Continue.
```

A visible browser window is open on that page (browser visibility is on by default) — log in
there normally, then click **Continue** in the BrowserAgent tab. The task picks up from
there using the same browser session, so you only have to log in once per site per session.
In persistent-browser (`cdp_attach`) mode that login also survives BrowserAgent restarts,
since it's stored in the persistent Chromium profile, not anything BrowserAgent manages.

## When BrowserAgent needs more information

If you ask for something that refers to a page BrowserAgent can't find — "check my course
pages" when nothing course-related is actually open — it doesn't fail with an error. It asks:

```text
Waiting for input

I understand that you want me to work with the user's course pages, but I don't have
any matching pages open or saved. Open those pages in the persistent browser, paste
their URLs, or tell me where to find them.

[text box]  [Continue]
```

Open the pages it's asking about (or just paste a URL) and click **Continue** — it picks up
right where it left off, with your answer added to the original request. This only happens in
`cdp_attach` mode, since `launch` mode has no persistent set of "your open tabs" to look at.

## Stopping and resuming

Click **Stop** to cancel a running task. It finishes whatever single step it's mid-way
through, then halts cleanly — nothing is left half-done. If BrowserAgent (or your computer)
is interrupted unexpectedly, refreshing the page reconnects to whatever was still running.

## Task history

The last 20 tasks are listed below the prompt box with their status and timestamp. Click one
to reopen its result. Prompts and results are stored **only on your own machine**
(`runtime/ui/jobs.db`) — nothing is sent anywhere except the local model (Ollama) and
whatever websites the task itself needs to visit. Click **clear** next to the history heading
to wipe it.

## Power users

Everything above talks to the same engine that `browser-agent run`, `browser-agent batch
run`, `targets.txt` files, and the benchmark scripts under `benchmarks/` already use — those
remain available for debugging and scripted/automated runs. See `README.md` and
`ARCHITECTURE.md` for that layer; you shouldn't need it for normal use.
