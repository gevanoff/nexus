from __future__ import annotations

import os

os.environ.setdefault("GATEWAY_BEARER_TOKEN", "test-token")

from app import telegram_notifications, user_store


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
