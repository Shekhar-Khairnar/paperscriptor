"""Full paper-writing pipeline through the web API, with a mocked OpenRouter, Semantic Scholar and OpenAlex
(no network, no credits, no LaTeX). Run: python tests/test_pipeline.py"""
import io, json, os, sys, tempfile, time, types, zipfile, struct, zlib
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from fastapi.testclient import TestClient
from paperscriptor import config, latex, llm, pipeline, prompts, references, server

config.RUNS_DIR = tempfile.mkdtemp()
latex.engine = lambda: None                                            # no PDF build in this test
references.time = types.SimpleNamespace(sleep=lambda s: None, time=time.time)  # skip S2 rate-limit pauses

TEX = r"""\documentclass{article}
\usepackage{iclr2025_conference,times}
\begin{document}
\title{T}\maketitle
\section{Introduction} %s
\bibliography{references}
\end{document}"""

calls = {"section": 0, "review": 0, "image_rejected": False}

class Resp:
    def __init__(self, code, data): self.status_code, self._d, self.text = code, data, json.dumps(data)
    def json(self): return self._d

def ok(text, cost=0.01):
    return Resp(200, {"choices": [{"message": {"content": text}, "finish_reason": "stop"}], "usage": {"cost": cost}})

def fake_post(url, headers=None, json=None, timeout=None):
    import json as J
    assert headers["Authorization"] == "Bearer sk-test"
    msgs = json["messages"]
    system = msgs[0]["content"] if msgs[0]["role"] == "system" else ""
    user = msgs[-1]["content"]
    if system.startswith(prompts.outline_agent_system_prompt[:60]):
        return ok("```json\n" + J.dumps({
            "plotting_plan": [],
            "intro_related_work_plan": {
                "introduction_strategy": {"hook_hypothesis": "h", "problem_gap_hypothesis": "g", "search_directions": ["vision backbones"]},
                "related_work_strategy": {"subsections": [{"subsection_title": "2.1 Attention", "limitation_search_queries": ["attention limits"]}]}},
            "section_plan": [{"section_title": "Experiments", "subsections": [{"subsection_title": "Setup", "content_bullets": ["x"], "citation_hints": ["He et al. (Deep Residual Learning)"]}]}]}) + "\n```")
    if not system and "CANDIDATE" not in str(user) and "candidates" in str(user):
        assert json.get("plugins") == [{"id": "web", "max_results": 3}]
        return ok('```json\n{"section_name":"s","candidates":[{"title":"Deep Residual Learning for Image Recognition","year":2016,"reason":"r"},{"title":"Attention Is All You Need","year":2017,"reason":"r"},{"title":"A Totally Made Up Paper","year":2020,"reason":"r"}]}\n```')
    if system.startswith("Role: Senior AI Researcher.\nTask: Write the introduction"):
        assert "He2016DeepResidual" in user and "Made Up" not in user
        return ok("```latex\n" + TEX % r"Intro \cite{He2016DeepResidual,Fake2020Bogus} and \citep{Vaswani2017AttentionAll}." + "\n```")
    if system.startswith("Role: Senior AI Researcher.\nTask: Complete a research paper"):
        calls["section"] += 1
        if isinstance(user, list):  # with images -> reject once to test fallback
            calls["image_rejected"] = True
            return Resp(404, {"error": {"message": "No endpoints found that support image input"}})
        assert "fig_one.png" in user
        return ok("```latex\n" + TEX % r"Full \cite{He2016DeepResidual} \includegraphics{figures/fig_one.png}" + "\n```")
    if system.startswith("You are a reviewer"):
        calls["review"] += 1
        score = 5 if calls["review"] == 1 else 6
        return ok("THOUGHT: fine\nREVIEW JSON:\n```json\n" + J.dumps({"Overall": score, "Soundness": 3, "Weaknesses": ["w"], "Questions": ["q"]}) + "\n```")
    if system.startswith("Role: Senior AI Researcher.\nTask: Revise"):
        assert "IDEA AND METHOD NOTES" in user and "My idea" in user     # the reviser sees the method notes too
        return ok('```json\n{"actions_taken":["a"]}\n```\n```latex\n' + TEX % r"Refined \cite{He2016DeepResidual} \cite{Nope1999} \includegraphics{figures/fig_one.png}" + "\n```")
    if system.startswith(pipeline.EDITOR_SYSTEM[:40]):
        calls["editor"] = calls.get("editor", 0) + 1
        assert "acc 91.2" in user and "He2016DeepResidual: Deep Residual Learning" in user, user[-3000:]
        return ok('```json\n{"issues": []}\n```')
    if system.startswith(pipeline.CLAIM_CHECK_SYSTEM[:40]):
        calls["check"] = calls.get("check", 0) + 1
        assert "acc 91.2" in user and "Accuracy plot" in user        # checks against the author's materials
        return ok("```json\n" + J.dumps({"issues": [
            {"text": r"Refined \cite{He2016DeepResidual}", "type": "overreach", "why": "too broad", "fix": "Checked"},
            {"text": "text that is not in the paper at all", "type": "number", "why": "x", "fix": "y"}],
            "author_todo": ["Add the optimizer settings"]}) + "\n```")
    raise AssertionError("unexpected call: " + system[:80] + str(user)[:200])

