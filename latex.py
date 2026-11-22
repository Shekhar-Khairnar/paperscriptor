"""Compiling LaTeX to PDF: finds a local engine, or sets up Tectonic (a single-file LaTeX engine) on request."""

import hashlib
import io
import os
import re
import shutil
import subprocess
import threading
import time
import zipfile

import requests

ROOT = os.path.dirname(os.path.abspath(__file__))
TOOLS_DIR = os.path.join(ROOT, "tools", "tectonic")
TECTONIC_EXE = os.path.join(TOOLS_DIR, "tectonic.exe" if os.name == "nt" else "tectonic")

# Official release from github.com/tectonic-typesetting/tectonic, pinned with its published SHA-256.
TECTONIC_VERSION = "0.17.0"
TECTONIC_URL = ("https://github.com/tectonic-typesetting/tectonic/releases/download/"
                "tectonic%400.17.0/tectonic-0.17.0-x86_64-pc-windows-msvc.zip")
TECTONIC_SHA256 = "f61ce51f0b0ade1015b7de7ef368541c5424e9756ecbd0d7af97d6d48030845f"

_setup = {"running": False, "error": None}
_compile_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def engine() -> str | None:
    """Name of the engine that will be used, or None if there is none."""
    if shutil.which("pdflatex") and shutil.which("bibtex"):
        return "pdflatex"
    if shutil.which("tectonic"):
        return "tectonic"
    if os.path.exists(TECTONIC_EXE):
        return "tectonic"
    return None


def status() -> dict:
    return {
        "engine": engine(),
        "installing": _setup["running"],
        "error": _setup["error"],
        "can_install": os.name == "nt",
    }


def download_tectonic() -> None:
    """Download Tectonic, check it against the pinned SHA-256, and put tectonic.exe in tools/tectonic.
    Raises on any problem; nothing is installed unless the checksum matches."""
    r = requests.get(TECTONIC_URL, timeout=300)
    r.raise_for_status()
    if hashlib.sha256(r.content).hexdigest() != TECTONIC_SHA256:
        raise RuntimeError("The downloaded file did not match the expected checksum, so it was not installed.")
    with zipfile.ZipFile(io.BytesIO(r.content)) as z:
        member = next((n for n in z.namelist() if os.path.basename(n).lower() in ("tectonic.exe", "tectonic")), None)
        if not member:
            raise RuntimeError("The download did not contain tectonic.exe.")
        data = z.read(member)
    os.makedirs(TOOLS_DIR, exist_ok=True)
    tmp = TECTONIC_EXE + ".part"
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, TECTONIC_EXE)


def install_tectonic():
    """Set up Tectonic in a background thread (used by the 'Set up PDF preview' button)."""
    if _setup["running"] or engine():
        return
    _setup.update(running=True, error=None)

    def work():
        try:
            download_tectonic()
        except Exception as e:
            _setup["error"] = f"Could not set up Tectonic: {e}"
        finally:
            _setup["running"] = False

    threading.Thread(target=work, daemon=True).start()


def _lock(path: str) -> threading.Lock:
    with _locks_guard:
        return _compile_locks.setdefault(os.path.abspath(path), threading.Lock())


FILE_LINE_RE = re.compile(r"^(?:\./)?((?:[A-Za-z]:)?[^:\s][^:]*?\.(?:tex|sty|cls|bib|bbl|ltx|def|cfg)):(\d+):\s*(.+)$")


def _norm_file(name: str | None) -> str | None:
    if not name:
        return None
    name = name.replace("\\", "/")
    return name[2:] if name.startswith("./") else name


