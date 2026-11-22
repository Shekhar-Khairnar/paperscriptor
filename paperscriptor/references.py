"""Checking that papers exist (Semantic Scholar, then OpenAlex) and turning them into BibTeX."""

import difflib
import re
import time

import requests

from .llm import Cancelled


S2_SEARCH_URL = "https://api.semanticscholar.org/graph/v1/paper/search"
OPENALEX_URL = "https://api.openalex.org/works"


# --------------------------------------------------------------------------- #
# Semantic Scholar verification
# --------------------------------------------------------------------------- #
def _date_allowed(paper: dict, cutoff: str) -> bool:
    try:
        cy, cm = (int(x) for x in cutoff.split("-")[:2])
    except Exception:
        return True
    pd = paper.get("publicationDate")
    if pd:
        try:
            y, m = (int(x) for x in pd.split("-")[:2])
            return (y, m) <= (cy, cm)
        except Exception:
            pass
    if paper.get("year"):
        return int(paper["year"]) <= cy
    return True


def _best_match(title, year_hint, cutoff, papers):
    """Pick the candidate whose title best matches (fuzzy), within the cutoff date."""
    best, best_score = None, 0.0
    for p in papers:
        if not p.get("title") or not _date_allowed(p, cutoff):
            continue
        raw = difflib.SequenceMatcher(None, title.lower(), p["title"].lower()).ratio() * 100
        if raw < 75:
            continue  # bonuses below must not lift a different paper over the threshold
        score = raw
        if year_hint and p.get("year") == year_hint:
            score += 10
        if p.get("abstract"):
            score += 2
        if "arxiv" not in (p.get("venue") or "arxiv").lower():
            score += 3  # prefer the published version over the preprint
        if score > best_score:
            best, best_score = p, score
    if not best or best_score <= 70:
        return None
    if not best.get("abstract"):
        # Publishers sometimes withhold abstracts; borrow one from a same-titled copy (e.g. the arXiv preprint)
        for p in papers:
            if p is not best and p.get("abstract") and _norm(p.get("title")) == _norm(best["title"]):
                best = {**best, "abstract": p["abstract"]}
                break
    return best


def s2_lookup(title, year_hint, cutoff, s2_key, job):
    """Returns (paper | None, status) with status in ok | nomatch | ratelimited | error."""
    headers = {"x-api-key": s2_key} if s2_key else {}
    params = {
        "query": title,
        "limit": 3,
        "fields": "title,authors,venue,year,abstract,citationCount,journal,publicationDate",
    }
    for attempt in range(3):
        job.check()
        try:
            r = requests.get(S2_SEARCH_URL, headers=headers, params=params, timeout=20)
        except requests.RequestException:
            time.sleep(2 * (attempt + 1))
            continue
        if r.status_code == 429:
            time.sleep(2 ** (attempt + 1))  # exponential backoff: 2s, 4s, 8s
            continue
        if r.status_code != 200:
            return None, "error"
        best = _best_match(title, year_hint, cutoff, r.json().get("data") or [])
        return best, ("ok" if best else "nomatch")
    return None, "ratelimited"


def _openalex_abstract(inverted: dict | None) -> str:
    if not inverted:
        return ""
    words = sorted((pos, w) for w, positions in inverted.items() for pos in positions)
    return " ".join(w for _, w in words)


def _openalex_to_paper(w: dict) -> dict:
    """Convert an OpenAlex work into the same shape Semantic Scholar returns."""
    src = (w.get("primary_location") or {}).get("source") or {}
    venue = (src.get("display_name") or "").split("/")[0].strip()
    if "arxiv" in venue.lower():
        venue = "arXiv"
    bib = w.get("biblio") or {}
    first, last = bib.get("first_page"), bib.get("last_page")
    pages = f"{first}--{last}" if first and last and first != last else first
    journal = {"name": venue, "volume": bib.get("volume"), "pages": pages} if src.get("type") == "journal" else None
    return {
        "title": w.get("display_name") or "",
        "authors": [{"name": a["author"]["display_name"]} for a in w.get("authorships") or []
                    if (a.get("author") or {}).get("display_name")],
        "venue": venue or "arXiv",
        "year": w.get("publication_year"),
        "publicationDate": w.get("publication_date"),
        "abstract": _openalex_abstract(w.get("abstract_inverted_index")),
        "citationCount": w.get("cited_by_count") or 0,
        "journal": journal,
    }


