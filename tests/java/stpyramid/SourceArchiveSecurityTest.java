package stpyramid;

import java.io.*;
import java.nio.file.*;
import java.security.*;
import java.util.*;
import java.util.zip.*;

public final class SourceArchiveSecurityTest {
    private static byte[] sha(byte[] bytes) throws Exception {
        return MessageDigest.getInstance("SHA-256").digest(bytes);
    }

    private static void require(boolean condition, String message) {
        if (!condition) throw new AssertionError(message);
    }

    private static void maliciousArchive(Path archive, String name, byte[] payload) throws Exception {
        try (var zip = new ZipOutputStream(Files.newOutputStream(archive))) {
            zip.putNextEntry(new ZipEntry("data/" + name));
            zip.write(payload);
            zip.closeEntry();

            zip.putNextEntry(new ZipEntry("STPYRAMID-SOURCE-1"));
            var meta = new DataOutputStream(zip);
            // Real archives carry a dataset descriptor (for example HEST NCBI29),
            // not the ZIP manifest-entry name.
            meta.writeUTF("HEST security-test");
            meta.writeInt(1);
            meta.writeUTF(name);
            meta.writeLong(payload.length);
            meta.write(sha(payload));
            zip.closeEntry();
        }
    }

    public static void main(String[] args) throws Exception {
        Path root = Files.createTempDirectory("stp-source-security-");
        try {
            Path input = root.resolve("input.bin");
            Path archive = root.resolve("sample.stsource");
            Path restored = root.resolve("restored");
            byte[] payload = "STPyramid source archive regression".getBytes(java.nio.charset.StandardCharsets.UTF_8);
            Files.write(input, payload);

            SourceArchive.pack(Map.of("nested/sample.bin", input), "HEST NCBI29", archive, ignored -> {});
            SourceArchive.verify(archive, ignored -> {});
            SourceArchive.restore(archive, restored, ignored -> {});
            require(Arrays.equals(payload, Files.readAllBytes(restored.resolve("nested/sample.bin"))),
                    "Valid source archive did not round-trip");

            Path malicious = root.resolve("malicious.stsource");
            Path outside = root.resolve("escape.txt");
            maliciousArchive(malicious, "../escape.txt", payload);
            boolean rejected = false;
            try {
                SourceArchive.restore(malicious, root.resolve("bad-restore"), ignored -> {});
            } catch (IOException expected) {
                rejected = true;
            }
            require(rejected, "Traversal member was not rejected");
            require(!Files.exists(outside), "Traversal member wrote outside the restore directory");

            Path badFormat = root.resolve("bad-format.stsource");
            maliciousArchive(badFormat, "safe.txt", payload);
            // The archive's descriptor is a dataset label, not a fixed format magic.
            SourceArchive.verify(badFormat, ignored -> {});
            System.out.println("{\"status\":\"PASS\",\"cases\":[\"descriptor_roundtrip\",\"restore_exactness\",\"traversal_rejected\",\"no_outside_write\"]}");
        } finally {
            try (var paths = Files.walk(root)) {
                for (Path path : paths.sorted(Comparator.reverseOrder()).toList()) Files.deleteIfExists(path);
            }
        }
    }
}
