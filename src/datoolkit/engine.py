"""Core application logic shared by every frontend (GUI now, TUI later).

Design rule: the LLM side of this class never touches sessions. The only path from a model
proposal to a terminal is a technician action in the frontend (Run/Insert), which writes to
the PTY like any keystroke.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import os
import platform
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Callable

from . import config as config_mod
from . import creds
from .case import Case, fence
from .config import Config, Host, Provider
from .llm import prompts
from .llm.client import SENSITIVITY_TIERS, LLMClient, detect_tier, is_private_mode
from .llm.private_mode import (Enclave, PrivateModeClient, PrivateModeError, list_private_models,
                               offers_private_mode, relay_url)
from .queue import Queue
from .safety.inject import suspicious
from .safety.redact import redact
from .safety.truncate import head_tail
from .sessions.askpass import AskpassBridge
from .sessions.manager import SessionManager
from .sessions.ssh import ssh_argv, target_label

MAX_TOOL_ROUNDS = 3
PROMPT_TIMEOUT = 300


class UserError(Exception):
    """An error to show the technician as-is."""


def _local_os() -> str:
    try:
        for line in Path("/etc/os-release").read_text().splitlines():
            if line.startswith("PRETTY_NAME="):
                return line.split("=", 1)[1].strip('"')
    except OSError:
        pass
    return platform.platform()


class Engine:
    def __init__(self, cfg: Config, emit: Callable[[dict], None], runtime_dir: Path,
                 save_config: Callable[[Config], None] = config_mod.save):
        self.cfg = cfg
        self._emit = emit
        self._save_config = save_config
        self.case: Case | None = None
        self.queue = Queue()
        self.conv: list[dict] = []   # OpenAI-format messages (no system prompt)
        self.chat: list[dict] = []   # display history
        self.sessions = SessionManager(on_change=self._sessions_changed)
        self.bridge = AskpassBridge(runtime_dir, self._askpass)
        self._models: dict[str, list[str]] = {}
        self._enclaves: dict[str, Enclave] = {}      # provider name -> attested enclave (Private Mode)
        self.attestation: dict | None = None         # latest attestation of the active private model
        self.last_usage: dict | None = None          # token usage of the last completed turn
        self._attest_task: asyncio.Task | None = None
        self._turn: asyncio.Task | None = None
        self._prompts: dict[str, tuple[asyncio.Future, dict]] = {}
        self._prompt_ids = itertools.count(1)

    # ---------------------------------------------------------------- lifecycle

    async def start(self) -> None:
        await self.bridge.start()

    async def stop(self) -> None:
        for task in (self._turn, self._attest_task):
            if task:
                task.cancel()
        self.sessions.close_all()
        await self.bridge.stop()

    def emit(self, type_: str, **data) -> None:
        self._emit({"type": type_, **data})

    def log(self, event: str, **data) -> None:
        if self.case:
            self.case.log(event, **data)

    # ---------------------------------------------------------------- state

    def snapshot(self) -> dict:
        return {
            "keyring_error": creds.backend_error(),
            "config": self._config_view(),
            "active_tier": self.active_tier(),
            "attestation": self.attestation,
            "case": self.case.to_dict() if self.case else None,
            "sessions": self.sessions.roster(),
            "queue": self.queue.to_list(),
            "chat": self.chat,
            "busy": self.busy,
            "prompts": [info for _, info in self._prompts.values()],
            "last_usage": self.last_usage,
        }

    def _config_view(self) -> dict:
        c = self.cfg.to_dict()
        for p in c["providers"]:
            p["has_key"] = self._has_secret("provider", p["name"])
        for h in c["hosts"]:
            h["has_password"] = self._has_secret("host", h["name"])
        return c

    @staticmethod
    def _has_secret(kind: str, name: str) -> bool:
        try:
            return creds.has_secret(kind, name)
        except Exception:  # noqa: BLE001 - keyring locked/unavailable
            return False

    def _changed(self) -> None:
        self.emit("state", state=self.snapshot())

    def _sessions_changed(self) -> None:
        self.emit("sessions", sessions=self.sessions.roster())

    def _queue_changed(self) -> None:
        self.emit("queue", queue=self.queue.to_list())

    @property
    def busy(self) -> bool:
        return self._turn is not None and not self._turn.done()

    # ---------------------------------------------------------------- providers & models

    def save_provider(self, data: dict, api_key: str | None = None, original_name: str | None = None) -> None:
        name = str(data.get("name", "")).strip()
        base_url = str(data.get("base_url", "")).strip().rstrip("/")
        if not name or not base_url:
            raise UserError("Provider needs a name and base URL.")
        overrides = {k.strip(): v.strip() for k, v in (data.get("tier_overrides") or {}).items() if k.strip()}
        prov = Provider(name=name, base_url=base_url, default_model=str(data.get("default_model", "")).strip(),
                        tier_overrides=overrides)
        old = self.cfg.provider(original_name or name)
        if original_name and original_name != name and old:
            key = creds.get_secret("provider", original_name)
            if key and not api_key:
                creds.set_secret("provider", name, key)
            creds.delete_secret("provider", original_name)
            if self.cfg.active_provider == original_name:
                self.cfg.active_provider = name
        if old:
            self.cfg.providers[self.cfg.providers.index(old)] = prov
        else:
            self.cfg.providers.append(prov)
        if api_key:
            creds.set_secret("provider", name, api_key)
        if not self.cfg.active_provider:
            self.cfg.active_provider = name
        self._models.pop(name, None)
        self._save_config(self.cfg)
        self._changed()

    def delete_provider(self, name: str) -> None:
        self.cfg.providers = [p for p in self.cfg.providers if p.name != name]
        creds.delete_secret("provider", name)
        if self.cfg.active_provider == name:
            self.cfg.active_provider = self.cfg.providers[0].name if self.cfg.providers else ""
            self.cfg.active_model = ""
        self._save_config(self.cfg)
        self._changed()

    def _client(self, provider: Provider, model: str = "") -> LLMClient | PrivateModeClient:
        key = creds.get_secret("provider", provider.name)
        if is_private_mode(model):
            enclave = self._enclaves.get(provider.name)
            if enclave is None or enclave.relay != relay_url(provider.base_url):
                enclave = self._enclaves[provider.name] = Enclave(relay_url(provider.base_url))
            return PrivateModeClient(provider.base_url, key, enclave)
        return LLMClient(provider.base_url, key)

    async def list_models(self, provider_name: str, refresh: bool = False) -> list[dict]:
        prov = self.cfg.provider(provider_name)
        if not prov:
            raise UserError(f"Unknown provider {provider_name}")
        if refresh or provider_name not in self._models:
            try:
                ids = await self._client(prov).list_models()
            except Exception as e:  # noqa: BLE001
                raise UserError(f"Could not list models from {prov.base_url}: {e}") from e
            if offers_private_mode(prov.base_url):
                try:
                    ids = sorted(set(ids) | set(await list_private_models(prov.base_url)))
                except Exception as e:  # noqa: BLE001 - the plain models are still usable
                    self.emit("toast", level="error", text=f"Could not list Private Mode models: {e}")
            self._models[provider_name] = ids
            if not prov.default_model:
                ids = self._models[provider_name]
                prov.default_model = next((m for m in ids if re.search(r"claude-opus-5[.-]5", m)), "")
            if not self.cfg.active_model and prov.default_model and prov.default_model in self._models[provider_name]:
                try:
                    self._check_tier(prov, prov.default_model)
                    self.cfg.active_provider, self.cfg.active_model = prov.name, prov.default_model
                except UserError:
                    pass
            self._save_config(self.cfg)
            self._changed()
        allowed = SENSITIVITY_TIERS[self.case.sensitivity] if self.case else set(SENSITIVITY_TIERS["open"])
        out = []
        for m in self._models[provider_name]:
            tier = detect_tier(m, prov.base_url, prov.tier_overrides)
            out.append({"id": m, "tier": tier, "allowed": tier in allowed})
        return out

    def select_model(self, provider_name: str, model: str) -> None:
        prov = self.cfg.provider(provider_name)
        if not prov:
            raise UserError(f"Unknown provider {provider_name}")
        self._check_tier(prov, model)
        self.cfg.active_provider, self.cfg.active_model = provider_name, model
        self.cfg.recent_models = ([f"{provider_name}|{model}"] +
                                  [r for r in self.cfg.recent_models if r != f"{provider_name}|{model}"])[:8]
        self._save_config(self.cfg)
        self.log("model_selected", provider=provider_name, model=model, tier=self.active_tier())
        self.attestation = None
        if is_private_mode(model):
            self._start_attestation(prov, model)
        self._changed()

    def _start_attestation(self, prov: Provider, model: str) -> None:
        if self._attest_task and not self._attest_task.done():
            self._attest_task.cancel()
        self.attestation = {"status": "checking", "model": model}
        self._attest_task = asyncio.create_task(self._attest(prov, model))

    async def _attest(self, prov: Provider, model: str) -> dict:
        """Attest the enclave a Private Mode model runs in; the result is shown and logged."""
        try:
            client = self._client(prov, model)
            att = await client.attest()
            self.attestation = {"status": "verified", "model": model, **att.to_dict()}
            self.log("enclave_attested", **self.attestation)
        except Exception as e:  # noqa: BLE001
            self.attestation = {"status": "failed", "model": model, "error": str(e)}
            self.log("enclave_attestation_failed", model=model, error=str(e))
        self._changed()
        return self.attestation

    async def attest_now(self) -> dict:
        prov = self.cfg.provider(self.cfg.active_provider)
        if not prov or not is_private_mode(self.cfg.active_model):
            raise UserError("The active model isn't an end-to-end encrypted (private/) model.")
        return await self._attest(prov, self.cfg.active_model)

    def active_tier(self) -> str | None:
        prov = self.cfg.provider(self.cfg.active_provider)
        if not prov or not self.cfg.active_model:
            return None
        return detect_tier(self.cfg.active_model, prov.base_url, prov.tier_overrides)

    def _check_tier(self, prov: Provider, model: str) -> str:
        tier = detect_tier(model, prov.base_url, prov.tier_overrides)
        if self.case and tier not in SENSITIVITY_TIERS[self.case.sensitivity]:
            raise UserError(f"This case is {self.case.sensitivity.upper()}: {tier.upper()}-tier models "
                            f"like {model} are not permitted.")
        return tier

    # ---------------------------------------------------------------- hosts

    def save_host(self, data: dict, password: str | None = None, original_name: str | None = None) -> None:
        name = str(data.get("name", "")).strip()
        if not name or not str(data.get("host", "")).strip():
            raise UserError("Host needs a name and address.")
        if data.get("kind") not in ("ssh", "winrm"):
            raise UserError("Host kind must be ssh or winrm.")
        port = data.get("port")
        host = Host(
            name=name, kind=data["kind"], host=str(data["host"]).strip(),
            port=int(port) if port not in (None, "", 0) else None,
            user=str(data.get("user", "")).strip(), auth=str(data.get("auth", "agent")),
            key_file=str(data.get("key_file", "")).strip(), jump=str(data.get("jump", "")).strip(),
            ssh_options=[o.strip() for o in data.get("ssh_options", []) if o.strip()],
            winrm_ssl=bool(data.get("winrm_ssl", True)),
            winrm_cert_validation=bool(data.get("winrm_cert_validation", True)),
            os_hint=str(data.get("os_hint", "")).strip(),
        )
        old = self.cfg.host(original_name or name)
        if original_name and original_name != name and old:
            pw = creds.get_secret("host", original_name)
            if pw and not password:
                creds.set_secret("host", name, pw)
            creds.delete_secret("host", original_name)
        if old:
            self.cfg.hosts[self.cfg.hosts.index(old)] = host
        else:
            self.cfg.hosts.append(host)
        if password:
            creds.set_secret("host", name, password)
        self._save_config(self.cfg)
        self._changed()

    def delete_host(self, name: str) -> None:
        self.cfg.hosts = [h for h in self.cfg.hosts if h.name != name]
        creds.delete_secret("host", name)
        self._save_config(self.cfg)
        self._changed()

    def forget_host_password(self, name: str) -> None:
        creds.delete_secret("host", name)
        self._changed()

    def save_settings(self, data: dict) -> None:
        s = self.cfg.settings
        for key in ("capture_max_lines", "capture_max_chars", "scrollback", "font_size", "context_warn_tokens"):
            if key in data:
                value = int(data[key])
                if value <= 0:
                    raise UserError(f"{key} must be positive.")
                setattr(s, key, value)
        self._save_config(self.cfg)
        self._changed()

    # ---------------------------------------------------------------- cases

    def new_case(self, name: str, sensitivity: str, notes: str = "") -> None:
        if self.busy:
            raise UserError("Wait for the AI to finish (or stop it) before starting a new case.")
        self.case = Case.create(name.strip() or "Untitled case", sensitivity, notes.strip())
        self.queue = Queue()
        self.conv, self.chat = [], []
        self.last_usage = None
        self._attach_case()
        self._persist()

    def list_cases(self) -> list[dict]:
        return Case.list_all()

    def open_case(self, case_id: str) -> None:
        """Resume a case from disk: conversation, chat history and queue. Sessions carry over."""
        if self.busy:
            raise UserError("Wait for the AI to finish (or stop it) before opening a case.")
        try:
            case = Case.load(case_id)
            state = case.load_state() or {}
        except (OSError, ValueError) as e:
            raise UserError(f"Could not open case {case_id}: {e}") from e
        self.case = case
        self.conv = list(state.get("conv", []))
        self.chat = list(state.get("chat", []))
        self.queue = Queue.from_list(state.get("queue", []))
        self.last_usage = None
        self.log("case_resumed", messages=len(self.chat), queue=len(self.queue.items))
        self._attach_case()
        if self.chat:
            self.chat.append({"kind": "note", "text": f"Case resumed {datetime.now():%Y-%m-%d %H:%M}. "
                              "Output of earlier commands is captured from the transcript files."})
        self._persist()

    def _attach_case(self) -> None:
        self.sessions.set_transcript_paths(self.case.transcript_path)
        prov = self.cfg.provider(self.cfg.active_provider)
        if prov and self.cfg.active_model:
            try:
                self._check_tier(prov, self.cfg.active_model)
            except UserError:
                self.cfg.active_model = ""
        for s in self.sessions.roster():
            self.log("session_carried_over", **s)
        self._changed()

    def _persist(self) -> None:
        """Write the resumable state after every change to the conversation or queue."""
        if not self.case:
            return
        try:
            self.case.save_state(self.conv, self.chat, self.queue.to_list())
        except OSError as e:
            self.emit("toast", level="error", text=f"Could not save case state: {e}")

    # ---------------------------------------------------------------- sessions

    def open_session(self, kind: str, host_name: str = "") -> dict:
        if not self.case:
            raise UserError("Start a case first.")
        if kind == "local":
            shell = os.environ.get("SHELL") or "/bin/bash"
            sid = self.sessions.unique_id("local")
            sess = self.sessions.spawn(sid, [shell], {}, name=sid, kind="local", target="this machine",
                                       shell=Path(shell).name, os_hint=_local_os())
        else:
            host = self.cfg.host(host_name)
            if not host:
                raise UserError(f"Unknown host {host_name}")
            sid = self.sessions.unique_id(host.name)
            env = self.bridge.env_for(sid)
            if host.kind == "ssh":
                sess = self.sessions.spawn(sid, ssh_argv(host), env, name=host.name, kind="ssh",
                                           target=target_label(host), shell="remote shell/CLI",
                                           os_hint=host.os_hint, host_name=host.name)
            else:
                env["DATOOLKIT_PSRP"] = json.dumps({
                    "host": host.host, "port": host.port, "user": host.user, "auth": host.auth,
                    "ssl": host.winrm_ssl, "cert_validation": host.winrm_cert_validation})
                src_root = str(Path(__file__).resolve().parents[1])
                env["PYTHONPATH"] = src_root + (":" + os.environ["PYTHONPATH"] if os.environ.get("PYTHONPATH") else "")
                sess = self.sessions.spawn(sid, [sys.executable, "-m", "datoolkit.sessions.psrp_console"], env,
                                           name=host.name, kind="winrm", target=target_label(host),
                                           shell="PowerShell (remoting, single-line)",
                                           os_hint=host.os_hint or "Windows", host_name=host.name)
        self.log("session_opened", **sess.roster())
        return sess.roster()

    def close_session(self, sid: str) -> None:
        self.sessions.close(sid)
        self.log("session_closed", session_id=sid)

    def set_session_hint(self, sid: str, os_hint: str) -> None:
        self.sessions.sessions[sid].os_hint = os_hint.strip()
        self._sessions_changed()

    # ---------------------------------------------------------------- credential prompts

    async def _askpass(self, sid: str, prompt: str, kind: str) -> str | None:
        sess = self.sessions.sessions.get(sid)
        host = self.cfg.host(sess.host_name) if sess and sess.host_name else None
        secret = kind == "password" or bool(re.search(r"password|passphrase|passcode|\bpin\b|secret", prompt, re.I))
        is_password = secret and "passphrase" not in prompt.lower()
        if sess and host and is_password and not sess.used_stored_password:
            # A prompt naming some other user@host (e.g. a jump host) is not ours to answer.
            other_host = "@" in prompt and host.host.lower() not in prompt.lower()
            stored = None if other_host else self._get_secret_safe("host", host.name)
            if stored:
                sess.used_stored_password = True
                self.log("credential_supplied", session_id=sid, source="keyring")
                return stored
        answer = await self.ask_user(sid, prompt, secret=secret, can_save=bool(host and is_password))
        self.log("credential_prompt", session_id=sid, prompt=prompt, answered=answer is not None)
        return answer

    def _get_secret_safe(self, kind: str, name: str) -> str | None:
        try:
            return creds.get_secret(kind, name)
        except Exception:  # noqa: BLE001
            return None

    async def ask_user(self, sid: str, prompt: str, secret: bool, can_save: bool) -> str | None:
        pid = str(next(self._prompt_ids))
        fut = asyncio.get_running_loop().create_future()
        info = {"id": pid, "session_id": sid, "text": prompt.strip(), "secret": secret, "can_save": can_save}
        self._prompts[pid] = (fut, info)
        self.emit("prompt", prompt=info)
        try:
            answer, save = await asyncio.wait_for(fut, PROMPT_TIMEOUT)
        except asyncio.TimeoutError:
            return None
        finally:
            self._prompts.pop(pid, None)
            self.emit("prompt_done", id=pid)
        if answer is not None and save and can_save:
            sess = self.sessions.sessions.get(sid)
            if sess and sess.host_name:
                creds.set_secret("host", sess.host_name, answer)
                sess.used_stored_password = True
        return answer

    def answer_prompt(self, pid: str, answer: str | None, save: bool = False) -> None:
        entry = self._prompts.get(pid)
        if entry and not entry[0].done():
            entry[0].set_result((answer, save))

    # ---------------------------------------------------------------- queue

    def update_item(self, num: int, **fields) -> None:
        was_pending = self.queue.get(num).status == "pending"
        p = self.queue.update(num, **fields)
        if "command" in fields and p.edited:
            self.log("proposal_edited", num=num, command=p.command, original=p.original_command, risk=p.risk)
        if "status" in fields:
            if p.status in ("ran", "inserted") and was_pending:
                # remember where this command's output starts in the transcript (see capture())
                p.capture_start = self._transcript_size(p.session_id)
            self.log("proposal_" + p.status, num=num, session_id=p.session_id, command=p.command,
                     note=p.note, risk=p.risk)
        self._queue_changed()
        self._persist()

    def move_item(self, num: int, delta: int) -> None:
        self.queue.move(num, delta)
        self._queue_changed()
        self._persist()

    def _transcript_size(self, sid: str) -> int | None:
        if not self.case:
            return None
        try:
            return self.case.transcript_path(sid).stat().st_size
        except OSError:
            return 0

    def capture(self, num: int) -> dict:
        """Output of a ran command taken from the session transcript on disk: the fallback when
        the terminal buffer no longer has it (page reloaded, session closed, case resumed)."""
        p = self.queue.get(num)
        if p.capture_start is None or not self.case:
            return {"text": "", "error": "No transcript position was recorded for this command."}
        end = None
        for other in self.queue.items:
            if (other.session_id == p.session_id and other.capture_start is not None
                    and other.capture_start > p.capture_start and (end is None or other.capture_start < end)):
                end = other.capture_start
        try:
            with self.case.transcript_path(p.session_id).open("rb") as f:
                f.seek(p.capture_start)
                data = f.read() if end is None else f.read(max(0, end - p.capture_start))
        except OSError as e:
            return {"text": "", "error": f"Transcript not readable: {e}"}
        text = data.decode("utf-8", errors="replace").strip("\n")
        return {"text": text, "source": "transcript"}

    def preview(self, texts: list[str]) -> list[dict]:
        s = self.cfg.settings
        out = []
        for t in texts:
            red, n = redact(t or "")
            cut, truncated = head_tail(red, s.capture_max_lines, s.capture_max_chars)
            out.append({"text": cut, "redactions": n, "truncated": truncated, "warnings": suspicious(cut)})
        return out

    # ---------------------------------------------------------------- chat

    def send(self, message: str = "", results: list[dict] | None = None,
             snippets: list[dict] | None = None) -> None:
        results, snippets = results or [], snippets or []
        if self.busy:
            raise UserError("The AI is still responding.")
        if not self.case:
            raise UserError("Start a case first.")
        prov, model, tier = self._require_model()
        if not (message.strip() or results or snippets):
            raise UserError("Nothing to send.")

        parts, shown = [], []
        if message.strip():
            parts.append(message.strip())
        if results:
            parts.append("[Results returned by the technician]")
        for r in results:
            p = self.queue.get(int(r["num"]))
            status = p.status if p.status in ("ran", "inserted", "skipped") else "ran"
            note = str(r.get("note", "") or p.note).strip()
            text = str(r.get("text", ""))
            label = {"ran": "RAN", "inserted": "RAN (edited in terminal)", "skipped": "SKIPPED"}[status]
            head = f"#{p.num} on session `{p.session_id}` {label}: `{p.command}`"
            if p.edited:
                head += f" (edited by technician; originally `{p.original_command}`)"
            block = [head]
            if note:
                block.append(f"Technician's note: {note}")
            if status != "skipped":
                block.append(fence(text) if text.strip() else "(no output captured)")
            parts.append("\n".join(block))
            shown.append({"num": p.num, "session_id": p.session_id, "command": p.command,
                          "status": status, "note": note, "text": text if status != "skipped" else ""})
        for s in snippets:
            parts.append(f"[Terminal excerpt from session `{s.get('session_id', '')}`, selected by the technician]\n"
                         + fence(str(s.get("text", ""))))
        content = "\n\n".join(parts)

        self.conv.append({"role": "user", "content": content})
        self.chat.append({"kind": "user", "text": message.strip(), "results": shown,
                          "snippets": [{"session_id": s.get("session_id", ""), "text": s.get("text", "")}
                                       for s in snippets]})
        for r in shown:
            self.queue.update(r["num"], status="sent")
        self.log("sent_to_ai", provider=prov.name, model=model, tier=tier, content=content)
        self.emit("chat", entry=self.chat[-1])
        self._queue_changed()
        self._persist()
        self._turn = asyncio.create_task(self._run_turn(prov, model, tier))

    def _require_model(self) -> tuple[Provider, str, str]:
        prov = self.cfg.provider(self.cfg.active_provider)
        if not prov or not self.cfg.active_model:
            raise UserError("Choose a model first.")
        tier = self._check_tier(prov, self.cfg.active_model)
        return prov, self.cfg.active_model, tier

    def stop_turn(self) -> None:
        if self.busy:
            self._turn.cancel()

    def _system_prompt(self) -> str:
        return prompts.build_system(self.sessions.roster(), self.case.name, self.case.notes)

    async def _run_turn(self, prov: Provider, model: str, tier: str) -> None:
        self.emit("turn_start", model=model, tier=tier)
        entry = {"kind": "assistant", "text": "", "reasoning": "", "proposals": [], "model": model, "tier": tier}
        usage = None
        error = None
        try:
            client = self._client(prov, model)
            if isinstance(client, PrivateModeClient):
                att = await client.attest()  # nothing is sent until the enclave has proved itself
                if not self.attestation or self.attestation.get("hpke_key_sha256") != att.hpke_key_sha256:
                    self.attestation = {"status": "verified", "model": model, **att.to_dict()}
                    self.log("enclave_attested", **self.attestation)
                    self._changed()
                entry["sealed"] = att.summary
            for _ in range(MAX_TOOL_ROUNDS):
                messages = [{"role": "system", "content": self._system_prompt()}] + self.conv
                result = None
                async for kind, val in client.stream(model, messages, [prompts.PROPOSE_TOOL]):
                    if kind == "text":
                        entry["text"] += val
                        self.emit("delta", kind="text", text=val)
                    elif kind == "reasoning":
                        entry["reasoning"] += val
                        self.emit("delta", kind="reasoning", text=val)
                    else:
                        result = val
                usage = result.usage
                retry = self._record_assistant(result, entry)
                if not retry:
                    break
                entry["text"] += "\n\n"
        except asyncio.CancelledError:
            error = "stopped"
            if entry["text"]:
                self.conv.append({"role": "assistant", "content": entry["text"] + "\n[response stopped by technician]"})
        except Exception as e:  # noqa: BLE001
            error = f"{type(e).__name__}: {e}"
        entry["text"] = entry["text"].strip()
        if entry["text"] or entry["proposals"] or entry["reasoning"]:
            self.chat.append(entry)
            self.log("assistant", model=model, tier=tier, text=entry["text"], proposals=entry["proposals"])
        if error:
            self.chat.append({"kind": "note", "text": f"AI request {error}"})
            self.log("turn_error", error=error)
        if usage:
            self.last_usage = usage
            self.log("usage", model=model, **{k: v for k, v in usage.items() if isinstance(v, int)})
        self.emit("turn_end", entry=entry, error=error, usage=usage, chat=self.chat)
        self._queue_changed()
        self._persist()

    def _record_assistant(self, result, entry: dict) -> bool:
        """Append the assistant turn to history. Returns True if the model should be asked again."""
        msg: dict = {"role": "assistant", "content": result.content or None}
        if result.tool_calls:
            msg["tool_calls"] = [{"id": c.id, "type": "function",
                                  "function": {"name": c.name, "arguments": c.arguments or "{}"}}
                                 for c in result.tool_calls]
        elif not result.content:
            msg["content"] = ""
        self.conv.append(msg)
        retry = False
        for call in result.tool_calls:
            if call.name != prompts.PROPOSE_TOOL["function"]["name"]:
                reply, retry = f"Unknown tool {call.name}. Use propose_commands.", True
            else:
                try:
                    items = call.parsed()["items"]
                    if not isinstance(items, list):
                        raise TypeError("items must be an array")
                    open_ids = [r["id"] for r in self.sessions.roster() if not r["exited"]]
                    added = self.queue.add(call.id, items, known_sessions=set(open_ids),
                                           fallback_session=open_ids[0] if len(open_ids) == 1 else "")
                    if not added:
                        raise ValueError("no commands in items")
                except Exception as e:  # noqa: BLE001
                    reply, retry = f"Invalid propose_commands arguments ({e}). Call it again with valid JSON.", True
                else:
                    nums = [p.num for p in added]
                    entry["proposals"] += nums
                    for p in added:
                        self.log("proposal", **p.to_dict())
                    known = {s["id"] for s in self.sessions.roster()}
                    unknown = sorted({p.session_id for p in added if p.session_id not in known})
                    reply = (f"Queued for technician review as {', '.join(f'#{n}' for n in nums)}. Results "
                             "will arrive in a later message; do not assume anything has run.")
                    if unknown:
                        reply += (f" Note: session id(s) {', '.join(unknown)} do not exist; the technician "
                                  "must pick a target for those.")
                    self._queue_changed()
            self.conv.append({"role": "tool", "tool_call_id": call.id, "content": reply})
        return retry

    # ---------------------------------------------------------------- export

    def export_markdown(self) -> str:
        if not self.case:
            raise UserError("No case to export.")
        path = self.case.export_markdown(self.chat, self.queue.to_list())
        self.log("exported_markdown", path=str(path))
        return str(path)

    async def ticket_summary(self) -> dict:
        if not self.case:
            raise UserError("No case to summarise.")
        if not self.conv:
            raise UserError("Nothing to summarise yet.")
        prov, model, tier = self._require_model()
        transcript = []
        for m in self.conv:
            if m["role"] == "user":
                transcript.append("TECHNICIAN:\n" + m["content"])
            elif m["role"] == "assistant" and m.get("content"):
                transcript.append("AI:\n" + m["content"])
            elif m["role"] == "assistant":
                transcript.append("AI proposed: " + "; ".join(c["function"]["arguments"] for c in m["tool_calls"]))
        messages = [{"role": "system", "content": prompts.SUMMARY_PROMPT},
                    {"role": "user", "content": f"Case: {self.case.name}\n\n" + "\n\n".join(transcript)}]
        self.log("sent_to_ai", purpose="ticket_summary", provider=prov.name, model=model, tier=tier)
        try:
            text = await self._client(prov, model).complete(model, messages)
        except Exception as e:  # noqa: BLE001
            raise UserError(f"Summary request failed: {e}") from e
        path = self.case.dir / "ticket-summary.md"
        path.write_text(text + "\n", encoding="utf-8")
        self.log("ticket_summary", path=str(path), text=text)
        return {"text": text, "path": str(path)}
