"""XGC 자동화 MCP 툴 계층.

opencode가 `uv run python -m tools.server` 로 이 패키지의 MCP 서버를 띄운다.
AGENTS.md 로드맵의 툴들(preflight/physics_advisor/input_builder/validator/
hpc_runner/data_analyzer/provenance)을 점진적으로 여기에 구현한다.
"""

__all__ = ["__version__"]
__version__ = "0.1.0"
