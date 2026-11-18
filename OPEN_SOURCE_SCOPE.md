# Open-source scope

This repository contains the original STPyramid Java, Python and C++ source,
the cross-platform Java packaging scripts, reproducibility helpers and
aggregate evidence records. Original STPyramid source is released under the
BSD 3-Clause License in `LICENSE`.

The repository does not include study images, H5AD files, complete archive
outputs, native EXE or DLL files, or the binary Windows runtime. Build the
native Windows runtime from the C++ sources with the commands in `README.md`,
or provide a locally qualified runtime to `java-desktop/package.ps1`.

The Shared 2.0 grouped-frame writer and reader, support-set codec and archive
integration are included as original STPyramid source. The underlying Shared
Count dependency is fetched from a pinned external revision. Its upstream
project has not provided a confirmed original-code license, so that dependency
is installed separately. The fetch
script verifies the pinned bytes and `THIRD_PARTY.md` records this boundary.

The Java source-archive mode is platform independent. The native Count and
TIFF workflow is qualified by the supplied runtime on Windows. Linux and
macOS packaging scripts bundle a matching JRE and expose the cross-platform
source-archive workflow. Native Count and TIFF backends for those operating
systems require a separately qualified implementation.
