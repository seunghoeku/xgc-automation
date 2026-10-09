# Mesh generation: recipes, TOMMS runs, deployments

XGC's configuration-space mesh (`.node`, `.ele`, `.flx.aif`) is produced by
`tomms_meshgen`, the command-line front end of TOMMS (Tokamak Modeling and
Meshing Software, SCOREC/RPI, built on Simmetrix). The procedure is described
at xgc.pppl.gov ("Generating Meshes for XGC Simulations"); this document is
about keeping the *recipe* of every mesh and running the tool through the
catalog so that a mesh, the run that used it and the parameters that made it
stay connected.

Module: `tools/meshes.py`. CLI: `xgc-sim mesh ...`. MCP: `mesh_index`,
`import_meshes`, `generate_mesh`, `mesh_status`, `preview_mesh`,
`deploy_mesh`, `annotate_mesh`.

## What a mesh is made from

`tomms_meshgen` takes no arguments. It reads `xgc_mesh_in` from the working
directory, and that file names everything else:

| `xgc_mesh_in` key | role | typical size |
|---|---|---|
| `eqdskFile` | equilibrium: EFIT geqdsk or XGC `.eqd` | 0.3-10 MB |
| `limiterFile` | wall curve (count, then R Z pairs); optional | ~1-10 KB |
| `inter_curve_spacing_file` | radial surface list `surf.txt` (count, then psi_N per line); used with `radial_uniform_meshing_unit -1` | ~1-10 KB |
| `intra_curve_spacing_file` | poloidal spacing `dpol.txt` (count, then psi_N and dx [m]) | ~2-25 KB |
| `inter_curve_spacing_private_file` | private-region surfaces (pairs); often named but absent, TOMMS ignores it then | <1 KB |
| `nodeFile`, `elmFile`, `addinfoflxFile` | output names; default `<eqdskFile>.node` etc. | |
| `num_phi_planes` | 1 for XGCa, the plane count for field-aligned XGC1 meshes | |
| `intra_curve_spacing_option` | -1 field-aligned (turbulence), -2 flux-expansion scaled (axisymmetric) | |

Everything but the equilibrium is a few kilobytes of text. The outputs are
0.5-130 MB and regenerable. Intermediate Simmetrix files (`.sms`, `.smb`,
`xgc_sim.log`, VTK dumps) are large and never kept.

A failed run leaves a `tomms_meshgen.<pid>.btr` backtrace beside the input
and no outputs; the usual causes (separatrix spacing too large, a surface
too close to the wall) are listed in the xgc.pppl.gov troubleshooting
section.

## Where things live

Same split as the shot index (`docs/simulation-catalog.md`, "Shot index"):

* **Catalog repository** (`paths.MESHES_DIR`, `kb/meshes/` in the user's
  catalog directory): one record `kb/meshes/<mesh_id>.yaml` and a directory
  `kb/meshes/<mesh_id>/` holding `xgc_mesh_in` and the small inputs it names
  (`surf.txt`, `dpol.txt`, the limiter, the private-region file), plus the
  failed variants under `attempts/`. Every write is synced like other
  catalog writes (`tools/catalog_sync.py`).
* **Data directory** (`<data_dir>/meshes/`): equilibria, content-addressed
  under `equilibria/<sha256[:12]>/<name>` so copies across mesh directories
  are stored once; outputs pulled on demand for a preview or a deployment
  under `<mesh_id>/`; the preview PNG.
* **The mesh machine** keeps the work tree itself (`<machine>.mesh.root`):
  for the author, `/p/epsi/sku/rpi_grid` on flux, one directory per
  equilibrium or campaign, `xgc_mesh_in` edited in place and dated copies
  (`xgc_mesh_in_apr_20`, `xgc_mesh_in_failed`) kept beside it.

`mesh_id` is the output file prefix (`tcv69271_nphi240_nt_jul_20_2026`),
which is also the name the simulation input references. No number series:
meshes are not simulations.

### The record

