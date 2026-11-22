"""Web server: the FastAPI app behind the browser UI. Start it with `python -m paperscriptor`."""

import hashlib
import json
import os
import re
import shutil
import threading
import time
import uuid
import webbrowser

import requests
import uvicorn
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import config, ingest, latex, llm, pipeline, workspace
from .config import DEFAULT_SEARCH, DEFAULT_WRITER, TEMPLATE_LABELS


def _code_stamp() -> str:
    """Fingerprint of the package's Python files, to notice when they change while the server keeps running."""
    h = hashlib.sha256()
    for root, dirs, files in sorted(os.walk(config.PACKAGE_DIR)):
        dirs[:] = sorted(d for d in dirs if d not in ("__pycache__", "static", "templates"))
        for name in sorted(files):
            if name.endswith(".py"):
                with open(os.path.join(root, name), "rb") as f:
                    h.update(os.path.relpath(os.path.join(root, name), config.PACKAGE_DIR).encode() + f.read())
    return h.hexdigest()[:16]


LOADED_CODE = _code_stamp()
app = FastAPI(title="PaperScriptor")
app.mount("/static", StaticFiles(directory=config.STATIC_DIR), name="static")
JOBS: dict[str, llm.Job] = {}


@app.middleware("http")
async def no_stale_ui(request: Request, call_next):
    """Always revalidate the page and its CSS/JS, so an update shows up after a reload."""
    response = await call_next(request)
    if request.url.path == "/" or request.url.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-cache"
    return response


@app.get("/")
def index():
    return FileResponse(os.path.join(config.STATIC_DIR, "index.html"))


@app.get("/api/config")
def get_config():
    ids = [d for d in os.listdir(config.TEMPLATES_DIR) if os.path.exists(os.path.join(config.TEMPLATES_DIR, d, "template.tex"))]
    order = list(TEMPLATE_LABELS)
    ids.sort(key=lambda d: (order.index(d) if d in order else len(order), d))
    return {
        "templates": [{"id": d, "label": TEMPLATE_LABELS.get(d, d)} for d in ids],
        "default_writer": DEFAULT_WRITER,
        "default_search": DEFAULT_SEARCH,
        "default_cutoff": pipeline.default_cutoff(),
        "env_key": bool(os.environ.get("OPENROUTER_API_KEY")),
        "runs_dir": config.RUNS_DIR,
        "restart_needed": _code_stamp() != LOADED_CODE,
        "ollama_url": config.OLLAMA_URL,
    }


def local_models() -> list[str]:
    """Models installed in the local Ollama server, as "ollama/<name>" (empty if Ollama isn't running)."""
    try:
        tags = requests.get(f"{config.OLLAMA_URL}/api/tags", timeout=0.8).json().get("models") or []
        return sorted(llm.LOCAL_PREFIX + m["name"] for m in tags if m.get("name"))
    except Exception:
        return []


@app.get("/api/models")
def models():
    """Model boxes: your local Ollama models first, then a short list of recommended OpenRouter models."""
    local = local_models()
    items = [{"id": m, "label": "Local · free, private"} for m in local]
    items += [{"id": m, "label": label} for m, label in config.RECOMMENDED_MODELS]
    return {"models": items, "local": local}


def _safe_name(name: str, used: set) -> str:
    base, ext = os.path.splitext(os.path.basename(name or "figure.png"))
    base = re.sub(r"[^A-Za-z0-9_-]+", "_", base).strip("_") or "figure"
    ext = ext.lower()
    candidate, n = f"{base}{ext}", 2
    while candidate.lower() in used:
        candidate, n = f"{base}_{n}{ext}", n + 1
    used.add(candidate.lower())
    return candidate


@app.post("/api/extract")
async def extract(file: UploadFile = File(...)):
    """Read text out of an uploaded document (md, txt, tex, pdf, docx, csv, json...)."""
    try:
        text, warning = ingest.extract_text(file.filename, await file.read())
    except ingest.InputError as e:
        raise HTTPException(400, str(e))
    return {"name": file.filename, "text": text, "chars": len(text), "warning": warning}


class RunError(ValueError):
    """A problem with the inputs of a new run; the message is shown to the user."""


