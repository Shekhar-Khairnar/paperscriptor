"""Compiling: missing-figure placeholders, LaTeX errors, PDF<->source jumps (SyncTeX) and the .zip export."""

import os
import posixpath
import re
import shutil
import time
import zipfile

from .. import latex
from .project import (
    WorkspaceError,
    _abs,
    _all_files,
    _hidden,
    _paper,
    _pdf_for,
    _read,
    load_state,
    safe_rel,
    save_state,
    summary,
)
from .chat import INPUT_RE


# --------------------------------------------------------------------------- #
# Compile and download
# --------------------------------------------------------------------------- #
GRAPHICS_RE = re.compile(r"\\includegraphics\*?\s*(?:\[[^\]]*\])?\s*\{([^}]+)\}")
GRAPHICSPATH_RE = re.compile(r"\\graphicspath\s*\{((?:\s*\{[^}]*\})+)\s*\}")
GRAPHIC_EXTS = (".pdf", ".png", ".jpg", ".jpeg", ".eps")


def _strip_comment(line: str) -> str:
    return re.sub(r"(?<!\\)%.*", "", line)


def _used_tex_files(run: str, main: str) -> list[str]:
    """The main file plus every .tex file it pulls in with \\input / \\include (recursively)."""
    order, queue = [], [main]
    while queue:
        cur = queue.pop(0)
        if cur in order or not os.path.isfile(_abs(run, cur)):
            continue
        order.append(cur)
        text = "\n".join(_strip_comment(l) for l in _read(_abs(run, cur)).splitlines())
        for ref in INPUT_RE.findall(text):
            ref = posixpath.normpath(ref.strip())
            for cand in (ref, ref + ".tex"):
                try:
                    cand = safe_rel(cand)
                except WorkspaceError:
                    continue
                if os.path.isfile(_abs(run, cand)):
                    queue.append(cand)
                    break
    return order


def missing_figures(run: str, main: str) -> list[dict]:
    """Figures used with \\includegraphics whose files are not in the project: [{file, line, name}]."""
    used = _used_tex_files(run, main)
    paths = [""]
    for rel in used:
        for group in GRAPHICSPATH_RE.findall(_read(_abs(run, rel))):
            paths += [d.strip() for d in re.findall(r"\{([^}]*)\}", group)]
    missing, seen = [], set()
    for rel in used:
        for i, line in enumerate(_read(_abs(run, rel)).splitlines(), 1):
            for name in GRAPHICS_RE.findall(_strip_comment(line)):
                name = name.strip()
                if not name or "#" in name or "\\" in name:
                    continue  # macro arguments, not real file names
                ext = os.path.splitext(name)[1].lower()
                cands = [name] if ext in GRAPHIC_EXTS else [name + e for e in GRAPHIC_EXTS]
                found = any(os.path.isfile(os.path.join(_paper(run), *(posixpath.join(d, c).split("/"))))
                            for d in paths for c in cands if posixpath.normpath(posixpath.join(d, c))[:2] != "..")
                if not found and name not in seen:
                    seen.add(name)
                    missing.append({"file": rel, "line": i, "name": name})
    return missing


def _png(width: int, height: int) -> bytes:
    """A light grey PNG with a darker border (pure Python, used as a placeholder figure)."""
    import struct
    import zlib
    rows = []
    for y in range(height):
        edge = y < 4 or y >= height - 4
        row = bytearray([0])
        for x in range(width):
            row += b"\x9a\x9a\x9a" if edge or x < 4 or x >= width - 4 else b"\xe6\xe6\xe6"
        rows.append(bytes(row))
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(b"".join(rows), 9)) + chunk(b"IEND", b"")