```yaml
mesh_id: tcv69271_nphi240_nt_jul_20_2026
origin: imported            # or automation (generated through the tools)
status: ok                  # ok | failed | running | unknown
device: tcv                 # read from the directory/equilibrium name,
shot: 69271                 #   or set by the user (device_basis says which)
geometry: {planes: 240, axisymmetric: false, field_aligned: true, ...}
headline: {num_phi_planes: '240', intra_curve_spacing_option: '-1', ...}
parameters: {...}           # every xgc_mesh_in key
recipe:
  file: tcv69271_nphi240_nt_jul_20_2026/xgc_mesh_in
  source_name: xgc_mesh_in  # which variant in the work directory
  attribution: nphi token   # how it was matched to the outputs (below)
  confidence: high
  candidates: [xgc_mesh_in]
inputs:                     # equilibrium (hash, remote path, data-dir copy),
  equilibrium: {...}        #   surf/dpol/limiter (hash, catalog copy) or
  surf: {...}               #   {name, missing: true}
outputs:                    # node/ele/flx: name, sha256, size, mtime, remote
  nodes: 28674
  triangles: 57041
source: {machine: flux, directory: /p/epsi/sku/rpi_grid/TCV_69271_benchmark}
tomms: {exe: ..., version: 2025_11_19, started_at: ..., exit_code: 0}
attempts: [{name: xgc_mesh_in_failed, saved: .../attempts/..., parameters_diff: {...}}]
deployments: [{machine: perlmutter, directory: ..., verified: true}]
used_by: [n694pe]           # derived by hash, never typed
note: ''                    # the human's
ai_note: ''                 # the agent's
```

`used_by` joins the output hashes with `referenced_inputs.files` of every
simulation record (a submission hashes the mesh files its input names), the
same mechanism as the shot index. `xgc-sim mesh link` recomputes it; the
import and the status check call it themselves.

## Importing an existing work tree

```bash
xgc-sim mesh import --dry-run            # scan, report, write nothing
xgc-sim mesh import                      # default: directories touched in the last 2 years
xgc-sim mesh import --since all          # everything
xgc-sim mesh import --since 2026-01-01 --no-equilibrium
```

One `find` lists the tree (two levels), the recipes are read in one round
trip, then the outputs and equilibria are hashed on the machine with
`sha256sum` (no Python needed there: flux's system Python is broken under
the login modules). A directory is selected when any `xgc_mesh_in*` or
`.node` in it is newer than `--since`.

One record per complete output triple. The `xgc_mesh_in` variant that
produced it is attributed by deterministic rules, in this order, and the
rule is recorded as `recipe.attribution` with a `confidence`:

| rule | example | confidence |
|---|---|---|
| `nodeFile` in the variant names the prefix | `nodeFile st40.node` | high |
| the variant's name suffix occurs in the prefix | `xgc_mesh_in_may_26` for `tcv69271_nphi160_nt_may_26_2026` | high |
| `nphiN` in the prefix matches one variant's `num_phi_planes` | `..._nphi240_...` | high |
| `xgca`/`xgc1` in the prefix matches one variant's plane count | `k030306_rmp_xgca_May_2025` | medium |
| exactly one variant written within 36 h of the output | | medium |
| a single non-failed variant | | medium; low when the directory holds several outputs (the recipe was edited in place between them) |
| otherwise | | `ambiguous`, every candidate listed, the plain `xgc_mesh_in` taken provisionally |

Variants whose name contains `fail`, `crash` or `bad` are never attributed;
they are saved under `attempts/` with the parameter difference to the final
recipe, so "this combination did not work" survives. A directory with a
recipe and no outputs becomes a `failed` record named `<directory>__no_output`.

The same mesh copied into several directories (same `.node` hash) is one
record: the directory with the oldest copy is the `source`, the others are
listed under `source.also_in`.

Device and shot are read from the directory and equilibrium names
(`TCV_69271_benchmark`, `g193793.03000_nt_addx`); `device_basis` records
that. Correct one with `xgc-sim mesh note <id> --device d3d --shot 193793`,
after which re-imports leave it alone. Nothing else is guessed: a named
input that is not in the directory is recorded as `missing`.

The import is idempotent. Re-run it after making meshes by hand on the
machine; records with `origin: automation` are never touched by it.

## Generating a mesh

```bash
xgc-sim mesh generate tcv69271_nphi128_nt --base tcv69271_nphi240_nt_jul_20_2026 \
    --set num_phi_planes=128 --description "half the planes for a cost check"
# review the parameter diff, the inputs to stage and the command, then
xgc-sim mesh generate tcv69271_nphi128_nt --base tcv69271_nphi240_nt_jul_20_2026 \
    --set num_phi_planes=128 --confirm
xgc-sim mesh status tcv69271_nphi128_nt     # until it reports ok or failed
xgc-sim mesh preview tcv69271_nphi128_nt    # PNG in the data directory
```

