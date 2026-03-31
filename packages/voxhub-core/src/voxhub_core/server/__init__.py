"""Server subpackage: SSH-invoked CLI, provenance, locking, logging.

Hard wall: local modules (``staging``, ``integrate``, etc.) must never
import from this subpackage.  Server wrappers call local functions then
add provenance on top.
"""
