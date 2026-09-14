"""``xgc-finit``: drive the non-Maxwellian f_init procedure for one case.

    xgc-finit CASE status
    xgc-finit CASE make-finit [--sigma-theta S] [--v-width W] [--step N] [--overwrite]
    xgc-finit CASE stage-dt0 --finit FILE [--dir NAME] [--confirm] [--overwrite]
    xgc-finit CASE stage-run --finit FILE [--dir NAME] [--confirm] [--overwrite]
    xgc-finit CASE dryrun DIR
    xgc-finit CASE submit DIR [--confirm] [--skip-dryrun]
    xgc-finit CASE jobs
    xgc-finit CASE adjust DIR [--factor F] [--check-only] [--ref-step N] [--cmp-step N] [--overwrite]
    xgc-finit CASE check DIR
    xgc-finit CASE compare [DIR ...] [--psi P] [--shift-finit] [--paper] [--no-particles]
    xgc-finit CASE extend-maxwell [--confirm]
    xgc-finit CASE distribution [--psi P] [--node N] [--paper]
    xgc-finit CASE er-profile [--paper]
    xgc-finit CASE log

``CASE`` is a name under ``f_init/cases/`` or a path to a case YAML. Anything
that writes a directory or consumes allocation is a plan until ``--confirm``
is given; see ``f_init/README.md`` for the procedure itself.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from . import config as cfg
from . import procedure as proc


def _print(data: Any, as_json: bool) -> None:
    if as_json:
        print(json.dumps(data, indent=2, default=str))


def cmd_status(case: cfg.Case, args: argparse.Namespace) -> int:
    inv = proc.inventory(case)
    if args.json:
        _print(inv, True)
        return 0
    print(f"{case.name}: {case.root} on {case.machine}")
    header = f"{'directory':<28} {'input':<5} {'f0 last':>7} {'2d steps':<22} {'jobs':<9} f_init files"
    print(header)
    print("-" * len(header))
    for d in inv["dirs"]:
        steps = d["twod_steps"]
        if len(steps) > 3:
            steps_text = f"{steps[0]}..{steps[-1]} ({len(steps)})"
        else:
            steps_text = " ".join(str(s) for s in steps)
        jobs = f"{d['finished_jobs']}/{d['job_outputs']} done"
        print(f"{d['name']:<28} {'yes' if d['has_input'] else 'no':<5} "
              f"{d['last_f0_step'] if d['last_f0_step'] is not None else '-':>7} "
              f"{steps_text:<22} {jobs:<9} {' '.join(d['f_init_files'])}")
    maxwell = inv["maxwell"]
    if maxwell is None:
        print(f"\nWARNING: stage A directory {case.maxwell_dir!r} not found")
    elif maxwell["last_f0_step"] is None:
        print(f"\nstage A ({case.maxwell_dir}) has no xgc.f0 snapshot yet")
    else:
        print(f"\nstage A ({case.maxwell_dir}) last f0 step {maxwell['last_f0_step']}; "
              f"f_init files: {maxwell['f_init_files'] or 'none'}")
    return 0


def cmd_make_finit(case: cfg.Case, args: argparse.Namespace) -> int:
    payload = proc.make_finit(
        case, sigma_theta=args.sigma_theta, v_width=args.v_width, step=args.step,
        overwrite=args.overwrite, out_dir=args.out_dir,
    )
    if args.json:
        _print(payload, True)
        return 0
    print(f"f0 step {payload['f0_step']}: {payload['nnodes']} nodes, "
          f"v-grid {payload['nvperp']}x{2 * payload['nvpara'] + 1}")
    print(f"smoothing: sigma_theta={payload['sigma_theta']} rad, v_width={payload['v_width']} cells")
    for out in payload["outputs"]:
        stats = payload["f_g_stats"].get(out["kind"], {})
        verb = "kept " if out.get("reused") else "wrote"
        print(f"  {verb} {out['path']}  (|f_g| max {stats.get('abs_max', 0):.3e}, rms {stats.get('rms', 0):.3e})")
    return 0


def _print_resources(res: dict[str, Any]) -> None:
    print(f"  nodes={res['nodes']} walltime={res['walltime']} qos={res['qos']} "
          f"account={res['account']} -> up to {res['node_hours']} node-hours")


def cmd_stage_dt0(case: cfg.Case, args: argparse.Namespace, *, run: bool = False) -> int:
    planner = proc.plan_run if run else proc.plan_dt0
    plan = planner(case, args.finit, dir_name=args.dir)
    if args.json and not args.confirm:
        _print(plan, True)
        return 0
    label = "production-run" if run else "dt=0"
    print(f"{label} directory: {plan['target_dir']}" + ("  (EXISTS)" if plan["exists"] else ""))
    print(f"copy from {case.maxwell_dir}/: {', '.join(plan['copy_files'])}")
    print("input changes:")
    for note in plan["input_notes"]:
        print(f"  {note}")
    print(plan["input_diff"])
    print(f"{plan['script_name']} changes:")
    for note in plan["script_notes"]:
        print(f"  {note}")
    _print_resources(plan["resources"])
    if not args.confirm:
        print("\nPlan only. Re-run with --confirm to write the directory.")
        return 0
    result = proc.stage_dt0(case, plan, overwrite=args.overwrite)
    print(f"\nwrote {result['target_dir']}: {', '.join(result['written'])}")
    print(f"next: xgc-finit {case.name} submit {plan['dir_name']} --confirm")
    return 0


def cmd_dryrun(case: cfg.Case, args: argparse.Namespace) -> int:
    result = proc.dryrun(case, args.dir)
    if args.json:
        _print(result, True)
    else:
        print(f"{result['dir']}: {result['exe']} --dryrun")
        print(result["summary"])
    return 1 if result["blocking"] else 0


def cmd_submit(case: cfg.Case, args: argparse.Namespace) -> int:
    if not args.confirm:
        plan = proc.submit_plan(case, args.dir)
        if args.json:
            _print(plan, True)
            return 0
        print(f"would run: sbatch {plan['script']}")
        _print_resources(plan["resources"])
        print("\nPlan only. Re-run with --confirm to submit (a dryrun gate runs first).")
        return 0
    result = proc.submit(case, args.dir, skip_dryrun=args.skip_dryrun)
    if args.json:
        _print(result, True)
        return 0
    if result["dryrun"]:
        print("dryrun: " + result["dryrun"]["summary"].splitlines()[0])
    print(f"submitted job {result['job_id']} for {result['dir']}")
    _print_resources(result["resources"])
    return 0


def cmd_jobs(case: cfg.Case, args: argparse.Namespace) -> int:
    rows = proc.job_states(case)
    if args.json:
        _print(rows, True)
        return 0
    if not rows:
        print("no jobs submitted through xgc-finit for this case")
        return 0
    for row in rows:
        exit_text = f" exit {row['exit_code']}" if row.get("exit_code") else ""
        print(f"{row['job_id']:>10}  {row['dir']:<28} {row['state']}{exit_text}  (submitted {row['submitted']})")
    return 0


def cmd_adjust(case: cfg.Case, args: argparse.Namespace, *, check_only: bool = False) -> int:
    payload = proc.adjust(
        case, args.dir, factor=getattr(args, "factor", None),
        check_only=check_only or getattr(args, "check_only", False),
        ref_step=args.ref_step, cmp_step=args.cmp_step,
        overwrite=getattr(args, "overwrite", False),
    )
    if args.json:
        _print(payload, True)
        return 0
    m = payload["metrics"]
    print(f"{args.dir}: f_init {payload['finit']}")
    print(f"  reference {case.maxwell_dir}/xgc.2d.{payload['ref_step']:05d}.bp vs dt0 xgc.2d.{payload['cmp_step']:05d}.bp")
    raw, fsa = m["raw_dn_over_n"], m["fsa_dn_over_n"]
    print(f"  (n_ref - n_dt0)/n  node-wise: rms {raw['rms']:.3e}, max {raw['abs_max']:.3e}")
    print(f"  (n_ref - n_dt0)/n  flux-avg : rms {fsa['rms']:.3e}, max {fsa['abs_max']:.3e}, mean {fsa['mean']:.3e}")
    if payload.get("output"):
        rng = m.get("scale_range", [None, None])
        print(f"  wrote {payload['output']}  (scale factor {rng[0]:.4f} .. {rng[1]:.4f}, factor {payload['factor']})")
        print(f"next: xgc-finit {case.name} stage-dt0 --finit {payload['output'].rsplit('/', 1)[-1]}")
    for key, path in payload.get("local", {}).items():
        print(f"  {key}: {path}")
    if payload.get("png_error"):
        print(f"  plot skipped: {payload['png_error']}")
    return 0


def cmd_compare(case: cfg.Case, args: argparse.Namespace) -> int:
    payload = proc.compare(
        case, args.dirs or None, labels=args.label or None, psi=args.psi,
        f0_step=args.f0_step, particles=not args.no_particles, end_window_ms=args.end_window,
        shift_finit=args.shift_finit, paper=args.paper, twod_runs=args.twod_runs or None,
        twod_scale=args.twod_scale,
    )
    if args.json:
        _print(payload, True)
        return 0
    how = (f"shifted by {payload['t_offset_ms']:.4f} ms (f0 step {payload['f0_step']})"
           if payload.get("shift_finit") else
           f"plotted from t=0 (f_init extracted at {payload['t_offset_ms']:.4f} ms, f0 step {payload['f0_step']})")
    print(f"traces at psi_N={payload['psi_at']:.3f} (index {payload['psi_index']}); f_init runs {how}")
    print(f"{'run':<22} {'label':<26} {'|vort| peak':>11} {'|shear| pk':>10} {'|eflux| pk':>10} {'shear std end':>13} {'noise last':>10}")
    for row in payload["summary"]:
        def f(v, fmt="{:.3g}"):
            return "-" if v is None else fmt.format(v)
        print(f"{row['name']:<22} {row['label']:<26} {f(row['vort_flux_peak']):>11} "
              f"{f(row['shear_peak_khz']):>10} {f(row['eflux_peak_mw']):>10} "
              f"{f(row['shear_end_std_khz']):>13} {f(row['noise_rms_last']):>10}")
    for key, path in payload.get("local", {}).items():
        print(f"  {key}: {path}")
    return 0


def cmd_extend_maxwell(case: cfg.Case, args: argparse.Namespace) -> int:
    plan = proc.plan_extend_maxwell(case)
    if args.json and not args.confirm:
        _print(plan, True)
        return 0
    print(f"continue {plan['target_dir']} from restart step {plan['restart_step']} "
          f"(last 2d output {plan['last_2d_step']}); sml_mstep={plan['sml_mstep']} unchanged "
          f"-> expected end step {plan['expected_end_step']}")
    print(f"{plan['script_name']} changes:")
    for note in plan["script_notes"]:
        print(f"  {note}")
    _print_resources(plan["resources"])
    if not args.confirm:
        print("\nPlan only. Re-run with --confirm to write the script and submit (dryrun gate first).")
        return 0
    result = proc.extend_maxwell(case, plan)
    if args.json:
        _print(result, True)
        return 0
    if result["dryrun"]:
        print("dryrun: " + result["dryrun"]["summary"].splitlines()[0])
    print(f"submitted job {result['job_id']} for {result['dir']}")
    _print_resources(result["resources"])
    return 0


def cmd_distribution(case: cfg.Case, args: argparse.Namespace) -> int:
    payload = proc.distribution_figure(
        case, no_smooth=args.no_smooth, smooth=args.smooth, smooth_label=args.smooth_label,
        psi=args.psi, node=args.node, clip_percent=args.clip_percent, paper=args.paper,
        normalize=args.normalize,
    )
    if args.json:
        _print(payload, True)
        return 0
    w = payload["where"]
    print(f"node {w['node']} at R={w['R_m']:.3f} m, Z={w['Z_m']:.3f} m, psi_N={w['psi_n']:.3f}; "
          f"colour range +-{payload['clip_percent']:.1f} %")
    for p in payload["panels"]:
        print(f"  {p['label']:<28} rms {p['rms_percent']:.2f} %  max {p['max_percent']:.1f} %   {p['file'].rsplit('/', 1)[-1]}")
    for key, path in payload.get("local", {}).items():
        print(f"  {key}: {path}")
    return 0


def cmd_er_profile(case: cfg.Case, args: argparse.Namespace) -> int:
    payload = proc.er_profile(case, f0_step=args.f0_step, paper=args.paper, psi_max=args.psi_max)
    if args.json:
        _print(payload, True)
        return 0
    lo, hi = payload["er_range_kv_per_m"]
    print(f"<E_r> at steps {payload['steps']} (t = {payload['time_ms'][0]:.3f}, {payload['time_ms'][1]:.3f} ms); "
          f"snapshot range {lo:.2f} .. {hi:.2f} kV/m")
    for key, path in payload.get("local", {}).items():
        print(f"  {key}: {path}")
    return 0


def cmd_log(case: cfg.Case, args: argparse.Namespace) -> int:
    entries = cfg.read_log(case)
    if args.json:
        _print(entries, True)
        return 0
    if not entries:
        print(f"no log yet ({case.log_path})")
        return 0
    for e in entries:
        detail = {k: v for k, v in e.items() if k not in {"time", "stage", "metrics", "input_notes", "script_notes", "f_g_stats"}}
        print(f"{e['time']}  {e['stage']:<11} {json.dumps(detail, default=str)}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="xgc-finit", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("case", help="case name under f_init/cases/ or a YAML path")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("status", help="what exists in the case root")

    p = sub.add_parser("make-finit", help="f0 snapshot -> f_init files (login node)")
    p.add_argument("--sigma-theta", type=float, default=None, help="poloidal width [rad]")
    p.add_argument("--v-width", type=float, default=None, help="velocity-space width [cells]")
    p.add_argument("--step", type=int, default=None, help="f0 step; default last")
    p.add_argument("--out-dir", default=None, help="write elsewhere than the run dir")
    p.add_argument("--overwrite", action="store_true")

    p = sub.add_parser("stage-dt0", help="plan/write a dt=0 directory for one f_init file")
    p.add_argument("--finit", required=True, help="f_init file name in the stage A directory")
    p.add_argument("--dir", default=None, help="directory name; default dt0_<tag>")
    p.add_argument("--confirm", action="store_true")
    p.add_argument("--overwrite", action="store_true")

    p = sub.add_parser("stage-run", help="plan/write a production-run directory (finit_sm_<tag>) for one f_init file")
    p.add_argument("--finit", required=True, help="f_init file name in the stage A directory, usually an adjusted one")
    p.add_argument("--dir", default=None, help="directory name; default finit_sm_<sigma>_<width>")
    p.add_argument("--confirm", action="store_true")
    p.add_argument("--overwrite", action="store_true")

    p = sub.add_parser("dryrun", help="xgca --dryrun in a staged directory")
    p.add_argument("dir")

    p = sub.add_parser("submit", help="sbatch the directory's job script")
    p.add_argument("dir")
    p.add_argument("--confirm", action="store_true")
    p.add_argument("--skip-dryrun", action="store_true")

    sub.add_parser("jobs", help="state of jobs submitted for this case")

    p = sub.add_parser("adjust", help="compare densities and write an adjusted f_init")
    p.add_argument("dir", help="dt=0 directory to compare against stage A")
    p.add_argument("--factor", type=float, default=None)
    p.add_argument("--check-only", action="store_true")
    p.add_argument("--ref-step", type=int, default=None)
    p.add_argument("--cmp-step", type=int, default=None)
    p.add_argument("--overwrite", action="store_true")

    p = sub.add_parser("check", help="density mismatch metrics only (no file written)")
    p.add_argument("dir")
    p.add_argument("--ref-step", type=int, default=None)
    p.add_argument("--cmp-step", type=int, default=None)

    p = sub.add_parser("compare", help="initial-transient figures: Maxwellian vs f_init production runs (login node)")
    p.add_argument("dirs", nargs="*", help="production-run directories; default every finit_* directory")
    p.add_argument("--label", action="append", default=[], help="legend label per directory")
    p.add_argument("--psi", type=float, default=0.45, help="psi_N of the 1-D traces")
    p.add_argument("--f0-step", type=int, default=None, help="time offset step; default last f0 step")
    p.add_argument("--end-window", type=float, default=0.04, help="magnified shear window [ms]")
    p.add_argument("--shift-finit", action="store_true",
                   help="continue the Maxwellian curve: shift f_init runs by the f0 extraction time")
    p.add_argument("--paper", action="store_true",
                   help="publication styling (large fonts, 13x10 in, thick lines); files get a _paper suffix")
    p.add_argument("--twod-runs", nargs="*", default=None,
                   help="runs for the (psi_N, t) figure, e.g. maxwell finit_sm_0.5_0.5; default all")
    p.add_argument("--twod-scale", choices=["shared", "per-panel"], default="shared",
                   help="colour range of the (psi_N, t) panels; per-panel makes a quiet run readable")
    p.add_argument("--no-particles", action="store_true", help="skip the particle-noise scan")

    p = sub.add_parser("extend-maxwell", help="resubmit stage A so it continues from its last checkpoint")
    p.add_argument("--confirm", action="store_true")

    p = sub.add_parser("distribution", help="paper Fig. 1: (f-f_M)/f_M of the unsmoothed and smoothed f_init at one node")
    p.add_argument("--no-smooth", default=None, help="file in stage A dir; default xgc.f_init_no_smooth.bp")
    p.add_argument("--smooth", default=None, help="file in stage A dir; default from the case's smooth settings")
    p.add_argument("--smooth-label", default=None)
    p.add_argument("--psi", type=float, default=0.45, help="psi_N of the outboard-midplane node")
    p.add_argument("--node", type=int, default=None, help="mesh node instead of --psi")
    p.add_argument("--clip-percent", type=float, default=None, help="symmetric colour range [%%]")
    p.add_argument("--normalize", choices=["local", "peak"], default="local",
                   help="(f-f_M)/f_M per cell, or relative to the node's peak f_M")
    p.add_argument("--paper", action="store_true")

    p = sub.add_parser("er-profile", help="<E_r>(psi_N) of stage A at the f_init snapshot and at the end")
    p.add_argument("--f0-step", type=int, default=None)
    p.add_argument("--psi-max", type=float, default=0.9, help="cut the plot here (outer boundary layer)")
    p.add_argument("--paper", action="store_true")

    sub.add_parser("log", help="what this CLI has done to the case")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        case = cfg.load_case(args.case)
        handlers = {
            "status": cmd_status,
            "make-finit": cmd_make_finit,
            "stage-dt0": cmd_stage_dt0,
            "stage-run": lambda c, a: cmd_stage_dt0(c, a, run=True),
            "dryrun": cmd_dryrun,
            "submit": cmd_submit,
            "jobs": cmd_jobs,
            "adjust": cmd_adjust,
            "check": lambda c, a: cmd_adjust(c, a, check_only=True),
            "compare": cmd_compare,
            "extend-maxwell": cmd_extend_maxwell,
            "distribution": cmd_distribution,
            "er-profile": cmd_er_profile,
            "log": cmd_log,
        }
        return handlers[args.command](case, args)
    except (cfg.CaseError, proc.ProcedureError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
