"""Single-owner control of the two local heavy MLX workloads."""

from __future__ import annotations

import fcntl
import json
import os
import signal
import socket
import subprocess
import threading
import time
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path

import config
import memory
import catalog
import progress
import ownership
from store import Store
from runtime import ModelRuntime, PORT as MODEL_PORT
from prompt_enhancer import PromptEnhancer
from transport import open_stream
from external_chat import ExternalChatMixin
from rooms import Rooms
from telemetry import Telemetry
import web_search
import rag


class ManagerError(Exception):
    pass


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def run(args: list[str], timeout: float = 15) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout,
                          check=False)


class Manager(ExternalChatMixin):
    def __init__(self):
        config.DATA_ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
        config.OUTPUT_ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._singleton = open(config.DATA_ROOT / "manager.lock", "a+")
        try:
            fcntl.flock(self._singleton, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ManagerError("UZEL is already running") from exc
        self._heavy = open(config.DATA_ROOT / "heavy.lock", "a+")
        self._heavy_held = False
        self._mu = threading.RLock()
        self._telemetry = Telemetry()
        self._active_chat = 0
        self._chat_requests = {}
        self._cancelled_chats = {}
        self._enhancer = PromptEnhancer(self)
        self.rag = rag.Connection()
        self.store = Store()
        self._job = self._load_job()
        self._worker = None
        self._session_worker = None
        if self._job and self._job.get('state') == 'running':
            self._worker = ownership.recover(self._job.get('owner'), config.DATA_ROOT / f"image-{self._job['id']}-progress.json")
            if not self._worker:
                try:
                    stage = json.loads((config.DATA_ROOT / f"image-{self._job['id']}-progress.json").read_text()).get('stage')
                except (OSError,ValueError):
                    stage = None
                self._job['state'] = 'cancelled' if self._job.get('cancel_reason') or stage == 'cancelled' else 'done' if stage == 'done' and Path(self._job['output']).is_file() else 'failed' if stage == 'failed' else 'interrupted'
                self._job['finished_at'] = now()
                self._save_job()
        self._mode = "idle"
        self._runtime = ModelRuntime()
        self._catalog = None
        self._delete_plans = {}
        if self._worker:
            self._mode = 'image'
        self._reconcile()
        if self._job:
            self.store.image(self._job)
        if self._worker:
            threading.Thread(target=self._watch_job, args=(self._worker,), daemon=True).start()
            if self._job.get('cancel_reason'):
                self._interrupt_worker(self._worker)
                threading.Thread(target=self._finish_cancel, args=(self._worker,), daemon=True).start()
        self.rooms = Rooms(self, self.store)

    def room_binding(self):
        """Metadata only: rooms pin the already selected engine, never load another."""
        with self._mu:
            self._reconcile()
            if self._mode == 'external_chat':
                return {'mode': 'external_chat', 'model': config.EXTERNAL_MODEL_ID}
            if self._mode == 'model' and self._runtime.state == 'ready':
                return {'mode': 'model', 'model': (self._runtime.model or {}).get('id')}
            return None

    def _prompt_guard(self):
        enhancer = getattr(self, '_enhancer', None)
        if enhancer and enhancer.busy() and not enhancer.owned():
            raise ManagerError('Prompt expansion is running; wait or cancel it first')

    def _load_job(self) -> dict | None:
        try:
            value = json.loads((config.DATA_ROOT / "last_job.json").read_text())
            if not isinstance(value, dict) or not value.get("id"):
                return None
            return value
        except (OSError, ValueError):
            return None

    def _save_job(self) -> None:
        if self._job is None:
            return
        path = config.DATA_ROOT / "last_job.json"
        temp = path.with_suffix(".tmp")
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(self._job, stream)
        os.replace(temp, path)
        self.store.image(self._job)

    def _lock_heavy(self) -> None:
        if self._heavy_held:
            return
        try:
            fcntl.flock(self._heavy, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ManagerError("Heavy-memory lock held by another process") from exc
        self._heavy_held = True

    def _unlock_heavy(self) -> None:
        if self._heavy_held:
            fcntl.flock(self._heavy, fcntl.LOCK_UN)
            self._heavy_held = False

    def _observe_lock(self):
        try:
            self._lock_heavy()
        except ManagerError:
            self._mode = 'conflict'

    def _reconcile(self) -> None:
        runtime = getattr(self, "_runtime", None)
        if runtime and runtime.running():
            self._mode = "conflict" if self._external_listener() or memory.image_processes() else "model"
            self._observe_lock()
            return
        if memory.listener_pid(MODEL_PORT):
            self._mode = "conflict"
            return
        listener = self._external_listener()
        external_chat = listener and listener == memory.launch_agent_pid(config.EXTERNAL_LABEL)
        worker_pid = (self._worker.pid if self._worker and
                      self._worker.poll() is None else None)
        images = [item for item in memory.image_processes()
                  if item["pid"] != worker_pid]
        if listener and not external_chat:
            self._mode = "conflict"
            self._observe_lock()
        elif external_chat and images:
            self._mode = "conflict"
            self._observe_lock()
        elif external_chat and worker_pid:
            self._mode = "conflict"
            self._observe_lock()
        elif external_chat:
            self._mode = "conflict" if self._mode == "image" else "external_chat"
            self._observe_lock()
        elif images:
            self._mode = "external_image"
            self._observe_lock()
        elif worker_pid:
            self._mode = "image"
            self._observe_lock()
        elif self._mode not in ("image",):
            self._mode = "idle"
            self._unlock_heavy()

    def _proxy_stats(self) -> dict | None:
        if not config.EXTERNAL_ENABLED or not config.STATS_PATH:
            return None
        try:
            with urllib.request.urlopen(config.PROXY_ENDPOINT + config.STATS_PATH, timeout=1) as r:
                return json.load(r)
        except (OSError, ValueError):
            return None

    def status(self) -> dict:
        metrics = self._telemetry.snapshot(self._collect_metrics)
        with self._mu:
            self._reconcile()
            image_pid = self._worker.pid if self._worker and self._worker.poll() is None else None
            return {**metrics,
                "mode": self._mode, "heavy_lock": self._heavy_held,
                "workload_lock_path": str(config.DATA_ROOT / "heavy.lock"),
                "external_chat_model_available": config.EXTERNAL_ENABLED and config.EXTERNAL_MODEL.is_dir(),
                "external_chat_sessions": bool(config.SESSION_PATH and config.SESSION_HEADER),
                "image_snapshot_available": config.image_snapshot() is not None,
                "image": {**metrics["image"], "pid": image_pid},
                "active_chat_requests": self._active_chat,
                "prompt_enhancement": self._enhancer.snapshot(),
                "job": dict(self._job) if self._job else None, "at": now(),
                "runtime": self._runtime.status(), "reserved": self._external_reservation(),
            }

    def _collect_metrics(self):
        listener = self._external_listener()
        pid = listener if listener == memory.launch_agent_pid(config.EXTERNAL_LABEL) else None
        with self._mu:
            image_pid = self._worker.pid if self._worker and self._worker.poll() is None else None
        return {"external_chat": {"pid": pid, "port": config.EXTERNAL_PORT,
                    "other_listener_pid": listener if listener != pid else None,
                    **memory.vmmap_summary(pid)},
                "image": {"processes": memory.image_processes(), **memory.vmmap_summary(image_pid)},
                "system": memory.system_memory(), "proxy": self._proxy_stats()}

    def _external_reservation(self) -> bool:
        if self._heavy_held:
            return False
        with open(config.DATA_ROOT / "heavy.lock", "a+") as probe:
            try:
                fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(probe, fcntl.LOCK_UN)
                return False
            except BlockingIOError:
                return True

    def image_progress(self):
        with self._mu:
            job = dict(self._job) if self._job else None
        if not job:
            return {"job": None}
        path = config.DATA_ROOT / f"image-{job['id']}-progress.json"
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            data = progress.from_log(self.logs("image"))
        if job["state"] != "running":
            data["stage"] = job["state"]
        job["progress"] = data
        return {"job": job}

    def model_catalog(self, refresh=False):
        with self._mu:
            cached = self._catalog
        if cached is not None and not refresh:
            return cached
        discovered = catalog.discover()
        with self._mu:
            self._catalog = discovered
            return discovered

    def _deletion_allowed(self):
        self._prompt_guard()
        self._reconcile()
        if (self._mode != "idle" or self._active_chat or self._external_reservation()
                or self._worker and self._worker.poll() is None):
            raise ManagerError("Unload models and wait for active jobs or benchmarks before deleting")

    def preview_delete(self, model_id):
        with self._mu:
            self._deletion_allowed()
            model = next((m for m in self.model_catalog(refresh=True)["models"] if m["id"] == model_id), None)
            if model is None:
                raise ManagerError("Model is no longer available")
            path = Path(model["path"]).resolve()
            # A cached model owns its entire HF repo, including snapshots and blobs.
            target = path.parent.parent if path.parent.name == "snapshots" and path.parent.parent.name.startswith("models--") else path
            if not target.is_dir() or target == Path.home() or not (path / "config.json").is_file() and model["kind"] != "image":
                raise ManagerError("Model deletion target could not be verified")
            size = 0
            for root, dirs, files in os.walk(target, followlinks=False):
                for name in files:
                    file = Path(root) / name
                    if not file.is_symlink():
                        size += file.stat().st_size
            inode = target.stat().st_ino
            token = uuid.uuid4().hex
            plan = {"token": token, "model": model_id, "name": model["name"], "path": str(target),
                    "bytes": size, "all_snapshots": target != path, "inode": inode, "expires": time.monotonic() + 300}
            self._delete_plans = {token: plan}
            return {k: v for k, v in plan.items() if k not in ("inode", "expires")}

    def trash_model(self, token, confirmation):
        with self._mu:
            self._deletion_allowed()
            plan = self._delete_plans.get(token)
            if plan is None or plan["expires"] < time.monotonic():
                raise ManagerError("Deletion preview expired; review the target again")
            if confirmation != plan["name"]:
                raise ManagerError("Model name confirmation does not match")
            target = Path(plan["path"])
            if target.is_symlink() or not target.is_dir() or target.stat().st_ino != plan["inode"]:
                raise ManagerError("Model path changed; review the target again")
            self._lock_heavy()
            try:
                if target.parts[:2] == ("/", "Volumes") and len(target.parts) > 3:
                    trash = Path(*target.parts[:3]) / ".Trashes" / str(os.getuid())
                else:
                    trash = Path.home() / ".Trash"
                trash.mkdir(parents=True, exist_ok=True, mode=0o700)
                if trash.stat().st_dev != target.stat().st_dev:
                    raise ManagerError("No same-volume Trash is available; model was not moved")
                destination = trash / f"{target.name}-{uuid.uuid4().hex[:8]}"
                target.rename(destination)
                self._delete_plans.clear()
                self._catalog = None
                return {"state": "trashed", "name": plan["name"], "trash_path": str(destination),
                        "note": "Restore the folder to its original path to use the model again"}
            finally:
                self._unlock_heavy()

    def start_model(self, model_id, backend="auto"):
        with self._mu:
            self._prompt_guard()
        listing = self.model_catalog(refresh=True)
        model = next((m for m in listing["models"] if m["id"] == model_id), None)
        if model is None or not model["available"]:
            raise ManagerError("Local model or compatible runtime is unavailable")
        if model["kind"] == "image":
            return self.set_mode("image")
        if backend == "auto":
            backend = model["backends"][0]
        if backend not in model["backends"]:
            raise ManagerError("This backend does not support the selected model")
        if model.get("external_chat") and backend == "omlx":
            return self.set_mode("external_chat")
        with self._mu:
            self._prompt_guard()
            self._reconcile()
            if self._mode in ("conflict", "external_image") or self._active_chat:
                raise ManagerError("Another workload or chat request is active")
            if self._worker and self._worker.poll() is None:
                raise ManagerError("Image generation is running")
            if self._runtime.running() and memory.established_connections(MODEL_PORT):
                raise ManagerError("Selected model has active requests")
            self._lock_heavy()
            try:
                self._stop_external()
                self._runtime.stop()
                executable = next(r["executable"] for r in listing["runtimes"] if r["id"] == backend)
                result = self._runtime.start(model, backend, executable)
                self._mode = "model"
                return result
            except (RuntimeError, OSError) as exc:
                self._reconcile()
                raise ManagerError(str(exc)) from exc

    @staticmethod
    def validate_chat(spec):
        if not isinstance(spec.get("web_search", False), bool):
            raise ManagerError("Invalid web search setting")
        if spec.get('rag') is not None:
            rag.validate_selection(spec['rag'])
        messages = spec.get("messages")
        max_tokens = spec.get("max_tokens", 1024)
        temperature = spec.get("temperature", 0.7)
        if (not isinstance(messages, list) or not 1 <= len(messages) <= 100
                or any(not isinstance(m, dict) or m.get("role") not in ("system", "user", "assistant")
                       or not isinstance(m.get("content"), str) for m in messages)
                or sum(len(m["content"]) for m in messages) > 100000
                or not isinstance(max_tokens, int) or isinstance(max_tokens, bool) or not 1 <= max_tokens <= 8192
                or not isinstance(temperature, (int, float)) or not 0 <= temperature <= 2):
            raise ManagerError("Invalid chat messages or generation settings")
        return messages, max_tokens, temperature

    def stream_chat(self, spec):
        messages, max_tokens, temperature = self.validate_chat(spec)
        request_id = spec.get('request_id') or uuid.uuid4().hex
        if not isinstance(request_id, str) or not 1 <= len(request_id) <= 100:
            raise ManagerError('Invalid chat request identifier')
        with self._mu:
            self._prompt_guard()
            self._reconcile()
            mode = self._mode
            if mode not in ("external_chat", "model"):
                raise ManagerError("Start a chat model first")
            if spec.get('room_binding') and spec['room_binding'] != self.room_binding():
                raise ManagerError('Room model changed; add the selected model as a new participant')
            if not hasattr(self, '_chat_requests'):
                self._chat_requests = {}
            if getattr(self, '_cancelled_chats', {}).pop(request_id, 0) > time.monotonic():
                raise ManagerError('Chat cancelled')
            if request_id in self._chat_requests:
                raise ManagerError('Chat request already active')
            control = {'cancelled': threading.Event(), 'socket': None, 'search_process': None}
            self._chat_requests[request_id] = control
            self._active_chat += 1
        try:
            def track_socket(sock):
                with self._mu:
                    control['socket'] = sock
                    cancelled = control['cancelled'].is_set()
                if cancelled and sock is not None:
                    try:
                        sock.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass
            if spec.get('rag'):
                client = self.rag.client(control['cancelled'], track_socket)
                flow = rag.retrieve(messages, rag.validate_selection(spec['rag']), client,
                    lambda prepared, tools: self._rag_model_request(mode, prepared, tools, min(max_tokens, 2048), temperature, track_socket))
                while True:
                    try:
                        event = next(flow)
                    except StopIteration as finished:
                        messages = finished.value
                        break
                    yield self._chat_event(event)
            if spec.get('web_search'):
                query = web_search.query_for(messages)
                yield self._chat_event({'type': 'web_search', 'phase': 'searching', 'query': query})
                def track_search(process):
                    with self._mu:
                        control['search_process'] = process
                        cancelled = control['cancelled'].is_set()
                    if process is not None and cancelled and process.poll() is None:
                        try:
                            process.terminate()
                        except OSError:
                            pass
                result = web_search.search(query, control['cancelled'], track_search)
                messages = web_search.with_sources(messages, result)
                if not spec.get('rag'):
                    self.validate_chat(dict(spec, messages=messages))
                yield self._chat_event(dict(result, type='web_search', phase='ready'))
            if control['cancelled'].is_set():
                raise ManagerError('Chat cancelled')
            if mode == "model":
                response = self._runtime.open_chat(messages, max_tokens, temperature, stream=True, on_socket=track_socket)
            else:
                self._ensure_proxy()
                payload = {"model": config.EXTERNAL_MODEL_ID, "messages": messages, "max_tokens": max_tokens,
                           "temperature": temperature, "stream": True}
                headers = {"Content-Type": "application/json"}
                if spec.get("session") and config.SESSION_HEADER:
                    headers[config.SESSION_HEADER] = str(spec["session"])
                request = urllib.request.Request(config.PROXY_ENDPOINT + "/v1/chat/completions",
                    data=json.dumps(payload).encode(), headers=headers, method="POST")
                response = open_stream(request, track_socket)
            with response:
                if control['cancelled'].is_set():
                    raise ManagerError('Chat cancelled')
                for line in response:
                    if control['cancelled'].is_set():
                        raise ManagerError('Chat cancelled')
                    yield line
        finally:
            with self._mu:
                self._chat_requests.pop(request_id, None)
            self.chat_leave()

    def _rag_model_request(self, mode, messages, tools, max_tokens, temperature, track_socket):
        try:
            if mode == 'model':
                response = self._runtime.open_chat(messages, max_tokens, temperature, on_socket=track_socket, tools=tools)
            else:
                self._ensure_proxy()
                payload = {'model': config.EXTERNAL_MODEL_ID, 'messages': messages, 'max_tokens': max_tokens,
                           'temperature': temperature, 'tools': tools, 'tool_choice': 'auto', 'stream': False}
                request = urllib.request.Request(config.PROXY_ENDPOINT + '/v1/chat/completions', data=json.dumps(payload).encode(),
                                                headers={'Content-Type': 'application/json'}, method='POST')
                response = open_stream(request, track_socket)
            with response:
                raw = response.read(rag.MAX_REPLY + 1)
            if len(raw) > rag.MAX_REPLY:
                raise rag.RagError('Ответ модели превышает допустимый размер')
            value = json.loads(raw)
            reply = value['choices'][0]['message']
            if not isinstance(reply, dict):
                raise ValueError()
            return reply
        except (KeyError, IndexError, ValueError):
            raise rag.RagError('Модель не вернула корректный ответ с tools') from None
        except urllib.error.HTTPError:
            raise rag.RagError('Движок отклонил вызов tools. Проверьте поддержку инструментов моделью') from None
        finally:
            track_socket(None)

    def cancel_chat(self, request_id):
        with self._mu:
            control = getattr(self, '_chat_requests', {}).get(request_id)
            if control is None:
                if not hasattr(self, '_cancelled_chats'):
                    self._cancelled_chats = {}
                self._cancelled_chats = {k:v for k,v in self._cancelled_chats.items() if v > time.monotonic()}
                if len(self._cancelled_chats) < 100:
                    self._cancelled_chats[request_id] = time.monotonic() + 60
                return {'cancelled': False}
            control['cancelled'].set()
            sock = control['socket']
            search_process = control.get('search_process')
        # Shutdown unblocks a read even when the model emits no more tokens.
        # close() alone can wait on the buffered reader's lock.
        if search_process is not None and search_process.poll() is None:
            try:
                search_process.terminate()
            except OSError:
                pass
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        return {'cancelled': True}

    @staticmethod
    def _chat_event(value):
        return b'data: ' + json.dumps(value, ensure_ascii=False).encode() + b'\n\n'

    def chat(self, spec):
        if spec.get('web_search') or spec.get('rag'):
            rag_result = None
            content, reasoning, result, finish = '', '', None, 'stop'
            for line in self.stream_chat(spec):
                if not line.startswith(b'data: ') or line.strip() == b'data: [DONE]':
                    continue
                chunk = json.loads(line[6:])
                if chunk.get('error'):
                    raise ManagerError(str(chunk['error']))
                if chunk.get('type') == 'web_search' and chunk.get('phase') == 'ready':
                    result = chunk
                if chunk.get('type') == 'rag' and chunk.get('phase') == 'ready':
                    rag_result = chunk
                for choice in chunk.get('choices', []):
                    delta = choice.get('delta', {})
                    content += delta.get('content') or ''
                    reasoning += delta.get('reasoning_content') or delta.get('reasoning') or ''
                    finish = choice.get('finish_reason') or finish
            return {'choices': [{'index': 0, 'message': {'role': 'assistant', 'content': content, 'reasoning_content': reasoning}, 'finish_reason': finish}], 'web_search': result, 'rag': rag_result}
        messages, max_tokens, temperature = self.validate_chat(spec)
        with self._mu:
            self._prompt_guard()
            self._reconcile()
            mode = self._mode
            if mode not in ("external_chat", "model"):
                raise ManagerError("Start a chat model first")
            self._active_chat += 1
        try:
            if mode == "model":
                return self._runtime.chat(messages, max_tokens, temperature)
            self._ensure_proxy()
            payload = {"model": config.EXTERNAL_MODEL_ID, "messages": messages, "max_tokens": max_tokens,
                       "temperature": temperature, "stream": False}
            session = spec.get("session")
            headers = {"Content-Type": "application/json"}
            if session and config.SESSION_HEADER:
                headers[config.SESSION_HEADER] = str(session)
            request = urllib.request.Request(config.PROXY_ENDPOINT + "/v1/chat/completions",
                data=json.dumps(payload).encode(), headers=headers, method="POST")
            with urllib.request.urlopen(request, timeout=300) as response:
                return json.load(response)
        except RuntimeError as exc:
            raise ManagerError(str(exc)) from exc
        finally:
            self.chat_leave()

    def chat_enter(self) -> None:
        with self._mu:
            self._prompt_guard()
            self._reconcile()
            if self._mode != "external_chat":
                raise ManagerError("External model is unavailable")
            self._ensure_proxy()
            self._active_chat += 1

    def chat_leave(self) -> None:
        with self._mu:
            self._active_chat = max(0, self._active_chat - 1)

    def set_mode(self, target: str) -> dict:
        if target not in ("idle", "external_chat", "image"):
            raise ManagerError("Mode must be idle, external_chat, or image")
        with self._mu:
            self._prompt_guard()
            self._reconcile()
            if self._mode in ("conflict", "external_image"):
                raise ManagerError("External heavy process detected; resolve it manually")
            if self._worker and self._worker.poll() is None:
                raise ManagerError("Image generation is running")
            runtime = getattr(self, "_runtime", None)
            if runtime and runtime.running():
                if self._active_chat or memory.established_connections(MODEL_PORT):
                    raise ManagerError("Selected model has active requests")
                runtime.stop()
            if target == "external_chat":
                self._lock_heavy()
                try:
                    self._start_external()
                except Exception:
                    self._reconcile()
                    raise
                self._mode = "external_chat"
            elif target == "image":
                if config.image_snapshot() is None:
                    raise ManagerError("Local Qwen-Image snapshot is incomplete")
                self._lock_heavy()
                self._stop_external()
                self._mode = "image"
            else:
                self._stop_external()
                self._mode = "idle"
                self._unlock_heavy()
            return {"mode": self._mode, "heavy_lock": self._heavy_held}

    def generate(self, spec: dict) -> dict:
        with self._mu:
            self._prompt_guard()
            self._reconcile()
            if self._mode != "image" or not self._heavy_held:
                raise ManagerError("Switch to image mode first")
            persistent = bool(spec.get('_session')) and self._enhancer.owned()
            reuse = (persistent and self._worker is getattr(self, '_session_worker', None)
                     and self._worker is not None and self._worker.poll() is None
                     and self._job and self._job['state'] == 'done')
            if self._worker and self._worker.poll() is None and not reuse:
                raise ManagerError("An image job is already running")
            if self._external_listener():
                raise ManagerError("External model is still running")
            snapshot = config.image_snapshot()
            if snapshot is None or not config.IMAGE_PYTHON.is_file():
                raise ManagerError("Local image model or runtime unavailable")
            system = memory.system_memory()
            pressure = system["pressure_free_percent"]
            if pressure is None or pressure < 20:
                raise ManagerError("Memory pressure is unknown or too high for image generation")
            prompt = spec.get("prompt")
            if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 30000:
                raise ManagerError("Prompt must contain 1-30000 characters")
            width, height = spec.get("width", 1024), spec.get("height", 1024)
            steps, seed = spec.get("steps", 30), spec.get("seed", 42)
            cache = spec.get("cache", "off")
            if (not all(isinstance(x, int) and not isinstance(x, bool) for x in
                        (width, height, steps, seed)) or
                width not in (512, 768, 1024, 1152) or height not in (512, 768, 1024, 1152) or
                not 1 <= steps <= 60 or not 0 <= seed <= 2**32-1 or
                cache not in ("off", "balanced")):
                raise ManagerError("Invalid image dimensions, steps, seed, or cache")
            job_id = uuid.uuid4().hex[:12]
            output = config.OUTPUT_ROOT / f"{job_id}.png"
            log_path = config.DATA_ROOT / f"image-{job_id}.log"
            payload = {"prompt": prompt, "output": str(output), "width": width,
                       "height": height, "steps": steps, "seed": seed,
                       "cache": cache, "model_path": str(snapshot)}
            payload["progress"] = str(config.DATA_ROOT / f"image-{job_id}-progress.json")
            payload.update(session=persistent, acceleration=config.IMAGE_ACCELERATION)
            env = dict(os.environ, HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1")
            if reuse:
                worker = self._worker
                log_path = Path(self._job['log'])
            else:
                with open(log_path, "w", encoding="utf-8") as log:
                    worker = subprocess.Popen([str(config.IMAGE_PYTHON),
                                               str(config.ROOT / "image_worker.py")],
                                              stdin=subprocess.PIPE, stdout=log,
                                              stderr=subprocess.STDOUT, text=True,
                                              env=env, start_new_session=True)
                self._worker = worker
                if persistent:
                    self._session_worker = worker
            self._job = {"id": job_id, "state": "running", "started_at": now(),
                         "steps": steps, "owner": ownership.record(worker),
                         "parameters": {k: payload[k] for k in ("prompt","width","height","steps","seed","cache","acceleration")},
                         "finished_at": None, "output": str(output),
                         "log": str(log_path), "exit_code": None,
                         "peak_footprint_bytes": None, "cancel_reason": None,
                         "swap_before_bytes": system["swap_used_bytes"],
                         "swap_after_bytes": None,
                         "min_pressure_free_percent": pressure}
            series = spec.get('series')
            if (isinstance(series, dict) and isinstance(series.get('id'), str)
                    and len(series['id']) == 12 and all(c in '0123456789abcdef' for c in series['id'])
                    and all(type(series.get(k)) is int for k in ('index','count'))
                    and 1 <= series['index'] <= series['count'] <= 20):
                self._job['parameters']['series'] = {k: series[k] for k in ('id','index','count')}
            expansion = spec.get('prompt_expansion')
            if isinstance(expansion, dict):
                self._job['parameters']['prompt_expansion'] = {
                    k: expansion[k] for k in ('original_prompt','model')
                    if isinstance(expansion.get(k), str) and len(expansion[k]) <= 30000}
            self._save_job()
            assert worker.stdin is not None
            try:
                worker.stdin.write(json.dumps(payload) + '\n')
                worker.stdin.flush()
                if not persistent:
                    worker.stdin.close()
            except OSError as exc:
                self._job['error'] = 'Could not send image job to worker: ' + str(exc)
                self._job['cancel_reason'] = 'Worker input failed'
                self._interrupt_worker(worker)
                threading.Thread(target=self._finish_cancel, args=(worker,), daemon=True).start()
            self._save_job()
            threading.Thread(target=self._watch_job, args=(worker, job_id, persistent), daemon=True).start()
            return dict(self._job)

    def _watch_job(self, worker: subprocess.Popen, job_id=None, persistent=False) -> None:
        terminal = None
        while worker.poll() is None:
            if persistent:
                try:
                    terminal = json.loads((config.DATA_ROOT / f'image-{job_id}-progress.json').read_text()).get('stage')
                except (OSError,ValueError):
                    terminal = None
                if terminal in ('done','failed','cancelled'):
                    break
            footprint = memory.vmmap_summary(worker.pid)["footprint_bytes"]
            pressure = memory.system_memory()["pressure_free_percent"]
            with self._mu:
                owns_job = self._job and self._worker is worker and (job_id is None or self._job['id'] == job_id)
                if owns_job and footprint is not None:
                    old = self._job["peak_footprint_bytes"] or 0
                    self._job["peak_footprint_bytes"] = max(old, footprint)
                if owns_job and pressure is not None:
                    old = self._job["min_pressure_free_percent"]
                    self._job["min_pressure_free_percent"] = min(old, pressure)
            time.sleep(.5 if persistent else 1)
        code = (0 if terminal == 'done' else 130 if terminal == 'cancelled' else 1) if persistent and terminal else worker.wait()
        swap_after = memory.system_memory()["swap_used_bytes"]
        with self._mu:
            if self._job and self._worker is worker and (job_id is None or self._job['id'] == job_id):
                try:
                    recorded = json.loads((config.DATA_ROOT / f"image-{self._job['id']}-progress.json").read_text())
                    self._job['progress'] = recorded
                    if isinstance(recorded.get('error'), str):
                        self._job['error'] = recorded['error'][:2000]
                except (OSError, ValueError, AttributeError):
                    pass
                self._job["exit_code"] = code
                self._job["finished_at"] = now()
                self._job["swap_after_bytes"] = swap_after
                if self._job.get('error'):
                    self._job['state'] = 'failed'
                elif self._job['cancel_reason'] or code == 130:
                    self._job['state'] = 'cancelled'
                elif code == 0 and Path(self._job['output']).is_file():
                    self._job['state'] = 'done'
                else:
                    self._job['state'] = 'failed'
                self._save_job()
                if not persistent or worker.poll() is not None:
                    self._worker = None

    def close_image_session(self):
        with self._mu:
            self._prompt_guard()
            worker = self._session_worker
            if worker is None:
                return
            if worker.stdin and not worker.stdin.closed:
                worker.stdin.close()
        try:
            worker.wait(timeout=30)
        except subprocess.TimeoutExpired:
            ownership.stop(worker, first=signal.SIGINT, grace=5)
        with self._mu:
            if self._session_worker is worker:
                self._session_worker = None
            if self._worker is worker:
                self._worker = None

    def _interrupt_worker(self, worker: subprocess.Popen) -> None:
        ownership.signal_group(worker, signal.SIGINT)

    def _finish_cancel(self, worker):
        try:
            ownership.stop(worker, first=None)
        except (OSError, RuntimeError) as exc:
            with self._mu:
                if self._worker is worker and self._job:
                    self._job['error'] = 'Cancellation failed: ' + str(exc)
                    self._save_job()

    def cancel(self) -> dict:
        with self._mu:
            if not self._worker or self._worker.poll() is not None:
                raise ManagerError("No running image job")
            assert self._job is not None
            if self._job.get('cancel_reason'):
                return dict(self._job)
            self._job["cancel_reason"] = "User requested"
            self._save_job()
            self._interrupt_worker(self._worker)
            threading.Thread(target=self._finish_cancel, args=(self._worker,), daemon=True).start()
            return dict(self._job)

    def logs(self, service: str) -> str:
        if service == "model":
            path = config.DATA_ROOT / "model.log"
        elif service == "external_chat":
            path = config.EXTERNAL_LOG
        elif service == "image" and self._job:
            path = Path(self._job["log"])
        elif service == "manager":
            path = config.DATA_ROOT / "stderr.log"
        else:
            raise ManagerError("Unknown log service")
        try:
            with open(path, "rb") as stream:
                stream.seek(max(0, path.stat().st_size - 32768))
                return stream.read().decode("utf-8", "replace")[-32768:]
        except OSError:
            return ""
