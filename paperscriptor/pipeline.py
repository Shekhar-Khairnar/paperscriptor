"""Paper-writing pipeline, ported from google-research/paper-orchestra to OpenRouter.

Steps (same order as methods/paper_writer.py):
  1. Outline agent        -> outline.json
  2. Literature agent     -> finds papers, verifies them on Semantic Scholar,
                             writes references.bib and the Intro + Related Work
  3. Section writing      -> fills the remaining sections (tables, figures)
  4. Review + refine      -> an LLM reviewer scores the draft; the writer revises;
                             a revision is kept only if the score does not drop
  5. Package              -> paper/ folder + paper.zip (+ paper.pdf if LaTeX is installed)
"""

import datetime
import json
import os
import re
import shutil
import time
import traceback
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed

import json_repair

from . import prompts, references
from .llm import Cancelled, ImagesUnsupported, Job, LLM, LLMError
from .references import _make_key, _norm, make_bibtex


RASTER_EXTS = (".png", ".jpg", ".jpeg")
STEPS = ["outline", "literature", "writing", "refining", "packaging"]


# --------------------------------------------------------------------------- #
# Response parsing
# --------------------------------------------------------------------------- #
def extract_json(text: str):
    blocks = re.findall(r"```json\s*(.*?)```", text, re.S)
    if not blocks and "{" in text:
        blocks = [text[text.find("{"): text.rfind("}") + 1]]
    for b in blocks:
        try:
            return json.loads(b)
        except ValueError:
            try:
                v = json_repair.loads(b)
                if v:
                    return v
            except Exception:
                pass
    return None


def extract_latex(text: str):
    m = re.search(r"```latex\s*(.*?)```", text, re.S)
    if m:
        tex = m.group(1)
    elif "```latex" in text:  # unterminated block
        tex = text.split("```latex", 1)[1]
    elif "\\documentclass" in text:
        tex = text[text.find("\\documentclass"):]
    else:
        return None
    tex = tex.strip()
    if "\\documentclass" not in tex or "\\end{document}" not in tex:
        return None
    return tex + "\n"


CITE_RE = re.compile(r"\\(cite[a-zA-Z]*\*?)(\[[^\]]*\])?(\[[^\]]*\])?\{([^}]*)\}")


def fix_citations(tex: str, valid_keys: set, job: Job) -> str:
    """Drop \\cite keys that are not in references.bib so LaTeX compiles cleanly."""
    dropped = set()

    def repl(m):
        keys = [k.strip() for k in m.group(4).split(",") if k.strip()]
        keep = [k for k in keys if k in valid_keys]
        dropped.update(k for k in keys if k not in valid_keys)
        if not keep:
            return ""
        return f"\\{m.group(1)}{m.group(2) or ''}{m.group(3) or ''}{{{','.join(keep)}}}"

    out = CITE_RE.sub(repl, tex)
    if dropped:
        job.log(f"  Removed {len(dropped)} citation key(s) that are not in the bibliography.")
    return out


# --------------------------------------------------------------------------- #
# Literature agent helpers (ported from HybridLiteratureAgent)
# --------------------------------------------------------------------------- #
def limit_search_tasks(tasks: list, limit: int) -> list:
    """Keep at most `limit` searches: drop near-duplicates, then prefer must-have citations, then the
    introduction, then related work, taking them round-robin across sections so no section is left out."""
    seen, unique = set(), []
    for t in tasks:
        key = _norm(t.get("focus", ""))
        if key and key not in seen:
            seen.add(key)
            unique.append(t)
    if limit <= 0 or len(unique) <= limit:
        return unique
    rank = lambda t: 0 if t["search_type"] == "targeted" else 1 if t["section"] == "Introduction" else 2
    groups: dict = {}
    for t in sorted(unique, key=rank):
        groups.setdefault((rank(t), t["section"]), []).append(t)
    picked = []
    for level in (0, 1, 2):
        lanes = [g for (r, _), g in groups.items() if r == level]
        while lanes and len(picked) < limit:
            for g in list(lanes):
                if g and len(picked) < limit:
                    picked.append(g.pop(0))
                if not g:
                    lanes.remove(g)
    return picked


def over_budget(job: Job, cfg: dict, share: float = 1.0) -> bool:
    """True once the run has spent `share` of its spending limit (0 or missing = no limit)."""
    budget = float(cfg.get("budget") or 0)
    return budget > 0 and job.cost >= budget * share