S2 = {
    "Deep Residual Learning for Image Recognition": {"title": "Deep Residual Learning for Image Recognition", "authors": [{"name": "Kaiming He"}], "venue": "CVPR", "year": 2016, "abstract": "Residual nets & stuff_50%.", "publicationDate": "2016-06-27", "journal": None},
    "Attention Is All You Need": {"title": "Attention is All you Need", "authors": [{"name": "Ashish Vaswani"}], "venue": "NeurIPS", "year": 2017, "abstract": "Transformers.", "publicationDate": "2017-06-12", "journal": {"name": "ArXiv", "volume": "abs/1706.03762"}},
}
def fake_get(url, headers=None, params=None, timeout=None):
    if "openalex" in url:
        return Resp(200, {"results": []})
    hit = S2.get(params["query"])
    return Resp(200, {"data": [hit] if hit else []})

llm.requests.post = fake_post
references.requests.get = fake_get

def png():
    raw = b"\x00\xff\x00\x00"
    def chunk(t, d): return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xffffffff)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)) + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")

c = TestClient(server.app)
assert c.get("/").status_code == 200
cfg = c.get("/api/config").json()
assert [t["id"] for t in cfg["templates"]] == ["iclr2025", "cvpr2025", "nature"]
# validation
r = c.post("/api/jobs", data={"api_key": "sk-test", "idea": "", "experimental_log": "x"})
assert r.status_code in (400, 422)
r = c.post("/api/jobs", data={"api_key": "sk-test", "template": "iclr2025",
                             "cutoff": "2024-11", "use_literature": "true", "use_web": "true", "refine_rounds": "2",
                             "captions": json.dumps(["Accuracy plot"])},
           files=[("materials", ("m.json", json.dumps({"idea": "My idea", "experimental_log": "acc 91.2", "documents": []}), "application/json")), ("figures", ("fig one.png", png(), "image/png"))])
assert r.status_code == 200, r.text
jid = r.json()["id"]
for _ in range(200):
    s = c.get(f"/api/jobs/{jid}").json()
    if s["status"] != "running": break
    time.sleep(0.1)
assert s["status"] == "done", "\n".join(s["logs"])
assert calls["image_rejected"]
z = zipfile.ZipFile(io.BytesIO(c.get(f"/api/jobs/{jid}/files/paper.zip").content))
names = sorted(z.namelist())
main = z.read("main.tex").decode(); bib = z.read("references.bib").decode()
assert "Nope1999" not in main and "He2016DeepResidual" in main
assert calls.get("check") == 1 and calls.get("editor") == 1, calls
assert r"Checked~\cite{He2016DeepResidual}" in main, main                # fix applied, citation kept
assert "Vaswani2017AttentionAll" not in bib and bib.count("@") == 1, bib  # uncited paper trimmed
assert "TODO.md" in names
todo = z.read("TODO.md").decode()
assert "Add the optimizer settings" in todo and "too broad" in todo, todo
assert "figures/fig_one.png" in names and "guidelines.md" not in names and "template.tex" not in names
assert c.get(f"/api/jobs/{jid}/files/..%2F..%2Fapp.py").status_code == 404
reviews = json.load(open(os.path.join(s["run_dir"], "reviews.json")))
assert [(r["version"], r["review"]["Overall"]) for r in reviews][:2] == [("draft", 5), ("v1", 6)], reviews
assert "He2016DeepResidual" in bib and "Made Up" not in bib

