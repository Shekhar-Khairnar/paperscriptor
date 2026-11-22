"""Chat editing: the prompt, multi-file edit blocks, verified citations (FIND PAPERS) and live status."""

import difflib
import json
import os
import posixpath
import re
import time

from .. import llm, pipeline, references
from .project import (
    HISTORY_TURNS,
    MAX_CONTEXT_CHARS,
    MAX_INPUT_CHARS,
    WorkspaceError,
    _abs,
    _busy,
    _busy_guard,
    _commit,
    _diff,
    _ext,
    _read,
    _snapshot,
    _write,
    is_text,
    list_files,
    load_state,
    safe_rel,
    save_state,
    summary,
)


# --------------------------------------------------------------------------- #
# Chat editing
# --------------------------------------------------------------------------- #
EDITOR_SYSTEM_PROMPT = r"""You are an expert academic writing assistant working inside a LaTeX editor with a live PDF preview.
The project can have several files (main paper, section files, supplementary material, bibliography).
The author asks you to change the project or asks questions about it. You edit the files directly.

Rules:
- Change only what the author asks for. Leave everything else exactly as it is.
- Never invent results, numbers, datasets, experiments or quotes. Numbers must come from the experimental log
  or already appear in the project. If a request needs information you do not have, say so instead of making it up.
- Cite only with the bibliography keys listed below. Never invent keys or BibTeX entries yourself.
- If the author wants citations or related work and the right papers are not in the bibliography, do not edit
  yet. Reply with one short sentence, then list the papers you are confident exist, with exact titles and years:

<<<<<<< FIND PAPERS
Attention Is All You Need (2017)
Exact title of another paper (year)
>>>>>>> END

  The app checks each title on Semantic Scholar and OpenAlex, adds the ones that really exist to the
  bibliography, and then asks you to make the edit with their keys. List at most 12 papers.
- Keep every document compiling: balanced braces and environments, escape special characters (% & $ # _), and
  keep the preamble and \documentclass unless asked to change them.
- Follow the venue guidelines if they are given.

Reply format:
1. First, a short plain-text reply to the author (1-4 sentences) that says what you changed, or answers the
   question. No LaTeX code in this part.
2. Then, if files must change, one or more edit blocks. Write the file's path on its own line, then the block:

path/to/file.tex
<<<<<<< SEARCH
exact lines copied from that file
=======
the new lines
>>>>>>> REPLACE

   - Use the exact path from the project file list. You may edit several files in one reply.
   - SEARCH must match the file exactly (copy it character for character, including indentation).
   - Include enough lines in SEARCH to be unique, but keep each block small. Use several blocks for several places.
   - To delete text, leave the part after ======= empty. To insert text, put the neighbouring line in SEARCH and
     repeat it in the replacement together with the new text.
   - To create a new file, or to replace the whole content of a file, leave SEARCH empty and put the full file
     content after =======. If you create a new .tex file that should appear in the paper, also add the matching
     \input{...} line to the right file.
   - Only edit files whose content you were shown.
3. If the author only asked a question, send no edit blocks.
4. Files the author attached to a message are reference material (notes, results, reviews). Use their content,
   but do not add them to the project unless asked.

About the app (so you can answer questions about it correctly):
- The app compiles the main file to PDF by itself after every change; the PDF shows next to the source.
- The author downloads the compiled PDF with the "PDF" button at the top right, and the whole LaTeX project with
  the ".zip" button next to it. If the PDF is missing or out of date, they press "Compile".
- You can only edit text files. You cannot create PDFs, images or other binary files, and you must not pretend to.
- You can create or edit any text file the author asks for: notes (notes.txt), to-do lists, a response to reviewers,
  a cover letter. Use an edit block with an empty SEARCH to create one. New files appear in the Files pane, and
  every file can be downloaded with its download button.
- Files such as TODO.md and notes.txt hold the author's open items and reminders. When the author asks about
  finishing, submitting or the final version, list the open items from those files first.
"""

