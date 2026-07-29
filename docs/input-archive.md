# Historical input archive

`input_backup/` is a read-only collection of XGC input files spanning many
code and input-schema generations. It is evidence for building the knowledge
base, not a set of validated baselines.

## Prioritization policy

Start with globally numbered inputs at `n600` or above. Older `n*`, `su*`,
`ti*`, `pf*`, `j*`, and test inputs remain searchable but are excluded from the
default inspection result because their schemas can differ substantially from
current XGC.

The read-only MCP tool:

```python
inspect_input_archive(min_number=600, max_files=100)
```

extracts:

- simulation number and machine code from the filename;
- file size and SHA-256;
- namelist and parameter names;
- selected physics/numerics parameters;
- filename-derived descriptive tags; and
- parameter-name alignment with the newest local XGC metadata cache.

No archive file is rewritten or copied.

## Initial inventory

The archive currently contains 427 files. Sixty-six use an `nXXX...` name and
17 files are at `n600+`, representing 15 distinct simulation numbers. `n623`
has three variants, which are preserved as variants rather than treated as
three simulation IDs.

Against the cached current XGC metadata:

- one recent input has at least 95% parameter-name alignment;
- sixteen have 80–95% alignment;
- none is automatically considered validated.

Common old parameters include `f0_update_analytic_alpha`,
`sml_f0_grid_alpha_start`, `diag_tracer_n`, and `diag_tracer_period`. Some
groups also contain old neutral-grid, source, smoothing, and Poisson-solver
parameters. These require migration rules or explicit removal before dryrun.

## First migration candidates

The ST40 sequence is the best first family to reconstruct because it is recent,
has several closely related variants, and includes production notes marked OK:

- `n620fr_ST40_1101365_wo_flow`
- `n621fr_ST40_110365_flow_b_para`
- `n624fr_ST40_flip_Bt`
- `n625fr_ST40_flip_Bt_minus_flow`

`n618fr_ST40_110365_fix_field_org_dt` is a useful predecessor. The inputs share
the same high-level numerical setup; important differences are concentrated in
the flow profile selection and sign. That makes the family suitable for
baseline-plus-overrides representation.

Recent ITER inputs with high schema alignment are not automatically good
baselines: production notes report failures or instability for several of
them. Runtime outcome is stronger evidence than filename recency or schema
alignment.

## Promotion to a validated baseline

Before adding any archive input to `kb/cases/`:

1. locate every referenced mesh, equilibrium, profile, ADIOS2, and PETSc file;
2. preserve the archived input and its SHA-256;
3. migrate removed/deprecated parameters in a separate working copy;
4. record a reviewable diff;
5. run the current `xgc --dryrun`; and
6. record the XGC commit, migrated input hash, warnings, and runtime outcome.

Until all steps pass, the status remains `unvalidated_archive`.
