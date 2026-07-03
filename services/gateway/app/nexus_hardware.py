from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any

import httpx

from app.config import S


NEXUS_HARDWARE_VERIFIED_AT = "2026-05-26"
NEXUS_HARDWARE_SNAPSHOT_VERSION = 1
PRODUCTION_CONTEXT_HOSTS = ("ai2", "ai1", "ada2", "meltdown", "copyfail")
KNOWN_NON_PRODUCTION_HOSTS = ("migraine",)


NEXUS_HOST_HARDWARE: dict[str, dict[str, Any]] = {
    "ai2": {
        "platform": "macos_arm64",
        "cpu": "Apple M3 Ultra",
        "cpu_cores": "32 CPU cores",
        "memory": "512 GiB unified memory",
        "accelerators": ["Apple Silicon GPU/Neural Engine through host-native MLX"],
        "role": "Primary gateway/control-plane host, host-native MLX reasoning/coding lane, and containerized TTS host.",
        "notes": "Best fit for MLX and CPU/unified-memory workloads. Not a CUDA host.",
    },
    "ai1": {
        "platform": "linux_x86_64",
        "cpu": "12th Gen Intel Core i7-12700F",
        "cpu_cores": "20 logical CPUs",
        "memory": "about 46 GiB system RAM observed",
        "accelerators": [
            "NVIDIA GeForce RTX 3090 24 GiB",
            "NVIDIA GeForce RTX 3090 24 GiB",
        ],
        "role": "Dual-GPU Linux/NVIDIA host for media ingress, secondary vLLM/CUDA capacity, embeddings, and overflow CUDA work.",
        "notes": "Schedule by per-GPU 24 GiB VRAM limits; do not assume a single 48 GiB CUDA device.",
    },
    "ada2": {
        "platform": "linux_x86_64",
        "cpu": "13th Gen Intel Core i7-13700K",
        "cpu_cores": "16 logical CPUs observed",
        "memory": "about 125 GiB system RAM observed",
        "accelerators": ["NVIDIA RTX 6000 Ada Generation, 48 GB class GPU with 46 GiB reported VRAM"],
        "role": "Primary heavy CUDA host for vLLM strong and high-VRAM image/video/music/OCR workloads.",
        "notes": "System RAM helps CPU offload and startup headroom, but VRAM contention remains the scheduling constraint.",
    },
    "meltdown": {
        "platform": "linux_x86_64",
        "cpu": "Intel Core i7-5930K",
        "cpu_cores": "12 logical CPUs",
        "memory": "about 47 GiB system RAM observed",
        "accelerators": ["NVIDIA GeForce RTX 5060 Ti 16 GB class GPU with 15.9 GiB reported VRAM"],
        "role": "Linux/NVIDIA overflow and staging host for lighter CUDA workloads.",
        "notes": "Use for smaller models or test deployments; avoid assuming ada2-class VRAM. Docker and NVIDIA Container Toolkit must be bootstrapped before Compose GPU workloads.",
    },
    "copyfail": {
        "platform": "linux_x86_64",
        "cpu": "Intel Celeron J3355 @ 2.00GHz",
        "cpu_cores": "2 logical CPUs",
        "memory": "about 7.4 GiB system RAM observed",
        "accelerators": [],
        "role": "Infrastructure-only host for metrics collection, deployment orchestration, and general IT operations.",
        "notes": "Do not schedule model-serving backends here; use it as a lightweight control node.",
    },
    "migraine": {
        "platform": "macos_arm64",
        "cpu": "Apple M2",
        "cpu_cores": "8 CPU cores",
        "memory": "8 GiB unified memory",
        "accelerators": ["Apple Silicon integrated GPU"],
        "role": "Client-only Hermes Gateway and Telegram bot host that consumes Nexus models through the gateway.",
        "notes": "Do not choose migraine for Nexus model placement; keep it out of backend scheduling unless an operator explicitly promotes it into topology.",
    },
}


_RUNTIME_SNAPSHOT: dict[str, Any] | None = None


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _snapshot_path() -> Path:
    return Path(str(getattr(S, "NEXUS_HARDWARE_SNAPSHOT_PATH", "") or "/var/lib/gateway/data/nexus_hardware_snapshot.json"))


