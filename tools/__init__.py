"""MCP tool layer for XGC automation.

opencode launches this package's MCP server via `uv run python -m tools.server`.
The tools (preflight / physics_advisor / input_builder / validator /
hpc_runner / data_analyzer / provenance / remote_gui /
simulation_catalog) are implemented here incrementally; see
`docs/tool-layers.md` for the map and `docs/roadmap.md` for status.
"""

__all__ = ["__version__"]
__version__ = "0.1.0"
