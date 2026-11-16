"""Layer 1 — AI-OS kernel.

Business-agnostic infrastructure: identity and access, namespaces (isolation),
model routing, memory, tools, sandboxed execution, the job queue, the agent
runtime and orchestration. This package must never import ``aios_workforce``
or ``aios_ecosystem``; upper layers plug in through the registries and hooks
exposed on :class:`aios_kernel.kernel.Kernel`.
"""

__version__ = "0.1.0"
