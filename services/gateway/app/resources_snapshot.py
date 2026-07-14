from __future__ import annotations

import os
import time
from typing import Any, Dict, List, Optional
from urllib.parse import urlsplit

import httpx
from fastapi import HTTPException

from app.backends import backend_hostname, get_registry, get_registry_sync_status, get_service_record_for_backend
from app.config import S
from app.health_checker import get_health_checker
from app.model_aliases import get_aliases, get_aliases_state


def backend_location_details(registry: Any, backend_name: str, *, base_url: str = "") -> Dict[str, str]:
    record = get_service_record_for_backend(backend_name, registry=registry)
    effective_base_url = ((record.base_url if record is not None else "") or base_url).strip()
    host = backend_hostname(backend_name, registry=registry, fallback_base_url=effective_base_url)
    details: Dict[str, str] = {}
    if effective_base_url:
        details["base_url"] = effective_base_url
    if host:
        details["host"] = host
        details["hostname"] = host
    return details


def status_exception_text(exc: Exception) -> str:
    if isinstance(exc, HTTPException):
        detail = exc.detail
        if isinstance(detail, dict):
            error = str(detail.get("error") or "").strip()
            message = str(detail.get("message") or "").strip()
            if error and message:
                return f"{error}: {message}"
            if error:
                return error
            if message:
                return message
            try:
                import json

                return json.dumps(detail, ensure_ascii=False, sort_keys=True)
            except Exception:
                return str(detail)
        if detail:
            return str(detail)
    return f"{type(exc).__name__}: {exc}"


def service_host_from_url(raw_url: str) -> str:
    value = str(raw_url or "").strip()
    if not value:
        return ""
    try:
        parsed = urlsplit(value)
        return (parsed.hostname or "").strip() or value
    except Exception:
        return value


def lifecycle_manager_base_url() -> str:
    return (getattr(S, "LIFECYCLE_MANAGER_BASE_URL", "") or os.environ.get("LIFECYCLE_MANAGER_BASE_URL") or "").strip().rstrip("/")


def lifecycle_timeout() -> float:
    try:
        return float(getattr(S, "LIFECYCLE_MANAGER_TIMEOUT_SEC", 15.0) or 15.0)
    except Exception:
        return 15.0


def telegram_gateway_dependency(
    registry: Any, checker: Any, aliases: Dict[str, Any], model: str = ""
) -> tuple[bool, str]:
    model = (model or os.getenv("TELEGRAM_GATEWAY_MODEL") or "fast").strip()
    alias = aliases.get(model.lower())
    backend_name = ""
    if alias is not None:
        backend_name = registry.resolve_backend_class(alias.backend) or alias.backend
    elif ":" in model:
        backend_name = registry.resolve_backend_class(model.split(":", 1)[0]) or model.split(":", 1)[0]
    else:
        backend_name = registry.resolve_backend_class(model) or model
    status = checker.get_status(backend_name) if backend_name else None
    if status is None:
        return False, f"gateway model {model or 'unknown'} has no backend health state"
    if not status.is_ready:
        return False, f"gateway model {model} backend {backend_name} is not ready: {status.error or 'readiness check failed'}"
    return True, f"gateway model {model} backend {backend_name} is ready"


async def call_lifecycle_manager(method: str, path: str, *, json_body: Optional[Dict[str, Any]] = None, timeout: Optional[float] = None) -> Dict[str, Any]:
    base = lifecycle_manager_base_url()
    if not base:
        raise HTTPException(status_code=503, detail={"error": "lifecycle_manager_unconfigured"})
    if not path.startswith("/"):
        path = "/" + path
    async with httpx.AsyncClient(timeout=timeout or lifecycle_timeout()) as client:
        try:
            response = await client.request(method.upper(), f"{base}{path}", json=json_body)
        except httpx.RequestError as exc:
            raise HTTPException(
                status_code=503,
                detail={"error": "lifecycle_manager_unreachable", "message": str(exc)},
            ) from exc
    if response.status_code >= 400:
        detail: Any
        try:
            detail = response.json()
        except Exception:
            detail = response.text[:1000]
        raise HTTPException(status_code=response.status_code, detail=detail)
    try:
        data = response.json()
    except Exception as exc:
        raise HTTPException(status_code=502, detail={"error": "lifecycle_manager_bad_response"}) from exc
    if not isinstance(data, dict):
        raise HTTPException(status_code=502, detail={"error": "lifecycle_manager_bad_response"})
    return data


