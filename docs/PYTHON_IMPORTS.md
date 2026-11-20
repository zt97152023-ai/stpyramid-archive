# Python import and path inventory

This inventory records the current first-party import strategy before package refactoring. It is intentionally descriptive: it does not change module locations or import behavior.

## Current import roots

The repository is not yet one conventional Python package. The current application adds selected directories to `sys.path` and imports codec modules as top-level modules. This is why moving a single file can break CLI, desktop or research entry points.

| File | Path mutation | Why it exists |
|---|---|---|
| `stpyramid.py` | Inserts repository `codec/` at index 0 | Lets the CLI import `integrated_encode`, `container_compare` and `joint_next` as top-level modules |
| `desktop_app.py` | Inserts repository root at index 0 | Lets the GUI import `desktop_i18n` when launched as a script or packaged app |
| `desktop_worker.py` | Inserts `codec/` and repository root at index 0 | Worker imports codec modules and root-level desktop/runtime helpers |
| `native_tile_encoder.py` | Inserts repository `codec/` at index 0 | Shared by image-only research/benchmark entry points that import codec modules as top-level names |
| `codec/stbaseline/shared_backend.py` | Inserts `codec/stbaseline/_shared_vendor/` at index 0, after validating the pinned source files | Makes the separately fetched `baseline.*` Count dependency importable |

These mutations are part of the current runtime contract. Do not remove them until the affected entry points and vendor loader have an explicit replacement and tests.

## Dynamic file-based imports

Several modules deliberately load sibling source files under private module names instead of importing them through a package:

| File | Dynamically loaded module | Purpose |
|---|---|---|
| `codec/joint_next.py` | `joint.py`, then `hf_adapter.py` | Reuses the prior image container implementation and loads the HF adapter lazily |
| `codec/l1_adapter.py` | `native_adapter.py`, `joint.py` | Loads the qualified native adapter and image geometry implementation under isolated names |
| `codec/stbaseline/shared_backend.py` | Frozen vendor `worker.py` | Loads the pinned Count worker and binds its runtime/model functions |

These dynamic loads preserve implementation reuse and historical behavior. Replacing them with ordinary imports needs explicit duplicate-module and import-order tests.

## Main dependency paths

### Integrated CLI

```text
stpyramid.py
  └── codec/ added to sys.path
      └── integrated_encode
          ├── container_compare
          ├── joint_next
          │   ├── stbaseline.*
          │   ├── joint.py (dynamic)
          │   └── hf_adapter.py (dynamic, lazy)
          ├── archive_image_encode
          │   ├── native_adapter
          │   ├── hf_adapter
          │   └── l1_adapter (dynamic adapter/geometry)
          └── stbaseline.grouped_count_encode
              └── stbaseline.shared_backend
                  └── pinned _shared_vendor/baseline.*
```

### Desktop clients

```text
Python GUI desktop_app.py
  └── desktop_worker.py (separate process)
      ├── root helpers: desktop_i18n, fetch_count_runtime, runtime_config, task_contract
      └── codec modules via codec/ import root

Java desktop
  └── NativeEngine starts the same desktop_worker.py process
      └── sends the same JSON task request
```

The Java client and Python GUI are separate front ends, but both eventually use the same Python worker for native Count/TIFF operations. The worker now validates the request with `task_contract.py` before dispatching the codec.

## Import rules for future changes

1. Do not move `codec/stbaseline/` independently from its relative imports.
2. Do not change top-level codec module names until CLI, worker, dynamic loader and research entry points have compatibility shims.
3. Do not move `_shared_vendor/` or alter its source pins as part of package cleanup.
4. Keep research/benchmark scripts out of the stable application import graph unless there is a tested product requirement.
5. Add import-contract tests before replacing any `sys.path` mutation.
6. Compare loaded module identities when replacing `importlib.util.spec_from_file_location`; loading the same file twice under different names can create distinct module state.

## Next safe package-refactor step

First create a complete dependency map for the stable application modules and add tests for:
- CLI import and each command;
- desktop worker request validation and event output;
- dynamic module loading and lazy HF initialization;
- Count vendor runtime loading and pin verification;
- legacy top-level imports used by research scripts.

Only then introduce a package namespace with temporary compatibility shims. Do not combine package migration with codec changes.
