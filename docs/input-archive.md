# Historical input archive

`input_backup/` is a read-only collection of XGC input files spanning many
code and input-schema generations. It is evidence for building the knowledge
base, not a set of validated baselines.

## Growth

Since 2026-09-10 the archive is no longer hand-copied. Every accepted
submission of a registered simulation appends its input here as
`<directory_name>` (first submission) or `<directory_name>.<MonDD>` (a later
submission whose input changed); see "Reproduction evidence" in
`docs/simulation-catalog.md`. The exact text each job ran is always in
`runs/master/<run_id>/input`; this directory is the browsable, one-file-per-
simulation view.

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

## Simulation notebook join

`Other_data/simulation_note_XGC1.xlsx` is the researcher's own record of each
numbered run's intent and outcome, one sheet per year back to 2008. Column
headers drift across years, so `tools/case_promotion.py` maps them by name
(`shot #`/`Shotname`, `note`/`general description`, ...).

```python
rank_promotion_candidates(min_number=600, max_files=100)
```

joins the archive inventory with the notebook and classifies each run's
outcome (`success` / `failed` / `inconclusive` / `unknown`) from the
researcher's own wording via keyword rules, quoting the matched phrases as
evidence. Failure wording dominates success wording. Ranking is deterministic:
recorded successes first, then schema alignment, then recency.

On the real data this reproduces the manual analysis below: the ST40 family
ranks first on `'OK'` notes, and every recent ITER input ranks last on
`'failed'`/`'numerical instability'` notes despite high schema alignment.

## Binary eras: xgc-eem / xgc-es / xgca

Until recently the field model was selected by the *binary*, not the input:
EM runs used `xgc-eem`, ES runs used `xgc-es`, and neoclassical runs used
(and still use) `xgca`. The notebook's Code column records which one each run
used. Current XGC unifies EM/ES into one binary switched by
`sml_electromagnetic` (default `.false.` = electrostatic).

This creates a silent trap for promotion: an EM-era input carries **no**
field-model switch, passes `xgc --dryrun` cleanly, and then runs
electrostatic — the wrong physics with no error. `classify_code_column`
therefore derives the binary era from the notebook, `plan_case_promotion`
warns that migration must add `sml_electromagnetic=.true.`, and the physics
classification cites whether the field model came from the input itself or
only from the notebook.

In the n500+ range the notebook records 53 ES, 16 EM, and 34 XGCa primaries;
all 16 EM inputs lack the switch and need this migration. XGCa runs are
flagged separately: they use a different binary that this pipeline has not
validated yet.

## Drafting a case from the archive

```python
plan_case_promotion("n620fr_ST40_1101365_wo_flow")   # writes nothing
write_draft_case("n620fr...", confirm=True)          # kb/cases/drafts/
```

The plan combines the parsed input, the notebook entry, and a deterministic
physics classification in which every field cites its source (a parameter
value or the notebook's Code column); anything not derivable is `unknown`,
never guessed. Drafting a run the notebook records as failed requires
`allow_failed=True`.

Drafts live under `kb/cases/drafts/` and are invisible to `list_cases` and
the physics advisor. Finalizing one into `kb/cases/` is a human step that
requires the checklist below.

## Promotion to a validated baseline

Before adding any archive input to `kb/cases/`:

1. locate every referenced mesh, equilibrium, profile, ADIOS2, and PETSc file;
2. preserve the archived input and its SHA-256;
3. migrate removed/deprecated parameters in a separate working copy;
4. record a reviewable diff;
5. run the current `xgc --dryrun`; and
6. record the XGC commit, migrated input hash, warnings, and runtime outcome.

Until all steps pass, the status remains `unvalidated_archive`.