BLOCK_RE = re.compile(r"<{5,9}[ \t]*SEARCH[^\n]*\n(.*?)\n?={5,9}[^\n]*\n(.*?)\n?>{5,9}[ \t]*REPLACE[^\n]*", re.S)
PATH_LINE_RE = re.compile(r"[\w\-./ ]+\.[A-Za-z0-9]{1,8}")
BIB_ENTRY_RE = re.compile(r"@(\w+)\s*\{\s*([^,\s]+)\s*,(.*?)(?=\n@|\Z)", re.S)
BIBITEM_RE = re.compile(r"\\bibitem(?:\[[^\]]*\])?\{([^}]+)\}")
INPUT_RE = re.compile(r"\\(?:input|include|subfile)\s*\{([^}]+)\}")
FIND_RE = re.compile(r"<{5,9}[ \t]*FIND PAPERS[^\n]*\n(.*?)\n?>{5,9}[ \t]*END[^\n]*", re.S)
BIBFILE_RE = re.compile(r"\\(?:bibliography|addbibresource)\s*(?:\[[^\]]*\])?\s*\{([^}]+)\}")
MAX_ATTACH_CHARS = 200_000


def _bib_entries(files: dict) -> list[tuple[str, str]]:
    entries = []
    for rel, text in files.items():
        if rel.lower().endswith(".bib"):
            for typ, key, body in BIB_ENTRY_RE.findall(text):
                if typ.lower() in ("comment", "string", "preamble"):
                    continue
                m = re.search(r"\btitle\s*=\s*[{\"](.+?)[}\"]\s*,?\s*$", body, re.M | re.I)
                entries.append((key.strip(), re.sub(r"[{}]", "", m.group(1)).strip()[:160] if m else ""))
        elif rel.lower().endswith(".tex"):
            entries += [(k.strip(), "") for k in BIBITEM_RE.findall(text)]
    return entries


def _cited(files: dict) -> set:
    keys = set()
    for rel, text in files.items():
        if rel.lower().endswith(".tex"):
            for m in pipeline.CITE_RE.finditer(text):
                keys.update(k.strip() for k in m.group(4).split(",") if k.strip())
    return keys


def _ordered_for_context(state: dict, files: dict, open_path: str | None) -> list[str]:
    """Open file, main file, files it \\input's, then the other .tex and notes files. .sty/.cls/.bib only if open."""
    order = []

    def add(p):
        if p in files and p not in order:
            order.append(p)

    add(open_path)
    add(state["main"])
    queue = [state["main"]]
    while queue:
        cur = queue.pop(0)
        for ref in INPUT_RE.findall(files.get(cur, "")):
            for cand in (ref, ref + ".tex"):
                cand = posixpath.normpath(cand.strip())
                if cand in files and cand not in order:
                    add(cand)
                    queue.append(cand)
    for p in sorted(files, key=str.lower):
        if _ext(p) in (".tex", ".md", ".txt", ".csv", ".tsv"):
            add(p)
    return order


def _context(run: str, state: dict, files: dict, open_path: str | None) -> tuple[str, set]:
    """Build the prompt context. Returns (text, set of files shown in full)."""
    parts = []
    settings = os.path.join(run, "inputs", "settings.json")
    if os.path.exists(settings):
        try:
            tpl = json.load(open(settings, encoding="utf-8")).get("template_dir", "")
            guide = _read(os.path.join(tpl, "guidelines.md"), 20_000)
            if guide.strip():
                parts.append(f"--- VENUE GUIDELINES ---\n{guide}")
        except (ValueError, OSError):
            pass
    idea = _read(os.path.join(run, "inputs", "idea.md"), MAX_INPUT_CHARS)
    log = _read(os.path.join(run, "inputs", "experimental_log.md"), MAX_INPUT_CHARS)
    if idea.strip():
        parts.append(f"--- AUTHOR'S IDEA AND METHOD NOTES ---\n{idea}")
    if log.strip():
        parts.append(f"--- EXPERIMENTAL LOG (the only source of numbers) ---\n{log}")
    if not idea.strip() and not log.strip():
        parts.append("--- NOTE ---\nNo separate notes or experimental log are available. Use only facts and numbers "
                     "already in the project files.")
    bib = _bib_entries(files)
    parts.append("--- BIBLIOGRAPHY KEYS YOU MAY CITE ---\n" +
                 ("\n".join(f"{k}: {t}" if t else k for k, t in bib[:500]) or "(none)"))

    shown, budget, blocks = set(), MAX_CONTEXT_CHARS, []
    for rel in _ordered_for_context(state, files, open_path):
        text = files[rel]
        if len(text) > budget:
            continue
        budget -= len(text)
        shown.add(rel)
        tag = " (MAIN FILE, compiled)" if rel == state["main"] else " (open in the editor)" if rel == open_path else ""
        blocks.append(f"=== FILE: {rel}{tag} ===\n{text}\n=== END OF FILE: {rel} ===")
    listing = []
    for f in list_files(run):
        note = "shown below" if f["path"] in shown else "not shown"
        listing.append(f"{f['path']}  ({f['kind']}, {f['size']:,} bytes, {note})")
    parts.append("--- PROJECT FILES ---\n" + "\n".join(listing[:400]))
    parts.append("--- FILE CONTENTS ---\n" + "\n\n".join(blocks))
    return "\n\n".join(parts), shown