async def build_registry_backend_status_payload() -> Dict[str, Any]:
    registry = get_registry()
    checker = get_health_checker()
    aliases = get_aliases()
    alias_state = get_aliases_state()
    alias_map: Dict[str, List[Dict[str, str]]] = {}
    for alias_name, target_backend in registry.legacy_mapping.items():
        if not isinstance(alias_name, str) or not isinstance(target_backend, str):
            continue
        alias_map.setdefault(target_backend, []).append(
            {"name": alias_name, "target": target_backend, "kind": "legacy"}
        )
    for alias_name, alias in aliases.items():
        resolved_backend = registry.resolve_backend_class(alias.backend)
        alias_map.setdefault(resolved_backend, []).append(
            {
                "name": alias_name,
                "target": f"{alias.backend}:{alias.upstream_model}",
                "kind": "model",
            }
        )

    lifecycle_by_backend: Dict[str, Dict[str, Any]] = {}
    lifecycle_payload: Dict[str, Any] = {}
    lifecycle_error = ""
    try:
        lifecycle_payload = await call_lifecycle_manager("GET", "/v1/lifecycle/status", timeout=3.0)
        lifecycle_backends = lifecycle_payload.get("backends") if isinstance(lifecycle_payload, dict) else None
        if isinstance(lifecycle_backends, list):
            for item in lifecycle_backends:
                if not isinstance(item, dict):
                    continue
                key = str(item.get("backend_class") or "").strip()
                if key:
                    resolved_key = registry.resolve_backend_class(key)
                    if resolved_key in registry.backends:
                        lifecycle_by_backend[resolved_key] = item
                    else:
                        lifecycle_by_backend[key] = item
    except Exception as exc:
        lifecycle_error = status_exception_text(exc)
        lifecycle_by_backend = {}

    backends = []
    for backend_class, config in registry.backends.items():
        entry: Dict[str, Any] = {
            "backend_class": backend_class,
            "provider": config.provider,
            "description": config.description,
            "base_url": config.base_url,
            "capabilities": list(config.supported_capabilities),
            "health": {
                "liveness": config.health_liveness,
                "readiness": config.health_readiness,
            },
        }
        alias_entries = alias_map.get(backend_class)
        if alias_entries:
            entry["aliases"] = sorted(alias_entries, key=lambda item: item.get("name") or "")
        location = backend_location_details(registry, backend_class, base_url=config.base_url)
        if location.get("host"):
            entry["host"] = location["host"]
        if location.get("hostname"):
            entry["hostname"] = location["hostname"]
        status = checker.get_status(backend_class)
        if status is not None:
            entry.update(
                {
                    "healthy": status.is_healthy,
                    "ready": status.is_ready,
                    "last_check": status.last_check,
                    "error": status.error,
                    "consecutive_failures": status.consecutive_failures,
                    "first_failure_at": status.first_failure_at,
                    "last_success_at": status.last_success_at,
                    "raw_healthy": status.raw_healthy,
                    "raw_ready": status.raw_ready,
                    "raw_error": status.raw_error,
                    "suppressed_error": status.suppressed_error,
                    "gateway_health": {
                        "healthy": status.is_healthy,
                        "ready": status.is_ready,
                        "last_check": status.last_check,
                        "error": status.error,
                        "consecutive_failures": status.consecutive_failures,
                        "first_failure_at": status.first_failure_at,
                        "last_success_at": status.last_success_at,
                        "raw_healthy": status.raw_healthy,
                        "raw_ready": status.raw_ready,
                        "raw_error": status.raw_error,
                        "suppressed_error": status.suppressed_error,
                    },
                }
            )
        lifecycle_entry = lifecycle_by_backend.get(backend_class)
        if lifecycle_entry:
            entry["lifecycle"] = lifecycle_entry
            for key in (
                "active",
                "healthy",
                "ready",
                "drained",
                "drain_reason",
                "tier",
                "tier_rank",
                "display_name",
                "estimated_vram_mb",
                "idle_observed_vram_mb",
                "peak_observed_vram_mb",
                "auto_start",
                "auto_stop",
                "requires_confirmation",
                "compose_managed",
                "status",
                "status_label",
                "status_color",
                "status_rank",
                "last_checked_at",
                "last_healthy_at",
                "last_ready_at",
                "last_confirmed_working_at",
                "last_unhealthy_at",
                "last_stopped_at",
                "last_health_error",
                "last_action",
                "last_action_at",
                "last_action_error",
                "last_restart_at",
                "canary_enabled",
                "canary_path",
                "canary_method",
                "canary_timeout_sec",
                "canary_failure_threshold",
                "canary_consecutive_failures",
                "canary_last_checked_at",
                "canary_last_success_at",
                "canary_last_error",
                "inflight",
            ):
                if key in lifecycle_entry:
                    entry[key] = lifecycle_entry[key]
            if lifecycle_entry.get("host"):
                entry["lifecycle_host"] = lifecycle_entry.get("host")
        if status is not None:
            check_interval = float(getattr(checker, "check_interval", 30.0) or 30.0)
            health_is_fresh = (time.time() - float(status.last_check or 0)) <= (check_interval * 3)
            lifecycle_blocks_ready = lifecycle_entry is not None and lifecycle_entry.get("ready") is False
            entry["healthy"] = status.is_healthy
            if not lifecycle_blocks_ready:
                entry["ready"] = status.is_ready
            entry["last_check"] = status.last_check
            if status.error:
                entry["error"] = status.error
                entry["health_error"] = status.error
            elif entry.get("health_error") and status.is_ready:
                entry["health_error"] = ""
            if status.is_ready and health_is_fresh and not lifecycle_blocks_ready:
                entry["active"] = True
                entry["status"] = "gateway_ready"
                entry["status_label"] = "Reachable and ready"
                entry["status_color"] = "green"
                entry["status_rank"] = 0
                entry["health_error"] = ""
                entry["last_health_error"] = ""
                entry["last_action_error"] = ""
        backends.append(entry)

    lifecycle_core = (
        lifecycle_payload.get("core_services")
        if isinstance(lifecycle_payload.get("core_services"), list)
        else []
    )
    lifecycle_core_by_id = {
        str(item.get("service_id") or ""): item
        for item in lifecycle_core
        if isinstance(item, dict) and item.get("service_id")
    }
    telegram_bot_specs = (
        (
            "hex",
            "Hex",
            "@CrypticHex_bot",
            "stackrot",
            "TELEGRAM_STACKROT_TOKEN",
            "TELEGRAM_STACKROT_MODEL",
            "stackrot-chat",
            "telegram_bridge_hex",
        ),
        (
            "tess",
            "Tess",
            "@Ms_Tess_bot",
            "ada2",
            "TELEGRAM_ADA2_TOKEN",
            "TELEGRAM_ADA2_MODEL",
            "ada2-chat",
            "telegram_bridge_tess",
        ),
        (
            "clarion",
            "Clarion",
            "@Dr_Clarion_bot",
            "ai2",
            "TELEGRAM_AI2_TOKEN",
            "TELEGRAM_AI2_MODEL",
            "ai2-chat",
            "telegram_bridge_clarion",
        ),
    )
    telegram_bots: list[Dict[str, Any]] = []
    for bot_id, bot_name, bot_username, host, token_env, model_env, default_model, runtime_id in telegram_bot_specs:
        runtime = lifecycle_core_by_id.get(runtime_id) or {}
        runtime_known = bool(runtime)
        runtime_ok = runtime.get("active") is True
        entry: Dict[str, Any] = {
            "service_id": f"telegram_bot_{bot_id}",
            "display_name": bot_name,
            "bot_username": bot_username,
            "host": host,
            "endpoint": "https://api.telegram.org",
            "gateway_model": (os.getenv(model_env) or default_model).strip(),
            "runtime": {
                "service_id": runtime_id,
                "known": runtime_known,
                "active": runtime_ok,
                "status": runtime.get("status") or "unknown",
                "status_label": runtime.get("status_label") or "Runtime status unavailable",
                "containers": runtime.get("containers") if isinstance(runtime.get("containers"), list) else [],
                "missing_components": runtime.get("missing_components") if isinstance(runtime.get("missing_components"), list) else [],
                "host_error": runtime.get("host_error") or "",
                "updated_at": runtime.get("updated_at") or 0,
            },
        }
        token = (
            os.getenv(token_env)
            or (os.getenv("TELEGRAM_TOKEN") if bot_id == "clarion" else "")
            or ""
        ).strip()
        if not token:
            entry.update(
                {
                    "healthy": False,
                    "ready": False,
                    "active": False,
                    "status": "unconfigured",
                    "status_label": "unconfigured",
                    "status_color": "yellow",
                    "status_rank": 1,
                    "updated_at": time.time(),
                    "error": f"{token_env} not configured",
                    "notes": f"{token_env} not configured · bridge runtime {entry['runtime']['status_label']}",
                }
            )
        else:
            try:
                async with httpx.AsyncClient(timeout=5.0) as client:
                    resp = await client.get(f"https://api.telegram.org/bot{token}/getMe")
                api_payload = resp.json() if resp.content else {}
                telegram_ok = bool(
                    resp.status_code == 200
                    and isinstance(api_payload, dict)
                    and api_payload.get("ok") is True
                )
                actual_username = (
                    str((api_payload.get("result") or {}).get("username") or "").strip()
                    if isinstance(api_payload, dict)
                    else ""
                )
                if actual_username:
                    entry["api_username"] = f"@{actual_username}"
                gateway_ok, gateway_note = telegram_gateway_dependency(
                    registry, checker, aliases, entry["gateway_model"]
                )
                ok = telegram_ok and gateway_ok and runtime_ok
                entry.update(
                    {
                        "active": ok,
                        "healthy": ok,
                        "ready": ok,
                        "status": "healthy" if ok else "error",
                        "status_label": "healthy" if ok else "error",
                        "status_color": "green" if ok else "red",
                        "status_rank": 0 if ok else 3,
                        "last_check": time.time(),
                        "updated_at": time.time(),
                    }
                )
                if not telegram_ok:
                    entry["error"] = f"telegram getMe failed (status {resp.status_code})"
                    entry["notes"] = entry["error"]
                elif not gateway_ok:
                    entry["error"] = gateway_note
                    entry["notes"] = f"Bridge runtime {entry['runtime']['status_label']} · Telegram getMe succeeded · {gateway_note}"
                elif not runtime_ok:
                    entry["error"] = f"bridge runtime {entry['runtime']['status_label']}"
                    entry["notes"] = f"Bridge runtime {entry['runtime']['status_label']} · Telegram getMe succeeded · {gateway_note}"
                else:
                    entry["notes"] = f"Bridge runtime {entry['runtime']['status_label']} · Telegram getMe succeeded · {gateway_note}"
            except Exception as exc:
                entry.update(
                    {
                        "active": False,
                        "healthy": False,
                        "ready": False,
                        "status": "error",
                        "status_label": "error",
                        "status_color": "red",
                        "status_rank": 3,
                        "last_check": time.time(),
                        "updated_at": time.time(),
                        "error": f"telegram check failed: {type(exc).__name__}: {exc}",
                        "notes": f"telegram check failed: {type(exc).__name__}: {exc}",
                    }
                )
        telegram_bots.append(entry)

    now = time.time()
    control_plane: list[Dict[str, Any]] = []

    lifecycle_base = lifecycle_manager_base_url()
    lifecycle_hosts = lifecycle_payload.get("hosts") if isinstance(lifecycle_payload.get("hosts"), list) else []
    lifecycle_backends_list = lifecycle_payload.get("backends") if isinstance(lifecycle_payload.get("backends"), list) else []
    lifecycle_ok = bool(lifecycle_base) and not lifecycle_error
    lifecycle_notes: list[str] = []
    if lifecycle_base:
        lifecycle_notes.append(f"endpoint {lifecycle_base}")
    if lifecycle_ok:
        lifecycle_notes.append(f"{len(lifecycle_hosts)} hosts")
        lifecycle_notes.append(f"{len(lifecycle_backends_list)} backends")
        if lifecycle_core:
            lifecycle_notes.append(f"{len(lifecycle_core)} core services")
    elif lifecycle_error:
        lifecycle_notes.append(lifecycle_error)
    control_plane.append(
        {
            "service_id": "lifecycle_manager",
            "display_name": "Lifecycle Manager",
            "host": service_host_from_url(lifecycle_base),
            "endpoint": lifecycle_base,
            "active": lifecycle_ok,
            "healthy": lifecycle_ok,
            "ready": lifecycle_ok,
            "status": "reachable" if lifecycle_ok else ("unconfigured" if not lifecycle_base else "error"),
            "status_label": "reachable" if lifecycle_ok else ("unconfigured" if not lifecycle_base else "unreachable"),
            "status_color": "green" if lifecycle_ok else ("yellow" if not lifecycle_base else "red"),
            "status_rank": 0 if lifecycle_ok else (1 if not lifecycle_base else 2),
            "updated_at": float(lifecycle_payload.get("generated_at") or now) if lifecycle_ok else now,
            "notes": " · ".join(part for part in lifecycle_notes if part),
        }
    )

    registry_sync = get_registry_sync_status()
    etcd_url = str(registry_sync.get("etcd_url") or "").strip()
    etcd_error = str(registry_sync.get("last_error") or "").strip()
    etcd_enabled = bool(registry_sync.get("enabled"))
    etcd_ok = etcd_enabled and not etcd_error and float(registry_sync.get("last_success") or 0) > 0
    etcd_notes: list[str] = []
    if etcd_url:
        etcd_notes.append(f"endpoint {etcd_url}")
    prefix = str(registry_sync.get("prefix") or "").strip()
    if prefix:
        etcd_notes.append(f"prefix {prefix}")
    seeded_count = int(registry_sync.get("last_seeded_count") or 0)
    if seeded_count:
        etcd_notes.append(f"{seeded_count} env-seeded")
    if etcd_enabled:
        etcd_notes.append(f"{int(registry_sync.get('last_etcd_count') or 0)} discovered")
    effective_count = int(registry_sync.get("last_effective_count") or 0)
    if effective_count:
        etcd_notes.append(f"{effective_count} active records")
    if etcd_error:
        etcd_notes.append(etcd_error)
    control_plane.append(
        {
            "service_id": "etcd_service_discovery",
            "display_name": "etcd Service Discovery",
            "host": service_host_from_url(etcd_url),
            "endpoint": etcd_url,
            "active": etcd_ok,
            "healthy": etcd_ok,
            "ready": etcd_ok,
            "status": "healthy" if etcd_ok else ("disabled" if not etcd_enabled else ("error" if etcd_error else "pending")),
            "status_label": "healthy" if etcd_ok else ("disabled" if not etcd_enabled else ("error" if etcd_error else "pending")),
            "status_color": "green" if etcd_ok else ("grey" if not etcd_enabled else ("red" if etcd_error else "yellow")),
            "status_rank": 0 if etcd_ok else (1 if not etcd_enabled else (2 if not etcd_error else 3)),
            "updated_at": float(registry_sync.get("last_success") or registry_sync.get("last_attempt") or now),
            "notes": " · ".join(part for part in etcd_notes if part),
        }
    )

    checker_snapshot = checker.status_snapshot()
    checker_running = bool(checker_snapshot.get("running"))
    checker_notes = [
        f"interval {float(checker_snapshot.get('check_interval') or 0):g}s",
        f"tracking {int(checker_snapshot.get('tracked_backends') or 0)} backends",
    ]
    ready_count = int(checker_snapshot.get("ready_backends") or 0)
    if ready_count > 0:
        checker_notes.append(f"{ready_count} ready")
    unhealthy_count = int(checker_snapshot.get("unhealthy_backends") or 0)
    if unhealthy_count > 0:
        checker_notes.append(f"{unhealthy_count} unhealthy")
    control_plane.append(
        {
            "service_id": "gateway_health_checks",
            "display_name": "Gateway Health Checks",
            "host": "gateway",
            "active": checker_running,
            "healthy": checker_running,
            "ready": checker_running,
            "status": "running" if checker_running else "stopped",
            "status_label": "running" if checker_running else "stopped",
            "status_color": "green" if checker_running else "red",
            "status_rank": 0 if checker_running else 2,
            "updated_at": float(checker_snapshot.get("last_check") or now),
            "notes": " · ".join(part for part in checker_notes if part),
        }
    )
    backends.sort(key=lambda item: item.get("backend_class") or "")
    return {
        "generated_at": time.time(),
        "settings": {
            "health_poll_interval_sec": getattr(checker, "check_interval", 30.0),
            "health_timeout_sec": getattr(checker, "timeout", 0.0),
            "health_failure_threshold": checker_snapshot.get("failure_threshold"),
            "health_failure_grace_sec": checker_snapshot.get("failure_grace_sec"),
        },
        "alias_config": {
            "source": alias_state.source,
            "configured_path": alias_state.configured_path,
            "error": alias_state.error,
        },
        "control_plane": control_plane,
        "telegram_bots": telegram_bots,
        "backends": backends,
    }


