# Codec naming

STPyramid uses mechanism-based names in documentation, user interfaces, build entry points and research figures. Historical experiment numbers are not public codec names.

## Canonical names

| Canonical name | Role | Decoder dependency | Public description |
|---|---|---|---|
| **Anchor Codec** | Exact signed residual coding with a paid one-hop native L1 reference for independent L0 tiles | Native parent-level reference | Reuses exact native parent pixels to anchor the low-frequency correction while retaining an independently decodable tile record |
| **Context Codec** | Parent-conditioned detail coding with the same paid anchor-correction stream | Known parent properties and the Anchor correction | Uses decoder-available parent context to condition the detail model; control and conditional modes share the same coefficient order and reference slots |
| **HF-Detail Codec** | Dense magnitude/sign representation on native L0 | Its paired Modular framing and qualified native runtime | Encodes the high-frequency/detail representation while reusing the exact Anchor correction stream where required |

## Naming rules

1. Use **Anchor**, **Context**, and **HF-Detail** in prose, UI labels, figures, benchmark tables and user-facing logs.
2. Use **Anchor Codec**, **Context Codec**, and **HF-Detail Codec** on first mention in technical documentation.
3. Do not use historical experiment numbers as algorithm names or version names.
4. Keep the three names distinct from standard JPEG XL. They are STPyramid codec modules that use qualified native JPEG XL-derived components; they are not three standard JPEG XL profiles.
5. Describe a route by its function (direct JXL, anchored correction, context-conditioned detail, or HF detail) rather than by an internal route integer.
6. Do not imply that Anchor or Context alone encodes an entire TIFF pyramid. The current integrated archive selects among per-tile routes and keeps native levels at or below their existing decoder boundaries.

## Compatibility boundary

The current archive format identifiers, record magic bytes, manifest runtime-pin keys, native ABI symbols, and source module paths are compatibility identifiers. They are not public codec names and are unchanged so existing archives remain readable.

Newly staged runtime bundles use descriptive folders and executable/library names:

| Codec | Runtime folder | Executable / library |
|---|---|---|
| Anchor | `anchor/` | `anchor_codec.exe`, `anchor_codec.dll` |
| Context | `context/` | `context_codec.exe`, `context_codec.dll` |
| HF-Detail | `hf-detail/` | `hf_detail_codec.exe`, `hf_detail_codec.dll` |

The runtime readers still accept previously staged folders and filenames during migration. Manifest pin keys and native ABI symbols are translated internally, so historical archives and runtime receipts remain readable.

New build entry points are descriptive:

```powershell
cmake --build build --parallel 2 --target stpyramid_native_codecs
```

Individual targets are also available:

- `stpyramid_anchor`
- `stpyramid_context`
- `stpyramid_hf_detail`

When a diagnostic must mention an internal file or ABI symbol, explain the user-facing codec name first and show the internal identifier only as a technical locator.
