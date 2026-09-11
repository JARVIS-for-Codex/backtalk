"""Minimal asynchronous client for the Codex App Server JSONL protocol."""

import asyncio
import contextlib
import json
import os
import shutil
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path


class CodexAppServerError(RuntimeError):
    pass


def _codex_executable():
    """Find Codex even when a Windows Desktop launcher has a bare PATH."""
    found = shutil.which("codex")
    if found:
        return found
    if os.name == "nt":
        local = os.environ.get("LOCALAPPDATA")
        if local:
            root = Path(local) / "OpenAI" / "Codex" / "bin"
            candidates = list(root.glob("*/codex.exe"))
            if candidates:
                return str(max(candidates, key=lambda p: p.stat().st_mtime))
    return "codex"


class CodexAppServer:
    def __init__(self, approval_handler: Callable | None = None,
                 config_overrides=None):
        self._approval_handler = approval_handler
        self._config_overrides = config_overrides or []
        self._process = None
        self._reader_task = None
        self._stderr_task = None
        self._pending = {}
        self._items = {}
        self._events = asyncio.Queue()
        self._request_id = 0
        self._write_lock = asyncio.Lock()
        self._stderr = []

    async def start(self):
        if self._process is not None:
            return
        command = [_codex_executable(), "app-server"]
        for override in self._config_overrides:
            command.extend(("--config", override))
        self._process = await asyncio.create_subprocess_exec(
            *command, stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        self._reader_task = asyncio.create_task(self._read_stdout())
        self._stderr_task = asyncio.create_task(self._read_stderr())
        await asyncio.wait_for(self.request("initialize", {
            "clientInfo": {"name": "backtalk", "title": "Backtalk",
                           "version": "1.0.0"},
            "capabilities": {"experimentalApi": True},
        }), 15)
        await self.notify("initialized", {})

    async def close(self):
        process = self._process
        if process is None:
            return
        if process.stdin:
            process.stdin.close()
            with contextlib.suppress(BrokenPipeError, ConnectionResetError):
                await process.stdin.wait_closed()
        try:
            await asyncio.wait_for(process.wait(), 3)
        except TimeoutError:
            process.terminate()
            await process.wait()
        for task in (self._reader_task, self._stderr_task):
            if task:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        self._process = None

    async def request(self, method, params=None):
        self._request_id += 1
        request_id = self._request_id
        future = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        await self._send({"method": method, "id": request_id,
                          "params": params or {}})
        return await future

    async def notify(self, method, params=None):
        await self._send({"method": method, "params": params or {}})

    async def events(self) -> AsyncIterator[dict]:
        while self._process is not None:
            yield await self._events.get()

    async def _send(self, message):
        if self._process is None or self._process.stdin is None:
            raise CodexAppServerError("Codex App Server is not running")
        data = (json.dumps(message, separators=(",", ":")) + "\n").encode()
        async with self._write_lock:
            self._process.stdin.write(data)
            await self._process.stdin.drain()

    async def _read_stdout(self):
        assert self._process and self._process.stdout
        while line := await self._process.stdout.readline():
            try:
                message = json.loads(line)
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
            request_id = message.get("id")
            if request_id in self._pending and "method" not in message:
                future = self._pending.pop(request_id)
                if "error" in message:
                    error = message["error"]
                    future.set_exception(CodexAppServerError(
                        str(error.get("message", error))))
                else:
                    future.set_result(message.get("result", {}))
            elif request_id is not None and "method" in message:
                await self._answer_server_request(message)
            elif "method" in message:
                params = message.get("params", {})
                item = params.get("item", {})
                item_id = item.get("id")
                if message["method"] == "item/started" and item_id:
                    self._items[item_id] = item
                elif message["method"] == "item/completed" and item_id:
                    self._items.pop(item_id, None)
                await self._events.put(message)

    async def _answer_server_request(self, message):
        decision = "decline"
        if self._approval_handler:
            try:
                params = dict(message.get("params", {}))
                item = self._items.get(params.get("itemId"))
                if item:
                    params["item"] = item
                decision = await self._approval_handler(
                    message.get("method", ""), params)
            except Exception:
                decision = "decline"
        await self._send({"id": message["id"],
                          "result": {"decision": decision}})

    async def _read_stderr(self):
        assert self._process and self._process.stderr
        while line := await self._process.stderr.readline():
            self._stderr.append(line.decode(errors="replace"))
            if len(self._stderr) > 100:
                del self._stderr[:50]
