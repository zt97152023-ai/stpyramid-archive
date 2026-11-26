package stpyramid;

import java.io.IOException;
import java.nio.file.*;
import java.util.*;
import java.util.stream.Stream;

final class Sources {
    record Sample(String id, Path image, Path counts, Path metadata) {
        Map<String, Path> files() {
            var result = new TreeMap<String, Path>();
            result.put("wsis/" + image.getFileName(), image);
            result.put("st/" + counts.getFileName(), counts);
            result.put("metadata/" + metadata.getFileName(), metadata);
            return result;
        }
    }

    static Sample hest(Path root, String id) throws IOException {
        if (!id.matches("[A-Za-z0-9][A-Za-z0-9_-]*")) {
            throw new IOException("Invalid HEST sample ID: " + id);
        }
        Path image = root.resolve("wsis").resolve(id + ".tif");
        if (!Files.isRegularFile(image, LinkOption.NOFOLLOW_LINKS)) {
            image = root.resolve("wsis").resolve(id + ".tiff");
        }
        Path counts = root.resolve("st").resolve(id + ".h5ad");
        Path metadata = root.resolve("metadata").resolve(id + ".json");
        for (Path p : List.of(image, counts, metadata)) {
            if (!Files.isRegularFile(p, LinkOption.NOFOLLOW_LINKS)) {
                throw new IOException("Missing HEST member: " + p);
            }
        }
        return new Sample(id, image, counts, metadata);
    }

    static List<String> listHest(Path root) throws IOException {
        try (var paths = Files.list(root.resolve("st"))) {
            var ids = paths.map(p -> p.getFileName().toString())
                .filter(s -> s.endsWith(".h5ad")).map(s -> s.substring(0, s.length() - 5)).sorted().distinct().toList();
            var complete = new ArrayList<String>();
            for (String id : ids) {
                try { hest(root, id); complete.add(id); }
                catch (IOException ignored) { /* Incomplete samples are excluded from automatic discovery. */ }
            }
            return complete;
        }
    }

    static Map<String, Path> directory(Path root) throws IOException {
        if (!Files.isDirectory(root, LinkOption.NOFOLLOW_LINKS)) {
            throw new IOException("Select a source directory: " + root);
        }
        var entries = new TreeMap<String, Path>();
        try (Stream<Path> paths = Files.walk(root)) {
            for (var it = paths.iterator(); it.hasNext();) {
                Path path = it.next();
                if (path.equals(root)) continue;
                String name = root.relativize(path).toString().replace(path.getFileSystem().getSeparator(), "/");
                if (Files.isDirectory(path, LinkOption.NOFOLLOW_LINKS)) entries.put(name + "/", path);
                else if (Files.isRegularFile(path, LinkOption.NOFOLLOW_LINKS)) entries.put(name, path);
                else throw new IOException("Unsupported source member: " + path);
            }
        }
        return entries;
    }

    static String format(Set<String> names) {
        var formats = new ArrayList<String>();
        for (String name : names) {
            String n = name.toLowerCase(Locale.ROOT);
            String tail = n.substring(n.lastIndexOf('/') + 1);
            if (tail.equals("experiment.xenium") || tail.equals("transcripts.zarr.zip")) add(formats, "Xenium");
            if (n.contains("binned_outputs/") || tail.equals("feature_slice.h5")) add(formats, "Visium HD");
            if (tail.equals("filtered_feature_bc_matrix.h5") || tail.equals("tissue_positions_list.csv") || tail.equals("tissue_positions.csv") || tail.equals("tissue_positions.parquet")) add(formats, "Space Ranger");
            if (tail.endsWith("_exprmat_file.csv") || tail.endsWith("_fov_positions_file.csv")) add(formats, "CosMx");
            if (tail.equals("detected_transcripts.csv") || tail.equals("cell_by_gene.csv")) add(formats, "MERSCOPE");
            if (tail.endsWith(".gef") || tail.endsWith(".gem.gz")) add(formats, "Stereo-seq");
            if (tail.endsWith(".h5ad")) add(formats, "H5AD");
            if (tail.equals("zarr.json") || tail.equals(".zgroup")) add(formats, "Zarr");
        }
        return formats.isEmpty() ? "Source directory" : String.join(", ", formats);
    }

    private static void add(List<String> found, String name) {
        if (!found.contains(name)) found.add(name);
    }
}
