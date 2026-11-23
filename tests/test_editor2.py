"""New editor features: verified citations, attachments, live status, rename, SyncTeX, error mapping, streaming client."""
import gzip, io, os, sys, shutil, tempfile, threading, time, zipfile, json
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["OPENROUTER_API_KEY"] = "sk-or-test"
from paperscriptor import config, latex, llm, pipeline, references, server, synctex, workspace
from fastapi.testclient import TestClient

TMP = tempfile.mkdtemp()
config.RUNS_DIR = TMP
c = TestClient(server.app)

MAIN = r"""\documentclass{article}
\begin{document}
\input{sections/intro}
Sparse attention works.
\bibliographystyle{plain}
\bibliography{refs}
\end{document}
"""
INTRO = "\\section{Introduction}\nTransformers are popular.\nLong inputs are costly.\n"
BIB = "@article{vaswani2017,\n  title={Attention Is All You Need},\n  year={2017}\n}\n"
buf = io.BytesIO()
with zipfile.ZipFile(buf, "w") as z:
    z.writestr("main.tex", MAIN); z.writestr("sections/intro.tex", INTRO); z.writestr("refs.bib", BIB)
pid = c.post("/api/projects/import", files=[("files", ("p.zip", buf.getvalue(), "application/zip"))]).json()["id"]
run = os.path.join(TMP, pid)
read = lambda p: c.get(f"/api/projects/{pid}/file", params={"path": p}).json()["text"]

# ---- mocks: model replies, streamed in small pieces ----
replies, calls = [], []
def fake_stream(self, model, system, user, temperature=0.7, history=(), on_text=None):
    calls.append({"user": user, "history": list(history), "reasoning": self.reasoning})
    self.job.add_cost(0.002)
    self.job.add_usage({"prompt_tokens": 100, "completion_tokens": 20, "completion_tokens_details": {"reasoning_tokens": 5}})
    text = replies.pop(0)
    if on_text:
        for i in range(0, len(text), 40):
            on_text(text[:i + 40]); time.sleep(0.01)
    return text
llm.LLM.stream_chat = fake_stream
llm.LLM.chat = lambda self, *a, **k: (_ for _ in ()).throw(AssertionError("chat() should not be called"))

# Paper databases: Longformer and Reformer exist, the made-up one doesn't; Attention is already in refs.bib
DB = {
    "longformer: the long-document transformer": {"title": "Longformer: The Long-Document Transformer", "year": 2020,
        "authors": [{"name": "Iz Beltagy"}, {"name": "Matthew E. Peters"}, {"name": "Arman Cohan"}], "venue": "arXiv",
        "abstract": "Transformer-based models are unable to process long sequences...", "journal": None},
    "reformer: the efficient transformer": {"title": "Reformer: The Efficient Transformer", "year": 2020,
        "authors": [{"name": "Nikita Kitaev"}], "venue": "ICLR", "abstract": "Large Transformer models...", "journal": None},
    "attention is all you need": {"title": "Attention Is All You Need", "year": 2017, "authors": [{"name": "Ashish Vaswani"}],
        "venue": "NeurIPS", "abstract": "The dominant sequence transduction models...", "journal": None},
}
looked_up = []
def fake_verify(title, year, cfg, state, job):
    looked_up.append(title)
    p = DB.get(title.lower())
    return (p, "Semantic Scholar") if p else (None, None)
references.verify_paper = fake_verify

# ---- 1. verified citations: FIND PAPERS -> check -> bib -> edit with new keys ----
replies.append("""I'll look these up first.

<<<<<<< FIND PAPERS
1. Longformer: The Long-Document Transformer (2020)
- Reformer: The Efficient Transformer, 2020
Attention Is All You Need (2017)
A Totally Made Up Paper About Sparse Things (2023)
>>>>>>> END""")
replies.append("""I cited Longformer and Reformer for the cost of long inputs.

sections/intro.tex
<<<<<<< SEARCH
Long inputs are costly.
=======
Long inputs are costly \\cite{Beltagy2020LongformerLongdocument,Kitaev2020ReformerEfficient,vaswani2017,FakeKey2023}.
>>>>>>> REPLACE""")
d = c.post(f"/api/projects/{pid}/chat", json={"message": "Add citations for long inputs being costly", "open_path": "sections/intro.tex"}).json()
assert looked_up == ["Longformer: The Long-Document Transformer", "Reformer: The Efficient Transformer",
                     "Attention Is All You Need", "A Totally Made Up Paper About Sparse Things"], looked_up
