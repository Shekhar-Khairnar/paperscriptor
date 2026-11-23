"""Cost controls: search cap with priorities, spending limit, cheap defaults. Run: python tests/test_cost.py"""
import json, os, sys, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["OPENROUTER_API_KEY"] = "sk-or-test"
from paperscriptor import config, llm, pipeline, server

config.RUNS_DIR = tempfile.mkdtemp()

# ---- the search cap keeps must-have citations first and covers every section ----
tasks = ([{"section": "Introduction", "focus": f"intro topic {i}", "search_type": "exploration"} for i in range(10)] +
         [{"section": f"Related Work: area {a}", "focus": f"area {a} query {i}", "search_type": "exploration"}
          for a in range(4) for i in range(8)] +
         [{"section": "Results - Setup", "focus": f"must cite {i}", "search_type": "targeted"} for i in range(5)] +
         [{"section": "Introduction", "focus": "Intro Topic 0", "search_type": "exploration"}])  # duplicate
picked = pipeline.limit_search_tasks(tasks, 20)
assert len(picked) == 20
assert [t["focus"] for t in picked[:5]] == [f"must cite {i}" for i in range(5)]           # targeted first
assert sum(t["section"] == "Introduction" for t in picked) == 10                         # then the introduction
assert {t["section"] for t in picked[15:]} == {f"Related Work: area {a}" for a in range(4)} or len(picked[15:]) < 4
assert len({t["focus"].lower() for t in picked}) == 20                                   # no duplicates
picked = pipeline.limit_search_tasks(tasks, 18)
rw = [t["section"] for t in picked if t["section"].startswith("Related")]
assert len(rw) == 3 and len(set(rw)) == 3                                                # round-robin across areas
assert len(pipeline.limit_search_tasks(tasks, 0)) == len(tasks) - 1                      # 0 = no cap (dedupe only)

# ---- spending limit ----
job = llm.Job("b", tempfile.mkdtemp())
job.add_cost(0.59)
assert not pipeline.over_budget(job, {"budget": 1.0}, 0.6)
job.add_cost(0.02)
assert pipeline.over_budget(job, {"budget": 1.0}, 0.6) and not pipeline.over_budget(job, {"budget": 1.0})
assert not pipeline.over_budget(job, {"budget": 0})                                      # 0 = no limit
assert not pipeline.over_budget(job, {})

# ---- cheap defaults: web search off, 20 searches, $1 limit, Inkling for search ----
job, cfg = server.prepare_run(api_key="k", materials={"idea": "x"})
assert cfg["use_web"] is False and cfg["max_searches"] == 20 and cfg["budget"] == 1.0, cfg
assert cfg["search_model"] == "thinkingmachines/inkling-2-small"
job, cfg = server.prepare_run(api_key="k", materials={"idea": "x"}, use_web=True, max_searches=500, budget=-3)
assert cfg["use_web"] is True and cfg["max_searches"] == 200 and cfg["budget"] == 0.0

# ---- the web form sends the same defaults ----
from fastapi.testclient import TestClient
c = TestClient(server.app)
seen = {}
pipeline_run = pipeline.run_pipeline
pipeline.run_pipeline = lambda job, cfg: seen.update(cfg)
r = c.post("/api/jobs", data={"api_key": "k"}, files=[("materials", ("m.json", json.dumps({"idea": "x"}), "application/json"))])
assert r.status_code == 200, r.text
import time; time.sleep(0.2)
assert seen["use_web"] is False and seen["max_searches"] == 20 and seen["budget"] == 1.0, seen
pipeline.run_pipeline = pipeline_run

