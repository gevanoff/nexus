from __future__ import annotations

import base64
import hashlib
import hmac
from dataclasses import dataclass

from cryptography.fernet import Fernet, InvalidToken


class SocialSecretError(RuntimeError):
    pass


@dataclass(frozen=True)
class SecretBox:
    _fernet: Fernet
    _signing_key: bytes

    @classmethod
    def from_key(cls, key: str) -> "SecretBox":
        raw = (key or "").strip()
        if not raw:
            raise SocialSecretError("SOCIAL_TOKEN_ENCRYPTION_KEY is not configured")
        try:
            decoded = base64.urlsafe_b64decode(raw.encode("ascii"))
        except Exception as exc:
            raise SocialSecretError("SOCIAL_TOKEN_ENCRYPTION_KEY must be a Fernet key") from exc
        if len(decoded) != 32:
            raise SocialSecretError("SOCIAL_TOKEN_ENCRYPTION_KEY must decode to 32 bytes")
        try:
            fernet = Fernet(raw.encode("ascii"))
        except Exception as exc:
            raise SocialSecretError("SOCIAL_TOKEN_ENCRYPTION_KEY is not a valid Fernet key") from exc
        signing_key = hashlib.sha256(b"nexus-social-media-signing\x00" + decoded).digest()
        return cls(_fernet=fernet, _signing_key=signing_key)

    def encrypt(self, value: str) -> str:
        text = value if isinstance(value, str) else str(value)
        return self._fernet.encrypt(text.encode("utf-8")).decode("ascii")

    def decrypt(self, value: str) -> str:
        try:
            return self._fernet.decrypt((value or "").encode("ascii")).decode("utf-8")
        except (InvalidToken, ValueError, UnicodeError) as exc:
            raise SocialSecretError("stored social credential cannot be decrypted") from exc

    def sign_media(self, asset_id: str, expires_ts: int) -> str:
        message = f"{asset_id}\n{int(expires_ts)}".encode("utf-8")
        digest = hmac.new(self._signing_key, message, hashlib.sha256).digest()
        return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")

    def verify_media(self, asset_id: str, expires_ts: int, signature: str) -> bool:
        expected = self.sign_media(asset_id, expires_ts)
        return hmac.compare_digest(expected, (signature or "").strip())
