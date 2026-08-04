"""NumPy spellings that changed across major versions, resolved in one place."""

from __future__ import annotations

# ``np.trapz`` was renamed ``np.trapezoid`` in NumPy 2.0 and removed in 2.4, so
# neither spelling is safe to write at a call site.
try:
    from numpy import trapezoid
except ImportError:  # numpy < 2.0
    from numpy import trapz as trapezoid

__all__ = ["trapezoid"]
