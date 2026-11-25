# STPyramid Java Desktop

Java 17+ Swing desktop and CLI for importing, compressing, verifying and restoring spatial transcriptomics datasets.

## Platform packages

The Java application and the source-directory archive format run on Windows, Linux and macOS. Each platform package contains a private Java runtime under jre/, so the user does not need to install Java or configure JAVA_HOME. Launch the bundle with Launch-STPyramidJava.sh on Linux or macOS, Launch-STPyramidJava.command on macOS, and Launch-STPyramidJava.cmd on Windows.

Build a Linux or macOS package on that operating system with a matching JDK. The JDK is used only while assembling the package. The resulting directory contains the runtime and does not require a Java installation on the target machine.

```sh
./java-desktop/package-linux.sh /path/to/jdk-17 /tmp/STPyramid-Linux-x64
./java-desktop/package-macos.sh /path/to/jdk-17 /tmp/STPyramid-macOS-arm64
```

The macOS package also creates STPyramid.app. Open the app from Finder or run its Contents/MacOS/STPyramid launcher. The Linux package includes a desktop entry file.

The cross-platform source commands are list-hest, hest-source, source, verify and restore for .stsource archives. The native Count＋TIFF codec is qualified in the supplied Windows runtime. Linux and macOS packages report that capability explicitly instead of attempting to load Windows binaries.

Use capabilities to inspect the installed package.

```sh
./Launch-STPyramidJava.sh capabilities
```

## Build on Windows

Use PowerShell 7 and a JDK with javac.

```powershell
./java-desktop/build.ps1 -Jdk C:/path/to/jdk -Output ./runs/STPyramidJava.jar
java -jar ./runs/STPyramidJava.jar
```

The JAR manages tasks, validation commands and archives. The native codecs and typed HDF5 reader run in a qualified Windows runtime. A JAR deployed separately can use an explicit runtime directory or the `STPYRAMID_PORTABLE_RUNTIME` environment variable. The native runtime must have `python/python.exe` and `app/desktop_worker.py`.

## Offline distribution

Extract the Windows x64 ZIP and double-click `Launch-STPyramidJava.cmd`. Keep
`STPyramidJava.jar`, `jre/` and `runtime/` together.
Pass an argument such as `capabilities` to the launcher to run the CLI.
The bundled Java and native runtime are discovered next to the application.
An offline bundle requires the verified pinned Count dependency.

To assemble a local offline distribution, use a Windows x64 JDK that includes the desktop modules and an installed qualified Windows runtime that has already prepared its pinned Count component.

```powershell
./java-desktop/package.ps1 -Jdk C:/path/to/jdk -Runtime C:/path/to/portable -OutputDir D:/builds/STPyramid-Java-Windows-Offline
```

The bundle contains third-party components. See `THIRD_PARTY.md` and the Java `legal/` directory. The packaged Count source has a pending original-code license upstream. The offline bundle is for local use until redistribution rights are established.

For a distribution that leaves the separate Count dependency to first use,
add `-FetchCountOnFirstUse`. This bundles the Java and native runtimes
and the dependency pin manifest. The first operation that needs Shared Count
requires network access and a writable extracted directory.

Build a versioned package from a clean Git checkout. The ZIP records the
source commit and the dependency mode in `RELEASE_INFO.json` and has a
separate SHA-256 file.

```powershell
./java-desktop/package.ps1 -Jdk C:/path/to/jdk -Runtime C:/path/to/portable -OutputDir D:/releases/STPyramid-Java-0.2.6-Windows-x64 -FetchCountOnFirstUse -ReleaseVersion v0.2.6
```

## HEST

Select the root containing wsis/, st/ and metadata/. Each complete sample contains wsis/ID.tif (or .tiff), st/ID.h5ad and metadata/ID.json. Batch mode accepts space-separated IDs. Leaving the list blank processes all discoverable trios in sorted order. Choose a new output path for each archive. For a partially completed batch, give the remaining sample IDs on the next run.

```powershell
java -jar STPyramidJava.jar list-hest E:/Hestdata
java -jar STPyramidJava.jar hest-fused E:/Hestdata NCBI29 H:/archive/NCBI29.stpyramid PATH_TO_PORTABLE_RUNTIME
java -jar STPyramidJava.jar hest-batch-fused E:/Hestdata H:/archive PATH_TO_PORTABLE_RUNTIME NCBI29 NCBI37
java -jar STPyramidJava.jar verify H:/archive/NCBI29.stpyramid PATH_TO_PORTABLE_RUNTIME
java -jar STPyramidJava.jar restore H:/archive/NCBI29.stpyramid H:/restored/NCBI29 PATH_TO_PORTABLE_RUNTIME
```

