"""Editor tests (multi-file) with a mocked model and a mocked LaTeX engine (no network, no credits)."""
import io, os, sys, shutil, tempfile, zipfile, json
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["OPENROUTER_API_KEY"] = "sk-or-test"
from paperscriptor import config, latex, llm, pipeline, references, server, workspace
from fastapi.testclient import TestClient

TMP = tempfile.mkdtemp()
config.RUNS_DIR = TMP
c = TestClient(server.app)

MAIN = r"""\documentclass{article}
\usepackage{natbib}
\title{Sparse Attention for Long Documents}
\begin{document}
\maketitle
\begin{abstract}
We study sparse attention. It is fast. Results show a 12.3\% gain on LongBench.
\end{abstract}
\input{sections/intro}
\section{Conclusion}
We presented a method.
\bibliographystyle{plainnat}
\bibliography{refs}
\end{document}
"""
INTRO = r"""\section{Introduction}
Long documents are hard \citep{vaswani2017}.
Transformers scale   quadratically
with length.
"""
SUPP = r"""\documentclass{article}
\begin{document}
\section{Extra results}
Supplementary text.
\end{document}
"""
BIB = "@article{vaswani2017,\n  title={Attention Is All You Need},\n  year={2017}\n}\n"

# ---- import an "Overleaf" zip with folders ----
buf = io.BytesIO()
with zipfile.ZipFile(buf, "w") as z:
    z.writestr("MyPaper/main.tex", MAIN)
    z.writestr("MyPaper/sections/intro.tex", INTRO)
    z.writestr("MyPaper/supplementary.tex", SUPP)
    z.writestr("MyPaper/refs.bib", BIB)
    z.writestr("MyPaper/figures/plot.png", b"\x89PNG fake")
    z.writestr("MyPaper/main.aux", "aux")
    pass
r = c.post("/api/projects/import", files=[("files", ("MyPaper.zip", buf.getvalue(), "application/zip"))])
assert r.status_code == 200, r.text
pid = r.json()["id"]
assert not os.path.exists(os.path.join(TMP, "evil.txt"))
slip = io.BytesIO()
with zipfile.ZipFile(slip, "w") as z:
    z.writestr("main.tex", MAIN); z.writestr("../evil.txt", "x"); z.writestr("..\evil2.txt", "x")
r2 = c.post("/api/projects/import", files=[("files", ("s.zip", slip.getvalue(), "application/zip"))])
assert r2.status_code == 200 and not os.path.exists(os.path.join(TMP, "evil.txt"))
assert [f["path"] for f in c.get(f"/api/projects/{r2.json()['id']}").json()["files"]] == ["main.tex"]
d = c.get(f"/api/projects/{pid}").json()
paths = [f["path"] for f in d["files"]]
assert paths == ["figures/plot.png", "main.tex", "refs.bib", "sections/intro.tex", "supplementary.tex"], paths
assert d["main"] == "main.tex" and sorted(d["main_candidates"]) == ["main.tex", "supplementary.tex"]
assert d["title"] == "Sparse Attention for Long Documents"
kinds = {f["path"]: (f["kind"], f["editable"]) for f in d["files"]}
assert kinds["figures/plot.png"] == ("image", False) and kinds["refs.bib"] == ("bib", True)
print("import ok:", paths)

# ---- read / raw / path safety ----
assert c.get(f"/api/projects/{pid}/file", params={"path": "sections/intro.tex"}).json()["text"] == INTRO
assert c.get(f"/api/projects/{pid}/file", params={"path": "figures/plot.png"}).status_code == 415
assert c.get(f"/api/projects/{pid}/raw", params={"path": "figures/plot.png"}).content == b"\x89PNG fake"
for bad in ["../workspace/state.json", "..\\..\\x.tex", "C:/Windows/win.ini", ".hidden.tex", "/etc/passwd"]:
    r = c.get(f"/api/projects/{pid}/file", params={"path": bad})
    assert r.status_code in (400, 404), (bad, r.status_code)
assert c.get("/api/projects/..%2F..%2Fetc").status_code == 404

# ---- chat edits two files + creates a new one in one reply ----
replies = []
def fake_chat(self, model, system, user, images=(), temperature=0.7, web=False, history=()):
    fake_chat.calls.append({"model": model, "user": user, "history": list(history)})
    self.job.add_cost(0.004)
    return replies.pop(0)
