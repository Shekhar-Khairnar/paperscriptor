"""Reading user files: source documents (md/txt/pdf/docx/...) and custom LaTeX templates."""

import io
import os
import posixpath
import re
import zipfile

TEXT_EXTS = {".md", ".markdown", ".txt", ".tex", ".csv", ".tsv", ".json", ".log", ".yaml", ".yml"}
DOC_EXTS = TEXT_EXTS | {".pdf", ".docx"}
MAX_DOC_CHARS = 300_000           # ~75k tokens per document
MAX_TEMPLATE_BYTES = 50 * 1024 * 1024


class InputError(ValueError):
    """A problem with a user file; the message is shown in the UI."""


# --------------------------------------------------------------------------- #
# Documents
# --------------------------------------------------------------------------- #
def _decode(data: bytes) -> str:
    for enc in ("utf-8-sig", "utf-16") if data[:2] in (b"\xff\xfe", b"\xfe\xff") else ("utf-8-sig", "cp1252"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def _pdf_text(data: bytes) -> str:
    from pypdf import PdfReader

    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            reader.decrypt("")
        pages = [(p.extract_text() or "").strip() for p in reader.pages]
    except Exception as e:
        raise InputError(f"Could not read this PDF ({e}).")
    return "\n\n".join(f"[Page {i}]\n{t}" for i, t in enumerate(pages, 1) if t)


def _docx_text(data: bytes) -> str:
    import docx
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    try:
        d = docx.Document(io.BytesIO(data))
    except Exception as e:
        raise InputError(f"Could not read this Word file ({e}).")
    out = []
    for child in d.element.body.iterchildren():
        tag = child.tag.rsplit("}", 1)[-1]
        if tag == "p":
            p = Paragraph(child, d)
            text = p.text.strip()
            if not text:
                continue
            style = (p.style.name if p.style is not None else "") or ""
            m = re.match(r"Heading (\d)", style)
            out.append(("#" * int(m.group(1)) + " " + text) if m else text)
        elif tag == "tbl":  # keep tables: results often live in them
            rows = []
            for row in Table(child, d).rows:
                cells = [c.text.strip().replace("\n", " ") for c in row.cells]
                rows.append("| " + " | ".join(cells) + " |")
            if rows:
                out.append("\n".join(rows))
    return "\n\n".join(out)


def extract_text(filename: str, data: bytes) -> tuple[str, str | None]:
    """Return (text, warning). Raises InputError for unsupported or unreadable files."""
    ext = os.path.splitext((filename or "").lower())[1]
    if ext == ".doc":
        raise InputError("Old .doc files aren't supported. In Word, use Save As > .docx (or PDF) and add that instead.")
    if ext not in DOC_EXTS:
        raise InputError(f"'{ext or filename}' isn't supported. Use .md, .txt, .tex, .pdf, .docx, .csv or .json.")
    if ext == ".pdf":
        text = _pdf_text(data)
    elif ext == ".docx":
        text = _docx_text(data)
    else:
        text = _decode(data)
    text = text.replace("\r\n", "\n").strip()
    if not text:
        hint = " It may be a scanned PDF with no text layer." if ext == ".pdf" else ""
        raise InputError(f"No text found in '{filename}'.{hint}")
    warning = None
    if len(text) > MAX_DOC_CHARS:
        text = text[:MAX_DOC_CHARS]
        warning = f"Very long, so only the first {MAX_DOC_CHARS:,} characters are used."
    return text, warning


def combine_materials(idea: str, log: str, documents: list) -> tuple[str, str]:
    """Merge the text boxes with uploaded documents into idea.md / experimental_log.md."""
    idea_parts = [idea.strip()] if idea.strip() else []
    log_parts = [log.strip()] if log.strip() else []
    for doc in documents:
        text = (doc.get("text") or "").strip()
        if not text:
            continue
        block = f"## Source document: {doc.get('name', 'document')}\n\n{text}"
        role = doc.get("role", "idea")
        if role in ("idea", "both"):
            idea_parts.append(block)
        if role in ("results", "both"):
            log_parts.append(block)
    idea_md = "\n\n---\n\n".join(idea_parts)
    log_md = "\n\n---\n\n".join(log_parts)
    if not idea_md and not log_md:
        raise InputError("Add your idea, your results, or at least one document.")
    if not idea_md:
        idea_md = ("(No separate idea/method document was provided. Infer the problem, method and "
                   "contributions from the experimental log below; do not invent details.)")
    if not log_md:
        log_md = ("(No separate experimental log was provided. Use only results that are stated in "
                  "idea.md; do not invent any numbers, tables or experiments.)")
    return idea_md, log_md


# --------------------------------------------------------------------------- #
# Custom LaTeX templates
# --------------------------------------------------------------------------- #
CUSTOM_TEMPLATE_RULES = r"""
## Notes for this custom template
* This template was supplied by the author. It may contain sample text, dummy sections, lorem ipsum, or
  publisher instructions. Treat all such sample content as placeholders and replace it with the real paper.
  Keep the document class, its options, the preamble (packages, macros) and required structural commands.
* Keep the main text to a typical conference/journal length (about 8-10 pages) unless the rules above say otherwise.
* Cite with \cite{} unless the template loads natbib (then \citep/\citet are fine) or biblatex.
* The bibliography file is `references.bib`. Keep `\bibliography{references}` (or `\addbibresource{references.bib}` with biblatex).
* Do not include author names or affiliations unless the rules above ask for them.
"""


def _safe_rel(path: str) -> str | None:
    path = path.replace("\\", "/").lstrip("/")
    norm = posixpath.normpath(path)
    if norm in (".", "") or norm.startswith("../") or norm == ".." or ":" in norm.split("/")[0]:
        return None
    parts = norm.split("/")
    if any(p in ("__MACOSX",) or p.startswith(".") for p in parts):
        return None
    return norm


def _collect(uploads: list[tuple[str, bytes]]) -> dict[str, bytes]:
    files: dict[str, bytes] = {}
    total = 0
    for name, data in uploads:
        if name.lower().endswith(".zip"):
            try:
                z = zipfile.ZipFile(io.BytesIO(data))
            except zipfile.BadZipFile:
                raise InputError(f"'{name}' is not a valid .zip file.")
            for info in z.infolist():
                if info.is_dir():
                    continue
                total += info.file_size
                if total > MAX_TEMPLATE_BYTES:
                    raise InputError("The template is larger than 50 MB.")
                rel = _safe_rel(info.filename)
                if rel:
                    files[rel] = z.read(info)
        else:
            total += len(data)
            if total > MAX_TEMPLATE_BYTES:
                raise InputError("The template is larger than 50 MB.")
            rel = _safe_rel(name)
            if rel:
                files[rel] = data
    return files


def _strip_prefix(files: dict[str, bytes], prefix: str) -> dict[str, bytes]:
    return {k[len(prefix):]: v for k, v in files.items() if k.startswith(prefix)}


def _pick_main(files: dict[str, bytes]) -> str:
    cands = []
    for rel, data in files.items():
        if rel.lower().endswith(".tex"):
            text = _decode(data)
            if "\\documentclass" in text:
                score = 0
                base = posixpath.basename(rel).lower()
                score += {"template.tex": 40, "main.tex": 30, "paper.tex": 20}.get(base, 0)
                score += 10 if "\\begin{document}" in text else 0
                score += 5 if "/" not in rel else 0
                score -= 30 if re.search(r"sample|example|demo|guide|instruction", base) else 0
                cands.append((score, rel))
    if not cands:
        raise InputError("No .tex file with \\documentclass was found in the template you uploaded.")
    return max(cands)[1]


def _fix_bibliography(tex: str) -> str:
    tex = re.sub(r"\\bibliography\{[^}]*\}", r"\\bibliography{references}", tex)
    tex = re.sub(r"\\addbibresource(\[[^\]]*\])?\{[^}]*\}", r"\\addbibresource{references.bib}", tex)
    if "\\bibliography{" not in tex and "\\addbibresource" not in tex and "\\end{document}" in tex:
        tex = tex.replace("\\end{document}", "\\bibliographystyle{plain}\n\\bibliography{references}\n\n\\end{document}", 1)
    return tex


def build_custom_template(uploads: list[tuple[str, bytes]], rules: str, dest: str) -> str:
    """Write a pipeline-ready template folder (template.tex + guidelines.md + support files).
    Returns the original name of the main .tex file."""
    files = _collect(uploads)
    if not files:
        raise InputError("Add your LaTeX template files (a .zip, a folder, or the .tex/.cls/.sty files).")

    # Drop a single shared top-level folder (common for zips and folder uploads)
    tops = {k.split("/", 1)[0] for k in files}
    if len(tops) == 1 and all("/" in k for k in files):
        files = _strip_prefix(files, tops.pop() + "/")

    main = _pick_main(files)
    if "/" in main:  # re-root at the main file's folder so relative paths keep working
        files = _strip_prefix(files, posixpath.dirname(main) + "/")
        main = posixpath.basename(main)

    guidelines = files.pop("guidelines.md", b"").decode("utf-8", errors="replace")
    main_tex = _fix_bibliography(_decode(files.pop(main)))
    for name in ("template.tex", "main.tex"):
        if name in files:  # avoid clashing with the files the pipeline writes
            files[f"orig_{name}"] = files.pop(name)

    os.makedirs(dest, exist_ok=True)
    for rel, data in files.items():
        path = os.path.join(dest, *rel.split("/"))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(data)
    with open(os.path.join(dest, "template.tex"), "w", encoding="utf-8") as f:
        f.write(main_tex)

    text = "# Paper Writing Guidelines\n\n"
    if rules.strip():
        text += "## Rules from the author\n" + rules.strip() + "\n\n"
    if guidelines.strip():
        text += guidelines.strip() + "\n\n"
    if not rules.strip() and not guidelines.strip():
        text += "No venue-specific rules were given. Follow the template's own formatting.\n\n"
    with open(os.path.join(dest, "guidelines.md"), "w", encoding="utf-8") as f:
        f.write(text + CUSTOM_TEMPLATE_RULES)
    return main


def unpack_project(uploads: list[tuple[str, bytes]], dest: str) -> str:
    """Write an existing LaTeX project (e.g. an Overleaf download) to dest unchanged.
    Returns the main .tex file name (relative to dest)."""
    files = _collect(uploads)
    if not files:
        raise InputError("Add your LaTeX project: the Overleaf .zip, a folder, or the .tex files.")
    tops = {k.split("/", 1)[0] for k in files}
    if len(tops) == 1 and all("/" in k for k in files):
        files = _strip_prefix(files, tops.pop() + "/")
    main = _pick_main(files)  # paths stay relative to the project root, as on Overleaf
    for rel, data in files.items():
        path = os.path.join(dest, *rel.split("/"))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(data)
    return main
