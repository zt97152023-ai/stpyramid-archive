"""Select ten studies using archive availability, platform and image dimensions."""
import json
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent
DATA = Path("E:/Hestdata")
JOB_ROOTS = tuple(Path(f"{drive}:/ResearchWorkspace/st_jxl_262_20261104/jobs") for drive in "EF")
QUOTAS = {"Spatial Transcriptomics": 3, "Visium": 4, "Xenium": 2}
ANCHOR = "NCBI224"


def read_json(path):
    return json.loads(path.read_text(encoding="utf8"))


def eligible():
    result = {}
    for root in JOB_ROOTS:
        for job in sorted(root.glob("*_build_v1")):
            sid = job.name.removesuffix("_build_v1")
            original = job / "packages/ours/image"
            direct = job / "packages/baseline/image"
            required = (original / "manifest.json", direct / "manifest.json", job / "source_identities.json",
                        DATA / f"metadata/{sid}.json")
            if not all(path.is_file() for path in required):
                continue
            historical = read_json(required[2])["image"]
            source = Path(historical["path"])
            if not source.is_file() or source.stat().st_size != historical["bytes"]:
                continue
            levels = read_json(required[1])["image"]["levels"]
            if not 3 <= len(levels) <= 16:
                continue
            shapes = [level["shape"] for level in levels]
            if any(shape[2] != 3 for shape in shapes) or any(
                    shapes[i + 1][:2] != [value // 2 for value in shapes[i][:2]] for i in (0, 1)):
                continue
            metadata = read_json(required[3])
            study = metadata.get("dataset_title")
            platform = metadata.get("st_technology")
            if not isinstance(study, str) or not study.strip() or platform not in QUOTAS:
                continue
            url = metadata.get("study_link")
            if isinstance(url, str):
                parts = urlsplit(url.strip())
                url = parts._replace(query="", fragment="").geturl() if parts.scheme in ("http", "https") else None
            else:
                url = None
            if sid in result:
                raise ValueError(f"Multiple eligible archives for {sid}")
            result[sid] = {
                "sample": sid, "study": study.strip(), "study_url": url, "platform": platform,
                "source": str(source), "source_sha256": historical["sha256"], "source_bytes": historical["bytes"],
                "historical_hf_l1": str(original), "historical_direct": str(direct),
                "native_shapes": shapes, "native_rgb_sha256": [level["pixel_sha256"] for level in levels],
                "l0_pixels": shapes[0][0] * shapes[0][1],
            }
    return result


def select():
    universe = eligible()
    chosen = [universe[ANCHOR]]
    used = {" ".join(chosen[0]["study"].casefold().split())}
    for platform, quota in QUOTAS.items():
        candidates = sorted((x for x in universe.values() if x["platform"] == platform),
                            key=lambda x: (x["l0_pixels"], x["sample"]))
        count = 0
        for item in candidates:
            study = " ".join(item["study"].casefold().split())
            if study in used:
                continue
            used.add(study)
            chosen.append(item)
            count += 1
            if count == quota:
                break
        if count != quota:
            raise ValueError(f"Only {count} distinct eligible studies for {platform}")
    selection = {
        "rule": "NCBI224 development anchor plus three ST, four Visium and two Xenium whole TIFFs. "
                "Per platform rank by L0 pixel area ascending, then sample ID, and retain at most one "
                "per normalized HEST dataset title across the cohort. Size-aware validation. "
                "Require an existing HF+L1 archive, source identity and compatible native geometry. "
                "No rates, candidate routes or timings used for selection.",
        "eligible": len(universe), "development_anchor": ANCHOR, "samples": chosen,
    }
    with (ROOT / "COHORT.json").open("x", encoding="utf8") as stream:
        json.dump(selection, stream, indent=2, ensure_ascii=False)
    for x in chosen:
        print(x["sample"], x["platform"], x["l0_pixels"], x["study"])


if __name__ == "__main__":
    select()
