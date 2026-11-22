"""The editor backend. Callers use `workspace.<function>`; the code lives in three modules:

- project.py: projects on disk, files, versions, listing and changes made by hand
- chat.py: chat editing, multi-file edit blocks, verified citations, live status
- build.py: compiling, missing-figure placeholders, SyncTeX jumps, .zip export
"""

from .build import build_zip, compile_run, missing_figures, sync_forward, sync_inverse, _make_placeholders
from .chat import (EDITOR_SYSTEM_PROMPT, apply_edits, chat, chat_status, parse_edits, stop,
                   _parse_find, _reply_text, _same_title)
from .project import (PID_RE, WorkspaceError, delete_file, delete_project, is_text, list_files, list_projects,
                      load_state, new_file, pdf_path, raw_path, read_file, rename_file, restore, run_dir, safe_rel,
                      save_file, save_state, set_main, summary, title_of, upload, _busy)

__all__ = [
    "build_zip", "compile_run", "missing_figures", "sync_forward", "sync_inverse", "_make_placeholders",
    "EDITOR_SYSTEM_PROMPT", "apply_edits", "chat", "chat_status", "parse_edits", "stop",
    "_parse_find", "_reply_text", "_same_title",
    "PID_RE", "WorkspaceError", "delete_file", "delete_project", "is_text", "list_files", "list_projects",
    "load_state", "new_file", "pdf_path", "raw_path", "read_file", "rename_file", "restore", "run_dir", "safe_rel",
    "save_file", "save_state", "set_main", "summary", "title_of", "upload", "_busy",
]
