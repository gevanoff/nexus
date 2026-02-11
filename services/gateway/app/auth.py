from __future__ import annotations

import functools
import ipaddress
import json
import logging
from fastapi import HTTPException, Request

from app.config import S

logger = logging.getLogger(__name__)


def _parse_allowlist(raw: str) -> list[ipaddress._BaseNetwork]:  # type: ignore[attr-defined]
    out: list[ipaddress._BaseNetwork] = []  # type: ignore[attr-defined]
    for part in (raw or "").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            # If it's a single IP, treat as /32 (or /128).
            if "/" not in part:
                ip = ipaddress.ip_address(part)
                part = f"{ip}/{32 if ip.version == 4 else 128}"
            out.append(ipaddress.ip_network(part, strict=False))
        except Exception:
            continue
    return out


def _client_ip_allowed(req: Request, *, raw_allowlist: str) -> bool:
    raw = (raw_allowlist or "").strip()
    if not raw:
        return True
    try:
        host = (getattr(getattr(req, "client", None), "host", None) or "").strip()
        if not host:
            return False
        ip = ipaddress.ip_address(host)
    except Exception:
        return False

    nets = _parse_allowlist(raw)
    if not nets:
        return False
    return any(ip in net for net in nets)


@functools.lru_cache(maxsize=16)
def _parse_token_policies(raw: str) -> tuple[dict[str, dict], bool]:
    if not raw:
        return {}, True
    try:
        obj = json.loads(raw)
    except Exception as e:
        logger.warning("Invalid GATEWAY_TOKEN_POLICIES_JSON (parse error): %s", e)
        return {}, False
    if not isinstance(obj, dict):
        logger.warning("Invalid GATEWAY_TOKEN_POLICIES_JSON (expected object at top-level)")
        return {}, False

    out: dict[str, dict] = {}
    for k, v in obj.items():
        if isinstance(k, str) and k and isinstance(v, dict):
            out[k] = v
    return out, True


def _load_token_policies() -> tuple[dict[str, dict], bool]:
    raw = (getattr(S, "GATEWAY_TOKEN_POLICIES_JSON", "") or "").strip()
    return _parse_token_policies(raw)


def bearer_token_from_headers(headers: dict[str, str] | None) -> str:
    try:
        auth = (headers or {}).get("authorization") or (headers or {}).get("Authorization") or ""
    except Exception:
        auth = ""
    auth = (auth or "").strip()
    if not auth.lower().startswith("bearer "):
        return ""
    return auth.split(" ", 1)[1].strip()


def token_policy_for_token(token: str) -> dict:
    if not isinstance(token, str) or not token.strip():
        return {}
    pols, _ok = _load_token_policies()
    return pols.get(token.strip(), {})


def _allowed_bearer_tokens() -> set[str]:
    raw = (getattr(S, "GATEWAY_BEARER_TOKENS", "") or "").strip()
    if raw:
        return {p.strip() for p in raw.split(",") if p.strip()}
    # Back-compat: single-token mode.
    return {S.GATEWAY_BEARER_TOKEN}


def require_bearer(req: Request) -> None:
    auth = req.headers.get("authorization", "")
    if not auth.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Missing bearer token")
    token = auth.split(" ", 1)[1].strip()
    if token not in _allowed_bearer_tokens():
        raise HTTPException(status_code=403, detail="Invalid bearer token")

    # Load token policies once per request and optionally fail closed if configured.
    pols, pols_ok = _load_token_policies()
    if (getattr(S, "GATEWAY_TOKEN_POLICIES_STRICT", False) is True) and (not pols_ok):
        raise HTTPException(status_code=500, detail="Token policy config invalid")

    # Attach token/policy for downstream handlers.
    policy = pols.get(token, {}) if isinstance(pols, dict) else {}
    try:
        req.state.bearer_token = token
        req.state.token_policy = policy
    except Exception:
        pass

    # IP allowlist check (global or per-token override).
    raw_allowlist = ""
    try:
        if isinstance(policy, dict):
            raw_allowlist = (policy.get("ip_allowlist") or "").strip()
        if not raw_allowlist:
            raw_allowlist = (getattr(S, "IP_ALLOWLIST", "") or "").strip()
    except Exception:
        raw_allowlist = (getattr(S, "IP_ALLOWLIST", "") or "").strip()

    if raw_allowlist:
        try:
            if not _client_ip_allowed(req, raw_allowlist=raw_allowlist):
                raise HTTPException(status_code=403, detail="Client IP not allowed")
        except HTTPException:
            raise
        except Exception:
            # If allowlist parsing fails, fail closed.
            raise HTTPException(status_code=403, detail="Client IP not allowed")
