"""Localized desktop labels and stage names. Jobs use language-independent IDs."""

LANGUAGES = {"English": "en", "简体中文": "zh"}
MODES = {"integrated": "Count + image (maximum compression)", "image": "Image only (fast fused)"}
ACTIONS = {"encode": "Compress", "verify": "Verify", "restore": "Restore"}

CHINESE = {
    "STPyramid · Spatial transcriptomics archive": "STPyramid · 空间转录组归档",
    "Lossless spatial transcriptomics compression and archiving": "空间转录组无损压缩与归档",
    "Compress": "压缩", "Verify": "核验", "Restore": "恢复",
    "Compress & archive": "压缩归档", "Verify & restore": "核验与恢复",
    "Job queue": "任务队列", "Job": "任务", "Status": "状态", "Archive size": "完整包大小",
    "Start queue": "开始队列", "Cancel current": "取消当前", "Remove job": "移除任务", "Open folder": "打开目录",
    "Waiting for jobs": "等待添加任务", "Ready to start": "可开始队列", "Queue finished": "队列已结束",
    "Queue paused": "队列已暂停", "Activity log": "运行日志", "Browse…": "浏览…",
    "Compression mode": "压缩模式", "Count + image (maximum compression)": "Count＋图像（极致压缩）",
    "Image only (fast fused)": "仅图像（快速融合）", "Whole-slide image (TIFF / BigTIFF)": "整片图像（TIFF / BigTIFF）",
    "Expression and coordinates (H5AD)": "表达与坐标（H5AD）", "Metadata attachment (optional JSON)": "附加元数据（JSON，可选）",
    "Archive output folder": "归档输出目录", "Archive name": "归档名称",
    "Keep intermediate files after success": "保留成功任务的中间文件", "Add compression job": "加入压缩队列",
    "Archive folder (contains manifest.json)": "归档目录（含 manifest.json）", "Restore output path": "恢复输出路径",
    "Decoder runtime folder (optional)": "历史归档运行时目录（可选）", "Add verification job": "加入核验队列",
    "Add restoration job": "加入恢复队列", "All files": "所有文件",
    "Select a valid TIFF file.": "请选择有效的 TIFF 文件。",
    "Select a valid H5AD file and, optionally, a JSON attachment.": "请选择有效的 H5AD 文件和可选的 JSON 附件。",
    "Enter an output folder and a valid archive name.": "请填写输出目录和有效的归档名称。",
    "Select an archive folder containing manifest.json.": "请选择含 manifest.json 的归档目录。",
    "Enter a restore output path.": "请填写恢复输出路径。",
    "The output path already exists or is queued. Choose a new path.": "输出路径已存在或已在队列中。请选择新路径。",
    "Queued": "等待", "Running": "处理中", "Completed": "已完成", "Cancelled": "已取消", "Failed": "失败",
    "Cancelling the current job": "正在取消当前任务", "The job is still running because cancellation failed.": "取消失败，当前任务仍在运行",
    "Windows could not terminate the current job.": "Windows 未能结束当前任务。",
    "Intermediate files retained at {path}": "中间文件保存在 {path}", "Exit STPyramid": "退出 STPyramid",
    "Cancel the current job and exit?": "取消当前任务并退出？",
    "Select a TIFF image file.": "请选择 TIFF 图像文件。",
    "Select an H5AD file and a valid JSON attachment.": "请选择 H5AD 文件和有效的 JSON 附件。",
    "Unknown compression mode.": "未知压缩模式。", "Lossless image verification failed.": "图像无损核验失败。",
    "Archive saved. Could not remove the temporary folder. {error}": "归档已保存，临时目录清理失败。{error}",
    "Select an archive folder created by STPyramid.": "请选择由 STPyramid 写出的归档目录。",
    "Archive verification failed.": "归档核验失败。", "Unknown archive operation.": "未知归档操作。",
    "Could not download the pinned Count runtime. Check your internet connection, then start the job again. {error}":
        "无法下载固定版本的 Count 运行时。请检查网络连接后重新开始任务。{error}",
}

STAGES = {
    "native_image_census": ("Scan native image levels", "扫描原生图像层"),
    "read_source_and_census": ("Scan native image levels", "扫描原生图像层"),
    "verify_census": ("Check tile metadata", "核对图块信息"),
    "pin_runtimes": ("Check decoder runtimes", "检查解码运行时"),
    "prepare_count_runtime": ("Download and verify the pinned Count runtime", "下载并核验固定版本的 Count 运行时"),
    "shared_count_encode": ("Encode spatial Count blocks", "编码 Count 空间块"),
    "direct_JXL9_all_tiles_including_immediate_decode_check": ("Encode direct JPEG XL tiles", "编码直接 JPEG XL 图块"),
    "P089_direct_dependency_package_assembly": ("Write direct reference package", "写入直接编码参考"),
    "P089_correction_source_package_assembly": ("Assemble low-frequency correction source", "组装低频校正来源"),
    "L0_091_all_candidate_adapter_including_checks": ("Generate L0 conditional candidates", "生成 L0 条件候选"),
    "L0_HF_all_candidate_adapter_including_checks": ("Generate L0 HF candidates", "生成 L0 HF 候选"),
    "L1_091_all_candidate_adapter_including_checks": ("Generate L1 conditional candidates", "生成 L1 条件候选"),
    "retained_089_091_strict_paid_record_selection": ("Compare full candidate sizes", "比较候选记录大小"),
    "direct_selection_and_image_package_assembly": ("Assemble direct image archive", "组装直接编码归档"),
    "hf_l1_conditional_selection_and_image_package_assembly": ("Assemble maximum-compression image archive", "组装极致版图像归档"),
    "collect_conditional_and_encode_direct_tiles": ("Collect conditional records and encode remaining tiles", "汇总条件记录并编码其余图块"),
    "assemble_image_package": ("Write image archive", "写入图像归档"),
    "assemble_complete_archive": ("Assemble Count and image archive", "组装 Count＋图像归档"),
    "verify_counts_pixels_and_files": ("Verify Count, pixels and metadata", "无损核验 Count、图像与元数据"),
    "verify_image_archive": ("Verify every native image level", "无损核验所有图像层"),
    "verify_archive": ("Verify archive", "核验归档"),
    "restore_archive": ("Restore archive contents", "恢复归档内容"),
}
for level in (0, 1):
    STAGES.update({
        f"prepare_native_L{level}": (f"Prepare L{level} parent references", f"准备 L{level} 父层参考"),
        f"encode_fused_L{level}": (f"Encode L{level} conditional tiles", f"编码 L{level} 条件图块"),
        f"P089_L{level}_source_spooling": (f"Prepare L{level} parent references", f"准备 L{level} 父层参考"),
        f"P089_L{level}_native_all_candidates": (f"Generate L{level} 089 candidates", f"生成 L{level} 的 089 候选"),
        f"P089_L{level}_native_receipt_validation": (f"Check L{level} 089 candidates", f"核验 L{level} 的 089 候选"),
    })


def translate(language, text, **values):
    label = CHINESE.get(text, text) if language == "zh" else text
    return label.format(**values) if values else label


def stage_name(language, key):
    labels = STAGES.get(key, (key, key))
    return labels[language == "zh"]