The integrated HEST desktop mode defaults to **Fast fusion**. The CLI commands
`hest`, `hest-batch` and `paired` select **Maximum compression**.
Their `-fused` counterparts select Fast fusion. Both modes in this source
revision write `Shared_2` Count and read historical `Shared_local` Count
archives. The published v0.2.5 tag contains the earlier Count writer.

Fast fusion uses up to eight native workers by default in integrated and
TIFF-only modes. Set `STPYRAMID_FUSED_WORKERS` to 1–8. The app prefers
permitted logical CPUs with lower observed utilization and caps the worker
count to the affinity set. L0 and L1 share a bounded worker pool, and records
are written in the original order. In integrated modes, use the separate
Verify command to check restored scientific H5AD content and every native
TIFF RGB level. See the repository README for
input limits. The paired CLI commands accept explicit TIFF, H5AD and JSON
paths outside a HEST directory.

Full native validation and restoration decode independent target tiles concurrently while keeping parent lookup, bank reads and stripe assembly ordered. `STPYRAMID_SCAN_WORKERS` controls full-scan workers in standalone verification and restoration. The default is 8 for fast fusion archives and 4 for other native profiles. Supported values are 1 to 8. Faster scans do not change the archive format or the separate region-query reader.

Maximum compression runs independent candidate families concurrently. Direct JPEG XL candidates for identical native RGB tiles share one encode and immediate decode check. Candidate selection uses the existing total bytes, route order, and tie-breaking rules. Set `STPYRAMID_MAXIMUM_WORKERS` to 1 through 8 to set the maximum per-job CPU allocation. The default is 8. The app prefers permitted logical CPUs with lower observed utilization at job start. A budget of 1 uses the serial path. Stage durations in parallel jobs can overlap. Use process wall time for end-to-end timing.

## Original source files

Use the native-directory mode for Space Ranger and Visium HD, Xenium, CosMx, MERSCOPE, Stereo-seq, Zarr and other source folders. Format labels are inferred from filenames. This mode streams files through ZIP/ZIP64 with DEFLATE, records SHA-256 and byte lengths, and verifies every member before publishing. The restore operation produces the original files and directory structure, including any vendor files present in the selected tree. Retain the primary source tree when there are additional files external to a selected folder. The HEST original-files mode packages the HEST trio using the original relative paths.

```powershell
java -jar STPyramidJava.jar hest-source E:/Hestdata NCBI29 H:/archive/NCBI29.stsource
java -jar STPyramidJava.jar source E:/datasets/vendor/outs H:/archive/vendor.stsource
java -jar STPyramidJava.jar verify H:/archive/vendor.stsource
java -jar STPyramidJava.jar restore H:/archive/vendor.stsource H:/restored/vendor
```

The Java source-archive mode does not need the native runtime. It preserves file bytes and paths. Filesystem permissions and timestamps are outside its restore scope. Native vendor readers, coordinate transformations and region queries for each proprietary vendor object require dedicated adapters. Use the explicit source mode to keep those files intact today. The app reports unsupported inputs without silently switching codec.

## Fixed HEST cohort benchmark

`benchmark_hest.py` runs the Java JAR against an explicit cohort manifest with the source SHA-256 and byte count for each TIFF, H5AD and JSON. Run the `pilot` command using the NCBI29 archive, original encoding log, and timed Java verification and restore logs in the acceptance directory. The gate verifies archive members, compares the restored H5AD values and types with the source, and compares every native RGB level with the source TIFF and archive manifest. It also checks the retained image metadata and original JSON.

After the gate passes, run `cohort` with the same arguments. The controller reuses the verified NCBI29 package and runs the Java encode, verify and restore commands for every other fixed sample. The output includes live `STATUS.json`, `results.csv` and per-attempt receipts and logs. Timings include Java process and backend startup for fresh invocations. The old pilot encode log reports native stage times. The cohort can be resumed once the previous controller and its workers have exited. Completed successes are skipped, and `--retry` repeats failed samples into fresh attempt directories. Use two workers only if local disk and memory permit.

`compare_java_hest.py` measures both NCBI29 profiles sequentially through Java on one logical CPU. It separately times encoding, full read verification and full restoration, then independently checks restored data and requires identical Count members. A verified fused run that saves encoding time qualifies the fixed cohort via `benchmark_hest.py cohort --profile fused` in a separate output directory. The comparison records elapsed wall time from Java launch to exit. Fused stage logs contain nested events, so do not add all stage times to estimate wall time.
