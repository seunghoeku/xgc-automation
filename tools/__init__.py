"""MCP tool layer for XGC automation.

opencode launches this package's MCP server via `uv run python -m tools.server`.
The tools from the AGENTS.md roadmap (preflight / physics_advisor /
input_builder / validator / hpc_runner / data_analyzer / provenance) are
implemented here incrementally.
"""

__all__ = ["__version__"]
__version__ = "0.1.0"
