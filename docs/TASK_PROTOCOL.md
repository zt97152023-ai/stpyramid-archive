# Desktop task protocol

The Java desktop client and Python desktop client invoke the same Python worker process. The worker accepts one UTF-8 JSON object on standard input and emits newline-delimited JSON events on standard output. Diagnostic output from codec modules is redirected to standard error.

This protocol is an internal application interface, not an archive-format definition.

## Request fields

Every request contains an `action`.

| Action | Required fields | Optional fields |
|---|---|---|
| `encode` | `mode`, `image`, `output` | `counts`, `metadata`, `keep_work`, `language` |
| `verify` | `archive` | `runtime`, `language` |
| `restore` | `archive`, `output` | `runtime`, `language` |

Supported encode modes:

- `image`: image-only fast fused archive.
- `integrated`: Count + image maximum-compression workflow.
- `integrated-fused`: Count + image fast-fused workflow.

Language is `en` or `zh`. Optional empty strings for `metadata`, `runtime` and the verify-only `output` field are treated as not supplied for compatibility with existing desktop forms. `keep_work` must be a JSON boolean.

The worker validates the request before importing or invoking the codec implementation. Validation is implemented in `task_contract.py`; keep this module standard-library-only.

## Event stream

Each standard-output line is one JSON object. Current event shapes include:

| Event | Example | Meaning |
|---|---|---|
| Stage begins | `{"begin":"shared_count_encode"}` | A named stage started |
| Stage ends | `{"end":"shared_count_encode","seconds":12.5}` | A named stage completed |
| Progress metadata | `{"native_workers":8}` | Worker/runtime detail |
| Final success | `{"result":{"output":"...","bytes":123}}` | Task completed |
| Final failure | `{"error":"ValueError: ..."}` | Task failed |

Clients should tolerate unknown event keys so new progress metadata can be added without breaking old clients. The process exit code remains authoritative: zero indicates success; non-zero indicates failure. A success event without a zero exit code must not be treated as a completed task.

## Runtime configuration

The Python worker reads validated settings from `runtime_config.py`:

| Environment variable | Default | Range |
|---|---:|---:|
| `STPYRAMID_FUSED_WORKERS` | 8 | 1–8 |
| `STPYRAMID_MAXIMUM_WORKERS` | 8 | 1–8 |
| `STPYRAMID_SCAN_WORKERS` | 8 for `l1_conditional`, otherwise 4 | 1–8 |
| `STPYRAMID_TIMEOUT_SECONDS` | 14,400 seconds | 1–86,400 |

The timeout setting applies to supervised native encoding. Existing worker-count environment names and defaults are preserved. Invalid values fail with an actionable message instead of being silently coerced.

## Compatibility rules

- Keep existing action names and mode names stable.
- Add optional fields before introducing new required fields.
- Do not send codec-specific options from the UI unless the worker validates and documents them.
- Do not change archive format identifiers or manifest members through this task protocol.
- Keep task validation independent from GUI and scientific dependencies.
- Clients must not assume every platform supports the native Count + TIFF backend.
