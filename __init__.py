"""High-accuracy reference energy calculations for DFThub datasets.

The public API is intentionally small.  Import :func:`compute_reference` to
run a complete archive calculation, or use :class:`ReferenceCalculator` when
the caller needs progress and cancellation hooks.
"""

__version__ = "0.2.3"

from .core import (
    BASIS_ALIASES,
    METHODS,
    CalculationCancelled,
    CalculationSettings,
    CalculationReport,
    ReferenceCalculator,
    compute_reference,
    calculate_reference,
    calculate_archive,
)
from .job_manager import EnergyJobManager


def create_app(*args, **kwargs):
    """Create the optional Flask API lazily.

    Flask is only needed for the HTTP service; importing the numerical library
    must remain possible in the PySCF environment without the web extra.
    """

    from .api import create_app as _create_app

    return _create_app(*args, **kwargs)

__all__ = [
    "__version__",
    "BASIS_ALIASES",
    "METHODS",
    "CalculationCancelled",
    "CalculationSettings",
    "CalculationReport",
    "ReferenceCalculator",
    "compute_reference",
    "calculate_reference",
    "calculate_archive",
    "EnergyJobManager",
    "create_app",
]