def _apply_one(text: str, search: str, replace: str) -> str | None:
    if search in text:
        i = text.index(search)
        return text[:i] + replace + text[i + len(search):]
    pattern = r"\s+".join(re.escape(tok) for tok in search.split())  # same text, different whitespace
    m = re.search(pattern, text)
    if m:
        return text[:m.start()] + replace + text[m.end():]
    return None


def parse_edits(text: str, default_path: str) -> list[tuple[str, str, str]]:
    """Return [(path, search, replace)] from a reply. The path is the line just before each block."""
    edits, last_path, prev_end = [], default_path, 0
    for m in BLOCK_RE.finditer(text):
        between = [l for l in text[prev_end:m.start()].splitlines() if l.strip()]
        if between:
            hint = between[-1].strip().strip("`*#").strip()
            hint = re.sub(r"^(file|path|filename)\s*[:=]?\s*", "", hint, flags=re.I).strip().rstrip(":").strip("`")
            if PATH_LINE_RE.fullmatch(hint):
                last_path = hint
        edits.append((last_path, m.group(1), m.group(2)))
        prev_end = m.end()
    return edits


def apply_edits(files: dict, edits: list, shown: set) -> tuple[dict, int, list]:
    files, applied, failed = dict(files), 0, []
    for path, search, replace in edits:
        try:
            rel = safe_rel(path)
        except WorkspaceError:
            failed.append((path, search, replace, "invalid path"))
            continue
        if not is_text(rel):
            failed.append((rel, search, replace, "not a text file"))
            continue
        if not search.strip():
            files[rel] = replace if replace.endswith("\n") or not replace else replace + "\n"
            applied += 1
            continue
        if rel not in files:
            failed.append((rel, search, replace, "file not found"))
            continue
        new = _apply_one(files[rel], search, replace)
        if new is None:
            failed.append((rel, search, replace, "text not found"))
        else:
            files[rel], applied = new, applied + 1
    return files, applied, failed


def _reply_text(text: str) -> str:
    text = FIND_RE.sub("", text)
    if "<<<<<<< FIND" in text:  # block still streaming
        text = text[:text.index("<<<<<<< FIND")]
    first = BLOCK_RE.search(text)
    if not first and "<<<<<<<" in text:  # an edit block that is still being written
        text = text[:text.index("<<<<<<<")] + "\n<<<<<<< SEARCH\n\n=======\n\n>>>>>>> REPLACE"
        first = BLOCK_RE.search(text)
    cut = first.start() if first else len(text)
    fence = text.find("```")
    if fence != -1:
        cut = min(cut, fence)
    reply_lines = text[:cut].rstrip().splitlines()
    if first and reply_lines and PATH_LINE_RE.fullmatch(reply_lines[-1].strip().strip("`*#:").strip()):
        reply_lines = reply_lines[:-1]  # drop the path line that belongs to the first block
    reply = "\n".join(reply_lines).strip()
    if not reply:
        tail = BLOCK_RE.sub("", text)
        reply = re.sub(r"```latex.*?(```|\Z)", "", tail, flags=re.S).strip()[:1500]
    return reply


def _history(state: dict) -> list[dict]:
    msgs = [m for m in state["messages"] if m["role"] in ("user", "assistant") and not m.get("error")]
    out = []
    for m in msgs[-HISTORY_TURNS:]:
        text = m["text"]
        if m["role"] == "assistant" and m.get("version"):
            text += f"\n(Edits applied to {', '.join(m.get('diff', {}).get('files', [])) or 'the project'}.)"
        out.append({"role": m["role"], "content": text})
    return out


