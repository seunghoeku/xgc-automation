"""Evidence-based physics advice for selecting an XGC baseline.

The advisor is intentionally conservative.  It does not ask an LLM to invent
XGC values.  Instead it:

1. matches a natural-language request against capabilities recorded in
   ``kb/cases/*.yaml``;
2. rejects known mismatches recorded by the case;
3. carries through only explicitly supplied values covered by deterministic
   rules; and
4. returns the evidence, limitations, and next input-builder action.

"Unsupported" is a useful answer: it prevents an apparently precise but
unvalidated input proposal.  The knowledge base grows through the case
promotion pipeline (tools/case_promotion.py), which drafts new baselines from
archived inputs plus the researcher's recorded outcomes.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from tools import input_builder


class PhysicsAdvisorError(RuntimeError):
    """The physics request or knowledge-base entry is malformed."""


@dataclass
class CaseMatch:
    """Deterministic match result for one validated case."""

    name: str
    case: dict[str, Any]
    matched: list[str] = field(default_factory=list)
    excluded: list[str] = field(default_factory=list)

    @property
    def score(self) -> int:
        return len(self.matched)


def _contains(text: str, patterns: list[str]) -> bool:
    return any(re.search(pattern, text, flags=re.IGNORECASE) for pattern in patterns)


def _case_match(description: str, case: dict[str, Any]) -> CaseMatch:
    advisor = case.get("advisor") or {}
    match = CaseMatch(name=case.get("name", ""), case=case)

    for signal in advisor.get("match_signals", []):
        patterns = signal.get("patterns") or []
        if patterns and _contains(description, patterns):
            match.matched.append(signal.get("label") or patterns[0])

    for exclusion in advisor.get("exclusions", []):
        patterns = exclusion.get("patterns") or []
        if patterns and _contains(description, patterns):
            reason = exclusion.get("reason") or exclusion.get("label") or patterns[0]
            match.excluded.append(reason)

    return match


def _explicit_overrides(description: str) -> tuple[dict[str, Any], list[str]]:
    """Extract only values explicitly stated by the user and covered by rules."""
    overrides: dict[str, Any] = {}
    evidence: list[str] = []

    step_patterns = [
        r"\b(?:run\s+for\s+)?(\d[\d_,]*)\s*(?:time\s*)?steps?\b",
    ]
    for pattern in step_patterns:
        found = re.search(pattern, description, flags=re.IGNORECASE)
        if found:
            steps = int(found.group(1).replace(",", "").replace("_", ""))
            if steps <= 0:
                raise PhysicsAdvisorError("step count must be positive")
            overrides["sml_mstep"] = steps
            evidence.append(f"user explicitly requested {steps} steps")
            break

    fresh_patterns = [
        r"\b(?:fresh|new)\s+(?:run|simulation)\b",
        r"\bwithout\s+(?:a\s+)?restart\b",
    ]
    if _contains(description, fresh_patterns):
        overrides["sml_restart"] = False
        evidence.append("user explicitly requested a fresh, non-restart run")

    return overrides, evidence


def advise(description: str, *, machine: str | None = None) -> dict[str, Any]:
    """Match a natural-language problem to a validated case and safe overrides.

    No files are written and no remote commands are run.  Returned overrides
    are candidates for ``plan_input``, which performs authoritative validation
    against the XGC source metadata before presenting a diff.
    """
    description = description.strip()
    if not description:
        raise PhysicsAdvisorError("problem description must not be empty")

    cases = [input_builder.load_case(item["name"]) for item in input_builder.list_cases()]
    if machine:
        cases = [case for case in cases if case.get("machine") == machine]
    if not cases:
        scope = f" on {machine!r}" if machine else ""
        return {
            "status": "unsupported",
            "summary": f"no validated baseline cases are available{scope}",
            "request": description,
            "candidates": [],
            "questions": ["Add and validate a representative case in kb/cases/."],
        }

    matches = [_case_match(description, case) for case in cases]
    matches.sort(key=lambda item: (bool(item.excluded), -item.score, item.name))
    viable = [item for item in matches if not item.excluded]

    candidate_summary = [
        {
            "case": item.name,
            "machine": item.case.get("machine"),
            "score": item.score,
            "matched": item.matched,
            "excluded": item.excluded,
        }
        for item in matches
    ]

    if not viable or viable[0].score == 0:
        exclusions = sorted({reason for item in matches for reason in item.excluded})
        if exclusions:
            summary = "no validated baseline supports the requested physics"
            questions = [
                "Provide a validated baseline case covering: " + "; ".join(exclusions)
            ]
        else:
            summary = "the request does not contain enough physics detail to select a baseline"
            questions = [
                "Specify the field model (electrostatic/electromagnetic), domain "
                "(core/edge-SOL), marker model, and collision model."
            ]
        return {
            "status": "unsupported" if exclusions else "needs_clarification",
            "summary": summary,
            "request": description,
            "candidates": candidate_summary,
            "questions": questions,
        }

    selected = viable[0]
    advisor = selected.case.get("advisor") or {}
    overrides, override_evidence = _explicit_overrides(description)
    minimum_score = int(advisor.get("minimum_match_signals", 2))
    status = "recommended" if selected.score >= minimum_score else "needs_clarification"

    warnings = list(advisor.get("warnings") or [])
    if status == "needs_clarification":
        warnings.insert(
            0,
            f"only {selected.score} matching signal(s) were found; "
            f"{minimum_score} are required for a recommendation",
        )

    return {
        "status": status,
        "summary": (
            f"use validated baseline {selected.name!r}"
            if status == "recommended"
            else f"{selected.name!r} is the closest baseline, but more detail is required"
        ),
        "request": description,
        "selected_case": {
            "name": selected.name,
            "description": (selected.case.get("description") or "").strip(),
            "machine": selected.case.get("machine"),
            "validated": selected.case.get("validated") or {},
        },
        "evidence": {
            "matched": selected.matched,
            "explicit_values": override_evidence,
            "source": f"kb/cases/{selected.name}.yaml",
        },
        "proposal": {
            "case": selected.name,
            "baseline_parameters": advisor.get("baseline_parameters") or {},
            "overrides": overrides,
            "invented_values": [],
            "next_action": (
                "Call plan_input with these overrides to obtain a metadata-validated "
                "diff; do not write until the human approves it."
            ),
        },
        "warnings": warnings,
        "candidates": candidate_summary,
        "questions": (
            []
            if status == "recommended"
            else [
                "Confirm the field model, domain, marker model, and collision model "
                "before planning an input diff."
            ]
        ),
    }