def collect_search_tasks(outline: dict) -> list:
    tasks = []
    plan = outline.get("intro_related_work_plan", {}) or {}
    intro = plan.get("introduction_strategy", {}) or {}
    for d in intro.get("search_directions", []) or []:
        tasks.append({
            "section": "Introduction",
            "focus": d,
            "context": f"Hook: {intro.get('hook_hypothesis', '')}. Gap: {intro.get('problem_gap_hypothesis', '')}",
            "search_type": "exploration",
        })
    rw = plan.get("related_work_strategy", {}) or {}
    for sub in rw.get("subsections", []) or []:
        queries = sub.get("limitation_search_queries") or [
            f"{sub.get('subsection_title', '')} {sub.get('methodology_cluster', '')}"
        ]
        for q in queries:
            tasks.append({
                "section": f"Related Work: {sub.get('subsection_title', 'General')}",
                "focus": q,
                "context": f"Mission: {sub.get('sota_investigation_mission', '')}. Hypothesis: {sub.get('limitation_hypothesis', '')}",
                "search_type": "exploration",
            })
    for section in outline.get("section_plan", []) or []:
        st = section.get("section_title", "Unknown Section")
        for sub in section.get("subsections", []) or []:
            sub_title = sub.get("subsection_title", "General")
            ctx = " ".join(sub.get("content_bullets", []) or [])
            for hint in sub.get("citation_hints", []) or []:
                tasks.append({
                    "section": f"{st} - {sub_title}",
                    "focus": hint,
                    "context": f"Must-have citation for section covering: {ctx}",
                    "search_type": "targeted",
                })
    return tasks


CANDIDATE_SCHEMA = '{"section_name": "string", "candidates": [{"title": "exact paper title", "year": 2020, "reason": "why it is relevant"}]}'


def discover_candidates(llm, model, task, outline, cutoff, use_web):
    how = "Use web search to find" if use_web else "Recall from your knowledge"
    if task["search_type"] == "targeted":
        prompt = f"""Identify the SPECIFIC academic paper described by this hint: "{task['focus']}".
CONTEXT: {task['context']}
CUTOFF DATE: Published BEFORE {cutoff}

INSTRUCTIONS:
1. {how} the EXACT title and year of this paper.
2. If the hint refers to a dataset, model architecture, or baseline (e.g., "research paper introducing..."), you MUST find the primary, original paper that introduced it.
3. Return EXACTLY 1 candidate paper that matches best.

YOU MUST return the results in json format (```json content```) following this schema:
{CANDIDATE_SCHEMA}"""
        temp = 0.1
    else:
        problem = (outline.get("intro_related_work_plan", {}) or {}).get("introduction_strategy", {}).get("problem_gap_hypothesis", "N/A")
        prompt = f"""Find 5-10 highly relevant, influential academic papers matching the search task. Do not force results; only return strictly relevant papers.
SEARCH TASK: {task['focus']}
CONTEXT FOR TASK: {task['context']}
BROADER PROJECT PROBLEM: {problem}
CUTOFF DATE: Published BEFORE {cutoff}

INSTRUCTIONS:
1. {how} REAL papers (ArXiv, CVPR, NeurIPS, ICML, ICLR, etc.). Use exact titles.
2. Only return papers published in top-tier conferences or journals, or highly-cited works, or the exact state-of-the-art relevant to the task.

YOU MUST return the results in json format (```json content```) following this schema:
{CANDIDATE_SCHEMA}"""
        temp = 0.4
    for _ in range(2):
        parsed = extract_json(llm.chat(model, None, prompt, temperature=temp, web=use_web))
        if isinstance(parsed, dict) and parsed.get("candidates"):
            return [c for c in parsed["candidates"] if isinstance(c, dict) and c.get("title")]
    return []


def inject_citations(outline: dict, papers: list) -> dict:
    by_section = {}
    for p in papers:
        by_section.setdefault(p["found_in_section"], []).append(p["citation_key"])
    plan = outline.get("intro_related_work_plan", {}) or {}
    intro = plan.get("introduction_strategy", {})
    if isinstance(intro, dict):
        intro["citation_candidates"] = sorted(set(by_section.get("Introduction", [])))
    for sub in (plan.get("related_work_strategy", {}) or {}).get("subsections", []) or []:
        sub["citation_candidates"] = by_section.get(f"Related Work: {sub.get('subsection_title', 'General')}", [])
    for section in outline.get("section_plan", []) or []:
        st = section.get("section_title", "Unknown Section")
        for sub in section.get("subsections", []) or []:
            sub["citation_candidates"] = by_section.get(f"{st} - {sub.get('subsection_title', 'General')}", [])
            sub.pop("citation_hints", None)
    return outline