def _baseline_snapshot(*, reason: str = "") -> dict[str, Any]:
    warnings = []
    if reason:
        warnings.append(reason)
    return {
        "version": NEXUS_HARDWARE_SNAPSHOT_VERSION,
        "source": "checked_in_baseline",
        "verified_at": NEXUS_HARDWARE_VERIFIED_AT,
        "refreshed_at": "",
        "hosts": deepcopy(NEXUS_HOST_HARDWARE),
        "warnings": warnings,
    }


def _is_snapshot(value: Any) -> bool:
    return isinstance(value, dict) and isinstance(value.get("hosts"), dict)


def _set_runtime_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
    global _RUNTIME_SNAPSHOT
    _RUNTIME_SNAPSHOT = snapshot
    return snapshot


def _format_gib_from_mb(total_mb: Any) -> str:
    try:
        mib = float(total_mb)
    except Exception:
        return ""
    if mib <= 0:
        return ""
    gib = mib / 1024.0
    if abs(gib - round(gib)) < 0.05:
        return f"{int(round(gib))} GiB"
    return f"{gib:.1f} GiB"


def _format_memory(host: dict[str, Any], baseline: dict[str, Any]) -> str:
    memory = host.get("memory") if isinstance(host.get("memory"), dict) else {}
    total = memory.get("total_mb") if isinstance(memory, dict) else None
    label = _format_gib_from_mb(total)
    if not label:
        return str(baseline.get("memory") or "")
    platform = str(baseline.get("platform") or host.get("platform") or "")
    if platform.startswith("macos"):
        return f"{label} unified memory observed"
    return f"about {label} system RAM observed"


def _format_cpu(host: dict[str, Any], baseline: dict[str, Any]) -> tuple[str, str]:
    cpu = host.get("cpu") if isinstance(host.get("cpu"), dict) else {}
    model = str(cpu.get("model_name") or cpu.get("brand_string") or baseline.get("cpu") or "").strip()
    logical = cpu.get("logical_cpus")
    physical = cpu.get("physical_cpus")
    platform = str(baseline.get("platform") or host.get("platform") or "")
    try:
        logical_int = int(logical)
    except Exception:
        logical_int = 0
    try:
        physical_int = int(physical)
    except Exception:
        physical_int = 0
    if platform.startswith("macos") and physical_int > 0:
        cores = f"{physical_int} CPU cores"
    elif logical_int > 0:
        cores = f"{logical_int} logical CPUs"
    else:
        cores = str(baseline.get("cpu_cores") or "")
    return model, cores


def _format_accelerators(host: dict[str, Any], baseline: dict[str, Any]) -> list[str]:
    gpus = host.get("gpus") if isinstance(host.get("gpus"), list) else []
    accelerators: list[str] = []
    for gpu in sorted((item for item in gpus if isinstance(item, dict)), key=lambda item: int(item.get("index") or 0)):
        name = str(gpu.get("name") or "").strip()
        if not name:
            continue
        total_mb = gpu.get("memory_total_mb")
        capacity = _format_gib_from_mb(total_mb)
        if capacity:
            accelerators.append(f"{name} {capacity}")
        else:
            accelerators.append(name)
    if accelerators:
        return accelerators
    baseline_accelerators = baseline.get("accelerators")
    if isinstance(baseline_accelerators, list):
        return [str(item) for item in baseline_accelerators]
    return []


def _host_from_lifecycle(name: str, live_host: dict[str, Any]) -> dict[str, Any]:
    baseline = deepcopy(NEXUS_HOST_HARDWARE.get(name, {}))
    cpu, cpu_cores = _format_cpu(live_host, baseline)
    return {
        **baseline,
        "cpu": cpu or baseline.get("cpu", ""),
        "cpu_cores": cpu_cores or baseline.get("cpu_cores", ""),
        "memory": _format_memory(live_host, baseline),
        "accelerators": _format_accelerators(live_host, baseline),
        "live_updated_at": live_host.get("updated_at") or 0,
        "probe_error": str(live_host.get("error") or ""),
    }


