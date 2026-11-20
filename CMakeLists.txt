cmake_minimum_required(VERSION 3.16)
project(ST_HF_L1_NATIVE_HANDOFF VERSION 1.0 LANGUAGES C CXX)

if(NOT APPLE AND NOT WIN32)
  message(FATAL_ERROR "The supplied platform layer is scoped to macOS and Windows.")
endif()
set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)
set(CMAKE_POSITION_INDEPENDENT_CODE ON)
set(CMAKE_RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/bin")
set(CMAKE_LIBRARY_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/lib")
set(CMAKE_ARCHIVE_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/lib")

# Use the handoff's pinned sources. No package manager or network fetch here.
set(BUILD_SHARED_LIBS OFF CACHE BOOL "Static upstream dependencies" FORCE)
set(BUILD_TESTING OFF CACHE BOOL "No upstream test build" FORCE)
foreach(feature TOOLS DEVTOOLS DOXYGEN MANPAGES BENCHMARK EXAMPLES JNI SJPEG OPENEXR
                VIEWERS TCMALLOC PLUGINS FUZZERS COVERAGE)
  set(JPEGXL_ENABLE_${feature} OFF CACHE BOOL "Minimal offline libjxl build" FORCE)
endforeach()
set(JPEGXL_ENABLE_SKCMS ON CACHE BOOL "Use bundled skcms" FORCE)
set(JPEGXL_FORCE_SYSTEM_HWY OFF CACHE BOOL "Use bundled Highway" FORCE)
set(JPEGXL_FORCE_SYSTEM_BROTLI OFF CACHE BOOL "Use bundled Brotli" FORCE)
set(JPEGXL_ENABLE_LTO OFF CACHE BOOL "Portable build without LTO" FORCE)
set(HWY_ENABLE_TESTS OFF CACHE BOOL "No upstream test build" FORCE)
set(HWY_ENABLE_EXAMPLES OFF CACHE BOOL "No upstream example build" FORCE)
if(NOT EXISTS "${CMAKE_CURRENT_SOURCE_DIR}/upstream/libjxl/CMakeLists.txt")
  message(FATAL_ERROR "Pinned upstream/libjxl sources are missing; restore the complete handoff.")
endif()
add_subdirectory(upstream/libjxl "${CMAKE_BINARY_DIR}/upstream/libjxl" EXCLUDE_FROM_ALL)
find_package(Threads REQUIRED)

function(st_native name)
  set(sources ${ARGN})
  add_library(${name}_objects OBJECT ${sources})
  target_include_directories(${name}_objects PRIVATE
    "${CMAKE_CURRENT_SOURCE_DIR}/native/portable"
    "${CMAKE_CURRENT_SOURCE_DIR}/upstream/libjxl"
    "${CMAKE_CURRENT_SOURCE_DIR}/upstream/libjxl/lib/include"
    "${CMAKE_CURRENT_SOURCE_DIR}/upstream/libjxl/third_party/highway"
    "${CMAKE_BINARY_DIR}/upstream/libjxl/lib/include")
  target_compile_definitions(${name}_objects PRIVATE JXL_STATIC_DEFINE JXL_CMS_STATIC_DEFINE)
  target_link_libraries(${name}_objects PRIVATE jxl jxl_cms hwy Threads::Threads)
  if(MSVC)
    target_compile_options(${name}_objects PRIVATE /EHsc)
    target_compile_definitions(${name}_objects PRIVATE _CRT_SECURE_NO_WARNINGS)
  endif()
  add_executable(${name} $<TARGET_OBJECTS:${name}_objects>)
  add_library(${name}_shared SHARED $<TARGET_OBJECTS:${name}_objects>)
  set_target_properties(${name}_shared PROPERTIES OUTPUT_NAME ${name})
  foreach(target ${name} ${name}_shared)
    target_link_libraries(${target} PRIVATE jxl jxl_cms hwy brotlienc brotlidec brotlicommon Threads::Threads)
    if(WIN32)
      target_link_libraries(${target} PRIVATE bcrypt)
    endif()
  endforeach()
endfunction()

st_native(slide_jxl089 native/089/slide_jxl089.cc)
st_native(slide_jxl091 native/091/slide_jxl091.cc
  native/091/vendor091/enc_known.cc native/091/vendor091/dec_known.cc)
st_native(slide_hf1 native/hf1/slide_hf1.cc
  native/hf1/vendorhf/enc_known.cc native/hf1/vendorhf/dec_known.cc)

add_executable(st_platform_selftest native/portable/platform_selftest.cc)
target_include_directories(st_platform_selftest PRIVATE native/portable)
if(WIN32)
  target_link_libraries(st_platform_selftest PRIVATE bcrypt)
endif()

# New handoff-only raw-RGB effort-11 baseline, using the same vendored libjxl.
add_executable(jxl11_cli native/jxl11_cli.cc)
target_link_libraries(jxl11_cli PRIVATE jxl jxl_cms Threads::Threads)
target_compile_definitions(jxl11_cli PRIVATE JXL_STATIC_DEFINE JXL_CMS_STATIC_DEFINE)
if(WIN32)
  target_link_libraries(jxl11_cli PRIVATE bcrypt)
endif()

# Stable, descriptive build entry points. Historical target and ABI names remain
# internal implementation details so existing runtime bundles stay readable.
add_custom_target(stpyramid_anchor
  DEPENDS slide_jxl089 slide_jxl089_shared)
add_custom_target(stpyramid_context
  DEPENDS slide_jxl091 slide_jxl091_shared)
add_custom_target(stpyramid_hf_detail
  DEPENDS slide_hf1 slide_hf1_shared)
add_custom_target(stpyramid_native_codecs
  DEPENDS stpyramid_anchor stpyramid_context stpyramid_hf_detail)
