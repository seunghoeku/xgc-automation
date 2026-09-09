# Unattended queue watch

Periodic "is it running yet?" polling should not go through a model. The
question has no judgement in it, and a chat loop re-reads its whole context on
every tick, so a ten-minute cadence would cost real money to learn that two
jobs are still pending. `tools/queue_watch.py` runs the same check from a
local timer for nothing, which is what makes a short cadence affordable.

It also covers a gap in `xgc-sim refresh`: refresh polls the runs the catalog
knows about, so a job submitted by hand -- no catalog record, no reserved
number -- is invisible to it. The watcher asks the scheduler what the user has
queued instead, which catches those too.

## Use

```bash
xgc-queue-watch                          # every reachable machine, full list
xgc-queue-watch --machine polaris        # narrow to one; repeatable
xgc-queue-watch --changes-only           # log transitions, not the whole queue
xgc-queue-watch --json --no-notify       # for scripts
xgc-queue-watch --rotate-log ~/Library/Logs/xgc-queue-watch.log  # archive weekly
```

Output is one block per machine: what changed, then the queue as it stands.

```
[2026-09-08T13:30:24-04:00] perlmutter: skipped -- SSH certificate for 'perlmutter' is missing or expired (certificate is not current); refresh it with the user's NERSC sshproxy workflow
[2026-09-08T13:30:32-04:00] polaris: 2 job(s) queued
  NEW      7600026 n649po [running] 32 node(s), elapsed 00:38:20, limit 06:00:00
  now      7600026 n649po [running] 32 node(s), elapsed 00:38:20, limit 06:00:00
  now      7600027 n649po [pending] 32 node(s), dependency hold; afterany:7600026, limit 06:00:00
[2026-09-08T13:30:32-04:00] 1 polled, 3 skipped, 0 failed; 2 job(s) queued in total
```

A desktop notification (macOS `osascript`, Linux `notify-send`) is raised for
changes only -- one banner per poll covering every machine, not one per
machine, and nothing at all for a queue that merely still exists. The full
list goes to the log every poll regardless. `--no-notify` suppresses the
banner. On macOS it is silently dropped if the parent process is not allowed
to post notifications (the launchd agent posts as **Script Editor**, so that
is the entry to enable in System Settings -> Notifications), so treat the log
as the record of truth.

## Reachability is decided per poll, not configured

Every machine in `machines.yaml` is polled by default. Which ones can actually
be reached changes through the day: a NERSC sshproxy certificate lasts 24 h,
and an MFA host is reachable only while the user's own ControlMaster session
is alive. `probe()` establishes that before each poll, without authenticating
and without anything that can prompt:

| Machine kind | What is checked | Cost |
| --- | --- | --- |
| sshproxy (Perlmutter) | certificate present and inside its validity window | local, no round trip |
| ControlMaster / MFA (Polaris, Stellar, Flux) | `ssh -O check` for a live master, then a bounded `true` that proves a non-interactive channel can still be opened | one short round trip |
| any | scheduler type and username are filled in, not `<placeholders>` | local |

The channel probe is not redundant: `ssh -O check` only inspects the local
master process, so a stale session can pass it while every command hangs until
the 60 s SSH timeout.

An unreachable machine is **skipped with its reason** and the others are still
polled. That is normal operation on a laptop, not an error, so it does not
fail the run. Exit codes:

| Code | Meaning |
| --- | --- |
| 0 | at least one machine polled |
| 1 | a poll broke unexpectedly |
| 2 | every requested machine was unreachable -- the watch is blind |

## What it reads and writes

Read-only against the machine: the scheduler's queue listing for the user, and
accounting for jobs that have just left the queue. It never submits, cancels,
or writes remote files, and it does not touch the catalog -- adopting a run
into `kb/simulations/` stays a deliberate act.

Locally it keeps one snapshot per machine under `.cache/queue-watch/<machine>.json`.
Deleting it makes the next poll report that machine's whole queue as new.

## Scheduling it

