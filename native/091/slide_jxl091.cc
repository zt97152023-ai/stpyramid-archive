// STBASELINE-091: parent-conditioned Squeeze details, NOT a .jxl file.
// New isolated Modular operators link immutable libjxl 0.12.0/074. Each tile
// encodes V then H details. The direct path builds the correction in memory.
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
#include "vendor091/known_context.h"

namespace {
namespace fs = std::filesystem;
using Bytes = std::vector<uint8_t>;
using Digest = std::array<uint8_t, 32>;
constexpr uint32_t kMaxGroups = 1000000, kTile = 256;
constexpr size_t kMaxNodes = 4095, kMaxStreamBytes = 2 * 1024 * 1024;
constexpr size_t kMaxRecordBytes = 2 * kMaxStreamBytes + 260;
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
    if(node.property < -1 || node.property>35 || node.property==1)TreeError(region,stage,n,"invalid-or-group-id-property");
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
  std::string rgb_name, anchor_name, old_name;
};
bool SafeName(const std::string& s) {
  return !s.empty() && s != "." && s != ".." &&
    s.find_first_not_of("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._-") == std::string::npos;
}
std::vector<Tile> ReadIndex(const fs::path& spool, bool expects_old = true) {
  auto bytes = ReadFile(spool / "index.tsv", 128 * 1024 * 1024);
  std::istringstream input(std::string(bytes.begin(), bytes.end()));
  std::string line; std::vector<Tile> result;
  std::set<std::pair<uint32_t, uint32_t>> positions;
  while (std::getline(input, line)) {
    if (!line.empty() && line.back() == '\r') line.pop_back();
    Need(!line.empty() && line.size() < 1024 && line.back() != '\t', "invalid spool row");
    std::istringstream row(line); std::vector<std::string> fields; std::string field;
    while (std::getline(row, field, '\t')) fields.push_back(field);
    Need(fields.size() == (expects_old ? 9 : 8),
         "index needs gid level x y w h rgb_filename anchor_filename and, for legacy encoding, old089_record_filename");
    Need(result.size() < kMaxGroups, "too many tiles");
    Tile t{Number(fields[0]), Number(fields[1]), Number(fields[2]), Number(fields[3]),
           Number(fields[4]), Number(fields[5]), fields[6], fields[7],
           expects_old ? fields[8] : std::string{}};
    Dimensions(t.w, t.h);
    const bool valid_id = expects_old ? t.id == result.size() :
        t.id < kMaxGroups && (result.empty() || t.id > result.back().id);
    Need(valid_id && t.level == 0 && t.x % kTile == 0 && t.y % kTile == 0,
         "invalid L0 ID/level/tile origin");
    Need(SafeName(t.rgb_name) && SafeName(t.anchor_name) &&
             (!expects_old || SafeName(t.old_name)), "unsafe spool filename");
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
  size_t coefficients = 0; TreeBudget tree{}; size_t parent_nodes=0, parent_pairs=0, target_values=0;
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
struct DecodedInfo { uint64_t consumed_bits = 0; size_t tree_nodes = 0; bool lz77_enabled = false; Digest coefficient_sha{}; size_t parent_nodes=0, parent_pairs=0, target_values=0; };
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

jxl::Image KnownShape(JxlMemoryManager* mm, uint32_t w, uint32_t h) {
  auto image=Take(jxl::Image::Create(mm,w,h,12,0),"known image");
  for(size_t c=0;c<12;++c) {
    const bool vertical=c<6;
    auto channel=Take(jxl::Channel::Create(mm,vertical?(w+1)/2:w/2,
                       vertical?h/2:h,1,vertical?1:0),"known channel");
    channel.component=int(c%3); jxl::ZeroFillImage(&channel.plane);
    image.channel.push_back(std::move(channel));
  }
  return image;
}
jxl::Image SelectChannels(const jxl::Image& image, const std::vector<size_t>& ids) {
  auto result=Take(jxl::Image::Create(image.memory_manager(),image.w,image.h,12,0),"selected image");
  for(size_t c:ids) {
    const auto& source=image.channel[c];
    auto target=Take(jxl::Channel::Create(image.memory_manager(),source.w,source.h,source.hshift,source.vshift),"selected channel");
    CopyChannel(source,&target); result.channel.push_back(std::move(target));
  }
  return result;
}
Digest ReferenceChannelHash(const jxl::Image& image,bool horizontal) {
  return CoefficientHash(SelectChannels(image,horizontal?std::vector<size_t>{6,7,8}:std::vector<size_t>{0,1,2}));
}
Digest DetailHash(const jxl::Image& image) {
  // Canonical receipt order remains the qualified089 H0,H1,H2,V0,V1,V2.
  return CoefficientHash(SelectChannels(image,{9,10,11,3,4,5}));
}
size_t TargetCount(const jxl::Image& image) {
  size_t count=0; for(size_t c=0;c<12;++c) if(jxl::st091::IsTarget(c)) count+=image.channel[c].w*image.channel[c].h;
  return count;
}
void CheckKnown(const jxl::Image& image) {
  Need(image.channel.size()==12 && image.transform.empty() && image.nb_meta_channels==0 && !image.error,"known image profile");
  for(size_t c=0;c<12;++c) {
    const auto& ch=image.channel[c]; const bool v=c<6;
    Need(ch.w==(v?(image.w+1)/2:image.w/2) && ch.h==(v?image.h/2:image.h) && ch.hshift==1 && ch.vshift==(v?1:0),"known channel shape");
    const int lo=jxl::st091::IsTarget(c)?-kDetailLimit:(c%3? -255:0);
    const int hi=jxl::st091::IsTarget(c)?kDetailLimit:255;
    for(size_t y=0;y<ch.h;++y) for(size_t x=0;x<ch.w;++x)
      Need(ch.Row(y)[x]>=lo && ch.Row(y)[x]<=hi,"known coefficient range");
  }
}
void FillReferenceChannels(jxl::Image* image,const jxl::Image& parent,bool horizontal) {
  for(size_t c=0;c<3;++c) {
    auto& dst=image->channel[(horizontal?6:0)+c]; const auto& src=parent.channel[c];
    Need(dst.w<=src.w && dst.h<=src.h && dst.hshift==src.hshift && dst.vshift==src.vshift,"parent projection shape");
    for(size_t y=0;y<dst.h;++y) if(dst.w) std::copy(src.Row(y),src.Row(y)+dst.w,dst.Row(y));
  }
}
jxl::Image ReconstructHorizontalLowpass(const jxl::Image& low,const jxl::Image& known) {
  auto image=SelectChannels(low,{0,1,2});
  auto vertical=SelectChannels(known,{3,4,5});
  for(auto& ch:vertical.channel) image.channel.push_back(std::move(ch));
  const auto params=Squeezes();
  Check(jxl::InvSqueeze(image,{params[1]},nullptr),"inverse vertical stage");
  Need(image.channel.size()==3,"horizontal lowpass count");
  for(const auto& ch:image.channel) Need(ch.w==(image.w+1)/2 && ch.h==image.h && ch.hshift==1 && ch.vshift==0,"horizontal lowpass shape");
  return image;
}
jxl::ModularOptions KnownOptions() {
  auto o=Options(StreamKind::Detail); o.max_properties=5;
  for(uint32_t p=16;p<=35;++p) o.splitting_heuristics_properties.push_back(p);
  return o;
}
std::pair<size_t,size_t> ParentSplits(const jxl::Tree& tree,const jxl::Image& image) {
  std::set<size_t> nodes; size_t pairs=0;
  for(size_t c:{3u,4u,5u,9u,10u,11u}) {
    if(!image.channel[c].w || !image.channel[c].h) continue;
    std::vector<size_t> pending{0};
    while(!pending.empty()) {
      size_t id=pending.back(); pending.pop_back(); const auto& n=tree[id];
      if(n.property==-1) continue;
      if(n.property==0) { pending.push_back(int32_t(c)>n.splitval?n.lchild:n.rchild); continue; }
      if(n.property>=16) {
        const size_t slot=(n.property-16)/4, previous=c%6-3;
        if(slot>=previous && slot<previous+3) { nodes.insert(id); ++pairs; }
      }
      pending.push_back(n.lchild); pending.push_back(n.rchild);
    }
  }
  return {nodes.size(),pairs};
}
Stream EncodeDetailStream(Memory& memory,const jxl::Image& image,uint32_t gid,bool enabled) {
  CheckKnown(image); Stream result; result.coefficients=TargetCount(image); result.coefficient_sha=DetailHash(image);
  if(!result.coefficients) { Need(image.w==1 && image.h==1,"empty known profile"); result.sha=Hash(AsView(result.bytes)); return result; }
  auto options=KnownOptions(); const jxl::st091::KnownContext context{enabled,nullptr,nullptr,&result.target_values};
  auto tree=Take(jxl::st091::KnownLearnTree(&image,&options,0,1,context,{}),"learn known HF tree");
  result.tree=BoundTree(&tree,gid); const auto parent=ParentSplits(tree,image);
  result.parent_nodes=enabled?parent.first:0; result.parent_pairs=enabled?parent.second:0;
  for(const auto& node:tree) Need(node.predictor==jxl::Predictor::Zero,"HF predictor is not Zero");
  jxl::BitWriter writer(&memory.mm);
  Check(jxl::st091::KnownGenericCompress(image,options,writer,nullptr,kLayer,gid,&tree,context),"encode known HF");
  Need(result.target_values==result.coefficients,"encoder emitted non-HF values");
  result.bits=writer.BitsWritten(); result.bytes=Finish(writer);
  Need(result.bits && result.bytes.size()<=kMaxStreamBytes,"known stream bounds");
  result.sha=Hash(AsView(result.bytes)); return result;
}
struct StageState {
  const jxl::Image* low; Digest expected; std::unique_ptr<jxl::Image> horizontal;
  Digest prefix_v;
};
jxl::Status PrepareHorizontal(jxl::Image& image,void* opaque) {
  auto& state=*static_cast<StageState*>(opaque);
  Need(!state.horizontal,"horizontal stage invoked twice");
  Need(ReferenceChannelHash(image,false)==state.prefix_v,"decoder overwrote known vertical prefix");
  // Reject hostile decoded V values before integer inverse arithmetic. H
  // targets and their not-yet-built prefixes are still initialized zero here.
  CheckKnown(image);
  auto horizontal=ReconstructHorizontalLowpass(*state.low,image); FillReferenceChannels(&image,horizontal,true);
  Need(ReferenceChannelHash(image,true)==state.expected,"horizontal parent SHA mismatch");
  state.horizontal=std::make_unique<jxl::Image>(std::move(horizontal)); return true;
}
std::pair<jxl::Image,jxl::Image> DecodeDetailStream(Memory& memory,const StreamView& stream,const jxl::Image& low,
    uint32_t gid,bool enabled,const Digest& prefix_v,const Digest& prefix_h,DecodedInfo* info) {
  auto image=KnownShape(&memory.mm,uint32_t(low.w),uint32_t(low.h)); FillReferenceChannels(&image,low,false);
  Need(ReferenceChannelHash(image,false)==prefix_v,"vertical parent SHA mismatch");
  StageState stage{&low,prefix_h,nullptr,prefix_v};
  if(!TargetCount(image)) {
    Need(image.w==1 && image.h==1 && stream.bits==0 && stream.bytes.size()==0,"empty HF bounds");
    Check(PrepareHorizontal(image,&stage),"empty horizontal stage");
  } else {
    Need(stream.bytes.size()>0,"missing HF payload");
    {
      Reader preflight(stream.bytes); jxl::GroupHeader header;
      Check(jxl::Bundle::Read(&preflight.br,&header),"known header");
      Need(header.transforms.empty() && !header.use_global_tree,"known forbidden transforms/tree");
      jxl::Tree tree; Check(jxl::DecodeTree(&memory.mm,&preflight.br,&tree,kMaxNodes),"known bounded tree");
      ValidateTree(tree,gid,"known decoded",kMaxNodes); info->tree_nodes=tree.size();
      for(const auto& node:tree) Need(node.predictor==jxl::Predictor::Zero,"HF predictor is not Zero");
      const auto parent=ParentSplits(tree,image); info->parent_nodes=enabled?parent.first:0; info->parent_pairs=enabled?parent.second:0;
      jxl::ANSCode code; std::vector<uint8_t> map;
      Check(jxl::DecodeHistograms(&memory.mm,&preflight.br,(tree.size()+1)/2,&code,&map,false),"known model");
      info->lz77_enabled=code.lz77.enabled;
      Need(preflight.br.TotalBitsConsumed()<=stream.bits,"known header bit bound");
      Check(preflight.br.AllReadsWithinBounds(),"known preflight overread");
    }
    auto options=KnownOptions(); Reader reader(stream.bytes); jxl::GroupHeader header;
    const jxl::st091::KnownContext context{enabled,PrepareHorizontal,&stage,&info->target_values};
    Check(jxl::st091::KnownGenericDecompress(&reader.br,image,&header,gid,&options,context),"known staged decode");
    info->consumed_bits=reader.br.TotalBitsConsumed(); reader.End(stream.bits);
  }
  Need(bool(stage.horizontal),"missing horizontal reconstruction");
  Need(info->target_values==TargetCount(image),"decoder consumed non-HF values");
  Need(ReferenceChannelHash(image,false)==prefix_v && ReferenceChannelHash(image,true)==prefix_h,"decoder overwrote known prefix");
  CheckKnown(image); info->coefficient_sha=DetailHash(image);
  return {std::move(image),std::move(*stage.horizontal)};
}
void ContextSelfcheck() {
  // Structural check of the actual property helper, not a substitute for
  // encoded synthetic-stream token counts and source-blocked round trips.
  Memory memory; auto image=KnownShape(&memory.mm,8,8);
  for(size_t c=0;c<image.channel.size();++c) {
    auto& ch=image.channel[c];
    for(size_t y=0;y<ch.h;++y) for(size_t x=0;x<ch.w;++x)
      ch.Row(y)[x]=int32_t(3+c+2*x+3*y);
  }
  const auto original=CoefficientHash(image);
  size_t parent_values=0,causal_values=0,target_channels=0,known_channels=0;
  for(size_t c=0;c<image.channel.size();++c) {
    if(!jxl::st091::IsTarget(c)) { Need(jxl::st091::IsParent(c),"unclassified channel"); ++known_channels; continue; }
    ++target_channels; const auto& ch=image.channel[c];
    auto control=Take(jxl::Channel::Create(&memory.mm,20,ch.w),"selfcheck control refs");
    auto conditional=Take(jxl::Channel::Create(&memory.mm,20,ch.w),"selfcheck conditional refs");
    for(size_t y=0;y<ch.h;++y) {
      jxl::st091::KnownReferences(ch,y,image,uint32_t(c),&control,{false,nullptr,nullptr,nullptr});
      jxl::st091::KnownReferences(ch,y,image,uint32_t(c),&conditional,{true,nullptr,nullptr,nullptr});
      size_t offset=0;
      for(int32_t previous=int32_t(c)-1;previous>=0 && offset<20;--previous) {
        const auto& ref=image.channel[size_t(previous)];
        if(ref.w!=ch.w || ref.h!=ch.h || ref.hshift!=ch.hshift || ref.vshift!=ch.vshift) continue;
        const bool parent=jxl::st091::IsParent(size_t(previous));
        for(size_t x=0;x<ch.w;++x) for(size_t p=0;p<4;++p) {
          const auto a=control.Row(x)[offset+p],b=conditional.Row(x)[offset+p];
          if(parent) { Need(a==0,"disabled parent slot not zero"); parent_values+=b!=0; }
          else { Need(a==b,"causal reference changed between arms"); causal_values+=a!=0; }
        }
        offset+=4;
      }
      for(size_t x=0;x<ch.w;++x) for(size_t p=offset;p<20;++p)
        Need(control.Row(x)[p]==0 && conditional.Row(x)[p]==0,"unused reference slot nonzero");
    }
  }
  Need(parent_values && causal_values && target_channels==6 && known_channels==6,"selfcheck coverage");
  Need(CoefficientHash(image)==original,"reference helper changed prefix or target values");
  for(int32_t property:{36,1}) {
    jxl::Tree malformed(3,jxl::PropertyDecisionNode::Leaf(jxl::Predictor::Zero));
    malformed[0]=jxl::PropertyDecisionNode::Split(property,0,1);
    bool rejected=false;
    try { ValidateTree(malformed,0,"selfcheck-property",kMaxNodes); }
    catch(const std::runtime_error&) { rejected=true; }
    Need(rejected,"forbidden reference/group property accepted");
  }
  std::cout<<"{\"ok\":true,\"target_channels\":[3,4,5,9,10,11],\"known_channels\":[0,1,2,6,7,8],\"parent_gate_zero\":true,\"causal_reference_equal\":true,\"prefix_readonly\":true,\"malformed_rejected\":[\"property36\",\"group_id\"]}\n";
}
struct Header { uint32_t mode,gid,w,h,aw,ah; Digest raw,anchor,vertical,horizontal; };
Header ReadTileRecordHeader(Cursor* cursor) {
  cursor->Magic("S091TIL1"); Need(cursor->U32()==1,"unknown091 profile");
  Header h{cursor->U32(),cursor->U32(),cursor->U32(),cursor->U32(),cursor->U32(),cursor->U32(),{},{},{},{}};
  h.raw=cursor->D(); h.anchor=cursor->D(); h.vertical=cursor->D(); h.horizontal=cursor->D(); Dimensions(h.w,h.h);
  Need(h.mode<=1 && h.gid<kMaxGroups && h.aw==(h.w+1)/2 && h.ah==(h.h+1)/2,"invalid091 identity"); return h;
}
jxl::Image RestoreLowpassCoefficients(Memory& memory,const StreamView& lp,View anchor,uint32_t gid,uint32_t w,uint32_t h,DecodedInfo* info) {
  auto low=DecodeStream(memory,lp,w,h,gid,StreamKind::Correction,info);
  auto color=ToColor(&memory.mm,anchor,(w+1)/2,(h+1)/2);
  for(size_t c=0;c<3;++c) for(size_t y=0;y<low.channel[c].h;++y) for(size_t x=0;x<low.channel[c].w;++x) {
    const int64_t value=int64_t(low.channel[c].Row(y)[x])+color.channel[c].Row(y)[x];
    Need(value>=(c? -255:0) && value<=255,"corrected LL range"); low.channel[c].Row(y)[x]=int32_t(value);
  }
  CheckShape(low,w,h,StreamKind::Lowpass); return low;
}
struct DecodeInfo { DecodedInfo detail,low; };
Bytes DecodeTileRecord(View record,View anchor,uint32_t gid,uint32_t w,uint32_t h,DecodeInfo* stats) {
  Need(record.size()>=260 && record.size()<=kMaxRecordBytes,"091 record bounds"); Cursor cursor{record}; const auto header=ReadTileRecordHeader(&cursor);
  Need(header.gid==gid && header.w==w && header.h==h,"091 requested identity differs");
  Need(anchor.data() && anchor.size()==size_t(header.aw)*header.ah*3 && Hash(anchor)==header.anchor,"091 anchor mismatch");
  const auto lp=ReadStream(&cursor,false), hf=ReadStream(&cursor,w==1 && h==1);
  Need(cursor.pos==record.size(),"trailing091 bytes"); Memory memory;
  auto low=RestoreLowpassCoefficients(memory,lp,anchor,gid,w,h,&stats->low);
  auto stages=DecodeDetailStream(memory,hf,low,gid,header.mode!=0,header.vertical,header.horizontal,&stats->detail);
  auto image=std::move(stages.second); auto horizontal=SelectChannels(stages.first,{9,10,11});
  for(auto& ch:horizontal.channel) image.channel.push_back(std::move(ch));
  Check(jxl::InvSqueeze(image,{Squeezes()[0]},nullptr),"inverse horizontal stage");
  Need(image.channel.size()==3,"final channel count");
  for(const auto& ch:image.channel) Need(ch.w==w && ch.h==h && !ch.hshift && !ch.vshift,"final channel shape");
  Bytes rgb(size_t(w)*h*3);
  for(size_t y=0;y<h;++y) for(size_t x=0;x<w;++x) {
    const int64_t g=image.channel[0].Row(y)[x],r=g+image.channel[1].Row(y)[x],b=g+image.channel[2].Row(y)[x];
    Need(g>=0 && g<=255 && r>=0 && r<=255 && b>=0 && b<=255,"RGB range");
    const size_t i=(y*w+x)*3; rgb[i]=uint8_t(r); rgb[i+1]=uint8_t(g); rgb[i+2]=uint8_t(b);
  }
  Need(Hash(AsView(rgb))==header.raw,"091 final RGB SHA"); return rgb;
}
Stream ReadStored089Correction(View record,const Tile& tile,const Digest& raw,const Digest& anchor) {
  Need(record.size()>=196 && record.size()<=2*kMaxStreamBytes+196,"old089 size"); Cursor cursor{record}; cursor.Magic("S089TIL1");
  Need(cursor.U32()==1 && cursor.U32()==1,"old089 anchored profile required");
  Need(cursor.U32()==tile.id && cursor.U32()==tile.w && cursor.U32()==tile.h && cursor.U32()==(tile.w+1)/2 && cursor.U32()==(tile.h+1)/2,"old089 identity");
  Need(cursor.D()==raw && cursor.D()==anchor,"old089 source binding");
  (void)ReadStream(&cursor,tile.w==1 && tile.h==1); const auto lp=ReadStream(&cursor,false);
  Need(cursor.pos==record.size(),"old089 trailing data"); Stream result;
  result.bytes.assign(lp.bytes.data(),lp.bytes.data()+lp.bytes.size()); result.bits=lp.bits; result.sha=lp.sha;
  result.coefficients=size_t((tile.w+1)/2)*((tile.h+1)/2)*3; return result;
}
Bytes SerializeTileRecord(const Tile& tile,bool enabled,const Digest& raw,const Digest& anchor,
             const Digest& vertical,const Digest& horizontal,const Stream& low,const Stream& hf) {
  Bytes output{'S','0','9','1','T','I','L','1'};
  for(uint32_t n:{1u,uint32_t(enabled),tile.id,tile.w,tile.h,(tile.w+1)/2,(tile.h+1)/2}) Put32(output,n);
  for(const auto& digest:{raw,anchor,vertical,horizontal}) PutDigest(output,digest);
  WriteStream(&output,low); WriteStream(&output,hf);
  Need(output.size()==260+low.bytes.size()+hf.bytes.size() && output.size()<=kMaxRecordBytes,"091 record size"); return output;
}
std::string StreamReceipt(const Stream& s,const DecodedInfo& d,bool reused) {
  Need(s.bits==d.consumed_bits && s.coefficient_sha==d.coefficient_sha,"stream receipt mismatch");
  std::ostringstream o; o<<"{\"bytes\":"<<s.bytes.size()<<",\"bits\":"<<s.bits<<",\"consumed_bits\":"<<d.consumed_bits
    <<",\"sha256\":\""<<Hex(s.sha)<<"\",\"coefficient_sha256\":\""<<Hex(s.coefficient_sha)<<"\",\"coefficients\":"<<s.coefficients
    <<",\"tree_nodes\":"<<d.tree_nodes<<",\"lz77_enabled\":"<<(d.lz77_enabled?"true":"false");
  if(reused) o<<",\"reused\":true";
  else {
    Need(s.tree.emitted_nodes==d.tree_nodes && s.parent_nodes==d.parent_nodes && s.parent_pairs==d.parent_pairs,"HF tree receipt");
    o<<",\"predictor\":\"Zero\",\"empty\":"<<(s.coefficients?"false":"true")
      <<",\"tree_learned_nodes\":"<<s.tree.learned_nodes<<",\"tree_pruned_splits\":"<<s.tree.pruned_splits
      <<",\"tree_collapsed_subtrees\":"<<s.tree.collapsed_subtrees<<",\"tree_was_pruned\":"<<(s.tree.pruned_splits?"true":"false")
      <<",\"parent_reference_node_count\":"<<s.parent_nodes<<",\"parent_reference_split_channel_pairs\":"<<s.parent_pairs;
    Need(s.target_values==d.target_values && s.target_values==s.coefficients,"logical HF value count");
    o<<",\"target_values\":"<<s.target_values<<",\"decoder_target_values\":"<<d.target_values;
  }
  o<<'}'; return o.str();
}
jxl::Image PrepareDetailChannels(Memory& memory, const Tile& tile,
                        const jxl::Image& transformed, const jxl::Image& low) {
  auto known = KnownShape(&memory.mm, tile.w, tile.h);
  for (size_t c = 0; c < 3; ++c) {
    CopyChannel(transformed.channel[6 + c], &known.channel[3 + c]);
    CopyChannel(transformed.channel[3 + c], &known.channel[9 + c]);
  }
  FillReferenceChannels(&known, low, false);
  auto hlow = ReconstructHorizontalLowpass(low, known);
  FillReferenceChannels(&known, hlow, true);
  CheckKnown(known);
  return known;
}

jxl::Image ComputeLowpassCorrection(Memory& memory, const Tile& tile,
                                     const jxl::Image& lowpass, View parent_rgb) {
  const uint32_t width = (tile.w + 1) / 2;
  const uint32_t height = (tile.h + 1) / 2;
  const auto reference = ToColor(&memory.mm, parent_rgb, width, height);
  auto correction = Shape(&memory.mm, tile.w, tile.h, StreamKind::Correction);
  for (size_t channel = 0; channel < 3; ++channel) {
    for (size_t y = 0; y < height; ++y) {
      for (size_t x = 0; x < width; ++x) {
        correction.channel[channel].Row(y)[x] =
            lowpass.channel[channel].Row(y)[x] - reference.channel[channel].Row(y)[x];
      }
    }
  }
  return correction;
}

struct EncodedTileRecord {
  Bytes bytes;
  size_t correction_bytes;
  size_t detail_bytes;
};

EncodedTileRecord EncodeParentConditionedTile(const Tile& tile, const Bytes& rgb,
                                              const Bytes& parent_rgb) {
  Memory memory;
  auto transformed = ToColor(&memory.mm, AsView(rgb), tile.w, tile.h);
  Check(jxl::FwdSqueeze(transformed, Squeezes(), nullptr), "direct091 forward");
  Need(transformed.channel.size() == 9 && transformed.transform.empty(), "direct091 Squeeze layout");
  const auto lowpass = SelectChannels(transformed, {0, 1, 2});
  const auto correction = ComputeLowpassCorrection(memory, tile, lowpass, AsView(parent_rgb));
  const auto correction_stream = EncodeStream(memory, correction, StreamKind::Correction, tile.id);
  const auto detail_channels = PrepareDetailChannels(memory, tile, transformed, lowpass);
  const auto detail_stream = EncodeDetailStream(memory, detail_channels, tile.id, true);
  auto record = SerializeTileRecord(tile, true, Hash(AsView(rgb)), Hash(AsView(parent_rgb)),
                       ReferenceChannelHash(detail_channels, false), ReferenceChannelHash(detail_channels, true),
                       correction_stream, detail_stream);
  DecodeInfo decoded;
  Need(DecodeTileRecord(AsView(record), AsView(parent_rgb), tile.id, tile.w, tile.h, &decoded) == rgb,
       "immediate direct091 reconstruction");
  Need(correction_stream.coefficient_sha == decoded.low.coefficient_sha &&
       detail_stream.coefficient_sha == decoded.detail.coefficient_sha,
       "direct091 reconstructed coefficients differ");
  return {std::move(record), correction_stream.bytes.size(), detail_stream.bytes.size()};
}

void EncodeTilesFromRgb(const fs::path& spool, const fs::path& output) {
  Need(fs::is_directory(spool) && !fs::is_symlink(spool), "invalid spool");
  Need(!fs::exists(output) && fs::is_directory(output.parent_path()), "fresh091 output required");
  const auto tiles = ReadIndex(spool, false);
  Need(fs::create_directory(output) && fs::create_directory(output / "conditional"),
       "direct091 output create");
  uint64_t correction_bytes = 0, detail_bytes = 0, record_bytes = 0;
  for (const auto& tile : tiles) {
    const auto rgb = ReadFile(spool / tile.rgb_name, kTile * kTile * 3);
    const auto parent_rgb = ReadFile(spool / tile.anchor_name, kTile * kTile * 3 / 4);
    const auto record = EncodeParentConditionedTile(tile, rgb, parent_rgb);
    WriteNew(output / Name("conditional/", tile.id), AsView(record.bytes));
    correction_bytes += record.correction_bytes;
    detail_bytes += record.detail_bytes;
    record_bytes += record.bytes.size();
  }
  std::cout << "{\"ok\":true,\"groups\":" << tiles.size() << ",\"correction_bytes\":" << correction_bytes
            << ",\"detail_bytes\":" << detail_bytes << ",\"record_bytes\":" << record_bytes << "}\n";
}

void EncodeTilesWithStoredCorrection(const fs::path& spool,const fs::path& output) {
  Need(fs::is_directory(spool) && !fs::is_symlink(spool),"invalid spool");
  Need(!fs::exists(output) && fs::is_directory(output.parent_path()),"fresh091 output required");
  const auto tiles=ReadIndex(spool); Need(fs::create_directory(output),"output create");
  for(const auto* name:{"control","conditional"}) Need(fs::create_directory(output/name),"arm create");
  uint64_t totals[2]={},hf_totals[2]={},lp_total=0;
  std::ostringstream receipt; receipt<<"{\"format\":\"st091-native-parent-context-bank-1\",\"standard_jxl\":false,\"abi_version\":1,\"profile\":1,\"groups\":"<<tiles.size()
    <<",\"tree_node_limit\":4095,\"tree_budget_policy\":\"bfs_parent_predictor_v1\",\"predictor\":\"Zero\",\"max_properties\":5,\"color_transform\":\"G,R-G,B-G\",\"squeeze\":\"H_then_V_first3_append\",\"decode_order\":\"LL,V,Hlow,H\",\"uint_method\":\"kBest\",\"lz77_method\":\"kOptimal_per_tile\",\"tiles\":[";
  for(size_t index=0;index<tiles.size();++index) {
    const auto& t=tiles[index]; Memory memory;
    auto rgb=ReadFile(spool/t.rgb_name,kTile*kTile*3),anchor=ReadFile(spool/t.anchor_name,kTile*kTile*3/4);
    Need(rgb.size()==size_t(t.w)*t.h*3 && anchor.size()==size_t((t.w+1)/2)*((t.h+1)/2)*3,"spool byte dimensions");
    const auto raw_sha=Hash(AsView(rgb)),anchor_sha=Hash(AsView(anchor));
    auto old=ReadFile(spool/t.old_name,2*kMaxStreamBytes+196); auto lp=ReadStored089Correction(AsView(old),t,raw_sha,anchor_sha);
    DecodedInfo old_info; auto low=RestoreLowpassCoefficients(memory,{AsView(lp.bytes),lp.bits,lp.sha},AsView(anchor),t.id,t.w,t.h,&old_info);
    lp.coefficient_sha=old_info.coefficient_sha;
    auto transformed=ToColor(&memory.mm,AsView(rgb),t.w,t.h); Check(jxl::FwdSqueeze(transformed,Squeezes(),nullptr),"091 forward");
    Need(CoefficientHash(SelectChannels(transformed,{0,1,2}))==CoefficientHash(low),"old correction does not reconstruct current LL");
    auto known=PrepareDetailChannels(memory,t,transformed,low);
    const auto pv=ReferenceChannelHash(known,false),ph=ReferenceChannelHash(known,true),hf_sha=DetailHash(known);
    const Stream arms[2]={EncodeDetailStream(memory,known,t.id,false),EncodeDetailStream(memory,known,t.id,true)};
    Bytes records[2]; DecodeInfo decoded[2]; std::string paths[2];
    for(size_t mode=0;mode<2;++mode) {
      records[mode]=SerializeTileRecord(t,mode!=0,raw_sha,anchor_sha,pv,ph,lp,arms[mode]);
      Need(DecodeTileRecord(AsView(records[mode]),AsView(anchor),t.id,t.w,t.h,&decoded[mode])==rgb,"immediate091 reconstruction");
      paths[mode]=Name(mode?"conditional/":"control/",t.id); WriteNew(output/paths[mode],AsView(records[mode]));
      totals[mode]+=records[mode].size(); hf_totals[mode]+=arms[mode].bytes.size();
    }
    lp_total+=lp.bytes.size(); if(index) receipt<<',';
    receipt<<"{\"gid\":"<<t.id<<",\"level\":0,\"x\":"<<t.x<<",\"y\":"<<t.y<<",\"w\":"<<t.w<<",\"h\":"<<t.h
      <<",\"anchor_w\":"<<(t.w+1)/2<<",\"anchor_h\":"<<(t.h+1)/2<<",\"raw_sha256\":\""<<Hex(raw_sha)<<"\",\"anchor_sha256\":\""<<Hex(anchor_sha)
      <<"\",\"old_record_sha256\":\""<<Hex(Hash(AsView(old)))<<"\",\"prefix_v_sha256\":\""<<Hex(pv)<<"\",\"prefix_h_sha256\":\""<<Hex(ph)
      <<"\",\"hf_coefficient_sha256\":\""<<Hex(hf_sha)<<"\",\"correction_reused_exact\":true,\"correction\":"<<StreamReceipt(lp,decoded[0].low,true);
    for(size_t mode=0;mode<2;++mode) {
      const std::string name=mode?"conditional":"control";
      receipt<<",\""<<name<<"_path\":\""<<paths[mode]<<"\",\""<<name<<"_bytes\":"<<records[mode].size()
        <<",\""<<name<<"_sha256\":\""<<Hex(Hash(AsView(records[mode])))<<"\",\""<<name<<"\":"<<StreamReceipt(arms[mode],decoded[mode].detail,false);
    }
    receipt<<'}'; std::cerr<<"encoded tile "<<index+1<<'/'<<tiles.size()<<'\n';
  }
  receipt<<"],\"costs\":{\"control_bytes\":"<<totals[0]<<",\"conditional_bytes\":"<<totals[1]<<",\"correction_stream_bytes\":"<<lp_total
    <<",\"control_hf_stream_bytes\":"<<hf_totals[0]<<",\"conditional_hf_stream_bytes\":"<<hf_totals[1]<<",\"record_header_bytes_per_tile\":260}}\n";
  WriteText(output/"receipt.json",receipt.str()); std::cout<<"{\"ok\":true,\"groups\":"<<tiles.size()<<"}\n";
}
void Error(char* out,size_t size,const char* text) {
  if(out && size) { const size_t n=std::min(size-1,std::strlen(text)); std::memcpy(out,text,n); out[n]=0; }
}
template<class F> int Api(char* error,size_t size,F body) {
  if(error && size) error[0]=0;
  try { body(); return 0; } catch(const std::exception& e) { Error(error,size,e.what()); return 1; }
  catch(...) { Error(error,size,"unknown native failure"); return 1; }
}
}  // namespace
extern "C" ST_NATIVE_EXPORT uint32_t st091_abi_version() { return 1; }
extern "C" ST_NATIVE_EXPORT int st091_decode(const uint8_t* tile,size_t size,const uint8_t* anchor,size_t anchor_size,
    uint32_t gid,uint32_t w,uint32_t h,uint8_t* out,size_t out_size,char* error,size_t error_size) {
  return Api(error,error_size,[&] {
    Need(tile && out,"null091 buffer"); Dimensions(w,h); Need(out_size==size_t(w)*h*3,"output size");
    DecodeInfo info; auto rgb=DecodeTileRecord({tile,size},{anchor,anchor_size},gid,w,h,&info); std::memcpy(out,rgb.data(),rgb.size());
  });
}
int main(int argc,char** argv) {
  try {
    stportable::UnsetEnv("ST074_S"); stportable::UnsetEnv("ST074_T");
    if(argc==2 && std::string(argv[1])=="tree_selfcheck") { TreeSelfcheck(); return 0; }
    if(argc==2 && std::string(argv[1])=="context_selfcheck") { ContextSelfcheck(); return 0; }
    if(argc==4 && std::string(argv[1])=="encode") { EncodeTilesWithStoredCorrection(fs::u8path(argv[2]),fs::u8path(argv[3])); return 0; }
    if(argc==4 && std::string(argv[1])=="encode_direct") { EncodeTilesFromRgb(fs::u8path(argv[2]),fs::u8path(argv[3])); return 0; }
    if(argc==5 && std::string(argv[1])=="decode") {
      auto blob=ReadFile(fs::u8path(argv[2]),kMaxRecordBytes); Cursor cursor{AsView(blob)}; auto h=ReadTileRecordHeader(&cursor);
      auto anchor=ReadFile(fs::u8path(argv[3]),kTile*kTile*3/4); DecodeInfo info;
      auto rgb=DecodeTileRecord(AsView(blob),AsView(anchor),h.gid,h.w,h.h,&info); WriteNew(fs::u8path(argv[4]),AsView(rgb));
      std::cout<<"{\"decoded_tiles\":1,\"rgb_bytes\":"<<rgb.size()<<"}\n"; return 0;
    }
    throw std::runtime_error("usage: encode spool newout | encode_direct spool newout | decode record anchor newrgb | tree_selfcheck | context_selfcheck");
  } catch(const std::exception& e) { std::cerr<<"FAIL: "<<e.what()<<'\n'; return 1; }
}