def prepare_run(*, api_key="", writer_model=DEFAULT_WRITER, search_model=DEFAULT_SEARCH, template="iclr2025",
                template_rules="", cutoff="", use_literature=True, use_web=False, refine_rounds=1,
                max_searches=20, budget=1.0,
                s2_api_key="", openalex_api_key="", materials=None, figures=(), template_uploads=()):
    """Validate the inputs, create the run folder and return (job, cfg). Shared by the web app and the CLI.
    materials: {idea, experimental_log, documents: [{name, role, text}]}
    figures: [(filename, bytes, caption)]; template_uploads: [(path, bytes)] when template == "__custom__"."""
    api_key = (api_key or "").strip() or os.environ.get("OPENROUTER_API_KEY", "")
    all_local = llm.is_local(writer_model or DEFAULT_WRITER) and (not use_literature or llm.is_local(search_model or DEFAULT_SEARCH))
    if not api_key and not all_local:
        raise RunError("Add your OpenRouter API key, or use local models (ollama/<model>) for every step.")
    mat = materials or {}
    try:
        idea, experimental_log = ingest.combine_materials(
            str(mat.get("idea") or ""), str(mat.get("experimental_log") or ""), mat.get("documents") or []
        )
    except ingest.InputError as e:
        raise RunError(str(e))
    custom = template == "__custom__"
    if not custom:
        template_dir = os.path.join(config.TEMPLATES_DIR, os.path.basename(template))
        if not os.path.exists(os.path.join(template_dir, "template.tex")):
            raise RunError(f"Unknown template '{template}'.")
    if cutoff and not re.fullmatch(r"\d{4}-\d{2}", cutoff):
        raise RunError("Research cutoff must look like 2025-06.")
    for name, _data, _cap in figures:
        if not name.lower().endswith((".png", ".jpg", ".jpeg", ".pdf")):
            raise RunError(f"'{name}' is not a PNG, JPG or PDF figure.")

    job_id = uuid.uuid4().hex[:8]
    run_dir = os.path.join(config.RUNS_DIR, f"{time.strftime('%Y%m%d-%H%M%S')}-{job_id}")
    template_label = template
    if custom:
        try:
            main_name = ingest.build_custom_template(list(template_uploads), template_rules,
                                                     os.path.join(run_dir, "template_src"))
        except ingest.InputError as e:
            shutil.rmtree(run_dir, ignore_errors=True)
            raise RunError(str(e))
        template_dir = os.path.join(run_dir, "template_src")
        template_label = f"your template (main file: {main_name})"
    fig_dir = os.path.join(run_dir, "paper", "figures")
    os.makedirs(fig_dir, exist_ok=True)

    # Save inputs so every run is reproducible from its folder
    os.makedirs(os.path.join(run_dir, "inputs", "documents"), exist_ok=True)
    with open(os.path.join(run_dir, "inputs", "idea.md"), "w", encoding="utf-8") as f:
        f.write(idea)
    with open(os.path.join(run_dir, "inputs", "experimental_log.md"), "w", encoding="utf-8") as f:
        f.write(experimental_log)
    used = set()
    for doc in mat.get("documents") or []:
        name = _safe_name(os.path.splitext(str(doc.get("name") or "document"))[0] + ".txt", used)
        with open(os.path.join(run_dir, "inputs", "documents", name), "w", encoding="utf-8") as f:
            f.write(f"role: {doc.get('role', 'idea')}\n\n{doc.get('text') or ''}")

    fig_info, used = [], set()
    for filename, data, caption in figures:
        name = _safe_name(filename, used)
        with open(os.path.join(fig_dir, name), "wb") as f:
            f.write(data)
        fig_info.append({"name": name, "caption": (caption or "").strip()})
    with open(os.path.join(fig_dir, "info.json"), "w", encoding="utf-8") as f:
        json.dump(fig_info, f, indent=2)

    cfg = {
        "api_key": api_key,
        "writer_model": (writer_model or "").strip() or DEFAULT_WRITER,
        "search_model": (search_model or "").strip() or DEFAULT_SEARCH,
        "template_dir": template_dir,
        "idea": idea,
        "experimental_log": experimental_log,
        "cutoff": cutoff or pipeline.default_cutoff(),
        "use_literature": use_literature,
        "use_web": use_web,
        "refine_rounds": max(0, min(3, int(refine_rounds))),
        "max_searches": max(0, min(200, int(max_searches))),
        "budget": max(0.0, float(budget)),
        "s2_api_key": (s2_api_key or "").strip() or os.environ.get("SEMANTIC_SCHOLAR_API_KEY", ""),
        "openalex_api_key": (openalex_api_key or "").strip() or os.environ.get("OPENALEX_API_KEY", ""),
        "figures": fig_info,
    }
    settings = {k: v for k, v in cfg.items()
                if k not in ("api_key", "s2_api_key", "openalex_api_key", "idea", "experimental_log")}
    with open(os.path.join(run_dir, "inputs", "settings.json"), "w", encoding="utf-8") as f:
        json.dump(settings, f, indent=2)

    job = llm.Job(job_id, run_dir)
    JOBS[job_id] = job
    n_docs = len(mat.get("documents") or [])
    job.log(f"Run started. Template: {template_label}, writer: {cfg['writer_model']}, "
            f"search: {cfg['search_model']}, cutoff: {cfg['cutoff']}"
            f"{f', {n_docs} document(s)' if n_docs else ''}.")
    return job, cfg


