#include "platform.h"
#include <iomanip>
#include <iostream>
#include <sstream>
#include <string>

static std::string Hex(const stportable::Digest& digest) {
  std::ostringstream out;
  out << std::hex << std::setfill('0');
  for (uint8_t v : digest) out << std::setw(2) << unsigned(v);
  return out.str();
}

int main() {
  try {
    struct Case { const char* input; const char* expected; };
    const Case cases[] = {
      {"", "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"},
      {"abc", "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"},
      {"abcdbcdecdefdefgefghfghighijhijkijkljklmklmnlmnomnopnopq",
       "248d6a61d20638b8e5c026930c3e6039a33ce45964ff2167f6ecedd419db06c1"}
    };
    for (const auto& c : cases) {
      const std::string input(c.input);
      stportable::Require(
        Hex(stportable::Sha256(reinterpret_cast<const uint8_t*>(input.data()), input.size())) == c.expected,
        "SHA256 known-answer mismatch");
    }
    std::cout << "{\"ok\":true,\"sha256_known_answer_cases\":3,\"codec_roundtrip_tested\":false}\n";
    return 0;
  } catch (const std::exception& e) {
    std::cerr << e.what() << '\n';
    return 1;
  }
}
