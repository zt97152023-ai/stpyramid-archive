# STPyramid 快速融合版 v1

此版本保存整片 TIFF 的原生 RGB8 金字塔。非纯色 L0 和 L1 图块分别参考原生 L1 和 L2，一次生成低频校正和细节记录。纯色及 L2 起的图块走直接 JPEG XL e9。输出包含全部原生层、索引和源图像元数据。

## Windows 命令

在仓库根目录使用 64 位 CPython 3.12。已核验的编码器和归档解码运行时随此 Git 版本保存。输入为含 3–16 层的 tiled RGB8 TIFF 或 BigTIFF，前两级相邻层须符合向下取整的二倍尺度关系。

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements-windows-tested.txt
New-Item -ItemType Directory -Force .\runs
python -B build_full.py encode .\input.tif .\runs\slide_image --work .\runs\slide_work
python -B build_full.py verify .\runs\slide_image
python -B build_full.py roi .\runs\slide_image .\runs\roi_L1.tif 1 0 0 256 256
python -B build_full.py restore .\runs\slide_image .\runs\restored
```

编码输出目录、工作目录和恢复目录使用新路径。ROI 坐标是所选原生层中的左闭右开像素边界。恢复文件为 `native_levels.tif`、`source.json` 和归档清单。无损范围为各层 RGB、原生几何及源图像元数据。归档计量单位为 `slide_image` 下所有文件的物理字节。

## 代码入口

- **选路与准备输入。** `build_full.py` 将编码交给 `encode_native_pyramid`。它按原生层扫描 TIFF，给 L0 和 L1 的非纯色图块分配条件编码，其余图块采用直接 JPEG XL e9。`write_tile_inputs` 写出目标 RGB、从原生父层抽取的参考 RGB 及图块局部编号。[pyramid_image_archive.py](../../pyramid_image_archive.py) · [native_tile_encoder.py](../../native_tile_encoder.py)
- **生成条件记录。** C++ 命令 `encode_direct` 进入 `EncodeTilesFromRgb`，后者逐块调用 `EncodeParentConditionedTile`。目标块经可逆颜色变换和 Squeeze 得到低频与细节。`ComputeLowpassCorrection` 计算目标低频系数与变换后父层参考之间的差。`EncodeDetailStream` 利用重建时可得到的低频上下文编码细节。`SerializeTileRecord` 封装两段载荷和校验信息，编码器随即试解并校验 RGB。旧命令 `encode` 进入 `EncodeTilesWithStoredCorrection`，从 089 记录读取已编码的低频校正，继续生成旧实验所需的 control 和 conditional 记录。[slide_jxl091.cc](../../native/091/slide_jxl091.cc)
- **组包。** `write_tile_archive` 接收快速版已定路线的记录。旧候选搜索继续通过 `write_package` 按完整记录字节数选路。组包器依图块顺序拼接 `images.bank`，在 `images.index` 写入偏移、长度、路线和 CRC，在 `anchors.index` 写入父块编号，并保存 `source.json` 与 `manifest.json`。清单中的 `build.wrapper_sha256` 记录实际组包源码的 SHA-256。[joint_next.py](../../codec/joint_next.py)
- **索引、读取和恢复。** Reader 先检查图块跨度、路线与父层依赖。`_read_tile_record` 按索引读取码流并检查 CRC。`_decode_referenced_tile` 递归取得所需父块，再调用归档绑定的原生库。091 的 `DecodeTileRecord` 加回低频校正，按依赖顺序恢复细节，经逆 Squeeze 和逆颜色变换还原 RGB，并校验像素哈希。`roi` 只读取相交图块和所需父块，`restore` 按原生层逐条带写出 TIFF。[joint_next.py](../../codec/joint_next.py) · [slide_jxl091.cc](../../native/091/slide_jxl091.cc)
- **旧入口与样本口径。** `encode_image.py` 保留早期 L0 单层实验入口。十张整片的样本与计时口径由 `cohort.py` 定义。[encode_image.py](../../encode_image.py) · [cohort.py](../../cohort.py)

## 从源码构建

在 Visual Studio 2022 x64 Developer PowerShell 中，用仓库里的固定依赖构建原生编码器。

```powershell
cmake -S . -B build -G Ninja -DCMAKE_BUILD_TYPE=Release -DCMAKE_POLICY_VERSION_MINIMUM=3.5
cmake --build build --parallel 2 --target slide_jxl091
```

生成文件为 `build/bin/slide_jxl091.exe`。快速版调用 `bin/slide_jxl091.exe` 编码。读取时使用 `runtime-windows-source/` 中与归档清单匹配的运行时。随附的 [L0、L1 条件记录字节一致性检查](native_clean_build.json) 记录了 v1 封存时的独立源码重建。命令行与运行时验证在 Windows 上完成。

本次重构沿用混合组织、全纯色和奇数边缘三份 TIFF。快速融合与直接 JPEG XL e9 各产生三份归档，四个数据成员均逐字节匹配重构前的输出，清单只更新组包源码哈希，归档总字节数一致。新旧入口交叉读取时，各原生层的像素哈希一致。重编的 C++ 解码命令和 DLL 分别还原了其中 18 条旧条件记录。此回归覆盖三份测试 TIFF，十张整片结果对应下方封存版本的实验记录。

## 十张整片验证

- [方法和计量范围](METHODS.md) · [逐片大小与耗时](RESULTS.md) · [机器可读汇总](RESULT.json)
- [样本、来源和源文件校验值](../../COHORT.json) · [逐样本证据](samples) · [受验证源码与运行时校验值](validated_source.json)

`cohort.py` 读取根目录 `COHORT.json`。先将各源 TIFF 和原方案图像归档放入清单所列路径，并在 `historical-runtime/` 提供与历史归档绑定一致的运行时，再运行 `python -B cohort.py`。队列采用原 Windows 工作站的逻辑核心编号。单张使用 `python -B cohort.py --index 0 --cpu 0`，`--index` 从 0 开始。发布的逐样本证据保留编码、读取、ROI 与字节账本，移除了本机归档路径。