def chat(run: str, message: str, selection: str, open_path: str, model: str, api_key: str,
         attachments: list | None = None) -> dict:
    pid = os.path.basename(run)
    message = (message or "").strip()
    if not message:
        raise WorkspaceError("Type a request first.")
    if not api_key and not llm.is_local(model):
        raise WorkspaceError("Add your OpenRouter API key on the main page or in the .env file, "
                             "or use a local model (ollama/<model>).")
    with _busy_guard:
        if pid in _busy:
            raise WorkspaceError("Still working on the previous request.", 409)
        job = llm.Job(pid, run)
        job.stage, job.partial = "Thinking…", ""
        _busy[pid] = job
    try:
        return _chat(run, job, message, (selection or "").strip(), open_path, model, api_key, attachments or [])
    finally:
        with _busy_guard:
            _busy.pop(pid, None)


def stop(run: str):
    job = _busy.get(os.path.basename(run))
    if job:
        job.cancel_requested = True


def chat_status(run: str) -> dict:
    """What the model is doing right now, for the live reply in the chat."""
    job = _busy.get(os.path.basename(run))
    if not job:
        return {"busy": False}
    text = job.partial or ""
    stage = job.stage
    if stage.startswith("Writing") and "<<<<<<< FIND" in text:
        stage = "Choosing papers to look up…"
    elif stage.startswith("Writing") and "<<<<<<<" in text:
        hints = [l.strip().strip("`*#:").strip() for l in text[:text.rfind("<<<<<<<")].splitlines() if l.strip()]
        path = next((h for h in reversed(hints) if PATH_LINE_RE.fullmatch(h)), None)
        stage = f"Editing {path}…" if path else "Editing…"
    return {"busy": True, "stage": stage, "reply": (_reply_text(text) if text else "")[:4000], "cost": round(job.cost, 5)}


def _target_bib(state: dict, files: dict) -> str:
    """The .bib file the main document uses (or the first .bib, or references.bib)."""
    base = posixpath.dirname(state["main"])
    for group in BIBFILE_RE.findall(files.get(state["main"], "")):
        for name in group.split(","):
            name = name.strip()
            if not name:
                continue
            rel = posixpath.normpath(posixpath.join(base, name if name.endswith(".bib") else name + ".bib"))
            try:
                return safe_rel(rel)
            except WorkspaceError:
                continue
    bibs = sorted(p for p in files if p.lower().endswith(".bib"))
    return bibs[0] if bibs else "references.bib"


def _parse_find(text: str) -> list[tuple[str, int | None]]:
    out = []
    for block in FIND_RE.findall(text):
        for line in block.splitlines():
            line = re.sub(r"^\s*(?:[-*\u2022]|\d+[.)])\s*", "", line).strip().strip('"')
            if len(line) < 6:
                continue
            m = re.search(r"[(,]\s*((?:19|20)\d{2})\s*\)?\s*$", line)
            year = int(m.group(1)) if m else None
            title = line[:m.start()].strip().strip('",') if m else line
            if title:
                out.append((title, year))
    return out[:12]


def _same_title(asked: str, found: str) -> bool:
    """Stricter than the pipeline's match: a chat citation must be the paper that was asked for."""
    return difflib.SequenceMatcher(None, references._norm(asked), references._norm(found)).ratio() >= 0.85


