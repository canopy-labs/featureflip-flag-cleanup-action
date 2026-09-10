"""Featureflip Flag Cleanup Action — deterministic dead/stale flag removal.

Self-contained Python tool: NOT in the npm workspace, NOT an SDK. Runs inside the CUSTOMER's CI, reading their
``FEATUREFLIP_API_TOKEN`` + ``GITHUB_TOKEN`` — never any Featureflip-internal
secret. Uses ``polyglot-piranha`` to remove stale/dead flags from JS/TS code.
"""

__all__ = ["run_piranha"]

from flag_cleanup.piranha_runner import run_piranha
