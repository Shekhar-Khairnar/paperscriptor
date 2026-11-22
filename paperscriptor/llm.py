"""LLM client: OpenRouter (normal and streaming calls) or a local Ollama server, plus the Job object that tracks a task."""

import base64
import json
import mimetypes
import os
import re
import threading
import time

import requests

from . import config


OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
LOCAL_PREFIX = "ollama/"   # "ollama/qwen3:14b" runs on the local Ollama server instead of OpenRouter
THINK_RE = re.compile(r"<think>.*?</think>\s*", re.S)


def is_local(model: str) -> bool:
    return (model or "").strip().lower().startswith(LOCAL_PREFIX)


def local_name(model: str) -> str:
    return (model or "").strip()[len(LOCAL_PREFIX):]


def visible_text(text: str) -> str:
    """Drop <think>...</think> reasoning that local reasoning models put in their answer (also while streaming)."""
    text = THINK_RE.sub("", text)
    i = text.find("<think>")
    return text[:i] if i != -1 else text


class Cancelled(Exception):
    pass


class LLMError(Exception):
    pass


class ImagesUnsupported(LLMError):
    pass


# --------------------------------------------------------------------------- #
# Job state (read by the web server while the pipeline runs in a thread)
# --------------------------------------------------------------------------- #
class Job:
    def __init__(self, job_id: str, run_dir: str):
        self.id = job_id
        self.run_dir = run_dir
        self.status = "running"  # running | done | error | cancelled
        self.step = "outline"
        self.logs: list[str] = []
        self.error = None
        self.cost = 0.0
        self.files: dict[str, str] = {}  # download name -> absolute path
        self.cancel_requested = False
        self.started_at = time.time()
        self.finished_at = None
        self._lock = threading.Lock()

    def log(self, msg: str):
        line = f"[{time.strftime('%H:%M:%S')}] {msg}"
        with self._lock:
            self.logs.append(line)
        with open(os.path.join(self.run_dir, "log.txt"), "a", encoding="utf-8") as f:
            f.write(line + "\n")

    def add_cost(self, c):
        with self._lock:
            self.cost += float(c or 0)

    def check(self):
        if self.cancel_requested:
            raise Cancelled()

    def snapshot(self, since: int = 0) -> dict:
        with self._lock:
            return {
                "id": self.id,
                "status": self.status,
                "step": self.step,
                "logs": self.logs[since:],
                "log_count": len(self.logs),
                "error": self.error,
                "cost": round(self.cost, 4),
                "files": sorted(self.files.keys()),
                "run_dir": self.run_dir,
                "project": os.path.basename(self.run_dir),
                "started_at": self.started_at,
                "finished_at": self.finished_at,
            }


# --------------------------------------------------------------------------- #
# OpenRouter client
# --------------------------------------------------------------------------- #
def _data_url(path: str) -> str:
    mime = mimetypes.guess_type(path)[0] or "image/png"
    with open(path, "rb") as f:
        return f"data:{mime};base64,{base64.b64encode(f.read()).decode()}"


