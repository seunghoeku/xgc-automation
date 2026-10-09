# Tape archive of simulation output: Perlmutter (NERSC HPSS)

Rules for moving a finished simulation from `$PSCRATCH` to tape and recording
where it went. Perlmutter first; the same structure will be reused for the
ALCF machines once their HPSS path is exercised.

These rules are **approximate, not mechanical**. Whenever two of them pull in
different directions for a given simulation, stop and ask the user; never
resolve the conflict silently. The catalog records the result
(`docs/simulation-catalog.md`, "Archiving"); driving `htar` is still a manual
step performed from the user's login session.

## Why now

`$PSCRATCH` purges files that have not been accessed for eight weeks and the
runs there are the only copy of several multi-TB datasets. Tape is the
long-term home; the catalog record is the index into it.

## What is archived

One simulation = one simulation directory = one `htar` archive, as the
directory stands on scratch **after** the steps below.

| Content | Rule |
|---|---|
| Inputs, logs, submit scripts, `fort.input.used`, small `.bp` setup files (`xgc.mesh.bp`, `xgc.bfield.bp`, `xgc.units.bp`, ...) | always in |
| Diagnostics (`xgc.oneddiag.bp`, `xgc.sheathdiag.bp`, `xgc.heatdiag*.bp`, ...) | always in |
| Field snapshots `xgc.3d.NNNNN.bp`, `xgc.f3d.NNNNN.bp`, `xgc.f0.NNNNN.bp` | in, unless thinned by rule 5 below |
| `<id>.aca` campaign archive and `.xgc-campaign/` | in (small; the `.aca` references the `.bp` files by path, so it is only useful again after a full restore into the same layout) |
| `restart_dir/` checkpoints | **out** of the simulation archive; see "Restart files" |
| `core*`, core dumps, `*_dryrun/`, build trees, copies of other runs, leftovers of failed attempts inside the directory, any other large file that is not XGC output | **out**; list them with sizes before excluding |

### Restart files

Checkpoints are archived only when they are needed (a continuation is
planned or plausible), and then as their **own** archive, never inside the
simulation archive. Before running `htar`:

```bash
cd $PSCRATCH/xgc-runs            # or wherever the simulation lives
mv n674pe_case/restart_dir restart_n674pe
```

The simulation directory is archived without the checkpoints. `restart_n674pe`
is archived separately (`restart_n674pe.tar`) if wanted, or deleted once the
simulation archive is verified. XGC never deletes old `xgc.restart.NNNNN.bp`
files, so a `restart_dir` routinely holds ten checkpoints of hundreds of GB
each; if the directory is kept at all, keep the newest one or two.

### Thinning large output (rule 5)

Some files may be left out of the archive when the data size is out of
proportion to the physics in it. Typical cases:

- `xgc.f0.NNNNN.bp` written far more often than intended;
- `xgc.3d` / `xgc.f3d` written at a short period in a run whose simulated
  time is short (a delta-f run with few particles, for example): keep only
  the long-period steps (every N-th file) and drop the rest;
- any other departure from the usual size pattern of a run of that kind.

**Always ask before excluding.** Present the candidate files, their count and
total size, what would remain, and the rationale; exclude only what the user
confirms. Write the exclusion (which files, which step period kept, why) into
the record's `ai_note` so a later reader knows the archive is not the whole
directory.

### Dev runs and failed runs

- `d`-series (debug, test, verification) runs are **not archived by
  default**. When such runs back a report, talk, or paper they may be bundled
  into **one** archive (one tar for the set, named after the purpose).
- A `failed` simulation may be skipped entirely. Ask; do not archive failed
  output without a yes, and do not delete it without one either.

## Naming

Default: the archive is named after the simulation directory, which starts
with the simulation id (`n674pe_d3d_qh_em.tar`). Rename when:

- the directory name carries little information (`n645pe.tar`, `test3.tar`);
- the name collides with another archive already on HPSS;
- a different name states the simulation more clearly;
- a suffix naming the paper or presentation the run supports makes it easier
  to find later (`n675pe_tcv_nt_matched_fsa_APS2026.tar`).

Keep the simulation id at the front in every case so the catalog and the tape
stay joined by the number. Confirm the final name before creating the
archive.

## Procedure

Run from the user's own Perlmutter session, never from automation.

