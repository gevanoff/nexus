# Nexus Telegram memory policy

This document defines the Honcho ownership and isolation model enforced by the
Nexus Gateway integration. Ingestion still requires the explicit
`HONCHO_MEMORY_ENABLED` and bot `TELEGRAM_MEMORY_ENABLED` deployment gates.

## Identity resolution

Use immutable numeric IDs. Never merge identities using Telegram usernames,
display names, chat titles, or bot persona names.

For a human participant, choose the narrowest available canonical owner key:

1. `nexus:{nexus_user_id}:telegram:{telegram_user_id}` when both are linked.
2. `telegram:{telegram_user_id}` when only Telegram identity is known.
3. `nexus:{nexus_user_id}` when only Nexus identity is known.
4. A chat-scoped anonymous peer only when neither identity is available.

When a Telegram-only peer is later linked to a Nexus user, record an audited
alias/migration to the composite owner instead of creating a second person or
silently copying memory.

Private-chat memory is partitioned by numeric Telegram chat ID. Its short-term
session key also includes the bot identity, for example
`telegram:private:{chat_id}:bot:{bot_id}`. This keeps Tess, Clarion, Hex, and
future bots from sharing immediate conversation context while still allowing
derived long-term memory to resolve through the same canonical human owner.

Group memory is partitioned by numeric group chat ID, never title. Each group
gets its own peer and long-term namespace. Short-term sessions include both the
group chat ID and bot ID. Human messages inside a group retain their canonical
participant owner as well as the containing group ID.

## Cross-bot sharing

All Nexus Telegram bots use one Honcho workspace and one configurable assistant
fleet observer for shared long-term conclusions. Each bot keeps separate
short-term sessions and prompt history. Store the fleet identifier explicitly
so a future policy can split bot memories without rewriting identity keys.

No bot may read raw short-term sessions belonging to another bot. Cross-bot
retrieval is limited to shared derived long-term memory and the current bot's
own session.

The first implementation stores each completed Telegram turn as a separate
Honcho session. This makes a single turn independently deletable even though
Honcho v3 does not expose individual message deletion. Immediate short-term
history remains bot-local and in memory; it is therefore discarded on bot
restart and never becomes cross-bot context. Before an expired raw session is
deleted, session conclusions are copied into the owner's global long-term
representation and recorded in Nexus's deletion registry.

## Recommended retention baseline

This small, private Nexus environment benefits from durable personalization but
does not need indefinite raw transcripts. The recommended defaults are:

| Data | Retention |
| --- | --- |
| Private-chat raw messages | 180 days |
| Group-chat raw messages | 90 days |
| Bot-specific short-term session summaries | 30 days after last activity |
| Attachments and large tool outputs | 30 days |
| Derived long-term conclusions | Until superseded or user-deleted; review stale items annually |
| Server-side export files | 7 days |
| Export/deletion audit metadata | 1 year |
| Backups containing deleted memory | Expire within 30 days |

Less private alternatives include retaining all raw messages indefinitely or
keeping one year of transcripts. A stricter alternative is 30 days of raw data
with only user-approved conclusions retained. The baseline above is the best
fit here because it preserves enough history to repair or re-derive memory while
limiting exposure from old group conversations.

## Deletion

The associated Nexus user can delete an individual message, a chat session, a
derived conclusion, or all memory for their canonical owner. Deleting source
messages must invalidate and rebuild affected summaries/conclusions so facts do
not survive solely in derived data. Primary data and active exports are removed
immediately; backup copies age out within 30 days and are not restored without
reapplying deletion tombstones.

Group deletion operates on a whole group partition and requires an explicitly
mapped Nexus group owner or administrator action. Leaving a group does not merge
or transfer its memory.

## Export and download authorization

Any Nexus administrator may request an export job. The job writes a dump to a
protected server-side directory with mode `0600`, an owner Nexus user ID, a
checksum, creation/expiry timestamps, and an audit record. The application
download endpoint authorizes only the associated Nexus user; administrator role
alone does not grant download access. Host root access remains outside this
application-level guarantee.

Group exports require an explicitly associated Nexus group owner. Without one,
an administrator may create a server-side operational dump, but no user download
URL is issued. Export files expire after seven days.

The Gateway exposes owner-authorized list/delete/download routes under
`/ui/api/user/memory`. Administrators create a user export through
`/ui/api/admin/memory/exports`; the resulting download route checks the export's
owner ID and does not treat administrator status as ownership.