@app.post("/api/jobs")
async def create_job(
    api_key: str = Form(""),
    writer_model: str = Form(DEFAULT_WRITER),
    search_model: str = Form(DEFAULT_SEARCH),
    template: str = Form("iclr2025"),
    template_rules: str = Form(""),
    cutoff: str = Form(""),
    use_literature: bool = Form(True),
    use_web: bool = Form(False),
    refine_rounds: int = Form(1),
    max_searches: int = Form(20),
    budget: float = Form(1.0),
    s2_api_key: str = Form(""),
    openalex_api_key: str = Form(""),
    captions: str = Form("[]"),
    # JSON {idea, experimental_log, documents:[{name, role, text}]}, sent as a file part so
    # long documents are not limited by the 1 MB cap on plain form fields.
    materials: UploadFile = File(...),
    figures: list[UploadFile] = File(default=[]),
    template_files: list[UploadFile] = File(default=[]),
):
    try:
        mat = json.loads(await materials.read())
        if not isinstance(mat, dict):
            raise ValueError
    except ValueError:
        raise HTTPException(400, "Could not read the submitted materials.")
    try:
        caption_list = json.loads(captions or "[]")
    except ValueError:
        caption_list = []
    figs = []
    for i, up in enumerate(figures or []):
        if up.filename:
            cap = caption_list[i] if i < len(caption_list) and isinstance(caption_list[i], str) else ""
            figs.append((up.filename, await up.read(), cap))
    uploads = [(up.filename or "", await up.read()) for up in template_files or []] if template == "__custom__" else []
    try:
        job, cfg = prepare_run(api_key=api_key, writer_model=writer_model, search_model=search_model, template=template,
                               template_rules=template_rules, cutoff=cutoff, use_literature=use_literature,
                               use_web=use_web, refine_rounds=refine_rounds, max_searches=max_searches,
                               budget=budget, s2_api_key=s2_api_key,
                               openalex_api_key=openalex_api_key, materials=mat, figures=figs,
                               template_uploads=uploads)
    except RunError as e:
        raise HTTPException(400, str(e))
    threading.Thread(target=pipeline.run_pipeline, args=(job, cfg), daemon=True).start()
    return {"id": job.id}


def _job(job_id: str) -> pipeline.Job:
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(404, "Run not found (the server may have restarted).")
    return job


@app.get("/api/jobs/{job_id}")
def job_status(job_id: str, since: int = 0):
    return _job(job_id).snapshot(since)


@app.post("/api/jobs/{job_id}/cancel")
def cancel(job_id: str):
    job = _job(job_id)
    job.cancel_requested = True
    job.log("Cancel requested; stopping after the current model call...")
    return {"ok": True}


@app.get("/api/jobs/{job_id}/files/{name}")
def download(job_id: str, name: str):
    path = _job(job_id).files.get(name)  # only files the pipeline registered
    if not path or not os.path.exists(path):
        raise HTTPException(404, "File not available.")
    return FileResponse(path, filename=name)


# --------------------------------------------------------------------------- #
# Editor: chat edits, versions, PDF preview
# --------------------------------------------------------------------------- #
def _run(pid: str) -> str:
    try:
        return workspace.run_dir(config.RUNS_DIR, pid)
    except workspace.WorkspaceError as e:
        raise HTTPException(e.code, str(e))


def _ws(fn, *args):
    try:
        return fn(*args)
    except workspace.WorkspaceError as e:
        raise HTTPException(e.code, str(e))


class Attachment(BaseModel):
    name: str
    text: str


class ChatBody(BaseModel):
    message: str
    selection: str = ""
    open_path: str = ""
    model: str = ""
    api_key: str = ""
    attachments: list[Attachment] = []


class RenameBody(BaseModel):
    path: str
    new_path: str


class FileBody(BaseModel):
    path: str
    text: str = ""


class PathBody(BaseModel):
    path: str


class RestoreBody(BaseModel):
    version: int


@app.get("/api/projects")
def projects():
    return {"projects": workspace.list_projects(config.RUNS_DIR)}


@app.post("/api/projects/import")
async def import_project(files: list[UploadFile] = File(default=[])):
    """Open an existing LaTeX project (e.g. an Overleaf download) in the editor."""
    uploads = [(up.filename or "", await up.read()) for up in files or []]
    pid = f"{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}"
    run = os.path.join(config.RUNS_DIR, pid)
    try:
        main = ingest.unpack_project(uploads, os.path.join(run, "paper"))
        os.makedirs(os.path.join(run, "workspace"), exist_ok=True)
        open(os.path.join(run, "workspace", "imported"), "w").close()
        workspace.load_state(run, main)
    except (ingest.InputError, workspace.WorkspaceError) as e:
        shutil.rmtree(run, ignore_errors=True)
        raise HTTPException(400, str(e))
    return {"id": pid}


