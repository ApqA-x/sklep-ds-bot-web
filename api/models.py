from __future__ import annotations

import re
from typing import Literal
from urllib.parse import quote

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

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
# копия ACTIVITY_CATEGORIES из dsbot domain.py — ключи activityCategoryChannelIds
ACTIVITY_CATEGORIES = {"join-leave", "messages", "voice-log", "profile"}
COMMAND_ACCESS_VALUES = {"all", "admin"}
COMMAND_NAME_RE = re.compile(r"^[a-z0-9\-]{1,32}$")


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
    autoRestoreRoles: bool | None = None
    autoRestoreNicknames: bool | None = None
    activityChannelId: str | None = None
    activityCategoryChannelIds: dict[str, str] | None = None
    activityEventTypes: list[str] | None = None
    commandAccess: dict[str, str] | None = None
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
        unknown = set(value) - ACTIVITY_CATEGORIES
        if unknown:
            raise ValueError(f"unknown activity categories: {sorted(unknown)}")
        return {k: _check_id(v, "channelId") for k, v in value.items()}

    @field_validator("commandAccess")
    @classmethod
    def _command_access(cls, value: dict[str, str] | None) -> dict[str, str] | None:
        if value is None:
            return None
        result: dict[str, str] = {}
        for name, access in value.items():
            key = name.strip().lower()
            if not COMMAND_NAME_RE.match(key):
                raise ValueError(f"invalid command name: {name!r}")
            if access not in COMMAND_ACCESS_VALUES:
                raise ValueError(f"commandAccess[{key}] must be one of {sorted(COMMAND_ACCESS_VALUES)}")
            result[key] = access
        return result

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


class MemberRoleAction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    roleId: str
    action: Literal["grant", "revoke"]

    @field_validator("roleId")
    @classmethod
    def _rid(cls, value: str) -> str:
        return _check_id(value, "roleId")


class TimeoutMemberAction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mute: bool
    seconds: int = 600

    @field_validator("seconds")
    @classmethod
    def _seconds(cls, value: int) -> int:
        if not 60 <= value <= 604_800:
            raise ValueError("seconds must be between 60 and 604800")
        return value


class MoveMemberAction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    channelId: str

    @field_validator("channelId")
    @classmethod
    def _cid(cls, value: str) -> str:
        return _check_id(value, "channelId")


class KickMemberAction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str

    @field_validator("reason")
    @classmethod
    def _reason(cls, value: str) -> str:
        value = value.strip()
        if not 1 <= len(value) <= 500:
            raise ValueError("reason must be 1..500 characters")
        return value


