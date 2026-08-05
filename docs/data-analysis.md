# Data analysis workflow

XGC output is ADIOS2 and may be several terabytes. Analysis follows one hard
boundary:

> Reduce on the HPC machine; transfer only bounded summary products.

## Relationship to XGC GUI Monitor

The XGC GUI Monitor supports both direct directory scanning and catalog-backed
browsing. Its scientific plugins are thin adapters that delegate calculations
to `xgc_analysis`, which is the correct reusable layer for future reducers.
For example, the divertor plugins operate on `xgc.heatdiag2.bp` through
`xgc_analysis.divertor_eich`.

This project does not duplicate the Monitor UI or place scientific transforms
in GUI code. `tools/data_analyzer.py` provides a headless reducer boundary that
can call `xgc_analysis` remotely and store only small derived artifacts locally.

## First product: run inventory

```python
analyze_run("stellar", "run-20260729T022500Z", product="inventory")
```

The inventory:

- resolves the remote directory from the run's provenance record;
- scans only its top level;
- records ADIOS2 product names and small-file metadata;
- never descends into or reads a `.bp` product;
- limits the number of returned entries and JSON payload size; and
- stores the result in `runs/<run_id>/summaries/inventory.json`.

If that file already exists, replacement is refused unless
`confirm_overwrite=True`.

## First scientific product: bounded 1-D profiles

The updated XGC-Analysis catalog supports selected logical-step reads. The
`oned_profiles` reducer uses that API on the remote machine:

```python
analyze_run(
    "stellar",
    "run-20260729T022500Z",
    product="oned_profiles",
    max_profile_points=512,
)
```

It opens the catalog with metadata restricted to `xgc.oneddiag.bp` and state
tracking disabled. It then reads only the first and latest logical steps of
the supported density, perpendicular-temperature, and parallel-flow channels.
The radial grid is downsampled to at most `max_profile_points`.

The local artifacts are:

- `runs/<run_id>/summaries/oned_profiles.json`, including reader version,
  module path, checkout commit when available, exact logical steps,
  source/returned radial sizes, and raw diagnostic values; and
- `runs/<run_id>/summaries/oned_profiles.svg`, rendered locally from that
  bounded JSON without transferring a BP store.

The temperature channel is deliberately labeled *perpendicular temperature*.
The reducer does not call it total temperature because that derived quantity
requires trustworthy species masses and parallel-flow correction.

Replacing either artifact requires `confirm_overwrite=True`.

### Remote analysis environment

The remote Python must be able to import the updated `xgc_analysis` package.
The reducer uses the machine's XGC environment script by default. Since your
XGC-Analysis checkout is personal, the `analysis:` block belongs in your
gitignored `machines.local.yaml` (the `.example` shows it under perlmutter):

```yaml
perlmutter:
  analysis:
    python: python3
    pythonpath: /remote/path/to/XGC-Analysis
    env_script: /remote/path/to/analysis-environment.sh
```

`pythonpath` may also be a list. These are remote paths and must be configured
per machine; the automation code does not hardcode a developer checkout. No
machine ships with an `analysis:` block preconfigured — without one,
`oned_profiles` fails with a "No module named 'xgc_analysis'" hint and the
bounded `inventory` product still works (it needs only the stdlib).

The compatible branches inspected during implementation were:

- XGC-Analysis: `codex/catalog-fast-open` at `252c3ea`
- XGC_reader: `XGC_analysis` at `43b4122`

XGC_reader now uses XGC-Analysis as its default backend, which validates the
combined reader direction. The reducer calls the canonical XGC-Analysis
catalog directly because that is where explicit logical-step selection lives;
using the legacy facade would materialize more data than this workflow needs.

## Planned reducers

Add further reducers only when a validated case produces the required
diagnostics. The likely sequence is:

1. time/performance summary from bounded text logs;
2. spectrum summaries for core simulations; and
3. divertor heat-load summaries for an edge-SOL case with
   `xgc.heatdiag2.bp`.

Each reducer must document its required XGC products, call reusable
`xgc_analysis` code where available, write a compact JSON/CSV/SVG/PNG artifact,
and enforce a transfer-size ceiling. Raw `.bp` pull is never an option.
