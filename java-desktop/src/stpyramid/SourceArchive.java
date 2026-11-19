package stpyramid;

import java.io.*;
import java.nio.file.*;
import java.nio.file.attribute.BasicFileAttributes;
import java.security.*;
import java.util.*;
import java.util.function.Consumer;
import java.util.zip.*;

final class SourceArchive {
    private static final String MANIFEST = "STPYRAMID-SOURCE-1";
    private static final String DATA = "data/";
    private static final int MAX_RECORDS = 2_000_000;
    // Bound attacker-controlled metadata before DataInputStream allocates strings.
    private static final long MAX_MANIFEST_BYTES = 256L * 1024 * 1024;
    private static final byte[] EMPTY = new byte[32];

    private record Member(String name, long length, byte[] digest) {}

    static void pack(Map<String, Path> sources, String format, Path output, Consumer<String> log) throws IOException {
        fresh(output);
        var parent = output.getParent();
        Files.createDirectories(parent);
        var staged = Files.createTempFile(parent, ".stsource-", ".tmp");
        try {
            var members = new ArrayList<Member>();
            if (sources.size() > MAX_RECORDS) throw new IOException("Too many input members");
            var names = new HashSet<String>();
            try (var out = new ZipOutputStream(new BufferedOutputStream(Files.newOutputStream(staged)))) {
                int index = 0;
                for (var source : sources.entrySet()) {
                    index++;
                    checkName(source.getKey());
                    if (!names.add(source.getKey())) throw new IOException("Duplicate source member: " + source.getKey());
                    out.putNextEntry(new ZipEntry(DATA + source.getKey()));
                    if (source.getKey().endsWith("/")) {
                        members.add(new Member(source.getKey(), -1, EMPTY));
                    } else {
                        if (sources.size() < 100 || index % 100 == 0 || index == sources.size()) {
                            log.accept("Compressing " + index + "/" + sources.size() + " " + source.getKey());
                        }
                        var digest = digest();
                        var before = Files.readAttributes(source.getValue(), BasicFileAttributes.class, LinkOption.NOFOLLOW_LINKS);
                        if (!before.isRegularFile()) throw new IOException("Source is no longer a regular file: " + source.getValue());
                        long size;
                        try (var in = new DigestInputStream(Files.newInputStream(source.getValue()), digest)) {
                            size = in.transferTo(out);
                        }
                        var after = Files.readAttributes(source.getValue(), BasicFileAttributes.class, LinkOption.NOFOLLOW_LINKS);
                        if (!after.isRegularFile() || size != before.size() || size != after.size() ||
                            !before.lastModifiedTime().equals(after.lastModifiedTime()) ||
                            !Objects.equals(before.fileKey(), after.fileKey())) {
                            throw new IOException("Source changed during compression: " + source.getValue());
                        }
                        members.add(new Member(source.getKey(), size, digest.digest()));
                    }
                    out.closeEntry();
                }
                out.putNextEntry(new ZipEntry(MANIFEST));
                var meta = new DataOutputStream(out);
                meta.writeUTF(format);
                meta.writeInt(members.size());
                for (Member m : members) {
                    meta.writeUTF(m.name());
                    meta.writeLong(m.length());
                    meta.write(m.digest());
                }
                out.closeEntry();
            }
            verify(staged, log);
            Files.move(staged, output);
            log.accept("Verified source archive: " + output + " (" + Files.size(output) + " bytes)");
        } finally {
            Files.deleteIfExists(staged);
        }
    }

    static void verify(Path path, Consumer<String> log) throws IOException {
        read(path, null, log);
    }

    static void restore(Path path, Path output, Consumer<String> log) throws IOException {
        fresh(output);
        Files.createDirectories(output.getParent());
        Path staged = Files.createTempDirectory(output.getParent(), ".stsource-restore-");
        try {
            read(path, staged, log);
            Files.move(staged, output);
            log.accept("Restored " + output);
        } finally {
            if (Files.exists(staged, LinkOption.NOFOLLOW_LINKS)) {
                if (!staged.toRealPath().getParent().equals(output.getParent().toRealPath())
                    || !staged.getFileName().toString().startsWith(".stsource-restore-")) {
                    throw new IOException("Unexpected staging path: " + staged);
                }
                try (var paths = Files.walk(staged)) {
                    for (Path p : paths.sorted(Comparator.reverseOrder()).toList()) Files.delete(p);
                }
            }
        }
    }