def _placeholder_pdf(label: str) -> bytes:
    text = re.sub(r"[()\\]", "", label)[:60]
    content = (f"0.9 g 0 0 400 250 re f 0.6 G 4 w 2 2 396 246 re S 0.35 g "
               f"BT /F1 14 Tf 20 120 Td (Missing figure: {text}) Tj ET\n")
    objs = ["<< /Type /Catalog /Pages 2 0 R >>", "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 400 250] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
            f"<< /Length {len(content)} >>\nstream\n{content}endstream", "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    out, offs = "%PDF-1.4\n", []
    for i, o in enumerate(objs, 1):
        offs.append(len(out))
        out += f"{i} 0 obj\n{o}\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n" + "".join(f"{o:010d} 00000 n \n" for o in offs)
    out += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF"
    return out.encode("latin-1")


def _make_placeholders(run: str, missing: list[dict]) -> str | None:
    """Write placeholder images for missing figures into run/build/placeholders (never into the project)."""
    if not missing:
        return None
    root = os.path.join(run, "build", "placeholders")
    shutil.rmtree(root, ignore_errors=True)
    png = None
    for m in missing:
        name = m["name"]
        ext = os.path.splitext(name)[1].lower()
        if ext not in GRAPHIC_EXTS:
            name, ext = name + ".png", ".png"
        if ext == ".eps":
            continue  # can't be placed by every engine; the error message will name it
        target = os.path.normpath(os.path.join(root, *name.split("/")))
        if not target.startswith(os.path.normpath(root) + os.sep):
            continue
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "wb") as f:
            if ext == ".pdf":
                f.write(_placeholder_pdf(posixpath.basename(m["name"])))
            else:
                png = png or _png(400, 250)
                f.write(png)  # .jpg/.jpeg too: graphicx reads the file type from the data
    return root


def compile_run(run: str) -> dict:
    state = load_state(run)
    main = state["main"]
    missing = missing_figures(run, main)
    holder = _make_placeholders(run, missing)
    res = latex.compile_project(_paper(run), main, search_dirs=[holder] if holder else [])
    names = {posixpath.basename(m["name"]) for m in missing}
    res["errors"] = [e for e in res["errors"]  # placeholders stand in for these
                     if not (names and "picture" in e["message"].lower() and any(n in e["message"] for n in names))]
    paper_abs = os.path.abspath(_paper(run)).replace("\\", "/").rstrip("/") + "/"
    for e in res["errors"]:
        f = (e.get("file") or "").replace("\\", "/")
        if f and (os.path.isabs(f) or ":" in f[:3]):
            fa = os.path.abspath(f).replace("\\", "/")
            e["file"] = fa[len(paper_abs):] if fa.lower().startswith(paper_abs.lower()) else None
            if not e["file"]:
                e["message"] = f"{posixpath.basename(f)}: {e['message']}"
        elif not f and e.get("line"):
            e["file"] = main
    state = load_state(run)  # may have changed during a long compile
    state["compile"] = {"ok": bool(res["pdf"]) and not res["errors"], "errors": res["errors"], "message": res["message"],
                        "engine": res["engine"], "seconds": res["seconds"], "time": time.time(),
                        "version": state["version"], "pdf": bool(res["pdf"]), "main": main, "missing": missing}
    save_state(run, state)
    if res["pdf"] and main == state["main"]:
        shutil.copy(res["pdf"], os.path.join(run, "paper.pdf"))
    return summary(run)


def build_zip(run: str) -> str:
    out = os.path.join(run, "paper.zip")
    files = _all_files(run)
    all_set = set(files)
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for rel in files:
            if not _hidden(rel, all_set):
                z.write(_abs(run, rel), rel)
    return out


def sync_forward(run: str, rel: str, line: int) -> dict:
    from .. import synctex
    state = load_state(run)
    path = os.path.splitext(_pdf_for(run, state["main"]))[0] + ".synctex.gz"
    hit = synctex.forward(path, _paper(run), safe_rel(rel), int(line))
    if not hit:
        raise WorkspaceError("No position found. Compile first, then try again.", 404)
    return hit


def sync_inverse(run: str, page: int, x: float, y: float) -> dict:
    from .. import synctex
    state = load_state(run)
    path = os.path.splitext(_pdf_for(run, state["main"]))[0] + ".synctex.gz"
    hit = synctex.inverse(path, _paper(run), int(page), float(x), float(y))
    if not hit or not os.path.isfile(_abs(run, hit["path"])):
        raise WorkspaceError("No source line found for that spot.", 404)
    return hit
