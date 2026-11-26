// STBASELINE-089: native-pyramid Squeeze lowpass reuse, NOT a .jxl file.
// Reuses immutable libjxl 0.12.0/074. Each tile has independent signed streams.
// Ordinary LL and external-anchor correction share the exact detail bytes.
#define NOMINMAX
#include "platform.h"
#include <algorithm>
#include <array>
#include <cstddef>
#include <cstdint>
#include <cstdlib>
#include <cstring>
#include <filesystem>
#include <fstream>
#include <iomanip>
#include <iostream>
#include <limits>
#include <map>
#include <memory>
#include <mutex>
#include <set>
#include <sstream>
#include <stdexcept>
#include <string>
#include <tuple>
#include <vector>
#include "lib/jxl/dec_ans.h"
#include "lib/jxl/enc_ans.h"
#include "lib/jxl/enc_aux_out.h"
#include "lib/jxl/enc_lz77.h"
#include "lib/jxl/fields.h"
#include "lib/jxl/memory_manager_internal.h"
#include "lib/jxl/modular/encoding/enc_encoding.h"
#include "lib/jxl/modular/encoding/enc_ma.h"
#include "lib/jxl/modular/encoding/encoding.h"
#include "lib/jxl/modular/encoding/ma_common.h"
#include "lib/jxl/modular/transform/enc_squeeze.h"
#include "lib/jxl/modular/transform/squeeze.h"

