"""Check the released files and independently recompute the P0 result table."""
from hashlib import file_digest
import json
from math import isclose
from pathlib import Path
from statistics import median


HERE = Path(__file__).resolve().parent
RECEIPTS = HERE / "receipts"
FIELDS = ("package_bytes", "publish_wall_s", "independent_readback_wall_s")


def read(path):
    return json.loads(path.read_bytes())


def sha(path):
    with path.open("rb") as handle:
        return file_digest(handle, "sha256").hexdigest()


def require(condition, message):
    if not condition:
        raise ValueError(message)


def load_pair(folder, sample, config, allow_failed=False):
    pair = []
    for arm in config["arms"]:
        case = folder / arm
        path = case / "RESULT.json"
        if allow_failed and not path.exists():
            failed = read(case / "ERROR.json")
            require((failed["sample"], failed["arm"], failed["state"]) == (sample, arm, "FAIL"), f"Wrong failure receipt: {case}")
            continue
        result = read(path)
        require((result["sample"], result["arm"], result["state"]) == (sample, arm, "PASS"), f"Wrong result: {case}")
        require(all(result[key] for key in ("typed_h5ad_exact", "original_json_exact", "all_members_verified")), f"Recovery failed: {case}")
        require(result["native_sha256"] == config["samples"][sample]["native_sha256"], f"Pixel hash mismatch: {case}")
        imported = read(case / "IMPORT.json")
        ledger = imported["encode"]["ledger"]
        require(result["package_bytes"] == ledger["total_package_bytes"] == sum(ledger["members"].values()), f"Byte ledger mismatch: {case}")
        require(result["publish_wall_s"] == imported["publish_wall_s"], f"Timing mismatch: {case}")
        pair.append(result)
    if len(pair) == 2:
        require(pair[0]["count_members"] == pair[1]["count_members"], f"Count differs across arms: {sample}")
    return pair


def check_summary(pairs, reported):
    require(reported["n"] == len(pairs), "Incorrect denominator")
    for field in FIELDS:
        summary = reported[field]
        left = [a[field] for a, _ in pairs]
        right = [b[field] for _, b in pairs]
        expected = {
            "regular_091_sum": sum(left),
            "direct_jxl_e3_sum": sum(right),
            "regular_091_wins": sum(a < b for a, b in zip(left, right, strict=True)),
            "direct_jxl_e3_wins": sum(b < a for a, b in zip(left, right, strict=True)),
            "median_regular_091_over_direct_e3": median(a / b for a, b in zip(left, right, strict=True)),
            "regular_091_saving_pct": 100 * (1 - sum(left) / sum(right)),
        }
        require(all(isclose(summary[key], value, abs_tol=1e-12) for key, value in expected.items()), f"Summary mismatch: {field}")


def main():
    for line in (HERE / "SHA256SUMS").read_text(encoding="utf8").splitlines():
        expected, path = line.split("  ", 1)
        path = (HERE / path).resolve(strict=True)
        require(path.is_relative_to(HERE) and sha(path) == expected, f"Release checksum mismatch: {path}")
    config = read(HERE / "CONFIG.json")
    report = read(RECEIPTS / "RECONCILED.json")
    audit = read(RECEIPTS / "INDEPENDENCE_AUDIT.json")
    cohort = config["frozen_samples"]
    require(list(config["arms"]) == ["regular_091", "direct_jxl_e3"], "Unexpected arms")
    require(len(cohort) == len(set(cohort)) == len(report["rows"]) == 10, "Unexpected cohort")
    require([row["sample"] for row in report["rows"]] == cohort, "Selected sample order differs")
    require(audit["confirmed_independent_n"] == report["confirmed_independent_n"] == 0, "Independence audit mismatch")
    require(audit["direct_development_count"] == 7 and audit["additional_same_study_count"] == 3, "Exposure audit mismatch")
    for path, expected in config["app_sources_sha256"].items():
        file = HERE / "overlay" / path if path == "pyramid_image_archive.py" else HERE.parent / "source" / path
        require(sha(file) == expected, f"Frozen code mismatch: {path}")
    require(len(load_pair(RECEIPTS / "preflight/NCBI29", "NCBI29", config)) == 2, "Preflight did not pass")
    first, selected = [], []
    first_arms = 0
    for row in report["rows"]:
        sample = row["sample"]
        formal = RECEIPTS / "formal" / sample
        first_pair = load_pair(formal, sample, config, allow_failed=True)
        first_arms += len(first_pair)
        if len(first_pair) == 2:
            first.append(first_pair)
        final = (RECEIPTS / row["selected_receipts"]).resolve(strict=True)
        require(final.is_relative_to(RECEIPTS) and final.name in {sample, f"{sample}_attempt02"}, "Invalid evidence selection")
        pair = load_pair(final, sample, config)
        for result in pair:
            for field in FIELDS:
                require(row[f"{result['arm']}_{field}"] == result[field], f"Selected-table mismatch: {sample}")
        selected.append(pair)
    require(len(first) == 8 and first_arms == 17, "Unexpected first-pass denominator")
    check_summary(first, report["first_pass_performance"])
    check_summary(selected, report["performance"])
    print("PASS: 2 preflight arms, 17/20 first-pass arms, 8/10 first-pass pairs, 10/10 accepted pairs, 0 independent sections.")
    print(json.dumps(report["performance"], indent=2))


if __name__ == "__main__":
    main()
