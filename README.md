# xgc-automation

Automation workflow for XGC (gyrokinetic code, xgc.pppl.gov) simulations and
analysis.
**Local brain + remote hands**: the LLM (ohmic.pppl.gov) runs locally
(macOS/Linux), while jobs are submitted, monitored, and analyzed on
Perlmutter/Polaris over SSH.

> See `AGENTS.md` for the detailed design and rules. opencode loads that file
> automatically at startup.

## Directory layout

| Path | Purpose |
|---|---|
| `tools/` | MCP tool layer (`preflight`, `hpc_runner`, ...) |
| `machines.yaml` | HPC machine definitions (single source of truth) |
| `templates/` | Jinja2 templates for job scripts and XGC input |
| `docs/` | Setup and per-machine build notes |
| `kb/` | Knowledge base for physics_advisor |
| `runs/` | Run records and pulled summary products |

## Docs

- `docs/ssh-setup.md` — ssh multiplexing setup for MFA-protected machines
- `docs/build-stellar.md` — building XGC on stellar (note: `-O2` crashes the compiler)
