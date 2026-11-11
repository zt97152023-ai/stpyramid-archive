// Copyright (c) the JPEG XL Project Authors. All rights reserved.
// Use of this adapted source is governed by the BSD license in LICENSE.
// WSI-RD-HF-L1. Isolated dense magnitude/sign adaptation of qualified091.
// Known channels are decoder-derived context, never symbols in this stream.
#ifndef STHF1_KNOWN_CONTEXT_H_
#define STHF1_KNOWN_CONTEXT_H_
#include "lib/jxl/modular/encoding/context_predict.h"
#include "lib/jxl/modular/encoding/enc_encoding.h"
#include "lib/jxl/modular/encoding/encoding.h"
#include "lib/jxl/image_ops.h"
namespace jxl { namespace sthf1 {
inline bool IsTarget(size_t c) { return c < 18 && c % 9 >= 3; }
inline bool IsParent(size_t c) { return c < 18 && c % 9 < 3; }
inline bool IsSign(size_t c) { return IsTarget(c) && ((c % 9 - 3) & 1); }
inline size_t Stage(size_t c) { return c < 9 ? 0 : 9; }
inline size_t Color(size_t c) { return (c % 9 - 3) / 2; }
inline pixel_type_w SignedValue(const Image& image, size_t stage, size_t color, size_t x, size_t y) {
  const pixel_type_w m = image.channel[stage+3+2*color].Row(y)[x];
  const pixel_type_w s = image.channel[stage+4+2*color].Row(y)[x];
  return s ? -m : m;
}
// Three exact parent slots, followed only by decoder-visible target values.
inline pixel_type_w ReferenceValue(const Image& image, size_t c, size_t slot, size_t x, size_t y) {
  const size_t stage = Stage(c), color = Color(c);
  if (slot < 3) return image.channel[stage+slot].Row(y)[x];
  if (IsSign(c)) {
    if (slot == 3) return image.channel[c-1].Row(y)[x];
    return color ? SignedValue(image, stage, color-1, x, y) : 0;
  }
  const size_t distance = slot-2;
  return color >= distance ? SignedValue(image, stage, color-distance, x, y) : 0;
}
struct KnownContext {
  bool parent_context = false;
  Status (*before_horizontal)(Image&, void*) = nullptr;
  void* opaque = nullptr;
  size_t* logical_values = nullptr;
  Status (*after_channel)(Image&, size_t, void*) = nullptr;
};
// Keep reference slots identical in both arms, including disabled parent slots.
inline void KnownReferences(const Channel& ch, size_t y, const Image& image,
                            uint32_t i, Channel* refs, const KnownContext& known) {
  ZeroFillImage(&refs->plane);
  for (size_t slot=0; slot<5 && slot*4+3<refs->w; ++slot) {
    if (known.parent_context || slot >= 3) {
      auto* dst = refs->Row(0)+slot*4;
      for (size_t x = 0; x < ch.w; ++x, dst += refs->plane.PixelsPerRow()) {
        const pixel_type_w value = ReferenceValue(image,i,slot,x,y);
        const pixel_type_w left = x ? ReferenceValue(image,i,slot,x-1,y) : 0;
        const pixel_type_w top = y ? ReferenceValue(image,i,slot,x,y-1) : left;
        const pixel_type_w corner = x && y ? ReferenceValue(image,i,slot,x-1,y-1) : left;
        const pixel_type_w residual = value-ClampedGradient(left, top, corner);
        dst[0]=std::abs(value); dst[1]=value;
        dst[2]=std::abs(residual); dst[3]=residual;
      }
    }
  }
}
StatusOr<Tree> KnownLearnTree(const Image*, const ModularOptions*, uint32_t,
                            uint32_t, const KnownContext&,
                            const std::vector<ModularMultiplierInfo>&);
Status KnownGenericCompress(const Image&, const ModularOptions&, BitWriter&,
                            AuxOut*, LayerType, size_t, const Tree*, const KnownContext&);
Status KnownGenericDecompress(BitReader*, Image&, GroupHeader*, size_t,
                              ModularOptions*, const KnownContext&);
}}  // namespace jxl::sthf1
#endif
