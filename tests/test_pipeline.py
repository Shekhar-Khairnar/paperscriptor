"""Full paper-writing pipeline through the web API, with a mocked OpenRouter, Semantic Scholar and OpenAlex
(no network, no credits, no LaTeX). Run: python tests/test_pipeline.py"""
import io, json, os, sys, tempfile, time, types, zipfile, struct, zlib
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from fastapi.testclient import TestClient
from paperscriptor import config, latex, llm, prompts, references, server

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
        assert json.get("plugins") == [{"id": "web", "max_results": 5}]
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
        return ok('```json\n{"actions_taken":["a"]}\n```\n```latex\n' + TEX % r"Refined \cite{He2016DeepResidual} \cite{Nope1999}" + "\n```")
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
assert "figures/fig_one.png" in names and "guidelines.md" not in names and "template.tex" not in names
assert c.get(f"/api/jobs/{jid}/files/..%2F..%2Fapp.py").status_code == 404
reviews = json.load(open(os.path.join(s["run_dir"], "reviews.json")))
assert [(r["version"], r["review"]["Overall"]) for r in reviews][:2] == [("draft", 5), ("v1", 6)], reviews
assert "He2016DeepResidual" in bib and "Made Up" not in bib
print("ALL PIPELINE TESTS PASSED")