# Fact check of a long paper: checked in parts (one may fail), placeholders are never filled in
filler = ("We report the measured results on the benchmark as logged by the authors. " * 100) + "\n"
LONG = (r"\documentclass{sn-jnl}" "\n" r"\abstract{All results are on simulated data.}" "\n" r"\begin{document}" "\n"
        r"\section{Introduction}" "\nOur method always works on every dataset.\n" + filler +
        r"\section{Results}" "\n" + filler + r"\section{Declarations}" "\nData availability statement here.\n" + filler +
        r"\end{document}" "\n")
assert len(pipeline._check_parts(LONG)) == 3 and "".join(pipeline._check_parts(LONG)) == LONG
assert pipeline._abstract(LONG) == "All results are on simulated data."

class FakeChecker:
    def __init__(self): self.users = []
    def chat(self, model, system, user, temperature=0.7, **kw):
        self.users.append(user)
        if "PART 2 OF 3" in user:
            raise llm.LLMError("cut off")
        issues = [{"text": "Our method always works on every dataset.", "type": "absolute", "why": "x",
                   "fix": "Our method works on the tested benchmark."},
                  {"text": "Data availability statement here.", "type": "missing", "why": "x", "fix": "No data are available."}]
        return "```json\n" + json.dumps({"issues": issues, "author_todo": ["Add data availability"]}) + "\n```"

job = llm.Job("fc", tempfile.mkdtemp())
checker = FakeChecker()
out, rep = pipeline.check_claims(checker, job, "m", LONG, "idea", "log", [])
assert len(checker.users) == 3 and all("PART" in u for u in checker.users)
assert sum("All results are on simulated data." in u for u in checker.users) == 3  # part 1 has it; parts 2-3 get it as context
assert "works on the tested benchmark" in out and "always works" not in out
assert "Data availability statement here." in out and "No data are available" not in out   # placeholder kept
assert rep["applied"] == 1 and rep["author_todo"] == ["Add data availability"] * 2, rep
assert any("part 2/3 skipped" in line for line in job.logs), job.logs

# A closing brace merged into the comment after it is restored; deliberate {% lines and balanced files are untouched
broken = ("\\documentclass{sn-jnl}\n\\newcommand{\\note}[1]{%\n  \\textbf{#1}}\n%%% SUMMARY %%%\n"
          "\\abstract{We report 88.1\\% F1 \\{sic\\}.%%% SUMMARY %%%\n\\begin{document}\nx\n\\end{document}")
fixed = pipeline.close_commented_braces(broken)
assert "\\abstract{We report 88.1\\% F1 \\{sic\\}.}%%% SUMMARY %%%" in fixed, fixed
assert "\\newcommand{\\note}[1]{%\n" in fixed and pipeline._abstract(fixed) == "We report 88.1\\% F1 \\{sic\\}."
assert pipeline.close_commented_braces(fixed) == fixed
assert pipeline.extract_latex("```latex\n" + broken + "\n```").count("sic\\}.}%") == 1     # applied to model output

# Sentences about the authors' materials instead of the study are rewritten ("meta")
new, n = pipeline._apply_fixes("The supplied evaluation reports 91.6% accuracy.",
                               [{"text": "The supplied evaluation reports 91.6% accuracy.", "type": "meta", "fix": "MarginRoute reaches 91.6% accuracy."}])
assert n == 1 and new == "MarginRoute reaches 91.6% accuracy.", new

