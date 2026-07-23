from __future__ import annotations

import asyncio
import json
import os
import re
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


def _path_env(name: str, default: str) -> str:
    value = _env(name, default).rstrip("/")
    return value or default.rstrip("/")


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


def _float_value(value: Any, default: float) -> float:
    try:
        parsed = float(value)
    except Exception:
        return default
    return parsed if parsed > 0 else default


def _int_value(value: Any, default: int) -> int:
    try:
        parsed = int(value)
    except Exception:
        return default
    return parsed if parsed > 0 else default


def _read_json(path: Path) -> Dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}


@dataclass
class HostPolicy:
    name: str
    ssh_target: str
    ssh_connect_target: str
    ssh_port: int
    repo_dir: str
    env_file: str
    platform: str
    resource_kind: str
    remote_shell: str = "bash -lc"
    error: str = ""
    os: Dict[str, Any] = field(default_factory=dict)
    cpu: Dict[str, Any] = field(default_factory=dict)
    memory: Dict[str, Any] = field(default_factory=dict)
    gpus: List[Dict[str, Any]] = field(default_factory=list)
    network_interfaces: List[Dict[str, Any]] = field(default_factory=list)
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
    health_check: str
    health_timeout_sec: float
    ready_path: str
    base_url: str
    canary_enabled: bool = False
    canary_path: str = ""
    canary_method: str = "GET"
    canary_timeout_sec: float = 0.0
    canary_payload: Dict[str, Any] = field(default_factory=dict)
    canary_failure_threshold: int = 2
    auto_restart_on_failure: bool = False
    auto_restart_cooldown_sec: float = 300.0
    auto_restart_timeout_sec: float = 120.0
    auto_restart_command: str = ""
    idle_observed_vram_mb: int = 0
    peak_observed_vram_mb: int = 0
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
    last_restart_at: float = 0.0
    drained: bool = False
    drain_reason: str = ""
    canary_consecutive_failures: int = 0
    canary_last_checked_at: float = 0.0
    canary_last_success_at: float = 0.0
    canary_last_error: str = ""
    models: List[Dict[str, Any]] = field(default_factory=list)
    models_error: str = ""
    models_checked_at: float = 0.0


@dataclass
class CoreServicePolicy:
    service_id: str
    display_name: str
    host: str
    components: List[str]
    tier: str = "core"
    notes: str = ""


class EnsureRequest(BaseModel):
    backend_class: str
    route_kind: str = ""
    reason: str = "ui"
    confirmed: bool = False
    allow_disruptive: bool = False


class EnsureCapacityRequest(BaseModel):
    backend_class: str
    route_kind: str = ""
    reason: str = "request"
    required_free_vram_mb: int = 0
    confirmed: bool = False
    allow_disruptive: bool = False
    execute: bool = True


class ActionRequest(BaseModel):
    backend_class: str
    action: str
    confirmed: bool = False
    allow_disruptive: bool = False


