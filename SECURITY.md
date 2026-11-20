# Security model and reporting

## Application security model

STPyramid is currently a local desktop/CLI scientific archiving application. The Java and Python front ends dispatch local processing jobs; the native codecs operate on user-selected files and local archive directories. The inspected application does not implement application user accounts, password login, OAuth/OIDC, JWTs, bearer access tokens, refresh tokens, browser sessions or role-based access control. Access to the application and its files is therefore primarily governed by the operating system account and filesystem permissions.

Do not describe archive checksums as authentication. SHA-256 and CRC32 detect unexpected changes relative to recorded values; because an ordinary archive manifest is stored alongside its payload, these checks do not prove who created the archive or protect against an attacker who can rewrite both payload and manifest. Use a separately authenticated and access-controlled distribution channel when archive provenance matters.

## Trust boundaries

- **User-selected source files and archive paths:** treat them as untrusted input. The application does not provide a general-purpose sandbox around native codecs or Python libraries.
- **Source archive files (`.stsource`):** treat incoming ZIP containers and their manifest/member names as untrusted. Extraction must reject unsafe paths, duplicate entries, malformed manifests, unexpected members and digest/length mismatches.
- **Pinned Count dependency:** `codec/stbaseline/_shared_vendor/SOURCE.json` defines the expected upstream commit, file lengths and SHA-256 digests. Cached files must be checked against those pins before use. The pin manifest itself is part of the trusted source checkout; this mechanism is not a signed software-update system.
- **Portable runtime:** only run a runtime obtained from a trusted release or built from reviewed source. Presence checks and archive integrity checks are not substitutes for operating-system code signing or binary provenance.
- **Archive manifests and checksums:** useful for corruption detection and reproducibility, but not cryptographic authenticity unless the manifest is authenticated independently.

## Implemented hardening in this branch

- Re-hash every cached Count dependency file against the pinned SHA-256 and exact byte length. A file's existence alone no longer makes the dependency runtime ready.
- Validate the pin manifest's upstream origin, commit, expected member count, unique relative paths, size bounds and digest syntax.
- Reject symlinked dependency paths and paths that resolve outside the vendored source directory. Verify HTTPS download redirects remain on `raw.githubusercontent.com`; verify downloaded bytes before publication.
- Publish downloaded dependency files using a same-directory temporary file, flush/fsync and atomic replacement; verify the published bytes again.
- Reject `.` and `..` path segments, absolute paths, Windows drive syntax, backslashes, reserved device names and malformed member names in Java source archive manifests.
- Bound source archive manifest size before parsing; reject duplicate logical names and unexpected format identifiers; use overflow-safe extracted-length accounting.
- Preserve the existing source archive format identifier and member payload representation. These checks do not change the scientific Count or TIFF codec streams.
- Apply 16 MiB bounds to baseline and integrated JSON manifests before parsing; validate baseline member lengths and SHA-256 descriptor syntax.
- Bound grouped Count compressed-index reads from the declared matrix shape, cap the shared model member at 16 MiB, and bound the occupancy-model read before decompression.
- Make the Shared Count runtime loader call the same canonical pinned-source verification used by the dependency downloader, so direct codec imports cannot bypass the cache checks.
- Authenticate the optional BPCells loopback worker with a one-time 256-bit token delivered over the child stdin pipe (not command-line arguments); reject unauthenticated peers and cap handshake attempts/time.
- Preflight HDF5 object graphs before Count metadata access; reject soft/external links, hard-link aliases/cycles, virtual datasets, external raw storage and object-reference datasets rather than following paths outside the selected HDF5 file.
- Apply the same HDF5 link preflight to the baseline, integrated, lossless-component, native-WSI and Shared-WSI readers; cap their archive JSON manifests at 16 MiB before parsing.

## Output permissions

The Python CLI and desktop worker set a restrictive POSIX umask (`077`) before creating archive outputs. Native POSIX codec output files use owner-only mode (`0600`). Java source-archive restoration stages into a private temporary directory before publishing it. On Windows, output permissions inherit the parent directory ACL; use an ACL-restricted destination for patient or otherwise sensitive data. Existing archives are not retroactively chmod'ed. Users who intentionally need group sharing can explicitly adjust permissions after creation and should apply equivalent controls to every archive member and parent directory.

## Operational recommendations

1. Keep the application, source code and portable runtime in directories not writable by untrusted users.
2. Do not open or restore archives from unknown sources on a machine containing sensitive data without an appropriate isolated environment.
3. Keep source data and archive outputs on filesystems with appropriate access controls and backups.
4. Verify downloaded releases through a trusted release channel. Do not treat an archive's internal SHA-256 values as a digital signature.
5. Report suspected vulnerabilities privately to the repository maintainers before publishing exploit details. Include the affected version/commit, reproduction steps and impact; do not include patient data or confidential slide content in reports.

## Scope and remaining work

This document records a source-level review and targeted hardening. It is not a formal penetration test, independent security certification or guarantee that all attack surfaces have been eliminated. In particular, the repository still needs dedicated adversarial tests for malformed ZIP/HDF5/TIFF inputs, archive resource exhaustion, Windows reparse-point and case-insensitive path behavior, native decoder robustness, and runtime binary provenance. The local application has no per-user account or role authorization boundary. The optional BPCells worker uses authenticated loopback IPC, but this is process-to-process authentication, not user login or authorization. A future networked service would require a separately designed identity, authorization, transport security and audit architecture.