bib = read("refs.bib")
assert bib.startswith(BIB) and "@" in bib[len(BIB):]
assert "Beltagy2020LongformerLongdocument" in bib and "Kitaev2020ReformerEfficient" in bib, bib
assert bib.count("Attention Is All You Need") == 1  # not added twice
intro = read("sections/intro.tex")
assert "\\cite{Beltagy2020LongformerLongdocument,Kitaev2020ReformerEfficient,vaswani2017}" in intro, intro  # FakeKey removed
follow = calls[-1]["user"]
assert "Beltagy2020LongformerLongdocument: Longformer" in follow and "vaswani2017: Attention Is All You Need (already" in follow
assert "Not found, do not cite: A Totally Made Up Paper" in follow
assert calls[-1]["history"][-1]["role"] == "assistant" and "FIND PAPERS" in calls[-1]["history"][-1]["content"]
last = d["messages"][-1]
assert sorted(last["diff"]["files"]) == ["refs.bib", "sections/intro.tex"], last["diff"]["files"]
assert last["text"].startswith("I cited Longformer") and "look these up" not in last["text"]
assert "Added 2 verified reference(s) to refs.bib" in last["text"] and "Couldn't find 1 suggested paper" in last["text"]
assert "FIND PAPERS" not in last["text"] and "<<<<<<<" not in last["text"]
print("verified citations ok:", last["diff"]["files"])

# ---- 2. FIND with no bib file at all creates references.bib and asks for \bibliography ----
buf2 = io.BytesIO()
with zipfile.ZipFile(buf2, "w") as z:
    z.writestr("paper.tex", "\\documentclass{article}\n\\begin{document}\nText.\n\\end{document}\n")
pid2 = c.post("/api/projects/import", files=[("files", ("q.zip", buf2.getvalue(), "application/zip"))]).json()["id"]
replies.append("<<<<<<< FIND PAPERS\nReformer: The Efficient Transformer (2020)\n>>>>>>> END")
replies.append("""Added the citation and bibliography.
paper.tex
<<<<<<< SEARCH
Text.
\\end{document}
=======
Text \\cite{Kitaev2020ReformerEfficient}.
\\bibliographystyle{plain}
\\bibliography{references}
\\end{document}
>>>>>>> REPLACE""")
d2 = c.post(f"/api/projects/{pid2}/chat", json={"message": "cite reformer"}).json()
files2 = [f["path"] for f in d2["files"]]
assert "references.bib" in files2, files2
assert "no bibliography command yet" in calls[-1]["user"] and "\\bibliography{references}" in calls[-1]["user"]
assert "\\cite{Kitaev2020ReformerEfficient}" in c.get(f"/api/projects/{pid2}/file", params={"path": "paper.tex"}).json()["text"]
print("new bib file ok")

# ---- 3. attachments go into the prompt and onto the message ----
replies.append("Your results file says 12.3%, which matches the paper.")
d = c.post(f"/api/projects/{pid}/chat", json={"message": "Check the numbers against my results", "reasoning": "low",
           "attachments": [{"name": "results.csv", "text": "model,score\nours,12.3\n"}, {"name": "empty.txt", "text": "  "}]}).json()
assert "=== ATTACHED: results.csv ===\nmodel,score\nours,12.3" in calls[-1]["user"]
assert "empty.txt" not in calls[-1]["user"]
user = d["messages"][-2]
assert user["attachments"] == ["results.csv"], user
assert calls[-1]["reasoning"] == "low" and calls[-2]["reasoning"] == ""          # effort reaches the model client
assert d["messages"][-1]["tokens"] == {"input": 100, "output": 20, "reasoning": 5}, d["messages"][-1]
assert d["tokens"]["input"] >= 200 and d["tokens"]["reasoning"] >= 10, d["tokens"]  # project totals add up
print("attachments ok")

# ---- 4. live status while the model is writing ----
gate = threading.Event()
def slow_stream(self, model, system, user, temperature=0.7, history=(), on_text=None):
    on_text("I'm tightening the intro.\n\nsections/intro.tex\n<<<<<<< SEARCH\nTransformers")
    gate.wait(5)
    return "I'm tightening the intro.\n\nsections/intro.tex\n<<<<<<< SEARCH\nTransformers are popular.\n=======\nTransformers are widely used.\n>>>>>>> REPLACE"