def _errors(log_text: str, out_text: str) -> list[dict]:
    """Pull readable errors out of a LaTeX log (and Tectonic's console output). Each has file, line, message."""
    errs = []
    lines = log_text.splitlines()
    for i, line in enumerate(lines):
        fm = FILE_LINE_RE.match(line)          # -file-line-error style: ./sections/intro.tex:12: Undefined ...
        if line.startswith("! ") or fm:
            msg = (fm.group(3) if fm else line[2:]).strip()
            where = int(fm.group(2)) if fm else None
            for nxt in lines[i + 1:i + 12]:
                m = re.match(r"l\.(\d+)\s?(.*)", nxt)
                if m:
                    where = where or int(m.group(1))
                    if m.group(2).strip():
                        msg += f" (near: {m.group(2).strip()[:80]})"
                    break
            errs.append({"file": _norm_file(fm.group(1)) if fm else None, "line": where, "message": msg})
    if not errs:
        for line in out_text.splitlines():
            m = re.match(r"error:\s*(?:(\S+?\.(?:tex|sty|cls|bib)):(\d+):\s*)?(.*)", line.strip())
            if m and m.group(3) and "halted on potentially-recoverable error" not in m.group(3):
                errs.append({"file": _norm_file(m.group(1)), "line": int(m.group(2)) if m.group(2) else None,
                             "message": m.group(3).strip()})
    seen, out = set(), []
    for e in errs:
        key = (e["file"], e["line"], e["message"])
        if key not in seen:
            seen.add(key)
            out.append(e)
    return out[:8]


def compile_project(paper_dir: str, main: str = "main.tex", timeout: int = 600, search_dirs=()) -> dict:
    """Compile paper_dir/main. Returns {ok, pdf, engine, errors, seconds, message}.
    search_dirs: extra folders LaTeX also looks in (used for placeholder images of missing figures).
    Like Overleaf, compiling continues past errors so a PDF is produced whenever possible."""
    eng = engine()
    stem = os.path.splitext(os.path.basename(main))[0]  # outputs land in paper_dir, named after the file
    pdf = os.path.join(paper_dir, stem + ".pdf")
    if not eng:
        return {"ok": False, "pdf": None, "engine": None, "errors": [], "seconds": 0,
                "message": "No LaTeX engine is installed yet."}
    with _lock(paper_dir):
        t0 = time.time()
        before = os.path.getmtime(pdf) if os.path.exists(pdf) else 0
        out_parts = []
        try:
            if eng == "pdflatex":
                tex = open(os.path.join(paper_dir, main), encoding="utf-8", errors="replace").read()
                bib = ["biber", stem] if ("\\addbibresource" in tex and shutil.which("biber")) else ["bibtex", stem]
                pl = ["pdflatex", "-interaction=nonstopmode", "-file-line-error", "-synctex=1", main]
                cmds = [pl, bib, pl, pl]
            else:
                exe = shutil.which("tectonic") or TECTONIC_EXE
                extra = [a for d in search_dirs for a in ("-Z", f"search-path={d}")]
                cmds = [[exe, "--keep-logs", "--synctex", "-Z", "continue-on-errors", *extra, "--outdir", ".", main]]
            env = dict(os.environ)
            if search_dirs:  # pdflatex/bibtex (TeX Live and MiKTeX): a trailing separator keeps the default paths
                env["TEXINPUTS"] = os.pathsep.join(search_dirs) + os.pathsep + env.get("TEXINPUTS", "")
            for cmd in cmds:
                left = max(30, timeout - (time.time() - t0))
                p = subprocess.run(cmd, cwd=paper_dir, capture_output=True, text=True,
                                   encoding="utf-8", errors="replace", timeout=left, env=env)
                out_parts.append((p.stdout or "") + (p.stderr or ""))
        except subprocess.TimeoutExpired:
            return {"ok": False, "pdf": pdf if os.path.exists(pdf) else None, "engine": eng, "errors": [],
                    "seconds": round(time.time() - t0, 1), "message": "Compiling took too long and was stopped."}
        except Exception as e:
            return {"ok": False, "pdf": None, "engine": eng, "errors": [], "seconds": round(time.time() - t0, 1),
                    "message": f"Could not run {eng}: {e}"}
        log_path = os.path.join(paper_dir, stem + ".log")
        log_text = open(log_path, encoding="utf-8", errors="replace").read() if os.path.exists(log_path) else ""
        out_text = "\n".join(out_parts)
        errors = _errors(log_text, out_text)
        fresh = os.path.exists(pdf) and os.path.getmtime(pdf) > before
        msg = "" if fresh else (out_text.strip().splitlines() or ["The PDF was not produced."])[-1][:300]
        return {"ok": fresh and not errors, "pdf": pdf if fresh else None, "engine": eng, "errors": errors,
                "seconds": round(time.time() - t0, 1), "message": msg}