fake_chat.calls = []
llm.LLM.chat = fake_chat
llm.LLM.stream_chat = lambda self, model, system, user, temperature=0.7, history=(), on_text=None: fake_chat(self, model, system, user, history=history)

replies.append("""I tightened the abstract, fixed the intro sentence and added a limitations file.

main.tex
<<<<<<< SEARCH
We study sparse attention. It is fast. Results show a 12.3\\% gain on LongBench.
=======
We study sparse attention and report a 12.3\\% gain on LongBench \\citep{vaswani2017,made_up_2099}.
>>>>>>> REPLACE

`sections/intro.tex`
<<<<<<< SEARCH
Transformers scale quadratically with length.
=======
Transformers scale quadratically with sequence length.
>>>>>>> REPLACE

sections/limitations.tex
<<<<<<< SEARCH
=======
\\section{Limitations}
Memory use grows on very long inputs.
>>>>>>> REPLACE

main.tex
<<<<<<< SEARCH
\\section{Conclusion}
=======
\\input{sections/limitations}
\\section{Conclusion}
>>>>>>> REPLACE
""")
d = c.post(f"/api/projects/{pid}/chat", json={"message": "Tighten and add limitations", "selection": "It is fast.",
                                              "open_path": "main.tex", "model": "thinkingmachines/inkling-2-small"}).json()
main = c.get(f"/api/projects/{pid}/file", params={"path": "main.tex"}).json()["text"]
intro = c.get(f"/api/projects/{pid}/file", params={"path": "sections/intro.tex"}).json()["text"]
lim = c.get(f"/api/projects/{pid}/file", params={"path": "sections/limitations.tex"}).json()["text"]
assert "report a 12.3" in main and "\\input{sections/limitations}" in main
assert "made_up_2099" not in main and "\\citep{vaswani2017}" in main
assert "sequence length" in intro and lim.startswith("\\section{Limitations}")
last = d["messages"][-1]
assert last["version"] == 2 and last["prev_version"] == 1
assert sorted(last["diff"]["files"]) == ["main.tex", "sections/intro.tex", "sections/limitations.tex"], last["diff"]["files"]
assert "### sections/limitations.tex (new file)" in last["diff"]["text"]
assert last["text"].startswith("I tightened") and "main.tex" not in last["text"].splitlines()[-1], last["text"]
call = fake_chat.calls[-1]
assert "=== FILE: main.tex (MAIN FILE, compiled) ===" in call["user"]
assert "=== FILE: sections/intro.tex ===" in call["user"] and "=== FILE: supplementary.tex ===" in call["user"]
assert "vaswani2017: Attention Is All You Need" in call["user"] and "SELECTED THIS PASSAGE IN main.tex" in call["user"]
assert "figures/plot.png  (image" in call["user"]
assert "=== FILE: refs.bib" not in call["user"]  # bib content only when open
print("multi-file chat ok:", last["diff"]["files"])

# ---- failed SEARCH in one file triggers a retry ----
replies.append("""Updated the supplementary.
supplementary.tex
<<<<<<< SEARCH
Supplementary text that does not exist.
=======
nope
>>>>>>> REPLACE""")
replies.append("""supplementary.tex
<<<<<<< SEARCH
Supplementary text.
=======
Supplementary text with a 12.3\\% gain, matching the main paper.
>>>>>>> REPLACE""")
d = c.post(f"/api/projects/{pid}/chat", json={"message": "Sync supplementary", "open_path": "supplementary.tex"}).json()
supp = c.get(f"/api/projects/{pid}/file", params={"path": "supplementary.tex"}).json()["text"]
assert "matching the main paper" in supp and d["version"] == 3
assert "couldn't be placed" not in d["messages"][-1]["text"]
assert "supplementary.tex (text not found)" in fake_chat.calls[-1]["user"]
assert fake_chat.calls[-2]["user"].index("=== FILE: supplementary.tex (open") < fake_chat.calls[-2]["user"].index("=== FILE: main.tex")
print("retry ok")

# ---- breaking the main file is refused, other edits still apply ----
replies.append("""Done.
main.tex
<<<<<<< SEARCH
\\end{document}
=======
>>>>>>> REPLACE
sections/intro.tex
<<<<<<< SEARCH
Long documents are hard
=======
Long documents are difficult
>>>>>>> REPLACE""")
d = c.post(f"/api/projects/{pid}/chat", json={"message": "x"}).json()
assert "\\end{document}" in c.get(f"/api/projects/{pid}/file", params={"path": "main.tex"}).json()["text"]
assert "difficult" in c.get(f"/api/projects/{pid}/file", params={"path": "sections/intro.tex"}).json()["text"]
assert "skipped" in d["messages"][-1]["text"] and d["version"] == 4
print("main guard ok")

