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

## Choose an installation mode

For general users, use the standalone
[HPC Queue Monitor](https://github.com/seunghoeku/hpc-queue-monitor). It installs
without this checkout or any XGC/LLM dependencies. Its default server file
includes addresses, scheduler types, and authentication modes for Perlmutter,
Polaris, Aurora, and Stellar; Flux stays disabled until its scheduler is
confirmed. Users enable the servers they need and supply their own accounts
and SSH aliases. SwiftBar is installed separately.

The checkout-based workflow remains available for XGC Automation users:

| Setting | This checkout | Standalone package on macOS |
| --- | --- | --- |
| Command | `xgc-queue-watch` | `hpc-monitor` |
| Servers | `machines.yaml` + `machines.local.yaml` | `~/Library/Application Support/HPC Queue Monitor/servers.yaml` |
| Poll selection | all configured machines or `--machine` | enabled entries in `servers.yaml` |
| Cache | `.cache/queue-watch/<machine>.json` | application-support `cache/`, separated by config path and server/account definition |
| Timer setup | copy and edit the launchd template below | `hpc-monitor install` |
| launchd label | `gov.pppl.xgc.queue-watch` | `org.hpc-queue-monitor.poll` |
| Plugin | `templates/swiftbar/xgc.30s.sh` | generated `hpc-monitor.30s.sh` |
| Poll interval | plist `StartInterval` (default 600 s) | YAML `poll_interval` (default 600 s; reinstall timer after changing it) |
| Stale threshold | 20 minutes | twice `poll_interval` |
| Log | `~/Library/Logs/xgc-queue-watch.log` | `~/Library/Logs/hpc-queue-monitor.log` |

The examples below use the checkout CLI unless explicitly marked otherwise.
Standalone installation and CLI details are in its
[README](https://github.com/seunghoeku/hpc-queue-monitor#readme).

## Checkout CLI

```bash
xgc-queue-watch                          # every reachable machine, full list
xgc-queue-watch --machine polaris        # narrow to one; repeatable
xgc-queue-watch --changes-only           # log transitions, not the whole queue
xgc-queue-watch --json --no-notify       # for scripts
xgc-queue-watch --history-limit 5        # retain recent departures per machine
xgc-queue-watch --swiftbar              # display local cache only; no SSH
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

## Server selection is configured; reachability is checked per poll

The checkout polls every machine in `machines.yaml` by default; the standalone
package selects enabled entries from `servers.yaml`. Which ones can actually
be reached changes through the day: a NERSC sshproxy certificate lasts 24 h,
and an MFA host is reachable only while the user's own ControlMaster session
is alive. `probe()` checks the required credentials/session before each poll,
without interactive authentication or prompting:

| Machine kind | What is checked | Cost |
| --- | --- | --- |
| sshproxy (Perlmutter) | certificate present and inside its validity window | local, no round trip |
| ControlMaster / MFA (Polaris, Aurora, Stellar, Flux) | `ssh -O check` for a live master, then a bounded `true` that proves a non-interactive channel can still be opened | one short round trip |
| Explicit `key` authentication | no master/certificate requirement; the queue request tests public-key access with interactive authentication disabled | queue request only |
| any | scheduler type and username are filled in, not `<placeholders>` | local |

The channel probe is not redundant: `ssh -O check` only inspects the local
master process, so a stale session can pass it while every command hangs until
the remote-command timeout (120 s by default).
The separate command-channel probe allows **30 s** (including on Aurora), so
a busy login node has time to open a channel. This does not change the 60 s SSH
connection timeout.

A failed credential/session probe is **skipped with its reason** and the others
are still polled. A connection or query failure during the queue request is
recorded as an error. Both preserve the last successful snapshot. A skipped
machine alone does not fail the run if another machine was polled. Exit codes
for a poll (configuration errors in the standalone CLI return 1):

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

The checkout keeps one snapshot per machine under `.cache/queue-watch/<machine>.json`.
Deleting it makes the next poll report that machine's whole queue as new.
The standalone package keeps independent snapshots in its application-support
cache. Changing the config path or a server/account definition starts a new
history; it does not read or import this checkout's cache.

## Scheduling the checkout CLI

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

## SwiftBar menu on macOS

The standalone package's `hpc-monitor install` generates the plugin in
SwiftBar's existing Plugin Folder, or a new application-support `plugins`
directory if no folder is configured. Select the printed folder in SwiftBar
and enable its Launch at Login option. A fresh package install starts a new
monitoring history. To migrate an existing checkout setup, stop/unload its
timer and disable `xgc.30s.sh` in SwiftBar before enabling the new plugin;
the installer does not remove the old timer/plugin, so both would otherwise
poll and display separately.

For the checkout plugin, install SwiftBar and choose `templates/swiftbar`
as its **Plugin Folder**. It contains the executable `xgc.30s.sh`; the plugin
finds the repository relative to its own location and uses `.venv/bin/python`.
If copying it to an existing plugin folder instead, set `XGC_AUTOMATION_ROOT`
to the repository in the plugin's environment. No personal path is committed.

Use a SwiftBar release containing the submenu refresh fix (available in
2.1.2 beta 1 and later betas). SwiftBar 2.1.0/2.1.1 can disable server submenus
when their titles or children change; restarting the app only temporarily
restores them. See [upstream issue #515](https://github.com/swiftbar/SwiftBar/issues/515)
and [fix #518](https://github.com/swiftbar/SwiftBar/pull/518).

All plugin menu labels are in English. Pending jobs show **Est. start** beside
their requested wall time. Each scheduled poll collects the scheduler's own
estimate in the existing queue query: Slurm `%S`, or PBS JSON
`estimated.start_time`. No additional per-job SSH requests are needed.
Queries use UTC and the C locale; the normalized `estimated_start` field in
JSON/cache is ISO 8601 with a UTC offset. The text log includes that timestamp,
and SwiftBar converts it to the Mac's local time with an explicit timezone.
Missing, hidden, or invalid estimates appear as `unavailable`; old estimates
are replaced on each successful poll, including when withdrawn. A timestamp
that has passed is labeled `estimate passed`. These are scheduling estimates,
not guaranteed start times; no estimate is inferred from queue position or
dependencies. Running and finished jobs do not display a pending-start estimate.

Keep the selected polling timer running: the default interval is ten minutes.
SwiftBar reads those local snapshots every thirty seconds, so opening the menu
does not wait for SSH, authenticate, or start another poll. The menu's refresh
action also only rereads the cache. Each server submenu shows current jobs and
the latest five observed departures, with job name, job ID, node count, and
requested wall time on the same line. Each job has a detail submenu for elapsed
time / queue reason or the time its departure was observed and its exit code.
The server row summarizes its jobs with the priority running, pending,
completed, then failed. It omits the state icon when none of those states is
present. A stale or unavailable connection adds a separate warning icon before
the state icon.
`completed` means the scheduler job completed, not that an entire simulation
and its restart chain finished; `timeout` remains a separate outcome.

The watcher's `--history-limit N` controls the per-machine retained history
(default five); change the timer arguments and the plugin's display limit if
you want more. The standalone package uses one YAML `history_limit` for both
polling and display. The normal text and JSON outputs also include the recent history
(`recent_finished`). History persists across application restarts and empty
queue polls. It contains only jobs previously seen in the watcher's queue
snapshots: there is no search for jobs that ended before monitoring, and a job
that starts and ends entirely between polls cannot be observed. Existing
snapshots are compatible and provide the starting queue.

Departures are ordered by **when the watcher observed them**, newest first;
departures first seen in the same poll have no known relative finish order.
`left_queue_at` is that observation time, not an exact scheduler end time.
Queue disappearance alone does not prove success. Unavailable or delayed
accounting is shown as an unconfirmed outcome and retried on subsequent polls
while the entry remains in the bounded history. Jobs that reappear in the queue
are removed from this history.

Connection failures retain the last successful queue and its timestamp, while
recording the failed attempt separately. The menu labels those queues as cached;
snapshots older than twenty minutes in the checkout, or twice `poll_interval`
in the standalone package, are marked stale. Neither is counted
in the menu bar's live running/pending totals. Snapshot writes are atomic so a
menu refresh cannot read a half-written JSON file.

## Checkout log rotation

Logs land in `~/Library/Logs/xgc-queue-watch.log`, and at a ten-minute cadence
listing every queue they would grow without limit. `--rotate-log` in the plist
points at that same file. Once a week it is moved to `xgc-queue-watch.log.1`,
the previous archive is discarded in the same atomic `os.replace`, and the next
poll creates a fresh file at the original path. Two files exist at any time, so
history spans one to two weeks depending on where in the cycle you look.
`--log-rotation-days` changes the period; nothing else trims the files.
The standalone installer sets up the same weekly rotation for
`hpc-queue-monitor.log`, with its rotation marker in the package's cache.
It does not expose a `--log-rotation-days` option.

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
