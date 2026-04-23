import base64
import json
import os
import signal
import sys
import time
import urllib.error
import urllib.parse
import urllib.request


_shutdown_requested = False


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _env_float(name: str, default: float) -> float:
    raw = _env(name)
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise SystemExit(f"Invalid {name}: {raw}") from exc
    if value <= 0:
        raise SystemExit(f"{name} must be greater than zero")
    return value


def _env_int(name: str, default: int) -> int:
    raw = _env(name)
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise SystemExit(f"Invalid {name}: {raw}") from exc
    if value <= 0:
        raise SystemExit(f"{name} must be greater than zero")
    return value


def _log(message: str) -> None:
    print(message, flush=True)


def _check_url(url: str, timeout: float) -> bool:
    request = urllib.request.Request(url, headers={"User-Agent": "nexus-service-registrar/1.0"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status = getattr(response, "status", 200)
            return 200 <= int(status) < 400
    except urllib.error.HTTPError as exc:
        return 200 <= int(exc.code) < 400
    except Exception:
        return False


def _parse_health_urls() -> list[str]:
    raw_urls = _env("NEXUS_SERVICE_HEALTH_URLS")
    if raw_urls:
        urls = [item.strip() for item in raw_urls.replace("\n", ",").split(",")]
        return [item for item in urls if item]
    single = _env("NEXUS_SERVICE_HEALTH_URL")
    return [single] if single else []


def _hostname_from_url(url: str) -> str:
    try:
        return (urllib.parse.urlparse(url).hostname or "").strip()
    except Exception:
        return ""


def _is_service_healthy(urls: list[str], timeout: float) -> bool:
    if not urls:
        return True
    for url in urls:
        if _check_url(url, timeout):
            return True
    return False


def _post_json(url: str, payload: dict[str, object], timeout: float) -> None:
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json", "User-Agent": "nexus-service-registrar/1.0"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        status = getattr(response, "status", 200)
        if int(status) >= 400:
            raise RuntimeError(f"etcd returned status {status}")


def _post_json_response(url: str, payload: dict[str, object], timeout: float) -> dict[str, object]:
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json", "User-Agent": "nexus-service-registrar/1.0"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        status = getattr(response, "status", 200)
        if int(status) >= 400:
            raise RuntimeError(f"etcd returned status {status}")
        raw = response.read().decode("utf-8")
    if not raw.strip():
        return {}
    parsed = json.loads(raw)
    if not isinstance(parsed, dict):
        raise RuntimeError("etcd returned a non-object response")
    return parsed


def _grant_lease(etcd_url: str, ttl_sec: int, timeout: float) -> str:
    response = _post_json_response(
        f"{etcd_url.rstrip('/')}/v3/lease/grant",
        {"TTL": ttl_sec},
        timeout,
    )
    lease_id = str(response.get("ID") or "").strip()
    if not lease_id:
        raise RuntimeError("etcd lease grant did not return an ID")
    return lease_id


def _keepalive_lease(etcd_url: str, lease_id: str, timeout: float) -> None:
    _post_json(
        f"{etcd_url.rstrip('/')}/v3/lease/keepalive",
        {"ID": int(lease_id)},
        timeout,
    )


def _revoke_lease(etcd_url: str, lease_id: str, timeout: float) -> None:
    _post_json(
        f"{etcd_url.rstrip('/')}/v3/lease/revoke",
        {"ID": int(lease_id)},
        timeout,
    )


def _put_record(etcd_url: str, key: str, value: dict[str, str], timeout: float, lease_id: str) -> None:
    payload = {
        "key": base64.b64encode(key.encode("utf-8")).decode("ascii"),
        "value": base64.b64encode(json.dumps(value, separators=(",", ":")).encode("utf-8")).decode("ascii"),
        "lease": int(lease_id),
    }
    _post_json(f"{etcd_url.rstrip('/')}/v3/kv/put", payload, timeout)


def _delete_record(etcd_url: str, key: str, timeout: float) -> None:
    payload = {"key": base64.b64encode(key.encode("utf-8")).decode("ascii")}
    _post_json(f"{etcd_url.rstrip('/')}/v3/kv/deleterange", payload, timeout)


def _request_shutdown(_signum: int, _frame: object) -> None:
    global _shutdown_requested
    _shutdown_requested = True


def main() -> int:
    service_name = _env("NEXUS_SERVICE_NAME")
    service_base_url = _env("NEXUS_SERVICE_BASE_URL")
    etcd_url = _env("ETCD_URL", "http://etcd:2379")
    service_health_urls = _parse_health_urls()
    service_metadata_url = _env("NEXUS_SERVICE_METADATA_URL")
    service_backend_class = _env("NEXUS_SERVICE_BACKEND_CLASS")
    service_hostname = _env("NEXUS_SERVICE_HOSTNAME") or _hostname_from_url(service_base_url)
    prefix = _env("NEXUS_REGISTRY_PREFIX", "/nexus/services/") or "/nexus/services/"
    interval_sec = _env_float("NEXUS_REGISTRATION_INTERVAL_SEC", 30.0)
    timeout_sec = _env_float("NEXUS_REGISTRATION_TIMEOUT_SEC", 5.0)
    retry_sec = _env_float("NEXUS_REGISTRATION_RETRY_SEC", min(interval_sec, 5.0))
    lease_ttl_sec = _env_int("NEXUS_REGISTRATION_LEASE_TTL_SEC", max(int(interval_sec * 3), 60))

    signal.signal(signal.SIGTERM, _request_shutdown)
    signal.signal(signal.SIGINT, _request_shutdown)

    if not service_name:
        raise SystemExit("NEXUS_SERVICE_NAME is required")
    if not service_base_url:
        raise SystemExit("NEXUS_SERVICE_BASE_URL is required")

    key = f"{prefix.rstrip('/')}/{service_name}"
    value = {
        "name": service_name,
        "base_url": service_base_url,
    }
    if service_metadata_url:
        value["metadata_url"] = service_metadata_url
    if service_backend_class:
        value["backend_class"] = service_backend_class
    if service_hostname:
        value["hostname"] = service_hostname

    is_registered = False
    lease_id = ""
    while not _shutdown_requested:
        if not _is_service_healthy(service_health_urls, timeout_sec):
            if is_registered:
                try:
                    if lease_id:
                        _revoke_lease(etcd_url, lease_id, timeout_sec)
                    else:
                        _delete_record(etcd_url, key, timeout_sec)
                    _log(f"deregistered {service_name} after health check failure")
                    is_registered = False
                    lease_id = ""
                except Exception as exc:
                    _log(f"deregistration failed for {service_name}: {type(exc).__name__}: {exc}")
            else:
                _log(f"waiting for healthy service: {service_name} ({', '.join(service_health_urls)})")
            time.sleep(retry_sec)
            continue
        try:
            if not lease_id:
                lease_id = _grant_lease(etcd_url, lease_ttl_sec, timeout_sec)
            else:
                _keepalive_lease(etcd_url, lease_id, timeout_sec)
            _put_record(etcd_url, key, value, timeout_sec, lease_id)
            if not is_registered:
                _log(
                    f"registered {service_name} -> {service_base_url} in etcd {etcd_url} "
                    f"with lease TTL {lease_ttl_sec}s"
                )
                is_registered = True
        except Exception as exc:
            is_registered = False
            lease_id = ""
            _log(f"registration failed for {service_name}: {type(exc).__name__}: {exc}")
            time.sleep(retry_sec)
            continue
        time.sleep(interval_sec)

    if is_registered:
        try:
            if lease_id:
                _revoke_lease(etcd_url, lease_id, timeout_sec)
            else:
                _delete_record(etcd_url, key, timeout_sec)
            _log(f"deregistered {service_name} during shutdown")
        except Exception as exc:
            _log(f"shutdown deregistration failed for {service_name}: {type(exc).__name__}: {exc}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        sys.exit(130)
