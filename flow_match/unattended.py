"""Unattended driver: keep one flow_match loop moving until a deadline.

    python -m flow_match.unattended <case> --until "2026-09-30 10:00" [--poll 600] [--max-submits 4]

Every ``--poll`` seconds the newest iteration of ``case`` is polled. When it
has completed, the same steps a human runs by hand follow, in order:
``analyze`` -> ``adjust`` -> ``plan`` -> ``stage`` -> ``submit`` of the next
iteration, with the case's own job settings (nodes, wall time) and no
overrides. Nothing is submitted after the deadline and nothing after
``--max-submits`` submissions; a failed, cancelled or timed-out run stops the
driver, which never resubmits or changes settings on its own. Everything is
appended to ``<case>.unattended.log`` beside the case yaml and to the case
log through the normal procedure calls.

This is the standing authorization the user gives for a night: the
per-submission approval of AGENTS.md rule 1 is granted in advance for the
iterations of this one case within the window, and the driver prints node
count and wall time before each submission for the record.
"""

from __future__ import annotations

import argparse
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path

from . import config as cfg
from . import procedure as proc

TERMINAL_BAD = {"failed", "cancelled", "timeout", "node_fail", "out_of_memory"}
IN_FLIGHT = {"running", "pending", "submitted", "queued"}


def _log(path: Path, message: str) -> None:
    line = f"{datetime.now().replace(microsecond=0).isoformat()} {message}"
    print(line, flush=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def _latest_label(case: cfg.Case) -> str | None:
    labels = sorted({e["label"] for e in cfg.read_log(case) if e.get("stage") == "stage" and e.get("label")})
    return labels[-1] if labels else None


def _has(case: cfg.Case, stage: str, label: str) -> bool:
    return any(e.get("stage") == stage and e.get("label") == label for e in cfg.read_log(case))


def advance(case: cfg.Case, label: str, log: Path, *, allow_submit: bool) -> str | None:
    """analyze + adjust ``label`` (once each), then stage and submit the next iteration.

    Returns the new label, or None when nothing was submitted.
    """
    it = proc._iteration(case, label)
    if not _has(case, "analyze", label):
        result = proc.analyze(case, it["run_dir"], label=label, machine=it.get("machine"))
        w = result["payload"]["window"]
        _log(log, f"{label}: analyzed steps {w['step_min']}-{w['step_max']} ({w['count']} samples), saturated={result['saturated']}, archive updated={bool(result.get('archive'))}")
    if not _has(case, "adjust", label):
        result = proc.adjust(case, label)
        m = result["metrics"]
        _log(log, f"{label}: adjusted; rms delta v_tor {m['rms_delta_vtor'] / 1e3:.2f} km/s ({(m['rms_delta_relative'] or 0) * 100:.0f}% of target), omega correction rms {m['rms_delta_omega'] / 1e3:.2f} krad/s -> {result['next_prf']}")
    if not allow_submit:
        _log(log, f"{label}: submission window closed; next iteration not staged")
        return None
    adj = [e for e in cfg.read_log(case) if e.get("stage") == "adjust" and e.get("label") == label][-1]
    plan = proc.plan_iteration(case, Path(adj["next_prf"]), machine=it.get("machine"))
    idp = plan["id_plan"]
    _log(log, f"plan {plan['label']}: {idp.get('simulation_id') or idp.get('simulation_number')} on {plan['machine']}; job {proc.describe_job(plan)}")
    staged = proc.stage_iteration(case, plan, confirm=True)
    _log(log, f"staged {staged['label']} as {staged['simulation_id']} in {staged['run_dir']}")
    sub = proc.submit_iteration(case, staged["label"], confirm=True)
    if not sub.get("submitted"):
        _log(log, f"{staged['label']}: submission refused: {sub.get('reason')}")
        return None
    _log(log, f"submitted {staged['label']} ({staged['simulation_id']}): job {sub.get('job_id')}")
    return staged["label"]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("case")
    ap.add_argument("--until", required=True, help="local time after which nothing is submitted, e.g. '2026-09-30 10:00'")
    ap.add_argument("--poll", type=int, default=600, help="seconds between scheduler polls")
    ap.add_argument("--max-submits", type=int, default=6, help="upper bound on submissions by this driver")
    args = ap.parse_args(argv)

    case = cfg.load_case(args.case)
    deadline = datetime.fromisoformat(args.until)
    log = case.log_path.with_name(f"{case.name}.unattended.log")
    _log(log, f"start: case {case.name}, deadline {deadline}, poll {args.poll}s, max submits {args.max_submits}")
    submits = 0
    unknown_polls = 0
    while True:
        try:
            label = _latest_label(case)
            if not label:
                _log(log, "no staged iteration; nothing to drive")
                return 1
            states = {s["label"]: s for s in proc.refresh_status(case)}
            state = (states.get(label) or {}).get("state")
            now = datetime.now()
            if state == "completed":
                unknown_polls = 0
                new = advance(case, label, log, allow_submit=(now < deadline and submits < args.max_submits))
                if new is None:
                    _log(log, "stop: nothing more to submit")
                    return 0
                submits += 1
                continue
            if state in TERMINAL_BAD:
                _log(log, f"stop: {label} ended with state {state}; human decision needed")
                return 2
            if state in (None, "unknown"):
                unknown_polls += 1
                _log(log, f"{label}: state unknown ({unknown_polls}); {(states.get(label) or {}).get('status')}")
                if unknown_polls >= 6:
                    _log(log, "stop: scheduler state unknown for an hour")
                    return 3
            else:
                unknown_polls = 0
                _log(log, f"{label}: {state}")
            if now >= deadline and state not in IN_FLIGHT:
                _log(log, "stop: deadline passed")
                return 0
        except Exception:
            _log(log, "error:\n" + traceback.format_exc())
            return 4
        time.sleep(args.poll)


if __name__ == "__main__":
    sys.exit(main())