llm.LLM.stream_chat = slow_stream
result = {}
t = threading.Thread(target=lambda: result.update(r=c.post(f"/api/projects/{pid}/chat", json={"message": "tighten"})))
t.start()
for _ in range(50):
    s = c.get(f"/api/projects/{pid}/chat/status").json()
    if s.get("busy") and s.get("reply"):
        break
    time.sleep(0.05)
assert s["busy"] and s["reply"] == "I'm tightening the intro." and s["stage"] == "Editing sections/intro.tex…", s
assert c.post(f"/api/projects/{pid}/chat", json={"message": "again"}).status_code == 409
gate.set(); t.join(5)
assert result["r"].status_code == 200 and "widely used" in read("sections/intro.tex")
assert c.get(f"/api/projects/{pid}/chat/status").json() == {"busy": False}
print("live status ok:", s["stage"])

# ---- 5. stop during streaming ----
def cancellable(self, model, system, user, temperature=0.7, history=(), on_text=None):
    for _ in range(100):
        if self.job.cancel_requested:
            raise llm.Cancelled()
        time.sleep(0.02)
    return "never"
llm.LLM.stream_chat = cancellable
t = threading.Thread(target=lambda: result.update(r=c.post(f"/api/projects/{pid}/chat", json={"message": "slow"})))
t.start(); time.sleep(0.2)
c.post(f"/api/projects/{pid}/stop"); t.join(5)
assert result["r"].json()["messages"][-1]["text"].startswith("Stopped")
llm.LLM.stream_chat = fake_stream
print("stop ok")

# ---- 6. rename (including the main file) ----
d = c.post(f"/api/projects/{pid}/rename", json={"path": "sections/intro.tex", "new_path": "sections/introduction.tex"}).json()
assert "sections/introduction.tex" in [f["path"] for f in d["files"]] and "sections/intro.tex" not in [f["path"] for f in d["files"]]
assert c.post(f"/api/projects/{pid}/rename", json={"path": "refs.bib", "new_path": "main.tex"}).status_code == 400
assert c.post(f"/api/projects/{pid}/rename", json={"path": "refs.bib", "new_path": "../x.bib"}).status_code == 400
d = c.post(f"/api/projects/{pid}/rename", json={"path": "main.tex", "new_path": "paper.tex"}).json()
assert d["main"] == "paper.tex"
c.post(f"/api/projects/{pid}/rename", json={"path": "paper.tex", "new_path": "main.tex"})
c.post(f"/api/projects/{pid}/rename", json={"path": "sections/introduction.tex", "new_path": "sections/intro.tex"})
print("rename ok")

# ---- 7. SyncTeX forward and inverse on a hand-made synctex file ----
paper = os.path.join(run, "paper")
abs_intro = os.path.join(paper, "sections", "intro.tex").replace("\\", "/")
sync = f"""SyncTeX Version:1
Input:1:./main.tex
Input:2:{abs_intro}
Output:pdf
Magnification:1000
Unit:1
X Offset:0
Y Offset:0
Content:
!100
{{1
[1,3:4736286,4736286:30000000,40000000,0
h2,2:4736286,9472573:20000000,655360,0
x2,2:4736286,9472573
h2,3:4736286,11000000:20000000,655360,0
h1,4:4736286,30000000:10000000,655360,0
]
}}1
Postamble:
"""
with gzip.open(os.path.join(paper, "main.synctex.gz"), "wt") as f:
    f.write(sync)
