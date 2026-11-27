"""Selective spatial exports and replay of archived analysis inputs."""
from contextlib import contextmanager
import csv
import hashlib
import html
import json
from pathlib import Path
import tempfile

import numpy as np
from PIL import Image, ImageDraw

from container_compare import _fresh, _plain
from stbaseline.integrity import sha256


def attachment(reader):
    name = "HEST_metadata.json"
    if name not in reader.manifest["count"].get("attachments", []):
        return {}
    member = "count/" + name
    desc = reader.manifest["files"][member]
    path = _plain(reader.root / member)
    # Check the bytes actually used by the query or analysis.
    limit = min(desc["bytes"], 64 * 1024**2)
    with path.open("rb") as handle:
        raw = handle.read(limit + 1)
    if len(raw) != desc["bytes"] or hashlib.sha256(raw).hexdigest() != desc["sha256"]:
        raise ValueError("Missing, oversized, or modified archive analysis metadata")
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("Archive analysis metadata must be a JSON object")
    return data


def windows(reader, data):
    result = {}
    for entry in data.get("windows", []):
        label, bbox = entry["region"], entry["local_pixels"]
        if not isinstance(label, str) or not label or label in result:
            raise ValueError("Stored window names must be unique nonempty strings")
        # Use the same bounds as the reader.
        reader._region_boxes(bbox, 0)
        result[label] = {"name": label, "bbox": bbox, "split": entry.get("split")}
    return result


def gene_axes(reader):
    ids = list(reader.metadata()["gene_ids"])
    dataset = reader._science_open().get("var/feature_name")
    names = list(dataset.asstr()[:]) if dataset is not None else ids
    if len(names) != len(ids):
        raise ValueError("Feature names disagree with the archived gene axis")
    return ids, names


def resolve_genes(labels, ids, names):
    positions = []
    for label in labels:
        hits = [i for i, gene in enumerate(ids) if gene == label]
        if not hits:
            hits = [i for i, name in enumerate(names) if name == label]
        if len(hits) != 1:
            message = "Unknown gene" if not hits else "Ambiguous gene; use a unique feature ID or --gene-indices"
            raise ValueError(f"{message}: {label}")
        positions.append(hits[0])
    return positions


@contextmanager
def result_directory(reader, out):
    target = _fresh(out, reader.root)
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".stpyramid-", dir=target.parent) as scratch:
        staged = Path(scratch) / "result"
        staged.mkdir()
        yield staged, target
        staged.rename(target)


def write_csv(path, fields, rows):
    with path.open("x", encoding="utf-8-sig", newline="") as stream:
        out = csv.writer(stream)
        out.writerow(fields)
        out.writerows(rows)


def write_json(path, value):
    with path.open("x", encoding="utf8") as stream:
        json.dump(value, stream, ensure_ascii=False, allow_nan=False, indent=2)
        stream.write("\n")


def write_report(path, title, description, fields, rows, links, preview=False):
    esc = lambda value: html.escape(str(value) if value is not None else "NA")
    headers = "".join(f"<th scope='col'>{esc(value)}</th>" for value in fields)
    body = "".join("<tr>" + "".join(f"<td>{esc(value)}</td>" for value in row) + "</tr>" for row in rows)
    resources = "".join(f'<li><a href="{esc(link)}">{esc(label)}</a></li>' for link, label in links)
    thumb = '<img src="preview.png" alt="缩小的区域图像预览">' if preview else ""
    table = f"<table><thead><tr>{headers}</tr></thead><tbody>{body}</tbody></table>" if fields else ""
    path.write_text(f"""<!doctype html>
<html lang="zh"><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(title)}</title>
<style>
body{{font:16px/1.55 system-ui,sans-serif;color:#172331;background:#f4f6fa;margin:0}}
main{{max-width:1100px;margin:auto;padding:2.5rem 1.4rem}} p{{max-width:78ch}}
img{{max-width:100%;height:auto;display:block;border-radius:.5rem}}
table{{border-collapse:collapse;background:white;width:100%;font-variant-numeric:tabular-nums}}
th,td{{text-align:left;padding:.4rem .6rem;border-bottom:1px solid #dce2ea}}
th{{color:white;background:#25374f}} a{{color:#144b87}} ul{{padding-left:1.3rem}}
</style><main><h1>{esc(title)}</h1><p>{esc(description)}</p>{thumb}<ul>{resources}</ul>{table}</main></html>
""", encoding="utf8")


def provenance(reader, command):
    return {"command": command, "archive_manifest_sha256": sha256(reader.root / "manifest.json"),
            "count_kind": reader.manifest["count"]["kind"], "image_profile": reader.manifest["image"]["profile"]}


