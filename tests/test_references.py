"""Reference checks with fake Semantic Scholar / OpenAlex responses (no network). Run: python tests/test_references.py"""
import os, sys, tempfile, time, types
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from paperscriptor import llm, references

references.time = types.SimpleNamespace(sleep=lambda s: None, time=time.time)


class R:
    def __init__(self, status, data): self.status_code, self._d, self.text = status, data, str(data)
    def json(self): return self._d


S2 = {
    "With Abstract": {"title": "With Abstract", "year": 2020, "abstract": "Full abstract.", "authors": [{"name": "A B"}]},
    "Only A Tldr Here": {"title": "Only A Tldr Here", "year": 2009, "abstract": None, "tldr": {"text": "Short summary."},
                         "authors": [{"name": "C D"}]},
    "No Abstract At All In This Journal": {"title": "No Abstract At All In This Journal", "year": 2010, "abstract": None,
                                           "authors": [{"name": "E F"}]},
    "Effects of team training on surgical outcomes": {"title": "Effects of team training on surgical outcomes in rural hospitals: a cohort", "year": 2011,
                                                   "abstract": None, "authors": [{"name": "G H"}]},
}
OA_LIMITED = {"on": False}


def fake_get(url, headers=None, params=None, timeout=None):
    if "openalex" in url:
        return R(429, {}) if OA_LIMITED["on"] else R(200, {"results": []})
    hit = S2.get(params["query"])
    assert "tldr" in params["fields"]
    return R(200, {"data": [hit] if hit else []})


references.requests.get = fake_get
job = llm.Job("r", tempfile.mkdtemp())
cfg = {"cutoff": "2026-11", "s2_api_key": "k", "openalex_api_key": ""}


def check(title):
    state = {"s2_down": False, "s2_streak": 0, "oa_down": False}
    return references.verify_paper(title, None, cfg, state, job)


p, src = check("With Abstract")
assert p["abstract"] == "Full abstract." and src == "Semantic Scholar"
p, _ = check("Only A Tldr Here")
assert p["abstract"] == "Short summary."                      # TLDR fills in a missing abstract
OA_LIMITED["on"] = True
p, src = check("No Abstract At All In This Journal")
assert p and p["abstract"] == "" and src == "Semantic Scholar"  # exact title: kept even without any abstract
p, _ = check("Effects of team training on surgical outcomes")
assert p is None                                              # near-miss title without abstract: dropped
p, _ = check("A Paper That Does Not Exist")
assert p is None
print("ALL REFERENCE TESTS PASSED")
