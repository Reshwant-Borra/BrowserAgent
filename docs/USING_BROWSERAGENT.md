# Using BrowserAgent

This is the normal way to use BrowserAgent — you don't need to know anything about tasks,
batches, workflows, or the CLI commands documented elsewhere. Type what you want in plain
English and BrowserAgent figures out how to run it.

## Start it

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