# --------------------------------------------------------------------------- #
# Review helpers
# --------------------------------------------------------------------------- #
SUBSCORES = ["Soundness", "Presentation", "Contribution", "Originality", "Quality", "Clarity", "Significance"]


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def review_paper(llm, model, tex):
    prompt = prompts.AGENTREVIEW_INSTRUCTIONS + f"\nHere is the paper you are asked to review:\n```\n{tex}\n```"
    for _ in range(2):
        review = extract_json(llm.chat(model, prompts.REVIEWER_SYSTEM_PROMPT, prompt, temperature=0.75))
        if isinstance(review, dict) and "Overall" in review:
            return review
    return None


def _better_or_equal(new, old):
    if _num(new.get("Overall")) != _num(old.get("Overall")):
        return _num(new.get("Overall")) > _num(old.get("Overall"))
    return sum(_num(new.get(k)) for k in SUBSCORES) >= sum(_num(old.get(k)) for k in SUBSCORES)


# --------------------------------------------------------------------------- #
# Main pipeline
# --------------------------------------------------------------------------- #
def _write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def _latex_call(llm, job, model, system, user, images=(), temperature=0.7, label="LaTeX"):
    """Call the model until it returns a complete LaTeX document (max 3 tries)."""
    for attempt in range(3):
        try:
            text = llm.chat(model, system, user, images=images, temperature=temperature)
        except ImagesUnsupported:
            job.log(f"  Model '{model}' does not accept images; continuing with captions only.")
            images = ()
            continue
        tex = extract_latex(text)
        if tex:
            return tex
        job.log(f"  {label}: response was not a complete LaTeX document, retrying ({attempt + 1}/3)...")
    raise LLMError(f"{label}: the model did not return a complete LaTeX document.")



# --------------------------------------------------------------------------- #
# Fidelity: writing rules, claim-to-evidence check, bibliography pruning, author TODO list
# --------------------------------------------------------------------------- #
FIDELITY_RULES = r"""
### FIDELITY RULES (follow strictly)
- Figure captions and figure descriptions: say only what the author's caption and data say. Never mention visual
  details (colours, arrows, dashed lines, annotations, panels, markers) that you were not told about.
- Every conclusion must follow from a comparison that was actually run in the experimental log. Do not generalise
  beyond the evaluated models, data and settings (for example, no claims about model types that were not tested).
- Present explanations of *why* a result happened as hypotheses ("may", "one possible explanation"), not as findings,
  unless the log contains an experiment that tests them. Never call something a correlation unless one was computed.
- Avoid absolute statements ("cannot", "always", "never", "proves", "guarantees") unless they are strictly true.
- State limitations and caveats (for example synthetic data) clearly once in the abstract and once in a limitations
  paragraph. Do not repeat the same caveat in every section and caption.
- Prefer precise, relevant citations over many citations; cite only where a reference supports the sentence.
- If information that only the authors can give is missing (hyperparameters, funding, author contributions, data
  links), do not invent it. Leave a short visible placeholder such as \textbf{[AUTHOR: add optimizer settings]}.
"""

CLAIM_CHECK_SYSTEM = """You are a meticulous scientific fact-checker. You compare a LaTeX manuscript with the authors' own
materials and fix only statements that the evidence does not support. You do not rewrite for style."""

CHECK_TYPES = {"number", "figure", "overreach", "mechanism", "absolute", "repetition", "missing"}


def _replace_once(text: str, old: str, new: str) -> str | None:
    if old in text:
        return text.replace(old, new, 1)
    pattern = r"\s+".join(re.escape(tok) for tok in old.split())  # same text, different line breaks
    m = re.search(pattern, text) if pattern else None
    return text[:m.start()] + new + text[m.end():] if m else None


