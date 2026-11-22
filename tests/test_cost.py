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

print("ALL COST TESTS PASSED")