`templates/launchd/xgc-queue-watch.plist.example` is the macOS timer. Copy it
to `~/Library/LaunchAgents/`, substitute the two placeholders, and:

```bash
launchctl bootstrap gui/$UID ~/Library/LaunchAgents/gov.pppl.xgc.queue-watch.plist
launchctl kickstart -p gui/$UID/gov.pppl.xgc.queue-watch   # run once now
launchctl print gui/$UID/gov.pppl.xgc.queue-watch          # state, last exit code
launchctl bootout gui/$UID/gov.pppl.xgc.queue-watch        # stop watching
```

`StartInterval` is the cadence in seconds; the default is 600. On Linux the
same command belongs in a crontab line.

## Log rotation

Logs land in `~/Library/Logs/xgc-queue-watch.log`, and at a ten-minute cadence
listing every queue they would grow without limit. `--rotate-log` in the plist
points at that same file. Once a week it is moved to `xgc-queue-watch.log.1`,
the previous archive is discarded in the same atomic `os.replace`, and the next
poll creates a fresh file at the original path. Two files exist at any time, so
history spans one to two weeks depending on where in the cycle you look.
`--log-rotation-days` changes the period; nothing else trims the files.

Rotation is what fits launchd, and two alternating log names would not:
`StandardOutPath` is fixed in the plist, so writing to a different name each
week would need the plist rewritten and reloaded from inside a poll. Moving the
content out from under one fixed name needs neither, because launchd opens that
path afresh for every poll.

Two details follow from stdout being bound to the inode rather than the name:

- Rotation runs **after** the report, so the rotating poll's lines close out
  the week being archived instead of being lost. The next poll starts the new
  file.
- When the week began cannot be read from the log itself -- its mtime moves
  with every poll -- so a marker file under `.cache/queue-watch/` records it.
  A missing or unreadable marker counts as due: the live file is archived and
  a dated period starts, which loses nothing.

## Credentials stay the human's job

AGENTS.md rule 3 does not bend for unattended runs. The watcher never attempts
sshproxy or an MFA handshake itself; it reports what needs refreshing and polls
whatever else it can. Because a NERSC certificate lasts 24 h, an unattended
watch goes blind to Perlmutter once a day until the user refreshes it, so the
watcher warns while the certificate still has four hours left rather than only
after it lapses.

## Scheduler coverage

Listing a whole user queue is a scheduler adapter concern, so nothing above
that layer branches on machine type. Two hooks in `tools/scheduler.py` cover
both shapes of scheduler:

- `user_queue_argv` / `parse_user_queue` -- name the user's jobs.
- `user_queue_detail_argv` / `parse_user_queue_detail` -- optional second round
  trip that fills in the fields the first could not carry. Returning `None`
  means phase one was already complete.

**Slurm** answers in one call: `squeue -u <user>` with an explicit format that
already carries state, node count, elapsed and limit times, queue reason, and
working directory, so the detail hook is unused.

**PBS** needs both, because its two formats each withhold something:

- `qstat -u <user>` exists but only in the table format, which truncates the
  job id at a fixed column width and marks the loss with `*`. A truncated id
  cannot be a snapshot key or be passed back to qstat, and `-w` still
  truncates. `qselect -u <user>` prints whole ids, one per line, and prints
  nothing for a user with no jobs.
- `-F json` is ignored when combined with `-u`, but honoured for explicit ids.
  So the detail pass is `qstat -f -F json <ids>`, which needs no column
  parsing at all.

PBS also does not report `PBS_O_WORKDIR` in qstat output at every site, so the
working directory is taken from `Output_Path`, which defaults to the submission
directory. A dependency hold is read out of `Hold_Types` and `depend` and
reported as e.g. `dependency hold; afterany:7600026` -- a chained restart is
the normal shape of a long XGC run, so it has to read as intentional rather
than as a stuck job.

The parsers were built against captured live output (Slurm on Perlmutter, PBS
Pro 2026.1 on Polaris); the PBS fixtures in `tests/test_queue_watch.py` are
that output, trimmed to the keys the adapter reads.