namespace {
namespace fs = std::filesystem;
using Bytes = std::vector<uint8_t>;
using Digest = std::array<uint8_t, 32>;
constexpr uint32_t kMaxGroups = 1000000, kTile = 256;
constexpr size_t kMaxNodes = 4095, kMaxStreamBytes = 2 * 1024 * 1024;
constexpr size_t kMaxRecordBytes = 2 * kMaxStreamBytes + 196;
constexpr int32_t kDetailLimit = 2048;
constexpr uint64_t kAllocatorLimit = 256ull * 1024 * 1024;
constexpr auto kLayer = jxl::LayerType::ModularGlobal;
struct View {
  const uint8_t* p; size_t n;
  const uint8_t* data() const { return p; }
  size_t size() const { return n; }
};
View AsView(const Bytes& b) { return {b.data(), b.size()}; }
void Need(bool ok, const char* message) { if (!ok) throw std::runtime_error(message); }
void Check(jxl::Status s, const char* message) { Need(bool(s), message); }
template<class T> T Take(jxl::StatusOr<T> value, const char* message) {
  Need(value.ok(), message); return std::move(value).value_();
}

// Caps allocations owned by libjxl, including our allocation header. C++ vectors
// and other process memory remain subject to the separate job RSS limit.
struct Memory {
  struct alignas(std::max_align_t) Header { size_t size; };
  JxlMemoryManager mm{};
  uint64_t live = 0;
  static void* Alloc(void* opaque, size_t size) {
    auto* self = static_cast<Memory*>(opaque);
    if (size > kAllocatorLimit - sizeof(Header) || size > SIZE_MAX - sizeof(Header) ||
        self->live > kAllocatorLimit - size - sizeof(Header)) return nullptr;
    auto* h = static_cast<Header*>(std::malloc(sizeof(Header) + size));
    if (!h) return nullptr;
    h->size = sizeof(Header) + size; self->live += h->size; return h + 1;
  }
  static void Free(void* opaque, void* address) {
    if (!address) return;
    auto* self = static_cast<Memory*>(opaque);
    auto* h = static_cast<Header*>(address) - 1;
    self->live -= h->size; std::free(h);
  }
  Memory() {
    JxlMemoryManager custom{this, Alloc, Free};
    Check(jxl::MemoryManagerInit(&mm, &custom), "memory manager");
  }
  Memory(const Memory&) = delete;
  Memory& operator=(const Memory&) = delete;
};

Digest Hash(View b) { return stportable::Sha256(b.data(), b.size()); }
std::string Hex(const Digest& d) {
  std::ostringstream out; out << std::hex << std::setfill('0');
  for (uint8_t v : d) out << std::setw(2) << unsigned(v); return out.str();
}
void Put32(Bytes& b, uint32_t v) { for (int i=0;i<4;++i) b.push_back(uint8_t(v >> (8*i))); }
void Put64(Bytes& b, uint64_t v) { for (int i=0;i<8;++i) b.push_back(uint8_t(v >> (8*i))); }
void PutDigest(Bytes& b, const Digest& d) { b.insert(b.end(), d.begin(), d.end()); }
struct Cursor {
  View v; size_t pos = 0;
  View Read(size_t n) { Need(pos <= v.size() && n <= v.size()-pos, "truncated bank"); View r{v.data()+pos,n}; pos += n; return r; }
  uint32_t U32() { auto b=Read(4); uint32_t x=0; for(int i=0;i<4;++i)x|=uint32_t(b.data()[i])<<(8*i); return x; }
  uint64_t U64() { auto b=Read(8); uint64_t x=0; for(int i=0;i<8;++i)x|=uint64_t(b.data()[i])<<(8*i); return x; }
  Digest D() { Digest d{}; auto b=Read(32); std::copy(b.data(),b.data()+32,d.begin()); return d; }
  void Magic(const char* s) { auto b=Read(8); Need(std::memcmp(b.data(),s,8)==0,"wrong bank magic/version"); }
};
Bytes ReadFile(const fs::path& path, size_t limit) {
  Need(!fs::is_symlink(path) && fs::is_regular_file(path),"input must be a regular non-symlink file");
  auto n=fs::file_size(path); Need(n>0 && n<=limit,"input file size outside limit");
  std::ifstream in(path,std::ios::binary); Need(bool(in),"cannot open input"); Bytes b(size_t(n),0);
  in.read(reinterpret_cast<char*>(b.data()),std::streamsize(n));
  Need(bool(in) && in.peek()==std::char_traits<char>::eof(),"input changed or read failed"); return b;
}
void WriteNew(const fs::path& path, View b) {
  stportable::WriteNew(path, b.data(), b.size());
}
void WriteText(const fs::path& p,const std::string& s) { WriteNew(p,{reinterpret_cast<const uint8_t*>(s.data()),s.size()}); }
uint32_t Number(const std::string& s) {
  Need(!s.empty() && s.size()<=10 && std::all_of(s.begin(),s.end(),[](unsigned char c){return c>='0'&&c<='9';}),"invalid decimal integer");
  auto n=std::stoull(s); Need(n<=UINT32_MAX,"integer overflow"); return uint32_t(n);
}
void Dimensions(uint32_t w,uint32_t h) { Need(w>0 && w<=kTile && h>0 && h<=kTile,"dimensions outside 1..256"); }
std::string Name(const char* prefix,uint32_t id) {
  std::ostringstream s; s<<prefix<<std::setfill('0')<<std::setw(6)<<id<<".bin"; return s.str();
}
Bytes Finish(jxl::BitWriter& writer) {
  Check(writer.WithMaxBits(8,kLayer,nullptr,[&]()->jxl::Status{writer.ZeroPadToByte();return true;}),"padding");
  auto s=writer.GetSpan(); return Bytes(s.data(),s.data()+s.size());
}
struct Reader {
  jxl::BitReader br; bool closed=false;
  explicit Reader(View v):br(v){}
  ~Reader(){if(!closed){auto s=br.Close();(void)s;}}
  void End(uint64_t bits) {
    Need(br.TotalBitsConsumed()==bits,"payload bit consumption mismatch"); Check(br.AllReadsWithinBounds(),"bitstream overread");
    auto s=br.Close();closed=true;Check(s,"bitreader close");
  }
};

[[noreturn]] void TreeError(uint32_t region,const char* stage,size_t nodes,const char* reason) {
  std::ostringstream message;message<<"tree "<<stage<<" region="<<region<<" nodes="<<nodes
    <<" bound="<<kMaxNodes<<" reason="<<reason;throw std::runtime_error(message.str());
}
void ValidateTree(const jxl::Tree& tree,uint32_t region,const char* stage,size_t limit) {
  const size_t n=tree.size();
  if(n==0)TreeError(region,stage,n,"empty");
  if(n%2==0)TreeError(region,stage,n,"even-node-count");
  if(n>limit)TreeError(region,stage,n,"node-limit");
  // A size-only check cannot make malformed child links safe for TokenizeTree.
  std::vector<uint8_t> seen(n,0);std::vector<uint32_t> pending{0};size_t visited=0;
  while(!pending.empty()){
    uint32_t id=pending.back();pending.pop_back();
    if(id>=n||seen[id])TreeError(region,stage,n,"invalid-repeated-or-cyclic-child");
    seen[id]=1;++visited;const auto& node=tree[id];
    if(node.property < -1 || node.property==1)TreeError(region,stage,n,"invalid-or-group-id-property");
    if(node.predictor>=jxl::Predictor::Best||node.multiplier==0)TreeError(region,stage,n,"invalid-fallback-predictor");
    if(node.property!=-1){pending.push_back(node.rchild);pending.push_back(node.lchild);}
  }
  if(visited!=n)TreeError(region,stage,n,"unreachable-nodes");
}
struct TreeBudget {
  size_t learned_nodes,emitted_nodes,pruned_splits,collapsed_subtrees;
};
TreeBudget BoundTree(jxl::Tree* tree,uint32_t region) {
  // The pinned native learner has no 4095-node ceiling. Its MakeSplitNode
  // (enc_ma.cc:81) preserves each parent's predictor/offset/multiplier when it
  // adds children. Before canonicalization those fields are the legitimate
  // pre-split predictor. TokenizeTree later replaces internal predictor fields,
  // so this deterministic pruning MUST occur before TokenizeTree.
  ValidateTree(*tree,region,"learned",jxl::kMaxTreeSize);
  TreeBudget result{tree->size(),tree->size(),0,0};
  if(tree->size()<=kMaxNodes)return result;
  jxl::Tree bounded;bounded.reserve(kMaxNodes);
  std::vector<uint32_t> source_ids;source_ids.reserve(kMaxNodes);
  auto leaf=[](const jxl::PropertyDecisionNode& n){return jxl::PropertyDecisionNode::Leaf(n.predictor,n.predictor_offset,n.multiplier);};
  bounded.push_back(leaf((*tree)[0]));source_ids.push_back(0);
  // Breadth-first budget: keep shallow splits in fixed left/right order. A
  // split costs exactly two additional nodes. Never relearn or tune a threshold.
  for(size_t pos=0;pos<source_ids.size();++pos){const auto& old=(*tree)[source_ids[pos]];
    if(old.property==-1)continue;
    if(bounded.size()+2>kMaxNodes){++result.collapsed_subtrees;continue;}
    auto split=old;split.lchild=uint32_t(bounded.size());split.rchild=uint32_t(bounded.size()+1);bounded[pos]=split;
    bounded.push_back(leaf((*tree)[old.lchild]));source_ids.push_back(old.lchild);
    bounded.push_back(leaf((*tree)[old.rchild]));source_ids.push_back(old.rchild);
  }
  result.emitted_nodes=bounded.size();result.pruned_splits=(result.learned_nodes-result.emitted_nodes)/2;
  ValidateTree(bounded,region,"bounded",kMaxNodes);*tree=std::move(bounded);return result;
}

bool SameTree(const jxl::Tree& a,const jxl::Tree& b){
  if(a.size()!=b.size())return false;
  for(size_t i=0;i<a.size();++i){const auto& x=a[i];const auto& y=b[i];
    if(std::tie(x.property,x.splitval,x.lchild,x.rchild,x.predictor,x.predictor_offset,x.multiplier)!=
       std::tie(y.property,y.splitval,y.lchild,y.rchild,y.predictor,y.predictor_offset,y.multiplier))return false;}
  return true;
}
void TreeSelfcheck(){
  auto balanced=[](size_t nodes){jxl::Tree tree(nodes);
    for(size_t i=0;i<nodes;++i){auto& n=tree[i];n=jxl::PropertyDecisionNode::Leaf(i%2?jxl::Predictor::Weighted:jxl::Predictor::Gradient,int64_t(i%7)-3,1);
      if(i<nodes/2){n.property=0;n.splitval=int32_t(i%2);n.lchild=uint32_t(2*i+1);n.rchild=uint32_t(2*i+2);}}
    return tree;};
  auto chain=[](size_t nodes){jxl::Tree tree(1,jxl::PropertyDecisionNode::Leaf(jxl::Predictor::Gradient));size_t current=0;
    while(tree.size()<nodes){auto& n=tree[current];n.property=0;n.splitval=1;n.lchild=uint32_t(tree.size());n.rchild=uint32_t(tree.size()+1);
      current=n.rchild;tree.push_back(jxl::PropertyDecisionNode::Leaf(jxl::Predictor::Weighted,1,1));tree.push_back(jxl::PropertyDecisionNode::Leaf(jxl::Predictor::Gradient,-1,1));}
    return tree;};
  std::vector<std::pair<std::string,jxl::Tree>> cases;
  cases.emplace_back("within",balanced(7));cases.emplace_back("oversized_balanced",balanced(8191));cases.emplace_back("oversized_chain",chain(8191));
  std::ostringstream out;out<<"{\"ok\":true,\"bound\":"<<kMaxNodes<<",\"cases\":[";
  for(size_t i=0;i<cases.size();++i){auto original=cases[i].second;auto stats=BoundTree(&cases[i].second,0);const auto& emitted=cases[i].second;
    Need(stats.emitted_nodes==(i==0?7:kMaxNodes)&&stats.pruned_splits==(i==0?0:2048),"tree selfcheck budget failed");
    Need((i==0)==SameTree(original,emitted),"tree selfcheck unchanged rule failed");
    // These two constructed trees are already in BFS order. Every collapsed
    // frontier leaf must retain its source parent's predictive parameters.
    for(size_t j=0;j<emitted.size();++j)if(original[j].property>=0&&emitted[j].property==-1)
      Need(emitted[j].predictor==original[j].predictor&&emitted[j].predictor_offset==original[j].predictor_offset&&emitted[j].multiplier==original[j].multiplier,"tree selfcheck fallback predictor changed");
    std::vector<jxl::Token> tokens;jxl::Tree canonical;Check(jxl::TokenizeTree(emitted,&tokens,&canonical),"tree selfcheck canonicalization");
    Need(canonical.size()==emitted.size(),"tree selfcheck canonical node count changed");ValidateTree(canonical,0,"selfcheck-canonical",kMaxNodes);
    if(i)out<<',';out<<"{\"name\":\""<<cases[i].first<<"\",\"learned_nodes\":"<<stats.learned_nodes<<",\"emitted_nodes\":"<<stats.emitted_nodes
      <<",\"pruned_splits\":"<<stats.pruned_splits<<",\"collapsed_subtrees\":"<<stats.collapsed_subtrees<<",\"unchanged\":"<<(i==0?"true":"false")<<"}";
  }
  auto reject=[](jxl::Tree tree,const char* reason){try{(void)BoundTree(&tree,0);}catch(const std::runtime_error& e){
      Need(std::string(e.what()).find(reason)!=std::string::npos&&std::string(e.what()).find("nodes=")!=std::string::npos,"tree selfcheck rejection diagnostic");return;}
    throw std::runtime_error("tree selfcheck accepted malformed tree");};
  reject({},"empty");reject(jxl::Tree(2),"even-node-count");auto cycle=balanced(3);cycle[0].lchild=0;reject(std::move(cycle),"cyclic-child");
  out<<"],\"malformed_rejected\":[\"empty\",\"even\",\"cycle\"]}\n";std::cout<<out.str();
}

enum class StreamKind { Detail, Lowpass, Correction };
struct Tile {
  uint32_t id, level, x, y, w, h;
  std::string rgb_name, anchor_name;
};
bool SafeName(const std::string& s) {
  return !s.empty() && s != "." && s != ".." &&
    s.find_first_not_of("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._-") == std::string::npos;
}
std::vector<Tile> ReadIndex(const fs::path& spool) {
  auto bytes = ReadFile(spool / "index.tsv", 128 * 1024 * 1024);
  std::istringstream input(std::string(bytes.begin(), bytes.end()));
  std::string line; std::vector<Tile> result;
  std::set<std::pair<uint32_t, uint32_t>> positions;
  while (std::getline(input, line)) {
    if (!line.empty() && line.back() == '\r') line.pop_back();
    Need(!line.empty() && line.size() < 1024 && line.back() != '\t', "invalid spool row");
    std::istringstream row(line); std::vector<std::string> fields; std::string field;
    while (std::getline(row, field, '\t')) fields.push_back(field);
    Need(fields.size() == 8, "index needs gid level x y w h rgb_filename anchor_filename");
    Need(result.size() < kMaxGroups, "too many tiles");
    Tile t{Number(fields[0]), Number(fields[1]), Number(fields[2]), Number(fields[3]),
           Number(fields[4]), Number(fields[5]), fields[6], fields[7]};
    Dimensions(t.w, t.h);
    Need(t.id == result.size() && t.level == 0 && t.x % kTile == 0 && t.y % kTile == 0,
         "invalid L0 ID/level/tile origin");
    Need(SafeName(t.rgb_name) && SafeName(t.anchor_name), "unsafe spool filename");
    Need(positions.emplace(t.x, t.y).second, "duplicate tile origin");
    result.push_back(std::move(t));
  }
  Need(!result.empty(), "empty spool"); return result;
}
std::vector<jxl::SqueezeParams> Squeezes() {
  std::vector<jxl::SqueezeParams> params(2);
  for (auto& p : params) { p.in_place = false; p.begin_c = 0; p.num_c = 3; }
  params[0].horizontal = true; params[1].horizontal = false; return params;
}
jxl::Image ToColor(JxlMemoryManager* mm, View rgb, uint32_t w, uint32_t h) {
  Need(rgb.size() == size_t(w) * h * 3, "wrong RGB input size");
  auto image = Take(jxl::Image::Create(mm, w, h, 9, 3), "create color image");
  for (size_t y = 0; y < h; ++y) for (size_t x = 0; x < w; ++x) {
    const size_t i = (y * w + x) * 3; const int32_t g = rgb.data()[i + 1];
    image.channel[0].Row(y)[x] = g;
    image.channel[1].Row(y)[x] = int32_t(rgb.data()[i]) - g;
    image.channel[2].Row(y)[x] = int32_t(rgb.data()[i + 2]) - g;
  }
  return image;
}
jxl::Image Shape(JxlMemoryManager* mm, uint32_t w, uint32_t h, StreamKind kind) {
  // Native signed int32 coefficients: fixed codec bitdepth is a heuristic,
  // never a uint16 RGB carrier or permission to clip the coefficients.
  auto image = Take(jxl::Image::Create(mm, w, h, kind == StreamKind::Detail ? 12 : 10, 0), "create stream shape");
  const uint32_t aw = (w + 1) / 2, ah = (h + 1) / 2;
  const size_t count = kind == StreamKind::Detail ? 6 : 3;
  for (size_t c = 0; c < count; ++c) {
    const bool horizontal = kind == StreamKind::Detail && c < 3;
    const uint32_t cw = horizontal ? w / 2 : aw;
    const uint32_t ch = kind != StreamKind::Detail ? ah : (horizontal ? h : h / 2);
    auto channel = Take(jxl::Channel::Create(mm, cw, ch, 1, horizontal ? 0 : 1), "create coefficient channel");
    channel.component = int(c % 3); image.channel.push_back(std::move(channel));
  }
  return image;
}
size_t CoefficientCount(const jxl::Image& image) {
  size_t count = 0; for (const auto& ch : image.channel) count += ch.w * ch.h; return count;
}
void CheckShape(const jxl::Image& image, uint32_t w, uint32_t h, StreamKind kind) {
  Need(image.w == w && image.h == h && image.nb_meta_channels == 0 && image.transform.empty() && !image.error,
       "invalid coefficient image profile");
  const size_t count = kind == StreamKind::Detail ? 6 : 3;
  Need(image.channel.size() == count, "wrong coefficient channel count");
  for (size_t c = 0; c < count; ++c) {
    const auto& ch = image.channel[c]; const bool horizontal = kind == StreamKind::Detail && c < 3;
    const size_t cw = horizontal ? w / 2 : (w + 1) / 2;
    const size_t height = kind != StreamKind::Detail ? (h + 1) / 2 : (horizontal ? h : h / 2);
    Need(ch.w == cw && ch.h == height && ch.hshift == 1 && ch.vshift == (horizontal ? 0 : 1), "wrong coefficient layout");
    const int32_t lo = kind == StreamKind::Detail ? -kDetailLimit :
                       (kind == StreamKind::Lowpass ? (c == 0 ? 0 : -255) : (c == 0 ? -255 : -510));
    const int32_t hi = kind == StreamKind::Detail ? kDetailLimit :
                       (kind == StreamKind::Lowpass ? 255 : (c == 0 ? 255 : 510));
    for (size_t y = 0; y < ch.h; ++y) for (size_t x = 0; x < ch.w; ++x)
      Need(ch.Row(y)[x] >= lo && ch.Row(y)[x] <= hi, "coefficient outside fixed signed range");
  }
}
Digest CoefficientHash(const jxl::Image& image) {
  Bytes data; data.reserve(CoefficientCount(image) * 4 + image.channel.size() * 16 + 12);
  Put32(data, uint32_t(image.w)); Put32(data, uint32_t(image.h)); Put32(data, uint32_t(image.channel.size()));
  for (const auto& ch : image.channel) {
    Put32(data, uint32_t(ch.w)); Put32(data, uint32_t(ch.h));
    Put32(data, uint32_t(ch.hshift)); Put32(data, uint32_t(ch.vshift));
    for (size_t y = 0; y < ch.h; ++y) for (size_t x = 0; x < ch.w; ++x) Put32(data, uint32_t(ch.Row(y)[x]));
  }
  return Hash(AsView(data));
}
void CopyChannel(const jxl::Channel& src, jxl::Channel* dst) {
  Need(src.w == dst->w && src.h == dst->h && src.hshift == dst->hshift && src.vshift == dst->vshift,
       "coefficient copy layout mismatch");
  for (size_t y = 0; y < src.h; ++y) if (src.w)
    std::copy(src.Row(y), src.Row(y) + src.w, dst->Row(y));
}
jxl::HistogramParams Params() {
  jxl::HistogramParams p; p.uint_method = jxl::HistogramParams::HybridUintMethod::kBest;
  p.lz77_method = jxl::HistogramParams::LZ77Method::kOptimal;
  p.force_huffman = false; p.streaming_mode = false; return p;
}
jxl::ModularOptions Options(StreamKind kind) {
  jxl::ModularOptions o; o.max_chan_size = kTile; o.group_dim = kTile;
  o.predictor = kind == StreamKind::Detail ? jxl::Predictor::Zero : jxl::Predictor::Best;
  o.max_properties = 0; o.nb_repeats = 0.5f; o.wp_mode = 0;
  o.splitting_heuristics_properties = {0, 15, 9, 10, 11, 12, 13};
  o.histogram_params = Params(); return o;
}
struct Stream {
  Bytes bytes; uint64_t bits = 0; Digest sha{}, coefficient_sha{};
  size_t coefficients = 0; TreeBudget tree{};
};
Stream EncodeStream(Memory& memory, const jxl::Image& image, StreamKind kind, uint32_t gid) {
  CheckShape(image, uint32_t(image.w), uint32_t(image.h), kind);
  Stream result; result.coefficients = CoefficientCount(image); result.coefficient_sha = CoefficientHash(image);
  if (result.coefficients == 0) {
    Need(kind == StreamKind::Detail && image.w == 1 && image.h == 1, "unexpected empty coefficient stream");
    result.sha = Hash(AsView(result.bytes)); return result;
  }
  auto options = Options(kind);
  auto tree = Take(jxl::LearnTree(&image, &options, 0, 1), "learn stream MA tree");
  result.tree = BoundTree(&tree, gid);
  jxl::BitWriter writer(&memory.mm);
  Check(jxl::ModularGenericCompress(image, options, writer, nullptr, kLayer, gid, &tree), "encode independent signed stream");
  result.bits = writer.BitsWritten(); result.bytes = Finish(writer);
  Need(result.bits > 0 && result.bytes.size() <= kMaxStreamBytes, "encoded stream outside byte bound");
  result.sha = Hash(AsView(result.bytes)); return result;
}
struct StreamView { View bytes; uint64_t bits; Digest sha; };
StreamView ReadStream(Cursor* cursor, bool may_be_empty) {
  const uint64_t bits = cursor->U64(), n = cursor->U64(); const auto sha = cursor->D();
  Need(n <= kMaxStreamBytes, "stream exceeds byte bound");
  Need((n == 0 && bits == 0 && may_be_empty) || (n > 0 && bits > (n - 1) * 8 && bits <= n * 8),
       "stream bit length mismatch");
  const auto payload = cursor->Read(size_t(n)); Need(Hash(payload) == sha, "stream SHA256 mismatch");
  if (bits % 8) Need((payload.data()[n - 1] >> (bits % 8)) == 0, "nonzero stream padding");
  return {payload, bits, sha};
}
void WriteStream(Bytes* output, const Stream& stream) {
  Put64(*output, stream.bits); Put64(*output, stream.bytes.size()); PutDigest(*output, stream.sha);
  output->insert(output->end(), stream.bytes.begin(), stream.bytes.end());
}
struct DecodedInfo { uint64_t consumed_bits = 0; size_t tree_nodes = 0; bool lz77_enabled = false; Digest coefficient_sha{}; };
jxl::Image DecodeStream(Memory& memory, const StreamView& stream, uint32_t w, uint32_t h,
                        uint32_t gid, StreamKind kind, DecodedInfo* info) {
  auto image = Shape(&memory.mm, w, h, kind); auto options = Options(kind);
  if (CoefficientCount(image) == 0) {
    Need(kind == StreamKind::Detail && w == 1 && h == 1 && stream.bytes.size() == 0 && stream.bits == 0,
         "empty detail profile mismatch");
    info->coefficient_sha = CoefficientHash(image); return image;
  }
  Need(stream.bytes.size() > 0, "missing coefficient stream");
  // Preflight rejects transforms and oversized trees before GenericDecompress.
  // Its actual LZ header is evidence of enabled syntax, not a token count.
  {
    Reader preflight(stream.bytes); jxl::GroupHeader header;
    Check(jxl::Bundle::Read(&preflight.br, &header), "read stream header");
    Need(header.transforms.empty() && !header.use_global_tree, "unexpected transform or external entropy model");
    jxl::Tree tree; Check(jxl::DecodeTree(&memory.mm, &preflight.br, &tree, kMaxNodes), "decode bounded stream tree");
    ValidateTree(tree, gid, "decoded", kMaxNodes); info->tree_nodes = tree.size();
    jxl::ANSCode code; std::vector<uint8_t> contexts;
    Check(jxl::DecodeHistograms(&memory.mm, &preflight.br, (tree.size() + 1) / 2, &code, &contexts, false), "read stream entropy model");
    info->lz77_enabled = code.lz77.enabled;
    Need(preflight.br.TotalBitsConsumed() <= stream.bits, "stream header exceeds meaningful bits");
    Check(preflight.br.AllReadsWithinBounds(), "stream preflight overread");
  }
  Reader reader(stream.bytes); jxl::GroupHeader header;
  Check(jxl::ModularGenericDecompress(&reader.br, image, &header, gid, &options, false), "decode signed coefficient stream");
  Need(header.transforms.empty() && !header.use_global_tree, "decoded stream profile changed");
  info->consumed_bits = reader.br.TotalBitsConsumed(); reader.End(stream.bits);
  CheckShape(image, w, h, kind); info->coefficient_sha = CoefficientHash(image); return image;
}
struct Header {
  uint32_t mode, gid, w, h, aw, ah; Digest raw_sha, anchor_sha;
};
Header ReadHeader(Cursor* cursor) {
  cursor->Magic("S089TIL1"); Need(cursor->U32() == 1, "unknown tile profile");
  Header h{cursor->U32(), cursor->U32(), cursor->U32(), cursor->U32(), cursor->U32(), cursor->U32(), {}, {}};
  h.raw_sha = cursor->D(); h.anchor_sha = cursor->D(); Dimensions(h.w, h.h);
  Need(h.mode <= 1 && h.gid < kMaxGroups && h.aw == (h.w + 1) / 2 && h.ah == (h.h + 1) / 2,
       "invalid tile mode/ID/anchor dimensions");
  if (h.mode == 0) Need(h.anchor_sha == Digest{}, "ordinary tile contains anchor binding");
  return h;
}
Bytes Record(const Tile& tile, uint32_t mode, const Digest& rgb_sha, const Digest& anchor_sha,
             const Stream& detail, const Stream& low) {
  Bytes record{'S', '0', '8', '9', 'T', 'I', 'L', '1'};
  for (uint32_t value : {1u, mode, tile.id, tile.w, tile.h, (tile.w + 1) / 2, (tile.h + 1) / 2}) Put32(record, value);
  PutDigest(record, rgb_sha); PutDigest(record, mode ? anchor_sha : Digest{});
  WriteStream(&record, detail); WriteStream(&record, low);
  Need(record.size() == 196 + detail.bytes.size() + low.bytes.size() && record.size() <= kMaxRecordBytes,
       "record size mismatch"); return record;
}
struct DecodeInfo { DecodedInfo detail, low; };
Bytes DecodeTile(View record, View anchor, uint32_t gid, uint32_t w, uint32_t h, DecodeInfo* stats) {
  Need(record.size() >= 196 && record.size() <= kMaxRecordBytes, "tile record size outside bound");
  Dimensions(w, h); Cursor cursor{record}; const auto header = ReadHeader(&cursor);
  Need(header.gid == gid && header.w == w && header.h == h, "tile identity/dimension binding mismatch");
  if (header.mode) {
    Need(anchor.data() && anchor.size() == size_t(header.aw) * header.ah * 3, "missing or wrong-sized native anchor");
    Need(Hash(anchor) == header.anchor_sha, "native anchor SHA256 mismatch");
  } else {
    Need(anchor.data() == nullptr && anchor.size() == 0, "ordinary tile rejects external anchor");
  }
  const auto hf = ReadStream(&cursor, w == 1 && h == 1);
  const auto lp = ReadStream(&cursor, false);
  Need(cursor.pos == record.size(), "trailing tile bytes");
  Memory memory;
  auto detail = DecodeStream(memory, hf, w, h, gid, StreamKind::Detail, &stats->detail);
  const auto low_kind = header.mode ? StreamKind::Correction : StreamKind::Lowpass;
  auto low = DecodeStream(memory, lp, w, h, gid, low_kind, &stats->low);
  if (header.mode) {
    auto transformed_anchor = ToColor(&memory.mm, anchor, header.aw, header.ah);
    for (size_t c = 0; c < 3; ++c) for (size_t y = 0; y < header.ah; ++y)
      for (size_t x = 0; x < header.aw; ++x) {
        const int64_t sum = int64_t(low.channel[c].Row(y)[x]) + transformed_anchor.channel[c].Row(y)[x];
        Need(sum >= (c == 0 ? 0 : -255) && sum <= 255, "restored lowpass outside color range");
        low.channel[c].Row(y)[x] = int32_t(sum);
      }
  }
  CheckShape(low, w, h, StreamKind::Lowpass);
  auto image = Take(jxl::Image::Create(&memory.mm, w, h, 9, 0), "create reconstruction image");
  for (auto& channel : low.channel) image.channel.push_back(std::move(channel));
  for (auto& channel : detail.channel) image.channel.push_back(std::move(channel));
  Need(image.channel.size() == 9, "wrong reconstruction layout");
  Check(jxl::InvSqueeze(image, Squeezes(), nullptr), "inverse integer Squeeze");
  Need(image.channel.size() == 3, "inverse Squeeze channel count");
  for (const auto& channel : image.channel)
    Need(channel.w == w && channel.h == h && channel.hshift == 0 && channel.vshift == 0, "inverse Squeeze shape mismatch");
  Bytes rgb(size_t(w) * h * 3);
  for (size_t y = 0; y < h; ++y) for (size_t x = 0; x < w; ++x) {
    const int64_t g = image.channel[0].Row(y)[x];
    const int64_t r = g + image.channel[1].Row(y)[x], b = g + image.channel[2].Row(y)[x];
    Need(g >= 0 && g <= 255 && r >= 0 && r <= 255 && b >= 0 && b <= 255, "restored RGB outside uint8 range");
    const size_t i = (y * w + x) * 3; rgb[i] = uint8_t(r); rgb[i + 1] = uint8_t(g); rgb[i + 2] = uint8_t(b);
  }
  Need(Hash(AsView(rgb)) == header.raw_sha, "restored RGB SHA256 mismatch"); return rgb;
}
std::string StreamReceipt(const Stream& stream, const DecodedInfo& decoded, StreamKind kind) {
  Need(stream.bits == decoded.consumed_bits && stream.tree.emitted_nodes == decoded.tree_nodes &&
       stream.coefficient_sha == decoded.coefficient_sha, "stream reproduction receipt mismatch");
  std::ostringstream out;
  out << "{\"bytes\":" << stream.bytes.size() << ",\"bits\":" << stream.bits
      << ",\"consumed_bits\":" << decoded.consumed_bits << ",\"sha256\":\"" << Hex(stream.sha)
      << "\",\"coefficient_sha256\":\"" << Hex(stream.coefficient_sha) << "\",\"coefficients\":" << stream.coefficients
      << ",\"tree_learned_nodes\":" << stream.tree.learned_nodes << ",\"tree_nodes\":" << stream.tree.emitted_nodes
      << ",\"tree_pruned_splits\":" << stream.tree.pruned_splits << ",\"tree_collapsed_subtrees\":" << stream.tree.collapsed_subtrees
      << ",\"tree_was_pruned\":" << (stream.tree.pruned_splits ? "true" : "false")
      << ",\"predictor\":\"" << (kind == StreamKind::Detail ? "Zero" : "Best")
      << "\",\"empty\":" << (stream.coefficients == 0 ? "true" : "false")
      << ",\"lz77_enabled\":" << (decoded.lz77_enabled ? "true" : "false") << '}'; return out.str();
}
void Encode(const fs::path& spool, const fs::path& out) {
  Need(fs::is_directory(spool) && !fs::is_symlink(spool), "spool must be a non-symlink directory");
  Need(!fs::exists(out) && fs::is_directory(out.parent_path()), "output must be a fresh child of an existing directory");
  const auto tiles = ReadIndex(spool);
  Need(fs::create_directory(out), "cannot create output directory");
  Need(fs::create_directory(out / "ordinary") && fs::create_directory(out / "anchored"), "cannot create record directories");
  std::ostringstream receipt;
  receipt << "{\"format\":\"st089-native-squeeze-bank-1\",\"standard_jxl\":false,\"abi_version\":1,\"profile\":1,\"groups\":" << tiles.size()
          << ",\"tree_node_limit\":4095,\"tree_budget_policy\":\"bfs_parent_predictor_v1\",\"detail_predictor\":\"Zero\",\"lowpass_predictor\":\"Best\","
             "\"color_transform\":\"G,R-G,B-G\",\"squeeze\":\"H_then_V_first3_append\",\"uint_method\":\"kBest\",\"lz77_method\":\"kOptimal_per_stream\",\"tiles\":[";
  uint64_t ordinary_cost = 0, anchored_cost = 0, detail_cost = 0, ll_cost = 0, correction_cost = 0;
  for (size_t index = 0; index < tiles.size(); ++index) {
    const auto& tile = tiles[index]; const uint32_t aw = (tile.w + 1) / 2, ah = (tile.h + 1) / 2;
    const auto rgb = ReadFile(spool / tile.rgb_name, size_t(kTile) * kTile * 3);
    const auto anchor = ReadFile(spool / tile.anchor_name, size_t(kTile / 2) * (kTile / 2) * 3);
    Need(rgb.size() == size_t(tile.w) * tile.h * 3 && anchor.size() == size_t(aw) * ah * 3, "spooled RGB/anchor size mismatch");
    const auto rgb_sha = Hash(AsView(rgb)), anchor_sha = Hash(AsView(anchor));
    Memory memory; auto squeezed = ToColor(&memory.mm, AsView(rgb), tile.w, tile.h);
    Check(jxl::FwdSqueeze(squeezed, Squeezes(), nullptr), "forward integer Squeeze");
    Need(squeezed.channel.size() == 9 && squeezed.transform.empty(), "unexpected forward Squeeze layout");
    auto detail_image = Shape(&memory.mm, tile.w, tile.h, StreamKind::Detail);
    auto low_image = Shape(&memory.mm, tile.w, tile.h, StreamKind::Lowpass);
    auto correction_image = Shape(&memory.mm, tile.w, tile.h, StreamKind::Correction);
    auto anchor_color = ToColor(&memory.mm, AsView(anchor), aw, ah);
    for (size_t c = 0; c < 6; ++c) CopyChannel(squeezed.channel[c + 3], &detail_image.channel[c]);
    for (size_t c = 0; c < 3; ++c) {
      CopyChannel(squeezed.channel[c], &low_image.channel[c]);
      for (size_t y = 0; y < ah; ++y) for (size_t x = 0; x < aw; ++x)
        correction_image.channel[c].Row(y)[x] = low_image.channel[c].Row(y)[x] - anchor_color.channel[c].Row(y)[x];
    }
    Need(CoefficientCount(detail_image) + CoefficientCount(low_image) == size_t(tile.w) * tile.h * 3,
         "Squeeze coefficient conservation failed");
    // The only detail encode. Each complete candidate physically contains and
    // pays for this identical byte stream; it is not an external free reference.
    const auto detail = EncodeStream(memory, detail_image, StreamKind::Detail, tile.id);
    const auto low = EncodeStream(memory, low_image, StreamKind::Lowpass, tile.id);
    const auto correction = EncodeStream(memory, correction_image, StreamKind::Correction, tile.id);
    const auto ordinary = Record(tile, 0, rgb_sha, anchor_sha, detail, low);
    const auto anchored = Record(tile, 1, rgb_sha, anchor_sha, detail, correction);
    DecodeInfo ordinary_check, anchored_check;
    Need(DecodeTile(AsView(ordinary), {nullptr, 0}, tile.id, tile.w, tile.h, &ordinary_check) == rgb,
         "ordinary immediate reconstruction mismatch");
    Need(DecodeTile(AsView(anchored), AsView(anchor), tile.id, tile.w, tile.h, &anchored_check) == rgb,
         "anchored immediate reconstruction mismatch");
    Need(ordinary_check.detail.consumed_bits == anchored_check.detail.consumed_bits &&
         ordinary_check.detail.coefficient_sha == anchored_check.detail.coefficient_sha,
         "matched detail reconstruction mismatch");
    const std::string ordinary_name = Name("ordinary/", tile.id), anchored_name = Name("anchored/", tile.id);
    WriteNew(out / ordinary_name, AsView(ordinary)); WriteNew(out / anchored_name, AsView(anchored));
    if (index) receipt << ',';
    receipt << "{\"gid\":" << tile.id << ",\"level\":0,\"x\":" << tile.x << ",\"y\":" << tile.y
            << ",\"w\":" << tile.w << ",\"h\":" << tile.h << ",\"anchor_w\":" << aw << ",\"anchor_h\":" << ah
            << ",\"raw_sha256\":\"" << Hex(rgb_sha) << "\",\"anchor_sha256\":\"" << Hex(anchor_sha)
            << "\",\"ordinary_path\":\"" << ordinary_name << "\",\"anchored_path\":\"" << anchored_name
            << "\",\"ordinary_bytes\":" << ordinary.size() << ",\"anchored_bytes\":" << anchored.size()
            << ",\"ordinary_sha256\":\"" << Hex(Hash(AsView(ordinary))) << "\",\"anchored_sha256\":\"" << Hex(Hash(AsView(anchored)))
            << "\",\"detail_shared_exact\":true,\"detail\":" << StreamReceipt(detail, ordinary_check.detail, StreamKind::Detail)
            << ",\"lowpass\":" << StreamReceipt(low, ordinary_check.low, StreamKind::Lowpass)
            << ",\"correction\":" << StreamReceipt(correction, anchored_check.low, StreamKind::Correction) << '}';
    ordinary_cost += ordinary.size(); anchored_cost += anchored.size(); detail_cost += detail.bytes.size();
    ll_cost += low.bytes.size(); correction_cost += correction.bytes.size();
    std::cerr << "encoded tile " << (index + 1) << '/' << tiles.size() << "\n";
  }
  receipt << "],\"costs\":{\"ordinary_bytes\":" << ordinary_cost << ",\"anchored_bytes\":" << anchored_cost
          << ",\"detail_stream_bytes\":" << detail_cost << ",\"lowpass_stream_bytes\":" << ll_cost
          << ",\"correction_stream_bytes\":" << correction_cost << ",\"record_header_bytes_per_tile\":196}}\n";
  WriteText(out / "receipt.json", receipt.str());
  std::cout << "{\"ok\":true,\"groups\":" << tiles.size() << ",\"ordinary_bytes\":" << ordinary_cost
            << ",\"anchored_bytes\":" << anchored_cost << "}\n";
}
void Error(char* out, size_t size, const char* text) {
  if (out && size) { const size_t n = std::min(size - 1, std::strlen(text)); std::memcpy(out, text, n); out[n] = 0; }
}
template<class F> int Api(char* error, size_t size, F body) {
  if (error && size) error[0] = 0;
  try { body(); return 0; } catch (const std::exception& e) { Error(error, size, e.what()); return 1; }
  catch (...) { Error(error, size, "unknown native failure"); return 1; }
}
}  // namespace