def export_region(reader, args, stored):
    modalities = args.modalities
    if (args.genes or args.gene_indices) and "counts" not in modalities:
        raise ValueError("Gene selection requires Count access")
    if not args.bbox and args.region not in stored:
        raise ValueError("Unknown stored window; list them with windows or use --bbox")
    bbox = args.bbox or stored[args.region]["bbox"]
    selected = names = None
    with result_directory(reader, args.output) as (folder,target):
        if "counts" in modalities:
            ids, names = gene_axes(reader)
            selected = resolve_genes(args.genes, ids, names) if args.genes else args.gene_indices
        data = reader.region(bbox, level=args.level, genes=selected, modalities=modalities)
        summary = []
        report = {**provenance(reader, "region"), "window": args.region, "level0_bbox": data["level0_bbox"].tolist(),
                  "image_bbox": data["image_bbox"].tolist(), "level": args.level, "modalities": modalities}
        # Preserve lossless arrays and axis order for downstream tools.
        if "counts" in data:
            fields = ("gene_id", "gene_name", "positions", "detected", "sum_count", "mean_count")
            n = data["counts"].shape[0]
            for i, (gene, position) in enumerate(zip(data["gene_ids"], data["gene_positions"], strict=True)):
                col = data["counts"][:,i]
                total = sum(map(int,col))
                summary.append((str(gene), names[int(position)], n, int(np.count_nonzero(col)),
                                total, total/n if n else None))
            report.update(positions=n, genes=int(data["counts"].shape[1]))
            write_csv(folder/"gene_summary.csv",fields,summary)
        else:
            fields = ()
        preview = "image" in data
        if preview:
            report["rgb_shape"] = list(data["image"].shape)
            thumbnail = Image.fromarray(data["image"])
            thumbnail.thumbnail((900,900))
            thumbnail.save(folder/"preview.png")
        np.savez_compressed(folder/"region.npz", **data)
        write_json(folder/"result.json",report)
        links = [("region.npz","精确数组与轴"),("result.json","查询与归档记录")]
        if fields:
            links.append(("gene_summary.csv","逐基因汇总"))
        write_report(folder/"report.html", "STPyramid 同窗查询",
                     f"L0 窗口 {bbox}，指定层级 {args.level}。如有图像预览，其原生 RGB 位于数组导出。",
                     fields, summary, links, preview)
    return {**report, "output": str(target), "report": str(target/"report.html")}


