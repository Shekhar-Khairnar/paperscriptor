"""Command-line tests with a mocked model and LaTeX engine (no network, no credits). Run: python tests/test_cli.py"""
import contextlib, io, os, shutil, sys, tempfile, time, zipfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["OPENROUTER_API_KEY"] = "sk-or-test"
from paperscriptor import cli, config, latex, llm, pipeline, server, workspace

TMP = tempfile.mkdtemp()
config.RUNS_DIR = TMP


def run(*argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = cli.main(list(argv))
    return code, out.getvalue() + err.getvalue()


# ---- empty state ----
code, out = run("list")
assert code == 0 and "No papers yet" in out
code, out = run("ask", "last", "hi")
assert code == 2 and "no papers yet" in out.lower(), out

# ---- import a folder ----
src = os.path.join(TMP, "_src", "MyPaper"); os.makedirs(os.path.join(src, "sections"))
open(os.path.join(src, "main.tex"), "w").write("\\documentclass{article}\n\\title{CLI Paper}\n\\begin{document}\n\\input{sections/intro}\nOld text.\n\\end{document}\n")
open(os.path.join(src, "sections", "intro.tex"), "w").write("\\section{Intro}\nHello.\n")
open(os.path.join(src, "supp.tex"), "w").write("\\documentclass{article}\\begin{document}S\\end{document}\n")
code, out = run("import", src)
assert code == 0 and "Main file: main.tex" in out and "supp.tex" in out, out
pid = workspace.list_projects(TMP)[0]["id"]
code, out = run("list")
assert pid in out and "CLI Paper" in out and "imported" in out
code, out = run("files", pid[-8:])
assert "main.tex  [main]" in out and "sections/intro.tex" in out, out

# ---- ask: edit two files, with live stages and diff ----
replies = ["I updated both files.\n\nmain.tex\n<<<<<<< SEARCH\nOld text.\n=======\nNew text.\n>>>>>>> REPLACE\n\n"
           "sections/intro.tex\n<<<<<<< SEARCH\nHello.\n=======\nHello, world.\n>>>>>>> REPLACE"]
def fake_stream(self, model, system, user, temperature=0.7, history=(), on_text=None):
    fake_stream.user = user
    self.job.add_cost(0.0004)
    on_text and on_text(replies[0][:30]); time.sleep(0.5)
    return replies.pop(0)
llm.LLM.stream_chat = fake_stream
notes = os.path.join(TMP, "_src", "reviews.md"); open(notes, "w").write("Reviewer 2: the intro is too short.")
latex.engine = lambda: None
code, out = run("ask", "last", "Fix the text", "--file", "main.tex", "--attach", notes, "--diff")
assert code == 0, out
assert "I updated both files." in out and "Changed main.tex, sections/intro.tex" in out and "+New text." in out, out
assert "=== ATTACHED: reviews.md ===" in fake_stream.user
r = workspace.run_dir(TMP, pid)
assert "New text." in open(os.path.join(r, "paper", "main.tex")).read()

# ---- versions, undo, restore ----
code, out = run("versions", "last")
assert "* v2" in out and "v1" in out, out
code, out = run("undo", "last")
assert code == 0 and "version 1" in out and "Old text." in open(os.path.join(r, "paper", "main.tex")).read()
code, out = run("restore", "last", "2")
assert "New text." in open(os.path.join(r, "paper", "main.tex")).read()

# ---- compile: no engine, then a fake one, choosing another main file ----
code, out = run("compile", "last")
assert code == 2 and "setup-latex" in out
def fake_compile(paper_dir, main="main.tex", timeout=600, search_dirs=()):
    pdf = os.path.join(paper_dir, os.path.splitext(os.path.basename(main))[0] + ".pdf"); open(pdf, "wb").write(b"%PDF")
    return {"ok": True, "pdf": pdf, "engine": "tectonic", "errors": [], "seconds": 0.5, "message": ""}
latex.engine = lambda: "tectonic"; latex.compile_project = fake_compile
code, out = run("compile", "last", "--main", "supp.tex")
assert code == 0 and "Compiled supp.tex with tectonic" in out and "supp.pdf" in out, out

# ---- export ----
dest = os.path.join(TMP, "_src", "out.zip")
code, out = run("export", "last", "-o", dest)
assert code == 0 and "main.tex" in zipfile.ZipFile(dest).namelist()
pdf_dest = os.path.join(TMP, "_src", "out.pdf")
code, out = run("export", "last", "--pdf", "-o", pdf_dest)
assert code == 0 and open(pdf_dest, "rb").read().startswith(b"%PDF"), out

# ---- write: full pipeline with a mocked model ----
idea = os.path.join(TMP, "_src", "idea.md"); open(idea, "w").write("We propose a tiny model.")
log = os.path.join(TMP, "_src", "results.csv"); open(log, "w").write("model,acc\nours,91.2\n")
called = {}
def fake_run(job, cfg):
    called["cfg"] = cfg
    job.log("Step 1/5 · Outline agent")
    os.makedirs(os.path.join(job.run_dir, "paper"), exist_ok=True)
    open(os.path.join(job.run_dir, "paper", "main.tex"), "w").write("\\documentclass{article}\\title{Tiny}\\begin{document}x\\end{document}")
    job.status = "done"; job.log("Finished.")
pipeline.run_pipeline = fake_run
code, out = run("write", "--idea", idea, "--log", log, "--template", "nature", "--rounds", "0", "--no-web")
assert code == 0, out
assert "Step 1/5" in out and "Done. Project:" in out, out
cfg = called["cfg"]
assert "tiny model" in cfg["idea"] and "91.2" in cfg["experimental_log"] and cfg["refine_rounds"] == 0 and cfg["use_web"] is False
assert cfg["template_dir"].endswith(os.path.join("templates", "nature"))
code, out = run("write", "--idea", idea, "--template", "nope")
assert code == 2 and "Unknown template" in out
code, out = run("write", "--idea", os.path.join(TMP, "missing.md"))
assert code == 2

# ---- write from a whole folder (documents, figures, captions file; reviews and scripts skipped) ----
from paperscriptor.workspace.build import _png
study = os.path.join(TMP, "_study"); os.makedirs(os.path.join(study, "figures"))
open(os.path.join(study, "idea.md"), "w").write("SYNTHETIC. We propose CheckNet, a small model.")
open(os.path.join(study, "experimental_log.md"), "w").write("Main results: CheckNet F1 88.1 vs baseline 84.0.")
open(os.path.join(study, "results.csv"), "w").write("model,f1\nCheckNet,88.1\nBaseline,84.0\n")
open(os.path.join(study, "figures", "fig1_main_results.png"), "wb").write(_png(20, 10))
open(os.path.join(study, "fig2_ablations.png"), "wb").write(_png(20, 10))
open(os.path.join(study, "figure_captions.md"), "w").write(
    "# Captions\n**fig1_main_results.png**: F1 of each model, mean over 3 seeds.\n\nfig2_ablations.png\nF1 when parts are removed.\n")
open(os.path.join(study, "reviewer_comments.md"), "w").write("Reviewer 1: unclear metric.")
open(os.path.join(study, "make_figures.py"), "w").write("print('plot')")
code, out = run("write", study, "--template", "nature", "--rounds", "0", "--no-web")
assert code == 0, out
cfg = called["cfg"]
assert "CheckNet, a small model" in cfg["idea"] and "88.1" in cfg["experimental_log"], cfg["idea"][:200]
assert "results.csv" in cfg["experimental_log"] and "Unclear metric" not in cfg["idea"] and "unclear metric" not in cfg["idea"]
caps = {f["name"]: f["caption"] for f in cfg["figures"]}
assert caps == {"fig1_main_results.png": "F1 of each model, mean over 3 seeds.",
                "fig2_ablations.png": "F1 when parts are removed."}, caps
assert "skipped reviewer_comments.md" in out and "skipped make_figures.py" in out, out
assert cfg["template_dir"].endswith(os.path.join("templates", "nature"))
code, out = run("write", os.path.join(TMP, "no_such_folder"))
assert code == 2 and "not a folder" in out

# A folder with one combined notes file: it is both the idea and the results
single = os.path.join(TMP, "_single"); os.makedirs(single)
open(os.path.join(single, "notes.md"), "w").write("Idea: CheckNet. Results: F1 88.1 vs 84.0.")
code, out = run("write", single, "--rounds", "0")
assert code == 0, out
assert "F1 88.1" in called["cfg"]["idea"] and "F1 88.1" in called["cfg"]["experimental_log"], called["cfg"]["experimental_log"]
assert "notes.md (both)" in out, out
from paperscriptor import ingest  # the web form: one document left as "Idea" still reaches the writer as results
idea_md, log_md = ingest.combine_materials("", "", [{"name": "notes.pdf", "role": "idea", "text": "F1 88.1"}])
assert "F1 88.1" in idea_md and "F1 88.1" in log_md, log_md
idea_md, log_md = ingest.combine_materials("", "", [{"name": "a.md", "role": "idea", "text": "Idea"},
                                                    {"name": "r.csv", "role": "results", "text": "f1,88.1"}])
assert "Idea" not in log_md and "88.1" in log_md

shutil.rmtree(TMP, ignore_errors=True)
print("ALL CLI TESTS PASSED")
