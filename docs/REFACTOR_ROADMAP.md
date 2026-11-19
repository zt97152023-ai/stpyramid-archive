# Engineering refactor roadmap

This roadmap separates safe maintainability work from changes that could affect scientific values, archive compatibility or published measurements.

## Current baseline

The current implementation has a clear runtime path but a mixed repository layout:

- `stpyramid.py` is the Python CLI entry point.
- `codec/integrated_encode.py` orchestrates the integrated Count + image workflow.
- `codec/stbaseline/` contains the grouped Count implementation and supporting codec modules.
- `codec/archive_image_encode.py` and `codec/joint_next.py` coordinate the integrated image path.
- `codec/container_compare.py` owns archive composition, verification, selective reads and restoration.
- Java and Python desktop applications are separate clients.
- Several root-level scripts are fixed-cohort benchmark tools, historical experiments or compatibility wrappers.

The current flat imports are deliberate in several places: the CLI and desktop worker add `codec/` to `sys.path`, while codec modules import sibling modules by their top-level names. Moving files or converting imports in one pass would be a high-risk change.

## Refactor sequence

### R0 — Make responsibilities discoverable

- [x] Rewrite the root README as a short project entry point.
- [x] Add a three-minute quick start with verified release downloads.
- [x] Document the Count, image and archive boundaries.
- [x] Add a clickable source code map.
- [x] Classify root-level research and compatibility scripts.
- [x] Document contributor and validation expectations.
- [x] Add dependency-free CLI parser/import contract tests and a three-platform CI workflow.

**Acceptance:** documentation links resolve; user paths and platform limitations match the published release and current source.

### R1 — Establish a behavior baseline

Before moving code:

1. Record the current source commit and release/runtime pins.
2. Run `tests/smoke.py` with the qualified Windows runtime.
3. Verify at least one current Shared 2.0 archive and one historical Shared_local archive.
4. Verify a representative historical image archive with its matching native runtime.
5. Save machine-readable outputs, source identities, decoded Count equality, native RGB equality and manifest summaries.

**Acceptance:** baseline evidence is reproducible and stored separately from source code. Do not proceed with structural code movement if this baseline is unavailable.

### R2 — Add import and CLI contract tests

- [x] Test that importing the CLI does not load scientific/native dependencies.
- [x] Test each documented CLI subcommand's help/parser contract without a runtime.
- [ ] Test desktop-worker request validation independently from GUI code.
- [ ] Inventory every `sys.path` mutation and flat import before changing package boundaries.
- Test desktop-worker request validation independently from GUI code.

**Acceptance:** tests describe existing behavior first; they must not encode a proposed behavior change as if it were current behavior. The initial CLI checks are wired to GitHub Actions on Windows, Linux and macOS; they remain pending until the workflow reports a successful run.

### R3 — Centralize runtime configuration

Create one small configuration module for:
- runtime directory discovery;
- worker-count bounds and environment variable parsing;
- timeout validation;
- platform capability reporting.

Keep existing environment variable names and defaults during the first migration. The configuration layer must not silently select a different codec, runtime or CPU policy.

**Acceptance:** old commands and environment settings produce the same selected runtime, worker count and timeout as before.

### R4 — Separate stable application code from research tooling

After the dependency inventory, move only scripts with no external path contracts:
- cohort and benchmark orchestration;
- historical candidate comparison tools;
- one-off research utilities.

Retain documented compatibility wrappers when external notebooks, scripts or benchmark records may refer to the old path. Do not move codec modules in this step.

**Acceptance:** benchmark outputs, fixed-cohort membership, source hashes and result ledgers remain unchanged.

### R5 — Introduce explicit Python package boundaries

Only after import-contract tests exist:
- introduce a package namespace for the stable Python application;
- replace flat imports incrementally, one module family at a time;
- keep temporary compatibility shims for old import paths;
- avoid changing archive serialization and decoder semantics.

**Acceptance:** old CLI invocations, desktop worker jobs, synthetic smoke tests and historical archive reads all pass.

### R6 — Unify task execution interfaces

Define a stable task request/result/event contract shared by CLI and desktop clients. The UI remains a client; it should not own codec logic. Progress events should identify stages, elapsed time, output paths and actionable failures without exposing temporary internal paths unnecessarily.

**Acceptance:** CLI and desktop produce equivalent archive bytes for a fixed deterministic fixture where the encoder is deterministic, and always produce identical decoded scientific values.

## Non-negotiable compatibility gates

Any change touching one of these is a compatibility-sensitive change, not a cosmetic refactor:

- archive format identifiers and manifest schema;
- Count frame headers, model tables, indexes or coding order;
- image record routes, anchor mapping or decoder ABI;
- runtime hashes and pinned external Count source;
- coordinate conventions and H5AD typed structure;
- verification semantics and restoration behavior.

For each such change, require:
1. synthetic exact-recovery tests;
2. current-format archive verification;
3. historical archive verification using the declared runtime;
4. byte and manifest accounting;
5. documented migration or explicit statement that the format is unchanged.

## Explicitly out of scope for cleanup-only work

- Re-training, re-fitting or re-tuning codec models.
- Changing compression candidates or tie-breaking rules.
- Rewriting historical results or benchmark ledgers.
- Removing old decoders before archive compatibility is established.
- Claiming Linux/macOS native Count + TIFF support based only on Java packaging success.
- Moving all files merely to create a visually neat directory tree.

## Next engineering task

The first code-changing task should be **R2: import and CLI contract tests**, followed by R3 runtime configuration. This creates a safety net before changing imports or moving scripts.
