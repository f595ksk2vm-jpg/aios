"""Composition root: the only place that knows about all three layers."""
from __future__ import annotations

import aios_ecosystem
import aios_workforce
from aios_kernel.config import Settings
from aios_kernel.kernel import Kernel


def build(settings: Settings | None = None) -> Kernel:
    kernel = Kernel(settings)
    aios_workforce.install(kernel)   # Layer 2 on Layer 1
    aios_ecosystem.install(kernel)   # Layer 3 on Layers 1 + 2
    return kernel
