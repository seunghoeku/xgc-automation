# Unattended queue watch

Periodic "is it running yet?" polling should not go through a model. The
question has no judgement in it, and a chat loop re-reads its whole context on
every tick, so a two-hour cadence costs real money to learn that two jobs are
still `PENDING`. `tools/queue_watch.py` runs the same check from a local timer
for nothing.

It also covers a gap in `xgc-sim refresh`: refresh polls the runs the catalog
knows about, so a job submitted by hand -- no catalog record, no reserved
number -- is invisible to it. The watcher asks the scheduler what the user has
queued instead, which catches those too.

## Use

```bash
xgc-queue-watch                          # poll perlmutter, report only changes
xgc-queue-watch --always-report          # print the queue even when unchanged
xgc-queue-watch --machine polaris        # any machine whose adapter supports it
xgc-queue-watch --json --no-notify       # for scripts
```

Output is one line per change plus the resulting queue:

```
[2026-09-01T05:59:05-04:00] perlmutter: 2 job(s) queued
  CHANGED  57769800 n632pe pending -> running (128 node(s), elapsed 0:31, limit 1-00:00:00)
  FINISHED 57795329 n632pe [timeout, exit 0:0] in /pscratch/sd/s/sku/...
```

Changes also raise a desktop notification (macOS `osascript`, Linux
`notify-send`); `--no-notify` suppresses it. On macOS the notification is
silently dropped if the parent process is not allowed to post them, so treat
the log as the record of truth.

## What it reads and writes

Read-only against the machine: the scheduler's queue listing for the user, and
accounting (`sacct`) for jobs that have just left the queue. It never submits,
cancels, or writes remote files, and it does not touch the catalog -- adopting a
run into `kb/simulations/` stays a deliberate act.

Locally it keeps one snapshot per machine under `.cache/queue-watch/<machine>.json`.
Deleting it makes the next poll report the whole queue as new.

## Scheduling it

`templates/launchd/xgc-queue-watch.plist.example` is the macOS timer. Copy it
to `~/Library/LaunchAgents/`, substitute the two placeholders, and:

```bash
launchctl bootstrap gui/$UID ~/Library/LaunchAgents/gov.pppl.xgc.queue-watch.plist
launchctl kickstart -p gui/$UID/gov.pppl.xgc.queue-watch   # run once now
launchctl print gui/$UID/gov.pppl.xgc.queue-watch          # state, last exit code
launchctl bootout gui/$UID/gov.pppl.xgc.queue-watch        # stop watching
```

Logs land in `~/Library/Logs/xgc-queue-watch.log`. On Linux the same command
belongs in a crontab line.

## Credentials stay the human's job

AGENTS.md rule 3 does not bend for unattended runs. When the NERSC certificate
expires the watcher reports `unreachable`, exits 2, and notifies -- it never
attempts sshproxy itself. Because a certificate lasts 24 h, an unattended watch
goes blind once a day until the user refreshes it, so the watcher warns while
the certificate still has four hours left rather than only after it lapses.

## Scheduler coverage

Listing a whole user queue is a scheduler adapter concern
(`user_queue_argv` / `parse_user_queue` in `tools/scheduler.py`), so nothing
above that layer branches on machine type. Slurm is implemented. The PBS
adapter raises `NotImplementedError`: an untested `qstat` parser that reports
an empty queue on a machine with jobs in it would be worse than an explicit
gap.