class LLM:
    def __init__(self, api_key: str, job: Job):
        self.api_key = api_key
        self.job = job

    def chat(self, model, system, user, images=(), temperature=0.7, web=False, history=()) -> str:
        if is_local(model):
            return self._ollama(model, system, user, images=images, temperature=temperature, history=history, web=web)
        if images:
            content = [{"type": "text", "text": user}] + [
                {"type": "image_url", "image_url": {"url": _data_url(p)}} for p in images
            ]
        else:
            content = user
        messages = ([{"role": "system", "content": system}] if system else []) + list(history) + [
            {"role": "user", "content": content}
        ]
        body = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": 32000,
            "usage": {"include": True},
        }
        if web:
            body["plugins"] = [{"id": "web", "max_results": 5}]
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "http://localhost",
            "X-Title": "PaperScriptor",
        }

        last_err = "unknown error"
        for attempt in range(5):
            self.job.check()
            try:
                r = requests.post(OPENROUTER_URL, headers=headers, json=body, timeout=900)
            except requests.RequestException as e:
                last_err = f"network error: {e}"
                time.sleep(3 * (attempt + 1))
                continue

            msg = _error_message(r)
            if r.status_code == 200 and not msg:
                data = r.json()
                choice = (data.get("choices") or [{}])[0]
                text = (choice.get("message") or {}).get("content") or ""
                self.job.add_cost((data.get("usage") or {}).get("cost"))
                if choice.get("finish_reason") == "length":
                    self.job.log("  ! The model hit its output limit; the response may be cut off.")
                if text.strip():
                    return text
                last_err = "empty response from model"
                time.sleep(2)
                continue

            low = (msg or "").lower()
            if r.status_code == 401:
                raise LLMError("OpenRouter rejected the API key (401). Check the key.")
            if r.status_code == 402:
                raise LLMError("OpenRouter says the account is out of credits (402).")
            if images and ("image" in low or "modalit" in low):
                raise ImagesUnsupported(msg)
            if r.status_code in (400, 404) and "max_tokens" in body:
                # Some models reject max_tokens=32000; let OpenRouter pick the default.
                body.pop("max_tokens")
                last_err = msg
                continue
            if r.status_code in (400, 404):
                raise LLMError(f"OpenRouter error {r.status_code} for model '{model}': {msg}")
            last_err = f"HTTP {r.status_code}: {msg}"
            time.sleep(min(30, 3 * 2**attempt))
        raise LLMError(f"OpenRouter call failed after retries: {last_err}")


    def stream_chat(self, model, system, user, temperature=0.7, history=(), on_text=None) -> str:
        """Like chat(), but streams the reply and calls on_text(text_so_far) as it arrives.
        Falls back to chat() (with its retries and error messages) if streaming doesn't work."""
        if is_local(model):
            return self._ollama(model, system, user, temperature=temperature, history=history, on_text=on_text or (lambda t: None))
        messages = ([{"role": "system", "content": system}] if system else []) + list(history) + [
            {"role": "user", "content": user}
        ]
        body = {"model": model, "messages": messages, "temperature": temperature, "max_tokens": 32000,
                "usage": {"include": True}, "stream": True}
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "http://localhost",
            "X-Title": "PaperScriptor",
        }
        fallback = lambda: self.chat(model, system, user, temperature=temperature, history=history)
        for _ in range(2):
            self.job.check()
            try:
                r = requests.post(OPENROUTER_URL, headers=headers, json=body, stream=True, timeout=(30, 300))
            except requests.RequestException:
                return fallback()
            if r.status_code != 200:
                r.close()
                if r.status_code in (400, 404) and "max_tokens" in body:
                    body.pop("max_tokens")
                    continue
                return fallback()  # gives the proper error message (401, 402, ...) or retries
            r.encoding = "utf-8"
            text, finish = "", None
            try:
                for raw in r.iter_lines(decode_unicode=True):
                    if self.job.cancel_requested:
                        raise Cancelled()
                    if not raw or not raw.startswith("data:"):
                        continue  # keep-alive comments such as ": OPENROUTER PROCESSING"
                    data = raw[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data)
                    except ValueError:
                        continue
                    if chunk.get("error"):
                        err = chunk["error"]
                        raise LLMError(f"OpenRouter error: {err.get('message', err) if isinstance(err, dict) else err}")
                    if chunk.get("usage"):
                        self.job.add_cost(chunk["usage"].get("cost"))
                    choice = (chunk.get("choices") or [{}])[0]
                    delta = (choice.get("delta") or {}).get("content") or ""
                    if delta:
                        text += delta
                        if on_text:
                            on_text(text)
                    finish = choice.get("finish_reason") or finish
            except requests.RequestException:
                if not text.strip():
                    return fallback()
            finally:
                r.close()
            if finish == "length":
                self.job.log("  ! The model hit its output limit; the response may be cut off.")
            if text.strip():
                return text
        return fallback()

    # ------------------------------------------------------------------ local models (Ollama)
    def _ollama(self, model, system, user, images=(), temperature=0.7, history=(), web=False, on_text=None) -> str:
        """Call the local Ollama server with its native API, which lets us ask for a context window big enough for
        a whole paper (Ollama's default is small and would silently cut long prompts)."""
        name = local_name(model)
        if web and not getattr(self, "_told_web", False):
            self._told_web = True
            self.job.log("  Web search isn't available for local models; papers are suggested from the model's own knowledge.")
        user_msg = {"role": "user", "content": user}
        if images:
            pics = []
            for p in images:
                with open(p, "rb") as f:
                    pics.append(base64.b64encode(f.read()).decode())
            user_msg["images"] = pics
        messages = ([{"role": "system", "content": system}] if system else []) + [
            {"role": m["role"], "content": m["content"] if isinstance(m["content"], str) else str(m["content"])}
            for m in history] + [user_msg]
        need = int(sum(len(m["content"]) for m in messages) / 3.5) + 8192  # prompt tokens (rough) + room to answer
        if need > config.OLLAMA_NUM_CTX and not getattr(self, "_told_ctx", False):
            self._told_ctx = True
            self.job.log(f"  ! This request needs about {need:,} tokens of context, more than the {config.OLLAMA_NUM_CTX:,} "
                         "allowed for local models, so the model may miss part of it. Set OLLAMA_NUM_CTX higher in .env "
                         "if your computer has the memory.")
        stream = on_text is not None
        body = {"model": name, "messages": messages, "stream": stream,
                "options": {"temperature": temperature, "num_ctx": min(config.OLLAMA_NUM_CTX, -(-need // 4096) * 4096)}}
        url = f"{config.OLLAMA_URL}/api/chat"
        last_err = "unknown error"
        for attempt in range(3):
            self.job.check()
            try:
                r = requests.post(url, json=body, stream=stream, timeout=(10, 3600))
            except requests.ConnectionError:
                raise LLMError(f"Ollama isn't running at {config.OLLAMA_URL}. Start the Ollama app (or run "
                               "`ollama serve`), then try again.")
            except requests.RequestException as e:
                last_err = f"network error: {e}"
                time.sleep(2)
                continue
            if r.status_code != 200:
                msg = _ollama_error(r)
                r.close()
                if r.status_code == 404 or "not found" in msg.lower():
                    raise LLMError(f"The model '{name}' isn't installed in Ollama. Run:  ollama pull {name}")
                if images and "image" in msg.lower():
                    raise ImagesUnsupported(msg)
                last_err = f"HTTP {r.status_code}: {msg}"
                time.sleep(2)
                continue
            text, reason = "", None
            try:
                if stream:
                    r.encoding = "utf-8"
                    for raw in r.iter_lines(decode_unicode=True):
                        if self.job.cancel_requested:
                            raise Cancelled()
                        if not raw:
                            continue
                        try:
                            chunk = json.loads(raw)
                        except ValueError:
                            continue
                        if chunk.get("error"):
                            raise LLMError(f"Ollama error: {chunk['error']}")
                        piece = (chunk.get("message") or {}).get("content") or ""
                        if piece:
                            text += piece
                            on_text(visible_text(text))
                        if chunk.get("done"):
                            reason = chunk.get("done_reason")
                            break
                else:
                    data = r.json()
                    if data.get("error"):
                        raise LLMError(f"Ollama error: {data['error']}")
                    text, reason = (data.get("message") or {}).get("content") or "", data.get("done_reason")
            except requests.RequestException as e:
                last_err = f"connection lost: {e}"
                continue
            finally:
                r.close()
            if reason == "length":
                self.job.log("  ! The local model hit its output limit; the response may be cut off.")
            text = visible_text(text).strip()
            if text:
                return text
            last_err = "empty response from model"
        raise LLMError(f"The local model '{name}' failed: {last_err}")


def _ollama_error(r) -> str:
    try:
        data = r.json()
        return str(data.get("error") or data)[:300]
    except ValueError:
        return r.text[:300]


def _error_message(r) -> str:
    try:
        data = r.json()
    except ValueError:
        return "" if r.status_code == 200 else r.text[:300]
    err = data.get("error") if isinstance(data, dict) else None
    if err:
        return err.get("message", str(err)) if isinstance(err, dict) else str(err)
    return "" if r.status_code == 200 else str(data)[:300]
