"""Local models through Ollama, with a fake Ollama server (no network). Run: python tests/test_ollama.py"""
import io, json, os, sys, tempfile, zipfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.pop("OPENROUTER_API_KEY", None)
os.environ["OPENROUTER_API_KEY"] = ""          # prove that local models need no OpenRouter key
from fastapi.testclient import TestClient
from paperscriptor import config, latex, llm, pipeline, server, workspace

config.RUNS_DIR = tempfile.mkdtemp()
config.OLLAMA_URL = "http://ollama.test:11434"
latex.engine = lambda: None
c = TestClient(server.app)
calls = []


class Resp:
    def __init__(self, status, data=None, lines=None):
        self.status_code, self._data, self._lines, self.encoding = status, data, lines or [], None
        self.text = json.dumps(data) if data is not None else ""
    def json(self): return self._data
    def iter_lines(self, decode_unicode=True): yield from self._lines
    def close(self): pass


def fake_post(url, json=None, stream=False, timeout=None, headers=None):
    calls.append({"url": url, "body": json, "stream": stream, "headers": headers})
    if json["model"] == "missing:7b":
        return Resp(404, {"error": 'model "missing:7b" not found, try pulling it first'})
    reply = SCRIPT.pop(0) if SCRIPT else "<think>let me think</think>Plain answer."
    if stream:
        parts = [reply[i:i + 7] for i in range(0, len(reply), 7)]
        lines = [json_dumps({"message": {"content": p}, "done": False}) for p in parts]
        return Resp(200, lines=lines + [json_dumps({"message": {"content": ""}, "done": True, "done_reason": "stop"})])
    return Resp(200, {"message": {"role": "assistant", "content": reply}, "done": True, "done_reason": "stop"})


json_dumps = json.dumps
SCRIPT = []
llm.requests.post = fake_post

# ---- 1. client: non-streaming call, think stripped, no auth header, context window raised ----
job = llm.Job("t", tempfile.mkdtemp())
cl = llm.LLM("", job)
SCRIPT.append("<think>private reasoning</think>\nHello from qwen.")
assert cl.chat("ollama/qwen3:14b", "be brief", "hi " * 40000) == "Hello from qwen."
call = calls[-1]
assert call["url"] == "http://ollama.test:11434/api/chat" and call["headers"] is None
assert call["body"]["model"] == "qwen3:14b" and call["body"]["stream"] is False
assert call["body"]["options"]["num_ctx"] == config.OLLAMA_NUM_CTX  # big prompt -> capped at the max
assert any("more than the" in l for l in job.logs), job.logs           # and the user is told
assert job.cost == 0.0
SCRIPT.append("short")
cl.chat("ollama/qwen3:14b", "", "tiny prompt")
n = calls[-1]["body"]["options"]["num_ctx"]; assert n <= 12288 and n % 4096 == 0, n  # small prompt -> small window

# ---- 2. streaming: live text never shows the <think> part ----
seen = []
SCRIPT.append("<think>step 1, step 2</think>The final reply.")
out = cl.stream_chat("ollama/llama3.1:8b", "sys", "hi", on_text=seen.append)
assert out == "The final reply." and calls[-1]["stream"] is True
assert seen and all("think" not in s and "step 1" not in s for s in seen), seen
assert seen[-1] == "The final reply."

# ---- 3. images go to vision models as base64; web search is skipped with a note ----
png = os.path.join(tempfile.mkdtemp(), "f.png"); open(png, "wb").write(b"\x89PNG fake")
SCRIPT.append("Saw the figure.")
cl.chat("ollama/llava:7b", "sys", "describe", images=[png], web=True)
msg = calls[-1]["body"]["messages"][-1]
assert msg["images"] and msg["content"] == "describe" and "plugins" not in calls[-1]["body"]
assert any("Web search isn't available" in l for l in job.logs)

# ---- 4. helpful errors ----
try:
    cl.chat("ollama/missing:7b", "", "hi"); raise SystemExit("expected error")
except llm.LLMError as e:
    assert "ollama pull missing:7b" in str(e), e
def refuse(*a, **k):
    raise llm.requests.ConnectionError("refused")
llm.requests.post = refuse
try:
    cl.stream_chat("ollama/qwen3:14b", "", "hi", on_text=print); raise SystemExit("expected error")
except llm.LLMError as e:
    assert "isn't running" in str(e) and "ollama serve" in str(e), e
llm.requests.post = fake_post

# ---- 5. a whole paper run with local models and no OpenRouter key ----
r = c.post("/api/jobs", data={"api_key": "", "writer_model": "ollama/qwen3:14b", "search_model": "ollama/qwen3:14b",
                              "use_literature": "false", "refine_rounds": "0"},
           files=[("materials", ("m.json", json.dumps({"idea": "idea", "experimental_log": "acc 91", "documents": []}), "application/json"))])
assert r.status_code == 200, r.text
r = c.post("/api/jobs", data={"api_key": "", "writer_model": "ollama/qwen3:14b", "search_model": "google/gemini-3-flash-preview"},
           files=[("materials", ("m.json", json.dumps({"idea": "idea"}), "application/json"))])
assert r.status_code == 400 and "local models" in r.json()["detail"]   # cloud search model still needs a key

# ---- 6. chat editing with a local model and no key ----
buf = io.BytesIO()
with zipfile.ZipFile(buf, "w") as z:
    z.writestr("main.tex", "\\documentclass{article}\\begin{document}\nOld.\n\\end{document}\n")
pid = c.post("/api/projects/import", files=[("files", ("p.zip", buf.getvalue(), "application/zip"))]).json()["id"]
SCRIPT.append("<think>hmm</think>Updated.\n\nmain.tex\n<<<<<<< SEARCH\nOld.\n=======\nNew.\n>>>>>>> REPLACE")
d = c.post(f"/api/projects/{pid}/chat", json={"message": "change it", "model": "ollama/qwen3:14b"}).json()
assert d["messages"][-1]["text"].startswith("Updated.") and d["version"] == 2, d["messages"][-1]
assert "New." in c.get(f"/api/projects/{pid}/file", params={"path": "main.tex"}).json()["text"]
d = c.post(f"/api/projects/{pid}/chat", json={"message": "x", "model": "google/gemini-3-flash-preview"})
assert d.status_code == 400 and "local model" in d.json()["detail"]

# ---- 7. model picker lists local models first ----
class Tags:
    def json(self): return {"models": [{"name": "qwen3:14b"}, {"name": "llama3.1:8b"}]}
orig_get = server.requests.get
server.requests.get = lambda url, timeout=None: Tags() if "/api/tags" in url else (_ for _ in ()).throw(Exception("offline"))
m = c.get("/api/models").json()
assert m["local"] == ["ollama/llama3.1:8b", "ollama/qwen3:14b"], m
assert [x["id"] for x in m["models"][:2]] == m["local"] and m["models"][0]["label"].startswith("Local"), m
assert [x["id"] for x in m["models"][2:]] == [i for i, _ in config.RECOMMENDED_MODELS]          # short list, no full catalog
assert not any("gemini-3-flash" in x["id"] for x in m["models"])
server.requests.get = orig_get

print("ALL OLLAMA TESTS PASSED")
