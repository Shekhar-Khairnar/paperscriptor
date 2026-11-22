"""Command-line interface for Paper Orchestra Local. Everything the web app does, from a terminal.

Examples
  python cli.py write --idea notes.md --log results.csv --template nature
  python cli.py import my-overleaf-project.zip
  python cli.py list
  python cli.py ask last "Shorten the abstract to 150 words"
  python cli.py ask last "Add citations for the claims in the introduction" --file sections/intro.tex
  python cli.py compile last
  python cli.py undo last
  python cli.py export last -o paper.zip
  python cli.py serve

PROJECT can be "last" (the most recently changed paper), a full id from `list`, or its start or end
(e.g. the last 8 characters).
The OpenRouter key comes from --api-key, the OPENROUTER_API_KEY environment variable, or the .env file.
"""

import argparse
import os
import shutil
import sys
import threading
import time

import app  # loads .env and shares the settings and run logic with the web app
import ingest
import latex
import pipeline
import workspace

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


class CliError(Exception):
    pass


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def resolve(project: str) -> str:
    """Turn 'last', a full id or an id prefix into a run folder."""
    projects = workspace.list_projects(app.RUNS_DIR)
    if not projects:
        raise CliError("There are no papers yet. Use `write` or `import` first.")
    if project in ("last", "latest", ""):
        pid = projects[0]["id"]
    else:
        hits = [p["id"] for p in projects if p["id"] == project] or \
               [p["id"] for p in projects if p["id"].startswith(project) or p["id"].endswith(project)]
        if not hits:
            raise CliError(f"No paper matches '{project}'. Run `python cli.py list` to see them.")
        if len(hits) > 1:
            raise CliError(f"'{project}' matches several papers: {', '.join(hits)}")
        pid = hits[0]
    return workspace.run_dir(app.RUNS_DIR, pid)


def read_doc(path: str) -> str:
    with open(path, "rb") as f:
        text, warning = ingest.extract_text(os.path.basename(path), f.read())
    if warning:
        print(f"  note: {os.path.basename(path)}: {warning}")
    return text


def split_suffix(arg: str, choices=None) -> tuple[str, str]:
    """'file.pdf:results' -> ('file.pdf', 'results'); keeps Windows drive letters like C:\\x intact."""
    if ":" in arg and not os.path.exists(arg):
        path, _, extra = arg.rpartition(":")
        if path and (choices is None or extra in choices):
            return path, extra
    return arg, ""


def guess_role(name: str) -> str:
    import re
    return "results" if re.search(r"result|experiment|eval|ablation|benchmark|table|metric|\blog\b|data|\.(csv|tsv|json)$",
                                  name, re.I) else "idea"


def need_key(args) -> str:
    key = (args.api_key or os.environ.get("OPENROUTER_API_KEY", "")).strip()
    if not key:
        raise CliError("No OpenRouter key. Pass --api-key, set OPENROUTER_API_KEY, or put it in the .env file.")
    return key


