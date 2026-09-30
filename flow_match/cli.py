"""``xgc-flowmatch``: drive the rotation-profile matching loop from the shell.

    xgc-flowmatch <case> analyze  <label> --run-dir <remote dir> [--machine M] [--last N]
    xgc-flowmatch <case> adjust   <label> [--current-prf F] [--relax R]
    xgc-flowmatch <case> plan     --prf F [--label L] [--machine M] [--kind dev] [--mstep N]
    xgc-flowmatch <case> stage    --prf F [...same as plan...] --confirm
    xgc-flowmatch <case> submit   <label> [--skip-dryrun] [--nodes N --walltime T ...] --confirm
    xgc-flowmatch <case> status
    xgc-flowmatch <case> campaign [--update L] [--list] [--verify L]
    xgc-flowmatch <case> history
    xgc-flowmatch <case> next     <label> [--machine M] [--kind K] [--mstep N]   # analyze + adjust + plan

Write or submit commands print a plan and stop unless ``--confirm`` is given
(AGENTS.md rule 1). ``submit`` always shows node count and wall time first.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import campaign as camp
from . import config as cfg
from . import procedure as proc


def _print(obj, as_json: bool) -> None:
    if as_json:
        print(json.dumps(obj, indent=1, default=str))
    else:
        print(obj)


def cmd_analyze(case: cfg.Case, args) -> int:
    run_dir = args.run_dir
    if not run_dir:
        it = proc._iteration(case, args.label)
        run_dir = it["run_dir"]
        args.machine = args.machine or it.get("machine")
    result = proc.analyze(case, run_dir, label=args.label, machine=args.machine, last=args.last, step_min=args.step_min, step_max=args.step_max)
    if args.json:
        _print({k: v for k, v in result.items() if k != "payload"}, True)
        return 0
    payload = result["payload"]
    w = payload["window"]
    print(f"{args.label}: {payload['var']} averaged over steps {w['step_min']}-{w['step_max']} ({w['count']} samples)")
    for s in payload.get("saturation", []):
        rc = s.get("relative_change")
        print(f"  psi_n={s['psi_n']:.2f}  omega {s['omega_second_half']/1e3:8.2f} krad/s  change {rc*100 if rc is not None else float('nan'):6.1f}%")
    print(f"  saturated: {result['saturated']} (tolerance {case.analysis.get('saturation_tol')})")
    for k, v in result["local"].items():
        print(f"  {k}: {v}")
    if result.get("archive"):
        a = result["archive"]
        print(f"  archive: {a['remote']} (+{len(a['added']['text'])} text, +{len(a['added']['data'])} data) -> {a['local']}")
    return 0


def cmd_adjust(case: cfg.Case, args) -> int:
    result = proc.adjust(case, args.label, current_prf=Path(args.current_prf) if args.current_prf else None, relax=args.relax, out_name=args.out_name)
    if args.json:
        _print({k: v for k, v in result.items() if k != "log"}, True)
        return 0
    m = result["metrics"]
    print(f"{args.label}: next profile {result['next_prf']}")
    print(f"  simulation read from {result['sim_source']}")
    print(f"  rms(v_tor target - sim) = {m['rms_delta_vtor']/1e3:.2f} km/s ({(m['rms_delta_relative'] or 0)*100:.1f}% of target rms), max {m['max_abs_delta_vtor']/1e3:.2f} km/s, psi {m['psi_range'][0]:.2f}-{m['psi_range'][1]:.2f}; omega correction rms {m['rms_delta_omega']/1e3:.2f} krad/s")
    print(f"  relax {m['relax']}, SOL mode {m['sol_mode']} from psi_n {m['sol_psi_start']:.2f}")
    if result["png"]:
        print(f"  plot: {result['png']}")
    return 0


def _plan(case: cfg.Case, args):
    prf = Path(args.prf) if args.prf else None
    if prf is None:
        raise proc.ProcedureError("--prf is required (the rotation profile for this iteration)")
    return proc.plan_iteration(case, prf, label=args.label, machine=args.machine, kind=args.kind, mstep=args.mstep, backend_name=getattr(args, "backend", None))


def _show_plan(plan, as_json: bool) -> None:
    if as_json:
        _print({k: v for k, v in plan.items() if k != "input_text"}, True)
        return
    idp = plan["id_plan"]
    print(f"iteration {plan['label']} on {plan['machine']} ({plan['kind']}, backend {plan['backend']}): simulation id {idp.get('simulation_id') or idp.get('simulation_number')} -> {idp.get('directory_name')}")
    print(f"  rotation profile: {plan['prf_local']} -> {plan['prf_remote']}")
    print(f"  copy: {', '.join(plan['copy_files'])}; mstep {plan['mstep']}")
    print(f"  job: {proc.describe_job(plan)}")
    print("  input diff:")
    print(plan["input_diff"] or "  (no change)")
    for note in plan["input_notes"]:
        print(f"  note: {note}")


def cmd_plan(case: cfg.Case, args) -> int:
    _show_plan(_plan(case, args), args.json)
    return 0


def cmd_stage(case: cfg.Case, args) -> int:
    plan = _plan(case, args)
    _show_plan(plan, args.json)
    if not args.confirm:
        print("\nnothing was staged; rerun with --confirm")
        return 0
    result = proc.stage_iteration(case, plan, confirm=True, overwrite_prf=args.overwrite_prf)
    print(f"staged {result['label']} as {result['simulation_id']} in {result['run_dir']}")
    return 0


def cmd_submit(case: cfg.Case, args) -> int:
    overrides = {k: getattr(args, k) for k in ("nodes", "walltime", "qos", "partition", "constraint", "account", "exclude_nodes") if getattr(args, k) is not None}
    result = proc.submit_iteration(case, args.label, confirm=args.confirm, skip_dryrun=args.skip_dryrun, overrides=overrides)
    if args.json:
        _print(result, True)
        return 0
    if not args.confirm:
        plan = result.get("plan") or {}
        res = plan.get("resources") or {}
        print(f"{args.label}: submission plan (not submitted)")
        for key in ("job_name", "remote_run_dir", "provenance_class"):
            if plan.get(key) is not None:
                print(f"  {key}: {plan[key]}")
        for key, value in res.items():
            print(f"  {key}: {value}")
        for warning in plan.get("warnings") or []:
            print(f"  warning: {warning}")
        dry = result.get("dryrun")
        if dry:
            print(f"  dryrun: {dry.get('summary') if isinstance(dry, dict) else dry}")
        if result.get("reason") and "confirmation" not in str(result["reason"]):
            print(f"  reason: {result['reason']}")
        if args.show_script and plan.get("script"):
            print("---- submit.sh ----")
            print(plan["script"])
        print("rerun with --confirm to submit")
        return 0
    if result.get("submitted"):
        print(f"submitted {args.label}: job {result.get('job_id')} run {result.get('run_id')}")
        return 0
    print(f"not submitted: {result.get('reason') or result}")
    return 1


def cmd_campaign(case: cfg.Case, args) -> int:
    """The study archive: update from an iteration, list, or verify a remote read."""
    if not camp.enabled(case):
        print("campaign is not enabled for this case (campaign.enabled)")
        return 1
    if args.update:
        it = proc._iteration(case, args.update)
        name, machine_cfg, target = proc._machine(case, it["machine"])
        stem = f"{it['run_dir'].rstrip('/')}/flow_match/midplane_{args.update}"
        window = None
        for e in reversed(cfg.read_log(case)):
            if e.get("stage") == "analyze" and e.get("label") == args.update:
                window = e.get("window")
                break
        result = camp.update_archive(case, args.update, machine=name, machine_cfg=machine_cfg, target=target, run_dir=it["run_dir"], analysis_stem=stem, window=window, input_prf_remote=it.get("prf_remote"))
        print(f"updated {result['remote']} with {args.update}: text {result['added']['text']}, data {result['added']['data']}")
        print(f"pulled to {result['local']}")
    if args.verify:
        r = camp.verify_remote_read(case, args.verify)
        print(f"remote read ok: {r['variable']} shape {r['shape']} first {r['first']}")
    if args.list or not (args.update or args.verify):
        path = camp.local_archive(case)
        print(f"archive: {path} ({'present' if path.exists() else 'missing'})")
        if path.exists():
            for label in camp.iterations(case):
                names = [n for n in camp.text_names(case) if n.startswith(f"{label}/")]
                print(f"  {label}: {', '.join(sorted(names))}")
    return 0


def cmd_status(case: cfg.Case, args) -> int:
    rows = proc.refresh_status(case)
    if args.json:
        _print(rows, True)
        return 0
    for r in rows:
        print(f"{r['label']:6} {r['simulation_id']:24} {r['machine']:11} job {r['job_id']!s:10} {r['state']}")
    if not rows:
        print("no submitted iterations")
    return 0


def cmd_history(case: cfg.Case, args) -> int:
    rows = proc.history(case)
    if args.json:
        _print(rows, True)
        return 0
    for r in rows:
        m = r.get("metrics") or {}
        rms = f"{m['rms_delta_vtor']/1e3:.2f} km/s" if m.get("rms_delta_vtor") is not None else "-"
        print(f"{r['label']:6} {r.get('simulation_id') or '-':10} {r.get('machine') or '-':11} {r.get('state') or '-':10} prf {Path(r.get('prf_local') or '-').name:40} saturated {r.get('saturated')!s:5} rms {rms}")
    if not rows:
        print("no iterations yet")
    return 0


def cmd_next(case: cfg.Case, args) -> int:
    """analyze + adjust the finished iteration, then plan the following one."""
    it = proc._iteration(case, args.label)
    args.run_dir = it["run_dir"]
    args.machine = args.machine or it.get("machine")
    args.last, args.step_min, args.step_max = args.last, None, None
    rc = cmd_analyze(case, args)
    if rc:
        return rc
    args.current_prf, args.out_name = None, None
    rc = cmd_adjust(case, args)
    if rc:
        return rc
    adj = [e for e in cfg.read_log(case) if e.get("stage") == "adjust" and e.get("label") == args.label][-1]
    args.prf = adj["next_prf"]
    args.label = None
    print()
    return cmd_plan(case, args)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="xgc-flowmatch", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("case")
    p.add_argument("--json", action="store_true")
    sub = p.add_subparsers(dest="command", required=True)

    a = sub.add_parser("analyze", help="midplane carbon flow of a finished run")
    a.add_argument("label")
    a.add_argument("--run-dir", default=None, help="remote run directory (default: the staged iteration's)")
    a.add_argument("--machine", default=None)
    a.add_argument("--last", type=int, default=None)
    a.add_argument("--step-min", type=int, default=None)
    a.add_argument("--step-max", type=int, default=None)

    b = sub.add_parser("adjust", help="next rotation profile from an analysis")
    b.add_argument("label")
    b.add_argument("--current-prf", default=None)
    b.add_argument("--relax", type=float, default=None)
    b.add_argument("--out-name", default=None)

    for name in ("plan", "stage"):
        c = sub.add_parser(name, help="plan (or stage) the next iteration")
        c.add_argument("--prf", required=True)
        c.add_argument("--label", default=None)
        c.add_argument("--machine", default=None)
        c.add_argument("--kind", default=None, choices=["production", "dev"])
        c.add_argument("--mstep", type=int, default=None)
        c.add_argument("--backend", default=None, choices=["automation", "direct"])
        if name == "stage":
            c.add_argument("--confirm", action="store_true")
            c.add_argument("--overwrite-prf", action="store_true")

    d = sub.add_parser("submit", help="submit a staged iteration")
    d.add_argument("label")
    d.add_argument("--confirm", action="store_true")
    d.add_argument("--skip-dryrun", action="store_true", default=None)
    d.add_argument("--show-script", action="store_true")
    for opt in ("nodes", "walltime", "qos", "partition", "constraint", "account"):
        d.add_argument(f"--{opt}", default=None)
    d.add_argument("--exclude-nodes", dest="exclude_nodes", default=None, help="Slurm hostlist to avoid, e.g. sai-n07n[1-12]")

    g = sub.add_parser("campaign", help="study archive (.aca): --update <label>, --list, --verify <label>")
    g.add_argument("--update", default=None, metavar="LABEL")
    g.add_argument("--list", action="store_true")
    g.add_argument("--verify", default=None, metavar="LABEL")

    sub.add_parser("status", help="scheduler state of every submitted iteration")
    sub.add_parser("history", help="one line per iteration")

    e = sub.add_parser("next", help="analyze + adjust a finished iteration and plan the next")
    e.add_argument("label")
    e.add_argument("--machine", default=None)
    e.add_argument("--kind", default=None, choices=["production", "dev"])
    e.add_argument("--mstep", type=int, default=None)
    e.add_argument("--backend", default=None, choices=["automation", "direct"])
    e.add_argument("--last", type=int, default=None)
    e.add_argument("--relax", type=float, default=None)
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if getattr(args, "nodes", None) is not None:
        args.nodes = int(args.nodes)
    handlers = {
        "analyze": cmd_analyze,
        "adjust": cmd_adjust,
        "plan": cmd_plan,
        "stage": cmd_stage,
        "submit": cmd_submit,
        "status": cmd_status,
        "history": cmd_history,
        "campaign": cmd_campaign,
        "next": cmd_next,
    }
    try:
        case = cfg.load_case(args.case)
        return handlers[args.command](case, args)
    except (cfg.CaseError, proc.ProcedureError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
