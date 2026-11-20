"""Validated runtime settings shared by the desktop worker.

Keep environment variable names and defaults stable: packaged deployments and
existing user launch scripts may rely on them.
"""
import os


def bounded_int_env(name, default, *, minimum=1, maximum=8):
    """Read a bounded integer setting with an actionable validation error."""
    raw = os.environ.get(name)
    if raw is None or raw == "":
        value = default
    else:
        try:
            value = int(raw)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{name} must be an integer from {minimum} to {maximum}") from exc
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be an integer from {minimum} to {maximum}")
    return value


def fused_worker_count():
    return bounded_int_env("STPYRAMID_FUSED_WORKERS", 8)


def maximum_worker_count():
    return bounded_int_env("STPYRAMID_MAXIMUM_WORKERS", 8)


def scan_worker_count(image_profile):
    default = 8 if image_profile == "l1_conditional" else 4
    return bounded_int_env("STPYRAMID_SCAN_WORKERS", default)


def encoding_timeout_seconds():
    """Maximum supervised encoding duration, in seconds (default: four hours)."""
    return bounded_int_env("STPYRAMID_TIMEOUT_SECONDS", 14400, maximum=86400)