extern "C" ST_NATIVE_EXPORT uint32_t st089_abi_version() { return 1; }
extern "C" ST_NATIVE_EXPORT int st089_decode(const uint8_t* tile, size_t tile_size,
    const uint8_t* anchor, size_t anchor_size, uint32_t gid, uint32_t w, uint32_t h,
    uint8_t* out, size_t out_size, char* error, size_t error_size) {
  return Api(error, error_size, [&] {
    Need(tile && out, "null tile or RGB output"); Dimensions(w, h);
    Need(out_size == size_t(w) * h * 3, "wrong RGB output size");
    DecodeInfo stats; const auto rgb = DecodeTile({tile, tile_size}, {anchor, anchor_size}, gid, w, h, &stats);
    std::memcpy(out, rgb.data(), rgb.size());
  });
}
int main(int argc, char** argv) {
  try {
    stportable::UnsetEnv("ST074_S"); stportable::UnsetEnv("ST074_T");
    if (argc == 2 && std::string(argv[1]) == "tree_selfcheck") { TreeSelfcheck(); return 0; }
    if (argc == 4 && std::string(argv[1]) == "encode") { Encode(fs::u8path(argv[2]), fs::u8path(argv[3])); return 0; }
    if (argc == 5 && std::string(argv[1]) == "decode") {
      const auto tile = ReadFile(fs::u8path(argv[2]), kMaxRecordBytes); Cursor cursor{AsView(tile)}; const auto header = ReadHeader(&cursor);
      Bytes anchor; View anchor_view{nullptr, 0};
      if (std::string(argv[3]) != "-") { anchor = ReadFile(fs::u8path(argv[3]), size_t(kTile / 2) * (kTile / 2) * 3); anchor_view = AsView(anchor); }
      DecodeInfo stats; const auto rgb = DecodeTile(AsView(tile), anchor_view, header.gid, header.w, header.h, &stats);
      WriteNew(fs::u8path(argv[4]), AsView(rgb));
      std::cout << "{\"decoded_tiles\":1,\"rgb_bytes\":" << rgb.size() << ",\"detail_consumed_bits\":"
                << stats.detail.consumed_bits << ",\"lowpass_consumed_bits\":" << stats.low.consumed_bits << "}\n"; return 0;
    }
    throw std::runtime_error("usage: encode spooldir newoutdir | decode tile anchor_or_dash newrgb | tree_selfcheck");
  } catch (const std::exception& e) { std::cerr << "FAIL: " << e.what() << '\n'; return 1; }
}
