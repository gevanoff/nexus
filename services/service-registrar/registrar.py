import base64
import json
import os
import sys
import time
import urllib.error
import urllib.request


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


def _put_record(etcd_url: str, key: str, value: dict[str, str], timeout: float) -> None:
    payload = {
        "key": base64.b64encode(key.encode("utf-8")).decode("ascii"),
        "value": base64.b64encode(json.dumps(value, separators=(",", ":")).encode("utf-8")).decode("ascii"),
    }
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        f"{etcd_url.rstrip('/')}/v3/kv/put",
        data=body,
        headers={"Content-Type": "application/json", "User-Agent": "nexus-service-registrar/1.0"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        status = getattr(response, "status", 200)
        if int(status) >= 400:
            raise RuntimeError(f"etcd returned status {status}")


def main() -> int:
    service_name = _env("NEXUS_SERVICE_NAME")
    service_base_url = _env("NEXUS_SERVICE_BASE_URL")
    etcd_url = _env("ETCD_URL", "http://etcd:2379")
    service_health_url = _env("NEXUS_SERVICE_HEALTH_URL")
    service_metadata_url = _env("NEXUS_SERVICE_METADATA_URL")
    service_backend_class = _env("NEXUS_SERVICE_BACKEND_CLASS")
    prefix = _env("NEXUS_REGISTRY_PREFIX", "/nexus/services/") or "/nexus/services/"
    interval_sec = _env_float("NEXUS_REGISTRATION_INTERVAL_SEC", 30.0)
    timeout_sec = _env_float("NEXUS_REGISTRATION_TIMEOUT_SEC", 5.0)
    retry_sec = _env_float("NEXUS_REGISTRATION_RETRY_SEC", min(interval_sec, 5.0))

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

    healthy_once = False
    while True:
        if service_health_url and not _check_url(service_health_url, timeout_sec):
            _log(f"waiting for healthy service: {service_name} ({service_health_url})")
            time.sleep(retry_sec)
            continue
        try:
            _put_record(etcd_url, key, value, timeout_sec)
            if not healthy_once:
                _log(f"registered {service_name} -> {service_base_url} in etcd {etcd_url}")
                healthy_once = True
        except Exception as exc:
            healthy_once = False
            _log(f"registration failed for {service_name}: {type(exc).__name__}: {exc}")
            time.sleep(retry_sec)
            continue
        time.sleep(interval_sec)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        sys.exit(130)