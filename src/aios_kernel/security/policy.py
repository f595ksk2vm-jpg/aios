"""What an agent may do, and when a human must sign off.

Two independent checks gate every tool call:

1. *Permission* — does the agent hold a permission matching the tool's? Agent
   permissions are data owned by humans (and attenuated copies made by the
   factory); nothing an agent can call ever widens them.
2. *Autonomy policy* — given the agent's autonomy level and the action's
   category, run it, or pause the run and ask a human.
"""
from __future__ import annotations

from fnmatch import fnmatchcase

AUTONOMY_LEVELS = ["assisted", "supervised", "autonomous", "strategic"]

# Action categories, from harmless to consequential.
CATEGORIES = ["read", "write_internal", "code_exec", "agent_mgmt", "external_comm", "initiative", "destructive",
              "financial", "legal", "sensitive_data"]

# Regardless of autonomy level these always need a human decision.
ALWAYS_APPROVE = {"financial", "legal", "sensitive_data"}

_AUTO_ALLOWED = {
    "assisted": {"read"},  # proposes everything else
    "supervised": {"read", "write_internal", "code_exec"},
    "autonomous": {"read", "write_internal", "code_exec", "agent_mgmt", "external_comm"},
    # Strategic agents may also launch new initiatives on their own, inside their budget.
    "strategic": {"read", "write_internal", "code_exec", "agent_mgmt", "external_comm", "initiative"},
}


def autonomy_rank(level: str) -> int:
    return AUTONOMY_LEVELS.index(level) if level in AUTONOMY_LEVELS else 0


def decide(autonomy: str, category: str) -> str:
    """Return ``"allow"`` or ``"approve"`` (human approval required)."""
    if category in ALWAYS_APPROVE:
        return "approve"
    allowed = _AUTO_ALLOWED.get(autonomy, _AUTO_ALLOWED["assisted"])
    return "allow" if category in allowed else "approve"


def has_permission(held: list[str], required: str) -> bool:
    """Patterns are fnmatch-style: ``fs.*`` covers ``fs.write``; ``*`` covers everything."""
    return any(fnmatchcase(required, pat) for pat in held)


def covered_by(candidate: str, held: list[str]) -> bool:
    """Is the permission *pattern* ``candidate`` no broader than what ``held`` already grants?

    Used for attenuation when one agent creates another: a child pattern is
    accepted only if a parent pattern equals it or subsumes it as a prefix
    wildcard. Conservative by construction — unknown shapes are rejected.
    """
    for pat in held:
        if pat == "*" or pat == candidate:
            return True
        if pat.endswith("*") and "*" not in pat[:-1] and candidate.startswith(pat[:-1]):
            return True
    return False
