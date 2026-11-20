package stpyramid;

import java.io.*;
import java.net.URISyntaxException;
import java.nio.charset.StandardCharsets;
import java.nio.file.*;
import java.util.*;
import java.util.function.Consumer;
import java.util.stream.Collectors;

final class NativeEngine {
    static Path installedRuntime() {
        String configured = System.getenv("STPYRAMID_PORTABLE_RUNTIME");
        if (configured != null && !configured.isBlank()) return Path.of(configured).toAbsolutePath().normalize();
        try {
            Path application = Path.of(NativeEngine.class.getProtectionDomain().getCodeSource().getLocation().toURI());
            Path sibling = application.resolveSibling("runtime");
            if (Files.isRegularFile(sibling.resolve("app/desktop_worker.py"))) return sibling;
        } catch (URISyntaxException ignored) {
            // A source-only installation may need an explicit portable runtime.
        }
        return null;
    }

    static void run(Path portable, Map<String, String> request, Consumer<String> log) throws IOException, InterruptedException {
        String problem = PlatformSupport.nativeRuntimeProblem(portable);
        if (problem != null) {
            throw new IOException(problem + " Use the source-archive workflow where native compression is unavailable.");
        }
        Path python = portable.resolve("python/python.exe");
        Path script = portable.resolve("app/desktop_worker.py");
        var launcher = new ProcessBuilder(python.toString(), "-I", "-B", "-X", "utf8", script.toString());
        var child = launcher.directory(portable.toFile()).redirectErrorStream(true).start();
        boolean complete = false;
        try {
            try (var in = child.outputWriter(StandardCharsets.UTF_8)) {
                in.write(request.entrySet().stream().map(e -> quote(e.getKey()) + ":" + quote(e.getValue()))
                    .collect(Collectors.joining(",", "{", "}\n")));
            }
            boolean result = false;
            try (var out = child.inputReader(StandardCharsets.UTF_8)) {
                for (String line; (line = out.readLine()) != null;) {
                    log.accept(line);
                    if (line.startsWith("{\"result\":")) result = true;
                }
            }
            int exit = child.waitFor();
            complete = true;
            if (exit != 0 || !result) throw new IOException("STPyramid runtime failed (exit " + exit + "). See job log.");
        } finally {
            if (!complete) {
                child.descendants().forEach(ProcessHandle::destroyForcibly);
                child.destroyForcibly();
            }
        }
    }

    private static String quote(String value) {
        var s = new StringBuilder("\"");
        for (char c : value.toCharArray()) {
            if (c <= 31 || c > 126) s.append(String.format("\\u%04x", (int)c));
            else {
                if (c == '"' || c == '\\') s.append('\\');
                s.append(c);
            }
        }
        return s.append('"').toString();
    }
}