def merge_resources_payloads(lifecycle_payload: Dict[str, Any] | None, registry_payload: Dict[str, Any] | None) -> Dict[str, Any]:
    if not registry_payload or not isinstance(registry_payload, dict):
        return lifecycle_payload or {}
    base = dict(lifecycle_payload) if isinstance(lifecycle_payload, dict) else {}
    lifecycle_backends = lifecycle_payload.get("backends") if isinstance(lifecycle_payload, dict) and isinstance(lifecycle_payload.get("backends"), list) else []
    registry_backends = registry_payload.get("backends") if isinstance(registry_payload.get("backends"), list) else []
    merged: dict[str, Dict[str, Any]] = {}
    for backend in lifecycle_backends:
        key = str((backend or {}).get("backend_class") or "").strip()
        if key:
            merged[key] = dict(backend)
    for backend in registry_backends:
        key = str((backend or {}).get("backend_class") or "").strip()
        if not key:
            continue
        existing = merged.get(key, {})
        merged[key] = {
            **existing,
            **backend,
            "capabilities": existing.get("capabilities") if isinstance(existing.get("capabilities"), list) and existing.get("capabilities") else backend.get("capabilities"),
            "aliases": backend.get("aliases") if isinstance(backend.get("aliases"), list) else existing.get("aliases"),
            "description": backend.get("description") or existing.get("description"),
            "provider": backend.get("provider") or existing.get("provider"),
            "base_url": backend.get("base_url") or existing.get("base_url"),
            "health": backend.get("health") or existing.get("health"),
            "hostname": existing.get("hostname") or backend.get("hostname"),
        }
    if merged:
        base["backends"] = list(merged.values())
    if isinstance(lifecycle_payload, dict) and isinstance(lifecycle_payload.get("core_services"), list):
        base["core_services"] = lifecycle_payload["core_services"]
    if isinstance(registry_payload.get("control_plane"), list):
        base["control_plane"] = registry_payload["control_plane"]
    if isinstance(registry_payload.get("telegram_bots"), list):
        base["telegram_bots"] = registry_payload["telegram_bots"]
    if registry_payload.get("alias_config"):
        base["alias_config"] = registry_payload["alias_config"]
    base["settings"] = {
        **(registry_payload.get("settings") if isinstance(registry_payload.get("settings"), dict) else {}),
        **(lifecycle_payload.get("settings") if isinstance(lifecycle_payload, dict) and isinstance(lifecycle_payload.get("settings"), dict) else {}),
    }
    base["generated_at"] = float(
        (lifecycle_payload.get("generated_at") if isinstance(lifecycle_payload, dict) else 0)
        or registry_payload.get("generated_at")
        or time.time()
    )
    return base