@app.get("/api/projects/{pid}")
def project(pid: str):
    return _ws(workspace.summary, _run(pid))


@app.delete("/api/projects/{pid}")
def project_delete_all(pid: str):
    run = os.path.join(config.RUNS_DIR, pid)
    if any(j.status == "running" and os.path.abspath(j.run_dir) == os.path.abspath(run) for j in JOBS.values()):
        raise HTTPException(409, "This paper is still being written. Cancel the run first.")
    return {"moved_to": _ws(workspace.delete_project, config.RUNS_DIR, pid)}


@app.get("/api/projects/{pid}/file")
def project_file(pid: str, path: str):
    return _ws(workspace.read_file, _run(pid), path)


@app.get("/api/projects/{pid}/raw")
def project_raw(pid: str, path: str):
    return FileResponse(_ws(workspace.raw_path, _run(pid), path), headers={"Cache-Control": "no-store"})


@app.post("/api/projects/{pid}/file")
def project_save(pid: str, body: FileBody):
    return _ws(workspace.save_file, _run(pid), body.path, body.text)


@app.post("/api/projects/{pid}/new")
def project_new(pid: str, body: PathBody):
    return _ws(workspace.new_file, _run(pid), body.path)


@app.post("/api/projects/{pid}/delete")
def project_delete(pid: str, body: PathBody):
    return _ws(workspace.delete_file, _run(pid), body.path)


@app.post("/api/projects/{pid}/upload")
async def project_upload(pid: str, files: list[UploadFile] = File(default=[]), folder: str = Form("")):
    run = _run(pid)
    uploads = [(up.filename or "file", await up.read()) for up in files or []]
    return _ws(workspace.upload, run, uploads, folder)


@app.post("/api/projects/{pid}/main")
def project_main(pid: str, body: PathBody):
    return _ws(workspace.set_main, _run(pid), body.path)


@app.post("/api/projects/{pid}/chat")
def project_chat(pid: str, body: ChatBody):
    key = body.api_key.strip() or os.environ.get("OPENROUTER_API_KEY", "")
    return _ws(workspace.chat, _run(pid), body.message, body.selection, body.open_path,
               body.model.strip() or DEFAULT_WRITER, key, [a.model_dump() for a in body.attachments])


@app.get("/api/projects/{pid}/chat/status")
def project_chat_status(pid: str):
    return workspace.chat_status(_run(pid))


@app.post("/api/projects/{pid}/rename")
def project_rename(pid: str, body: RenameBody):
    return _ws(workspace.rename_file, _run(pid), body.path, body.new_path)


@app.get("/api/projects/{pid}/synctex/forward")
def project_sync_forward(pid: str, path: str, line: int):
    return _ws(workspace.sync_forward, _run(pid), path, line)


@app.get("/api/projects/{pid}/synctex/inverse")
def project_sync_inverse(pid: str, page: int, x: float, y: float):
    return _ws(workspace.sync_inverse, _run(pid), page, x, y)


@app.post("/api/projects/{pid}/stop")
def project_stop(pid: str):
    workspace.stop(_run(pid))
    return {"ok": True}


@app.post("/api/projects/{pid}/restore")
def project_restore(pid: str, body: RestoreBody):
    return _ws(workspace.restore, _run(pid), body.version)


@app.post("/api/projects/{pid}/compile")
def project_compile(pid: str):
    return _ws(workspace.compile_run, _run(pid))


@app.get("/api/projects/{pid}/pdf")
def project_pdf(pid: str):
    path = _ws(workspace.pdf_path, _run(pid))
    if not path:
        raise HTTPException(404, "No PDF yet.")
    return FileResponse(path, media_type="application/pdf", headers={"Cache-Control": "no-store"})


@app.get("/api/projects/{pid}/zip")
def project_zip(pid: str):
    path = _ws(workspace.build_zip, _run(pid))
    return FileResponse(path, filename="paper.zip")


@app.get("/api/latex")
def latex_status():
    return latex.status()


@app.post("/api/latex/setup")
def latex_setup():
    latex.install_tectonic()
    return latex.status()


def serve(port: int | None = None, open_browser: bool = True):
    port = port or config.PORT
    url = f"http://{config.HOST}:{port}"
    print(f"\n  PaperScriptor is running at {url}\n  Outputs are saved in {config.RUNS_DIR}\n")
    if open_browser:
        threading.Timer(1.5, lambda: webbrowser.open(url)).start()
    uvicorn.run(app, host=config.HOST, port=port, log_level="warning")


if __name__ == "__main__":
    serve()
