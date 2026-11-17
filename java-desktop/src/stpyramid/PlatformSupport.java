package stpyramid;

import java.nio.file.Files;
import java.nio.file.Path;
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

    static String nativeCompressionStatus(Path runtime) {
        if (!isWindows()) {
            return "Native Count＋TIFF compression is not bundled for " + osName()
                + " in this release. Source-directory and HEST byte-exact archive modes are available.";
        }
        if (runtime == null || !Files.isRegularFile(runtime.resolve("python/python.exe"))
                || !Files.isRegularFile(runtime.resolve("app/desktop_worker.py"))) {
            return "Windows native runtime is not installed next to the application.";
        }
        return "Windows native Count＋TIFF compression is available.";
    }
}
