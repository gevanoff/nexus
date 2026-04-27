from __future__ import annotations

import asyncio
import json
import os
import shlex
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel


app = FastAPI(title="Nexus Backend Lifecycle Manager", version="0.1")


def _env(name: str, default: str = "") -> str:
    value = os.environ.get(name)
    if value is None:
        return default
    value = value.strip()
    return value if value else default


def _int_env(name: str, default: int) -> int:
    raw = _env(name)
    if not raw:
        return default
    try:
        return int(raw)
    except Exception:
        return default


def _float_env(name: str, default: float) -> float:
    raw = _env(name)
    if not raw:
        return default
    try:
        return float(raw)
    except Exception:
        return default


def _bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return False


def _as_list(value: Any) -> List[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str) and value.strip():
        return [item.strip() for item in value.split(",") if item.strip()]
    return []


def _read_json(path: Path) -> Dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}


@dataclass
class HostPolicy:
    name: str
    ssh_target: str
    repo_dir: str
    env_file: str
    platform: str
    resource_kind: str
    remote_shell: str = "bash -lc"
    error: str = ""
    memory: Dict[str, Any] = field(default_factory=dict)
    gpus: List[Dict[str, Any]] = field(default_factory=list)
    containers: Dict[str, str] = field(default_factory=dict)
    updated_at: float = 0.0


@dataclass
class BackendPolicy:
    backend_class: str
    display_name: str
    host: str
    components: List[str]
    compose_files: List[str]
    tier: str
    tier_rank: int
    capabilities: List[str]
    estimated_vram_mb: int
    auto_start: bool
    auto_stop: bool
    requires_confirmation: bool
    compose_managed: bool
    ready_path: str
    base_url: str
    notes: str = ""
    active: bool = False
    healthy: Optional[bool] = None
    ready: Optional[bool] = None
    health_error: str = ""
    last_checked_at: float = 0.0
    last_healthy_at: float = 0.0
    last_ready_at: float = 0.0
    last_unhealthy_at: float = 0.0
    last_stopped_at: float = 0.0
    last_health_error: str = ""
    last_requested_at: float = 0.0
    inflight: int = 0
    last_action: str = ""
    last_action_at: float = 0.0
    last_action_error: str = ""


class EnsureRequest(BaseModel):
    backend_class: str
    route_kind: str = ""
    reason: str = "ui"
    confirmed: bool = False
    allow_disruptive: bool = False


class ActionRequest(BaseModel):
    backend_class: str
    action: str
    confirmed: bool = False
    allow_disruptive: bool = False


class NotifyRequest(BaseModel):
    backend_class: str
    event: str
    route_kind: str = ""


