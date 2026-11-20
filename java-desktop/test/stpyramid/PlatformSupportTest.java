package stpyramid;

import java.nio.file.Files;
import java.nio.file.Path;

final class PlatformSupportTest {
    public static void main(String[] args) throws Exception {
        if (PlatformSupport.nativeRuntimeProblem(null, false) == null) {
            throw new AssertionError("Non-Windows native backend must be reported unavailable");
        }
        if (PlatformSupport.nativeRuntimeProblem(null, true) == null) {
            throw new AssertionError("Missing Windows runtime must be reported");
        }

        Path root = Files.createTempDirectory("stpyramid-runtime-contract-");
        try {
            String first = PlatformSupport.requiredNativeRuntimeFiles().get(0);
            Path missing = root.resolve(first);
            Files.createDirectories(missing.getParent());
            Files.writeString(missing, "test");
            String problem = PlatformSupport.nativeRuntimeProblem(root, true);
            if (problem == null || !problem.contains("app/desktop_worker.py")) {
                throw new AssertionError("Incomplete runtime should identify its first missing member: " + problem);
            }

            for (String relative : PlatformSupport.requiredNativeRuntimeFiles()) {
                Path file = root.resolve(relative);
                Files.createDirectories(file.getParent());
                if (!Files.exists(file)) Files.writeString(file, "test");
            }
            if (PlatformSupport.nativeRuntimeProblem(root, true) != null) {
                throw new AssertionError("Complete runtime layout should pass the file-presence check");
            }
        } finally {
            try (var walk = Files.walk(root)) {
                walk.sorted((a, b) -> b.compareTo(a)).forEach(path -> {
                    try { Files.deleteIfExists(path); }
                    catch (Exception error) { throw new RuntimeException(error); }
                });
            }
        }
        Path legacyRoot = Files.createTempDirectory("stpyramid-legacy-runtime-");
        try {
            for (String relative : PlatformSupport.requiredApplicationFiles()) {
                Path file = legacyRoot.resolve(relative);
                if (relative.equals("app/bin/context_codec.exe")) {
                    file = legacyRoot.resolve("app/bin/slide_jxl091.exe");
                }
                Files.createDirectories(file.getParent());
                Files.writeString(file, "test");
            }
            for (String relative : PlatformSupport.legacyCodecRuntimeFiles()) {
                Path file = legacyRoot.resolve(relative);
                Files.createDirectories(file.getParent());
                Files.writeString(file, "test");
            }
            if (PlatformSupport.nativeRuntimeProblem(legacyRoot, true) != null) {
                throw new AssertionError("Legacy runtime layout must remain readable during migration");
            }
        } finally {
            try (var walk = Files.walk(legacyRoot)) {
                walk.sorted((a, b) -> b.compareTo(a)).forEach(path -> {
                    try { Files.deleteIfExists(path); }
                    catch (Exception error) { throw new RuntimeException(error); }
                });
            }
        }
        System.out.println("PlatformSupport runtime layout checks passed");
    }
}