# ---- model can't write outside the project or to binary files ----
replies.append("""Trying.
../../evil.tex
<<<<<<< SEARCH
=======
bad
>>>>>>> REPLACE
figures/plot.png
<<<<<<< SEARCH
=======
bad
>>>>>>> REPLACE""")
d = c.post(f"/api/projects/{pid}/chat", json={"message": "x"}).json()
assert d["version"] == 4 and not os.path.exists(os.path.join(TMP, "evil.tex"))
assert open(os.path.join(TMP, pid, "paper", "figures", "plot.png"), "rb").read() == b"\x89PNG fake"
print("write guard ok")

# ---- question only ----
replies.append("The abstract is 14 words long.")
d = c.post(f"/api/projects/{pid}/chat", json={"message": "How long is the abstract?"}).json()
assert d["version"] == 4 and "version" not in d["messages"][-1]

# ---- model error ----
def boom(self, *a, **k):
    raise llm.LLMError("OpenRouter says the account is out of credits (402).")
llm.LLM.chat = boom
llm.LLM.stream_chat = boom
d = c.post(f"/api/projects/{pid}/chat", json={"message": "anything"}).json()
assert d["messages"][-1]["error"] and "402" in d["messages"][-1]["text"] and d["version"] == 4
llm.LLM.chat = fake_chat
llm.LLM.stream_chat = lambda self, model, system, user, temperature=0.7, history=(), on_text=None: fake_chat(self, model, system, user, history=history)

# ---- hand edits, new file, upload, delete, set main ----
assert c.post(f"/api/projects/{pid}/file", json={"path": "main.tex", "text": "no class"}).status_code == 400
d = c.post(f"/api/projects/{pid}/file", json={"path": "sections/intro.tex", "text": INTRO + "% note\n"}).json()
assert d["version"] == 5 and d["messages"][-1]["role"] == "event"
d = c.post(f"/api/projects/{pid}/new", json={"path": "sections/appendix.tex"}).json()
assert "sections/appendix.tex" in [f["path"] for f in d["files"]] and d["version"] == 6
assert c.post(f"/api/projects/{pid}/new", json={"path": "sections/appendix.tex"}).status_code == 400
assert c.post(f"/api/projects/{pid}/new", json={"path": "x.png"}).status_code == 400
r = c.post(f"/api/projects/{pid}/upload", data={"folder": "figures"},
           files=[("files", ("new plot.png", b"\x89PNG2", "image/png")), ("files", ("table.csv", b"a,b\n1,2\n", "text/csv"))])
d = r.json()
assert {"figures/new plot.png", "figures/table.csv"} <= {f["path"] for f in d["files"]} and d["version"] == 7, r.text
assert c.post(f"/api/projects/{pid}/upload", data={"folder": "../x"}, files=[("files", ("a.txt", b"a", "text/plain"))]).status_code == 400
assert c.post(f"/api/projects/{pid}/delete", json={"path": "main.tex"}).status_code == 400
d = c.post(f"/api/projects/{pid}/delete", json={"path": "sections/appendix.tex"}).json()
assert "sections/appendix.tex" not in [f["path"] for f in d["files"]] and d["version"] == 8
d = c.post(f"/api/projects/{pid}/main", json={"path": "supplementary.tex"}).json()
assert d["main"] == "supplementary.tex" and d["title"] == "Untitled paper"
assert c.post(f"/api/projects/{pid}/main", json={"path": "refs.bib"}).status_code == 400
d = c.post(f"/api/projects/{pid}/main", json={"path": "main.tex"}).json()
print("file actions ok: v", d["version"])

# ---- restore v1 brings back every text file, removes files created later ----
d = c.post(f"/api/projects/{pid}/restore", json={"version": 1}).json()
paths = {f["path"] for f in d["files"]}
assert "sections/limitations.tex" not in paths and "figures/table.csv" not in paths
assert c.get(f"/api/projects/{pid}/file", params={"path": "sections/intro.tex"}).json()["text"] == INTRO
assert c.get(f"/api/projects/{pid}/file", params={"path": "main.tex"}).json()["text"] == MAIN
assert "figures/new plot.png" in paths  # binary uploads are not versioned
d2 = c.post(f"/api/projects/{pid}/restore", json={"version": 4}).json()
assert "sections/limitations.tex" in {f["path"] for f in d2["files"]}
assert c.post(f"/api/projects/{pid}/restore", json={"version": 99}).status_code == 404
print("restore ok")