class LifecycleManager:
    def __init__(self) -> None:
        self.policy_path = Path(_env("NEXUS_LIFECYCLE_POLICY", "/app/config/backend_lifecycle.json"))
        self.topology_path = Path(_env("NEXUS_TOPOLOGY_FILE", "/app/config/production.json"))
        self.state_path = Path(_env("NEXUS_LIFECYCLE_STATE_PATH", "/app/state/backend_state.json"))
        self.mode = _env("NEXUS_LIFECYCLE_MODE", "")
        self.poll_interval_sec = _int_env("NEXUS_LIFECYCLE_POLL_INTERVAL_SEC", 15)
        self.request_hot_window_sec = 900
        self.optional_idle_stop_sec = 1800
        self.memory_pressure_used_ratio = 0.9
        self.target_free_vram_mb = 4096
        self.llm_advisor_enabled = False
        self.llm_advisor_model = "coder"
        self.llm_advisor_base_url = "http://gateway:8800/v1"
        self.ssh_identity_file = _env("NEXUS_LIFECYCLE_SSH_IDENTITY", "/root/.ssh/nexus_lifecycle_ed25519")
        self.hosts: Dict[str, HostPolicy] = {}
        self.backends: Dict[str, BackendPolicy] = {}
        self._task: Optional[asyncio.Task] = None
        self._lock = asyncio.Lock()
        self._load_config()
        self._load_state()

    def _load_config(self) -> None:
        policy = _read_json(self.policy_path)
        topology = _read_json(self.topology_path)

        defaults = topology.get("defaults") if isinstance(topology.get("defaults"), dict) else {}
        repo_by_platform = defaults.get("repo_dir_by_platform") if isinstance(defaults.get("repo_dir_by_platform"), dict) else {}
        default_env = defaults.get("env") if isinstance(defaults.get("env"), dict) else {}
        topology_hosts = topology.get("hosts") if isinstance(topology.get("hosts"), dict) else {}
        policy_hosts = policy.get("hosts") if isinstance(policy.get("hosts"), dict) else {}
        settings = policy.get("settings") if isinstance(policy.get("settings"), dict) else {}
        tiers = policy.get("tiers") if isinstance(policy.get("tiers"), dict) else {}
        policy_backends = policy.get("backends") if isinstance(policy.get("backends"), dict) else {}

        self.mode = self.mode or str(settings.get("mode") or "observe").strip().lower()
        self.poll_interval_sec = max(5, int(settings.get("poll_interval_sec") or self.poll_interval_sec))
        self.request_hot_window_sec = int(settings.get("request_hot_window_sec") or self.request_hot_window_sec)
        self.optional_idle_stop_sec = int(settings.get("optional_idle_stop_sec") or self.optional_idle_stop_sec)
        self.memory_pressure_used_ratio = float(settings.get("memory_pressure_used_ratio") or self.memory_pressure_used_ratio)
        self.target_free_vram_mb = int(settings.get("target_free_vram_mb") or self.target_free_vram_mb)
        llm_cfg = settings.get("llm_advisor") if isinstance(settings.get("llm_advisor"), dict) else {}
        self.llm_advisor_enabled = _bool(llm_cfg.get("enabled"))
        self.llm_advisor_model = str(llm_cfg.get("model") or "coder").strip()
        self.llm_advisor_base_url = str(llm_cfg.get("base_url") or "http://gateway:8800/v1").strip().rstrip("/")

        hosts: Dict[str, HostPolicy] = {}
        for name, topo in topology_hosts.items():
            if not isinstance(topo, dict):
                continue
            host_policy = policy_hosts.get(name) if isinstance(policy_hosts.get(name), dict) else {}
            platform = str(topo.get("platform") or "").strip()
            repo_dir = str(host_policy.get("repo_dir") or repo_by_platform.get(platform) or "").strip()
            env_file = str(host_policy.get("env_file") or f"deploy/env/.env.prod.{name}").strip()
            hosts[name] = HostPolicy(
                name=name,
                ssh_target=str(host_policy.get("ssh_target") or topo.get("ssh_target") or name).strip(),
                repo_dir=repo_dir,
                env_file=env_file,
                platform=platform,
                resource_kind=str(host_policy.get("resource_kind") or ("linux_nvidia" if platform == "linux" else platform)).strip(),
                remote_shell=str(host_policy.get("remote_shell") or ("/bin/zsh -lic" if platform == "macos" else "bash -lc")),
            )
        for name, host_policy in policy_hosts.items():
            if name in hosts or not isinstance(host_policy, dict):
                continue
            platform = str(host_policy.get("platform") or "").strip()
            hosts[name] = HostPolicy(
                name=name,
                ssh_target=str(host_policy.get("ssh_target") or name).strip(),
                repo_dir=str(host_policy.get("repo_dir") or repo_by_platform.get(platform) or "").strip(),
                env_file=str(host_policy.get("env_file") or f"deploy/env/.env.prod.{name}").strip(),
                platform=platform,
                resource_kind=str(host_policy.get("resource_kind") or platform).strip(),
                remote_shell=str(host_policy.get("remote_shell") or ("/bin/zsh -lic" if platform == "macos" else "bash -lc")),
            )
        self.hosts = hosts

        backends: Dict[str, BackendPolicy] = {}
        for backend_class, raw_cfg in policy_backends.items():
            if not isinstance(raw_cfg, dict):
                continue
            tier = str(raw_cfg.get("tier") or "optional").strip().lower()
            tier_cfg = tiers.get(tier) if isinstance(tiers.get(tier), dict) else {}
            components = _as_list(raw_cfg.get("components")) or _as_list(raw_cfg.get("component"))
            compose_files = _as_list(raw_cfg.get("compose_files")) or _as_list(raw_cfg.get("compose_file"))
            base_url = str(raw_cfg.get("base_url") or default_env.get(self._base_url_env_name(backend_class)) or "").strip()
            backends[backend_class] = BackendPolicy(
                backend_class=backend_class,
                display_name=str(raw_cfg.get("display_name") or backend_class).strip(),
                host=str(raw_cfg.get("host") or "").strip(),
                components=components,
                compose_files=compose_files,
                tier=tier,
                tier_rank=int(tier_cfg.get("rank") or 0),
                capabilities=_as_list(raw_cfg.get("capabilities")),
                estimated_vram_mb=int(raw_cfg.get("estimated_vram_mb") or 0),
                auto_start=_bool(raw_cfg.get("auto_start")),
                auto_stop=_bool(raw_cfg.get("auto_stop")),
                requires_confirmation=_bool(raw_cfg.get("requires_confirmation")),
                compose_managed=not (raw_cfg.get("compose_managed") is False),
                ready_path=str(raw_cfg.get("ready_path") or "/readyz").strip(),
                base_url=base_url,
                notes=str(raw_cfg.get("notes") or "").strip(),
            )
        self.backends = backends

    def _load_state(self) -> None:
        try:
            raw = _read_json(self.state_path)
        except Exception:
            return
        raw_backends = raw.get("backends") if isinstance(raw.get("backends"), dict) else raw
        if not isinstance(raw_backends, dict):
            return
        float_fields = {
            "last_checked_at",
            "last_healthy_at",
            "last_ready_at",
            "last_unhealthy_at",
            "last_stopped_at",
            "last_requested_at",
            "last_action_at",
        }
        str_fields = {"last_action", "last_action_error", "last_health_error"}
        for backend_class, state in raw_backends.items():
            backend = self.backends.get(str(backend_class))
            if backend is None or not isinstance(state, dict):
                continue
            for field_name in float_fields:
                try:
                    setattr(backend, field_name, float(state.get(field_name) or 0.0))
                except Exception:
                    continue
            for field_name in str_fields:
                setattr(backend, field_name, str(state.get(field_name) or ""))

    def _save_state(self) -> None:
        state = {
            "version": 1,
            "generated_at": time.time(),
            "backends": {
                backend.backend_class: {
                    "last_checked_at": backend.last_checked_at,
                    "last_healthy_at": backend.last_healthy_at,
                    "last_ready_at": backend.last_ready_at,
                    "last_unhealthy_at": backend.last_unhealthy_at,
                    "last_stopped_at": backend.last_stopped_at,
                    "last_health_error": backend.last_health_error,
                    "last_requested_at": backend.last_requested_at,
                    "last_action": backend.last_action,
                    "last_action_at": backend.last_action_at,
                    "last_action_error": backend.last_action_error,
                }
                for backend in self.backends.values()
            },
        }
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = self.state_path.with_name(self.state_path.name + ".tmp")
            tmp_path.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")
            tmp_path.replace(self.state_path)
        except Exception:
            return

    @staticmethod
    def _base_url_env_name(backend_class: str) -> str:
        return {
            "local_vllm": "VLLM_BASE_URL",
            "local_vllm_fast": "VLLM_FAST_BASE_URL",
            "local_vllm_embeddings": "VLLM_EMBEDDINGS_BASE_URL",
            "local_mlx": "MLX_BASE_URL",
            "gpu_heavy": "IMAGES_ADVERTISE_BASE_URL",
            "gpu_fast": "SDXL_TURBO_ADVERTISE_BASE_URL",
            "lighton_ocr": "LIGHTON_OCR_ADVERTISE_BASE_URL",
            "skyreels_v2": "SKYREELS_V2_ADVERTISE_BASE_URL",
            "personaplex": "PERSONAPLEX_ADVERTISE_BASE_URL",
            "followyourcanvas": "FOLLOWYOURCANVAS_ADVERTISE_BASE_URL",
            "heartmula_music": "HEARTMULA_ADVERTISE_BASE_URL",
            "pocket_tts": "POCKET_TTS_ADVERTISE_BASE_URL",
            "luxtts": "LUXTTS_ADVERTISE_BASE_URL",
            "qwen3_tts": "QWEN3_TTS_ADVERTISE_BASE_URL",
        }.get(backend_class, "")

    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        await self.refresh()
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        self._task = None

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(self.poll_interval_sec)
            try:
                await self.refresh()
                if self.mode == "auto":
                    await self._relieve_pressure()
            except Exception:
                pass

    async def refresh(self) -> Dict[str, Any]:
        async with self._lock:
            await asyncio.gather(*(self._refresh_host(host) for host in self.hosts.values()))
            self._refresh_active_flags()
            await self._refresh_health()
            self._refresh_active_flags()
            self._save_state()
            return self.status()

    async def _refresh_host(self, host: HostPolicy) -> None:
        host.error = ""
        host.updated_at = time.time()
        try:
            if host.resource_kind == "linux_nvidia":
                raw = await self._ssh(host, self._linux_probe_command())
                self._parse_linux_probe(host, raw)
            elif host.resource_kind == "macos":
                raw = await self._ssh(host, self._macos_probe_command())
                self._parse_macos_probe(host, raw)
            else:
                raw = await self._ssh(host, self._docker_probe_command())
                host.containers = self._parse_containers(raw)
        except Exception as exc:
            host.error = f"{type(exc).__name__}: {exc}"

    async def _refresh_health(self) -> None:
        async with httpx.AsyncClient(timeout=5.0) as client:
            tasks = [self._check_backend_health(client, backend) for backend in self.backends.values()]
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)

    async def _check_backend_health(self, client: httpx.AsyncClient, backend: BackendPolicy) -> None:
        if backend.compose_managed and not backend.active:
            backend.healthy = None
            backend.ready = False
            backend.health_error = ""
            return

        now = time.time()
        base_url = backend.base_url.rstrip("/")
        if not base_url:
            backend.healthy = None
            backend.ready = None
            backend.health_error = "base_url not configured"
            backend.last_checked_at = now
            if backend.active:
                backend.last_unhealthy_at = now
                backend.last_health_error = backend.health_error
            return
        path = backend.ready_path or "/readyz"
        if not path.startswith("/"):
            path = "/" + path
        try:
            response = await client.get(f"{base_url}{path}")
            backend.last_checked_at = now
            backend.healthy = response.status_code < 500
            backend.ready = response.status_code == 200
            backend.health_error = "" if response.status_code == 200 else f"HTTP {response.status_code}"
            if backend.ready:
                backend.last_healthy_at = now
                backend.last_ready_at = now
                backend.last_health_error = ""
            else:
                backend.last_unhealthy_at = now
                backend.last_health_error = backend.health_error
        except Exception as exc:
            backend.last_checked_at = now
            backend.healthy = False
            backend.ready = False
            backend.health_error = f"{type(exc).__name__}: {exc}"
            backend.last_unhealthy_at = now
            backend.last_health_error = backend.health_error

    def _refresh_active_flags(self) -> None:
        for backend in self.backends.values():
            host = self.hosts.get(backend.host)
            if host is None:
                backend.active = False
                continue
            if not backend.compose_managed:
                backend.active = backend.ready is True
                continue
            expected = [f"nexus-{component}" for component in backend.components]
            if not expected:
                backend.active = False
                continue
            backend.active = all(
                any(name == expected_name or name.startswith(expected_name + "-") for name in host.containers)
                for expected_name in expected
            )

    async def ensure(self, req: EnsureRequest) -> Dict[str, Any]:
        backend = self._backend_or_404(req.backend_class)
        backend.last_requested_at = time.time()
        if backend.active and backend.ready is not False:
            return {"ok": True, "decision": "already_active", "backend": self._backend_status(backend)}
        plan = self._activation_plan(backend, confirmed=req.confirmed, allow_disruptive=req.allow_disruptive)
        if plan["decision"] in {"requires_confirmation", "blocked", "observe_only"}:
            return await self._attach_llm_advice(plan)
        if self.mode not in {"assisted", "auto"} and not req.confirmed:
            plan["decision"] = "observe_only"
            plan["message"] = "Lifecycle manager is not in assisted/auto mode."
            return await self._attach_llm_advice(plan)
        await self._execute_plan(plan)
        await self.refresh()
        return plan

    async def action(self, req: ActionRequest) -> Dict[str, Any]:
        backend = self._backend_or_404(req.backend_class)
        action = req.action.strip().lower()
        if action not in {"activate", "start", "deactivate", "stop"}:
            raise HTTPException(status_code=400, detail="action must be activate/start/deactivate/stop")
        if action in {"activate", "start"}:
            plan = self._activation_plan(backend, confirmed=req.confirmed, allow_disruptive=req.allow_disruptive)
        else:
            if backend.tier == "crucial" and not req.confirmed:
                return {
                    "ok": False,
                    "decision": "requires_confirmation",
                    "message": "Stopping a crucial backend requires confirmation.",
                    "backend": self._backend_status(backend),
                }
            plan = {"ok": True, "decision": "deactivate", "start": [], "stop": [backend.backend_class], "backend": self._backend_status(backend)}
        if plan.get("decision") in {"requires_confirmation", "blocked", "observe_only"}:
            return await self._attach_llm_advice(plan)
        await self._execute_plan(plan)
        await self.refresh()
        return plan

    def notify(self, req: NotifyRequest) -> Dict[str, Any]:
        backend = self._backend_or_404(req.backend_class)
        event = req.event.strip().lower()
        backend.last_requested_at = time.time()
        if event in {"start", "started", "acquire"}:
            backend.inflight += 1
        elif event in {"finish", "finished", "release", "end", "error"}:
            backend.inflight = max(0, backend.inflight - 1)
        return {"ok": True, "backend": self._backend_status(backend)}

    def _activation_plan(self, backend: BackendPolicy, *, confirmed: bool, allow_disruptive: bool) -> Dict[str, Any]:
        if not backend.compose_managed:
            return {
                "ok": False,
                "decision": "blocked",
                "message": "Backend is not compose-managed by the lifecycle manager.",
                "backend": self._backend_status(backend),
            }
        if backend.requires_confirmation and not confirmed:
            return {
                "ok": False,
                "decision": "requires_confirmation",
                "message": "This backend is marked as requiring operator confirmation.",
                "backend": self._backend_status(backend),
                "conflicts": self._same_host_active(backend),
            }
        if not backend.auto_start and not confirmed:
            return {
                "ok": False,
                "decision": "requires_confirmation",
                "message": "Policy disables automatic start for this backend.",
                "backend": self._backend_status(backend),
                "conflicts": self._same_host_active(backend),
            }

        host = self.hosts.get(backend.host)
        if host is None:
            return {"ok": False, "decision": "blocked", "message": f"Unknown host {backend.host}", "backend": self._backend_status(backend)}
        if backend.estimated_vram_mb <= 0:
            return {"ok": True, "decision": "activate", "start": [backend.backend_class], "stop": [], "backend": self._backend_status(backend)}
        free_mb = self._host_free_vram(host)
        needed_mb = max(0, backend.estimated_vram_mb - free_mb + self.target_free_vram_mb)
        if needed_mb <= 0:
            return {"ok": True, "decision": "activate", "start": [backend.backend_class], "stop": [], "backend": self._backend_status(backend)}

        victims: List[BackendPolicy] = []
        freed = 0
        for candidate in self._same_host_candidates(backend):
            if candidate.backend_class == backend.backend_class or not candidate.active:
                continue
            if candidate.tier_rank > backend.tier_rank and not allow_disruptive:
                continue
            if candidate.tier_rank == backend.tier_rank and candidate.tier != "optional" and not allow_disruptive:
                continue
            if not candidate.auto_stop and not allow_disruptive:
                continue
            if candidate.inflight > 0:
                continue
            age = time.time() - (candidate.last_requested_at or 0)
            if age < self.optional_idle_stop_sec and candidate.tier != "optional" and not allow_disruptive:
                continue
            victims.append(candidate)
            freed += candidate.estimated_vram_mb
            if freed >= needed_mb:
                break

        if freed >= needed_mb:
            return {
                "ok": True,
                "decision": "swap",
                "start": [backend.backend_class],
                "stop": [victim.backend_class for victim in victims],
                "backend": self._backend_status(backend),
                "conflicts": [self._backend_status(victim) for victim in victims],
            }

        return {
            "ok": False,
            "decision": "requires_confirmation",
            "message": "Insufficient free VRAM for an easy swap.",
            "needed_vram_mb": needed_mb,
            "free_vram_mb": free_mb,
            "backend": self._backend_status(backend),
            "conflicts": self._same_host_active(backend),
        }

    async def _execute_plan(self, plan: Dict[str, Any]) -> None:
        stop_items = [self.backends[item] for item in plan.get("stop", []) if item in self.backends]
        start_items = [self.backends[item] for item in plan.get("start", []) if item in self.backends]
        for backend in stop_items:
            await self._compose(backend, "stop")
        for backend in start_items:
            await self._compose(backend, "up -d --build")

    async def _attach_llm_advice(self, plan: Dict[str, Any]) -> Dict[str, Any]:
        if not self.llm_advisor_enabled:
            return plan
        if not self.llm_advisor_base_url or not self.llm_advisor_model:
            return plan
        summary = {
            "plan": plan,
            "mode": self.mode,
            "hosts": [
                {
                    "name": host.name,
                    "resource_kind": host.resource_kind,
                    "error": host.error,
                    "gpus": host.gpus,
                }
                for host in self.hosts.values()
            ],
            "active_backends": [
                self._backend_status(backend)
                for backend in self.backends.values()
                if backend.active
            ],
        }
        messages = [
            {
                "role": "system",
                "content": (
                    "You advise a local AI backend lifecycle manager. Reply with compact JSON containing "
                    "recommendation, rationale, and risks. Do not ask questions."
                ),
            },
            {"role": "user", "content": json.dumps(summary, ensure_ascii=False)[:12000]},
        ]
        headers = {}
        token = _env("GATEWAY_BEARER_TOKEN")
        if token:
            headers["Authorization"] = f"Bearer {token}"
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.post(
                    f"{self.llm_advisor_base_url}/chat/completions",
                    headers=headers,
                    json={"model": self.llm_advisor_model, "messages": messages, "temperature": 0},
                )
            if response.status_code >= 400:
                plan["advisor_error"] = f"HTTP {response.status_code}"
                return plan
            payload = response.json()
            content = ""
            choices = payload.get("choices") if isinstance(payload, dict) else None
            if isinstance(choices, list) and choices:
                msg = choices[0].get("message") if isinstance(choices[0], dict) else None
                if isinstance(msg, dict):
                    content = str(msg.get("content") or "")
            if content:
                plan["advisor"] = content[:4000]
        except Exception as exc:
            plan["advisor_error"] = f"{type(exc).__name__}: {exc}"
        return plan

    async def _compose(self, backend: BackendPolicy, compose_action: str) -> None:
        host = self.hosts.get(backend.host)
        if host is None:
            raise HTTPException(status_code=400, detail=f"unknown host {backend.host}")
        if not backend.compose_files:
            raise HTTPException(status_code=400, detail=f"no compose files configured for {backend.backend_class}")
        compose_args = " ".join(f"-f {shlex.quote(item)}" for item in backend.compose_files)
        command = f"cd {shlex.quote(host.repo_dir)} && docker compose --env-file {shlex.quote(host.env_file)} {compose_args} {compose_action}"
        backend.last_action = compose_action
        backend.last_action_at = time.time()
        try:
            await self._ssh(host, command, timeout=900)
            backend.last_action_error = ""
            if compose_action.strip().lower().startswith("stop"):
                backend.last_stopped_at = time.time()
            self._save_state()
        except Exception as exc:
            backend.last_action_error = f"{type(exc).__name__}: {exc}"
            self._save_state()
            raise

    async def _relieve_pressure(self) -> None:
        for host in self.hosts.values():
            total, used, free = self._host_vram_tuple(host)
            if total <= 0:
                continue
            if used / total < self.memory_pressure_used_ratio and free >= self.target_free_vram_mb:
                continue
            candidates = [
                backend
                for backend in self.backends.values()
                if backend.host == host.name
                and backend.active
                and backend.auto_stop
                and backend.tier == "optional"
                and backend.inflight == 0
                and (time.time() - (backend.last_requested_at or 0)) >= self.optional_idle_stop_sec
            ]
            candidates.sort(key=lambda item: (item.tier_rank, item.last_requested_at or 0))
            for backend in candidates:
                try:
                    await self._compose(backend, "stop")
                except Exception:
                    continue
                free += backend.estimated_vram_mb
                if free >= self.target_free_vram_mb:
                    break

    def _same_host_candidates(self, backend: BackendPolicy) -> List[BackendPolicy]:
        return [candidate for candidate in self.backends.values() if candidate.host == backend.host]

    def _same_host_active(self, backend: BackendPolicy) -> List[Dict[str, Any]]:
        return [self._backend_status(candidate) for candidate in self._same_host_candidates(backend) if candidate.active]

    def _backend_or_404(self, backend_class: str) -> BackendPolicy:
        key = backend_class.strip()
        backend = self.backends.get(key)
        if backend is None:
            raise HTTPException(status_code=404, detail=f"unknown backend_class {backend_class}")
        return backend

    def _host_free_vram(self, host: HostPolicy) -> int:
        _total, _used, free = self._host_vram_tuple(host)
        return free

    def _host_vram_tuple(self, host: HostPolicy) -> tuple[int, int, int]:
        total = sum(int(gpu.get("memory_total_mb") or 0) for gpu in host.gpus)
        used = sum(int(gpu.get("memory_used_mb") or 0) for gpu in host.gpus)
        free = sum(int(gpu.get("memory_free_mb") or 0) for gpu in host.gpus)
        return total, used, free

    async def _ssh(self, host: HostPolicy, command: str, *, timeout: int = 30) -> str:
        remote_command = f"{host.remote_shell} {shlex.quote(command)}"
        ssh_args = [
            "ssh",
            "-o",
            "BatchMode=yes",
            "-o",
            "ConnectTimeout=5",
            "-o",
            "StrictHostKeyChecking=accept-new",
            "-o",
            "UserKnownHostsFile=/tmp/nexus_lifecycle_known_hosts",
        ]
        if self.ssh_identity_file:
            ssh_args.extend(["-i", self.ssh_identity_file])
        ssh_args.extend([host.ssh_target, remote_command])
        proc = await asyncio.to_thread(
            subprocess.run,
            ssh_args,
            text=True,
            capture_output=True,
            timeout=timeout,
        )
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "").strip()
            raise RuntimeError(detail or f"ssh returned {proc.returncode}")
        return proc.stdout or ""

    @staticmethod
    def _docker_probe_command() -> str:
        return "docker ps --format '{{.Names}}\\t{{.Status}}' 2>/dev/null || true"

    @classmethod
    def _linux_probe_command(cls) -> str:
        return (
            "printf '__GPU__\\n'; "
            "nvidia-smi --query-gpu=index,name,memory.total,memory.used,memory.free,utilization.gpu "
            "--format=csv,noheader,nounits 2>/dev/null || true; "
            "printf '__MEM__\\n'; free -m 2>/dev/null | awk '/^Mem:/ {print $2\" \"$3\" \"$7}'; "
            "printf '__DOCKER__\\n'; "
            f"{cls._docker_probe_command()}"
        )

    @classmethod
    def _macos_probe_command(cls) -> str:
        return (
            "printf '__MEM__\\n'; "
            "total_bytes=$(sysctl -n hw.memsize 2>/dev/null || echo 0); "
            "printf '%s 0 0\\n' $((total_bytes / 1024 / 1024)); "
            "printf '__DOCKER__\\n'; "
            f"{cls._docker_probe_command()}"
        )

    def _parse_linux_probe(self, host: HostPolicy, raw: str) -> None:
        sections = self._sections(raw)
        host.gpus = []
        for line in sections.get("GPU", []):
            parts = [part.strip() for part in line.split(",")]
            if len(parts) < 6:
                continue
            try:
                host.gpus.append(
                    {
                        "index": int(parts[0]),
                        "name": parts[1],
                        "memory_total_mb": int(parts[2]),
                        "memory_used_mb": int(parts[3]),
                        "memory_free_mb": int(parts[4]),
                        "utilization_gpu_pct": int(parts[5]),
                    }
                )
            except Exception:
                continue
        mem_lines = sections.get("MEM", [])
        if mem_lines:
            bits = mem_lines[0].split()
            if len(bits) >= 3:
                host.memory = {"total_mb": int(bits[0]), "used_mb": int(bits[1]), "available_mb": int(bits[2])}
        host.containers = self._parse_containers("\n".join(sections.get("DOCKER", [])))

    def _parse_macos_probe(self, host: HostPolicy, raw: str) -> None:
        sections = self._sections(raw)
        mem_lines = sections.get("MEM", [])
        if mem_lines:
            bits = mem_lines[0].split()
            if len(bits) >= 3:
                host.memory = {"total_mb": int(bits[0]), "used_mb": int(bits[1]), "available_mb": int(bits[2])}
        host.gpus = []
        host.containers = self._parse_containers("\n".join(sections.get("DOCKER", [])))

    @staticmethod
    def _sections(raw: str) -> Dict[str, List[str]]:
        sections: Dict[str, List[str]] = {}
        current = ""
        for raw_line in raw.splitlines():
            line = raw_line.strip()
            if line.startswith("__") and line.endswith("__"):
                current = line.strip("_")
                sections[current] = []
            elif current:
                sections.setdefault(current, []).append(line)
        return sections

    @staticmethod
    def _parse_containers(raw: str) -> Dict[str, str]:
        containers: Dict[str, str] = {}
        for line in raw.splitlines():
            if not line.strip():
                continue
            parts = line.split("\t", 1)
            if len(parts) != 2:
                continue
            containers[parts[0].strip()] = parts[1].strip()
        return containers

    def _backend_status(self, backend: BackendPolicy) -> Dict[str, Any]:
        lifecycle = self._backend_lifecycle_state(backend)
        return {
            "backend_class": backend.backend_class,
            "display_name": backend.display_name,
            "host": backend.host,
            "components": backend.components,
            "tier": backend.tier,
            "tier_rank": backend.tier_rank,
            "capabilities": backend.capabilities,
            "estimated_vram_mb": backend.estimated_vram_mb,
            "auto_start": backend.auto_start,
            "auto_stop": backend.auto_stop,
            "requires_confirmation": backend.requires_confirmation,
            "compose_managed": backend.compose_managed,
            "active": backend.active,
            "healthy": backend.healthy,
            "ready": backend.ready,
            "health_error": backend.health_error,
            "status": lifecycle["status"],
            "status_label": lifecycle["status_label"],
            "status_color": lifecycle["status_color"],
            "status_rank": lifecycle["status_rank"],
            "last_checked_at": backend.last_checked_at,
            "last_healthy_at": backend.last_healthy_at,
            "last_ready_at": backend.last_ready_at,
            "last_confirmed_working_at": backend.last_ready_at,
            "last_unhealthy_at": backend.last_unhealthy_at,
            "last_stopped_at": backend.last_stopped_at,
            "last_health_error": backend.last_health_error,
            "last_requested_at": backend.last_requested_at,
            "inflight": backend.inflight,
            "last_action": backend.last_action,
            "last_action_at": backend.last_action_at,
            "last_action_error": backend.last_action_error,
            "notes": backend.notes,
        }

    @staticmethod
    def _backend_lifecycle_state(backend: BackendPolicy) -> Dict[str, Any]:
        last_working_at = backend.last_ready_at or backend.last_healthy_at
        last_unhealthy_at = backend.last_unhealthy_at or 0.0

        if backend.active:
            if backend.ready is True:
                return {
                    "status": "active_ready",
                    "status_label": "Active and ready",
                    "status_color": "green",
                    "status_rank": 0,
                }
            if backend.ready is False or backend.healthy is False:
                return {
                    "status": "active_unhealthy",
                    "status_label": "Active but unhealthy",
                    "status_color": "red",
                    "status_rank": 3,
                }
            return {
                "status": "active_unknown",
                "status_label": "Active, not checked yet",
                "status_color": "grey",
                "status_rank": 2,
            }

        if last_working_at and last_working_at >= last_unhealthy_at:
            return {
                "status": "traded_out_working",
                "status_label": "Known working, traded out",
                "status_color": "blue",
                "status_rank": 1,
            }
        if last_unhealthy_at and last_unhealthy_at > last_working_at:
            return {
                "status": "inactive_unhealthy",
                "status_label": "Disabled after unhealthy",
                "status_color": "purple",
                "status_rank": 4,
            }
        return {
            "status": "inactive_unknown",
            "status_label": "No healthy check yet",
            "status_color": "grey",
            "status_rank": 5,
        }

    def status(self) -> Dict[str, Any]:
        return {
            "ok": True,
            "mode": self.mode,
            "generated_at": time.time(),
            "settings": {
                "poll_interval_sec": self.poll_interval_sec,
                "request_hot_window_sec": self.request_hot_window_sec,
                "optional_idle_stop_sec": self.optional_idle_stop_sec,
                "memory_pressure_used_ratio": self.memory_pressure_used_ratio,
                "target_free_vram_mb": self.target_free_vram_mb,
                "llm_advisor_enabled": self.llm_advisor_enabled,
                "llm_advisor_model": self.llm_advisor_model,
            },
            "hosts": [
                {
                    "name": host.name,
                    "ssh_target": host.ssh_target,
                    "platform": host.platform,
                    "resource_kind": host.resource_kind,
                    "error": host.error,
                    "memory": host.memory,
                    "gpus": host.gpus,
                    "containers": host.containers,
                    "updated_at": host.updated_at,
                }
                for host in sorted(self.hosts.values(), key=lambda item: item.name)
            ],
            "backends": [self._backend_status(backend) for backend in sorted(self.backends.values(), key=lambda item: item.backend_class)],
        }


manager = LifecycleManager()


@app.on_event("startup")
async def _startup() -> None:
    await manager.start()


@app.on_event("shutdown")
async def _shutdown() -> None:
    await manager.stop()


@app.get("/healthz")
def healthz() -> Dict[str, Any]:
    return {"ok": True, "time": time.time()}


@app.get("/readyz")
def readyz() -> Dict[str, Any]:
    return {"ok": True, "mode": manager.mode}


@app.get("/v1/lifecycle/status")
async def lifecycle_status(refresh: bool = False) -> Dict[str, Any]:
    if refresh:
        return await manager.refresh()
    return manager.status()


@app.post("/v1/lifecycle/ensure")
async def lifecycle_ensure(req: EnsureRequest) -> Dict[str, Any]:
    return await manager.ensure(req)


@app.post("/v1/lifecycle/action")
async def lifecycle_action(req: ActionRequest) -> Dict[str, Any]:
    return await manager.action(req)


@app.post("/v1/lifecycle/notify")
async def lifecycle_notify(req: NotifyRequest) -> Dict[str, Any]:
    return manager.notify(req)