class MlxPrefetchRequest(BaseModel):
    backend_class: str = "local_mlx"
    model: str


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
        self.mlx_prefetch_max_attempts = max(1, _int_env("MLX_PREFETCH_MAX_ATTEMPTS", 5))
        self.mlx_prefetch_retry_base_sec = max(1, _int_env("MLX_PREFETCH_RETRY_BASE_SEC", 30))
        self.mlx_prefetch_retry_max_sec = max(
            self.mlx_prefetch_retry_base_sec,
            _int_env("MLX_PREFETCH_RETRY_MAX_SEC", 300),
        )
        self.mlx_prefetch_progress_interval_sec = max(1, _int_env("MLX_PREFETCH_PROGRESS_INTERVAL_SEC", 5))
        self.request_hot_window_sec = 900
        self.optional_idle_stop_sec = 1800
        self.memory_pressure_used_ratio = 0.9
        self.target_free_vram_mb = 4096
        self.llm_advisor_enabled = False
        self.llm_advisor_mode = "advise"
        self.llm_advisor_model = "coder"
        self.llm_advisor_base_url = "http://gateway:8800/v1"
        self.llm_advisor_timeout_sec = 20.0
        self.llm_advisor_max_context_chars = 3000
        self.llm_advisor_max_tokens = 768
        self.llm_advisor_min_confidence = 0.55
        self.model_probe_enabled = False
        self.ssh_identity_file = _env("NEXUS_LIFECYCLE_SSH_IDENTITY", "/root/.ssh/nexus_lifecycle_ed25519")
        self.hosts: Dict[str, HostPolicy] = {}
        self.backends: Dict[str, BackendPolicy] = {}
        self.core_services: Dict[str, CoreServicePolicy] = {}
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
        policy_core_services = policy.get("core_services") if isinstance(policy.get("core_services"), dict) else {}

        self.mode = self.mode or str(settings.get("mode") or "observe").strip().lower()
        self.poll_interval_sec = max(5, int(settings.get("poll_interval_sec") or self.poll_interval_sec))
        self.request_hot_window_sec = int(settings.get("request_hot_window_sec") or self.request_hot_window_sec)
        self.optional_idle_stop_sec = int(settings.get("optional_idle_stop_sec") or self.optional_idle_stop_sec)
        self.memory_pressure_used_ratio = float(settings.get("memory_pressure_used_ratio") or self.memory_pressure_used_ratio)
        self.target_free_vram_mb = int(settings.get("target_free_vram_mb") or self.target_free_vram_mb)
        llm_cfg = settings.get("llm_advisor") if isinstance(settings.get("llm_advisor"), dict) else {}
        self.llm_advisor_enabled = _bool(llm_cfg.get("enabled"))
        self.llm_advisor_mode = str(llm_cfg.get("mode") or "advise").strip().lower()
        self.llm_advisor_model = str(llm_cfg.get("model") or "coder").strip()
        self.llm_advisor_base_url = str(llm_cfg.get("base_url") or "http://gateway:8800/v1").strip().rstrip("/")
        self.llm_advisor_timeout_sec = _float_value(llm_cfg.get("timeout_sec"), self.llm_advisor_timeout_sec)
        self.llm_advisor_max_context_chars = int(llm_cfg.get("max_context_chars") or self.llm_advisor_max_context_chars)
        self.llm_advisor_max_tokens = int(llm_cfg.get("max_tokens") or self.llm_advisor_max_tokens)
        self.llm_advisor_min_confidence = float(llm_cfg.get("min_confidence") or self.llm_advisor_min_confidence)
        self.model_probe_enabled = _bool(settings.get("model_probe_enabled")) or self.llm_advisor_enabled or _bool(llm_cfg.get("include_models"))

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
                ssh_connect_target=str(
                    host_policy.get("ssh_connect_target") or topo.get("ssh_connect_target") or ""
                ).strip(),
                ssh_port=_int_value(host_policy.get("ssh_port") or topo.get("ssh_port"), 0),
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
                ssh_connect_target=str(host_policy.get("ssh_connect_target") or "").strip(),
                ssh_port=_int_value(host_policy.get("ssh_port"), 0),
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
            base_url_env_name = self._base_url_env_name(backend_class)
            base_url = str(raw_cfg.get("base_url") or _env(base_url_env_name) or default_env.get(base_url_env_name) or "").strip()
            canary_cfg = raw_cfg.get("canary") if isinstance(raw_cfg.get("canary"), dict) else {}
            canary_payload = canary_cfg.get("payload") if isinstance(canary_cfg.get("payload"), dict) else {}
            restart_cfg = raw_cfg.get("auto_restart") if isinstance(raw_cfg.get("auto_restart"), dict) else {}
            failure_threshold = _int_value(
                canary_cfg.get("failure_threshold") or restart_cfg.get("on_consecutive_failures"),
                3,
            )
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
                idle_observed_vram_mb=int(raw_cfg.get("idle_observed_vram_mb") or 0),
                peak_observed_vram_mb=int(raw_cfg.get("peak_observed_vram_mb") or 0),
                auto_start=_bool(raw_cfg.get("auto_start")),
                auto_stop=_bool(raw_cfg.get("auto_stop")),
                requires_confirmation=_bool(raw_cfg.get("requires_confirmation")),
                compose_managed=raw_cfg.get("compose_managed") is not False,
                health_check=str(raw_cfg.get("health_check") or "http").strip().lower(),
                health_timeout_sec=_float_value(raw_cfg.get("health_timeout_sec"), 10.0),
                ready_path=str(raw_cfg.get("ready_path") or "/readyz").strip(),
                base_url=base_url,
                canary_enabled=_bool(canary_cfg.get("enabled")),
                canary_path=str(canary_cfg.get("path") or canary_cfg.get("ready_path") or "").strip(),
                canary_method=str(canary_cfg.get("method") or "GET").strip().upper() or "GET",
                canary_timeout_sec=_float_value(canary_cfg.get("timeout_sec"), 0.0),
                canary_payload=canary_payload,
                canary_failure_threshold=failure_threshold,
                auto_restart_on_failure=_bool(restart_cfg.get("enabled")),
                auto_restart_cooldown_sec=_float_value(restart_cfg.get("cooldown_sec"), 300.0),
                auto_restart_timeout_sec=_float_value(restart_cfg.get("timeout_sec"), 120.0),
                auto_restart_command=str(restart_cfg.get("command") or "").strip(),
                notes=str(raw_cfg.get("notes") or "").strip(),
            )
        self.backends = backends

        core_services: Dict[str, CoreServicePolicy] = {}
        for service_id, raw_cfg in policy_core_services.items():
            if not isinstance(raw_cfg, dict):
                continue
            components = _as_list(raw_cfg.get("components")) or _as_list(raw_cfg.get("component"))
            core_services[str(service_id)] = CoreServicePolicy(
                service_id=str(service_id),
                display_name=str(raw_cfg.get("display_name") or service_id).strip(),
                host=str(raw_cfg.get("host") or "").strip(),
                components=components,
                tier=str(raw_cfg.get("tier") or "core").strip().lower(),
                notes=str(raw_cfg.get("notes") or "").strip(),
            )
        self.core_services = core_services

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
            "last_restart_at",
            "canary_last_checked_at",
            "canary_last_success_at",
        }
        str_fields = {"last_action", "last_action_error", "last_health_error", "drain_reason", "canary_last_error"}
        int_fields = {"canary_consecutive_failures"}
        bool_fields = {"drained"}
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
            for field_name in int_fields:
                try:
                    setattr(backend, field_name, int(state.get(field_name) or 0))
                except Exception:
                    continue
            for field_name in bool_fields:
                setattr(backend, field_name, _bool(state.get(field_name)))

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
                    "last_restart_at": backend.last_restart_at,
                    "drained": backend.drained,
                    "drain_reason": backend.drain_reason,
                    "canary_consecutive_failures": backend.canary_consecutive_failures,
                    "canary_last_checked_at": backend.canary_last_checked_at,
                    "canary_last_success_at": backend.canary_last_success_at,
                    "canary_last_error": backend.canary_last_error,
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
            "ltx_video": "LTX_VIDEO_ADVERTISE_BASE_URL",
            "hunyuan_video": "HUNYUAN_VIDEO_ADVERTISE_BASE_URL",
            "ace_step_music": "ACE_STEP_ADVERTISE_BASE_URL",
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
            if host.resource_kind in {"linux", "linux_nvidia", "linux_infra"}:
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
            canary_tasks = [self._check_backend_canary(client, backend) for backend in self.backends.values() if backend.canary_enabled]
            if canary_tasks:
                await asyncio.gather(*canary_tasks, return_exceptions=True)
            if self.model_probe_enabled:
                model_tasks = [self._check_backend_models(client, backend) for backend in self.backends.values()]
                if model_tasks:
                    await asyncio.gather(*model_tasks, return_exceptions=True)

    async def _check_backend_health(self, client: httpx.AsyncClient, backend: BackendPolicy) -> None:
        if backend.compose_managed and not backend.active:
            backend.healthy = None
            backend.ready = False
            backend.health_error = ""
            backend.drained = False
            backend.drain_reason = ""
            return

        now = time.time()
        if backend.health_check in {"container", "none"}:
            backend.last_checked_at = now
            backend.healthy = backend.active
            backend.ready = backend.active
            backend.health_error = "" if backend.active else "container is not running"
            backend.drained = False if backend.active else backend.drained
            backend.drain_reason = "" if backend.active else backend.drain_reason
            if backend.active:
                backend.last_healthy_at = now
                backend.last_ready_at = now
                backend.last_health_error = ""
            else:
                backend.last_unhealthy_at = now
                backend.last_health_error = backend.health_error
            return

        base_url = backend.base_url.rstrip("/")
        if not base_url:
            backend.healthy = None
            backend.ready = None
            backend.health_error = "base_url not configured"
            backend.last_checked_at = now
            backend.drained = False
            backend.drain_reason = ""
            if backend.active:
                backend.last_unhealthy_at = now
                backend.last_health_error = backend.health_error
            return
        path = backend.ready_path or "/readyz"
        if not path.startswith("/"):
            path = "/" + path
        try:
            timeout = httpx.Timeout(
                connect=5.0,
                read=backend.health_timeout_sec,
                write=5.0,
                pool=5.0,
            )
            response = await client.get(f"{base_url}{path}", timeout=timeout)
            backend.last_checked_at = now
            backend.healthy = response.status_code < 500
            backend.ready = response.status_code == 200
            backend.health_error = "" if response.status_code == 200 else f"HTTP {response.status_code}"
            if response.status_code == 200:
                backend.drained = False
                backend.drain_reason = ""
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
            backend.drained = False
            backend.drain_reason = ""
            backend.last_unhealthy_at = now
            backend.last_health_error = backend.health_error

    @staticmethod
    def _canary_url(backend: BackendPolicy) -> str:
        base_url = backend.base_url.rstrip("/")
        if not base_url:
            return ""
        path = backend.canary_path or backend.ready_path or ""
        if not path.startswith("/"):
            path = "/" + path
        return f"{base_url}{path}"

    @staticmethod
    def _response_error_text(response: httpx.Response) -> str:
        try:
            detail = (response.text or "").strip()
        except Exception:
            detail = ""
        if detail:
            detail = detail.replace("\n", " ")[:600]
            return f"HTTP {response.status_code}: {detail}"
        return f"HTTP {response.status_code}"

    @staticmethod
    def _canary_response_ok(backend: BackendPolicy, response: httpx.Response) -> bool:
        if response.status_code != 200:
            return False
        if backend.canary_method != "POST":
            return True
        try:
            payload = response.json()
        except Exception:
            return False
        choices = payload.get("choices") if isinstance(payload, dict) else None
        return isinstance(choices, list) and len(choices) > 0

    def _mark_backend_canary_success(self, backend: BackendPolicy, now: float) -> None:
        backend.canary_consecutive_failures = 0
        backend.canary_last_checked_at = now
        backend.canary_last_success_at = now
        backend.canary_last_error = ""
        backend.drained = False
        backend.drain_reason = ""
        backend.ready = True
        backend.last_ready_at = now
        backend.last_healthy_at = now
        if backend.last_health_error == backend.health_error:
            backend.last_health_error = ""
        backend.health_error = ""

    async def _check_backend_canary(self, client: httpx.AsyncClient, backend: BackendPolicy) -> None:
        now = time.time()
        if not backend.canary_enabled:
            return
        if not backend.base_url or not backend.canary_path:
            backend.canary_last_checked_at = now
            backend.canary_last_error = "canary is enabled but not configured"
            return
        if backend.healthy is False or backend.ready is not True:
            backend.canary_last_checked_at = now
            return

        timeout_sec = backend.canary_timeout_sec or backend.health_timeout_sec or 5.0
        timeout = httpx.Timeout(connect=2.0, read=timeout_sec, write=2.0, pool=2.0)
        url = self._canary_url(backend)
        error = ""
        try:
            request_kwargs: Dict[str, Any] = {"timeout": timeout}
            if backend.canary_method == "POST":
                request_kwargs["json"] = backend.canary_payload
            response = await client.request(backend.canary_method, url, **request_kwargs)
            if not self._canary_response_ok(backend, response):
                error = self._response_error_text(response)
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"

        if not error:
            self._mark_backend_canary_success(backend, now)
            return

        backend.canary_consecutive_failures += 1
        backend.canary_last_checked_at = now
        backend.canary_last_error = error
        if backend.canary_consecutive_failures < backend.canary_failure_threshold:
            return
        backend.drained = True
        backend.drain_reason = f"active canary failed: {error}"
        backend.ready = False
        backend.health_error = backend.drain_reason
        backend.last_unhealthy_at = now
        backend.last_health_error = backend.drain_reason
        await self._maybe_auto_restart_backend(backend, reason=backend.drain_reason)

    @staticmethod
    def _models_url_candidates(backend: BackendPolicy) -> List[str]:
        base_url = backend.base_url.rstrip("/")
        if not base_url:
            return []
        paths: List[str] = []
        ready_path = backend.ready_path or ""
        if ready_path.endswith("/models"):
            paths.append(ready_path if ready_path.startswith("/") else "/" + ready_path)
        paths.append("/models")
        if not base_url.endswith("/v1"):
            paths.append("/v1/models")

        urls: List[str] = []
        seen: set[str] = set()
        for path in paths:
            url = f"{base_url}{path}"
            if url in seen:
                continue
            seen.add(url)
            urls.append(url)
        return urls

    @staticmethod
    def _coerce_model_items(payload: Any) -> List[Dict[str, Any]]:
        data = payload.get("data") if isinstance(payload, dict) else payload
        if not isinstance(data, list):
            return []
        out: List[Dict[str, Any]] = []
        for item in data[:20]:
            if isinstance(item, dict):
                model_id = str(item.get("id") or item.get("name") or "").strip()
                if not model_id:
                    continue
                out.append(
                    {
                        "id": model_id,
                        "object": str(item.get("object") or "").strip(),
                        "owned_by": str(item.get("owned_by") or item.get("provider") or "").strip(),
                    }
                )
            elif isinstance(item, str) and item.strip():
                out.append({"id": item.strip(), "object": "", "owned_by": ""})
        return out

    async def _check_backend_models(self, client: httpx.AsyncClient, backend: BackendPolicy) -> None:
        if not backend.active or backend.ready is not True or not backend.base_url:
            backend.models = []
            backend.models_error = ""
            backend.models_checked_at = 0.0
            return

        last_error = ""
        timeout = httpx.Timeout(connect=2.0, read=3.0, write=2.0, pool=2.0)
        for url in self._models_url_candidates(backend):
            try:
                response = await client.get(url, timeout=timeout)
                if response.status_code == 404:
                    last_error = "HTTP 404"
                    continue
                if response.status_code >= 400:
                    last_error = f"HTTP {response.status_code}"
                    continue
                backend.models = self._coerce_model_items(response.json())
                backend.models_error = ""
                backend.models_checked_at = time.time()
                return
            except Exception as exc:
                last_error = f"{type(exc).__name__}: {exc}"
        backend.models = []
        backend.models_error = last_error
        backend.models_checked_at = time.time()

    @staticmethod
    def _component_container_active(name: str, expected_name: str) -> bool:
        if name == expected_name:
            return True
        if not name.startswith(expected_name + "-"):
            return False
        suffix = name[len(expected_name) + 1 :]
        return not suffix.startswith("registrar")

    @staticmethod
    def _container_status_ready(status: str) -> bool:
        normalized = str(status or "").strip().lower()
        if not normalized.startswith("up"):
            return False
        return "unhealthy" not in normalized and "health: starting" not in normalized

    def _refresh_active_flags(self) -> None:
        for backend in self.backends.values():
            host = self.hosts.get(backend.host)
            if host is None:
                backend.active = False
                continue
            if not backend.compose_managed:
                backend.active = bool(backend.base_url) and backend.last_checked_at > 0 and backend.healthy is not False
                continue
            expected = [f"nexus-{component}" for component in backend.components]
            if not expected:
                backend.active = False
                continue
            was_active = backend.active
            backend.active = all(
                any(self._component_container_active(name, expected_name) for name in host.containers)
                for expected_name in expected
            )
            if was_active and not backend.active:
                backend.last_stopped_at = time.time()

    async def ensure(self, req: EnsureRequest) -> Dict[str, Any]:
        backend = self._backend_or_404(req.backend_class)
        backend.last_requested_at = time.time()
        if backend.active and backend.ready is not False:
            return {"ok": True, "decision": "already_active", "backend": self._backend_status(backend)}
        plan = self._activation_plan(backend, confirmed=req.confirmed, allow_disruptive=req.allow_disruptive)
        plan = await self._maybe_apply_llm_decision(
            plan,
            purpose="activation",
            backend=backend,
            route_kind=req.route_kind,
            reason=req.reason,
            confirmed=req.confirmed,
            allow_disruptive=req.allow_disruptive,
            required_free_vram_mb=0,
        )
        if plan["decision"] in {"requires_confirmation", "blocked", "observe_only"}:
            return await self._attach_llm_advice(plan)
        if self.mode not in {"assisted", "auto"} and not req.confirmed:
            plan["decision"] = "observe_only"
            plan["message"] = "Lifecycle manager is not in assisted/auto mode."
            return await self._attach_llm_advice(plan)
        await self._execute_plan(plan)
        await self.refresh()
        return plan

    async def ensure_capacity(self, req: EnsureCapacityRequest) -> Dict[str, Any]:
        await self.refresh()
        backend = self._backend_or_404(req.backend_class)
        backend.last_requested_at = time.time()
        required_free_vram_mb = self._required_free_vram_mb(backend, req.required_free_vram_mb)
        plan = self._capacity_plan(
            backend,
            required_free_vram_mb=required_free_vram_mb,
            confirmed=req.confirmed,
            allow_disruptive=req.allow_disruptive,
        )
        plan["route_kind"] = req.route_kind
        plan["reason"] = req.reason
        plan = await self._maybe_apply_llm_decision(
            plan,
            purpose="capacity",
            backend=backend,
            route_kind=req.route_kind,
            reason=req.reason,
            confirmed=req.confirmed,
            allow_disruptive=req.allow_disruptive,
            required_free_vram_mb=required_free_vram_mb,
        )
        if plan.get("decision") in {"requires_confirmation", "blocked", "observe_only"}:
            return await self._attach_llm_advice(plan)
        if not req.execute or not (plan.get("start") or plan.get("stop")):
            return plan
        if self.mode not in {"assisted", "auto"} and not req.confirmed:
            plan["ok"] = False
            plan["decision"] = "observe_only"
            plan["message"] = "Lifecycle manager is not in assisted/auto mode."
            return await self._attach_llm_advice(plan)
        await self._execute_plan(plan)
        await self.refresh()
        backend = self._backend_or_404(req.backend_class)
        plan["ok"] = True
        plan["decision"] = "capacity_freed" if plan.get("stop") else "capacity_ready"
        plan["backend"] = self._backend_status(backend)
        return plan

    async def action(self, req: ActionRequest) -> Dict[str, Any]:
        backend = self._backend_or_404(req.backend_class)
        action = req.action.strip().lower()
        if action not in {"activate", "start", "deactivate", "stop", "restart"}:
            raise HTTPException(status_code=400, detail="action must be activate/start/deactivate/stop/restart")
        if action == "restart":
            await self._restart_backend(backend, reason="manual restart", automatic=False)
            await self.refresh()
            return {"ok": True, "decision": "restart", "backend": self._backend_status(backend)}
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

    async def prefetch_mlx_model(self, req: MlxPrefetchRequest) -> Dict[str, Any]:
        backend = self._backend_or_404(req.backend_class)
        if backend.backend_class != "local_mlx":
            raise HTTPException(status_code=400, detail="MLX prefetch is only supported for local_mlx")
        model = req.model.strip()
        if not model:
            raise HTTPException(status_code=400, detail="model is required")
        if any(ch in model for ch in ("\x00", "\n", "\r")):
            raise HTTPException(status_code=400, detail="model contains invalid characters")
        host = self.hosts.get(backend.host)
        if host is None:
            raise HTTPException(status_code=400, detail=f"unknown host {backend.host}")

        safe_name = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in model)[:160] or "model"
        mlx_root = _path_env("MLX_NATIVE_ROOT", "/var/lib/mlx")
        log_path = f"{mlx_root}/logs/prefetch-{safe_name}.log"
        prefetch_script = f"{host.repo_dir}/services/mlx/scripts/prefetch-models.sh"
        max_attempts = max(1, int(getattr(self, "mlx_prefetch_max_attempts", 5)))
        retry_base_sec = max(1, int(getattr(self, "mlx_prefetch_retry_base_sec", 30)))
        retry_max_sec = max(retry_base_sec, int(getattr(self, "mlx_prefetch_retry_max_sec", 300)))
        progress_interval_sec = max(1, int(getattr(self, "mlx_prefetch_progress_interval_sec", 5)))
        inner_command = (
            f"{shlex.quote(prefetch_script)} --model {shlex.quote(model)} "
            f"--max-attempts {max_attempts} --retry-base-sec {retry_base_sec} "
            f"--retry-max-sec {retry_max_sec} --progress-interval-sec {progress_interval_sec} "
            f">>{shlex.quote(log_path)} 2>&1 </dev/null & "
            "pid=$!; disown \"$pid\" 2>/dev/null || true; echo \"$pid\""
        )
        command = (
            f"sudo -n install -d -o mlx -m 775 {shlex.quote(mlx_root)}/logs; "
            f"if [ ! -x {shlex.quote(prefetch_script)} ]; then "
            "echo 'repository MLX prefetch helper not found' >&2; exit 127; "
            "fi; "
            "sudo -n -H -u mlx env "
            f"MLX_ENV_FILE={shlex.quote(mlx_root)}/mlx.env "
            f"MLX_VENV={shlex.quote(mlx_root)}/env "
            f"/bin/bash -lc {shlex.quote(inner_command)}"
        )
        backend.last_action = "prefetch"
        backend.last_action_at = time.time()
        try:
            stdout = await self._ssh(host, command, timeout=30)
        except Exception as exc:
            backend.last_action_error = f"{type(exc).__name__}: {exc}"
            self._save_state()
            raise
        backend.last_action_error = ""
        self._save_state()
        return {
            "ok": True,
            "decision": "prefetch_started",
            "backend_class": backend.backend_class,
            "host": backend.host,
            "model": model,
            "pid": (stdout or "").strip().splitlines()[-1] if (stdout or "").strip() else "",
            "log_path": log_path,
            "max_attempts": max_attempts,
            "retry_base_sec": retry_base_sec,
            "retry_max_sec": retry_max_sec,
            "progress_interval_sec": progress_interval_sec,
            "backend": self._backend_status(backend),
        }

    async def purge_mlx_model_cache(self, req: MlxPrefetchRequest) -> Dict[str, Any]:
        backend = self._backend_or_404(req.backend_class)
        if backend.backend_class != "local_mlx":
            raise HTTPException(status_code=400, detail="MLX cache purge is only supported for local_mlx")
        model = req.model.strip()
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*", model):
            raise HTTPException(status_code=400, detail="model must be a Hugging Face repository id in ORG/REPO form")
        host = self.hosts.get(backend.host)
        if host is None:
            raise HTTPException(status_code=400, detail=f"unknown host {backend.host}")

        mlx_root = _path_env("MLX_NATIVE_ROOT", "/var/lib/mlx")
        command = (
            f"cd {shlex.quote(host.repo_dir)} && "
            f"MLX_NATIVE_ROOT={shlex.quote(mlx_root)} "
            f"./deploy/scripts/purge-mlx-model-cache.sh --model {shlex.quote(model)}"
        )
        backend.last_action = "purge_cache"
        backend.last_action_at = time.time()
        try:
            stdout = await self._ssh(host, command, timeout=600)
        except Exception as exc:
            backend.last_action_error = f"{type(exc).__name__}: {exc}"
            self._save_state()
            raise

        removed_paths = [
            line.split("=", 1)[1]
            for line in (stdout or "").splitlines()
            if line.startswith("removed_path=")
        ]
        backend.last_action_error = ""
        self._save_state()
        return {
            "ok": True,
            "decision": "cache_purged",
            "backend_class": backend.backend_class,
            "host": backend.host,
            "model": model,
            "removed_paths": removed_paths,
            "backend": self._backend_status(backend),
        }

    async def redownload_mlx_model(self, req: MlxPrefetchRequest) -> Dict[str, Any]:
        purge = await self.purge_mlx_model_cache(req)
        prefetch = await self.prefetch_mlx_model(req)
        return {
            "ok": True,
            "decision": "redownload_started",
            "model": req.model.strip(),
            "purge": purge,
            "prefetch": prefetch,
            "pid": prefetch.get("pid", ""),
            "log_path": prefetch.get("log_path", ""),
        }

    async def switch_mlx_huge_model(self, req: MlxPrefetchRequest) -> Dict[str, Any]:
        backend = self._backend_or_404(req.backend_class)
        if backend.backend_class != "local_mlx":
            raise HTTPException(status_code=400, detail="MLX Huge switching is only supported for local_mlx")
        model = req.model.strip()
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*", model):
            raise HTTPException(status_code=400, detail="model must be a Hugging Face repository id in ORG/REPO form")
        host = self.hosts.get(backend.host)
        if host is None:
            raise HTTPException(status_code=400, detail=f"unknown host {backend.host}")

        mlx_root = _path_env("MLX_NATIVE_ROOT", "/var/lib/mlx")
        command = (
            f"cd {shlex.quote(host.repo_dir)} && "
            f"MLX_NATIVE_ROOT={shlex.quote(mlx_root)} "
            f"./deploy/scripts/switch-mlx-huge-model.sh --model {shlex.quote(model)} --timeout-sec 3600"
        )
        backend.last_action = "switch_resident_huge_model"
        backend.last_action_at = time.time()
        try:
            stdout = await self._ssh(host, command, timeout=3900)
        except Exception as exc:
            backend.last_action_error = f"{type(exc).__name__}: {exc}"
            self._save_state()
            raise

        backend.last_action_error = ""
        self._save_state()
        await self.refresh()
        return {
            "ok": True,
            "decision": "resident_huge_model_ready",
            "backend_class": backend.backend_class,
            "host": backend.host,
            "model": model,
            "stdout": (stdout or "").strip(),
            "backend": self._backend_status(backend),
        }

    async def sync_mlx_cache_status(self) -> Dict[str, Any]:
        backend = self._backend_or_404("local_mlx")
        host = self.hosts.get(backend.host)
        if host is None:
            raise HTTPException(status_code=400, detail=f"unknown host {backend.host}")

        command = (
            f"cd {shlex.quote(host.repo_dir)} && "
            "if [ ! -x deploy/scripts/sync-mlx-cache-status.sh ]; then "
            "echo 'sync-mlx-cache-status.sh helper not found' >&2; exit 127; "
            "fi; "
            "./deploy/scripts/sync-mlx-cache-status.sh"
        )
        try:
            stdout = await self._ssh(host, command, timeout=30)
        except Exception as exc:
            raise HTTPException(status_code=503, detail=f"MLX cache status sync failed: {exc}") from exc
        return {
            "ok": True,
            "decision": "cache_status_synced",
            "backend_class": backend.backend_class,
            "host": backend.host,
            "stdout": stdout.strip(),
        }

    def notify(self, req: NotifyRequest) -> Dict[str, Any]:
        backend = self._backend_or_404(req.backend_class)
        event = req.event.strip().lower()
        backend.last_requested_at = time.time()
        if event in {"start", "started", "acquire"}:
            backend.inflight += 1
        elif event in {"finish", "finished", "release", "end", "error"}:
            backend.inflight = max(0, backend.inflight - 1)
        return {"ok": True, "backend": self._backend_status(backend)}

    @staticmethod
    def _required_free_vram_mb(backend: BackendPolicy, requested_mb: int = 0) -> int:
        return max(
            0,
            int(requested_mb or 0),
            int(backend.peak_observed_vram_mb or 0),
            int(backend.estimated_vram_mb or 0),
        )

    @staticmethod
    def _freed_vram_mb(candidate: BackendPolicy) -> int:
        return max(0, int(candidate.idle_observed_vram_mb or candidate.estimated_vram_mb or 0))

    def _capacity_stop_candidates(self, backend: BackendPolicy, *, confirmed: bool, allow_disruptive: bool) -> List[BackendPolicy]:
        candidates: List[BackendPolicy] = []
        for candidate in self._same_host_candidates(backend):
            if candidate.backend_class == backend.backend_class or not candidate.active:
                continue
            if candidate.inflight > 0:
                continue
            safe_optional = candidate.tier == "optional" and candidate.auto_stop and not candidate.requires_confirmation
            if not allow_disruptive:
                if not safe_optional:
                    continue
            else:
                if candidate.tier == "crucial" and not confirmed:
                    continue
                if candidate.requires_confirmation and not confirmed:
                    continue
                if not candidate.auto_stop and not confirmed:
                    continue
            candidates.append(candidate)
        candidates.sort(
            key=lambda item: (
                0 if item.tier == "optional" and item.auto_stop else 1,
                item.tier_rank,
                item.last_requested_at or 0,
                -self._freed_vram_mb(item),
            )
        )
        return candidates

    def _capacity_plan(
        self,
        backend: BackendPolicy,
        *,
        required_free_vram_mb: int,
        confirmed: bool,
        allow_disruptive: bool,
    ) -> Dict[str, Any]:
        host = self.hosts.get(backend.host)
        if host is None:
            return {"ok": False, "decision": "blocked", "message": f"Unknown host {backend.host}", "backend": self._backend_status(backend)}

        target_free_vram_mb = required_free_vram_mb + (self.target_free_vram_mb if required_free_vram_mb > 0 else 0)
        free_vram_mb = self._host_free_vram(host)
        needed_vram_mb = max(0, target_free_vram_mb - free_vram_mb)
        start_items: List[str] = []

        if not backend.active:
            if not backend.compose_managed:
                return {
                    "ok": False,
                    "decision": "blocked",
                    "message": "Backend is not active and is not compose-managed by the lifecycle manager.",
                    "backend": self._backend_status(backend),
                    "required_free_vram_mb": required_free_vram_mb,
                    "target_free_vram_mb": target_free_vram_mb,
                    "free_vram_mb": free_vram_mb,
                    "needed_vram_mb": needed_vram_mb,
                }
            if backend.requires_confirmation and not confirmed:
                return {
                    "ok": False,
                    "decision": "requires_confirmation",
                    "message": "This backend is marked as requiring operator confirmation.",
                    "backend": self._backend_status(backend),
                    "conflicts": self._same_host_active(backend),
                    "required_free_vram_mb": required_free_vram_mb,
                    "target_free_vram_mb": target_free_vram_mb,
                    "free_vram_mb": free_vram_mb,
                    "needed_vram_mb": needed_vram_mb,
                }
            if not backend.auto_start and not confirmed:
                return {
                    "ok": False,
                    "decision": "requires_confirmation",
                    "message": "Policy disables automatic start for this backend.",
                    "backend": self._backend_status(backend),
                    "conflicts": self._same_host_active(backend),
                    "required_free_vram_mb": required_free_vram_mb,
                    "target_free_vram_mb": target_free_vram_mb,
                    "free_vram_mb": free_vram_mb,
                    "needed_vram_mb": needed_vram_mb,
                }
            start_items = [backend.backend_class]

        if needed_vram_mb <= 0:
            return {
                "ok": True,
                "decision": "capacity_ready",
                "start": start_items,
                "stop": [],
                "backend": self._backend_status(backend),
                "required_free_vram_mb": required_free_vram_mb,
                "target_free_vram_mb": target_free_vram_mb,
                "free_vram_mb": free_vram_mb,
                "needed_vram_mb": 0,
            }

        victims: List[BackendPolicy] = []
        freed_vram_mb = 0
        for candidate in self._capacity_stop_candidates(backend, confirmed=confirmed, allow_disruptive=allow_disruptive):
            victims.append(candidate)
            freed_vram_mb += self._freed_vram_mb(candidate)
            if freed_vram_mb >= needed_vram_mb:
                break

        if freed_vram_mb >= needed_vram_mb:
            return {
                "ok": True,
                "decision": "capacity_swap",
                "start": start_items,
                "stop": [victim.backend_class for victim in victims],
                "backend": self._backend_status(backend),
                "conflicts": [self._backend_status(victim) for victim in victims],
                "required_free_vram_mb": required_free_vram_mb,
                "target_free_vram_mb": target_free_vram_mb,
                "free_vram_mb": free_vram_mb,
                "needed_vram_mb": needed_vram_mb,
                "freed_vram_mb": freed_vram_mb,
            }

        return {
            "ok": False,
            "decision": "requires_confirmation",
            "message": "Insufficient free VRAM for this request.",
            "start": start_items,
            "stop": [victim.backend_class for victim in victims],
            "backend": self._backend_status(backend),
            "conflicts": self._same_host_active(backend),
            "required_free_vram_mb": required_free_vram_mb,
            "target_free_vram_mb": target_free_vram_mb,
            "free_vram_mb": free_vram_mb,
            "needed_vram_mb": needed_vram_mb,
            "freed_vram_mb": freed_vram_mb,
        }

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

    def _llm_decision_enabled(self) -> bool:
        if not self.llm_advisor_enabled:
            return False
        return self.llm_advisor_mode in {"decide", "decision", "plan", "assist", "assisted"}

    @staticmethod
    def _compact_models(models: List[Dict[str, Any]]) -> List[Dict[str, str]]:
        out: List[Dict[str, str]] = []
        for item in models[:8]:
            model_id = str(item.get("id") or "").strip()
            if not model_id:
                continue
            out.append({"id": model_id})
        return out

    def _compact_backend_for_llm(self, backend: BackendPolicy) -> Dict[str, Any]:
        return {
            "backend_class": backend.backend_class,
            "host": backend.host,
            "components": backend.components,
            "tier": backend.tier,
            "tier_rank": backend.tier_rank,
            "capabilities": backend.capabilities,
            "estimated_vram_mb": backend.estimated_vram_mb,
            "idle_observed_vram_mb": backend.idle_observed_vram_mb,
            "peak_observed_vram_mb": backend.peak_observed_vram_mb,
            "auto_start": backend.auto_start,
            "auto_stop": backend.auto_stop,
            "requires_confirmation": backend.requires_confirmation,
            "compose_managed": backend.compose_managed,
            "active": backend.active,
            "ready": backend.ready,
            "inflight": backend.inflight,
            "models": self._compact_models(backend.models),
            "models_error": backend.models_error,
            "notes": backend.notes[:180],
        }

    def _host_for_llm(self, host: Optional[HostPolicy]) -> Dict[str, Any]:
        if host is None:
            return {}
        total, used, free = self._host_vram_tuple(host)
        gpus = [
            {
                "index": gpu.get("index"),
                "name": gpu.get("name"),
                "memory_total_mb": gpu.get("memory_total_mb"),
                "memory_used_mb": gpu.get("memory_used_mb"),
                "memory_free_mb": gpu.get("memory_free_mb"),
                "utilization_gpu_pct": gpu.get("utilization_gpu_pct"),
            }
            for gpu in host.gpus[:8]
        ]
        containers = [
            {"name": name, "status": status[:120]}
            for name, status in sorted(host.containers.items())
            if name.startswith("nexus-")
        ][:40]
        return {
            "name": host.name,
            "resource_kind": host.resource_kind,
            "error": host.error,
            "memory": host.memory,
            "gpus": gpus,
            "network_interfaces": host.network_interfaces[:8],
            "vram": {"total_mb": total, "used_mb": used, "free_mb": free},
            "container_count": len(host.containers),
            "containers": containers[:12],
        }

    def _compact_plan_for_llm(self, plan: Dict[str, Any]) -> Dict[str, Any]:
        out: Dict[str, Any] = {
            "ok": plan.get("ok"),
            "decision": plan.get("decision"),
            "message": plan.get("message"),
            "start": plan.get("start") or [],
            "stop": plan.get("stop") or [],
            "required_free_vram_mb": plan.get("required_free_vram_mb"),
            "target_free_vram_mb": plan.get("target_free_vram_mb"),
            "free_vram_mb": plan.get("free_vram_mb"),
            "needed_vram_mb": plan.get("needed_vram_mb"),
            "freed_vram_mb": plan.get("freed_vram_mb"),
        }
        conflicts = plan.get("conflicts")
        if isinstance(conflicts, list):
            out["conflicts"] = [
                {
                    "backend_class": item.get("backend_class"),
                    "host": item.get("host"),
                    "tier": item.get("tier"),
                    "estimated_vram_mb": item.get("estimated_vram_mb"),
                    "idle_observed_vram_mb": item.get("idle_observed_vram_mb"),
                    "auto_stop": item.get("auto_stop"),
                    "requires_confirmation": item.get("requires_confirmation"),
                    "active": item.get("active"),
                    "ready": item.get("ready"),
                    "inflight": item.get("inflight"),
                }
                for item in conflicts
                if isinstance(item, dict)
            ]
        return out

    def _llm_context(
        self,
        *,
        plan: Dict[str, Any],
        purpose: str,
        backend: BackendPolicy,
        route_kind: str,
        reason: str,
        confirmed: bool,
        allow_disruptive: bool,
        required_free_vram_mb: int,
    ) -> Dict[str, Any]:
        host = self.hosts.get(backend.host)
        same_host = [candidate for candidate in self._same_host_candidates(backend)]
        return {
            "task": purpose,
            "mode": self.mode,
            "request": {
                "backend_class": backend.backend_class,
                "route_kind": route_kind,
                "reason": reason,
                "confirmed": confirmed,
                "allow_disruptive": allow_disruptive,
                "required_free_vram_mb": required_free_vram_mb,
                "target_free_vram_mb": plan.get("target_free_vram_mb"),
                "free_vram_mb": plan.get("free_vram_mb"),
                "needed_vram_mb": plan.get("needed_vram_mb"),
            },
            "deterministic_plan": self._compact_plan_for_llm(plan),
            "target_backend": self._compact_backend_for_llm(backend),
            "host": self._host_for_llm(host),
            "same_host_backends": [self._compact_backend_for_llm(candidate) for candidate in same_host],
            "guardrails": (
                "JSON only. Use same_host backend_class IDs. Never stop inflight backends. "
                "Without confirmation, stop only optional auto_stop backends. Start only requested backend."
            ),
            "expected_response": "{recommendation,decision,start,stop,confidence,rationale,risks}",
        }

    @staticmethod
    def _extract_json_object(text: str) -> Optional[Dict[str, Any]]:
        raw = (text or "").strip()
        if not raw:
            return None
        try:
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, dict) else None
        except Exception:
            pass

        start = raw.find("{")
        end = raw.rfind("}")
        if start < 0 or end <= start:
            return None
        try:
            parsed = json.loads(raw[start : end + 1])
            return parsed if isinstance(parsed, dict) else None
        except Exception:
            return None

    async def _call_llm_planner(self, context: Dict[str, Any]) -> Dict[str, Any]:
        if not self.llm_advisor_base_url or not self.llm_advisor_model:
            return {"error": "llm planner is not configured"}

        context_text = json.dumps(context, ensure_ascii=False, separators=(",", ":"))[: self.llm_advisor_max_context_chars]
        messages = [
            {
                "role": "system",
                "content": (
                    "You are a fast local lifecycle planning assistant for Nexus AI backends. "
                    "Choose a minimal backend start/stop plan from the provided policy and live resource state. "
                    "Never ignore guardrails. Do not include analysis. The first character of your reply must be {. "
                    "Return only compact JSON."
                ),
            },
            {"role": "user", "content": context_text},
        ]
        headers = {}
        token = _env("GATEWAY_BEARER_TOKEN")
        if token:
            headers["Authorization"] = f"Bearer {token}"
        try:
            async with httpx.AsyncClient(timeout=self.llm_advisor_timeout_sec) as client:
                response = await client.post(
                    f"{self.llm_advisor_base_url}/chat/completions",
                    headers=headers,
                    json={
                        "model": self.llm_advisor_model,
                        "messages": messages,
                        "temperature": 0,
                        "max_tokens": self.llm_advisor_max_tokens,
                    },
                )
            if response.status_code >= 400:
                return {"error": f"HTTP {response.status_code}: {response.text[:1000]}", "prompt_chars": len(context_text)}
            payload = response.json()
            content = ""
            finish_reason = ""
            choices = payload.get("choices") if isinstance(payload, dict) else None
            if isinstance(choices, list) and choices:
                finish_reason = str(choices[0].get("finish_reason") or "")
                msg = choices[0].get("message") if isinstance(choices[0], dict) else None
                if isinstance(msg, dict):
                    content = str(msg.get("content") or "")
            parsed = self._extract_json_object(content)
            if parsed is None:
                return {
                    "error": "llm returned non-json content",
                    "content": content[:2000],
                    "finish_reason": finish_reason,
                    "prompt_chars": len(context_text),
                }
            return {"content": content[:4000], "proposal": parsed, "finish_reason": finish_reason, "prompt_chars": len(context_text)}
        except Exception as exc:
            return {"error": f"{type(exc).__name__}: {exc}"}

    @staticmethod
    def _list_field(value: Any) -> List[str]:
        if isinstance(value, list):
            return [str(item).strip() for item in value if str(item).strip()]
        if isinstance(value, str) and value.strip():
            return [item.strip() for item in value.split(",") if item.strip()]
        return []

    def _validate_llm_proposal(
        self,
        proposal: Dict[str, Any],
        *,
        base_plan: Dict[str, Any],
        purpose: str,
        backend: BackendPolicy,
        confirmed: bool,
        allow_disruptive: bool,
        required_free_vram_mb: int,
    ) -> tuple[Optional[Dict[str, Any]], str]:
        recommendation = str(proposal.get("recommendation") or "").strip().lower()
        decision_hint = str(proposal.get("decision") or "").strip().lower()
        if not recommendation and decision_hint in {"requires_confirmation", "blocked"}:
            recommendation = decision_hint
        if not recommendation and (proposal.get("start") or proposal.get("stop") or decision_hint):
            recommendation = "apply_plan"
        if recommendation in {"", "use_deterministic", "deterministic", "no_change"}:
            return None, "llm chose deterministic plan"
        if recommendation in {"requires_confirmation", "blocked"}:
            return None, f"llm recommended {recommendation}"

        requested_start = self._list_field(proposal.get("start"))
        requested_stop = self._list_field(proposal.get("stop"))
        start_items: List[str] = []
        stop_items: List[str] = []

        for item in requested_start:
            if item != backend.backend_class:
                return None, f"llm tried to start unrelated backend {item}"
            if item not in start_items:
                start_items.append(item)
        if backend.active and start_items:
            return None, "llm tried to start an already-active backend"
        if not backend.active and not start_items and purpose in {"activation", "capacity"}:
            start_items.append(backend.backend_class)

        for item in requested_stop:
            if item in stop_items:
                continue
            candidate = self.backends.get(item)
            if candidate is None:
                return None, f"llm requested unknown stop backend {item}"
            if candidate.backend_class == backend.backend_class:
                return None, "llm tried to stop the requested backend"
            if candidate.host != backend.host:
                return None, f"llm tried to stop backend on another host: {item}"
            if not candidate.active:
                return None, f"llm tried to stop inactive backend {item}"
            if candidate.inflight > 0:
                return None, f"llm tried to stop inflight backend {item}"
            safe_optional = candidate.tier == "optional" and candidate.auto_stop and not candidate.requires_confirmation
            if not allow_disruptive and not safe_optional:
                return None, f"llm requested disruptive stop without permission: {item}"
            if allow_disruptive:
                if candidate.tier == "crucial" and not confirmed:
                    return None, f"llm requested unconfirmed crucial stop: {item}"
                if candidate.requires_confirmation and not confirmed:
                    return None, f"llm requested unconfirmed protected stop: {item}"
                if not candidate.auto_stop and not confirmed:
                    return None, f"llm requested unconfirmed non-auto-stop backend: {item}"
            stop_items.append(item)

        confidence_raw = proposal.get("confidence")
        try:
            confidence = float(confidence_raw if confidence_raw is not None else 0.0)
        except Exception:
            confidence = 0.0
        base_start = self._list_field(base_plan.get("start"))
        base_stop = self._list_field(base_plan.get("stop"))
        matches_base_plan = set(start_items) == set(base_start) and set(stop_items) == set(base_stop)
        if confidence_raw in (None, "") and matches_base_plan and (start_items or stop_items):
            confidence = self.llm_advisor_min_confidence
        if confidence < self.llm_advisor_min_confidence:
            return None, f"llm confidence {confidence:.2f} below threshold {self.llm_advisor_min_confidence:.2f}"

        host = self.hosts.get(backend.host)
        free_vram_mb = int(base_plan.get("free_vram_mb") or (self._host_free_vram(host) if host else 0))
        target_free_vram_mb = int(
            base_plan.get("target_free_vram_mb")
            or (required_free_vram_mb + (self.target_free_vram_mb if required_free_vram_mb > 0 else 0))
            or 0
        )
        needed_vram_mb = max(0, target_free_vram_mb - free_vram_mb)
        freed_vram_mb = sum(self._freed_vram_mb(self.backends[item]) for item in stop_items)

        if purpose == "activation" and backend.estimated_vram_mb > 0:
            needed_vram_mb = max(0, backend.estimated_vram_mb - free_vram_mb + self.target_free_vram_mb)
            target_free_vram_mb = free_vram_mb + needed_vram_mb

        if needed_vram_mb > 0 and freed_vram_mb < needed_vram_mb:
            return None, f"llm plan frees {freed_vram_mb}MB but needs {needed_vram_mb}MB"

        if purpose == "capacity":
            decision = "capacity_swap" if stop_items or start_items else "capacity_ready"
        else:
            decision = "swap" if stop_items else "activate"

        plan = dict(base_plan)
        plan.update(
            {
                "ok": True,
                "decision": decision,
                "message": "LLM-selected lifecycle plan validated against policy.",
                "start": start_items,
                "stop": stop_items,
                "backend": self._backend_status(backend),
                "conflicts": [self._backend_status(self.backends[item]) for item in stop_items],
                "required_free_vram_mb": required_free_vram_mb or base_plan.get("required_free_vram_mb", 0),
                "target_free_vram_mb": target_free_vram_mb,
                "free_vram_mb": free_vram_mb,
                "needed_vram_mb": needed_vram_mb,
                "freed_vram_mb": freed_vram_mb,
                "llm_decision": {
                    "used": True,
                    "model": self.llm_advisor_model,
                    "confidence": confidence,
                    "recommendation": recommendation or "apply_plan",
                    "rationale": str(proposal.get("rationale") or "")[:1000],
                    "risks": self._list_field(proposal.get("risks"))[:8],
                    "proposal": {
                        "decision": proposal.get("decision"),
                        "start": start_items,
                        "stop": stop_items,
                    },
                },
            }
        )
        return plan, ""

    async def _maybe_apply_llm_decision(
        self,
        plan: Dict[str, Any],
        *,
        purpose: str,
        backend: BackendPolicy,
        route_kind: str,
        reason: str,
        confirmed: bool,
        allow_disruptive: bool,
        required_free_vram_mb: int,
    ) -> Dict[str, Any]:
        if not self._llm_decision_enabled():
            return plan
        if plan.get("decision") in {"requires_confirmation", "blocked", "observe_only"} and not confirmed:
            plan["llm_decision"] = {
                "used": False,
                "model": self.llm_advisor_model,
                "error": f"deterministic plan is {plan.get('decision')}; confirmation is required before LLM selection",
            }
            return plan
        context = self._llm_context(
            plan=plan,
            purpose=purpose,
            backend=backend,
            route_kind=route_kind,
            reason=reason,
            confirmed=confirmed,
            allow_disruptive=allow_disruptive,
            required_free_vram_mb=required_free_vram_mb,
        )
        result = await self._call_llm_planner(context)
        if result.get("error"):
            plan["llm_decision"] = {
                "used": False,
                "model": self.llm_advisor_model,
                "error": result.get("error"),
                "content": result.get("content", ""),
                "finish_reason": result.get("finish_reason", ""),
                "prompt_chars": result.get("prompt_chars"),
            }
            return plan
        proposal = result.get("proposal")
        if not isinstance(proposal, dict):
            plan["llm_decision"] = {"used": False, "model": self.llm_advisor_model, "error": "missing proposal"}
            return plan
        llm_plan, error = self._validate_llm_proposal(
            proposal,
            base_plan=plan,
            purpose=purpose,
            backend=backend,
            confirmed=confirmed,
            allow_disruptive=allow_disruptive,
            required_free_vram_mb=required_free_vram_mb,
        )
        if llm_plan is None:
            plan["llm_decision"] = {
                "used": False,
                "model": self.llm_advisor_model,
                "error": error,
                "proposal": proposal,
                "finish_reason": result.get("finish_reason", ""),
                "prompt_chars": result.get("prompt_chars"),
            }
            return plan
        if isinstance(llm_plan.get("llm_decision"), dict):
            llm_plan["llm_decision"]["finish_reason"] = result.get("finish_reason", "")
            llm_plan["llm_decision"]["prompt_chars"] = result.get("prompt_chars")
        return llm_plan

    async def _execute_plan(self, plan: Dict[str, Any]) -> None:
        stop_items = [self.backends[item] for item in plan.get("stop", []) if item in self.backends]
        start_items = [self.backends[item] for item in plan.get("start", []) if item in self.backends]
        for backend in stop_items:
            await self._compose(backend, "stop")
        for backend in start_items:
            await self._compose(backend, "up -d --build")

    async def _maybe_auto_restart_backend(self, backend: BackendPolicy, *, reason: str) -> None:
        if not backend.auto_restart_on_failure:
            return
        now = time.time()
        cooldown_sec = max(0.0, float(backend.auto_restart_cooldown_sec or 0.0))
        if cooldown_sec > 0 and backend.last_restart_at > 0 and (now - backend.last_restart_at) < cooldown_sec:
            return
        backend.last_restart_at = now
        self._save_state()
        try:
            await self._restart_backend(backend, reason=reason, automatic=True)
        except Exception:
            return

    async def _attach_llm_advice(self, plan: Dict[str, Any]) -> Dict[str, Any]:
        if not self.llm_advisor_enabled:
            return plan
        if plan.get("advisor") or (isinstance(plan.get("llm_decision"), dict) and plan["llm_decision"].get("used") is True):
            return plan
        if not self.llm_advisor_base_url or not self.llm_advisor_model:
            return plan
        summary = {
            "plan": self._compact_plan_for_llm(plan),
            "mode": self.mode,
            "hosts": [self._host_for_llm(host) for host in self.hosts.values()],
            "active_backends": [
                self._compact_backend_for_llm(backend)
                for backend in self.backends.values()
                if backend.active
            ],
        }
        context_text = json.dumps(summary, ensure_ascii=False, separators=(",", ":"))[: self.llm_advisor_max_context_chars]
        messages = [
            {
                "role": "system",
                "content": (
                    "You advise a local AI backend lifecycle manager. The deterministic plan is authoritative. "
                    "If the plan is requires_confirmation or blocked, recommend that state and explain the operator "
                    "confirmation or resource issue; do not present forbidden stop/start actions as approved. "
                    "Reply only with compact JSON containing recommendation, rationale, and risks."
                ),
            },
            {"role": "user", "content": context_text},
        ]
        headers = {}
        token = _env("GATEWAY_BEARER_TOKEN")
        if token:
            headers["Authorization"] = f"Bearer {token}"
        try:
            async with httpx.AsyncClient(timeout=self.llm_advisor_timeout_sec) as client:
                response = await client.post(
                    f"{self.llm_advisor_base_url}/chat/completions",
                    headers=headers,
                    json={
                        "model": self.llm_advisor_model,
                        "messages": messages,
                        "temperature": 0,
                        "max_tokens": self.llm_advisor_max_tokens,
                    },
                )
            if response.status_code >= 400:
                plan["advisor_error"] = f"HTTP {response.status_code}: {response.text[:1000]}"
                plan["advisor_prompt_chars"] = len(context_text)
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

    async def _restart_backend(self, backend: BackendPolicy, *, reason: str, automatic: bool) -> None:
        if backend.compose_managed:
            label = "auto_restart" if automatic else "restart"
            backend.last_action = f"{label}:restart"
            backend.last_action_at = time.time()
            try:
                await self._compose(backend, "restart")
                backend.last_action = label
                backend.last_action_error = ""
            except Exception as exc:
                backend.last_action = label
                backend.last_action_error = f"{type(exc).__name__}: {exc}"
                self._save_state()
                raise
            return

        if not backend.auto_restart_command:
            raise HTTPException(status_code=400, detail=f"no restart command configured for {backend.backend_class}")
        host = self.hosts.get(backend.host)
        if host is None:
            raise HTTPException(status_code=400, detail=f"unknown host {backend.host}")
        command = backend.auto_restart_command
        if host.repo_dir:
            command = f"cd {shlex.quote(host.repo_dir)} && {command}"
        label = "auto_restart" if automatic else "restart"
        backend.last_action = label
        backend.last_action_at = time.time()
        try:
            await self._ssh(host, command, timeout=max(30, int(backend.auto_restart_timeout_sec or 120.0)))
            backend.last_action_error = ""
            backend.drained = True
            backend.drain_reason = f"{reason}; restart requested"
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
        return [
            self._backend_status(candidate)
            for candidate in self._same_host_candidates(backend)
            if candidate.active and candidate.backend_class != backend.backend_class
        ]

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
        if self.ssh_identity_file and Path(self.ssh_identity_file).expanduser().is_file():
            ssh_args.extend(["-i", self.ssh_identity_file])
        if host.ssh_port > 0:
            ssh_args.extend(["-p", str(host.ssh_port)])
        ssh_args.extend([host.ssh_connect_target or host.ssh_target, remote_command])
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
        return (
            "docker_bin=\"$(command -v docker 2>/dev/null || true)\"; "
            "if [ -z \"$docker_bin\" ]; then "
            "PATH=\"/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$PATH\"; "
            "docker_bin=\"$(command -v docker 2>/dev/null || true)\"; "
            "fi; "
            "if [ -n \"$docker_bin\" ]; then "
            "context_output=\"\"; default_output=\"\"; "
            "if [ \"$(uname -s 2>/dev/null || true)\" = Darwin ]; then "
            "colima_context=\"${DOCKER_CONTEXT:-colima}\"; "
            "context_output=$(\"$docker_bin\" --context \"$colima_context\" ps --format '{{.Names}}\\t{{.Status}}' 2>/dev/null || true); "
            "fi; "
            "default_output=$(\"$docker_bin\" ps --format '{{.Names}}\\t{{.Status}}' 2>/dev/null || true); "
            "if [ -n \"$context_output\" ]; then printf '%s\\n' \"$context_output\"; "
            "elif [ -n \"$default_output\" ]; then printf '%s\\n' \"$default_output\"; "
            "fi; "
            "fi; true"
        )

    @classmethod
    def _linux_probe_command(cls) -> str:
        return (
            "printf '__OS__\\n'; "
            "if [ -r /etc/os-release ]; then "
            ". /etc/os-release; "
            "printf 'name=%s\\n' \"${PRETTY_NAME:-${NAME:-Linux}}\"; "
            "printf 'id=%s\\n' \"${ID:-linux}\"; "
            "printf 'version_id=%s\\n' \"${VERSION_ID:-}\"; "
            "else "
            "printf 'name=%s\\n' \"$(uname -sr 2>/dev/null || echo Linux)\"; "
            "printf 'id=linux\\n'; "
            "fi; "
            "printf '__CPU__\\n'; "
            "if command -v lscpu >/dev/null 2>&1; then "
            "lscpu | awk -F: '/^Model name:/ {gsub(/^[ \\t]+/,\"\",$2); print \"model_name=\" $2} "
            "/^CPU\\(s\\):/ {gsub(/^[ \\t]+/,\"\",$2); print \"logical_cpus=\" $2} "
            "/^Core\\(s\\) per socket:/ {gsub(/^[ \\t]+/,\"\",$2); print \"cores_per_socket=\" $2} "
            "/^Socket\\(s\\):/ {gsub(/^[ \\t]+/,\"\",$2); print \"sockets=\" $2} "
            "/^Thread\\(s\\) per core:/ {gsub(/^[ \\t]+/,\"\",$2); print \"threads_per_core=\" $2}'; "
            "else "
            "awk -F: '/^model name/ {gsub(/^[ \\t]+/,\"\",$2); print \"model_name=\" $2; exit}' /proc/cpuinfo 2>/dev/null || true; "
            "printf 'logical_cpus=%s\\n' \"$(getconf _NPROCESSORS_ONLN 2>/dev/null || echo 0)\"; "
            "fi; "
            "printf '__GPU__\\n'; "
            "nvidia-smi --query-gpu=index,name,memory.total,memory.used,memory.free,utilization.gpu "
            "--format=csv,noheader,nounits 2>/dev/null || true; "
            "printf '__MEM__\\n'; free -m 2>/dev/null | awk '/^Mem:/ {print $2\" \"$3\" \"$7}'; "
            "printf '__NET__\\n'; "
            "if [ -d /sys/class/net ]; then "
            "for net_path in /sys/class/net/*; do "
            "iface=\"${net_path##*/}\"; "
            "case \"$iface\" in ''|lo|docker*|br-*|veth*|virbr*|tun*|tap*|cni*|flannel*) continue ;; esac; "
            "mac=\"$(cat \"$net_path/address\" 2>/dev/null || true)\"; "
            "operstate=\"$(cat \"$net_path/operstate\" 2>/dev/null || true)\"; "
            "carrier=\"$(cat \"$net_path/carrier\" 2>/dev/null || true)\"; "
            "speed=\"$(cat \"$net_path/speed\" 2>/dev/null || true)\"; "
            "duplex=\"$(cat \"$net_path/duplex\" 2>/dev/null || true)\"; "
            "if [ \"$speed\" = \"-1\" ]; then speed=\"\"; fi; "
            "supported=\"\"; current_media=\"\"; "
            "if command -v ethtool >/dev/null 2>&1; then "
            "ethtool_out=\"$(ethtool \"$iface\" 2>/dev/null || true)\"; "
            "supported=\"$(printf '%s\\n' \"$ethtool_out\" | awk '/^[ \\t]*Supported link modes:/ {capture=1; sub(/^[ \\t]*Supported link modes:[ \\t]*/, \"\"); line=$0; next} capture && /^[ \\t]+/ {line=line \" \" $0; next} capture {capture=0} END {gsub(/^[ \\t]+|[ \\t]+$/, \"\", line); print line}')\"; "
            "current_media=\"$(printf '%s\\n' \"$ethtool_out\" | awk -F: '/^[ \\t]*Speed:/ {speed=$2} /^[ \\t]*Duplex:/ {duplex=$2} /^[ \\t]*Port:/ {port=$2} END {gsub(/^[ \\t]+|[ \\t]+$/, \"\", speed); gsub(/^[ \\t]+|[ \\t]+$/, \"\", duplex); gsub(/^[ \\t]+|[ \\t]+$/, \"\", port); out=speed; if (duplex != \"\") out=out \" \" duplex; if (port != \"\") out=out \" \" port; print out}')\"; "
            "fi; "
            "printf 'name=%s\\tmac=%s\\toperstate=%s\\tcarrier=%s\\tcurrent_speed_mbps=%s\\tduplex=%s\\tcurrent_media=%s\\tsupported_media=%s\\n' "
            "\"$iface\" \"$mac\" \"$operstate\" \"$carrier\" \"$speed\" \"$duplex\" \"$current_media\" \"$supported\"; "
            "done; "
            "fi; "
            "printf '__DOCKER__\\n'; "
            f"{cls._docker_probe_command()}"
        )

    @classmethod
    def _macos_probe_command(cls) -> str:
        return (
            "printf '__OS__\\n'; "
            "if command -v sw_vers >/dev/null 2>&1; then "
            "printf 'name=%s %s\\n' \"$(sw_vers -productName 2>/dev/null || echo macOS)\" \"$(sw_vers -productVersion 2>/dev/null || true)\"; "
            "printf 'id=macos\\n'; "
            "printf 'version_id=%s\\n' \"$(sw_vers -productVersion 2>/dev/null || true)\"; "
            "printf 'build=%s\\n' \"$(sw_vers -buildVersion 2>/dev/null || true)\"; "
            "else "
            "printf 'name=%s\\n' \"$(uname -sr 2>/dev/null || echo macOS)\"; "
            "printf 'id=macos\\n'; "
            "fi; "
            "printf '__CPU__\\n'; "
            "cpu_name=\"$(sysctl -n machdep.cpu.brand_string 2>/dev/null || true)\"; "
            "if [ -z \"$cpu_name\" ] && command -v system_profiler >/dev/null 2>&1; then "
            "cpu_name=\"$(system_profiler SPHardwareDataType 2>/dev/null | awk -F: '/Chip:/ {gsub(/^[ \\t]+/,\"\",$2); print $2; exit}')\"; "
            "fi; "
            "if [ -z \"$cpu_name\" ]; then cpu_name=\"$(sysctl -n hw.model 2>/dev/null || echo Apple Silicon)\"; fi; "
            "printf 'model_name=%s\\n' \"$cpu_name\"; "
            "printf 'logical_cpus=%s\\n' \"$(sysctl -n hw.ncpu 2>/dev/null || echo 0)\"; "
            "printf 'physical_cpus=%s\\n' \"$(sysctl -n hw.physicalcpu 2>/dev/null || echo 0)\"; "
            "printf '__MEM__\\n'; "
            "if command -v python3 >/dev/null 2>&1; then "
            "python3 -c \"import re,subprocess; "
            "total=int(subprocess.check_output(['sysctl','-n','hw.memsize']).strip() or 0)//1048576; "
            "out=subprocess.run(['vm_stat'],capture_output=True,text=True).stdout; "
            "page=int(next(iter(re.findall(r'page size of (\\d+) bytes', out)), 4096)); "
            "counts={m.group(1).strip():int(m.group(2)) for m in re.finditer(r'^Pages ([^:]+):\\s+(\\d+)\\.', out, re.M)}; "
            "available_pages=sum(counts.get(k,0) for k in ('free','inactive','speculative')); "
            "available_mb=available_pages*page//1048576; "
            "print(total, max(0,total-available_mb), available_mb)\"; "
            "elif command -v free >/dev/null 2>&1 && free -m 2>/dev/null | awk '/^Mem:/ {print $2\" \"$3\" \"$7; found=1} END {exit found ? 0 : 1}'; then :; else "
            "total_bytes=$(sysctl -n hw.memsize 2>/dev/null || echo 0); total_mb=$((total_bytes / 1024 / 1024)); "
            "printf '%s %s %s\\n' \"$total_mb\" 0 0; "
            "fi; "
            "printf '__NET__\\n'; "
            "if command -v networksetup >/dev/null 2>&1 && command -v ifconfig >/dev/null 2>&1; then "
            "networksetup -listallhardwareports 2>/dev/null | awk -F': ' '/^Hardware Port:/ {port=$2} /^Device:/ {device=$2} /^Ethernet Address:/ {mac=$2; if (device != \"\") print port \"\\t\" device \"\\t\" mac; port=\"\"; device=\"\"; mac=\"\"}' | "
            "while IFS=\"$(printf '\\t')\" read -r port device mac; do "
            "if [ -z \"$device\" ]; then continue; fi; "
            "ifconfig_out=\"$(ifconfig -m \"$device\" 2>/dev/null || true)\"; "
            "status=\"$(printf '%s\\n' \"$ifconfig_out\" | awk -F': ' '/^[ \\t]*status:/ {print $2; exit}')\"; "
            "current_media=\"$(printf '%s\\n' \"$ifconfig_out\" | awk -F': ' '/^[ \\t]*media:/ {print $2; exit}')\"; "
            "supported=\"$(printf '%s\\n' \"$ifconfig_out\" | awk '/^[ \\t]*supported media:/ {capture=1; next} capture && /^[ \\t]*media / {gsub(/^[ \\t]+/, \"\"); line=line \" \" $0; next} capture && !/^[ \\t]*media / {capture=0} END {gsub(/^[ \\t]+|[ \\t]+$/, \"\", line); print line}')\"; "
            "media_detail=\"$(networksetup -getMedia \"$device\" 2>/dev/null | tr '\\n' ';' || true)\"; "
            "printf 'name=%s\\tdisplay_name=%s\\tmac=%s\\toperstate=%s\\tcurrent_media=%s\\tsupported_media=%s\\tmedia_detail=%s\\n' "
            "\"$device\" \"$port\" \"$mac\" \"$status\" \"$current_media\" \"$supported\" \"$media_detail\"; "
            "done; "
            "fi; "
            "printf '__DOCKER__\\n'; "
            f"{cls._docker_probe_command()}"
        )

    def _parse_linux_probe(self, host: HostPolicy, raw: str) -> None:
        sections = self._sections(raw)
        host.os = self._parse_key_values(sections.get("OS", []))
        host.cpu = self._parse_key_values(sections.get("CPU", []))
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
        host.network_interfaces = self._parse_network_interfaces(sections.get("NET", []))
        host.containers = self._parse_containers("\n".join(sections.get("DOCKER", [])))

    def _parse_macos_probe(self, host: HostPolicy, raw: str) -> None:
        sections = self._sections(raw)
        host.os = self._parse_key_values(sections.get("OS", []))
        host.cpu = self._parse_key_values(sections.get("CPU", []))
        mem_lines = sections.get("MEM", [])
        if mem_lines:
            bits = mem_lines[0].split()
            if len(bits) >= 3:
                host.memory = {"total_mb": int(bits[0]), "used_mb": int(bits[1]), "available_mb": int(bits[2])}
        host.network_interfaces = self._parse_network_interfaces(sections.get("NET", []))
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
    def _parse_key_values(lines: List[str]) -> Dict[str, Any]:
        values: Dict[str, Any] = {}
        int_fields = {"logical_cpus", "physical_cpus", "cores_per_socket", "sockets", "threads_per_core"}
        for line in lines:
            key, sep, value = line.partition("=")
            if not sep:
                continue
            key = key.strip()
            value = value.strip()
            if not key:
                continue
            if key in int_fields:
                try:
                    values[key] = int(value)
                    continue
                except Exception:
                    pass
            values[key] = value
        return values

    @classmethod
    def _parse_network_interfaces(cls, lines: List[str]) -> List[Dict[str, Any]]:
        interfaces: List[Dict[str, Any]] = []
        for line in lines:
            values: Dict[str, str] = {}
            for part in line.split("\t"):
                key, sep, value = part.partition("=")
                if not sep:
                    continue
                values[key.strip()] = value.strip()
            name = values.get("name", "")
            if not name:
                continue
            current_speed = cls._int_or_none(values.get("current_speed_mbps"))
            current_media = values.get("current_media") or values.get("media_detail") or ""
            if current_speed is None:
                current_speed = cls._max_network_speed_mbps(current_media)
            supported_media = values.get("supported_media") or ""
            theoretical_speed = cls._max_network_speed_mbps(supported_media)
            if theoretical_speed is None and current_speed is not None:
                theoretical_speed = current_speed
            carrier = values.get("carrier")
            operstate = values.get("operstate", "")
            active = carrier == "1" or operstate.lower() in {"active", "up"}
            interfaces.append(
                {
                    "name": name,
                    "display_name": values.get("display_name") or name,
                    "mac": values.get("mac") or "",
                    "operstate": operstate,
                    "carrier": carrier or "",
                    "active": active,
                    "duplex": values.get("duplex") or "",
                    "current_media": current_media,
                    "supported_media": supported_media,
                    "current_speed_mbps": current_speed,
                    "theoretical_speed_mbps": theoretical_speed,
                }
            )
        interfaces.sort(
            key=lambda item: (
                0 if item.get("active") else 1,
                -int(item.get("theoretical_speed_mbps") or 0),
                str(item.get("name") or ""),
            )
        )
        return interfaces

    @staticmethod
    def _int_or_none(value: Any) -> Optional[int]:
        try:
            parsed = int(str(value or "").strip())
        except Exception:
            return None
        return parsed if parsed > 0 else None

    @classmethod
    def _max_network_speed_mbps(cls, text: str) -> Optional[int]:
        speeds = cls._network_speed_values_mbps(text)
        return max(speeds) if speeds else None

    @staticmethod
    def _network_speed_values_mbps(text: str) -> List[int]:
        value = str(text or "")
        speeds: List[int] = []
        for match in re.finditer(r"(\d+(?:\.\d+)?)\s*[Gg]\s*(?:base|bit|b|bps|/s)?", value):
            speeds.append(int(float(match.group(1)) * 1000))
        for match in re.finditer(r"(\d+(?:\.\d+)?)\s*[Mm]\s*(?:base|bit|b|bps|/s)?", value):
            speeds.append(int(float(match.group(1))))
        for match in re.finditer(r"(?<![A-Za-z])(\d+(?:\.\d+)?)\s*[Bb]ase", value):
            speeds.append(int(float(match.group(1))))
        return [speed for speed in speeds if speed > 0]

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
            "idle_observed_vram_mb": backend.idle_observed_vram_mb,
            "peak_observed_vram_mb": backend.peak_observed_vram_mb,
            "auto_start": backend.auto_start,
            "auto_stop": backend.auto_stop,
            "requires_confirmation": backend.requires_confirmation,
            "compose_managed": backend.compose_managed,
            "health_check": backend.health_check,
            "health_timeout_sec": backend.health_timeout_sec,
            "canary_enabled": backend.canary_enabled,
            "canary_path": backend.canary_path,
            "canary_method": backend.canary_method,
            "canary_timeout_sec": backend.canary_timeout_sec,
            "canary_failure_threshold": backend.canary_failure_threshold,
            "active": backend.active,
            "healthy": backend.healthy,
            "ready": backend.ready,
            "health_error": backend.health_error,
            "drained": backend.drained,
            "drain_reason": backend.drain_reason,
            "status": lifecycle["status"],
            "status_label": lifecycle["status_label"],
            "status_color": lifecycle["status_color"],
            "status_rank": lifecycle["status_rank"],
            "last_checked_at": backend.last_checked_at,
            "last_healthy_at": backend.last_healthy_at,
            "last_ready_at": backend.last_ready_at,
            "last_confirmed_working_at": max(backend.last_ready_at, backend.canary_last_success_at),
            "last_unhealthy_at": backend.last_unhealthy_at,
            "last_stopped_at": backend.last_stopped_at,
            "last_health_error": backend.last_health_error,
            "last_requested_at": backend.last_requested_at,
            "inflight": backend.inflight,
            "last_action": backend.last_action,
            "last_action_at": backend.last_action_at,
            "last_action_error": backend.last_action_error,
            "last_restart_at": backend.last_restart_at,
            "canary_consecutive_failures": backend.canary_consecutive_failures,
            "canary_last_checked_at": backend.canary_last_checked_at,
            "canary_last_success_at": backend.canary_last_success_at,
            "canary_last_error": backend.canary_last_error,
            "models": backend.models,
            "models_error": backend.models_error,
            "models_checked_at": backend.models_checked_at,
            "notes": backend.notes,
        }

    def _core_service_status(self, service: CoreServicePolicy) -> Dict[str, Any]:
        host = self.hosts.get(service.host)
        expected = [f"nexus-{component}" for component in service.components]
        containers: List[Dict[str, str]] = []
        active_by_component: Dict[str, bool] = {}
        if host is not None:
            for component, expected_name in zip(service.components, expected):
                matched = [
                    {"name": name, "status": status}
                    for name, status in sorted(host.containers.items())
                    if self._component_container_active(name, expected_name)
                ]
                containers.extend(matched)
                active_by_component[component] = any(
                    self._container_status_ready(item["status"])
                    for item in matched
                )

        missing = [component for component in service.components if not active_by_component.get(component)]
        host_error = host.error if host is not None else f"unknown host {service.host}"
        active = not missing and not host_error
        if host_error:
            status = "host_error"
            label = "Host probe failed"
            color = "red"
            rank = 3
        elif not service.components:
            status = "active"
            label = "Host reachable"
            color = "green"
            rank = 0
        elif active:
            status = "active"
            label = "Active"
            color = "green"
            rank = 0
        elif any("unhealthy" in item["status"].lower() for item in containers):
            status = "unhealthy"
            label = "Unhealthy"
            color = "red"
            rank = 3
        elif containers:
            status = "partial"
            label = "Partial"
            color = "yellow"
            rank = 2
        else:
            status = "missing"
            label = "Missing"
            color = "red"
            rank = 4

        return {
            "service_id": service.service_id,
            "display_name": service.display_name,
            "host": service.host,
            "components": service.components,
            "tier": service.tier,
            "active": active,
            "status": status,
            "status_label": label,
            "status_color": color,
            "status_rank": rank,
            "containers": containers,
            "missing_components": missing,
            "host_error": host_error,
            "updated_at": host.updated_at if host is not None else 0.0,
            "notes": service.notes,
        }

    @staticmethod
    def _backend_lifecycle_state(backend: BackendPolicy) -> Dict[str, Any]:
        last_working_at = backend.last_ready_at or backend.last_healthy_at
        last_unhealthy_at = backend.last_unhealthy_at or 0.0
        last_stopped_at = backend.last_stopped_at or 0.0

        if backend.active:
            if backend.ready is True:
                return {
                    "status": "active_ready",
                    "status_label": "Active and ready",
                    "status_color": "green",
                    "status_rank": 0,
                }
            if backend.drained:
                return {
                    "status": "active_drained",
                    "status_label": "Drained after canary failure",
                    "status_color": "red",
                    "status_rank": 3,
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

        if last_working_at and (last_working_at >= last_unhealthy_at or last_stopped_at >= last_unhealthy_at):
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
                "llm_advisor_mode": self.llm_advisor_mode,
                "llm_advisor_model": self.llm_advisor_model,
                "llm_advisor_max_context_chars": self.llm_advisor_max_context_chars,
                "llm_advisor_max_tokens": self.llm_advisor_max_tokens,
                "llm_advisor_min_confidence": self.llm_advisor_min_confidence,
                "model_probe_enabled": self.model_probe_enabled,
            },
            "hosts": [
                {
                    "name": host.name,
                    "ssh_target": host.ssh_target,
                    "ssh_connect_target": host.ssh_connect_target,
                    "ssh_port": host.ssh_port,
                    "platform": host.platform,
                    "resource_kind": host.resource_kind,
                    "error": host.error,
                    "os": host.os,
                    "cpu": host.cpu,
                    "memory": host.memory,
                    "gpus": host.gpus,
                    "network_interfaces": host.network_interfaces,
                    "containers": host.containers,
                    "updated_at": host.updated_at,
                }
                for host in sorted(self.hosts.values(), key=lambda item: item.name)
            ],
            "core_services": [
                self._core_service_status(service)
                for service in sorted(self.core_services.values(), key=lambda item: (item.host, item.service_id))
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


@app.post("/v1/lifecycle/ensure-capacity")
async def lifecycle_ensure_capacity(req: EnsureCapacityRequest) -> Dict[str, Any]:
    return await manager.ensure_capacity(req)


@app.post("/v1/lifecycle/action")
async def lifecycle_action(req: ActionRequest) -> Dict[str, Any]:
    return await manager.action(req)


@app.post("/v1/lifecycle/mlx/prefetch")
async def lifecycle_mlx_prefetch(req: MlxPrefetchRequest) -> Dict[str, Any]:
    return await manager.prefetch_mlx_model(req)


@app.post("/v1/lifecycle/mlx/cache/purge")
async def lifecycle_mlx_cache_purge(req: MlxPrefetchRequest) -> Dict[str, Any]:
    return await manager.purge_mlx_model_cache(req)


@app.post("/v1/lifecycle/mlx/cache/redownload")
async def lifecycle_mlx_cache_redownload(req: MlxPrefetchRequest) -> Dict[str, Any]:
    return await manager.redownload_mlx_model(req)


@app.post("/v1/lifecycle/mlx/huge-lane/switch")
async def lifecycle_mlx_huge_lane_switch(req: MlxPrefetchRequest) -> Dict[str, Any]:
    return await manager.switch_mlx_huge_model(req)


@app.post("/v1/lifecycle/mlx/cache-status/sync")
async def lifecycle_mlx_cache_status_sync() -> Dict[str, Any]:
    return await manager.sync_mlx_cache_status()


@app.post("/v1/lifecycle/notify")
async def lifecycle_notify(req: NotifyRequest) -> Dict[str, Any]:
    return manager.notify(req)
