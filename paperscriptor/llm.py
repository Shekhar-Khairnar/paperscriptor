"""OpenRouter client (normal and streaming calls) and the Job object that tracks a running task."""

import base64
import json
import mimetypes
import os
import threading
import time

import requests


OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"


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


def _error_message(r) -> str:
    try:
        data = r.json()
    except ValueError:
        return "" if r.status_code == 200 else r.text[:300]
    err = data.get("error") if isinstance(data, dict) else None
    if err:
        return err.get("message", str(err)) if isinstance(err, dict) else str(err)
    return "" if r.status_code == 200 else str(data)[:300]
