package stpyramid;

import java.io.IOException;
import java.nio.file.*;
import java.util.*;
import java.util.function.Consumer;

public final class Main {
    private Main() {}

    public static void main(String[] args) {
        if (args.length == 0) {
            if (java.awt.GraphicsEnvironment.isHeadless()) { System.out.println(help()); return; }
            javax.swing.SwingUtilities.invokeLater(Desktop::show);
            return;
        }
        try { execute(args, System.out::println); }
        catch (Exception e) {
            System.err.println(e.getMessage());
            System.exit(1);
        }
    }

    static void execute(String[] args, Consumer<String> log) throws Exception {
        switch (args[0]) {
            case "help", "--help" -> log.accept(help());
            case "capabilities" -> {
                length(args, 1, 1);
                log.accept("OS: " + PlatformSupport.osName());
                log.accept("Java: " + PlatformSupport.javaVersion());
                log.accept("Native compression: " + PlatformSupport.nativeCompressionStatus(NativeEngine.installedRuntime()));
                log.accept("Source archive: available on every supported Java desktop platform.");
            }
            case "list-hest" -> {
                length(args, 2, 2);
                Sources.listHest(path(args[1])).forEach(log);
            }
            case "source" -> {
                length(args, 3, 3);
                Path input = path(args[1]);
                Path output = path(args[2]);
                if (resolved(output).startsWith(input.toRealPath())) throw new IOException("Choose an output outside the source directory");
                var files = Sources.directory(input);
                SourceArchive.pack(files, Sources.format(files.keySet()), output, log);
            }
            case "hest-source" -> {
                length(args, 4, 4);
                Path root = path(args[1]), output = path(args[3]);
                var sample = Sources.hest(root, args[2]);
                SourceArchive.pack(sample.files(), "HEST " + sample.id(), output, log);
            }
            case "hest", "hest-fused" -> {
                length(args, 4, 5);
                encodeHest(path(args[1]), args[2], path(args[3]), runtime(args, 4), args[0].equals("hest-fused"), log);
            }
            case "paired", "paired-fused" -> {
                length(args, 5, 6);
                encodePaired(path(args[1]), path(args[2]), path(args[3]), path(args[4]), runtime(args, 5), args[0].equals("paired-fused"), log);
            }
            case "hest-batch", "hest-batch-fused" -> {
                length(args, 4, Integer.MAX_VALUE);
                Path root = path(args[1]), output = path(args[2]), runtime = path(args[3]);
                var ids = args.length == 4 ? Sources.listHest(root) :
                    List.copyOf(new LinkedHashSet<>(Arrays.asList(args).subList(4, args.length)));
                if (ids.isEmpty()) throw new IOException("No complete HEST samples found");
                Files.createDirectories(output);
                int passed = 0;
                var failures = new ArrayList<String>();
                for (String id : ids) {
                    try {
                        Sources.hest(root, id);
                        log.accept("HEST sample " + id);
                        encodeHest(root, id, output.resolve(id + ".stpyramid"), runtime, args[0].equals("hest-batch-fused"), log);
                        passed++;
                    } catch (IOException ex) {
                        log.accept(id + ": " + ex.getMessage());
                        failures.add(id);
                    }
                }
                log.accept("Batch created " + passed + "/" + ids.size() + " new archives");
                if (!failures.isEmpty()) throw new IOException("Samples requiring attention: " + String.join(", ", failures));
            }
            case "image" -> {
                length(args, 3, 4);
                Path image = path(args[1]), dest = path(args[2]);
                SourceArchive.fresh(dest);
                NativeEngine.run(runtime(args, 3), Map.of("action", "encode", "mode", "image", "image", image.toString(), "output", dest.toString()), log);
            }
            case "verify", "restore" -> {
                boolean restore = args[0].equals("restore");
                int required = restore ? 3 : 2;
                length(args, required, required + 1);
                Path archive = path(args[1]);
                if (Files.isRegularFile(archive, LinkOption.NOFOLLOW_LINKS)) {
                    if (restore) SourceArchive.restore(archive, path(args[2]), log);
                    else SourceArchive.verify(archive, log);
                } else if (Files.isDirectory(archive, LinkOption.NOFOLLOW_LINKS)) {
                    var request = new HashMap<>(Map.of("action", args[0], "archive", archive.toString()));
                    if (restore) {
                        Path dest = path(args[2]);
                        SourceArchive.fresh(dest);
                        request.put("output", dest.toString());
                    }
                    NativeEngine.run(runtime(args, required), request, log);
                } else throw new IOException("Select an archive file or a native archive directory");
            }
            default -> throw new IOException(help());
        }
    }

    private static void encodeHest(Path root, String id, Path dest, Path runtime, boolean fused, Consumer<String> log) throws Exception {
        var s = Sources.hest(root, id);
        encodePaired(s.image(), s.counts(), s.metadata(), dest, runtime, fused, log);
    }

    private static void encodePaired(Path image, Path counts, Path metadata, Path dest, Path runtime, boolean fused,
                                     Consumer<String> log) throws IOException, InterruptedException {
        SourceArchive.fresh(dest);
        NativeEngine.run(runtime, Map.of("action", "encode", "mode", fused ? "integrated-fused" : "integrated", "image", image.toString(),
            "counts", counts.toString(), "metadata", metadata.toString(), "output", dest.toString()), log);
    }

    private static void length(String[] args, int min, int max) throws IOException {
        if (args.length < min || args.length > max) throw new IOException(help());
    }

    private static Path path(String raw) { return Path.of(raw).toAbsolutePath().normalize(); }

    private static Path runtime(String[] args, int index) {
        return args.length > index && !args[index].isBlank() ? path(args[index]) : NativeEngine.installedRuntime();
    }

    private static Path resolved(Path path) throws IOException {
        if (Files.exists(path, LinkOption.NOFOLLOW_LINKS)) return path.toRealPath();
        return resolved(path.getParent()).resolve(path.getFileName());
    }

    private static String help() {
        return """
            STPyramid Java Desktop
              [no arguments]                             Open desktop app
              capabilities                              Show platform and codec support
              list-hest HEST_ROOT                        Discover complete HEST trios
              hest HEST_ROOT ID OUTPUT [PORTABLE_RUNTIME]  Count + native TIFF compression
              hest-batch HEST_ROOT OUTPUT_DIR PORTABLE_RUNTIME [ID ...]
              paired TIFF H5AD JSON OUTPUT [PORTABLE_RUNTIME]
              Add -fused to hest, hest-batch or paired to select fast TIFF fusion.
              hest-source HEST_ROOT ID OUTPUT.stsource   Byte-exact HEST trio
              source INPUT_DIR OUTPUT.stsource           Byte-exact source-directory compression
              image TIFF OUTPUT [PORTABLE_RUNTIME]       Fast native TIFF compression
              verify ARCHIVE [PORTABLE_RUNTIME]
              restore ARCHIVE OUTPUT [PORTABLE_RUNTIME]
            """;
    }
}