1. **Check the catalog.** The simulation must have a record and must not be
   in flight (`submitted`, `running`, `interrupted` with a restart planned).
   The outcome is noted (`note` or `ai_note`). `xgc-sim show <id>`.
2. **Inventory the directory.** `du -sh` of the directory, `restart_dir`,
   and anything matching the exclusion list; count of `xgc.3d`, `xgc.f3d`,
   `xgc.f0` files. Decide the restart handling and any thinning with the
   user (rules above).
3. **Move `restart_dir` up** (see "Restart files").
4. **Create the archive** with `htar` on a login node for anything that fits
   in a session, or in an `xfer` job for multi-TB directories (the `xfer`
   QOS exists for HPSS transfers and does not charge node hours; check the
   current NERSC docs for its header before submitting):

   ```bash
   htar -cvf /home/s/<user>/<name>.tar <directory>
   # with exclusions: build a file list first and pass it with -L
   find <directory> -type f ! -name 'core*' ! -path '*/xgc.f0.*' > /tmp/<name>.list
   htar -cvf /home/s/<user>/<name>.tar -L /tmp/<name>.list
   ```

   HPSS home is `/home/<first letter>/<user>` (`/home/s/sku`). Two `htar`
   traps: a single member file cannot exceed `htar`'s member-size limit
   (68 GB at the time of writing; a large aggregated `.bp` subfile can hit
   it, in which case use `hsi put` for that file or split the archive by
   step range), and one archive should stay in the hundreds-of-GB to
   few-TB range, so split a very large run by step range
   (`<name>_steps0000-2000.tar`).
5. **Verify** by listing the archive and comparing member count and sizes
   against the directory:

   ```bash
   htar -tvf /home/s/<user>/<name>.tar | tail
   hsi ls -l /home/s/<user>/<name>.tar
   ```
6. **Record it:**

   ```bash
   xgc-sim archive n674pe --system hpss --path /home/s/sku/n674pe_d3d_qh_em.tar
   xgc-sim ai-note n674pe "HPSS archive excludes restart_dir (moved to restart_n674pe, deleted) and xgc.f0.* (kept every 500th step)"
   ```

   One record, one `archive.path`. A separate restart archive goes into
   the `ai_note`, not into `archive.path`.
7. **Only then clean scratch**, following the existing deletion rule: save
   the small provenance files into the catalog's `runs/<class>/<run_id>/remote_files/`,
   confirm with the user, `rm -rf` the directory and the restart copy.
   The catalog repository holds only text provenance, figures and small
   reduced data: **about 20 MB per simulation**, up to 50 MB when the
   files warrant it, and anything beyond that only with the user's
   explicit permission. No `.bp` products
   are copied off the machine when the directory is deleted; the tape
   archive is their copy. (Inputs, logs, submit scripts, timing files and
   analysis scripts fit; `xgc.oneddiag.bp` and the mesh do not.)

## Checklist the agent presents before anything is created

```
simulation   n674pe  (status analyzed, note present)
directory    /pscratch/sd/s/sku/xgc-runs/n674pe_d3d_qh_em   12.4 TB
restart_dir  10 checkpoints, 8.1 TB  -> mv to ../restart_n674pe, not archived / archived separately?
exclude      core.12345 (3.1 GB), n674pe_dryrun/ (40 MB)
thin?        xgc.f0.*.bp: 160 files, 2.9 TB, period 25 -> keep period 500 (8 files, 145 MB)?  [ask]
archive      /home/s/sku/n674pe_d3d_qh_em.tar   est. 4.3 TB   name OK?
```

Nothing runs until the user answers each `?`.

## What is already on tape

`xgc-sim hpss perlmutter` prints the parsed HPSS listing kept in the catalog
(`kb/archives/perlmutter_hpss.yaml`, refreshed with `--refresh`), and
`xgc-sim show <id>` shows a record's tape entries. Check it before archiving
so a simulation is not put on tape twice, and refresh it after a transfer so
the new archive is indexed and linked (`docs/simulation-catalog.md`, "Tape
index").

## Not yet done

- `tools/` do not drive `htar`; the record is written after a manual
  transfer. A `plan_archive` tool that produces the checklist above from
  the remote inventory is the natural next step (`docs/roadmap.md`).
- Polaris and Aurora (ALCF HPSS) and stellar-ai (no tape; project space
  only) need their own sections once a transfer has been exercised there.
