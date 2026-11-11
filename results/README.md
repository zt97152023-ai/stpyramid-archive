# Results and manuscript evidence

The machine-readable ledger separates historical archive measurements, mechanism controls and the release smoke test. The code release did not regenerate the seven-slide benchmark.

## Image package

- NCBI466, six native levels and 1,365 tiles. Same-container direct JXL e9 used 30,305,985 B and the final conditional pyramid used 27,147,235 B, a 10.42% reduction.
- Seven verified slides from one mouse spinal-cord study. Image packages sum to 333,784,972 B for direct JXL e9 and 296,805,846 B for the conditional pyramid, a reduction of 11.08%. This is the verified subset of the 262-input campaign.
- Selected NCBI218, seven native levels and 1,962 tiles. Direct JXL e10 used 63,568,398 B and the archived pyramid used 55,315,820 B, a 12.98% reduction. Its e11 whole-slide job stopped after 523 tiles at the memory limit.

## Integrated Count + image package

On NCBI466, the verified Shared_local plus HF/L1 archive uses 29,562,396 B. The smallest measured direct integrated reference on that slide uses HDF5 gzip9 plus JXL e9 at 33,331,931 B, a difference of 11.31%. The seven-slide batch uses BPCells plus direct JXL e9 as its integrated comparator. The combined byte count decreases from 370,165,418 B to 331,144,730 B, a 10.54% reduction. These totals include Count, scientific metadata, image and package members. Image package bytes and integrated bytes have different denominators. The two modality codecs are composed into one archive.

## Mechanism and access

The 089 ablation holds the high-frequency stream fixed at 16,585,179 B. Replacing independent low-frequency storage with exact native-parent correction reduces the low-frequency stream from 7,436,065 B to 2,065,475 B. In a separate fixed-091 experiment, switching on the decoder-derived low-frequency context reduces the complete package from 28,648,255 B to 28,293,399 B.

For NCBI466, the same-position 64×64 tissue ROI took 9.12 ms with direct e9 and 31.95 ms with the final pyramid. Full restore took 8.87 and 27.88 seconds, respectively, including writing and verification. These historical timings use uncontrolled OS cache. A background scheduling guarantee or measurement of interference with foreground viewing remains separate from these results.

## Release check

The Windows source build produced the three encoder/decoder executable and library pairs. A new synthetic archive selected a 091 reference record and restored all three native levels and the typed H5AD in separate processes. The smoke also decodes 091 and HF candidates directly, checks ROI and Count subsets, and rejects a copied archive with an altered payload. The release reader also re-verified the historical NCBI218 integrated archive with its pinned historical runtime.
