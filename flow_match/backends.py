"""Where an iteration is recorded: the xgc-automation catalog, or nothing but the log.

Two backends implement the same three operations (reserve a run directory,
submit, poll):

``automation``
    The Mac + xgc-automation path. Every iteration reserves a global
    ``simulation_id`` and is submitted through ``hpc_runner.submit``, so
    provenance, input backup, the CSV notebook and ``ai_note`` follow
    (AGENTS.md rules 1, 4, 5).

``direct``
    A controller without the catalog (flux, or any host that only has this
    package, ``tools/ssh.py``, ``tools/scheduler.py`` and ``tools/machines.py``).
    The run directory is ``<run_root>/<case>_<label>``, the job script is
    rendered with the same template ``hpc_runner.prepare_submission`` uses,
    ``sbatch`` runs over SSH, and the only records are the case log and the
    campaign archive. No simulation number is minted; if the run is later
    wanted in the catalog it is adopted (``xgc-sim adopt-remote``), never
    numbered retroactively.

Which backend a case uses is ``backend:`` in the case yaml (default
``automation``) or ``--backend`` on the command line.
"""

from __future__ import annotations

from typing import Any, Protocol

from tools import dryrun
from tools import hpc_runner as hr
from tools import scheduler as sched
from tools import simulation_registry as sr
from tools import ssh

BACKENDS = ("automation", "direct")


class Backend(Protocol):
    name: str

    def reserve(self, case, plan: dict[str, Any], machine_cfg: dict[str, Any]) -> dict[str, Any]:
        """Return {'simulation_id', 'run_dir'} for a new iteration."""

    def submit(self, case, it: dict[str, Any], kwargs: dict[str, Any], *, confirm: bool, skip_dryrun: bool | None) -> dict[str, Any]:
        """Submit a staged iteration; a plan without ``confirm``."""

    def status(self, it: dict[str, Any]) -> dict[str, Any]:
        """Scheduler state of a submitted iteration: {'state': ..., ...}."""


class AutomationBackend:
    name = "automation"

    def reserve(self, case, plan, machine_cfg):
        name = plan["machine"]
        reserved = sr.reserve_simulation_id(
            name,
            plan["short_name"],
            tags=["flow-match", case.name, plan["label"]],
            expected_number=plan["id_plan"].get("simulation_number"),
            confirm=True,
            kind=plan["kind"],
        )
        sim_id = reserved.get("simulation_id") or reserved.get("record_id")
        directory = sr.simulation_directory(sim_id, name)
        return {"simulation_id": sim_id, "run_dir": directory["remote_directory"]}

    def submit(self, case, it, kwargs, *, confirm, skip_dryrun):
        return hr.submit(
            it["machine"],
            confirm=confirm,
            case_dir=it["run_dir"],
            simulation_id=it["simulation_id"],
            skip_dryrun=skip_dryrun,
            **kwargs,
        )

    def status(self, it):
        return hr.status(it["machine"], str(it["job_id"]))


class DirectBackend:
    name = "direct"

    def reserve(self, case, plan, machine_cfg):
        paths = machine_cfg.get("paths") or {}
        run_root = paths.get("run_root") or paths.get("scratch")
        label = f"{case.name}_{plan['label']}"
        return {"simulation_id": label, "run_dir": f"{run_root}/{label}"}

    def submit(self, case, it, kwargs, *, confirm, skip_dryrun):
        machine = it["machine"]
        job_name = kwargs.pop("job_name", None) or f"{it['simulation_id']}"
        plan = hr.prepare_submission(machine, job_name=job_name, run_dir=it["run_dir"], **kwargs)
        result: dict[str, Any] = {
            "submitted": False,
            "plan": plan.to_dict(),
            "backend": self.name,
        }
        if not confirm:
            result["reason"] = "confirmation required; review the plan and resubmit with confirm=True"
            return result
        machine_cfg = hr.m.get_machine(machine)
        target = ssh.host_for(machine_cfg)
        # The same login-node dryrun gate hpc_runner applies (docs/workflow.md):
        # a rejected input never reaches sbatch. Machines without a login-node
        # dryrun (Polaris, Aurora) skip it here; there is no parallel dryrun job.
        if not skip_dryrun and hr.dryrun_settings(machine_cfg).get("login_node", True):
            report = dryrun.run_dryrun(
                machine,
                it["run_dir"],
                n_ranks=plan.resources.get("total_ranks") or plan.resources.get("ranks"),
                xgc_exe=kwargs.get("xgc_exe"),
                env_script=kwargs.get("env_script"),
            )
            result["dryrun"] = report.to_dict()
            if report.blocking:
                result["reason"] = f"input validation failed: {report.summary()}"
                return result
        script_path = f"{it['run_dir']}/submit.sh"
        ssh.write_file(target, script_path, plan.script)
        adapter = sched.get_scheduler(machine_cfg)
        run = ssh.run(target, adapter.submit_argv(script_path), cwd=it["run_dir"], timeout=120.0, check=True)
        job_id = adapter.parse_job_id(run.stdout)
        result.update({"submitted": True, "job_id": job_id, "run_id": plan.run_id, "remote_run_dir": it["run_dir"], "script_path": script_path})
        return result

    def status(self, it):
        machine_cfg = hr.m.get_machine(it["machine"])
        target = ssh.host_for(machine_cfg)
        adapter = sched.get_scheduler(machine_cfg)
        job_id = str(it["job_id"])
        run = ssh.run(target, adapter.status_argv(job_id), timeout=60.0)
        state = adapter.parse_status(run.stdout, run.returncode)
        exit_code = None
        if state in (sched.UNKNOWN, None) or state in sched.TERMINAL_STATES:
            acct = ssh.run(target, adapter.accounting_argv(job_id), timeout=60.0)
            if acct.ok and acct.stdout.strip():
                acct_state, exit_code = adapter.parse_accounting(acct.stdout)
                if acct_state and acct_state != sched.UNKNOWN:
                    state = acct_state
        return {"state": state, "exit_code": exit_code, "terminal": state in sched.TERMINAL_STATES, "backend": self.name}


def get_backend(name: str | None) -> Backend:
    name = (name or "automation").lower()
    if name == "automation":
        return AutomationBackend()
    if name == "direct":
        return DirectBackend()
    raise ValueError(f"unknown backend {name!r}; choose one of {BACKENDS}")
