"""Minimal SyncTeX reader: jump from a source line to the PDF, and from a point in the PDF to a source line.

The .synctex.gz file written by pdflatex (-synctex=1) or Tectonic (--synctex) lists, for every page, boxes with
the input file and line they came from. Coordinates are in scaled points from the top-left corner of the page.
"""

import gzip
import os
import posixpath
import re

_RECORD = re.compile(r"^([\[(hvxkg$])(\d+),(\d+):(-?\d+),(-?\d+)(?::(-?\d+)(?:,(-?\d+),(-?\d+))?)?")
_cache: dict[str, tuple[float, dict]] = {}
SP_PER_BP = 65781.76  # TeX scaled points per PDF point


def _load(path: str) -> dict | None:
    if not os.path.exists(path):
        return None
    mtime = os.path.getmtime(path)
    hit = _cache.get(path)
    if hit and hit[0] == mtime:
        return hit[1]
    opener = gzip.open if path.endswith(".gz") else open
    inputs, records, unit, mag, xoff, yoff, page = {}, [], 1.0, 1000.0, 0.0, 0.0, 0
    with opener(path, "rt", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.rstrip("\n")
            if line.startswith("Input:"):
                tag, _, name = line[6:].partition(":")
                inputs[int(tag)] = name
            elif line.startswith("Unit:"):
                unit = float(line[5:] or 1)
            elif line.startswith("Magnification:"):
                mag = float(line[14:] or 1000)
            elif line.startswith("X Offset:"):
                xoff = float(line[9:] or 0)
            elif line.startswith("Y Offset:"):
                yoff = float(line[9:] or 0)
            elif line.startswith("{"):
                page = int(line[1:] or 0)
            else:
                m = _RECORD.match(line)
                if m and page:
                    kind, tag, ln, x, y, w, h, d = m.groups()
                    k = unit * mag / 1000.0 / SP_PER_BP
                    records.append((page, int(tag), int(ln), (int(x) * unit + xoff) * mag / 1000.0 / SP_PER_BP,
                                    (int(y) * unit + yoff) * mag / 1000.0 / SP_PER_BP,
                                    abs(int(w)) * k if w else 0.0, int(h) * k if h else 0.0, int(d) * k if d else 0.0, kind))
    data = {"inputs": inputs, "records": records}
    _cache[path] = (mtime, data)
    return data


def _rel(name: str, paper_dir: str) -> str:
    name = name.replace("\\", "/")
    root = os.path.abspath(paper_dir).replace("\\", "/").rstrip("/") + "/"
    if os.path.isabs(name) or re.match(r"^[A-Za-z]:/", name):
        absn = os.path.abspath(name).replace("\\", "/")
        if absn.lower().startswith(root.lower()):
            name = absn[len(root):]
        else:
            return ""
    return posixpath.normpath(name.lstrip("./") if name.startswith("./") else name)


def forward(synctex_path: str, paper_dir: str, rel_file: str, line: int) -> dict | None:
    """Source line -> {page, x, y} in PDF points from the top-left of the page."""
    data = _load(synctex_path)
    if not data:
        return None
    tags = [t for t, n in data["inputs"].items() if _rel(n, paper_dir) == rel_file
            or _rel(n, paper_dir) == rel_file.rsplit(".", 1)[0]]
    if not tags:
        return None
    recs = [r for r in data["records"] if r[1] in tags]
    if not recs:
        return None
    best = min(recs, key=lambda r: (abs(r[2] - line), r[0], r[4]))
    same = [r for r in recs if r[2] == best[2] and r[0] == best[0]]
    return {"page": best[0], "x": min(r[3] for r in same), "y": min(r[4] - (r[6] or 0) for r in same)}


def inverse(synctex_path: str, paper_dir: str, page: int, x: float, y: float) -> dict | None:
    """Point in the PDF (points from top-left) -> {path, line}."""
    data = _load(synctex_path)
    if not data:
        return None
    best, best_score = None, None
    for r in data["records"]:
        if r[0] != page or r[8] in ("[", "("):
            continue
        rx, ry, w, h, d = r[3], r[4], r[5], r[6], r[7]
        dx = 0.0 if rx <= x <= rx + max(w, 1.0) else min(abs(x - rx), abs(x - rx - w))
        dy = 0.0 if ry - max(h, 8.0) <= y <= ry + max(d, 2.0) else min(abs(y - ry), abs(y - ry + h))
        score = dy * 4 + dx
        if best_score is None or score < best_score:
            best, best_score = r, score
    if not best:
        return None
    rel = _rel(data["inputs"].get(best[1], ""), paper_dir)
    if rel and not os.path.splitext(rel)[1] and os.path.exists(os.path.join(paper_dir, rel + ".tex")):
        rel += ".tex"
    return {"path": rel, "line": best[2]} if rel else None