# ---- web search asks for 3 results per search (about $0.012) ----
body = {}
class R:
    status_code = 200
    def json(self): return {"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}], "usage": {"cost": 0}}
llm.requests.post = lambda url, headers=None, json=None, timeout=None: body.update(json) or R()
llm.LLM("k", llm.Job("w", tempfile.mkdtemp())).chat("thinkingmachines/inkling-2-small", None, "find papers", web=True)
assert body["plugins"] == [{"id": "web", "max_results": 3}], body
assert "reasoning" not in body                                                           # default: the model decides

# ---- reasoning effort reaches OpenRouter; token counts are kept ----
assert config.DEFAULT_WRITER == "openai/gpt-6-luna" and config.RECOMMENDED_MODELS[0][0] == config.DEFAULT_WRITER
assert [llm.normalize_effort(x) for x in ("off", "LOW", "default", "", "silly", "xhigh")] == ["none", "low", "", "", "", "xhigh"]
class U:
    status_code = 200
    def json(self): return {"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
                            "usage": {"cost": 0.003, "prompt_tokens": 1200, "completion_tokens": 300,
                                      "completion_tokens_details": {"reasoning_tokens": 250}}}
body.clear()
llm.requests.post = lambda url, headers=None, json=None, timeout=None: body.update(json) or U()
uj = llm.Job("u", tempfile.mkdtemp())
llm.LLM("k", uj, "high").chat("openai/gpt-6-luna", None, "write")
assert body["reasoning"] == {"effort": "high"}, body
llm.LLM("k", uj, "none").chat("openai/gpt-6-luna", None, "write")
assert body["reasoning"] == {"effort": "none"}
assert uj.tokens == {"input": 2400, "output": 600, "reasoning": 500} and abs(uj.cost - 0.006) < 1e-9, (uj.tokens, uj.cost)
assert uj.snapshot()["tokens"] == uj.tokens
assert llm.token_summary(uj.tokens) == "2,400 input and 600 output tokens (500 of them reasoning)"

# a model that refuses the reasoning field is retried once without it
seen_bodies = []
class Refuse:
    status_code = 400
    text = '{"error": {"message": "reasoning is not supported for this model"}}'
    def json(self): return {"error": {"message": "reasoning is not supported for this model"}}
def picky(url, headers=None, json=None, timeout=None):
    seen_bodies.append(dict(json))
    return Refuse() if "reasoning" in json else U()
llm.requests.post = picky
assert llm.LLM("k", uj, "low").chat("some/model", None, "write") == "ok"
assert "reasoning" in seen_bodies[0] and "reasoning" not in seen_bodies[1], seen_bodies

job, cfg = server.prepare_run(api_key="k", materials={"idea": "x"}, reasoning="Low")
assert cfg["reasoning"] == "low" and cfg["writer_model"] == "openai/gpt-6-luna", cfg
assert "reasoning effort: low" in job.logs[0], job.logs
pipeline.run_pipeline = lambda job, cfg: seen.update(cfg)
r = c.post("/api/jobs", data={"api_key": "k", "reasoning": "medium"},
           files=[("materials", ("m.json", json.dumps({"idea": "x"}), "application/json"))])
assert r.status_code == 200, r.text
time.sleep(0.2)
assert seen["reasoning"] == "medium", seen
pipeline.run_pipeline = pipeline_run
assert "none" in c.get("/api/config").json()["efforts"]

# ---- prices per 1M tokens come from OpenRouter's public list (cached) ----
class Models:
    def json(self): return {"data": [{"id": "openai/gpt-6-luna", "pricing": {"prompt": "0.0000001", "completion": "0.0000005"}},
                                     {"id": "broken/model", "pricing": {"prompt": "n/a"}}]}
hits = []
llm.requests.get = lambda url, timeout=None: hits.append(url) or Models()
llm._prices.update(at=0.0, data={})
p = c.get("/api/prices").json()["prices"]
assert p == {"openai/gpt-6-luna": {"input": 0.1, "output": 0.5}}, p
c.get("/api/prices")
assert len(hits) == 1                                                                    # cached
llm._prices.update(at=0.0, data={})
llm.requests.get = lambda url, timeout=None: (_ for _ in ()).throw(OSError("offline"))
assert c.get("/api/prices").json() == {"prices": {}}                                     # offline: no prices, no error

print("ALL COST TESTS PASSED")