def _snapshot_from_lifecycle_payload(payload: dict[str, Any]) -> dict[str, Any]:
    live_hosts_raw = payload.get("hosts") if isinstance(payload.get("hosts"), list) else []
    live_hosts = {
        str(host.get("name") or ""): host
        for host in live_hosts_raw
        if isinstance(host, dict) and str(host.get("name") or "").strip()
    }
    hosts: dict[str, dict[str, Any]] = {}
    warnings: list[str] = []
    for name in (*PRODUCTION_CONTEXT_HOSTS, *KNOWN_NON_PRODUCTION_HOSTS):
        live_host = live_hosts.get(name)
        if live_host is None:
            hosts[name] = deepcopy(NEXUS_HOST_HARDWARE[name])
            warnings.append(f"{name}: lifecycle status did not include this host; using checked-in baseline")
            continue
        hosts[name] = _host_from_lifecycle(name, live_host)
        if hosts[name].get("probe_error"):
            warnings.append(f"{name}: lifecycle probe failed; retained baseline fields where live data was unavailable")
    return {
        "version": NEXUS_HARDWARE_SNAPSHOT_VERSION,
        "source": "live_lifecycle_refresh",
        "refreshed_at": _now_iso(),
        "lifecycle_generated_at": payload.get("generated_at") or 0,
        "hosts": hosts,
        "warnings": warnings,
    }


def _write_snapshot(snapshot: dict[str, Any]) -> None:
    try:
        path = _snapshot_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_name(path.name + ".tmp")
        tmp_path.write_text(json.dumps(snapshot, indent=2, sort_keys=True), encoding="utf-8")
        tmp_path.replace(path)
    except Exception as exc:
        warnings = snapshot.setdefault("warnings", [])
        if isinstance(warnings, list):
            warnings.append(f"hardware snapshot cache write failed: {type(exc).__name__}: {exc}")


def update_hardware_snapshot_from_lifecycle_payload(payload: dict[str, Any], *, persist: bool = True) -> dict[str, Any]:
    snapshot = _snapshot_from_lifecycle_payload(payload)
    if persist:
        _write_snapshot(snapshot)
    return _set_runtime_snapshot(snapshot)


def _load_cached_snapshot(*, reason: str = "") -> dict[str, Any] | None:
    try:
        raw = json.loads(_snapshot_path().read_text(encoding="utf-8"))
    except Exception:
        return None
    if not _is_snapshot(raw):
        return None
    snapshot = deepcopy(raw)
    cached_source = str(snapshot.get("source") or "unknown")
    snapshot["source"] = "last_cached_snapshot"
    snapshot["cached_source"] = cached_source
    warnings = snapshot.setdefault("warnings", [])
    if isinstance(warnings, list) and reason:
        warnings.append(reason)
    return snapshot


def _fallback_snapshot(*, reason: str = "") -> dict[str, Any]:
    cached = _load_cached_snapshot(reason=reason)
    if cached is not None:
        return _set_runtime_snapshot(cached)
    return _set_runtime_snapshot(_baseline_snapshot(reason=reason))