fw = c.get(f"/api/projects/{pid}/synctex/forward", params={"path": "sections/intro.tex", "line": 3}).json()
assert fw["page"] == 1 and abs(fw["x"] - 72.0) < 0.1 and abs(fw["y"] - (11000000 - 655360) / 65781.76) < 0.5, fw
fw2 = c.get(f"/api/projects/{pid}/synctex/forward", params={"path": "main.tex", "line": 4}).json()
assert abs(fw2["y"] - (30000000 - 655360) / 65781.76) < 0.5, fw2
inv = c.get(f"/api/projects/{pid}/synctex/inverse", params={"page": 1, "x": 100, "y": 9472573 / 65781.76 - 3}).json()
assert inv == {"path": "sections/intro.tex", "line": 2}, inv
inv2 = c.get(f"/api/projects/{pid}/synctex/inverse", params={"page": 1, "x": 90, "y": 30000000 / 65781.76}).json()
assert inv2 == {"path": "main.tex", "line": 4}, inv2
assert c.get(f"/api/projects/{pid}/synctex/inverse", params={"page": 9, "x": 1, "y": 1}).status_code == 404
assert c.get(f"/api/projects/{pid}/synctex/forward", params={"path": "refs.bib", "line": 1}).status_code == 404
print("synctex ok:", fw, inv)

# ---- 8. compile errors are mapped to project files ----
def fake_compile(paper_dir, main="main.tex", timeout=600, search_dirs=()):
    return {"ok": False, "pdf": None, "engine": "pdflatex", "seconds": 1, "message": "",
            "errors": [{"file": os.path.join(paper_dir, "sections", "intro.tex"), "line": 2, "message": "Undefined control sequence."},
                       {"file": "C:/texlive/2025/texmf-dist/tex/latex/foo.sty", "line": 9, "message": "Package error."},
                       {"file": None, "line": 7, "message": "Missing $ inserted."},
                       {"file": "sections/intro.tex", "line": 3, "message": "Extra }."}]}
latex.engine = lambda: "pdflatex"
latex.compile_project = fake_compile
d = c.post(f"/api/projects/{pid}/compile").json()
errs = d["compile"]["errors"]
assert errs[0]["file"] == "sections/intro.tex" and errs[0]["line"] == 2, errs[0]
assert errs[1]["file"] is None and errs[1]["message"].startswith("foo.sty: "), errs[1]
assert errs[2]["file"] == "main.tex" and errs[3]["file"] == "sections/intro.tex"
print("error mapping ok")

# ---- 9. reply text while a block is half-written, and FIND parsing ----
assert workspace._reply_text("Working on it.\n\nmain.tex\n<<<<<<< SEARCH\nfoo") == "Working on it."
assert workspace._reply_text("Looking up.\n<<<<<<< FIND PAPERS\nA paper") == "Looking up."
assert workspace._parse_find("<<<<<<< FIND PAPERS\n* \"Deep Residual Learning for Image Recognition\" (2016)\nshort\n>>>>>>> END") == \
    [("Deep Residual Learning for Image Recognition", 2016)]
print("parsers ok")

# ---- 10. streaming client parses OpenRouter SSE, adds cost, falls back on HTTP errors ----
import requests as rq
class FakeResp:
    def __init__(self, status, lines): self.status_code, self._lines, self.encoding = status, lines, None
    def iter_lines(self, decode_unicode=True): yield from self._lines
    def close(self): pass
sse = [": OPENROUTER PROCESSING", "", 'data: {"choices":[{"delta":{"content":"Hel"}}]}',
       'data: {"choices":[{"delta":{"content":"lo"},"finish_reason":"stop"}]}',
       'data: {"choices":[],"usage":{"cost":0.0012}}', "data: [DONE]"]
orig_post = rq.post
import importlib; importlib.reload(llm)  # undo the mocks above for the client test
rq.post = lambda *a, **k: FakeResp(200, sse)
job = llm.Job("x", run)
seen = []
out = llm.LLM("k", job).stream_chat("m", "sys", "hi", on_text=seen.append)
assert out == "Hello" and seen == ["Hel", "Hello"] and abs(job.cost - 0.0012) < 1e-9, (out, seen, job.cost)
fell = []
llm.LLM.chat = lambda self, *a, **k: fell.append(1) or "fallback"
rq.post = lambda *a, **k: FakeResp(401, [])
assert llm.LLM("k", job).stream_chat("m", "sys", "hi") == "fallback" and fell
rq.post = lambda *a, **k: FakeResp(200, ['data: {"error":{"message":"Provider overloaded"}}'])
try:
    llm.LLM("k", job).stream_chat("m", "sys", "hi"); raise SystemExit("expected error")
except llm.LLMError as e:
    assert "Provider overloaded" in str(e)
rq.post = orig_post
print("streaming client ok")

shutil.rmtree(TMP, ignore_errors=True)
print("\nALL NEW-FEATURE TESTS PASSED")