def _find_papers(run: str, state: dict, files: dict, wanted: list, job) -> dict:
    """Verify suggested titles on Semantic Scholar / OpenAlex and build BibTeX for the real ones."""
    cfg = {"cutoff": pipeline.default_cutoff(),
           "s2_api_key": os.environ.get("SEMANTIC_SCHOLAR_API_KEY", ""),
           "openalex_api_key": os.environ.get("OPENALEX_API_KEY", "")}
    vstate = {"s2_down": False, "s2_streak": 0, "oa_down": False}
    existing = _bib_entries(files)
    keys = {k for k, _ in existing}
    known = {references._norm(t): k for k, t in existing if t}
    added, already, missing = [], [], []
    for i, (title, year) in enumerate(wanted, 1):
        job.check()
        job.stage = f"Checking papers on Semantic Scholar and OpenAlex… {i}/{len(wanted)}"
        if vstate["s2_down"] and vstate["oa_down"]:
            missing.append(title)
            continue
        paper, _source = references.verify_paper(title, year, cfg, vstate, job)
        if not paper or not _same_title(title, paper["title"]):
            missing.append(title)
            continue
        norm = references._norm(paper["title"])
        if norm in known:
            already.append((known[norm], paper["title"]))
            continue
        key = base = references._make_key(paper.get("authors") or [], paper.get("year"), paper["title"])
        suffix = ord("a")
        while key in keys:
            key, suffix = f"{base}{chr(suffix)}", suffix + 1
        keys.add(key)
        known[norm] = key
        journal = paper.get("journal") or {}
        added.append({
            "citation_key": key, "title": paper["title"],
            "authors": [a["name"] for a in paper.get("authors") or [] if a.get("name")],
            "venue": paper.get("venue") or "arXiv", "year": paper.get("year") or year,
            "abstract": (paper.get("abstract") or "")[:600],
            "journal": journal.get("name"), "volume": journal.get("volume"), "pages": journal.get("pages"),
        })
    bib = _target_bib(state, files)
    text = None
    if added:
        cur = files.get(bib, "")
        sep = "" if not cur or cur.endswith("\n\n") else ("\n" if cur.endswith("\n") else "\n\n")
        text = cur + sep + references.make_bibtex(added)
    return {"bib": bib, "bib_text": text, "added": added, "already": already, "missing": missing}


