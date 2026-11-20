package stpyramid;

import java.nio.file.Files;
import java.nio.file.Path;
import java.util.List;
import java.util.Locale;

final class PlatformSupport {
    private PlatformSupport() {}

    static String osName() {
        String value = System.getProperty("os.name", "unknown").toLowerCase(Locale.ROOT);
        if (value.contains("win")) return "Windows";
        if (value.contains("mac") || value.contains("darwin")) return "macOS";
        if (value.contains("linux")) return "Linux";
        return value;
    }

    static boolean isWindows() {
        return osName().equals("Windows");
    }

    static String javaVersion() {
        return System.getProperty("java.version", "unknown");
    }

    /**
     * Return an actionable problem if the portable Windows runtime is incomplete.
     * ABI and SHA-256 receipt validation remains the native adapter's responsibility.
     */
    static String nativeRuntimeProblem(Path runtime) {
        return nativeRuntimeProblem(runtime, isWindows());
    }

    static String nativeRuntimeProblem(Path runtime, boolean windows) {
        if (!windows) {
            return "Native Count＋TIFF compression is currently qualified for Windows only.";
        }
        if (runtime == null) {
            return "Windows native runtime is not installed. Select the extracted STPyramid runtime folder.";
        }

        Path root = runtime.toAbsolutePath().normalize();
        List<String> required = requiredNativeRuntimeFiles();
        for (String relative : required) {
            Path file = root.resolve(relative).normalize();
            if (!file.startsWith(root) || !Files.isRegularFile(file)) {
                return "Windows native runtime is incomplete: missing " + relative;
            }
        }
        return null;
    }

    static List<String> requiredNativeRuntimeFiles() {
        return List.of(
            "python/python.exe",
            "app/desktop_worker.py",
            "app/runtime_config.py",
            "app/task_contract.py",
            "app/native_tile_encoder.py",
            "app/pyramid_image_archive.py",
            "app/codec/integrated_encode.py",
            "app/codec/archive_image_encode.py",
            "app/codec/container_compare.py",
            "app/codec/joint_next.py",
            "app/codec/stbaseline/grouped_count_encode.py",
            "app/codec/stbaseline/grouped_count.py",
            "app/codec/stbaseline/_shared_vendor/SOURCE.json",
            "app/bin/slide_jxl091.exe",
            "app/runtime-windows-source/089/slide_jxl089.exe",
            "app/runtime-windows-source/089/slide_jxl089.dll",
            "app/runtime-windows-source/089/build_receipt.json",
            "app/runtime-windows-source/091/slide_jxl091.exe",
            "app/runtime-windows-source/091/slide_jxl091.dll",
            "app/runtime-windows-source/091/build_receipt.json",
            "app/runtime-windows-source/hf/slide_hf1.exe",
            "app/runtime-windows-source/hf/slide_hf1.dll",
            "app/runtime-windows-source/hf/build_receipt.json"
        );
    }

    static String nativeCompressionStatus(Path runtime) {
        if (!isWindows()) {
            return "Native Count＋TIFF compression is not bundled for " + osName()
                + " in this release. Source-directory and HEST byte-exact archive modes are available.";
        }
        String problem = nativeRuntimeProblem(runtime);
        if (problem != null) return problem;
        return "Windows native runtime files found. Codec ABI and file pins are checked before use; "
            + "the pinned Count source is verified before each Count job.";
    }
}