class EmbedSpec(BaseModel):
    """Embed-блок для отправки от бота. Поля опциональны; картинки — имена вложений сообщения (attachment://)."""

    model_config = ConfigDict(extra="forbid")

    title: str | None = None
    description: str | None = None
    color: int | None = None
    thumbnail: str | None = None
    image: str | None = None
    authorName: str | None = None
    authorIcon: str | None = None
    footerText: str | None = None
    footerIcon: str | None = None

    @field_validator("title", "description", "authorName", "footerText")
    @classmethod
    def _strip_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        return value or None

    @field_validator("color")
    @classmethod
    def _color(cls, value: int | None) -> int | None:
        if value is None:
            return None
        if not 0 <= value <= 0xFFFFFF:
            raise ValueError("color must be 0..16777215")
        return value

    @field_validator("thumbnail", "image", "authorIcon", "footerIcon")
    @classmethod
    def _att(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value:
            return None
        if len(value) > 255 or "\n" in value or "\r" in value:
            raise ValueError("attachment name too long or multiline")
        return value

    @model_validator(mode="after")
    def _limits(self) -> "EmbedSpec":
        if self.title and len(self.title) > 256:
            raise ValueError("title must be at most 256 characters")
        if self.description and len(self.description) > 4000:
            raise ValueError("description must be at most 4000 characters")
        if self.authorName and len(self.authorName) > 256:
            raise ValueError("authorName must be at most 256 characters")
        if self.footerText and len(self.footerText) > 2048:
            raise ValueError("footerText must be at most 2048 characters")
        total = sum(len(v or "") for v in (self.title, self.description, self.authorName, self.footerText))
        if total > 6000:
            raise ValueError("embed text must be at most 6000 characters")
        return self

    def is_empty(self) -> bool:
        return not any(
            getattr(self, f)
            for f in ("title", "description", "color", "thumbnail", "image", "authorName", "authorIcon", "footerText", "footerIcon")
        )

    def discord_embed(self, uploaded: set[str]) -> dict:
        """Discord-payload блока; поля-картинки ссылаются на вложения из uploaded (иначе ValueError)."""
        payload: dict = {"color": self.color if self.color is not None else 0}
        if self.title:
            payload["title"] = self.title
        if self.description:
            payload["description"] = self.description

        def attach(name: str | None) -> str:
            if name not in uploaded:
                raise ValueError(f"attachment {name!r} was not uploaded with this message")
            return f"attachment://{quote(name)}"

        if self.image:
            payload["image"] = {"url": attach(self.image)}
        if self.thumbnail:
            payload["thumbnail"] = {"url": attach(self.thumbnail)}
        if self.authorName or self.authorIcon:
            author: dict = {}
            if self.authorName:
                author["name"] = self.authorName
            if self.authorIcon:
                author["icon_url"] = attach(self.authorIcon)
            payload["author"] = author
        if self.footerText or self.footerIcon:
            footer: dict = {}
            if self.footerText:
                footer["text"] = self.footerText
            if self.footerIcon:
                footer["icon_url"] = attach(self.footerIcon)
            payload["footer"] = footer
        return payload

    def audit_summary(self) -> dict:
        """Компактное представление блока для журнала сайта."""
        summary: dict = {"color": self.color if self.color is not None else 0}
        if self.title:
            summary["title"] = self.title
        if self.description:
            summary["description"] = self.description[:200] + ("…" if len(self.description) > 200 else "")
        if self.thumbnail:
            summary["thumbnail"] = self.thumbnail
        if self.image:
            summary["image"] = self.image
        if self.authorName:
            summary["author"] = self.authorName
        if self.footerText:
            summary["footer"] = self.footerText
        return summary


class ChannelMessageAction(BaseModel):
    """Тело сообщения бота: текст и/или embed-блок (хотя бы одно)."""

    model_config = ConfigDict(extra="forbid")

    content: str | None = None
    embed: EmbedSpec | None = None

    @field_validator("content")
    @classmethod
    def _content(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if value and len(value) > 2000:
            raise ValueError("content must be at most 2000 characters")
        return value or None

    @model_validator(mode="after")
    def _at_least_one(self) -> "ChannelMessageAction":
        if not self.content and (self.embed is None or self.embed.is_empty()):
            raise ValueError("content or embed required")
        return self


class ChatPresetAction(BaseModel):
    """Пресет: add сохраняет текстовый пресет или embed-блок с названием и каналами, remove удаляет по id."""

    model_config = ConfigDict(extra="forbid")

    action: Literal["add", "remove"]
    kind: Literal["text", "embed"] = "text"
    text: str | None = None
    name: str | None = None
    channelIds: list[str] | None = None
    presetId: str | None = None
    embed: EmbedSpec | None = None

    @model_validator(mode="after")
    def _check_per_action(self) -> "ChatPresetAction":
        if self.action == "add":
            name = (self.name or "").strip()
            if len(name) > 100:
                raise ValueError("name must be at most 100 characters")
            self.name = name or None
            channels = _check_id_list(self.channelIds or [], "channelId")
            if not 1 <= len(channels) <= 20:
                raise ValueError("channelIds must contain 1..20 channel ids")
            self.channelIds = channels
            if self.kind == "embed":
                self.text = None
                if self.embed is None or self.embed.is_empty():
                    raise ValueError("embed is required for an embed preset")
                if any(getattr(self.embed, f) for f in ("thumbnail", "image", "authorIcon", "footerIcon")):
                    raise ValueError("preset embed cannot reference attachments")
            else:
                self.embed = None
                text = (self.text or "").strip()
                if not 1 <= len(text) <= 2000:
                    raise ValueError("text must be 1..2000 characters")
                self.text = text
        else:
            preset_id = (self.presetId or "").strip()
            if not 1 <= len(preset_id) <= 64:
                raise ValueError("presetId is required for remove")
            self.presetId = preset_id
        return self


class InviteCreateAction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    channelId: str
    maxAge: int = 86400
    maxUses: int = 0

    @field_validator("channelId")
    @classmethod
    def _cid(cls, value: str) -> str:
        return _check_id(value, "channelId")

    @field_validator("maxAge")
    @classmethod
    def _age(cls, value: int) -> int:
        if not 0 <= value <= 604_800:
            raise ValueError("maxAge must be between 0 and 604800")
        return value
