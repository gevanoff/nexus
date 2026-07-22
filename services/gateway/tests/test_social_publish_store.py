from __future__ import annotations

import sqlite3
import time

from cryptography.fernet import Fernet

from app.social_publish_crypto import SecretBox
from app import social_publish_store as store


def _db_path(tmp_path):
    path = tmp_path / "users.sqlite"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE users(id INTEGER PRIMARY KEY, username TEXT)")
    conn.execute("INSERT INTO users(id,username) VALUES(1,'one'),(2,'two')")
    conn.commit()
    conn.close()
    return str(path)


def test_secret_box_round_trip_and_media_signature():
    box = SecretBox.from_key(Fernet.generate_key().decode())
    encrypted = box.encrypt("refresh-token")
    assert encrypted != "refresh-token"
    assert box.decrypt(encrypted) == "refresh-token"
    signature = box.sign_media("asset", 12345)
    assert box.verify_media("asset", 12345, signature)
    assert not box.verify_media("asset", 12346, signature)


def test_oauth_state_is_scoped_single_use_and_expiring(tmp_path):
    db_path = _db_path(tmp_path)
    value = store.create_oauth_state(
        db_path,
        user_id=1,
        provider="youtube",
        redirect_after="/ui/social/publish",
        ttl_sec=60,
    )
    assert store.consume_oauth_state(db_path, state=value, provider="meta") is None
    record = store.consume_oauth_state(db_path, state=value, provider="youtube")
    assert record is not None
    assert record["user_id"] == 1
    assert store.consume_oauth_state(db_path, state=value, provider="youtube") is None


def test_accounts_are_user_scoped_and_list_redacts_credentials(tmp_path):
    db_path = _db_path(tmp_path)
    box = SecretBox.from_key(Fernet.generate_key().decode())
    for user_id, suffix in ((1, "one"), (2, "two")):
        store.upsert_account(
            db_path,
            user_id=user_id,
            provider="youtube",
            external_account_id=f"channel-{suffix}",
            display_name=suffix,
            account_type="channel",
            scopes=["youtube.upload"],
            access_token_enc=box.encrypt(f"access-{suffix}"),
            refresh_token_enc=box.encrypt(f"refresh-{suffix}"),
            token_type="Bearer",
            access_expires_ts=int(time.time()) + 3600,
            refresh_expires_ts=None,
            metadata={},
        )
    accounts = store.list_accounts(db_path, user_id=1)
    assert [item["external_account_id"] for item in accounts] == ["channel-one"]
    assert "access_token_enc" not in accounts[0]
    detailed = store.get_account(db_path, user_id=1, account_id=accounts[0]["id"], include_secrets=True)
    assert detailed is not None
    assert box.decrypt(detailed["access_token_enc"]) == "access-one"


def test_publication_idempotency_returns_same_record(tmp_path):
    db_path = _db_path(tmp_path)
    box = SecretBox.from_key(Fernet.generate_key().decode())
    account = store.upsert_account(
        db_path,
        user_id=1,
        provider="youtube",
        external_account_id="channel",
        display_name="Channel",
        account_type="channel",
        scopes=[],
        access_token_enc=box.encrypt("access"),
        refresh_token_enc=None,
        token_type="Bearer",
        access_expires_ts=None,
        refresh_expires_ts=None,
        metadata={},
    )
    media_path = tmp_path / "clip.mp4"
    media_path.write_bytes(b"video")
    media = store.create_media_asset(
        db_path,
        user_id=1,
        path=str(media_path),
        filename="clip.mp4",
        mime_type="video/mp4",
        size_bytes=5,
        sha256="abc",
        metadata={},
        ttl_sec=3600,
    )
    first = store.create_or_get_publication(
        db_path,
        user_id=1,
        provider="youtube",
        account_id=account["id"],
        media_id=media["id"],
        idempotency_key="same",
        request_payload={"title": "One"},
        consent_ts=int(time.time()),
    )
    second = store.create_or_get_publication(
        db_path,
        user_id=1,
        provider="youtube",
        account_id=account["id"],
        media_id=media["id"],
        idempotency_key="same",
        request_payload={"title": "Two"},
        consent_ts=int(time.time()),
    )
    assert first["id"] == second["id"]
    assert second["request"]["title"] == "One"
