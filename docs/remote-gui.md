# Remote XGC_GUI Monitor

`xgc-remote-gui` launches the existing XGC-Devel GUI against an XGC simulation
directory on an HPC login node. It is an orchestration command, not another GUI:
XGC-Devel still supplies `utils/input_gui/app.py` and the Monitor implementation.

The launcher:

1. resolves a target from an absolute remote path or local run provenance;
2. verifies the remote directory, GUI source, Python dependencies, optional BP
   source, and optional `.xgcg` dashboard preset;
3. starts XGC_GUI on remote `127.0.0.1` in the simulation directory;
4. creates a loopback-only SSH port forward;
5. opens the local `/monitor` page; and
6. records enough local session state to inspect or stop the tunnel later.

It never runs `srun`, `sbatch`, or `qsub`, and it never transfers BP output.

## Configuration

Add a per-machine `gui` section to the gitignored `machines.local.yaml`:

```yaml
perlmutter:
  gui:
    source: /pscratch/sd/a/alice/XGC-Devel
    python: /pscratch/sd/a/alice/venvs/xgc-gui/bin/python3
    env_script: /pscratch/sd/a/alice/env/xgc-gui.sh  # optional
    remote_port: 5080                                # optional
```

`source` must contain `utils/input_gui/app.py`. The selected Python environment
must import `flask`, `numpy`, `matplotlib`, and `adios2`. If `remote_port` is
omitted, the launcher selects a high port for each session.

Perlmutter uses the current NERSC sshproxy key/certificate, multiplexed over
the toolchain's own socket rather than the user's. MFA hosts retain their
existing ControlMaster contract. See `docs/ssh-setup.md`.

## Resolve before launching

Explicit remote directory:

```bash
uv run xgc-remote-gui plan \
  --machine perlmutter \
  --dir /pscratch/sd/a/alice/my-simulation
```

Recorded run:

```bash
uv run xgc-remote-gui plan \
  --machine perlmutter \
  --run-id run-20260807T120000Z
```

Search local provenance first when the exact run ID is unknown:

```bash
uv run xgc-remote-gui search "TCV radia" --machine perlmutter
```

`--query` may be used directly only when it resolves to exactly one provenance
record. Ambiguous searches stop and list candidate run IDs. An agent may extract
an absolute path from the conversation, but it must pass that path explicitly;
the Python command does not read chat history.

## Open the Monitor

```bash
uv run xgc-remote-gui open \
  --machine perlmutter \
  --run-id run-20260807T120000Z
```

The local port is selected automatically. The returned JSON includes a session
ID and URL such as `http://127.0.0.1:53142/monitor`. The browser opens only after
that URL responds successfully. Use `--no-browser` to print the URL without
opening it.

Select one logical BP source:

```bash
uv run xgc-remote-gui open \
  --machine perlmutter \
  --dir /pscratch/sd/a/alice/my-simulation \
  --source xgc.oneddiag.bp
```

Load a saved dashboard preset:

```bash
uv run xgc-remote-gui open \
  --machine perlmutter \
  --dir /pscratch/sd/a/alice/my-simulation \
  --preset /pscratch/sd/a/alice/my-simulation/dashboard.xgcg
```

`--preset auto` loads the preset only when exactly one top-level `.xgcg` file
is present. Multiple candidates require an explicit choice.

## Status and cleanup

```bash
uv run xgc-remote-gui status
uv run xgc-remote-gui status gui-20260807T120000Z-ab12
uv run xgc-remote-gui stop gui-20260807T120000Z-ab12
```

Session metadata and bounded server logs live under `.cache/remote-gui/`, which
is gitignored. `stop` signals only an SSH process whose command still matches
the recorded launcher-owned tunnel; a stale or reused PID is not killed.

## MCP tools

The same deterministic operations are available to the local agent:

- `search_remote_gui_targets`
- `plan_remote_gui`
- `launch_remote_gui`
- `remote_gui_status`
- `stop_remote_gui`

The normal conversational workflow is to search or resolve, show the plan, and
then launch. No scheduler approval is involved because the GUI runs only on the
login node, but the target and local URL remain explicit in the result.
