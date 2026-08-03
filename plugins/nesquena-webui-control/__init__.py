"""Lifecycle shim for Hermes' user-plugin allow-list.

The functional plugin lives under ``dashboard/``. Hermes' enable command
discovers user plugins through ``plugin.yaml`` and expects a register function,
so this intentionally registers no agent hooks or tools.
"""


def register(_context) -> None:
    return None
