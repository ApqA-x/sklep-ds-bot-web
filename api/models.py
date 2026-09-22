from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, field_validator

SNOWFLAKE_RE = re.compile(r"^\d{5,25}$")

# копия ACTIVITY_EVENT_TYPES из sklep-ds-bot/voice_tracker/domain.py
ACTIVITY_EVENT_TYPES = {
    "member_join",
    "member_leave",
    "invite_create",
    "invite_delete",
    "invite_used",
    "message_create",
    "message_update",
    "message_delete",
    "reaction_add",
    "reaction_remove",
    "voice_join",
    "voice_leave",
    "voice_move",
    "profile_nickname_update",
    "profile_roles_update",
}
TRACKING_MODES = {"all", "none", "specific"}


def _check_id(value: str, field: str) -> str:
    value = value.strip()
    if value and not SNOWFLAKE_RE.match(value):
        raise ValueError(f"{field} must be a Discord snowflake id")
    return value


def _check_id_list(values: list[str], field: str) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        clean = _check_id(value, field)
        if clean and clean not in seen:
            seen.add(clean)
            result.append(clean)
    return result


class GuildSettingsPatch(BaseModel):
    """Allowlist writable fields of guild_settings (mirror of domain.GuildSettings)."""

    model_config = ConfigDict(extra="forbid")

    trackingMode: str | None = None
    trackedChannelIds: list[str] | None = None
    summaryChannelId: str | None = None
    fallbackSummaryChannelId: str | None = None
    autoRoleId: str | None = None
    autoUnmuteUserIds: list[str] | None = None
    trustedUserIds: list[str] | None = None
    soundboardEnforcementEnabled: bool | None = None
    activityChannelId: str | None = None
    activityCategoryChannelIds: dict[str, str] | None = None
    activityEventTypes: list[str] | None = None
    expectedUpdatedAt: str | None = None

    @field_validator("trackingMode")
    @classmethod
    def _mode(cls, value: str | None) -> str | None:
        if value is not None and value not in TRACKING_MODES:
            raise ValueError(f"trackingMode must be one of {sorted(TRACKING_MODES)}")
        return value

    @field_validator("trackedChannelIds", "autoUnmuteUserIds", "trustedUserIds")
    @classmethod
    def _id_lists(cls, value: list[str] | None, info) -> list[str] | None:
        return None if value is None else _check_id_list(value, info.field_name)

    @field_validator("summaryChannelId", "fallbackSummaryChannelId", "autoRoleId", "activityChannelId")
    @classmethod
    def _ids(cls, value: str | None) -> str | None:
        return None if value is None else _check_id(value, "channel/role id")

    @field_validator("activityEventTypes")
    @classmethod
    def _events(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        unknown = set(value) - ACTIVITY_EVENT_TYPES
        if unknown:
            raise ValueError(f"unknown activity event types: {sorted(unknown)}")
        return sorted(set(value))

    @field_validator("activityCategoryChannelIds")
    @classmethod
    def _categories(cls, value: dict[str, str] | None) -> dict[str, str] | None:
        if value is None:
            return None
        return {_check_id(k, "categoryId"): _check_id(v, "channelId") for k, v in value.items()}

    def mongo_set(self) -> dict[str, object]:
        """Patch fields (without conflict token) keyed as in Mongo documents."""
        data = self.model_dump(exclude_unset=True, exclude_none=True)
        data.pop("expectedUpdatedAt", None)
        return data


class ListMemberAction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    userId: str
    action: Literal["add", "remove"]

    @field_validator("userId")
    @classmethod
    def _uid(cls, value: str) -> str:
        return _check_id(value, "userId")


class StalkerAction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    watcherUserId: str
    targetUserId: str
    action: Literal["add", "remove"]

    @field_validator("watcherUserId", "targetUserId")
    @classmethod
    def _uid(cls, value: str) -> str:
        if not value.strip() or not SNOWFLAKE_RE.match(value.strip()):
            raise ValueError("user id must be a Discord snowflake")
        return value.strip()
