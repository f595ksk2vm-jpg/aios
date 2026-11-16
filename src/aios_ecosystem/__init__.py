"""Layer 3 — Business & Project Ecosystem.

Businesses, projects, workspaces, deliverables, customers, finances, KPIs and
workflows: everything the workforce creates and runs. Each business and each
project is a kernel namespace, which is what keeps them isolated.
"""
from .plugin import Ecosystem, install

__all__ = ["Ecosystem", "install"]
