# Contributing to STPyramid

Thank you for helping improve STPyramid. The project prioritizes exact scientific recovery, archive compatibility and reproducible measurements over code movement or cosmetic refactoring.

## Before changing code

1. Read [the architecture](docs/ARCHITECTURE.md) and [the code map](docs/CODEMAP.md).
2. Identify whether the change affects the Count codec, image codec, archive/container, desktop client or packaging.
3. Check whether it changes an archive member, manifest field, codec identifier, runtime pin or decoder behavior. These are compatibility-sensitive changes.
4. Keep research experiments and historical evidence separate from the stable user workflow.

## Development principles

- Keep Count and image codec implementations independent.
- Keep archive assembly and spatial coordinate binding in the container layer.
- Do not silently fall back to a different codec when an input or backend is unsupported.
- Preserve exact Count values, native RGB values and supported metadata.
- Do not claim byte-identical TIFF/H5AD containers after restore; the recovery contract concerns decoded values and supported structure.
- Do not commit study datasets, patient/sample source data, generated archives, native runtime binaries or secrets.
- Retain upstream license and attribution notices for third-party code.

## Validation expectations

For Python integrated codec or archive changes, run the synthetic end-to-end smoke test on a qualified Windows environment:

```powershell
python tests/smoke.py --runtime-dir .\runtime-windows --output .\runs\smoke
```

For compatibility-sensitive changes, also verify representative historical archives using the runtime versions declared by those archives. A new synthetic smoke test alone is not proof of backward compatibility.

For native changes, build the affected CMake targets and run the end-to-end smoke test. For Java UI or packaging changes, build and launch the package on the target platform and check the capabilities it reports.

If a test cannot be run, state the reason and do not describe it as passed.

## Pull requests

A useful pull request includes:

- The user or developer problem being solved.
- The exact components and files changed.
- Whether archive format or compatibility behavior changes.
- Tests run, with platform and command.
- Known limitations and any evidence that remains unverified.

Keep refactors small enough that reviewers can distinguish behavior changes from code movement.
