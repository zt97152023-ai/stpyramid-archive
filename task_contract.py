"""Validate the JSON task protocol used by desktop clients.

This module intentionally uses only the Python standard library so request
validation can be tested without loading codecs, scientific packages or the
native runtime.
"""

ACTIONS = frozenset({"encode", "verify", "restore"})
ENCODE_MODES = frozenset({"image", "integrated", "integrated-fused"})


def _required_text(request, key):
    value = request.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Task field '{key}' must be a non-empty string")
    return value


def validate_task(request):
    """Validate and return a task request without modifying it."""
    if not isinstance(request, dict):
        raise ValueError("Task request must be a JSON object")

    action = request.get("action")
    if not isinstance(action, str) or action not in ACTIONS:
        raise ValueError("Task action must be one of: encode, verify, restore")

    language = request.get("language", "en")
    if not isinstance(language, str) or language not in ("en", "zh"):
        raise ValueError("Task language must be 'en' or 'zh'")

    if action == "encode":
        mode = request.get("mode")
        if not isinstance(mode, str) or mode not in ENCODE_MODES:
            raise ValueError("Encode mode must be one of: image, integrated, integrated-fused")
        _required_text(request, "image")
        _required_text(request, "output")
        if mode in ("integrated", "integrated-fused"):
            _required_text(request, "counts")
            metadata = request.get("metadata")
            if metadata not in (None, "") and (not isinstance(metadata, str) or not metadata.strip()):
                raise ValueError("Task field 'metadata' must be a non-empty string when supplied")
    else:
        _required_text(request, "archive")
        if action == "restore":
            _required_text(request, "output")

    runtime = request.get("runtime")
    if runtime not in (None, "") and (not isinstance(runtime, str) or not runtime.strip()):
        raise ValueError("Task field 'runtime' must be a non-empty string when supplied")

    keep_work = request.get("keep_work", False)
    if type(keep_work) is not bool:
        raise ValueError("Task field 'keep_work' must be a boolean")

    return request