async def refresh_hardware_snapshot_from_lifecycle() -> dict[str, Any]:
    if not bool(getattr(S, "NEXUS_HARDWARE_REFRESH_ON_STARTUP", True)):
        return _fallback_snapshot(reason="hardware refresh disabled; using cached snapshot or checked-in baseline")

    base_url = str(getattr(S, "LIFECYCLE_MANAGER_BASE_URL", "") or "").strip().rstrip("/")
    if not base_url:
        return _fallback_snapshot(reason="lifecycle manager URL is not configured; using cached snapshot or checked-in baseline")

    timeout = float(getattr(S, "NEXUS_HARDWARE_REFRESH_TIMEOUT_SEC", 45.0) or 45.0)
    attempts = max(1, int(getattr(S, "NEXUS_HARDWARE_REFRESH_ATTEMPTS", 3) or 3))
    retry_delay = max(0.0, float(getattr(S, "NEXUS_HARDWARE_REFRESH_RETRY_DELAY_SEC", 5.0) or 5.0))
    last_exc: Exception | None = None
    payload: Any = None
    for attempt in range(1, attempts + 1):
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.get(f"{base_url}/v1/lifecycle/status", params={"refresh": "true"})
                response.raise_for_status()
                payload = response.json()
            last_exc = None
            break
        except httpx.ReadTimeout as exc:
            last_exc = exc
            break
        except Exception as exc:
            last_exc = exc
            if attempt >= attempts:
                break
            await asyncio.sleep(retry_delay)
    if last_exc is not None:
        return _fallback_snapshot(
            reason=f"lifecycle hardware refresh failed ({type(last_exc).__name__}: {last_exc}); using cached snapshot or checked-in baseline"
        )
    if not isinstance(payload, dict):
        return _fallback_snapshot(reason="lifecycle hardware refresh returned a non-object payload; using cached snapshot or checked-in baseline")
    return update_hardware_snapshot_from_lifecycle_payload(payload, persist=True)


def current_hardware_snapshot() -> dict[str, Any]:
    if _RUNTIME_SNAPSHOT is not None:
        return _RUNTIME_SNAPSHOT
    return _fallback_snapshot()


def hardware_snapshot_summary(snapshot: dict[str, Any] | None = None) -> dict[str, Any]:
    snapshot = snapshot or current_hardware_snapshot()
    hosts = snapshot.get("hosts") if isinstance(snapshot.get("hosts"), dict) else {}
    return {
        "source": snapshot.get("source") or "unknown",
        "refreshed_at": snapshot.get("refreshed_at") or snapshot.get("verified_at") or "",
        "hosts": sorted(str(name) for name in hosts.keys()),
        "warnings": snapshot.get("warnings") if isinstance(snapshot.get("warnings"), list) else [],
    }


def scheduled_task_hardware_context() -> str:
    snapshot = current_hardware_snapshot()
    source = str(snapshot.get("source") or "unknown")
    if source == "checked_in_baseline":
        stamp = f"verified {snapshot.get('verified_at') or NEXUS_HARDWARE_VERIFIED_AT} via WSL SSH probes"
    elif source == "last_cached_snapshot":
        stamp = f"loaded from cached snapshot refreshed {snapshot.get('refreshed_at') or 'unknown'}"
    else:
        stamp = f"refreshed {snapshot.get('refreshed_at') or 'unknown'} via lifecycle-manager"
    hosts = snapshot.get("hosts") if isinstance(snapshot.get("hosts"), dict) else NEXUS_HOST_HARDWARE
    lines = [
        f"Nexus production host hardware context (source: {source}; {stamp}):",
        "Use this for model/backend host-fit reasoning before choosing where a model should run. "
        "This is hardware capacity, not current load; check live resource tools for transient pressure.",
    ]
    for host in PRODUCTION_CONTEXT_HOSTS:
        spec = hosts.get(host) if isinstance(hosts.get(host), dict) else NEXUS_HOST_HARDWARE[host]
        accelerators = "; ".join(str(item) for item in spec["accelerators"])
        accelerator_text = f"accelerators: {accelerators}" if accelerators else "accelerators: none"
        lines.append(
            f"- {host}: {spec['platform']}; {spec['cpu']}; {spec['cpu_cores']}; "
            f"{spec['memory']}; {accelerator_text}. {spec['role']} {spec['notes']}"
        )
    for host in KNOWN_NON_PRODUCTION_HOSTS:
        spec = hosts.get(host) if isinstance(hosts.get(host), dict) else NEXUS_HOST_HARDWARE[host]
        lines.append(
            f"- {host}: {spec['platform']}; {spec['cpu']}; {spec['cpu_cores']}; {spec['memory']}. "
            f"{spec['role']} {spec['notes']}"
        )
    warnings = snapshot.get("warnings")
    if isinstance(warnings, list) and warnings:
        lines.append("Hardware context warnings: " + " | ".join(str(item) for item in warnings if str(item).strip()))
    return "\n".join(lines)
