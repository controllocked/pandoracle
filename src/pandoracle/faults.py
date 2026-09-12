from __future__ import annotations

import os


def trigger_fault(point: str) -> None:
    """Trigger a deterministic test-only interruption at a durable boundary."""
    configured = os.environ.get("PANDORACLE_TEST_FAULT_POINT")
    if configured is None:
        return
    configured_point, separator, action = configured.partition(":")
    if configured_point != point:
        return
    os.environ.pop("PANDORACLE_TEST_FAULT_POINT", None)
    if not separator or action == "interrupt":
        raise KeyboardInterrupt(f"injected fault at {point}")
    if action == "exit":
        os._exit(97)
    raise RuntimeError(f"unknown test fault action: {action}")