# Editor pass helpers: unreferenced figures/tables, bibliography titles, removing a citation that does not fit
T = (r"\begin{figure}\includegraphics{a}\label{fig:main}\end{figure}\begin{figure}\label{fig:blur}\end{figure}"
     r"\begin{table}\label{tab:res}\end{table}\label{sec:x} See \Cref{fig:blur} and Table~\ref{tab:res,sec:x}.")
assert pipeline.unreferenced_labels(T) == ["fig:main"], pipeline.unreferenced_labels(T)
assert pipeline.bib_titles("@article{A1,\n  title = {{Deep} Residual\n  Learning},\n  year={2016}\n}\n@misc{B2,\n year={1}\n}\n") == \
    "- A1: Deep Residual Learning\n- B2: ?"
SENT = r"Drying pulses release carbon~\cite{Birch1958,Davidson2006}."
new, n = pipeline._apply_fixes(SENT, [{"text": SENT, "type": "citation", "fix": r"Drying pulses release carbon~\cite{Birch1958}."}])
assert n == 1 and new == r"Drying pulses release carbon~\cite{Birch1958}.", new
lib = [{"title": f"P{i}", "targeted": i == 7, "citation_count": i} for i in range(50)]   # citation library cap
kept = pipeline.trim_library(lib)
assert len(kept) == 40 and kept[0]["title"] == "P7" and lib[49] in kept and lib[0] not in kept and lib[8] not in kept
assert [p["title"] for p in kept] == sorted((p["title"] for p in kept), key=lambda t: int(t[1:]))   # original order kept
assert pipeline.trim_library(lib[:5]) == lib[:5]
IMG = r"\includegraphics[width=\linewidth]{figures/fig1_accuracy_by_domain.png}"   # the check once deleted these as "missing"
new, n = pipeline._apply_fixes("x " + IMG + " y", [{"text": IMG, "type": "missing", "fix": ""},
                                                  {"text": r"As Table~\ref{tab:main} shows, on synthetic data", "type": "repetition", "fix": ""}])
assert n == 0 and IMG in new, new
class FakeEditor:
    def chat(self, model, system, user, temperature=0.7, **kw):
        assert "fig:main" in user and "never referenced" in user
        return json.dumps({"issues": [{"text": "Results are good.", "type": "figure_ref",
                                       "fix": r"Results are good (Fig.~\ref{fig:main})."}]})
ed_job = llm.Job("ed", tempfile.mkdtemp())
out, issues = pipeline.edit_paper(FakeEditor(), ed_job, "m", r"\documentclass{article}\begin{document}" + T + r" Results are good.\end{document}", "log", [], "")
assert r"Results are good (Fig.~\ref{fig:main})." in out and pipeline.unreferenced_labels(out) == [], out
assert any("Editor pass: 1 edit(s) (1 figure_ref)" in l for l in ed_job.logs), ed_job.logs

# A revision that deletes figures, tables or most of the text is rejected even if the reviewer likes it
DOC = "\\documentclass{article}\n\\begin{document}\n%s\n\\end{document}"
full = DOC % ("Results text. " * 40 + "\\begin{table}x\\end{table}\\includegraphics{figures/a.png}")
assert pipeline.lost_content(full, full) == ""
assert pipeline.lost_content(full, DOC % ("Results text. " * 40 + "\\begin{table}x\\end{table}")) == "revision dropped figures"
assert pipeline.lost_content(full, DOC % ("Results text. " * 40 + "\\includegraphics{figures/a.png}")) == "revision dropped tables"
assert "cut the text" in pipeline.lost_content(full, DOC % ("Short. \\begin{table}x\\end{table}\\includegraphics{figures/a.png}"))

# A model that spends its whole output allowance thinking is retried once, not five times
cut = {"n": 0}
def cut_post(url, headers=None, json=None, timeout=None):
    cut["n"] += 1
    return Resp(200, {"choices": [{"message": {"content": ""}, "finish_reason": "length"}], "usage": {"cost": 0.02}})
llm.requests.post = cut_post
try:
    llm.LLM("sk-test", job).chat("m", "s", "u")
    raise AssertionError("expected LLMError")
except llm.LLMError as e:
    assert "thinking" in str(e) and cut["n"] == 2, (e, cut)
print("ALL PIPELINE TESTS PASSED")
