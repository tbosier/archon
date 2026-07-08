"""Provider adapter package.

Kept import-light on purpose: importing :mod:`archon.providers` must not pull in
concrete adapters (which import back into the core), so callers go through
:mod:`archon.providers.registry` explicitly.
"""
