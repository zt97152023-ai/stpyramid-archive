// Copyright (c) the JPEG XL Project Authors. All rights reserved.
// Use of this adapted source is governed by the BSD license in LICENSE.
// STBASELINE-091. Isolated adaptation of BSD-licensed JPEG XL Modular code.
// Known channels are decoder-derived context, never symbols in this stream.
#ifndef ST091_KNOWN_CONTEXT_H_
#define ST091_KNOWN_CONTEXT_H_
#include "lib/jxl/modular/encoding/context_predict.h"
#include "lib/jxl/modular/encoding/enc_encoding.h"
#include "lib/jxl/modular/encoding/encoding.h"
#include "lib/jxl/image_ops.h"
namespace jxl { namespace st091 {
inline bool IsTarget(size_t c) { return (c >= 3 && c < 6) || (c >= 9 && c < 12); }
inline bool IsParent(size_t c) { return c < 3 || (c >= 6 && c < 9); }
struct KnownContext {
  bool parent_context = false;
  Status (*before_horizontal)(Image&, void*) = nullptr;
  void* opaque = nullptr;
  size_t* logical_values = nullptr;
};
// Keep reference slots identical in both arms, including disabled parent slots.
inline void KnownReferences(const Channel& ch, size_t y, const Image& image,
                            uint32_t i, Channel* refs, const KnownContext& known) {
  ZeroFillImage(&refs->plane);
  size_t offset = 0;
  for (int32_t j = static_cast<int32_t>(i)-1; j >= 0 && offset < refs->w; --j) {
    const auto& r = image.channel[j];
    if (r.w != ch.w || r.h != ch.h || r.hshift != ch.hshift || r.vshift != ch.vshift) continue;
    // Explicitly advance even when parent properties are gated off.
    if (known.parent_context || !IsParent(size_t(j))) {
      auto* dst = refs->Row(0)+offset;
      const auto* row = r.Row(y); const auto* prev = r.Row(y ? y-1 : 0);
      for (size_t x = 0; x < ch.w; ++x, dst += refs->plane.PixelsPerRow()) {
        const pixel_type_w value = row[x], left = x ? row[x-1] : 0;
        const pixel_type_w top = y ? prev[x] : left;
        const pixel_type_w corner = x && y ? prev[x-1] : left;
        const pixel_type_w residual = value-ClampedGradient(left, top, corner);
        dst[0]=std::abs(value); dst[1]=value;
        dst[2]=std::abs(residual); dst[3]=residual;
      }
    }
    offset += kExtraPropsPerChannel;
  }
}
StatusOr<Tree> KnownLearnTree(const Image*, const ModularOptions*, uint32_t,
                            uint32_t, const KnownContext&,
                            const std::vector<ModularMultiplierInfo>&);
Status KnownGenericCompress(const Image&, const ModularOptions&, BitWriter&,
                            AuxOut*, LayerType, size_t, const Tree*, const KnownContext&);
Status KnownGenericDecompress(BitReader*, Image&, GroupHeader*, size_t,
                              ModularOptions*, const KnownContext&);
}}  // namespace jxl::st091
#endif
