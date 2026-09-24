# HPC-Campaign archives on completion

XGC itself writes only ADIOS2 `.bp` products and has no campaign support. An
HPC-Campaign archive (`.aca`, an SQLite file that ADIOS2 can open like one
dataset) is made afterwards by XGC-Analysis's packager,
`python -m xgc_analysis.catalog.package_xgc_run`, which drives
`hpc_campaign manager`. `tools/campaign.py` runs that packager when a
simulation finishes and records the result in the catalog.

## When it runs

| Event | What happens |
|---|---|
| A simulation turns `completed` (seen by `xgc-sim refresh`, `refresh_simulations`, or a `job_status` check) | If the machine has `campaign.auto: true` and the simulation's kind is in `auto_kinds` (default: `production`), the packager starts detached on the login node |
| Every later refresh | Pending builds are retried and running ones are checked for a result |
| `interrupted` (wall-clock timeout) | Nothing happens. The restart chain has not finished yet |
| Completed again after a restart | The existing archive is marked `stale` and is **not** replaced automatically |

Refresh starts the packager and returns immediately; it never waits for the
build. Nothing polls on its own: a build is started or collected only when a
refresh or status check runs. `queue_watch` stays read-only and does not do
this.

## Where the output goes

```
<run_dir>/<simulation_id>.aca          present only when complete
<run_dir>/.xgc-campaign/
    <simulation_id>.aca                the archive while it is being built
    xgc_run_manifest.json              packager manifest (also embedded)
    build.log                          packager output
    result.json                        exit code and finish time
    lock/                              present while a build runs
```

The archive is built inside `.xgc-campaign/` and moved into the run directory
only on success. A `<simulation_id>.aca` next to the output is therefore
always a finished archive. The packager runs under `timeout` at the six-hour
login-node limit (`docs/data-analysis.md`), because packaging is dominated by
file access.

## Catalog field

Each record gets a `campaign` mapping:
- `state`: `pending`, `building`, `built`, or `failed`.
- `for_run_id`: the completed run the archive describes.
- `archive`, `log`, `machine`, `hostname`.
- The start and finish times, `exit_code` and `size_bytes`.
- `reason` when something is wrong.
- `stale: true` once a later run has completed.

| State | Meaning | Retried automatically |
|---|---|---|
| `pending` | Not started: the machine was unreachable, or the interpreter cannot import `xgc_analysis` and `hpc_campaign` | yes, on every refresh |
| `building` | Started; no result yet | collected on every refresh |
| `built` | The archive is in place | – |
| `failed` | The packager failed or timed out (see `reason`, which includes the log tail), or an archive not made by this tool was already present | no; retry by hand |

## Manual use

```bash
xgc-sim campaign n645pe                      # plan and command; contacts nothing
xgc-sim campaign n645pe --confirm            # start it
xgc-sim campaign n645pe --status             # collect the result
xgc-sim campaign n645pe --replace --confirm  # rebuild over an existing archive
```

The MCP tools are `build_campaign(record_id, confirm, replace)` and
`campaign_status(record_id)`. Manual builds work for any kind and ignore
`campaign.auto`. `--replace` overwrites the archive, so it follows AGENTS.md
rule 1: get the user's agreement first.

## Configuration (`machines.local.yaml`)

```yaml
perlmutter:
  campaign:
    auto: true                    # off unless set
    pythonpath: /global/homes/s/sku/git/XGC-Analysis
    # python: ...                 # default: analysis.python
    # env_script: ...             # default: analysis.env_script, then xgc.env_script
    hostname: NERSC               # REQUIRED HPC-Campaign host alias (--hostname)
    # auto_kinds: [production]    # add dev to package test runs too
    # timeout_seconds: 21600      # capped at the six-hour login-node limit
    # archive_name: "{simulation_id}.aca"
```

`hostname` is required: without it `hpc_campaign` records the login node's
FQDN (e.g. `polaris-login-04.hsn.cm.polaris.alcf.anl.gov`) as the data's host,
and ADIOS then opens the data through its remote path even on that same
machine: variables list, but reads fail with `EVPathRemoteFile ... FileNotOpen`
(verified on Polaris and Aurora 2026-09-24, hpc-campaign 0.7.1). Until it is
set, builds stay `pending`.

Chosen aliases: `NERSC-Perlmutter`, `ALCF-Polaris` and `ALCF-Aurora`; the ALCF two are kept separate because the
two machines have different data filesystems (`/grand` and `/lus/flare`) and
different home filesystems. Each machine's `~/.config/hpc-campaign/config.yaml`
names the same alias (plus `campaignstorepath` and `cachepath` under
`<project space>/hpc-campaign/`). With that, a test archive read its data on
the machine that wrote it; the variables were listed and all steps came back.

`hpc-campaign` 0.7.x writes ACA 0.7 and needs ADIOS2 2.12 (0.6 pairs with
2.11). Install it in a dedicated venv, never into an environment whose adios2
came from conda: pip then lays the 2.12 wheel over conda's files, the old
`adios2_bindings.cpython-312-*.so` shadows the new `abi3.so`, and
`import adios2` fails with `cannot import name 'DerivedVarType'` (this broke
Perlmutter's `myenv3_12`, the analysis and GUI interpreter, on 2026-09-24).
Perlmutter and Aurora use dedicated venvs; Polaris's venv already had pip's
adios2 2.12. Aurora therefore uses a dedicated venv with pip's adios2 2.12 and
`env_script: ""`, so the `adios2/2.11.0` module from `aurora.sh` is never
loaded alongside it.

The interpreter must import both `xgc_analysis.catalog.package_xgc_run` and
`hpc_campaign`. The start script checks this first; a failing check leaves the
build `pending` rather than `failed`, so installing the package later is
enough and the next refresh picks the build up. Install the package into the
interpreter:

```bash
python -m pip install hpc-campaign
```

Or install XGC-Analysis with its extra:

```bash
python -m pip install -e '/path/to/XGC-Analysis[campaign]'
```

## Reading the archive elsewhere

The archive records host aliases. A reader on another machine needs the same
alias in `~/.config/hpc-campaign/hosts.yaml`; otherwise the XGC GUI Monitor
warns "Campaign host(s) not configured locally". From Python, open it with
`xgc_analysis.catalog.open_campaign_catalog(path)`. In the GUI, use Monitor →
`XGC Catalog` mode.
