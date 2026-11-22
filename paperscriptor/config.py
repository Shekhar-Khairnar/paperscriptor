"""Paths and settings shared by the web app, the CLI and the pipeline.

Data (your papers in runs/, the LaTeX engine in tools/, and .env) lives in HOME:
- the repository folder when you run from a checkout (or after `pip install -e .`);
- otherwise ~/PaperScriptor, or wherever PAPERSCRIPTOR_HOME points.
"""

import os

PACKAGE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(PACKAGE_DIR, "static")
TEMPLATES_DIR = os.path.join(PACKAGE_DIR, "templates")


def _default_home() -> str:
    if os.environ.get("PAPERSCRIPTOR_HOME"):
        return os.path.abspath(os.environ["PAPERSCRIPTOR_HOME"])
    repo = os.path.dirname(PACKAGE_DIR)
    if os.path.exists(os.path.join(repo, "pyproject.toml")):
        return repo
    return os.path.join(os.path.expanduser("~"), "PaperScriptor")


def load_env_file(path: str):
    """Read KEY=VALUE lines from .env (optional) without overriding real environment variables."""
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


HOME = _default_home()
load_env_file(os.path.join(HOME, ".env"))

RUNS_DIR = os.path.join(HOME, "runs")
TOOLS_DIR = os.path.join(HOME, "tools")
HOST, PORT = "127.0.0.1", int(os.environ.get("PORT", "8000"))

# Local models: write "ollama/<model>" (e.g. ollama/qwen3:14b) in any model field. No API key is needed for them.
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434").rstrip("/")  # 127.0.0.1: "localhost" can stall on Windows
OLLAMA_NUM_CTX = int(os.environ.get("OLLAMA_NUM_CTX", "32768"))  # largest context window to request

DEFAULT_WRITER = "thinkingmachines/inkling-2-small"
DEFAULT_SEARCH = "thinkingmachines/inkling-2-small"  # accurate paper titles, cheap; openai/gpt-oss-20b is cheaper still
TEMPLATE_LABELS = {
    "iclr2025": "ICLR 2025",
    "cvpr2025": "CVPR 2025",
    "nature": "Nature (Springer Nature template)",
}