# ---- compile: no engine, then a fake engine with a main in a subfolder ----
latex.engine = lambda: None
d = c.post(f"/api/projects/{pid}/compile").json()
assert d["compile"]["pdf"] is False and "No LaTeX engine" in d["compile"]["message"]
seen = {}
def fake_compile(paper_dir, main="main.tex", timeout=600, search_dirs=()):
    seen["main"] = main
    pdf = os.path.join(paper_dir, os.path.splitext(os.path.basename(main))[0] + ".pdf")
    open(pdf, "wb").write(b"%PDF-1.4 fake")
    return {"ok": True, "pdf": pdf, "engine": "pdflatex", "errors": [], "seconds": 1.0, "message": ""}
latex.compile_project = fake_compile
d = c.post(f"/api/projects/{pid}/compile").json()
assert d["pdf"] and d["compile"]["main"] == "main.tex" and seen["main"] == "main.tex"
assert "main.pdf" not in [f["path"] for f in d["files"]]  # build output hidden
r = c.get(f"/api/projects/{pid}/pdf")
assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
c.post(f"/api/projects/{pid}/upload", data={"folder": "supp"}, files=[("files", ("appendix.tex", SUPP.encode(), "text/plain"))])
d = c.post(f"/api/projects/{pid}/main", json={"path": "supp/appendix.tex"}).json()
assert not d["pdf"]
d = c.post(f"/api/projects/{pid}/compile").json()
assert seen["main"] == "supp/appendix.tex" and d["pdf"] and "appendix.pdf" not in [f["path"] for f in d["files"]]
c.post(f"/api/projects/{pid}/main", json={"path": "main.tex"})
print("compile ok")

# ---- zip has the whole tree, no build files ----
r = c.get(f"/api/projects/{pid}/zip")
names = sorted(zipfile.ZipFile(io.BytesIO(r.content)).namelist())
assert {"main.tex", "refs.bib", "sections/intro.tex", "supplementary.tex", "figures/plot.png", "supp/appendix.tex"} <= set(names), names
assert not any(n.endswith((".aux", ".pdf")) for n in names), names
print("zip ok:", names)

# ---- older single-file editor state is migrated ----
run = os.path.join(TMP, "20261121-120000-abcdef12")
os.makedirs(os.path.join(run, "paper")); os.makedirs(os.path.join(run, "workspace", "versions")); os.makedirs(os.path.join(run, "inputs"))
open(os.path.join(run, "paper", "main.tex"), "w").write(SUPP)
open(os.path.join(run, "workspace", "versions", "v0001.tex"), "w").write(SUPP.replace("Supplementary", "Old"))
json.dump({"main": "main.tex", "messages": [], "version": 1, "cost": 0, "compile": None,
           "versions": [{"n": 1, "note": "Original", "time": 0}]}, open(os.path.join(run, "workspace", "state.json"), "w"))
open(os.path.join(run, "inputs", "idea.md"), "w").write("idea")
open(os.path.join(run, "inputs", "experimental_log.md"), "w").write("LongBench +12.3%")
d = c.get("/api/projects/20261121-120000-abcdef12").json()
assert d["version"] == 2 and d["has_inputs"]
open(os.path.join(run, "paper", "extra.tex"), "w").write("% extra\n")
d = c.post("/api/projects/20261121-120000-abcdef12/restore", json={"version": 1}).json()  # partial: no deletions
assert "Old text" in open(os.path.join(run, "paper", "main.tex")).read() and os.path.exists(os.path.join(run, "paper", "extra.tex"))
replies.append("Fine as is.")
c.post("/api/projects/20261121-120000-abcdef12/chat", json={"message": "check numbers"})
assert "LongBench +12.3%" in fake_chat.calls[-1]["user"]
print("migration ok")

# ---- latex error parser ----
errs = latex._errors("! Undefined control sequence.\nl.42 \\foo\n", "")
assert errs[0] == {"file": None, "line": 42, "message": "Undefined control sequence. (near: \\foo)"}

shutil.rmtree(TMP, ignore_errors=True)
print("\nALL EDITOR TESTS PASSED")
