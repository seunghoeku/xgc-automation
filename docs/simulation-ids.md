# Simulation IDs and directory names

`simulation_id` is the permanent identifier for one scientific simulation.
It is separate from `run_id`, which identifies one scheduler submission or
restart attempt.

## Format

```text
n<global sequential number><machine code>[_<secondary machine code>...]
```

Examples:

```text
n645au
n646do
n647pe_po
```

The number is global across machines. A failed, cancelled, or moved simulation
still consumes its number; numbers are never reused. Historical records in
`simulation_note_XGC1.xlsx` extend through `n644`, so the registry begins at
645. Historical duplicate numbers are treated as record-keeping errors, not as
precedent for machine-local number sequences.

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
plan_simulation_id       preview n645au and directory; writes nothing
        |
  [human approves]
        |
reserve_simulation_id    atomically reserves 645 and advances next_number
        |
stage_simulation_case    creates/copies the baseline to the canonical path
        |
plan_input               metadata validation and human-readable diff
        |
plan_job                 retains timestamp run_id for this submission attempt
```

When confirming a reservation, callers must pass the exact `expected_number`
shown in the plan. If another reservation has advanced the sequence, the stale
plan is rejected rather than silently assigning a different ID.

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
  machines: [aurora]
  machine_codes: [au]
  directory_name: n645au_em_kbm_linear
  tags: [electromagnetic, kbm, linear]
```
