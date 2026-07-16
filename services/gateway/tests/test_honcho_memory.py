from __future__ import annotations

import json
import os
import stat
import time

import httpx
import pytest

from app import honcho_memory, telegram_notifications, user_store


def _configure(tmp_path, monkeypatch):
    monkeypatch.setenv("HONCHO_MEMORY_ENABLED", "true")
    monkeypatch.setenv("HONCHO_BASE_URL", "http://honcho.test")
    monkeypatch.setenv("HONCHO_WORKSPACE_TOKEN", "test-workspace-token")
    monkeypatch.setenv("HONCHO_MEMORY_REGISTRY_PATH", str(tmp_path / "honcho-memory.sqlite"))
    monkeypatch.setenv("HONCHO_MEMORY_EXPORT_DIR", str(tmp_path / "exports"))
    honcho_memory.init()


def _linked_user(tmp_path, monkeypatch):
    db_path = tmp_path / "users.sqlite"
    user_store.init_db(str(db_path))
    user = user_store.create_user_with_admin(str(db_path), username="memory-owner", password="secret", admin=False)
    settings = user_store.get_settings(str(db_path), user_id=user.id)
    settings["telegram"]["link"] = {
        "telegram_user_id": "445566",
        "linked_chat_id": "445566",
    }
    user_store.set_settings(str(db_path), user_id=user.id, settings=settings)
    monkeypatch.setattr(telegram_notifications.S, "USER_DB_PATH", str(db_path))
    return user


@pytest.mark.asyncio
async def test_health_status_probes_workspace(tmp_path, monkeypatch):
    _configure(tmp_path, monkeypatch)
    calls = []

    async def fake_request(method, path, *, body=None):
        calls.append((method, path, body))
        return {"id": "nexus"}

    monkeypatch.setattr(honcho_memory, "_request", fake_request)

    assert await honcho_memory.health_status() == {
        "enabled": True,
        "configured": True,
        "reason": "",
    }
    assert calls == [("POST", "/workspaces", {"id": "nexus", "metadata": {"managed_by": "nexus"}})]


@pytest.mark.asyncio
async def test_health_status_reports_honcho_transport_failure(tmp_path, monkeypatch):
    _configure(tmp_path, monkeypatch)

    async def fake_request(method, path, *, body=None):
        request = httpx.Request(method, "http://honcho.test/v3/workspaces")
        raise httpx.ConnectError("unreachable", request=request)

    monkeypatch.setattr(honcho_memory, "_request", fake_request)

    assert await honcho_memory.health_status() == {
        "enabled": False,
        "configured": True,
        "reason": "honcho_unavailable",
    }


def test_private_identity_uses_linked_composite_owner_and_bot_session(tmp_path, monkeypatch):
    _configure(tmp_path, monkeypatch)
    user = _linked_user(tmp_path, monkeypatch)

    identity = honcho_memory.resolve_identity(
        {"chat_id": "445566", "chat_type": "private", "telegram_user_id": "445566", "bot_id": "101"}
    )

    assert identity["owner_user_id"] == user.id
    assert identity["participant_user_id"] == user.id
    assert identity["owner_key"] == f"nexus:{user.id}:telegram:445566"
    assert identity["partition_key"] == "telegram:private:445566"
    assert identity["short_term_key"] == "telegram:private:445566:bot:101"
    assert identity["retrieval_keys"] == [f"nexus:{user.id}"]


def test_ui_identity_isolates_conversation_and_soul_while_linking_long_term_owner(tmp_path, monkeypatch):
    _configure(tmp_path, monkeypatch)
    user = _linked_user(tmp_path, monkeypatch)

    identity = honcho_memory.resolve_ui_identity(
        owner_user_id=user.id,
        conversation_id="conversation-1",
        soul_name="ada2",
    )

    assert identity["owner_key"] == f"nexus:{user.id}"
    assert identity["partition_key"] == f"nexus:ui:{user.id}:conversation:conversation-1"
    assert identity["short_term_key"].endswith(":soul:ada2")
    assert identity["bot_id"] == "ui:ada2"
    assert identity["source_kind"] == "nexus_chat_ui"
    assert identity["retrieval_keys"] == [
        f"nexus:{user.id}:telegram:445566",
        "telegram:445566",
    ]


