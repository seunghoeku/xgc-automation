# Simulation IDs and directory names

`simulation_id` is the permanent identifier for one scientific simulation.
It is separate from `run_id`, which identifies one scheduler submission or
restart attempt.

The catalog also exposes `simulation_tag` and `sub_tag`. For automation-owned
records, `simulation_tag` defaults to `simulation_id`; imported or manually
adopted work may reuse that tag. Catalog items are equal only when both tags
match, and `sub_tag` defaults to the directory basename. This grouping rule does
not change global `simulation_id` allocation.

## Format

```text
<series><sequential number><machine code>[_<secondary machine code>...]
```

There are two series, told apart by the first letter and the number width:

| `kind` | Prefix | Digits | Counter | Used for |
|---|---|---|---|---|
| `production` | `n` | 3 or more | `next_number` | physics simulations whose results will be studied |
| `dev` | `d` | 4 or more | `next_dev_number` | short test, debug, and algorithm-verification runs made while developing the code |

Examples:

```text
n645au          production, Aurora
n646do          production, Doudna
n647pe_po       production, Perlmutter then Polaris
d0001pe         dev, Perlmutter
d0002po_pe      dev, Polaris then Perlmutter
```

Within a series the number is global across machines. A failed, cancelled, or
moved simulation still consumes its number; numbers are never reused.
Historical records in `simulation_note_XGC1.xlsx` extend through `n644`, so
the production registry begins at 645. Historical duplicate numbers are treated
as record-keeping errors, not as precedent for machine-local number sequences.

The dev series starts at `d0001` (September 2026). Before it existed, code
tests consumed production numbers (n662pe through n670pe are g2 verification
runs); those keep their numbers and are marked `kind: dev` in the catalog. The
prefix is `d`, not `t`, because `t` was already the Titan-era prefix in the
historical notebook (`ti204`).

Choosing the series is a judgement about purpose, not size: a small run whose
result enters a paper is production, a 4-node smoke test of a new binary is
dev. A dev run that turns out to matter scientifically is not renumbered; it
gets a new production number, with the dev id recorded in the new record's
note or tags.

Machine codes:

| Machine | Code |
|---|---|
| Perlmutter | `pe` |
| Polaris | `po` |
| Aurora | `au` |
| Doudna | `do` |
| Frontier | `fr` |
| Summit | `su` |
| Stellar | `st` |
| STELLAR-AI (stellarai-amd) | `sa` |
| Flux | `fl` |

For a multi-machine simulation, list the primary machine first. The same
`simulation_id` follows the simulation to every listed machine.

## Directory name

The directory name is the ID plus a short physics/purpose slug:

```text
n645au_em_kbm_linear
n646do_tcv_nt_delta_f
n647pe_po_tcv_benchmark
```

Do not repeat `Aurora`, `Doudna`, or another machine name in the slug. The
registry removes machine-name and machine-code tokens during normalization.
Full machine names live in the `machines` metadata field; physics and purpose
labels live in `tags`.

## Approval workflow

```text
plan_simulation_id       preview n645au (or d0001au with kind="dev") and
        |                directory; writes nothing
  [human approves]
        |
reserve_simulation_id    atomically reserves 645 and advances the series
        |                counter (next_number or next_dev_number)
        |
stage_simulation_case    creates/copies the baseline to the canonical path
        |
plan_input               metadata validation and human-readable diff
        |
plan_job                 retains timestamp run_id for this submission attempt
```

When confirming a reservation, callers must pass the exact `expected_number`
shown in the plan and the same `kind`. If another reservation has advanced
the sequence, the stale plan is rejected rather than silently assigning a
different ID. Both tools default to `kind="production"`; the model should ask
which series a run belongs to whenever the purpose is not obvious.

Reserving a number also opens the simulation's catalog record at
`kb/simulations/<simulation_id>.yaml`, which then accumulates its runs, status,
archive location, and human note. See `docs/simulation-catalog.md`.

The mutable source of truth is `kb/simulation_registry.yaml`. Provenance stores
both identifiers:

```yaml
simulation_id: n645au
run_id: run-20260729T...
simulation:
  simulation_number: 645
  kind: production
  machines: [aurora]
  machine_codes: [au]
  directory_name: n645au_em_kbm_linear
  tags: [electromagnetic, kbm, linear]
```