def print_compile(d: dict):
    c = d.get("compile") or {}
    if not c.get("engine"):
        print("No LaTeX engine yet. Run `python cli.py setup-latex` (or install MiKTeX / TeX Live).")
        return
    pdf = workspace.pdf_path(d["run_dir"])
    print(f"Compiled {c.get('main')} with {c.get('engine')} in {c.get('seconds')}s.")
    for m in c.get("missing") or []:
        print(f"  missing figure (placeholder used): {m['name']}  ({m['file']}, line {m['line']})")
    for e in c.get("errors") or []:
        where = f"{e['file']}, line {e['line']}: " if e.get("file") and e.get("line") else (f"line {e['line']}: " if e.get("line") else "")
        print(f"  error: {where}{e['message']}")
    if c.get("message") and not c.get("pdf"):
        print(f"  {c['message']}")
    print(f"PDF: {pdf}" if pdf and c.get("pdf") else "No PDF was produced.")


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #
def cmd_write(args):
    docs, idea_parts, log_parts = [], [], []
    for path in args.idea or []:
        idea_parts.append(read_doc(path))
    for path in args.log or []:
        log_parts.append(read_doc(path))
    for arg in args.doc or []:
        path, role = split_suffix(arg, ("idea", "results", "both"))
        docs.append({"name": os.path.basename(path), "role": role or guess_role(path), "text": read_doc(path)})
    figures = []
    for arg in args.figure or []:
        path, caption = split_suffix(arg)
        with open(path, "rb") as f:
            figures.append((os.path.basename(path), f.read(), caption))

    template, uploads = args.template, []
    if os.path.exists(template):  # a journal's own kit: .zip, folder or .tex file
        if os.path.isdir(template):
            for root, _, files in os.walk(template):
                for n in files:
                    full = os.path.join(root, n)
                    with open(full, "rb") as f:
                        uploads.append((os.path.relpath(full, template).replace("\\", "/"), f.read()))
        else:
            with open(template, "rb") as f:
                uploads.append((os.path.basename(template), f.read()))
        template = "__custom__"
    try:
        job, cfg = app.prepare_run(
            api_key=need_key(args), writer_model=args.writer, search_model=args.searcher, template=template,
            template_rules=args.rules or "", cutoff=args.cutoff or "", use_literature=not args.no_literature,
            use_web=not args.no_web and not args.no_literature, refine_rounds=args.rounds,
            s2_api_key=os.environ.get("SEMANTIC_SCHOLAR_API_KEY", ""), openalex_api_key=os.environ.get("OPENALEX_API_KEY", ""),
            materials={"idea": "\n\n".join(idea_parts), "experimental_log": "\n\n".join(log_parts), "documents": docs},
            figures=figures, template_uploads=uploads)
    except app.RunError as e:
        raise CliError(str(e))

    t = threading.Thread(target=pipeline.run_pipeline, args=(job, cfg), daemon=True)
    t.start()
    shown = 0
    try:
        while t.is_alive() or shown < len(job.logs):
            for line in job.logs[shown:]:
                print(line)
            shown = len(job.logs)
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("\nStopping after the current model call...")
        job.cancel_requested = True
        t.join()
        for line in job.logs[shown:]:
            print(line)
    pid = os.path.basename(job.run_dir)
    if job.status == "done":
        print(f"\nDone. Project: {pid}\nFolder:  {job.run_dir}")
        if "paper.pdf" in job.files:
            print(f"PDF:     {job.files['paper.pdf']}")
        print(f'Next:    python cli.py ask {pid[-8:]} "your change"   or   python cli.py serve')
        return 0
    return 1


def cmd_import(args):
    uploads = []
    for path in args.paths:
        if os.path.isdir(path):
            for root, _, files in os.walk(path):
                for n in files:
                    full = os.path.join(root, n)
                    with open(full, "rb") as f:
                        uploads.append((os.path.join(os.path.basename(os.path.abspath(path)),
                                                     os.path.relpath(full, path)).replace("\\", "/"), f.read()))
        else:
            with open(path, "rb") as f:
                uploads.append((os.path.basename(path), f.read()))
    pid = f"{time.strftime('%Y%m%d-%H%M%S')}-{os.urandom(4).hex()}"
    run = os.path.join(app.RUNS_DIR, pid)
    try:
        main = ingest.unpack_project(uploads, os.path.join(run, "paper"))
        os.makedirs(os.path.join(run, "workspace"), exist_ok=True)
        open(os.path.join(run, "workspace", "imported"), "w").close()
        workspace.load_state(run, main)
    except (ingest.InputError, workspace.WorkspaceError) as e:
        shutil.rmtree(run, ignore_errors=True)
        raise CliError(str(e))
    d = workspace.summary(run)
    print(f"Imported {len(d['files'])} file(s) as project {pid}. Main file: {d['main']}")
    if len(d["main_candidates"]) > 1:
        print(f"Other documents you can compile: {', '.join(c for c in d['main_candidates'] if c != d['main'])}")
    return 0