def check_claims(llm, job, model, tex, idea, log_md, figures):
    """Find and fix unsupported statements. Returns (new_tex, report dict)."""
    fig_ctx = "\n".join(f"- {f['name']}: {f['caption'] or '(no caption given)'}" for f in figures) or "(no figures)"
    user = f"""--- AUTHORS' IDEA AND METHOD NOTES ---
{idea}

--- AUTHORS' EXPERIMENTAL LOG (the only source of results) ---
{log_md}

--- FIGURES THE AUTHORS PROVIDED (file: caption) ---
{fig_ctx}

--- MANUSCRIPT (LaTeX) ---
{tex}

TASK: Find up to 25 statements in the manuscript with one of these problems:
- "number": a number that is not in the materials and cannot be derived from them.
- "figure": a caption or sentence describing figure content not stated in the authors' caption (e.g. arrows, dashed lines, colours, annotations).
- "overreach": a conclusion broader than the comparisons actually run (e.g. about models or settings that were not tested).
- "mechanism": an explanation of why something happened stated as an established finding, or called a correlation without one being computed.
- "absolute": a categorical claim ("cannot", "always", "proves") that is not strictly true.
- "repetition": the same caveat repeated in many places (keep the first, shorten the later ones).
- "missing": the paper says something exists (a file, code, dataset, experiment, setting) that is not in the materials.

Return ONLY a JSON object:
{{"issues": [{{"text": "exact text copied from the LaTeX (one sentence or clause, at most 400 characters)",
  "type": "one of the types above", "why": "one short sentence",
  "fix": "replacement LaTeX that is hedged or corrected; keep every \\\\cite{{...}} and \\\\ref{{...}} from the original; empty string to delete"}}],
 "author_todo": ["things only the authors can provide or decide, e.g. missing settings, files to add, statements to verify"]}}
Rules: "text" must match the LaTeX exactly. Do not touch the preamble, correct numbers, tables of correct results or the
bibliography. If there are no problems, return empty lists."""
    report = {"issues": [], "applied": 0, "author_todo": []}
    try:
        data = extract_json(llm.chat(model, CLAIM_CHECK_SYSTEM, user, temperature=0.1)) or {}
    except LLMError as e:
        job.log(f"  ! Fact check skipped: {e}")
        return tex, report
    issues = [i for i in (data.get("issues") or []) if isinstance(i, dict)]
    todo = [str(t).strip() for t in (data.get("author_todo") or []) if str(t).strip()]
    new_tex = tex
    for issue in issues[:25]:
        old, fix, kind = str(issue.get("text") or ""), str(issue.get("fix") or ""), str(issue.get("type") or "")
        if len(old) < 12 or kind not in CHECK_TYPES or "\\documentclass" in old or "\\end{document}" in old:
            continue
        cites = re.findall(r"\\cite[a-zA-Z]*\*?(?:\[[^\]]*\])*\{[^}]*\}", old)
        missing = [c for c in cites if c not in fix]
        if fix and missing:  # never lose a citation while rewording
            end = len(fix.rstrip(" .")) if fix.rstrip().endswith(".") else len(fix)
            fix = fix[:end] + "~" + "".join(missing) + fix[end:]
        updated = _replace_once(new_tex, old, fix)
        if updated is not None:
            new_tex = updated
            issue["applied"] = True
            report["applied"] += 1
    if "\\documentclass" not in new_tex or "\\end{document}" not in new_tex:
        job.log("  ! Fact check changes would break the document; keeping the previous version.")
        return tex, report
    report.update(issues=issues, author_todo=todo)
    return new_tex, report


def prune_bib(bib: str, tex: str) -> tuple[str, int]:
    """Keep only bibliography entries that the paper cites. Returns (new_bib, removed_count)."""
    cited = {k.strip() for m in CITE_RE.finditer(tex) for k in m.group(4).split(",") if k.strip()}
    entries = [e for e in re.split(r"(?m)^(?=@\w+\s*\{)", bib) if e.strip()]
    keep, removed = [], 0
    for e in entries:
        m = re.match(r"@\w+\s*\{\s*([^,\s]+)", e)
        if m and m.group(1) not in cited:
            removed += 1
        else:
            keep.append(e.strip() + "\n")
    return "\n".join(keep), removed


PLACEHOLDER_RE = re.compile(r"(?m)^\s*([A-Z][A-Za-z /-]{2,40}) here\.\s*$")
AUTHOR_NOTE_RE = re.compile(r"\[AUTHOR:\s*([^\]]+)\]")


def author_todo_md(tex: str, report: dict) -> str:
    """A checklist of what only the authors can finish, for paper/TODO.md."""
    lines = tex.splitlines()
    fill = []
    for i, line in enumerate(lines, 1):
        for m in PLACEHOLDER_RE.finditer(line):
            fill.append(f"- [ ] {m.group(1)} (main.tex, line {i})")
        for m in AUTHOR_NOTE_RE.finditer(line):
            fill.append(f"- [ ] {m.group(1).strip()} (main.tex, line {i})")
    out = ["# Before you submit", "",
           "Open items found by PaperScriptor. Ask the chat to help with any of them, and tick or delete lines as you",
           "finish them. The chat reads this file and will remind you of what is still open.", ""]
    if fill:
        out += ["## Fill in", ""] + fill + [""]
    if report.get("author_todo"):
        out += ["## Provide or check", ""] + [f"- [ ] {t}" for t in report["author_todo"][:20]] + [""]
    changed = [i for i in report.get("issues", []) if i.get("applied")]
    if changed:
        out += ["## Statements the fact check changed (please review)", ""]
        out += [f"- {i.get('type')}: {str(i.get('why') or '').strip()} Was: \"{str(i.get('text'))[:160]}\"" for i in changed[:15]]
        out += [""]
    out += ["## Always", "", "- [ ] Check every number against your own results.",
            "- [ ] Read every claim and make sure your evidence supports it.",
            "- [ ] Follow your venue's policy on AI-assisted writing.", ""]
    return "\n".join(out)


