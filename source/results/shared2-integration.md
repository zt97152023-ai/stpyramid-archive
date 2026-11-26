# Shared 2.0 integration verification

New Windows CLI and desktop Count + TIFF archives identify their Count codec as
`Shared_2`. The reader also accepts historical integrated `Shared_local`
archives. The qualification used the existing pinned Shared engine and Windows
native TIFF runtimes with CPython 3.12.14.

## Count stream parity

The integration regenerated the Count streams for all six development samples
from their original H5AD inputs. Each bank, compressed index, base model and
optional occupancy member matched the corresponding frozen development member
by SHA-256. The byte totals below cover those stream members.

| Tissue section | Genes per group | Occupancy model | Stream bytes | Exact H5AD and queries |
|---|---:|---|---:|---|
| MEND12 | 256 | No | 239,221 | Pass |
| NCBI29 | 256 | No | 339,128 | Pass |
| NCBI791 | 256 | Yes | 1,435,292 | Pass |
| SPA45 | 256 | No | 83,710 | Pass |
| TENX105 | 64 | Yes | 357,033 | Pass |
| TENX141 | 64 | Yes | 1,237,591 | Pass |

For every sample, the test compared the CSR arrays, the restored H5AD semantic
digest and a datatype oracle against the original H5AD. It also compared the
frozen whole-slide single-gene, regional all-gene and regional eight-gene
queries directly with input counts. Reordered, repeated and empty selections
were checked separately.

## Integrated archive verification

NCBI29 was assembled with the same fast-fused image component in each Count
configuration. The previous `Shared_local` package occupied 29,288,380 bytes
and the new `Shared_2` package occupied 29,287,485 bytes. The new checkout read
both archives. All seven native TIFF levels, the H5AD semantic digest and
datatype oracle, and the original HEST JSON passed comparison with the source.

The synthetic Windows CLI end-to-end test passed with the maximum-compression
image path. It covered encode, verify, restore, image ROI, Count subsets,
aligned regional reads and rejection of a changed image member.

The assembled Windows Java package used its bundled JRE with Java absent from
PATH. The NCBI29 fast-fused command encoded a 29,287,485-byte archive, and
separate verify and restore commands passed. An independent check matched all
seven original TIFF levels, H5AD contents and datatypes, and the JSON
attachment. Java process wall times for this single run were 52.75 s to
encode, 7.45 s to verify and 8.70 s to restore. The Java reader also verified
the historical Shared_local archive.

This verification covers six Count components and one real full Count + TIFF
pair. The six cases are development samples. The figures above describe physical
bytes and exactness for these particular artifacts.
