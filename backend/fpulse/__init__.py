"""F-Pulse: AI-native, human-governed data pipeline builder."""

import os as _os

# Cap OpenBLAS/OMP threads before NumPy is imported anywhere in this process.
# NumPy's bundled OpenBLAS allocates a per-thread work buffer sized to the
# machine's core count on first import; on many-core or memory-constrained
# Windows hosts that can fail with "OpenBLAS error: Memory allocation still
# failed after 10 retries, giving up." F-Pulse's workload is ETL (pandas
# group/merge/IO), not dense linear algebra, so one BLAS thread costs nothing.
# Runs before any fpulse submodule (hence pandas/numpy) is imported, so it is
# effective however the backend is launched (uvicorn, CLI, service, tests).
# setdefault keeps an operator's own override intact.
_os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
_os.environ.setdefault("OMP_NUM_THREADS", "1")

__version__ = "1.0.0"
