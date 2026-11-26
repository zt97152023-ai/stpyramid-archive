# STPyramid P0 paired archive benchmark

This deposit accompanies the whole-archive comparison in the STPyramid
manuscript. It freezes the development-exposed cohort of ten HEST-1k
sections, the codec source, the all-level JPEG XL effort 3 adapter, and the
historical publication and independent full-readback receipts. There are zero
study-and-donor-independent sections in this comparison.

## Verify the archived results

Run from the repository root with CPython 3.12. This audit needs no datasets,
native binaries or third-party Python packages.

~~~powershell
python p0/verify_evidence.py
~~~

The script checks public-file checksums, the six application source pins,
the two preflight receipts, 20 first-pass arm outcomes, four retry outcomes
and the ten accepted pairs. It checks the member-size ledger, matching Count
members, native pixel hashes and exactness flags, and recomputes the timing and
size table from the selected per-arm receipts.

| Metric | Regular 091 | All-level JXL e3 | Median within-section 091/e3 |
|---|---:|---:|---:|
| Complete archive sum (B) | 1,624,220,193 | 2,096,637,549 | 0.77 |
| Publication sum (s) | 608.40 | 273.81 | 2.72 |
| Independent full readback sum (s) | 236.79 | 44.72 | 3.72 |

Regular 091 reduced aggregate bytes by 22.53%. All-level JXL e3 published
and read back faster on each of the ten paired sections. The first pass
accepted 17/20 arm outcomes and 8/10 pairs. Both arms of NCBI702 and NCBI791
were rerun in isolation. The eight first-pass pairs are retained as a
sensitivity comparison. See [selected runs](p0/receipts/RECONCILED.json)
and [exposure audit](p0/receipts/INDEPENDENCE_AUDIT.json).

## Frozen files and provenance

| Path | Contents |
|---|---|
| [source](source/) | Exact committed source tree from revision fef2915d8faed840f0fd763f4d5c82adec515364. Its Git tree ID is 63addac26f0f96280ed06da0e41761686e3f4df0. |
| [p0/overlay](p0/overlay/) | Exact measured Python adapter, including the experimental direct3 switch, verified by the application source pin. |
| [p0/CONFIG.json](p0/CONFIG.json) | Fixed inputs with byte lengths and SHA-256, affinity and workers, arm order, native RGB and Count-semantic reference hashes. |
| [p0/RUNTIME_PINS.json](p0/RUNTIME_PINS.json) | SHA-256 and byte lengths of the seven native binaries used historically. |
| [p0/receipts](p0/receipts/) | Preflight, first-pass and isolated-retry records, per-arm ledgers, failures, final selection and independence audit. |
| [p0/ORIGINAL_SHA256.json](p0/ORIGINAL_SHA256.json) | Checksums of local originals before path normalization, and of the historical controller. |
| [p0/SHA256SUMS](p0/SHA256SUMS) | Checksums of the distributable P0 files. |

The public JSON and CSV copies use relative paths. Local host paths, donor
labels, mtime fields and controller PID were removed or normalized. The
original historical controller's timed encode and verify functions are
preserved in [worker.py](p0/worker.py). The portable controller implements
staging, the NCBI29 gate, fixed arm order and explicit paired retries.
Three historical first-pass failures remain in the receipts: NCBI791 regular
091 failed its extra datatype check, NCBI791 direct e3 exited while importing
Numba, and NCBI702 regular 091 hit a Windows access violation during readback.

## Run the benchmark

Use 64-bit Windows, CPython 3.12, and at least 24 available CPUs. The historical
environment used CPython 3.12.14, 24 pinned encoding CPUs, eight readback
workers, fixed source and output volumes, an unmanaged OS page cache and a
Numba cache warmed by the NCBI29 preflight. Use a fresh ASCII work path outside
the inputs and allow for large intermediate image files. The staged input
sizes are checked before the run. The timed encoder verifies source identity
and the worker checks it against the pins. A dataset revision containing the
same filenames can still differ in bytes.

Obtain access under the original terms from [HEST-1k](https://huggingface.co/datasets/MahmoodLab/hest).
Place the listed files under one directory using the CONFIG-relative paths
(wsis/, st/ and metadata/). This deposit contains no slide images, H5AD files,
clinical source metadata files, complete study packages, EXE or DLL files.
The separate Shared Count dependency is fetched and checksum-checked by
source/fetch_count_runtime.py at its pinned upstream revision.

Install the Python requirements, then build and stage the native binaries in
an x64 Visual Studio 2022 Developer PowerShell as described in
[the frozen source README](source/README.md#build-the-native-windows-runtime).

~~~powershell
python -m pip install -r source/requirements-windows-tested.txt
cmake -S source -B build -G Ninja -DCMAKE_BUILD_TYPE=Release -DCMAKE_POLICY_VERSION_MINIMUM=3.5
cmake --build build --parallel 2 --target slide_jxl089 slide_jxl089_shared slide_jxl091 slide_jxl091_shared slide_hf1 slide_hf1_shared
python source/stage_runtime.py --bin build/bin --output runtime-p0
python p0/reproduce.py prepare --work X:/p0-replay --hest-root Y:/hest --runtime-dir runtime-p0 --native-encoder build/bin/slide_jxl091.exe --allow-rebuilt-runtime
python p0/reproduce.py preflight --work X:/p0-replay
python p0/reproduce.py formal --work X:/p0-replay
~~~

Use --cpus with a comma-separated set of 24 permitted logical CPU IDs if the
historical affinity mask is unavailable. Use distinct source and work volumes
to match the historical setup. For an incomplete formal pair, use
the following command for that section. It creates a fresh directory and runs
both arms in the frozen order.

~~~powershell
python p0/reproduce.py retry --work X:/p0-replay --sample NCBI791
~~~

To require exact historical binary hashes, supply the qualified runtime
directories and top-level native encoder and omit --allow-rebuilt-runtime.
Rebuilt binaries can have different hashes, archive manifest identities and
timings. The preparation receipt records exact versus rebuilt runtime status.
The exact historical timing is documented by the archived receipts. Each
local run records its own values and verifies the scientific objects and
package members.

The portable runner was checked in a fresh work directory using the exact
historical runtime pins. Both NCBI29 preflight arms passed and produced the
same manifest SHA-256, package bytes, native pixel hashes and Count member
identities as the historical preflight. This check is separate from the
archived ten-section run. See [runner validation](p0/RUNNER_VALIDATION.json).

Original STPyramid code uses the [BSD-3-Clause license](source/LICENSE).
See [third-party notices](source/THIRD_PARTY.md) for other code and the
[open-source scope](source/OPEN_SOURCE_SCOPE.md) for the redistribution boundary.