def edge_labels(data):
    """Rasterize the geometry used by the archived TENX97 edge plan."""
    raster_um = 3.0
    scale = 2 * float(data["native_pixel_spacing_um"]) / raster_um
    dims = tuple(int(size)//3+2 for size in data["source_size_um"])
    if len(dims) != 2 or min(dims) < 1 or dims[0]*dims[1] > 16_777_216:
        raise ValueError("An edge plan must specify a bounded spatial raster")
    features = data["geometry"]["features"]
    labels = np.zeros(dims[::-1], dtype=np.int8)
    occupied = np.zeros_like(labels, dtype=bool)
    overlap = np.zeros_like(occupied)
    for feature in features:
        name = feature["properties"]["classification"]["name"]
        image = Image.new("1", dims)
        painter = ImageDraw.Draw(image)
        if feature["geometry"]["type"] != "MultiPolygon":
            raise ValueError("Expected MultiPolygon edge annotations")
        for polygon in feature["geometry"]["coordinates"]:
            for i, ring in enumerate(polygon):
                points = np.rint(np.asarray(ring) * scale).astype(int)
                painter.polygon(list(map(tuple, points)), fill=int(i == 0))
        mask = np.asarray(image, dtype=bool)
        overlap |= occupied & mask
        occupied |= mask
        labels[mask] = {"Invasive Tumor": -1, "Stroma": 1}.get(name, 0)
    labels[overlap] = 0
    return labels


def edge_window(data, query, label, mask, names, membership, anchors, bands):
    counts, xy = query["counts"], query["coordinates"]
    if counts.size and int(counts.max()) > np.iinfo(np.int64).max // counts.size:
        raise ValueError("Count totals exceed the exact integer bound of this analysis")
    panel = counts.sum(axis=1).astype(np.int64)
    positions = [names.index(gene) for gene in data["genes"]]
    grid = int(data["grid_um"])
    candidates = [row for row in data["frozen_candidates"] if row["region"] == label]
    groups = {(int(row["x_um"])//grid, int(row["y_um"])//grid, int(row["side"])): i
              for i,row in enumerate(candidates)}
    if len(groups) != len(candidates):
        raise ValueError("Duplicate candidates for an archived edge window")
    x, y = np.rint(xy.T / 3.0).astype(int)
    inside = (x >= 0) & (x < mask.shape[1]) & (y >= 0) & (y < mask.shape[0])
    sides = np.zeros(len(x), dtype=np.int8)
    sides[inside] = mask[y[inside], x[inside]]
    grid_xy = np.floor(xy / grid).astype(int)
    which = np.asarray([groups.get((int(x), int(y), int(side)), -1)
                        for (x,y),side in zip(grid_xy,sides)])
    accepted = (which >= 0) & (panel > 0)
    grouped = np.zeros((len(candidates), 2+len(positions)), dtype=np.int64)
    np.add.at(grouped, which[accepted], np.c_[np.ones(int(accepted.sum()), dtype=np.int64),
                                              panel[accepted], counts[accepted][:,positions].astype(np.int64)])
    local = []
    for i, (row, values) in enumerate(zip(candidates, grouped)):
        n, total, *gene_counts = map(int, values)
        if n < data["minimum_cells"]:
            continue
        name, distance = row["spot_id"], float(row["distance_um"])
        expression = [float(np.log1p(10000*k/total)) for k in gene_counts]
        record = (label,name,distance,n,total,*gene_counts,*expression)
        anchors.append(record)
        local.append(record)
        membership.extend((label,name,str(gene)) for gene in query["spot_ids"][accepted & (which == i)])
    distance = np.asarray([row[2] for row in local])
    for j,gene in enumerate(data["genes"]):
        expression = np.asarray([row[5+len(positions)+j] for row in local])
        for lo in range(-200,200,40):
            selected = (distance >= lo) & (distance < lo+40)
            bands.append((label,gene,lo,lo+40,int(selected.sum()),
                          float(expression[selected].mean()) if selected.any() else None,
                          sum(row[3] for row,use in zip(local,selected) if use)))


def replay_edge(reader, args, data, stored):
    required = {"geometry","frozen_candidates","source_size_um","native_pixel_spacing_um",
                "vendor_um_to_package_l0","genes","grid_um","minimum_cells"}
    if data.get("sample") != "TENX97" or not required <= data.keys() or not stored:
        raise ValueError("This archive has no supported edge-analysis plan")
    if data["grid_um"] <= 0 or data["minimum_cells"] < 1 or not data["genes"]:
        raise ValueError("Invalid archived edge-analysis parameters")
    if data["vendor_um_to_package_l0"] != reader.manifest["mapping"]["xy_to_level0"]:
        raise ValueError("Edge plan and archive coordinate transforms disagree")
    selected = args.regions or list(stored)
    if len(set(selected)) != len(selected) or any(label not in stored for label in selected):
        raise ValueError("Select unique stored analysis windows")
    with result_directory(reader, args.output) as (folder,target):
        _, names = gene_axes(reader)
        kinds = reader._science_open().get("var/feature_type")
        if kinds is None:
            raise ValueError("Edge replay requires archived feature types")
        panel = np.flatnonzero(kinds.asstr()[:] == "Gene Expression")
        names = [names[i] for i in panel]
        if len(panel) == 0 or any(names.count(gene) != 1 for gene in data["genes"]):
            raise ValueError("Archived edge genes must resolve uniquely in the expression panel")
        mask = edge_labels(data)
        anchors, bands, membership, ranges = [], [], [], []
        for i,label in enumerate(selected,1):
            bbox = stored[label]["bbox"]
            mode = ("counts","image") if args.include_image else ("counts",)
            query = reader.region(bbox, genes=panel.tolist(), modalities=mode)
            edge_window(data,query,label,mask,names,membership,anchors,bands)
            info = {"name":label,"bbox":bbox,"positions_read":int(query["counts"].shape[0])}
            if args.include_image:
                filename = f"window_{i:02d}_rgb.npy"
                np.save(folder/filename, query["image"], allow_pickle=False)
                info["native_rgb"] = filename
            ranges.append(info)
        band_fields = ("region","gene","lo_um","hi_um","anchors","value","n_cells")
        write_csv(folder/"bands.csv",band_fields,bands)
        write_csv(folder/"anchors.csv",("region","spot_id","distance_um","n_cells","total_panel",
                  *(gene+"_count" for gene in data["genes"]),*data["genes"]),anchors)
        write_csv(folder/"included_cells.csv",("region","anchor_id","cell_id"),membership)
        report = {**provenance(reader,"replay-edge"),"sample":data.get("sample"),"scope":data.get("scope"),
                  "windows":ranges,"anchors":len(anchors),"included_cells":len(membership),
                  "band_points":len(bands),"genes":data["genes"],"grid_um":data["grid_um"],
                  "minimum_cells":data["minimum_cells"],"expression_panel_features":len(panel)}
        write_json(folder/"result.json",report)
        write_report(folder/"report.html","STPyramid 浸润边缘复算",
                     f"{len(ranges)} 个归档窗口，{len(anchors)} 个方格，{len(membership)} 个纳入细胞。"
                     "曲线为原固定距离带的方格平均值，空带显示 NA。",
                     band_fields, bands, [("bands.csv","距离带曲线"),("anchors.csv","方格结果"),
                     ("included_cells.csv","纳入细胞"),("result.json","分析与归档记录")])
    return {**report,"output":str(target),"report":str(target/"report.html")}


def execute(reader,args):
    # An explicit box works on archives without named analysis windows.
    data = attachment(reader) if args.command != "region" or args.region else {}
    stored = windows(reader,data)
    if args.command == "windows":
        return {**provenance(reader,"windows"), "scope":data.get("scope"),
                "level0_shape":reader.manifest["image"]["levels"][0]["shape"],
                "windows":list(stored.values())}
    if args.command == "region":
        return export_region(reader,args,stored)
    return replay_edge(reader,args,data,stored)