async def build_resources_snapshot(*, refresh_lifecycle: bool = False) -> Dict[str, Any]:
    lifecycle_payload: Dict[str, Any] | None = None
    registry_payload: Dict[str, Any] | None = None
    lifecycle_error = ""
    registry_error = ""

    lifecycle_path = "/v1/lifecycle/status?refresh=true" if refresh_lifecycle else "/v1/lifecycle/status"
    try:
        lifecycle_payload = await call_lifecycle_manager("GET", lifecycle_path, timeout=max(lifecycle_timeout(), 30.0))
    except Exception as exc:
        lifecycle_error = status_exception_text(exc)

    try:
        registry_payload = await build_registry_backend_status_payload()
    except Exception as exc:
        registry_error = status_exception_text(exc)

    payload = merge_resources_payloads(lifecycle_payload, registry_payload)
    if not payload:
        return {
            "ok": False,
            "error": lifecycle_error or registry_error or "resources unavailable",
            "lifecycle_error": lifecycle_error,
            "registry_error": registry_error,
            "resources": {},
        }

    return {
        "ok": True,
        "resources": payload,
        "lifecycle_error": lifecycle_error,
        "registry_error": registry_error,
        "sources": {
            "lifecycle": {"ok": bool(lifecycle_payload), "error": lifecycle_error},
            "registry": {"ok": bool(registry_payload), "error": registry_error},
        },
    }
