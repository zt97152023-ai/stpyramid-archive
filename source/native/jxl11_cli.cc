// Small RGB8-only research baseline. Native libjxl 0.12.0; no parallel runner.
// This wrapper is new handoff code, not a previously measured whole-slide result.
#include "portable/platform.h"
#include <jxl/color_encoding.h>
#include <jxl/decode.h>
#include <jxl/encode.h>
#include <fstream>
#include <iostream>
#include <memory>
#include <string>
#include <vector>

namespace fs = std::filesystem;
using Bytes = std::vector<uint8_t>;
using stportable::Require;
constexpr size_t kMaxBytes = 64u * 1024u * 1024u;

Bytes Read(const fs::path& p) {
  std::ifstream f(p, std::ios::binary | std::ios::ate);
  Require(bool(f), "cannot open input");
  const auto n = f.tellg();
  Require(n >= 0 && uint64_t(n) <= kMaxBytes, "input exceeds 64 MiB bound");
  Bytes b(static_cast<size_t>(n));
  f.seekg(0);
  f.read(reinterpret_cast<char*>(b.data()), std::streamsize(b.size()));
  Require(bool(f), "cannot read input");
  return b;
}

uint32_t Dimension(const char* text) {
  const std::string s(text);
  Require(!s.empty() && s.find_first_not_of("0123456789") == std::string::npos,
          "invalid dimension");
  size_t end = 0;
  const uint64_t v = std::stoull(s, &end);
  Require(end == s.size() && v > 0 && v <= 4096, "dimension must be 1..4096");
  return static_cast<uint32_t>(v);
}

Bytes Encode(const Bytes& raw, uint32_t w, uint32_t h) {
  Require(raw.size() == size_t(w) * h * 3, "RGB8 byte count mismatch");
  std::unique_ptr<JxlEncoder, decltype(&JxlEncoderDestroy)> enc(
      JxlEncoderCreate(nullptr), &JxlEncoderDestroy);
  Require(bool(enc), "encoder creation failed");
  JxlEncoderAllowExpertOptions(enc.get());
  JxlBasicInfo info;
  JxlEncoderInitBasicInfo(&info);
  info.xsize = w; info.ysize = h;
  info.bits_per_sample = 8; info.exponent_bits_per_sample = 0;
  info.num_color_channels = 3; info.num_extra_channels = 0;
  info.uses_original_profile = JXL_TRUE;
  Require(JxlEncoderSetBasicInfo(enc.get(), &info) == JXL_ENC_SUCCESS, "basic info");
  JxlColorEncoding color;
  JxlColorEncodingSetToSRGB(&color, JXL_FALSE);
  Require(JxlEncoderSetColorEncoding(enc.get(), &color) == JXL_ENC_SUCCESS, "sRGB");
  auto* frame = JxlEncoderFrameSettingsCreate(enc.get(), nullptr);
  Require(frame != nullptr, "frame settings");
  Require(JxlEncoderSetFrameLossless(frame, JXL_TRUE) == JXL_ENC_SUCCESS, "lossless");
  Require(JxlEncoderSetFrameDistance(frame, 0.0f) == JXL_ENC_SUCCESS, "distance zero");
  Require(JxlEncoderFrameSettingsSetOption(frame, JXL_ENC_FRAME_SETTING_EFFORT, 11)
              == JXL_ENC_SUCCESS, "effort 11");
  const JxlPixelFormat format{3, JXL_TYPE_UINT8, JXL_NATIVE_ENDIAN, 0};
  Require(JxlEncoderAddImageFrame(frame, &format, raw.data(), raw.size())
              == JXL_ENC_SUCCESS, "add image");
  JxlEncoderCloseInput(enc.get());
  Bytes out(4096);
  size_t used = 0;
  while (true) {
    uint8_t* next = out.data() + used;
    size_t available = out.size() - used;
    const auto status = JxlEncoderProcessOutput(enc.get(), &next, &available);
    used = out.size() - available;
    if (status == JXL_ENC_SUCCESS) break;
    Require(status == JXL_ENC_NEED_MORE_OUTPUT, "encoder failed");
    Require(out.size() < kMaxBytes, "output exceeds 64 MiB bound");
    out.resize(std::min(kMaxBytes, out.size() * 2));
  }
  out.resize(used);
  return out;
}

