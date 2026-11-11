#include <jxl/encode.h>
#include <cstdio>
#include <cstdlib>
#include <fstream>
#include <iterator>
#include <string>
#include <vector>
#include <stdexcept>

void Check(JxlEncoderStatus s) { if(s!=JXL_ENC_SUCCESS) throw std::runtime_error("JXL encoder API failed"); }
int main(int argc,char** argv) {
  try {
    if(argc!=7) throw std::runtime_error("usage: jxl074 raw.rgb width height output.jxl S|-1 T|-1");
    const int w=std::stoi(argv[2]),h=std::stoi(argv[3]),S=std::stoi(argv[5]),T=std::stoi(argv[6]);
    if(w<1||h<1||w>4096||h>4096||!((S==-1&&T==-1)||((S==0||S==1)&&(T==0||T==1)))) throw std::runtime_error("scope/flag error");
    _putenv_s("ST074_S",S<0?"":S?"1":"0");_putenv_s("ST074_T",T<0?"":T?"1":"0");
    std::ifstream in(argv[1],std::ios::binary);if(!in)throw std::runtime_error("no input");
    std::vector<unsigned char> pixels((std::istreambuf_iterator<char>(in)),{});
    if(pixels.size()!=size_t(w)*h*3)throw std::runtime_error("bad RGB length");
    {std::ifstream exists(argv[4],std::ios::binary);if(exists)throw std::runtime_error("refuse existing output");}
    JxlEncoder* enc=JxlEncoderCreate(nullptr);if(!enc)throw std::runtime_error("no encoder");
    JxlBasicInfo info;JxlEncoderInitBasicInfo(&info);info.xsize=w;info.ysize=h;
    info.bits_per_sample=8;info.num_color_channels=3;info.uses_original_profile=JXL_TRUE;
    Check(JxlEncoderSetBasicInfo(enc,&info));
    JxlColorEncoding color;JxlColorEncodingSetToSRGB(&color,JXL_FALSE);Check(JxlEncoderSetColorEncoding(enc,&color));
    auto* fs=JxlEncoderFrameSettingsCreate(enc,nullptr);
    Check(JxlEncoderSetFrameLossless(fs,JXL_TRUE));Check(JxlEncoderSetFrameDistance(fs,0));
    Check(JxlEncoderFrameSettingsSetOption(fs,JXL_ENC_FRAME_SETTING_EFFORT,9));
    Check(JxlEncoderFrameSettingsSetOption(fs,JXL_ENC_FRAME_SETTING_MODULAR,1));
    Check(JxlEncoderFrameSettingsSetOption(fs,JXL_ENC_FRAME_SETTING_MODULAR_GROUP_SIZE,1));
    Check(JxlEncoderFrameSettingsSetOption(fs,JXL_ENC_FRAME_SETTING_BUFFERING,0));
    JxlPixelFormat fmt={3,JXL_TYPE_UINT8,JXL_NATIVE_ENDIAN,0};
    Check(JxlEncoderAddImageFrame(fs,&fmt,pixels.data(),pixels.size()));JxlEncoderCloseInput(enc);
    std::vector<unsigned char> result(1024*1024);unsigned char* next=result.data();size_t avail=result.size();
    JxlEncoderStatus st=JXL_ENC_NEED_MORE_OUTPUT;
    while(st==JXL_ENC_NEED_MORE_OUTPUT){
      st=JxlEncoderProcessOutput(enc,&next,&avail);
      if(st==JXL_ENC_NEED_MORE_OUTPUT){size_t used=next-result.data();result.resize(result.size()*2);next=result.data()+used;avail=result.size()-used;}
    }
    Check(st);result.resize(next-result.data());JxlEncoderDestroy(enc);
    std::ofstream out(argv[4],std::ios::binary);out.write(reinterpret_cast<const char*>(result.data()),result.size());
    if(!out)throw std::runtime_error("output failure");
    std::printf("{\"encoder_version\":%u,\"bytes\":%zu,\"S\":%d,\"T\":%d}\n",JxlEncoderVersion(),result.size(),S,T);
  }catch(const std::exception& e){std::fprintf(stderr,"FAIL: %s\n",e.what());return 1;}
  return 0;
}
