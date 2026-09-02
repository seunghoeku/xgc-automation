---
name: f-init
description: Non-Maxwellian f_init procedure for XGC (case 0) - XGCa Maxwellian run, f0 -> f_init distribution files with poloidal/velocity smoothing, dt=0 charge-density check, density adjustment loop. Use when the user mentions f_init, f init procedure, dt0 or dt=0 check, density adjustment of a distribution, or the xgc-finit CLI.
---

# f_init procedure (case 0)

Everything lives in `f_init/`; read `f_init/README.md` once for the physics and
the parameter table. Drive the work with the `xgc-finit` CLI (`uv run
xgc-finit ...` from the repository root), never with hand-written ssh commands.
The case is a YAML under `f_init/cases/` (currently `cbc6`); copy
`example.yaml` for a new one and set `root`, `species.slot`, and `smooth`.

## Order of operations

1. `xgc-finit CASE status` - what exists. Stage A (`maxwell/`) must show a
   last f0 step and `1/N done`; if not, the XGCa run is not finished.
2. `xgc-finit CASE make-finit` - writes `xgc.f_init_no_smooth.bp` and, with
   smoothing on, `xgc.f_init_smooth_<sigma>_<width>.bp` into `maxwell/`.
   Refuses to overwrite; add `--overwrite` only if the user says so.
3. `xgc-finit CASE stage-dt0 --finit <file>` - show the printed input diff
   and node-hour line to the user, then re-run with `--confirm`.
4. `xgc-finit CASE submit dt0_<tag>` - show the cost line, get approval, then
   `--confirm` (runs `xgca --dryrun` first and stops on rejection).
5. Wait for the job (`xgc-finit CASE jobs`; the queue-watch timer also
   notifies). Do not poll from a chat loop.
6. `xgc-finit CASE adjust dt0_<tag>` - prints the density mismatch and writes
   `xgc.f_init_adjusted_<tag>.bp`. Report the flux-averaged rms/max.
7. Optional verification loop: `stage-dt0 --finit xgc.f_init_adjusted_<tag>.bp`,
   `submit`, then `check dt0_adjusted_<tag>`; the mismatch should shrink.

## Rules that apply here

- Never pass `--confirm` or `--overwrite` without the user's explicit go-ahead
  for that specific step (AGENTS.md rule 1). Plans are free; show them.
- Login-node steps (`make-finit`, `adjust`, `check`) are bounded file-access
  work and allowed there; the XGC runs go through sbatch only.
- Every step appends to `f_init/cases/CASE.log.yaml`; cite it rather than
  re-deriving what was done. Pulled JSON/PNG summaries are in `f_init/cases/CASE/`.
- Cases 1 and 2 (turbulence input -> XGCa input, mesh interpolation) are not
  implemented; say so instead of improvising them.
