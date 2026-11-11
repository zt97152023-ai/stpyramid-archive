# Third-party source notices

- The vendored libjxl source retains its [LICENSE](upstream/libjxl/LICENSE), [PATENTS](upstream/libjxl/PATENTS) and authorship files. The snapshot is derived from the locally qualified research build and contains optional research instrumentation. The release runner clears the experimental ST074 switches.
- The vendored [Brotli](upstream/libjxl/third_party/brotli/LICENSE), [Highway](upstream/libjxl/third_party/highway/LICENSE) and [skcms](upstream/libjxl/third_party/skcms/LICENSE) trees keep their own notices.
- The adapted [091](native/091/vendor091/LICENSE) and [HF1](native/hf1/vendorhf/LICENSE) JPEG XL Modular code retains the upstream BSD notice.
- The Shared Count backend comes from a [fixed public revision](https://github.com/zt97152023-ai/shared-st-count-codec/tree/4cf99cc6842cf9e9b680c1270a7e3375b6b402e1). Its original-code license is listed as pending by that project. Its source is fetched separately and is not included in the public source ZIP. The fetch script verifies the exact previously used source bytes.
- Study datasets and their licenses are separate from code. The results ledger contains sample labels and aggregate measurements only.
