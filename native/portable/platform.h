// Platform adapters only. Codec mathematics and wire formats live elsewhere.
#ifndef ST_HF_L1_PORTABLE_PLATFORM_H_
#define ST_HF_L1_PORTABLE_PLATFORM_H_

#include <algorithm>
#include <array>
#include <cerrno>
#include <cstddef>
#include <cstdint>
#include <cstdlib>
#include <filesystem>
#include <stdexcept>

#if defined(_WIN32)
#ifndef NOMINMAX
#define NOMINMAX
#endif
#include <windows.h>
#include <bcrypt.h>
#define ST_NATIVE_EXPORT __declspec(dllexport)
#elif defined(__APPLE__)
#include <CommonCrypto/CommonDigest.h>
#include <fcntl.h>
#include <unistd.h>
#define ST_NATIVE_EXPORT __attribute__((visibility("default")))
#else
#error "This handoff platform layer currently supports macOS and Windows only."
#endif

namespace stportable {
using Digest = std::array<uint8_t, 32>;
inline void Require(bool value, const char* message) {
  if (!value) throw std::runtime_error(message);
}

inline Digest Sha256(const uint8_t* data, size_t size) {
  Require(data != nullptr || size == 0, "null SHA256 input");
  Digest out{};
#if defined(_WIN32)
  struct Handles {
    BCRYPT_ALG_HANDLE alg = nullptr;
    BCRYPT_HASH_HANDLE hash = nullptr;
    ~Handles() {
      if (hash) BCryptDestroyHash(hash);
      if (alg) BCryptCloseAlgorithmProvider(alg, 0);
    }
  } h;
  Require(BCryptOpenAlgorithmProvider(&h.alg, BCRYPT_SHA256_ALGORITHM, nullptr, 0) >= 0,
          "SHA256 provider");
  Require(BCryptCreateHash(h.alg, &h.hash, nullptr, 0, nullptr, 0, 0) >= 0,
          "SHA256 state");
  for (size_t pos = 0; pos < size;) {
    const ULONG n = static_cast<ULONG>(std::min<size_t>(size-pos, 1u << 30));
    Require(BCryptHashData(h.hash, const_cast<PUCHAR>(data+pos), n, 0) >= 0,
            "SHA256 update");
    pos += n;
  }
  Require(BCryptFinishHash(h.hash, out.data(), ULONG(out.size()), 0) >= 0,
          "SHA256 finish");
#else
  CC_SHA256_CTX ctx;
  Require(CC_SHA256_Init(&ctx) == 1, "SHA256 state");
  for (size_t pos = 0; pos < size;) {
    const CC_LONG n = static_cast<CC_LONG>(std::min<size_t>(size-pos, 1u << 30));
    Require(CC_SHA256_Update(&ctx, data+pos, n) == 1, "SHA256 update");
    pos += n;
  }
  Require(CC_SHA256_Final(out.data(), &ctx) == 1, "SHA256 finish");
#endif
  return out;
}

// Create-only output. Partial files survive errors for diagnosis.
inline void WriteNew(const std::filesystem::path& path, const uint8_t* data,
                     size_t size) {
  Require(data != nullptr || size == 0, "null output buffer");
#if defined(_WIN32)
  HANDLE f = CreateFileW(path.c_str(), GENERIC_WRITE, 0, nullptr, CREATE_NEW,
                         FILE_ATTRIBUTE_NORMAL, nullptr);
  Require(f != INVALID_HANDLE_VALUE, "output exists or cannot create output");
  bool ok = true;
  for (size_t pos = 0; pos < size && ok;) {
    const DWORD n = DWORD(std::min<size_t>(size-pos, 1u << 30));
    DWORD written = 0;
    ok = WriteFile(f, data+pos, n, &written, nullptr) != 0 && written == n;
    pos += written;
  }
  ok = CloseHandle(f) != 0 && ok;
  Require(ok, "write failed; partial file preserved");
#else
  int flags = O_WRONLY | O_CREAT | O_EXCL;
#ifdef O_CLOEXEC
  flags |= O_CLOEXEC;
#endif
#ifdef O_NOFOLLOW
  flags |= O_NOFOLLOW;
#endif
  const int fd = ::open(path.c_str(), flags, 0600);
  Require(fd >= 0, "output exists or cannot create output");
  bool ok = true;
  for (size_t pos = 0; pos < size;) {
    const size_t n = std::min<size_t>(size-pos, 1u << 30);
    const ssize_t written = ::write(fd, data+pos, n);
    if (written < 0 && errno == EINTR) continue;
    if (written <= 0) { ok = false; break; }
    pos += size_t(written);
  }
  // Never retry close after an error: the descriptor may have been released.
  if (::close(fd) != 0) ok = false;
  Require(ok, "write failed; partial file preserved");
#endif
}

inline void UnsetEnv(const char* name) {
#if defined(_WIN32)
  Require(_putenv_s(name, "") == 0, "cannot clear codec environment variable");
#else
  Require(::unsetenv(name) == 0, "cannot clear codec environment variable");
#endif
}
}  // namespace stportable
#endif
