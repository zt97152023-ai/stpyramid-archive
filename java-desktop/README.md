# STPyramid Java Desktop

Java 17+ Swing desktop and CLI for importing, compressing, verifying and restoring spatial transcriptomics datasets.

## Build on Windows

Use PowerShell 7 and a JDK with javac.

```powershell
./java-desktop/build.ps1 -Jdk C:/path/to/jdk -Output ./runs/STPyramidJava.jar
java -jar ./runs/STPyramidJava.jar
```

The JAR manages tasks, validation commands and archives. The native codecs and typed HDF5 reader run in a qualified Windows runtime. A JAR deployed separately can use an explicit runtime directory or the `STPYRAMID_PORTABLE_RUNTIME` environment variable. The native runtime must have `python/python.exe` and `app/desktop_worker.py`.

## Offline distribution

Extract the Windows x64 ZIP and double-click `Launch-STPyramidJava.cmd`. Keep the JAR, `jre/` and `runtime/` together. The launcher uses the bundled Java runtime and preselects the bundled native runtime. The JAR also discovers the adjacent runtime directly. The offline bundle fails with an error if the pinned Count files are missing or invalid.

To assemble a local offline distribution, use a Windows x64 JDK that includes the desktop modules and an installed qualified Windows runtime that has already prepared its pinned Count component.

```powershell
./java-desktop/package.ps1 -Jdk C:/path/to/jdk -Runtime C:/path/to/portable -OutputDir D:/releases/STPyramid-Java-0.2.1-Windows-x64
```

The bundle contains third-party components. See `THIRD_PARTY.md` and the Java `legal/` directory. The packaged Count source has a pending original-code license upstream. The offline bundle is for local use until redistribution rights are established.

## HEST

Select the root containing wsis/, st/ and metadata/. Each complete sample contains wsis/ID.tif (or .tiff), st/ID.h5ad and metadata/ID.json. Batch mode accepts space-separated IDs. Leaving the list blank processes all discoverable trios in sorted order. Choose a new output path for each archive. For a partially completed batch, give the remaining sample IDs on the next run.

```powershell
java -jar STPyramidJava.jar list-hest E:/Hestdata
java -jar STPyramidJava.jar hest E:/Hestdata NCBI29 H:/archive/NCBI29.stpyramid PATH_TO_PORTABLE_RUNTIME
java -jar STPyramidJava.jar hest-batch E:/Hestdata H:/archive PATH_TO_PORTABLE_RUNTIME NCBI29 NCBI37
java -jar STPyramidJava.jar verify H:/archive/NCBI29.stpyramid PATH_TO_PORTABLE_RUNTIME
java -jar STPyramidJava.jar restore H:/archive/NCBI29.stpyramid H:/restored/NCBI29 PATH_TO_PORTABLE_RUNTIME
```

The integrated HEST mode defaults to **Fast fusion**. Select **Maximum compression** when desired. The CLI commands `hest`, `hest-batch` and `paired` retain maximum compression. Their `-fused` counterparts use the fused native-pyramid codec and the same Shared_local Count component. The fused integrated encoder uses up to four native workers by default. Set `STPYRAMID_FUSED_WORKERS` to an integer from 1 to 8 to tune the resource allocation. Smaller CPU affinity sets reduce the effective worker count. Encoded records are written in the original order. Each native record and the published complete archive retain their lossless checks. Both modes verify restored scientific H5AD content and every native TIFF RGB level. They require supported H5AD Count data, pre-aligned L0 coordinates and supported native TIFF geometry and storage. See the repository README for precise limits. The separate image mode invokes the fast TIFF pipeline for image-only archives. The paired CLI command accepts explicitly selected TIFF, H5AD and JSON paths outside a HEST directory.

Full native validation and restoration decode independent target tiles concurrently while keeping parent lookup, bank reads and stripe assembly ordered. `STPYRAMID_SCAN_WORKERS` controls full-scan workers in standalone verification and restoration. The default is 4 and supported values are 1 to 8. Faster scans do not change the archive format or the separate region-query reader.

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