def _chat(run, job, message, selection, open_path, model, api_key, attachments) -> dict:
    state = load_state(run)
    old = _snapshot(run)
    try:
        open_path = safe_rel(open_path) if open_path else None
    except WorkspaceError:
        open_path = None
    history = _history(state)
    user_msg = {"role": "user", "text": message, "time": time.time()}
    if selection:
        user_msg["selection"] = selection[:4000]
        if open_path:
            user_msg["selection_file"] = open_path
    att_text, att_names, budget = [], [], MAX_ATTACH_CHARS
    for a in attachments:
        name, text = str(a.get("name") or "attachment")[:120], str(a.get("text") or "")
        if not text.strip() or budget <= 0:
            continue
        text = text[:budget]
        budget -= len(text)
        att_names.append(name)
        att_text.append(f"=== ATTACHED: {name} ===\n{text}\n=== END OF ATTACHMENT: {name} ===")
    if att_names:
        user_msg["attachments"] = att_names

    context, shown = _context(run, state, old, open_path)
    prompt = context + "\n\n"
    if att_text:
        prompt += "--- FILES THE AUTHOR ATTACHED TO THIS MESSAGE ---\n" + "\n\n".join(att_text) + "\n\n"
    if selection:
        prompt += f"--- THE AUTHOR HAS SELECTED THIS PASSAGE{f' IN {open_path}' if open_path else ''} ---\n{selection[:4000]}\n\n"
    prompt += f"--- AUTHOR'S REQUEST ---\n{message}\n"

    client = llm.LLM(api_key, job)

    def ask(user_text, hist, stage="Writing…", temperature=0.3):
        job.stage, job.partial = stage, ""
        return client.stream_chat(model, EDITOR_SYSTEM_PROMPT, user_text, temperature=temperature, history=hist,
                               on_text=lambda t: setattr(job, "partial", t))

    reply_msg = {"role": "assistant", "time": time.time()}
    notes = []
    try:
        text = ask(prompt, history)
        last_prompt, last_hist = prompt, history
        new, failed, first_reply = dict(old), [], ""
        wanted = _parse_find(text)
        if wanted and not BLOCK_RE.search(text):
            first_reply = _reply_text(text)
            found = _find_papers(run, state, old, wanted, job)
            if found["added"]:
                new[found["bib"]] = found["bib_text"]
                shown = shown | {found["bib"]}
                notes.append(f"Added {len(found['added'])} verified reference(s) to {found['bib']}: " +
                             "; ".join(f"{p['title']} ({p['year']})" for p in found["added"]) + ".")
            if found["missing"]:
                notes.append(f"Couldn't find {len(found['missing'])} suggested paper(s) on Semantic Scholar or "
                             "OpenAlex, so they weren't cited: " + "; ".join(found["missing"]) + ".")
            lines = [f"{p['citation_key']}: {p['title']} ({p['year']}). {', '.join(p['authors'][:3])}"
                     f"{' et al.' if len(p['authors']) > 3 else ''}. Abstract: {p['abstract']}" for p in found["added"]]
            lines += [f"{k}: {t} (already in the bibliography)" for k, t in found["already"]]
            follow = ("Paper check results.\n"
                      + ("Verified, available keys:\n" + "\n".join(lines) + "\n"
                         if lines else "None of the suggested papers could be verified.\n")
                      + (("Not found, do not cite: " + "; ".join(found["missing"]) + "\n") if found["missing"] else "")
                      + ("" if BIBFILE_RE.search(old.get(state["main"], "")) or not found["added"] else
                         f"The main file has no bibliography command yet; add \\bibliographystyle{{plain}} and "
                         f"\\bibliography{{{found['bib'][:-4]}}} before \\end{{document}}.\n")
                      + "\nNow make the change the author asked for, with edit blocks, citing only these keys and keys "
                        "already in the bibliography. Do not edit the .bib file; the new entries are already there. "
                        "If nothing could be verified, say so and make no edits.")
            last_hist = history + [{"role": "user", "content": prompt}, {"role": "assistant", "content": text}]
            last_prompt = follow
            text = ask(follow, last_hist)
        edits = parse_edits(text, state["main"])
        if edits:
            job.stage = "Applying edits…"
            new, _, failed = apply_edits(new, edits, shown)
            retryable = [f for f in failed if f[3] in ("text not found", "file not found")]
            failed = [f for f in failed if f not in retryable]
            if retryable:  # one retry for edits whose SEARCH text did not match
                involved = sorted({f[0] for f in retryable if f[0] in new})
                current = "\n\n".join(f"=== FILE: {p} ===\n{new[p]}\n=== END OF FILE: {p} ===" for p in involved)
                retry = (f"{len(retryable)} of your edit blocks could not be applied, so they were skipped:\n\n" +
                         "\n\n".join(f"{p} ({why}):\n{srch}" for p, srch, _, why in retryable) +
                         f"\n\nHere are the affected files as they are now, with your other edits applied:\n\n{current}\n\n"
                         "Send ONLY the failed edits again, each with its file path line, and SEARCH text copied exactly.")
                hist2 = last_hist + [{"role": "user", "content": last_prompt}, {"role": "assistant", "content": text}]
                text2 = ask(retry, hist2, stage="Fixing edits that didn't match…", temperature=0.2)
                new, _, failed2 = apply_edits(new, parse_edits(text2, state["main"]), shown)
                failed += failed2
        elif not wanted:
            full = pipeline.extract_latex(text)
            if full:
                new[state["main"]] = full
        if job.cancel_requested:
            raise llm.Cancelled()

        reply = _reply_text(text) or first_reply
        if failed:
            notes.append(f"{len(failed)} edit(s) couldn't be placed and were skipped.")
        main = state["main"]
        if new.get(main) != old.get(main) and ("\\documentclass" not in (new.get(main) or "")
                                               or "\\end{document}" not in (new.get(main) or "")):
            new[main] = old[main]
            notes.append(f"A change to {main} would have removed \\documentclass or \\end{{document}}, so it was skipped.")
        changed = [p for p in new if new[p] != old.get(p)]
        if changed:
            valid = {k for k, _ in _bib_entries(new)} | _cited(old)
            for p in changed:
                if p.lower().endswith(".tex"):
                    new[p] = pipeline.fix_citations(new[p], valid, job)
            for p in changed:
                _write(_abs(run, p), new[p])
            diff = _diff(old, {**old, **{p: new[p] for p in changed}})
            reply_msg["prev_version"] = state["version"]
            reply_msg["version"] = _commit(run, state, message)
            reply_msg["diff"] = diff
        reply_msg["text"] = "\n\n".join(x for x in [reply or ("Done." if changed else "(The model sent an empty reply.)")] + notes)
    except llm.Cancelled:
        reply_msg.update(text="Stopped. No changes were made.", error=True)
    except llm.LLMError as e:
        reply_msg.update(text=str(e), error=True)
    reply_msg["cost"] = round(job.cost, 5)
    state["cost"] = state.get("cost", 0.0) + job.cost
    state["messages"] += [user_msg, reply_msg]
    save_state(run, state)
    return summary(run)