The plan (`plan_generation`) starts from an existing record's recipe copy,
applies the overrides, forces the output names to `<mesh_id>.node/.ele/.flx.aif`
(no `rename_mesh.sh` afterwards) and sets `SimLic` from the machine's
`mesh.tomms.license`. It writes nothing. `start_generation` (CLI `--confirm`,
MCP `confirm=True`) creates `<mesh.root>/<mesh_id>/` on the machine, pushes
the small inputs from the catalog copy, copies the equilibrium from the
base's directory when it is on the same machine (else from the data
directory), and starts

```
nohup setsid bash -lc 'cd <dir> && <mesh.tomms.env lines> && tomms_meshgen > tomms.out 2>&1; echo $? > tomms.rc'
```

detached on the login node. The login shell matters: on flux `module` is
only initialised for login shells (`MODULEPATH` from
`/etc/profile.d/scl-init.sh`), and the 2025_11_19 build needs
`intel/2023.2.0` for `libifport.so.5` and the `simmodsuite` module for the
license server. There is no scheduler and no node-hour cost; absolute rule 4
does not apply, rule 1 (confirm first) does.

`check_generation` (`xgc-sim mesh status`, MCP `mesh_status`) reads
`tomms.rc`; with exit code 0 and the three outputs present it hashes them,
reads the vertex and triangle counts from the file headers and sets
`status: ok`, otherwise `failed` with the log tail in `ai_note`.

A mesh of a few ten thousand vertices takes minutes; ITER-size field-aligned
meshes take longer. Poll when asked, do not loop.

### Mesh machine configuration

`machines.yaml`:

```yaml
flux:
  mesh:
    root: <path/to/mesh/work>        # overlay
    tomms:
      exe: /p/epsi/rpi/install/TOMMS/2025_11_19/bin/tomms_meshgen
      env:
        - module load intel/2023.2.0 openmpi/4.1.6-intel-2023.2.0 simmodsuite/2025.0-250217-dev
        - export OMP_NUM_THREADS=1
      license: /usr/pppl/Simmetrix/simmodsuite.lic
      installs: /p/epsi/rpi/install/TOMMS   # dated builds; 2026_09_22 is the newest
```

`machines.local.yaml` binds `mesh.root` (and the usual `user`, `auth`). Any
machine with a non-placeholder `mesh.root` can be the mesh machine; the
tools default to the first one.

## Checking a mesh

`xgc-sim mesh preview <id>` pulls `.node` and `.ele` into
`<data_dir>/meshes/<id>/` (refusing above `--max-mb`, default 200) and draws
the whole domain, the outboard edge and the lower region at 150 dpi. The
human looks at the magnetic axis, every place a flux surface approaches the
wall, and the X-point, as the TOMMS guide asks. Needs matplotlib:
`uv sync --extra mesh`.

## Deploying to a simulation machine

```bash
xgc-sim mesh deploy tcv69271_nphi128_nt perlmutter /pscratch/sd/s/sku/inputs/tcv69271 --confirm
```

Same machine: a remote `cp`. Otherwise the outputs are pulled into the data
directory (verified against the recorded hashes) and pushed. The remote
copies are hashed with `sha256sum` and the deployment is recorded with
`verified: true/false`. A staged simulation input then names those paths and
the next submission's `referenced_inputs` carry the same hashes, which is
what `used_by` joins on.

## Not automated (yet)

* Making `surf.txt` and `dpol.txt` from profiles and the ion gyroradius: the
  IDL procedure on xgc.pppl.gov (`retrieve_efit`, SOL extrapolation,
  resolution tables in rho_i). This is the physics step a model could
  propose; it belongs after the deterministic loop above is in daily use.
* `efit2eqd` (geqdsk to `.eqd`, `/p/epsi/sku/efit2eqd`), wall-curve editing
  (`TCV_wall_adj.txt` and the like are made by hand).
* Choosing the TOMMS build: `mesh.tomms.exe` is one path; change it in the
  overlay when a newer build is validated and the record keeps the version.
