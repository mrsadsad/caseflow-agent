"""Loopback-only HTTP workbench. No framework and no browser credentials."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import re
import threading
from urllib.parse import urlsplit

from .store import Conflict, DomainError


MAX_BODY = 16_384
ID = r"[A-Za-z0-9_-]{1,96}"
INDEX = Path(__file__).resolve().parent / "web" / "index.html"


class WorkbenchServer(ThreadingHTTPServer):
    """At most two workers and eight running/queued Agent activities."""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, store, runtime_factory, host, port):
        if host not in {"127.0.0.1", "localhost"}:
            raise ValueError("CaseFlow 只允许绑定 127.0.0.1 或 localhost。")
        self.store = store
        self.runtime_factory = runtime_factory
        self.executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="caseflow")
        self.slots = threading.BoundedSemaphore(8)
        self.active = set()
        self.active_lock = threading.Lock()
        self._executor_closed = False
        try:
            super().__init__((host, port), Handler)
        except BaseException:
            self.executor.shutdown(wait=False, cancel_futures=True)
            raise
        self.authority = f"{host}:{self.server_address[1]}"
        self.origin = f"http://{self.authority}"

    def launch(self, run_id, runtime):
        """Caller owns one slot and has inserted run_id into active."""
        def work():
            try:
                runtime.run(run_id)
            except Exception:
                # The runtime normally persists its own failures. Never emit raw
                # exception text, which could include provider URLs/credentials.
                try:
                    run = self.store.get_run(run_id)
                    if run["status"] in {"ready", "running"}:
                        self.store.add_event(run_id, "worker_error", {
                            "detail": "执行器中断；请检查服务端配置或恢复任务。"
                        })
                        self.store.save_run(run_id, run["revision"], run["state"], "error")
                except Exception:
                    pass

        try:
            future = self.executor.submit(work)
        except BaseException:
            with self.active_lock:
                self.active.discard(run_id)
            self.slots.release()
            raise

        def finished(_future):
            with self.active_lock:
                self.active.discard(run_id)
            self.slots.release()

        future.add_done_callback(finished)

    def server_close(self):
        super().server_close()
        if not self._executor_closed:
            self._executor_closed = True
            self.executor.shutdown(wait=True, cancel_futures=True)


class Handler(BaseHTTPRequestHandler):
    server_version = "CaseFlow/1.0"
    sys_version = ""

    def log_message(self, format, *args):
        # Avoid storing submitted case text or accidental query-string secrets.
        pass

    def _respond(self, status, payload, *, html=False):
        body = payload.encode("utf-8") if html else json.dumps(
            payload, ensure_ascii=False, allow_nan=False
        ).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8" if html else "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        self.wfile.write(body)

    def _error(self, status, message, code="request_error"):
        self._respond(status, {"error": message, "code": code})

    def _boundary(self, write=False):
        hosts = self.headers.get_all("Host", [])
        if hosts != [self.server.authority]:
            self._error(403, "Host 与本地服务地址不匹配。", "invalid_host")
            return False
        if write:
            origins = self.headers.get_all("Origin", [])
            if origins and origins != [self.server.origin]:
                self._error(403, "只接受本工作台页面发起的写入。", "invalid_origin")
                return False
            # Origin-less JSON clients such as curl are allowed. Browser JSON
            # writes from another origin require a preflight, which is denied.
            if self.headers.get("Sec-Fetch-Site", "") == "cross-site":
                self._error(403, "拒绝跨站写入。", "cross_site")
                return False
        return True

    def _body(self):
        if self.headers.get_all("Transfer-Encoding"):
            raise RequestError(400, "不支持 Transfer-Encoding。")
        if self.headers.get_content_type() != "application/json":
            raise RequestError(415, "请求必须使用 application/json。")
        if len(self.headers.get_all("Content-Type", [])) != 1:
            raise RequestError(400, "Content-Type 必须唯一。")
        lengths = self.headers.get_all("Content-Length", [])
        if not lengths:
            raise RequestError(411, "缺少 Content-Length。")
        if len(lengths) != 1 or not re.fullmatch(r"[0-9]+", lengths[0]):
            raise RequestError(400, "无效的 Content-Length。")
        size = int(lengths[0])
        if size > MAX_BODY:
            raise RequestError(413, "请求内容过大。")
        raw = self.rfile.read(size)
        if len(raw) != size:
            raise RequestError(400, "请求内容不完整。")
        try:
            def unique(pairs):
                result = {}
                for key, value in pairs:
                    if key in result:
                        raise ValueError("duplicate key")
                    result[key] = value
                return result
            data = json.loads(raw.decode("utf-8"), object_pairs_hook=unique,
                              parse_constant=lambda _: (_ for _ in ()).throw(ValueError("constant")))
        except (UnicodeDecodeError, ValueError):
            raise RequestError(400, "无效的 JSON。") from None
        if not isinstance(data, dict):
            raise RequestError(400, "JSON 顶层必须是对象。")
        return data

    def setup(self):
        super().setup()
        self.connection.settimeout(10)

    def do_OPTIONS(self):
        self._error(405, "此本地服务不允许跨源预检。")

    def do_GET(self):
        if not self._boundary():
            return
        path = urlsplit(self.path).path
        try:
            if path == "/":
                self._respond(200, INDEX.read_text(encoding="utf-8"), html=True)
            elif path == "/api/bootstrap":
                store = self.server.store
                self._respond(200, {"cases": store.list_cases(), "runs": store.list_runs(),
                                    "proposals": store.proposals(), "outbox": store.outbox(), "ledger": store.ledger()})
            elif match := re.fullmatch(rf"/api/runs/({ID})", path):
                run_id = match[1]
                self._respond(200, {"run": self.server.store.get_run(run_id),
                                    "events": self.server.store.events(run_id),
                                    "proposals": self.server.store.proposals(run_id)})
            else:
                self._error(404, "页面或接口不存在。")
        except DomainError as exc:
            self._error(404, str(exc))
        except Exception:
            self._error(500, "读取失败，请检查本地服务。", "internal_error")

    def do_POST(self):
        if not self._boundary(write=True):
            return
        try:
            body = self._body()
            path = urlsplit(self.path).path
            if path == "/api/runs":
                self._create(body)
            elif match := re.fullmatch(rf"/api/runs/({ID})/resume", path):
                self._keys(body, set())
                self._resume(match[1])
            elif match := re.fullmatch(rf"/api/proposals/({ID})/decision", path):
                self._keys(body, {"approved"})
                if type(body.get("approved")) is not bool:
                    raise RequestError(400, "approved 必须是布尔值。")
                result = self.server.store.approve(match[1], body["approved"])
                self._respond(200, {"result": result})
            elif path in {"/api/dispatch", "/api/dispatch-crash"}:
                self._keys(body, set())
                result = self.server.store.dispatch(fail_after_effect=path.endswith("-crash"))
                self._respond(200, {"result": result})
            else:
                self._error(404, "接口不存在。")
        except RequestError as exc:
            self._error(exc.status, exc.message)
        except Conflict as exc:
            self._error(409, str(exc), "conflict")
        except DomainError as exc:
            self._error(409, str(exc), "domain_error")
        except Exception:
            self._error(500, "操作失败，请检查服务配置并刷新状态。", "internal_error")

    @staticmethod
    def _keys(body, allowed):
        if set(body) - allowed:
            raise RequestError(400, "请求含有不支持的字段。")

    def _runtime(self, mode):
        runtime = self.server.runtime_factory()
        if mode == "live" and getattr(runtime, "provider", None) is None:
            raise RequestError(409, "Live 模式未配置模型。请在启动服务前设置服务端模型环境变量。")
        return runtime

    def _create(self, body):
        self._keys(body, {"case_id", "mode", "max_steps"})
        if type(body.get("case_id")) is not str or not re.fullmatch(ID, body["case_id"]):
            raise RequestError(400, "case_id 必须是有效的案例 ID。")
        if body.get("mode") not in ("demo", "live"):
            raise RequestError(400, "mode 必须为 demo 或 live。")
        steps = body.get("max_steps", 12)
        if type(steps) is not int or not 1 <= steps <= 40:
            raise RequestError(400, "max_steps 必须是 1–40 之间的整数。")
        runtime = self._runtime(body["mode"])
        if not self.server.slots.acquire(blocking=False):
            raise RequestError(429, "当前已有 8 个执行或排队任务，请稍后重试。")
        try:
            run = self.server.store.create_run(body["case_id"], body["mode"], max_steps=steps)
        except BaseException:
            self.server.slots.release()
            raise
        with self.server.active_lock:
            self.server.active.add(run["id"])
        self.server.launch(run["id"], runtime)
        self._respond(202, {"run": run})

    def _resume(self, run_id):
        run = self.server.store.get_run(run_id)
        if run["status"] not in {"interrupted", "error"}:
            raise RequestError(409, "只能恢复 interrupted / error 任务；恢复不会重置执行预算。")
        runtime = self._runtime(run["mode"])
        with self.server.active_lock:
            if run_id in self.server.active:
                raise RequestError(409, "该任务正在执行或排队。")
            if not self.server.slots.acquire(blocking=False):
                raise RequestError(429, "当前已有 8 个执行或排队任务，请稍后重试。")
            self.server.active.add(run_id)
        self.server.launch(run_id, runtime)
        self._respond(202, {"run": run})


class RequestError(Exception):
    def __init__(self, status, message):
        self.status = status
        self.message = message
        super().__init__(message)


def make_server(store, runtime_factory, host="127.0.0.1", port=8780):
    return WorkbenchServer(store, runtime_factory, host, port)


def serve(store, runtime_factory, host="127.0.0.1", port=8780):
    with make_server(store, runtime_factory, host, port) as server:
        print(f"CaseFlow 工作台：{server.origin}")
        print("仅本机可访问。Demo 是固定策略；Live 才会调用已配置的模型。")
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            print("\n正在关闭本地工作台…")
