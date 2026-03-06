from __future__ import annotations

import functools
import ipaddress
import json
import logging
from fastapi import HTTPException, Request

from app.config import S
from app import user_store

logger = logging.getLogger(__name__)


def _peer_ip(req: Request) -> str:
    try:
        c = req.client
        return (c.host or "").strip() if c else ""
    except Exception:
        return ""


def _parse_forwarded_for(header_value: str) -> str:
    raw = (header_value or "").strip()
    if not raw:
        return ""
    for part in raw.split(","):
        token = part.strip().strip('"').strip()
        if not token:
            continue
        if token.startswith("[") and token.endswith("]"):
            token = token[1:-1].strip()
        if token.startswith("for="):
            token = token[4:].strip().strip('"').strip()
        if token.startswith("[") and "]" in token:
            token = token[1: token.index("]")]
        if token.count(":") == 1 and token.rsplit(":", 1)[1].isdigit() and "." in token:
            token = token.rsplit(":", 1)[0]
        try:
            ipaddress.ip_address(token)
            return token
        except Exception:
            continue
    return ""


def _parse_proxy_trust(raw: str) -> list[ipaddress._BaseNetwork]:  # type: ignore[attr-defined]
    out: list[ipaddress._BaseNetwork] = []  # type: ignore[attr-defined]
    for part in (raw or "").split(","):
        s = part.strip()
        if not s:
            continue
        try:
            if "/" not in s:
                ip = ipaddress.ip_address(s)
                s = f"{ip}/{32 if ip.version == 4 else 128}"
            out.append(ipaddress.ip_network(s, strict=False))
        except Exception:
            continue
    return out


def _ip_in_networks(ip_s: str, networks: list[ipaddress._BaseNetwork]) -> bool:  # type: ignore[attr-defined]
    try:
        ip = ipaddress.ip_address((ip_s or "").strip())
    except Exception:
        return False
    for net in networks:
        try:
            if ip in net:
                return True
        except Exception:
            continue
    return False


def _client_ip(req: Request) -> str:
    # SYNC-CHECK(proxy-ip-resolution): keep behavior aligned with UI routes.
    peer = _peer_ip(req)
    trusted_raw = (getattr(S, "TRUST_PROXY_CIDRS", "") or "").strip()
    trusted = _parse_proxy_trust(trusted_raw)
    if not trusted or not _ip_in_networks(peer, trusted):
        return peer

    try:
        xff = (req.headers.get("x-forwarded-for") or "").strip()
    except Exception:
        xff = ""
    parsed_xff = _parse_forwarded_for(xff)
    if parsed_xff:
        return parsed_xff

    try:
        xri = (req.headers.get("x-real-ip") or "").strip()
    except Exception:
        xri = ""
    parsed_xri = _parse_forwarded_for(xri)
    if parsed_xri:
        return parsed_xri

    try:
        fwd = (req.headers.get("forwarded") or "").strip()
    except Exception:
        fwd = ""
    parsed_fwd = _parse_forwarded_for(fwd)
    if parsed_fwd:
        return parsed_fwd

    return peer


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
        host = _client_ip(req)
        if not host:
            return False
        ip = ipaddress.ip_address(host)
    except Exception:
        return False

    nets = _parse_allowlist(raw)
    if not nets:
        return False
    return any(ip in net for net in nets)


def _ip_allowlist_detail(req: Request, message: str) -> object:
    if not bool(getattr(S, "IP_ALLOWLIST_DEBUG", False)):
        return message
    try:
        headers = req.headers
    except Exception:
        headers = {}
    return {
        "error": "ip_allowlist_denied",
        "message": message,
        "client_ip": _client_ip(req),
        "peer_ip": _peer_ip(req),
        "x_forwarded_for": (headers.get("x-forwarded-for") or "").strip(),
        "x_real_ip": (headers.get("x-real-ip") or "").strip(),
        "forwarded": (headers.get("forwarded") or "").strip(),
        "ip_allowlist": (getattr(S, "IP_ALLOWLIST", "") or "").strip(),
        "trust_proxy_cidrs": (getattr(S, "TRUST_PROXY_CIDRS", "") or "").strip(),
    }


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
    token_clean = token.strip()

    pols, _ok = _load_token_policies()
    static_policy = pols.get(token_clean, {})
    if isinstance(static_policy, dict) and static_policy:
        return static_policy

    # API-key policy lookup (best-effort; no auth decision here).
    try:
        if not bool(getattr(S, "USER_AUTH_ENABLED", True)):
            return {}
        resolved = user_store.get_user_by_api_key(S.USER_DB_PATH, token=token_clean, touch_last_used=False)
        if not resolved:
            return {}
        _user, key_meta = resolved
        policy = key_meta.get("policy") if isinstance(key_meta, dict) else {}
        return policy if isinstance(policy, dict) else {}
    except Exception:
        return {}


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
    static_token_ok = token in _allowed_bearer_tokens()

    key_user = None
    key_meta = None
    if not static_token_ok:
        try:
            if bool(getattr(S, "USER_AUTH_ENABLED", True)):
                resolved = user_store.get_user_by_api_key(S.USER_DB_PATH, token=token, touch_last_used=True)
                if resolved:
                    key_user, key_meta = resolved
        except Exception:
            key_user = None
            key_meta = None

    if not static_token_ok and key_user is None:
        raise HTTPException(status_code=403, detail="Invalid bearer token")

    # Load token policies once per request and optionally fail closed if configured.
    pols, pols_ok = _load_token_policies()
    if (getattr(S, "GATEWAY_TOKEN_POLICIES_STRICT", False) is True) and (not pols_ok):
        raise HTTPException(status_code=500, detail="Token policy config invalid")

    # Attach token/policy for downstream handlers.
    policy = pols.get(token, {}) if isinstance(pols, dict) else {}
    if not isinstance(policy, dict):
        policy = {}
    if key_meta is not None and isinstance(key_meta, dict):
        key_policy = key_meta.get("policy")
        if isinstance(key_policy, dict):
            policy = key_policy
    try:
        req.state.bearer_token = token
        req.state.token_policy = policy
        req.state.auth_kind = "api_key" if key_user is not None else "static_bearer"
        if key_user is not None:
            req.state.user = key_user
            req.state.api_key = key_meta
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
                raise HTTPException(status_code=403, detail=_ip_allowlist_detail(req, "Client IP not allowed"))
        except HTTPException:
            raise
        except Exception:
            # If allowlist parsing fails, fail closed.
            raise HTTPException(status_code=403, detail=_ip_allowlist_detail(req, "Client IP not allowed"))