def run_pipeline(job: Job, cfg: dict):
    """cfg keys: api_key, writer_model, search_model, template_dir, idea, experimental_log,
    cutoff, use_literature, use_web, refine_rounds, s2_api_key, figures [{name, caption}]"""
    try:
        _run(job, cfg)
        job.status = "done"
        job.step = "done"
        job.log(f"Finished. Total OpenRouter cost: ${job.cost:.4f}. Output folder: {job.run_dir}")
    except Cancelled:
        job.status = "cancelled"
        job.log(f"Cancelled. OpenRouter cost so far: ${job.cost:.4f}.")
    except Exception as e:
        job.status = "error"
        job.error = str(e)
        job.log(f"ERROR: {e}")
        with open(os.path.join(job.run_dir, "log.txt"), "a", encoding="utf-8") as f:
            f.write(traceback.format_exc())
    finally:
        job.finished_at = time.time()


def _run(job: Job, cfg: dict):
    run = job.run_dir
    llm = LLM(cfg["api_key"], job)
    writer, searcher = cfg["writer_model"], cfg["search_model"]
    cutoff = cfg["cutoff"]
    idea, log_md = cfg["idea"], cfg["experimental_log"]

    # Working LaTeX project = copy of the template (+ uploaded figures already in paper/figures)
    paper_dir = os.path.join(run, "paper")
    shutil.copytree(cfg["template_dir"], paper_dir, dirs_exist_ok=True)
    with open(os.path.join(paper_dir, "template.tex"), encoding="utf-8") as f:
        template_tex = f.read()
    with open(os.path.join(paper_dir, "guidelines.md"), encoding="utf-8") as f:
        guidelines = f.read()
    os.remove(os.path.join(paper_dir, "template.tex"))
    os.remove(os.path.join(paper_dir, "guidelines.md"))

    # ---------------- Step 1: Outline ----------------
    job.step = "outline"
    job.log(f"Step 1/6 · Outline agent ({writer})")
    user = (
        f"'idea.md':\n{idea}\n\n'experimental_log.md':\n{log_md}\n\n"
        f"'template.tex':\n{template_tex}\n\n'conference_guidelines.md':\n{guidelines}"
    )
    outline = None
    for attempt in range(3):
        outline = extract_json(llm.chat(writer, prompts.outline_agent_system_prompt.format(cutoff_date=cutoff), user))
        if isinstance(outline, dict) and "section_plan" in outline:
            break
        job.log(f"  Outline was not valid JSON, retrying ({attempt + 1}/3)...")
        outline = None
    if outline is None:
        raise LLMError("The outline agent did not return a valid outline.")
    _write(os.path.join(run, "outline.json"), json.dumps(outline, indent=2))
    job.files["outline.json"] = os.path.join(run, "outline.json")
    n_sections = len(outline.get("section_plan", []))
    job.log(f"  Outline ready: {n_sections} planned sections.")

    # ---------------- Step 2: Literature ----------------
    job.step = "literature"
    papers: list = []
    current_tex = template_tex
    if cfg["use_literature"]:
        planned = collect_search_tasks(outline)
        limit = int(cfg.get("max_searches") or 0)
        tasks = limit_search_tasks(planned, limit)
        trimmed = f" (the {len(tasks)} most important of {len(planned)} planned, to keep cost down)" if len(tasks) < len(planned) else ""
        job.log(f"Step 2/6 · Literature agent: {len(tasks)} searches{trimmed} ({searcher}"
                f"{', web search on, about $0.012 each' if cfg['use_web'] else ''})")
        found, stopped = [], False
        with ThreadPoolExecutor(max_workers=6) as ex:
            futs = {ex.submit(discover_candidates, llm, searcher, t, outline, cutoff, cfg["use_web"]): t for t in tasks}
            for i, fut in enumerate(as_completed(futs), 1):
                t = futs[fut]
                try:
                    for c in fut.result():
                        found.append((c, t))
                except Cancelled:
                    raise
                except Exception as e:
                    job.log(f"  Search failed for '{t['focus'][:60]}': {e}")
                if i % 5 == 0 or i == len(tasks):
                    job.log(f"  Searched {i}/{len(tasks)}, {len(found)} candidates so far (cost ${job.cost:.2f}).")
                if not stopped and over_budget(job, cfg, 0.6):
                    stopped = True
                    for f in futs:
                        f.cancel()
                    job.log(f"  Stopping the search early: ${job.cost:.2f} spent, 60% of the ${float(cfg['budget']):.2f} "
                            "spending limit. Writing continues with the papers found so far.")

        # De-duplicate candidates before verifying them
        seen, unique = set(), []
        for c, t in found:
            k = _norm(c.get("title"))
            if k and k not in seen:
                seen.add(k)
                unique.append((c, t))
        job.log(f"  Verifying {len(unique)} unique candidates on Semantic Scholar, with OpenAlex as backup "
                "(about 1 per second)...")

        registry, keys = {}, set()
        state = {"s2_down": False, "s2_streak": 0, "oa_down": False}
        by_source = {"Semantic Scholar": 0, "OpenAlex": 0}
        for i, (c, t) in enumerate(unique, 1):
            job.check()
            if state["s2_down"] and state["oa_down"]:
                job.log(f"  ! Both paper databases are unavailable; stopping verification at {i - 1}/{len(unique)}.")
                break
            year = c.get("year") if isinstance(c.get("year"), int) else None
            s2, source = references.verify_paper(c["title"], year, cfg, state, job)
            if s2 and _norm(s2["title"]) not in registry:
                by_source[source] += 1
                key = base = _make_key(s2.get("authors") or [], s2.get("year"), s2["title"])
                suffix = ord("a")
                while key in keys:
                    key = f"{base}{chr(suffix)}"
                    suffix += 1
                keys.add(key)
                journal = s2.get("journal") or {}
                abstract = s2["abstract"]
                registry[_norm(s2["title"])] = {
                    "citation_key": key,
                    "title": s2["title"],
                    "authors": [a["name"] for a in s2.get("authors") or [] if a.get("name")],
                    "venue": s2.get("venue") or "arXiv",
                    "year": s2.get("year") or year,
                    "abstract": abstract[:1500] + ("... [Truncated]" if len(abstract) > 1500 else ""),
                    "citation_count": s2.get("citationCount") or 0,
                    "found_in_section": t["section"],
                    "reason": c.get("reason", ""),
                    "journal": journal.get("name"),
                    "volume": journal.get("volume"),
                    "pages": journal.get("pages"),
                }
            if i % 10 == 0 or i == len(unique):
                job.log(f"  Checked {i}/{len(unique)} · {len(registry)} verified papers "
                        f"(Semantic Scholar {by_source['Semantic Scholar']}, OpenAlex {by_source['OpenAlex']}).")
        papers = list(registry.values())
        if not papers:
            job.log("  ! No papers could be verified (both paper databases may be rate-limiting). "
                    "The paper will be written without citations.")

        citation_map = {p["citation_key"]: {k: p[k] for k in ("citation_key", "title", "authors", "venue", "year", "abstract")} for p in papers}
        _write(os.path.join(run, "citation_map.json"), json.dumps(citation_map, indent=2))
        _write(os.path.join(paper_dir, "references.bib"), make_bibtex(papers) if papers else "")
        outline = inject_citations(outline, papers)
        _write(os.path.join(run, "outline.json"), json.dumps(outline, indent=2))

        job.log(f"  Writing Introduction and Related Work with {len(papers)} references ({writer})...")
        payload = {
            "template.tex": template_tex,
            "intro_related_work_plan": outline.get("intro_related_work_plan", {}),
            "project_idea": idea,
            "project_experimental_log": log_md,
            "citation_checklist": [p["citation_key"] for p in papers],
            "collected_papers": papers,
        }
        system = prompts.literature_review_agent_writter_prompt.format(
            paper_count=len(papers), min_cite_paper_count=int(len(papers) * 0.9), cutoff_date=cutoff
        )
        current_tex = _latex_call(
            llm, job, writer, system,
            "Generate the LaTeX for Introduction and Related Work sections.\n\n" + json.dumps(payload),
            label="Intro/Related Work",
        )
        current_tex = fix_citations(current_tex, set(citation_map), job)
        _write(os.path.join(run, "drafts", "1_intro_related_work.tex"), current_tex)
    else:
        job.log("Step 2/6 · Literature search skipped (turned off).")
        citation_map = {}
        _write(os.path.join(paper_dir, "references.bib"), "")

    # ---------------- Step 3: Section writing ----------------
    job.step = "writing"
    figures = cfg.get("figures") or []
    image_paths = [os.path.join(paper_dir, "figures", f["name"]) for f in figures if f["name"].lower().endswith(RASTER_EXTS)]
    job.log(f"Step 3/6 · Section writing agent ({writer}, {len(figures)} figure(s))")
    if figures:
        fig_ctx = "### AVAILABLE FIGURES LIST\n" + "".join(
            f"Item {i}:\n  - Filename: {f['name']}\n  - Caption: {f['caption'] or 'No caption provided.'}\n"
            "--------------------------------------------------\n"
            for i, f in enumerate(figures, 1)
        )
    else:
        fig_ctx = "No figures provided. Do NOT include any \\includegraphics commands."
    lib_ctx = "No citation data provided. Do NOT use any \\cite commands." if not citation_map else (
        "### REFERENCE LIBRARY (Use these keys for \\cite{})\n" + "".join(
            f"--- Key: {k} ---\nTitle: {v['title']}\nAuthors: {', '.join(v['authors'][:3])}"
            f"{' et al.' if len(v['authors']) > 3 else ''} ({v['year']})\nAbstract: {v['abstract']}\n\n"
            for k, v in citation_map.items()
        )
    )
    missing_note = "" if cfg["use_literature"] else (
        "\nNOTE: The Introduction and Related Work sections are also still placeholders. Write them too, without citations.\n"
    )
    user = f"""
Here are the input materials for the paper.

--- INPUT: outline.json (Structure & Citation Candidates) ---
{json.dumps(outline, indent=2)}

--- INPUT: citation_map.json (Reference Library) ---
{lib_ctx}

--- INPUT: idea.md (Technical Method Details) ---
{idea}

--- INPUT: experimental_log.md (Raw Data for Tables) ---
{log_md}

--- INPUT: figures_list (Available Figure Files) ---
{fig_ctx}

--- INPUT: conference_guidelines.md ---
{guidelines}

--- INPUT: template.tex (DO NOT CHANGE EXISTING TEXT. FILL MISSING SECTIONS.) ---
{current_tex}
{missing_note}
**INSTRUCTION**:
1. Identify missing sections in `template.tex`.
2. Generate LaTeX content for those sections based on `outline.json`.
3. Construct tables using data from `experimental_log.md`.
4. Insert figures using the filenames from the 'figures_list' input.
5. Use citations from the Reference Library.
6. Return the **full** compilable LaTeX file.
{FIDELITY_RULES}"""
    current_tex = _latex_call(llm, job, writer, prompts.section_writing_agent_prompt, user,
                              images=image_paths, label="Section writing")
    current_tex = fix_citations(current_tex, set(citation_map), job)
    _write(os.path.join(run, "drafts", "2_full_draft.tex"), current_tex)
    job.log("  Full draft written.")

    # ---------------- Step 4: Review + refine ----------------
    job.step = "refining"
    rounds = int(cfg.get("refine_rounds") or 0)
    reviews = []
    if rounds > 0:
        job.log(f"Step 4/6 · Review and refine ({rounds} round(s), {writer})")
        review = review_paper(llm, writer, current_tex)
        if review is None:
            job.log("  ! Reviewer did not return a usable review; skipping refinement.")
        else:
            reviews.append({"version": "draft", "review": review})
            job.log(f"  Reviewer score for the first draft: {review.get('Overall')}/10")
            worklogs = []
            citations_json = json.dumps(citation_map, indent=2)
            for i in range(1, rounds + 1):
                job.check()
                if over_budget(job, cfg):
                    job.log(f"  Skipping the remaining review rounds: ${job.cost:.2f} reached the "
                            f"${float(cfg['budget']):.2f} spending limit.")
                    break
                user = f"""
REFLECTION ITERATION {i}.

--- PREVIOUS ITERATION LOG ---
{json.dumps(worklogs, indent=4) if worklogs else 'None'}

--- CURRENT REVIEWER FEEDBACK (Score: {review.get('Overall')}) ---
{json.dumps(review, indent=2)}

--- CONFERENCE GUIDELINES ---
{guidelines}

--- EXPERIMENTAL LOG (Data Truth) ---
{log_md}

--- CITATION MAP (Reference Truth) ---
{citations_json}

--- CURRENT LATEX SOURCE ---
{current_tex}

INSTRUCTION:
1. Your goal is to IMPROVE the reviewer score (Current: {review.get('Overall')}).
2. Address the 'Weaknesses' and 'Questions' in the Reviewer Feedback above.
3. Output the JSON Worklog first, then the Full Revised LaTeX.
{FIDELITY_RULES}"""
                text = llm.chat(writer, prompts.content_refinement_agent_system_prompt, user)
                new_tex = extract_latex(text)
                if not new_tex:
                    job.log(f"  Round {i}: revision was not a complete LaTeX document; keeping previous version.")
                    continue
                new_tex = fix_citations(new_tex, set(citation_map), job)
                worklog = extract_json(text.split("```latex")[0]) or {}
                _write(os.path.join(run, "drafts", f"3_refined_v{i}.tex"), new_tex)
                new_review = review_paper(llm, writer, new_tex)
                if new_review is None:
                    job.log(f"  Round {i}: reviewer failed; keeping previous version.")
                    continue
                accepted = _better_or_equal(new_review, review)
                reviews.append({"version": f"v{i}", "review": new_review, "accepted": accepted, "worklog": worklog})
                job.log(f"  Round {i}: score {review.get('Overall')} -> {new_review.get('Overall')}/10 · "
                        f"{'kept revision' if accepted else 'reverted (score dropped)'}")
                if accepted:
                    current_tex, review = new_tex, new_review
                    worklogs.append(worklog)
        _write(os.path.join(run, "reviews.json"), json.dumps(reviews, indent=2))
        job.files["reviews.json"] = os.path.join(run, "reviews.json")
    else:
        job.log("Step 4/6 · Refinement skipped (0 rounds).")

    # ---------------- Step 5: Fact check ----------------
    job.step = "checking"
    report = {"issues": [], "applied": 0, "author_todo": []}
    if cfg.get("fact_check", True) and not over_budget(job, cfg):
        job.log(f"Step 5/6 · Fact check: comparing every claim with your materials ({writer})")
        current_tex, report = check_claims(llm, job, writer, current_tex, idea, log_md, figures)
        current_tex = fix_citations(current_tex, set(citation_map), job)
        kinds = {}
        for i in report["issues"]:
            if i.get("applied"):
                kinds[i.get("type")] = kinds.get(i.get("type"), 0) + 1
        detail = ", ".join(f"{n} {k}" for k, n in sorted(kinds.items()))
        job.log(f"  Fixed {report['applied']} unsupported statement(s){f' ({detail})' if detail else ''}; "
                f"{len(report['author_todo'])} item(s) for you in TODO.md.")
        _write(os.path.join(run, "fact_check.json"), json.dumps(report, indent=2))
        job.files["fact_check.json"] = os.path.join(run, "fact_check.json")
    else:
        job.log("Step 5/6 · Fact check skipped" + (" (spending limit reached)." if over_budget(job, cfg) else "."))

    # ---------------- Step 6: Package ----------------
    job.step = "packaging"
    job.log("Step 6/6 · Packaging the LaTeX project")
    _write(os.path.join(paper_dir, "main.tex"), current_tex)
    bib_path = os.path.join(paper_dir, "references.bib")
    if os.path.exists(bib_path):
        with open(bib_path, encoding="utf-8") as f:
            pruned, removed = prune_bib(f.read(), current_tex)
        if removed:
            _write(bib_path, pruned)
            job.log(f"  Bibliography trimmed to the {pruned.count('@')} papers the text cites ({removed} uncited removed).")
    _write(os.path.join(paper_dir, "TODO.md"), author_todo_md(current_tex, report))
    job.files["main.tex"] = os.path.join(paper_dir, "main.tex")
    job.files["references.bib"] = os.path.join(paper_dir, "references.bib")

    pdf = compile_pdf(paper_dir, job)
    if pdf:
        shutil.copy(pdf, os.path.join(run, "paper.pdf"))
        job.files["paper.pdf"] = os.path.join(run, "paper.pdf")

    zip_path = os.path.join(run, "paper.zip")
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
        for root, _, files in os.walk(paper_dir):
            for name in files:
                if name.endswith((".aux", ".log", ".blg", ".out")):
                    continue
                full = os.path.join(root, name)
                z.write(full, os.path.relpath(full, paper_dir))
    job.files["paper.zip"] = zip_path


def compile_pdf(paper_dir: str, job: Job):
    """Compile with a local LaTeX engine if one exists. Returns the PDF path or None."""
    from . import latex

    eng = latex.engine()
    if not eng:
        job.log("  No LaTeX engine yet, so no PDF was built. Open the paper in the editor and press "
                "'Set up PDF preview', or upload paper.zip to Overleaf (New Project > Upload Project).")
        return None
    job.log(f"  Compiling PDF with {eng}...")
    res = latex.compile_project(paper_dir, "main.tex")
    if res["pdf"]:
        job.log(f"  PDF built in {res['seconds']}s" + (f" ({len(res['errors'])} LaTeX warning(s)/error(s))." if res["errors"] else "."))
        return res["pdf"]
    job.log("  ! PDF compilation failed: " + (res["errors"][0]["message"] if res["errors"] else res["message"])
            + " The LaTeX files are still in paper.zip.")
    return None


def default_cutoff() -> str:
    return datetime.date.today().strftime("%Y-%m")
