"""Layer 2 — AI Workforce.

Roles, departments, hierarchy, the agent factory, collaboration protocols and
performance tracking. Built entirely on the kernel's public surface; knows
nothing about businesses or projects (Layer 3).
"""
from .plugin import Workforce, install

__all__ = ["Workforce", "install"]
