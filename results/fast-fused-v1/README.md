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

- [pyramid_image_archive.py](../../pyramid_image_archive.py) 管理扫描、图块选路、记录收集和组包。`build_full.py` 提供原有命令入口。
- [native_tile_encoder.py](../../native_tile_encoder.py) 生成目标与父层参考的原生输入，并启动 C++ 编码器。归档收集条件记录时，使用实际送入编码器的图块 ID。
- [encode_image.py](../../encode_image.py) 用于早期 L0 单层实验。十张整片的口径由 [cohort.py](../../cohort.py) 定义，核验和读回计时由该脚本调用归档读取器完成。

## 从源码构建

在 Visual Studio 2022 x64 Developer PowerShell 中，用仓库里的固定依赖构建原生编码器。

```powershell
cmake -S . -B build -G Ninja -DCMAKE_BUILD_TYPE=Release -DCMAKE_POLICY_VERSION_MINIMUM=3.5
cmake --build build --parallel 2 --target slide_jxl091
```

生成文件为 `build/bin/slide_jxl091.exe`。如需替换本地测试用编码器，复制到 `bin/slide_jxl091.exe`。已有归档读取时使用它们绑定的运行时。独立源码重建通过了 [L0、L1 条件记录的字节一致性检查](native_clean_build.json)。本版本的命令行与运行时验证在 Windows 上完成。

## 十张整片验证

- [方法和计量范围](METHODS.md) · [逐片大小与耗时](RESULTS.md) · [机器可读汇总](RESULT.json)
- [样本、来源和源文件校验值](../../COHORT.json) · [逐样本证据](samples) · [受验证源码与运行时校验值](validated_source.json)

`cohort.py` 读取根目录 `COHORT.json`。先将各源 TIFF 和原方案图像归档放入清单所列路径，并在 `historical-runtime/` 提供与历史归档绑定一致的运行时，再运行 `python -B cohort.py`。队列采用原 Windows 工作站的逻辑核心编号。单张使用 `python -B cohort.py --index 0 --cpu 0`，`--index` 从 0 开始。发布的逐样本证据保留编码、读取、ROI 与字节账本，移除了本机归档路径。
