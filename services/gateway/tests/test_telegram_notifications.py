from __future__ import annotations

import os

os.environ.setdefault("GATEWAY_BEARER_TOKEN", "test-token")

from services.app import telegram_notifications, user_store


def test_resolve_notification_target_uses_app_scoped_coding_preferences(tmp_path, monkeypatch):
    db_path = tmp_path / "users.sqlite"
    user_store.init_db(str(db_path))
    user = user_store.create_user_with_admin(str(db_path), username="alice", password="secret", admin=False)
    settings = user_store.get_settings(str(db_path), user_id=user.id)
    settings["telegram"] = {
        "notifications_enabled": True,
        "chat_id": "12345",
        "username": "alice_tg",
        "apps": {
            "coding": {
                "enabled": True,
                "notify_on_attention": True,
                "notify_on_recovery": False,
                "notify_on_noteworthy": True,
            }
        },
    }
    user_store.set_settings(str(db_path), user_id=user.id, settings=settings)
    monkeypatch.setattr(telegram_notifications.S, "USER_DB_PATH", str(db_path))

    target = telegram_notifications.resolve_notification_target(user_id=user.id, owner_username="alice", app="coding")

    assert target["enabled"] is True
    assert target["chat_id"] == "12345"
    assert target["mention_username"] == "alice_tg"
    assert target["notify_on_attention"] is True
    assert target["notify_on_recovery"] is False
    assert target["notify_on_noteworthy"] is True


def test_resolve_notification_target_supports_legacy_coding_flags(tmp_path, monkeypatch):
    db_path = tmp_path / "users.sqlite"
    user_store.init_db(str(db_path))
    user = user_store.create_user_with_admin(str(db_path), username="bob", password="secret", admin=False)
    settings = user_store.get_settings(str(db_path), user_id=user.id)
    settings["telegram"] = {
        "notifications_enabled": True,
        "chat_id": "67890",
        "username": "bob_tg",
        "notify_on_attention": False,
        "notify_on_recovery": True,
        "notify_on_noteworthy": False,
    }
    user_store.set_settings(str(db_path), user_id=user.id, settings=settings)
    monkeypatch.setattr(telegram_notifications.S, "USER_DB_PATH", str(db_path))

    target = telegram_notifications.resolve_notification_target(user_id=user.id, owner_username="bob", app="coding")

    assert target["enabled"] is True
    assert target["notify_on_attention"] is False
    assert target["notify_on_recovery"] is True
    assert target["notify_on_noteworthy"] is False


def test_create_and_redeem_link_code_updates_saved_chat_binding(tmp_path, monkeypatch):
    db_path = tmp_path / "users.sqlite"
    user_store.init_db(str(db_path))
    user = user_store.create_user_with_admin(str(db_path), username="carol", password="secret", admin=False)
    monkeypatch.setattr(telegram_notifications.S, "USER_DB_PATH", str(db_path))

    payload = telegram_notifications.create_link_code(user_id=user.id)

    assert payload["code"]
    assert payload["command"] == f"/link {payload['code']}"
    result = telegram_notifications.redeem_link_code(
        code=payload["code"],
        chat_id="123456789",
        username="carol_tg",
        telegram_user_id="9911",
        chat_type="private",
    )

    assert result["ok"] is True
    settings = user_store.get_settings(str(db_path), user_id=user.id)
    assert settings["telegram"]["chat_id"] == "123456789"
    assert settings["telegram"]["username"] == "carol_tg"
    assert settings["telegram"]["link"]["linked_chat_id"] == "123456789"
    assert settings["telegram"]["link"]["linked_username"] == "carol_tg"
    assert "pending_code_hash" not in settings["telegram"]["link"]


def test_redeem_link_code_rejects_expired_codes(tmp_path, monkeypatch):
    db_path = tmp_path / "users.sqlite"
    user_store.init_db(str(db_path))
    user = user_store.create_user_with_admin(str(db_path), username="dave", password="secret", admin=False)
    monkeypatch.setattr(telegram_notifications.S, "USER_DB_PATH", str(db_path))

    payload = telegram_notifications.create_link_code(user_id=user.id)
    settings = user_store.get_settings(str(db_path), user_id=user.id)
    settings["telegram"]["link"]["pending_expires_ts"] = 1
    user_store.set_settings(str(db_path), user_id=user.id, settings=settings)

    result = telegram_notifications.redeem_link_code(
        code=payload["code"],
        chat_id="123456789",
        username="dave_tg",
        telegram_user_id="3311",
        chat_type="private",
    )

    assert result["ok"] is False
    assert result["error"] == "code_expired"