def test_group_identity_is_partitioned_by_numeric_chat(tmp_path, monkeypatch):
    _configure(tmp_path, monkeypatch)
    user = _linked_user(tmp_path, monkeypatch)

    identity = honcho_memory.resolve_identity(
        {"chat_id": "-100123", "chat_type": "supergroup", "telegram_user_id": "445566", "bot_id": "202"}
    )

    assert identity["owner_user_id"] is None
    assert identity["participant_user_id"] == user.id
    assert identity["owner_key"] == "telegram:group:-100123"
    assert identity["participant_key"] == f"nexus:{user.id}:telegram:445566"
    assert identity["short_term_key"] == "telegram:group:-100123:bot:202"


def test_linked_identity_audits_and_adopts_telegram_only_sessions(tmp_path, monkeypatch):
    _configure(tmp_path, monkeypatch)
    user = _linked_user(tmp_path, monkeypatch)
    now = int(time.time())
    conn = honcho_memory._connect()
    try:
        conn.execute(
            """
            INSERT INTO memory_sessions(
                id,honcho_session_id,owner_user_id,owner_key,participant_key,partition_key,
                chat_id,chat_type,bot_id,telegram_message_id,created_ts,expires_ts,status,metadata_json
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                "legacy",
                "legacy_session",
                None,
                "telegram:445566",
                "telegram:445566",
                "telegram:private:445566",
                "445566",
                "private",
                "101",
                "1",
                now,
                now + 3600,
                "active",
                "{}",
            ),
        )
        conn.commit()
    finally:
        conn.close()
    identity = honcho_memory.resolve_identity(
        {"chat_id": "445566", "chat_type": "private", "telegram_user_id": "445566", "bot_id": "101"}
    )

    honcho_memory._record_identity_aliases(identity)

    adopted = honcho_memory.list_sessions_for_user(user.id)
    assert [item["id"] for item in adopted] == ["legacy"]
    conn = honcho_memory._connect()
    try:
        alias = conn.execute("SELECT old_owner_key,new_owner_key FROM memory_identity_aliases").fetchone()
    finally:
        conn.close()
    assert tuple(alias) == ("telegram:445566", f"nexus:{user.id}:telegram:445566")


@pytest.mark.asyncio
async def test_ingest_turn_records_enforced_private_retention(tmp_path, monkeypatch):
    _configure(tmp_path, monkeypatch)
    user = _linked_user(tmp_path, monkeypatch)
    calls = []

    async def fake_request(method, path, *, body=None):
        calls.append((method, path, body))
        if path.endswith("/messages"):
            return [{"id": "message-1"}, {"id": "message-2"}]
        return {"id": "ok"}

    monkeypatch.setattr(honcho_memory, "_request", fake_request)
    before = int(time.time())
    result = await honcho_memory.ingest_turn(
        {
            "chat_id": "445566",
            "chat_type": "private",
            "telegram_user_id": "445566",
            "bot_id": "101",
            "telegram_message_id": "88",
            "user_text": "Remember that I like tea.",
            "assistant_text": "I will remember that preference.",
        }
    )

    assert result["stored"] is True
    assert before + 180 * 86400 <= result["expires_ts"] <= int(time.time()) + 180 * 86400
    sessions = honcho_memory.list_sessions_for_user(user.id)
    assert len(sessions) == 1
    assert sessions[0]["bot_id"] == "101"
    assert sessions[0]["metadata"]["retention_class"] == "private_raw"
    message_call = next(call for call in calls if call[1].endswith("/messages"))
    assert [item["metadata"]["role"] for item in message_call[2]["messages"]] == ["user", "assistant"]


@pytest.mark.asyncio
async def test_ui_context_reads_nexus_and_linked_telegram_representations(tmp_path, monkeypatch):
    _configure(tmp_path, monkeypatch)
    user = _linked_user(tmp_path, monkeypatch)
    targets = []

    async def fake_request(method, path, *, body=None):
        targets.append(body["target"])
        return {"representation": f"memory-{len(targets)}"}

    monkeypatch.setattr(honcho_memory, "_request", fake_request)
    result = await honcho_memory.get_ui_context(
        owner_user_id=user.id,
        conversation_id="conversation-1",
        soul_name="ai2",
        message="What do I prefer?",
    )

    assert len(targets) == 3
    assert result["context"] == "memory-1\n\nmemory-2\n\nmemory-3"


@pytest.mark.asyncio
async def test_ingest_ui_turn_records_source_conversation_and_soul(tmp_path, monkeypatch):
    _configure(tmp_path, monkeypatch)
    user = _linked_user(tmp_path, monkeypatch)
    calls = []

    async def fake_request(method, path, *, body=None):
        calls.append((method, path, body))
        return {"id": "ok"}

    monkeypatch.setattr(honcho_memory, "_request", fake_request)
    result = await honcho_memory.ingest_ui_turn(
        owner_user_id=user.id,
        conversation_id="conversation-1",
        soul_name="ada2",
        turn_id="turn-1",
        user_text="Remember my preference.",
        assistant_text="I will remember it.",
    )

    assert result["stored"] is True
    sessions = honcho_memory.list_sessions_for_user(user.id)
    assert len(sessions) == 1
    session = sessions[0]
    assert session["source_kind"] == "nexus_chat_ui"
    assert session["source_turn_id"] == "turn-1"
    assert session["telegram_message_id"] == ""
    assert session["chat_id"] == "conversation-1"
    assert session["bot_id"] == "ui:ada2"
    assert session["metadata"]["soul"] == "ada2"
    message_call = next(call for call in calls if call[1].endswith("/messages"))
    assert [item["metadata"]["source_kind"] for item in message_call[2]["messages"]] == [
        "nexus_chat_ui",
        "nexus_chat_ui",
    ]


def test_export_download_is_owner_gated_and_mode_0600(tmp_path, monkeypatch):
    _configure(tmp_path, monkeypatch)
    now = int(time.time())
    export_dir = tmp_path / "exports"
    export_dir.mkdir()
    path = export_dir / "memory-export-test.json"
    path.write_text(json.dumps({"ok": True}), encoding="utf-8")
    os.chmod(path, 0o600)
    conn = honcho_memory._connect()
    try:
        conn.execute(
            "INSERT INTO memory_exports(id,owner_user_id,requested_by_user_id,path,checksum_sha256,created_ts,expires_ts,status) VALUES(?,?,?,?,?,?,?,'ready')",
            ("test", 7, 1, str(path), "abc", now, now + 3600),
        )
        conn.commit()
    finally:
        conn.close()

    with pytest.raises(KeyError):
        honcho_memory.export_for_download("test", owner_user_id=1)
    result = honcho_memory.export_for_download("test", owner_user_id=7)

    assert result["path"] == str(path)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


@pytest.mark.asyncio
async def test_maintenance_preserves_conclusions_then_expires_raw_session(tmp_path, monkeypatch):
    _configure(tmp_path, monkeypatch)
    now = int(time.time())
    conn = honcho_memory._connect()
    try:
        conn.execute(
            """
            INSERT INTO memory_sessions(
                id,honcho_session_id,owner_user_id,owner_key,participant_key,partition_key,
                chat_id,chat_type,bot_id,telegram_message_id,created_ts,expires_ts,status,metadata_json
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                "expired-turn",
                "expired_session",
                7,
                "nexus:7:telegram:8",
                "nexus:7:telegram:8",
                "telegram:private:8",
                "8",
                "private",
                "9",
                "10",
                now - 100,
                now - 1,
                "active",
                "{}",
            ),
        )
        conn.commit()
    finally:
        conn.close()
    deletes = []

    async def fake_preserve(item):
        assert item["id"] == "expired-turn"
        return 2

    async def fake_request(method, path, *, body=None):
        if method == "DELETE":
            deletes.append(path)
        return None

    monkeypatch.setattr(honcho_memory, "_preserve_conclusions", fake_preserve)
    monkeypatch.setattr(honcho_memory, "_request", fake_request)

    result = await honcho_memory.run_maintenance_once()

    assert result["sessions_expired"] == 1
    assert result["conclusions_preserved"] == 2
    assert deletes
    conn = honcho_memory._connect()
    try:
        status = conn.execute("SELECT status FROM memory_sessions WHERE id='expired-turn'").fetchone()[0]
    finally:
        conn.close()
    assert status == "expired"