def openalex_lookup(title, year_hint, cutoff, oa_key, job):
    """Fallback verification via OpenAlex. Returns (paper | None, status) like s2_lookup."""
    clean = re.sub(r"[^\w\s-]", " ", title)  # commas/colons break OpenAlex filter syntax
    params = {
        "filter": f"title.search:{clean}",
        "per-page": 5,
        "select": "display_name,publication_year,publication_date,authorships,primary_location,"
                  "cited_by_count,biblio,abstract_inverted_index",
    }
    if oa_key:
        params["api_key"] = oa_key
    for attempt in range(3):
        job.check()
        try:
            r = requests.get(OPENALEX_URL, params=params, timeout=20)
        except requests.RequestException:
            time.sleep(2 * (attempt + 1))
            continue
        if r.status_code == 429:
            return None, "ratelimited"  # daily budget used up; retrying won't help
        if r.status_code != 200:
            return None, "error"
        papers = [_openalex_to_paper(w) for w in r.json().get("results") or []]
        best = _best_match(title, year_hint, cutoff, papers)
        return best, ("ok" if best else "nomatch")
    return None, "error"


def _safe(lookup, *args):
    """Run one lookup; an unexpected error (bad JSON, odd response) counts as 'not found', not a crash."""
    try:
        return lookup(*args)
    except Cancelled:
        raise
    except Exception:
        return None, "error"


def verify_paper(title, year_hint, cfg, state, job):
    """Semantic Scholar first; OpenAlex when S2 finds nothing, lacks an abstract, or is rate-limiting.
    Returns (paper, source_name) or (None, None)."""
    cutoff = cfg["cutoff"]
    if not state["s2_down"]:
        paper, status = _safe(s2_lookup, title, year_hint, cutoff, cfg.get("s2_api_key"), job)
        time.sleep(1.1)  # stay under 1 request/second
        if status == "ratelimited":
            state["s2_streak"] += 1
            if state["s2_streak"] >= 2:
                state["s2_down"] = True
                job.log("  Semantic Scholar keeps rate-limiting (no API key?), so OpenAlex is used for the rest of this run.")
        else:
            state["s2_streak"] = 0
        if paper and paper.get("abstract"):
            return paper, "Semantic Scholar"
    if not state["oa_down"]:
        paper, status = _safe(openalex_lookup, title, year_hint, cutoff, cfg.get("openalex_api_key"), job)
        time.sleep(0.2)
        if status == "ratelimited":
            state["oa_down"] = True
            job.log("  OpenAlex's free daily budget is used up. A free OpenAlex key raises it "
                    "(https://openalex.org/settings/api).")
        if paper and paper.get("abstract"):
            return paper, "OpenAlex"
    return None, None


def _norm(title: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (title or "").lower())


STOPWORDS = {"the", "a", "an", "in", "on", "at", "for", "to", "of", "and", "is", "are", "with", "by", "study"}


def _make_key(authors, year, title):
    last = authors[0]["name"].split()[-1] if authors and authors[0].get("name") else "Unknown"
    last = re.sub(r"[^a-zA-Z]", "", last).capitalize() or "Anon"
    words = [w.capitalize() for w in re.sub(r"[^a-zA-Z0-9\s]", "", title.lower()).split() if w not in STOPWORDS]
    return f"{last}{year or 'nd'}{''.join(words[:2]) or 'Paper'}"


def _bib_escape(s: str) -> str:
    return re.sub(r"(?<!\\)([&%#_$])", r"\\\1", str(s or ""))


def make_bibtex(papers: list) -> str:
    entries = []
    for p in papers:
        authors = " and ".join(_bib_escape(a) for a in p["authors"]) or "Unknown"
        if p.get("journal"):
            venue_line = f"  journal={{{_bib_escape(p['journal'])}}},\n"
            kind = "article"
        else:
            venue_line = f"  booktitle={{{_bib_escape(p['venue'] or 'arXiv')}}},\n"
            kind = "inproceedings"
        entry = (
            f"@{kind}{{{p['citation_key']},\n"
            f"  title={{{{{_bib_escape(p['title'])}}}}},\n"
            f"  author={{{authors}}},\n{venue_line}"
            f"  year={{{p['year'] or ''}}}"
        )
        if p.get("volume"):
            entry += f",\n  volume={{{_bib_escape(p['volume'])}}}"
        if p.get("pages"):
            entry += f",\n  pages={{{_bib_escape(p['pages'])}}}"
        entries.append(entry + "\n}")
    return "\n\n".join(entries) + "\n"
