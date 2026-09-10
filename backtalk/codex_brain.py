"""Codex App Server backend for Backtalk's existing WarmBrain contract."""

import asyncio
import os
import re
from datetime import datetime
from types import SimpleNamespace

from backtalk import signals
from backtalk.codex_app_server import CodexAppServer
from backtalk.config import CFG, DISCIPLINE
from backtalk.permission_profile import (PROFILE_NAME, config_override,
                                         load_trusted_realms)
from backtalk.vlog import log

_SENTENCE_END = re.compile(r"(?<=[.!?])\s")
SESSION_FILE = os.path.join(CFG["signals_dir"], ".backtalk_session")


class PermissionResultAllow:
    def __init__(self, behavior="allow"):
        self.behavior = behavior


class PermissionResultDeny:
    def __init__(self, behavior="deny", message="", interrupt=False):
        self.behavior = behavior
        self.message = message
        self.interrupt = interrupt


class WarmBrain:
    """Keep Backtalk's public brain shape while speaking to Codex."""

    def __init__(self, model=None, can_use_tool=None, resume_id=None):
        self.model = model or CFG["model"]
        self._can_use_tool = can_use_tool
        self._resume_id = resume_id
        self._trusted_realms = load_trusted_realms()
        overrides = []
        if self._trusted_realms:
            overrides = [config_override(self._trusted_realms),
                         f'default_permissions="{PROFILE_NAME}"']
            if os.name == "nt":
                overrides.append('windows.sandbox="unelevated"')
        self._server = CodexAppServer(self._approval_request, overrides)
        self._thread_id = None
        self._turn_id = None
        self._permission_mode = CFG["permission_mode"]
        self._effort = str(CFG.get("effort") or "")
        self._turn_usage = {}
        self.session = {"turns": 0, "out_tokens": 0, "in_tokens": 0,
                        "cost": 0.0}

    def _thread_params(self):
        mode = self._permission_mode
        approval = "never" if mode == "bypassPermissions" else "on-request"
        params = {
            "cwd": os.path.abspath(CFG["agent_dir"]),
            "model": self.model,
            "approvalPolicy": approval,
            "serviceName": "backtalk",
        }
        if mode == "bypassPermissions":
            params["sandbox"] = "danger-full-access"
        elif self._trusted_realms:
            params["permissions"] = PROFILE_NAME
        else:
            params["sandbox"] = "read-only"
        return params

    async def start(self):
        await self._server.start()
        resume, self._resume_id = self._resume_id, None
        if resume:
            try:
                result = await self._server.request("thread/resume", {
                    "threadId": resume, **self._thread_params()})
                self._thread_id = result["thread"]["id"]
                log(f"[brain] resumed Codex thread {resume[:8]}")
                return
            except Exception as exc:
                log(f"[brain] resume failed ({str(exc)[:80]}), starting fresh")
        result = await self._server.request("thread/start", self._thread_params())
        self._thread_id = result["thread"]["id"]
        self._remember_session()

    async def set_permission_mode(self, backtalk_mode):
        # Codex applies this per turn; no reconnect or lost conversation.
        self._permission_mode = backtalk_mode

    async def context_usage(self):
        return None

    def _remember_session(self):
        if not CFG.get("resume_last_session") or not self._thread_id:
            return
        try:
            with open(SESSION_FILE, "w", encoding="utf-8") as stream:
                stream.write(self._thread_id)
        except OSError:
            pass

    async def _pull_rate_limits(self):
        if not CFG.get("show_usage"):
            return
        try:
            usage = await asyncio.wait_for(
                self._server.request("account/rateLimits/read"), 5)
            limits = usage.get("rateLimits") or {}
            for name, key in (("five_hour", "primary"),
                              ("seven_day", "secondary")):
                window = limits.get(key)
                if window:
                    used = window.get("usedPercent")
                    signals.set_rate_limit(
                        name, used / 100 if used is not None else None,
                        window.get("resetsAt"))
        except Exception:
            pass

    async def _approval_request(self, method, params):
        if self._permission_mode == "bypassPermissions":
            return "accept"
        if not self._can_use_tool:
            return "decline"
        if "commandExecution" in method:
            item = params.get("item") or {}
            command = (params.get("command") or item.get("command") or
                       params.get("cmd") or "")
            if isinstance(command, list):
                command = " ".join(map(str, command))
            tool, tool_input = "Bash", {"command": str(command)}
        elif "fileChange" in method:
            changes = (params.get("item") or {}).get("changes") or []
            path = changes[0].get("path") if changes else None
            tool, tool_input = "Edit", {
                "file_path": path or params.get("grantRoot") or "a file"}
        else:
            tool, tool_input = "Codex", params
        ctx = SimpleNamespace(display_name=tool,
                              description=str(params.get("reason") or ""))
        answer = await self._can_use_tool(tool, tool_input, ctx)
        return "accept" if getattr(answer, "behavior", "deny") == "allow" \
            else "decline"

    async def command(self, cmd):
        if cmd == "/clear":
            result = await self._server.request("thread/start",
                                                self._thread_params())
            self._thread_id = result["thread"]["id"]
            self._remember_session()
            return "cleared"
        if cmd == "/compact":
            await self._server.request("thread/compact/start",
                                       {"threadId": self._thread_id})
            saw_compaction = False
            async for event in self._server.events():
                data = event.get("params", {})
                if data.get("threadId") not in (None, self._thread_id):
                    continue
                if event.get("method") == "item/completed":
                    item = data.get("item", {})
                    saw_compaction = (saw_compaction or
                                      item.get("type") == "contextCompaction")
                elif event.get("method") == "turn/completed" and saw_compaction:
                    break
            return "compacting"
        if cmd.startswith("/model "):
            self.model = cmd.split(None, 1)[1]
            return f"model set to {self.model}"
        if cmd.startswith("/effort "):
            self._effort = cmd.split(None, 1)[1]
            return f"effort set to {self._effort}"
        return "error: unknown command"

    async def interrupt(self):
        if self._thread_id and self._turn_id:
            await self._server.request("turn/interrupt", {
                "threadId": self._thread_id, "turnId": self._turn_id})

    async def reset_turn(self, timeout=8.0):
        if self._turn_id:
            try:
                await asyncio.wait_for(self.interrupt(), timeout)
            except Exception:
                pass
            self._turn_id = None

    async def stop(self):
        await self._server.close()

    async def ask_stream(self, utterance):
        params = {
            "threadId": self._thread_id,
            "input": [{"type": "text", "text":
                       f"{DISCIPLINE}\n\nThe person says: {utterance}"}],
            "cwd": os.path.abspath(CFG["agent_dir"]),
            "model": self.model,
            "approvalPolicy": ("never" if self._permission_mode ==
                               "bypassPermissions" else "on-request"),
        }
        if self._effort:
            params["effort"] = self._effort
        result = await self._server.request("turn/start", params)
        self._turn_id = result.get("turn", {}).get("id")
        buf = ""
        async for event in self._server.events():
            method = event.get("method")
            data = event.get("params", {})
            if data.get("threadId") not in (None, self._thread_id):
                continue
            if method == "item/agentMessage/delta":
                buf += str(data.get("delta", ""))
                while True:
                    match = _SENTENCE_END.search(buf)
                    if not match:
                        break
                    sentence, buf = buf[:match.end()].strip(), buf[match.end():]
                    if sentence:
                        yield sentence
            elif method == "thread/tokenUsage/updated":
                usage = (data.get("tokenUsage") or {}).get("last") or {}
                if usage:
                    self._turn_usage = usage
            elif method == "turn/completed":
                turn = data.get("turn", {})
                if self._turn_id and turn.get("id") != self._turn_id:
                    continue
                self._turn_id = None
                self.session["turns"] += 1
                self.session["in_tokens"] += int(
                    self._turn_usage.get("inputTokens") or 0)
                self.session["out_tokens"] += int(
                    self._turn_usage.get("outputTokens") or 0)
                self._turn_usage = {}
                self._remember_session()
                await self._pull_rate_limits()
                break
        if buf.strip():
            yield buf.strip()