    private static void read(Path path, Path directory, Consumer<String> log) throws IOException {
        try (var zip = new ZipFile(path.toFile())) {
            var entries = new HashMap<String, ZipEntry>();
            for (var it = zip.entries(); it.hasMoreElements();) {
                var z = it.nextElement();
                if (entries.putIfAbsent(z.getName(), z) != null) throw new IOException("Duplicate ZIP entry: " + z.getName());
            }
            var manifest = entries.remove(MANIFEST);
            if (manifest == null || manifest.getSize() < 0 || manifest.getSize() > MAX_MANIFEST_BYTES) {
                throw new IOException("Missing or oversized STPyramid source manifest");
            }
            try (var meta = new DataInputStream(new BufferedInputStream(zip.getInputStream(manifest)))) {
                String format = meta.readUTF();
                if (!MANIFEST.equals(format)) throw new IOException("Unsupported source archive format: " + format);
                log.accept("Verifying " + format);
                int count = meta.readInt();
                if (count < 0 || count > MAX_RECORDS || count != entries.size()) throw new IOException("Unexpected ZIP member count");
                var names = new HashSet<String>();
                for (int i = 0; i < count; i++) {
                    String name = meta.readUTF();
                    checkName(name);
                    if (!names.add(name)) throw new IOException("Duplicate normalized archive member: " + name);
                    long length = meta.readLong();
                    byte[] expectedDigest = meta.readNBytes(32);
                    var entry = entries.remove(DATA + name);
                    if (entry == null || expectedDigest.length != 32 || length < -1 ||
                        entry.isDirectory() != (length == -1)) {
                        throw new IOException("Invalid ZIP member: " + name);
                    }
                    Path target = directory == null ? null : directory.resolve(name).normalize();
                    if (target != null && !target.startsWith(directory)) throw new IOException("Invalid restore path");
                    if (length == -1) {
                        if (entry.getSize() != 0 || !MessageDigest.isEqual(expectedDigest, EMPTY)) {
                            throw new IOException("Invalid directory entry");
                        }
                        if (target != null) Files.createDirectories(target);
                        continue;
                    }
                    if (entry.getSize() != length) throw new IOException("Wrong source length: " + name);
                    if (target != null) Files.createDirectories(target.getParent());
                    var sha = digest();
                    long actual = 0;
                    try (var in = new DigestInputStream(zip.getInputStream(entry), sha);
                         var out = target == null ? OutputStream.nullOutputStream() :
                             Files.newOutputStream(target, StandardOpenOption.CREATE_NEW, StandardOpenOption.WRITE)) {
                        byte[] buffer = new byte[128 * 1024];
                        for (int n; (n = in.read(buffer)) != -1;) {
                            if (n > length - actual) throw new IOException("Source length exceeded: " + name);
                            actual += n;
                            out.write(buffer, 0, n);
                        }
                    }
                    if (actual != length || !MessageDigest.isEqual(expectedDigest, sha.digest())) {
                        throw new IOException("Source SHA-256 mismatch: " + name);
                    }
                }
                if (!entries.isEmpty() || meta.read() != -1) throw new IOException("Unexpected archive data");
                log.accept("Byte-for-byte verification passed for " + count + " members");
            }
        }
    }

    static void fresh(Path path) throws IOException {
        if (path.getParent() == null || Files.exists(path, LinkOption.NOFOLLOW_LINKS)) {
            throw new IOException("Output must be a new path: " + path);
        }
    }

    private static MessageDigest digest() {
        try { return MessageDigest.getInstance("SHA-256"); }
        catch (NoSuchAlgorithmException impossible) { throw new AssertionError(impossible); }
    }

    private static void checkName(String path) throws IOException {
        if (path == null || path.isEmpty() || path.length() > 32_000 || path.startsWith("/") ||
            path.indexOf('\\') >= 0 || path.indexOf(':') >= 0) {
            throw new IOException("Unsupported archive member name: " + path);
        }
        String name = path.endsWith("/") ? path.substring(0, path.length() - 1) : path;
        for (String part : name.split("/", -1)) {
            String stem = part.split("\\.", 2)[0].toUpperCase(Locale.ROOT);
            if (part.isEmpty() || part.equals(".") || part.equals("..") ||
                part.endsWith(".") || part.endsWith(" ") ||
                part.chars().anyMatch(c -> c < 32 || ("<>" + '"' + "|?*").indexOf(c) >= 0) ||
                stem.matches("CON|PRN|AUX|NUL|COM[1-9¹²³]|LPT[1-9¹²³]")) {
                throw new IOException("Unsupported archive member name: " + path);
            }
        }
    }
}