def cmd_list(args):
    projects = workspace.list_projects(app.RUNS_DIR)
    if not projects:
        print("No papers yet.")
    for p in projects:
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(p["updated"]))
        print(f"{p['id']}  {when}  {'imported ' if p['imported'] else 'written  '}  {p['title']}")
    return 0


def cmd_files(args):
    d = workspace.summary(resolve(args.project))
    for f in d["files"]:
        tag = "  [main]" if f["path"] == d["main"] else ""
        print(f"{f['size']:>10,}  {f['path']}{tag}")
    return 0


def cmd_ask(args):
    run = resolve(args.project)
    attachments = [{"name": os.path.basename(p), "text": read_doc(p)} for p in args.attach or []]
    model = args.model or app.DEFAULT_WRITER
    result, error = {}, {}

    def work():
        try:
            result["d"] = workspace.chat(run, args.message, args.selection or "", args.file or "", model,
                                         need_key(args), attachments)
        except (workspace.WorkspaceError, CliError) as e:
            error["e"] = str(e)

    t = threading.Thread(target=work, daemon=True)
    t.start()
    last_stage = None
    try:
        while t.is_alive():
            st = workspace.chat_status(run)
            if st.get("busy") and st.get("stage") != last_stage:
                last_stage = st["stage"]
                print(f"  {last_stage}")
            time.sleep(0.4)
    except KeyboardInterrupt:
        print("Stopping...")
        workspace.stop(run)
        t.join()
    if error:
        raise CliError(error["e"])
    d = result["d"]
    m = d["messages"][-1]
    print("\n" + m["text"])
    if m.get("version"):
        diff = m["diff"]
        print(f"\nChanged {', '.join(diff['files'])} (+{diff['added']} -{diff['removed']} lines), now version {m['version']}."
              f" Cost ${m.get('cost', 0):.4f}.")
        if args.diff:
            print("\n" + diff["text"])
        if not args.no_compile and d["latex"]["engine"]:
            print()
            print_compile(workspace.compile_run(run))
    else:
        print(f"\nNo files changed. Cost ${m.get('cost', 0):.4f}.")
    return 1 if m.get("error") else 0


def cmd_compile(args):
    run = resolve(args.project)
    if args.main:
        workspace.set_main(run, args.main)
    if not latex.engine():
        raise CliError("No LaTeX engine yet. Run `python cli.py setup-latex` (or install MiKTeX / TeX Live).")
    print("Compiling... (the first time can take a few minutes while LaTeX packages download)")
    print_compile(workspace.compile_run(run))
    return 0


def cmd_versions(args):
    d = workspace.summary(resolve(args.project))
    for v in d["versions"]:
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(v["time"]))
        print(f"{'*' if v['n'] == d['version'] else ' '} v{v['n']:<4} {when}  {v['note']}")
    return 0


def cmd_restore(args):
    run = resolve(args.project)
    d = workspace.summary(run)
    target = args.version
    if target is None:  # undo: go back to the version before the current one
        older = [v["n"] for v in d["versions"] if v["n"] < d["version"]]
        if not older:
            raise CliError("Nothing to undo.")
        target = max(older)
    d = workspace.restore(run, target)
    print(f"Went back to version {target} (saved as version {d['version']}).")
    return 0


def cmd_export(args):
    run = resolve(args.project)
    path = workspace.build_zip(run)
    if args.output:
        shutil.copy(path, args.output)
        path = args.output
    print(f"Saved {path}")
    return 0


def cmd_setup_latex(args):
    import setup_latex
    return setup_latex.main()


def cmd_serve(args):
    app.serve(port=args.port, open_browser=not args.no_browser)
    return 0


# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python cli.py", description="Paper Orchestra Local from the command line.",
                                formatter_class=argparse.RawDescriptionHelpFormatter,
                                epilog='PROJECT can be "last", a full id from `list`, or its start or end (e.g. the last 8 characters).')
    p.add_argument("--api-key", help="OpenRouter key (default: OPENROUTER_API_KEY or .env)")
    sub = p.add_subparsers(dest="command", required=True)

    w = sub.add_parser("write", help="write a new paper from your notes and results")
    w.add_argument("--idea", action="append", metavar="FILE", help="idea / method notes (md, txt, pdf, docx...); repeatable")
    w.add_argument("--log", action="append", metavar="FILE", help="experimental results; repeatable")
    w.add_argument("--doc", action="append", metavar="FILE[:ROLE]", help="extra document, ROLE = idea, results or both")
    w.add_argument("--figure", action="append", metavar="FILE[:CAPTION]", help="figure (png, jpg, pdf) with a caption")
    w.add_argument("--template", default="iclr2025",
                   help="iclr2025, cvpr2025, nature, or a path to your own template (.zip, folder or .tex)")
    w.add_argument("--rules", help="formatting rules for your own template, e.g. '8 pages max'")
    w.add_argument("--writer", default=app.DEFAULT_WRITER, help=f"writing model (default {app.DEFAULT_WRITER})")
    w.add_argument("--searcher", default=app.DEFAULT_SEARCH, help=f"paper-search model (default {app.DEFAULT_SEARCH})")
    w.add_argument("--cutoff", help="only cite papers up to this month, e.g. 2025-06")
    w.add_argument("--rounds", type=int, default=1, help="review-and-refine rounds, 0 to 3 (default 1)")
    w.add_argument("--no-literature", action="store_true", help="don't search for and cite papers")
    w.add_argument("--no-web", action="store_true", help="don't use web search when finding papers")
    w.set_defaults(func=cmd_write)

    i = sub.add_parser("import", help="import an existing LaTeX project (Overleaf .zip, folder or files)")
    i.add_argument("paths", nargs="+")
    i.set_defaults(func=cmd_import)

    sub.add_parser("list", help="list your papers").set_defaults(func=cmd_list)

    f = sub.add_parser("files", help="list a paper's files")
    f.add_argument("project")
    f.set_defaults(func=cmd_files)

    a = sub.add_parser("ask", help="ask the model to change the paper (or ask a question about it)")
    a.add_argument("project")
    a.add_argument("message")
    a.add_argument("--file", help="the file you are talking about, e.g. sections/intro.tex")
    a.add_argument("--selection", help="a passage you want changed (copied from the file)")
    a.add_argument("--attach", action="append", metavar="FILE", help="attach notes, results or reviews; repeatable")
    a.add_argument("--model", help=f"model to use (default {app.DEFAULT_WRITER})")
    a.add_argument("--diff", action="store_true", help="print the exact changes")
    a.add_argument("--no-compile", action="store_true", help="don't recompile after a change")
    a.set_defaults(func=cmd_ask)

    c = sub.add_parser("compile", help="compile the paper to PDF")
    c.add_argument("project")
    c.add_argument("--main", help="compile this .tex file instead (it becomes the main file)")
    c.set_defaults(func=cmd_compile)

    v = sub.add_parser("versions", help="show the saved versions")
    v.add_argument("project")
    v.set_defaults(func=cmd_versions)

    u = sub.add_parser("undo", help="go back to the previous version")
    u.add_argument("project")
    u.set_defaults(func=cmd_restore, version=None)

    r = sub.add_parser("restore", help="go back to a specific version")
    r.add_argument("project")
    r.add_argument("version", type=int)
    r.set_defaults(func=cmd_restore)

    e = sub.add_parser("export", help="save the project as a .zip (for Overleaf)")
    e.add_argument("project")
    e.add_argument("-o", "--output", help="where to save the .zip")
    e.set_defaults(func=cmd_export)

    sub.add_parser("setup-latex", help="download the LaTeX engine used for PDFs (one time)").set_defaults(func=cmd_setup_latex)

    s = sub.add_parser("serve", help="start the web app")
    s.add_argument("--port", type=int, default=app.PORT)
    s.add_argument("--no-browser", action="store_true")
    s.set_defaults(func=cmd_serve)
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args) or 0
    except (CliError, workspace.WorkspaceError, FileNotFoundError) as e:
        print(f"Error: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