Bytes Decode(const Bytes& stream) {
  std::unique_ptr<JxlDecoder, decltype(&JxlDecoderDestroy)> dec(
      JxlDecoderCreate(nullptr), &JxlDecoderDestroy);
  Require(bool(dec), "decoder creation failed");
  Require(JxlDecoderSetKeepOrientation(dec.get(), JXL_TRUE) == JXL_DEC_SUCCESS,
          "keep orientation");
  Require(JxlDecoderSubscribeEvents(dec.get(), JXL_DEC_BASIC_INFO | JXL_DEC_FULL_IMAGE)
              == JXL_DEC_SUCCESS, "subscribe events");
  Require(JxlDecoderSetInput(dec.get(), stream.data(), stream.size()) == JXL_DEC_SUCCESS,
          "decoder input");
  JxlDecoderCloseInput(dec.get());
  const JxlPixelFormat format{3, JXL_TYPE_UINT8, JXL_NATIVE_ENDIAN, 0};
  Bytes out;
  bool basic = false;
  unsigned frames = 0;
  size_t expected = 0;
  while (true) {
    const auto status = JxlDecoderProcessInput(dec.get());
    if (status == JXL_DEC_BASIC_INFO) {
      JxlBasicInfo info;
      Require(JxlDecoderGetBasicInfo(dec.get(), &info) == JXL_DEC_SUCCESS, "basic info");
      Require(!basic && info.bits_per_sample == 8 && info.exponent_bits_per_sample == 0
              && info.num_color_channels == 3 && info.num_extra_channels == 0
              && info.xsize > 0 && info.xsize <= 4096 && info.ysize > 0 && info.ysize <= 4096
              && !info.have_animation && info.orientation == JXL_ORIENT_IDENTITY,
              "only one orientation-1 RGB8 frame supported");
      expected = size_t(info.xsize) * info.ysize * 3;
      basic = true;
    } else if (status == JXL_DEC_NEED_IMAGE_OUT_BUFFER) {
      size_t n = 0;
      Require(basic && frames == 0 && out.empty(), "unexpected image buffer");
      Require(JxlDecoderImageOutBufferSize(dec.get(), &format, &n) == JXL_DEC_SUCCESS
                  && n == expected && n <= kMaxBytes, "output size");
      out.resize(n);
      Require(JxlDecoderSetImageOutBuffer(dec.get(), &format, out.data(), n)
                  == JXL_DEC_SUCCESS, "image buffer");
    } else if (status == JXL_DEC_FULL_IMAGE) {
      ++frames;
    } else if (status == JXL_DEC_SUCCESS) {
      Require(basic && frames == 1 && out.size() == expected, "incomplete image");
      Require(JxlDecoderReleaseInput(dec.get()) == 0, "unexpected trailing input");
      return out;
    } else {
      throw std::runtime_error("decoder failed or incomplete stream");
    }
  }
}

int main(int argc, char** argv) {
  try {
    // Bundled research libjxl has opt-in ST074 instrumentation/experiments.
    // The direct baseline always uses the ordinary upstream path.
    stportable::UnsetEnv("ST074_S");
    stportable::UnsetEnv("ST074_T");
    if (argc == 6 && std::string(argv[1]) == "encode") {
      const auto result = Encode(Read(argv[2]), Dimension(argv[3]), Dimension(argv[4]));
      stportable::WriteNew(argv[5], result.data(), result.size());
    } else if (argc == 4 && std::string(argv[1]) == "decode") {
      const auto result = Decode(Read(argv[2]));
      stportable::WriteNew(argv[3], result.data(), result.size());
    } else {
      std::cerr << "jxl11_cli encode input.rgb width height new.jxl\n"
                   "jxl11_cli decode input.jxl new.rgb\n";
      return 2;
    }
    return 0;
  } catch (const std::exception& e) {
    std::cerr << e.what() << '\n';
    return 1;
  }
}
