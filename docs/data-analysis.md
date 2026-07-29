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

## Planned scientific reducers

Add reducers only when a validated case produces the required diagnostics.
The likely sequence is:

1. time/performance summary from bounded text logs;
2. profile and spectrum summaries for core simulations;
3. divertor heat-load summaries for an edge-SOL case with
   `xgc.heatdiag2.bp`.

Each reducer must document its required XGC products, call reusable
`xgc_analysis` code where available, write a compact JSON/CSV/PNG artifact,
and enforce a transfer-size ceiling. Raw `.bp` pull is never an option.
