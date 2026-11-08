"""
Discord 表情反应插件。

两种用法：
1. 自动反应：收到 Discord 消息后，按「回复表情倾向」滑块掷骰，
   命中时让当前模型根据消息内容挑一个 emoji，再添加 reaction。
2. LLM 工具：提供 discord_react 工具，模型可在需要时自己选择一个 emoji
   对当前消息或指定消息添加 reaction。

不修改消息内容，不发送可见消息，只添加 reaction。
"""

import asyncio
import random
import re
import time
import uuid
from collections import deque
from typing import Any

from astrbot.api import logger, star
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import Provider, ProviderRequest

DEFAULT_POOL = ["👍", "😂", "🥰", "🤔", "👀", "🎉", "😮", "😢", "🔥", "✨", "💯", "🤝", "🫡", "🙏"]
CUSTOM_EMOJI_RE = re.compile(r"^<a?:[A-Za-z0-9_]+:\d+>$")
SAFE_TEXT_RE = re.compile(r"^[\w\W]{1,32}$")


class Main(star.Star):
    def __init__(self, context, config=None):
        super().__init__(context, config)
        self.context = context
        self.config = config or {}
        self._last_reaction: dict[str, float] = {}
        self._tasks: set[asyncio.Task] = set()
        self._reaction_history: dict[str, deque[dict[str, Any]]] = {}
        self._message_cache: dict[str, tuple[str, str]] = {}
        self._listener_clients: set[int] = set()
        self._attach_platform_listeners()

    # ------------------------------------------------------------------
    # 配置
    # ------------------------------------------------------------------
    def _str(self, key: str, default: str = "") -> str:
        value = self.config.get(key, default)
        return str(value).strip() if value is not None else default

    def _int(self, key: str, default: int, low: int, high: int) -> int:
        try:
            value = int(self.config.get(key, default))
        except (TypeError, ValueError):
            value = default
        return max(low, min(high, value))

    def _bool(self, key: str, default: bool = False) -> bool:
        value = self.config.get(key, default)
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return bool(value)
        return str(value).strip().lower() in {"1", "true", "yes", "on", "开启"}

    def _pool(self) -> list[str]:
        raw = self._str("emoji_pool", ",".join(DEFAULT_POOL))
        items = [item.strip() for item in re.split(r"[,，\n]", raw) if item.strip()]
        cleaned = [item for item in (self._clean_emoji(item) for item in items) if item]
        return cleaned or list(DEFAULT_POOL)

    def _tendency(self) -> int:
        return self._int("tendency", 30, 0, 100)

    def _cooldown(self) -> int:
        return self._int("cooldown_seconds", 15, 0, 300)

    def _reaction_limit(self) -> int:
        return self._int("reaction_context_limit", 10, 1, 50)

    # ------------------------------------------------------------------
    # 监听别人的 reaction
    # ------------------------------------------------------------------
    def _attach_platform_listeners(self) -> None:
        """插件加载时尽量直接挂到已启动的 Discord 客户端上。"""
        manager = getattr(self.context, "platform_manager", None)
        for platform in getattr(manager, "platform_insts", []) or []:
            client = getattr(platform, "client", None)
            if client is None:
                continue
            config = getattr(platform, "config", {}) or {}
            platform_type = str(config.get("type", "")).lower()
            if (
                "discord" not in platform_type
                and client.__class__.__name__ != "DiscordBotClient"
            ):
                continue
            self._attach_client(client)

    def _ensure_listener(self, event: AstrMessageEvent) -> None:
        client = getattr(event, "client", None)
        if client is not None:
            self._attach_client(client)

    def _attach_client(self, client) -> None:
        client_id = id(client)
        if client_id in self._listener_clients:
            return
        self._listener_clients.add(client_id)

        async def on_raw_reaction_add(payload):
            await self._handle_reaction(payload, client, removed=False)

        async def on_raw_reaction_remove(payload):
            await self._handle_reaction(payload, client, removed=True)

        # 直接替换客户端实例上的事件处理器；AstrBot 自身的 Discord 适配器
        # 没有监听 reaction，因此不会覆盖已有业务逻辑。
        setattr(client, "on_raw_reaction_add", on_raw_reaction_add)
        setattr(client, "on_raw_reaction_remove", on_raw_reaction_remove)
        logger.info("[discord-reaction] 已挂载 Discord reaction 监听")

    async def _handle_reaction(
        self,
        payload,
        client,
        *,
        removed: bool = False,
    ) -> None:
        if not self._bool("enabled", True):
            return
        if not self._bool("reaction_listen_enabled", True):
            return
        try:
            bot_user = getattr(client, "user", None)
            if bot_user is not None and getattr(payload, "user_id", None) == bot_user.id:
                return

            channel_id = str(getattr(payload, "channel_id", "") or "")
            message_id = str(getattr(payload, "message_id", "") or "")
            emoji = str(getattr(payload, "emoji", "") or "").strip()
            if not channel_id or not message_id or not emoji:
                return

            author_name, content = await self._get_message_summary(
                payload, client, message_id
            )
            user_id = str(getattr(payload, "user_id", "") or "")
            user_name = self._reaction_user_name(payload, client, user_id)

            entry = {
                "message_id": message_id,
                "emoji": emoji,
                "user_id": user_id,
                "user_name": user_name,
                "message_author": author_name,
                "content": content,
                "removed": removed,
                "time": time.time(),
            }
            history = self._reaction_history.setdefault(
                channel_id, deque(maxlen=self._reaction_limit())
            )
            history.append(entry)
            logger.debug(
                "[discord-reaction] 收到 reaction | channel=%s message=%s emoji=%s user=%s removed=%s",
                channel_id,
                message_id,
                emoji,
                user_name,
                removed,
            )
        except Exception as exc:
            logger.warning("[discord-reaction] 处理 reaction 失败: %s", exc)

    async def _get_message_summary(
        self,
        payload,
        client,
        message_id: str,
    ) -> tuple[str, str]:
        cached = self._message_cache.get(message_id)
        if cached:
            return cached

        author_name = ""
        content = ""
        try:
            channel = client.get_channel(getattr(payload, "channel_id", None))
            if channel is None:
                channel = await client.fetch_channel(
                    getattr(payload, "channel_id", None)
                )
            message = await channel.fetch_message(int(message_id))
            author = getattr(message, "author", None)
            author_name = str(
                getattr(author, "display_name", None)
                or getattr(author, "name", "")
                or ""
            )
            content = str(getattr(message, "content", "") or "").strip()[:300]
            self._message_cache[message_id] = (author_name, content)
            if len(self._message_cache) > 1000:
                self._message_cache.clear()
        except Exception as exc:
            logger.debug("[discord-reaction] 无法读取 reaction 对应消息: %s", exc)
        return author_name, content

    @staticmethod
    def _reaction_user_name(payload, client, user_id: str) -> str:
        member = getattr(payload, "member", None)
        if member is not None:
            return str(
                getattr(member, "display_name", None)
                or getattr(member, "name", "")
                or user_id
            )
        user = None
        try:
            user = client.get_user(int(user_id)) if user_id else None
        except (TypeError, ValueError):
            user = None
        if user is not None:
            return str(
                getattr(user, "display_name", None)
                or getattr(user, "name", "")
                or user_id
            )
        return user_id

    @filter.on_llm_request()
    async def inject_reaction_context(
        self, event: AstrMessageEvent, req: ProviderRequest
    ) -> None:
        if not self._bool("enabled", True) or not self._bool(
            "reaction_context_enabled", True
        ):
            return
        channel_id = self._event_channel_id(event)
        history = self._reaction_history.get(channel_id)
        if not history:
            return

        lines = []
        for entry in list(history)[-self._reaction_limit() :]:
            action = "移除" if entry.get("removed") else "添加"
            content = entry.get("content") or "（无文本内容）"
            lines.append(
                f"- {entry.get('user_name') or '未知用户'} {action}了 "
                f"{entry.get('emoji')} 到 {entry.get('message_author') or '某条消息'} "
                f"的消息「{str(content)[:120]}」"
            )
        if not lines:
            return

        note = (
            "\n\n[Discord 最近的表情 reaction]\n"
            "以下是当前频道最近被添加/移除的 reaction，供你理解对话氛围；"
            "不要逐条复述，除非用户问起。\n" + "\n".join(lines)
        )
        req.system_prompt = (req.system_prompt or "") + note

    @filter.llm_tool(name="discord_get_reactions")
    async def discord_get_reactions(
        self,
        event: AstrMessageEvent,
        message_id: str = "",
    ) -> str:
        """查看当前 Discord 频道最近的表情 reaction；也可以查某一条消息的 reaction。

        Args:
            message_id(string): 可选。只查看这条 Discord 消息 ID 的 reaction；留空查看频道最近记录。
        """
        if not self._bool("enabled", True) or not self._bool(
            "reaction_listen_enabled", True
        ):
            return "Discord reaction listening is disabled."

        channel_id = self._event_channel_id(event)
        history = list(self._reaction_history.get(channel_id, []))
        target_id = str(message_id or "").strip()
        if target_id:
            history = [item for item in history if item.get("message_id") == target_id]
        if not history:
            return "No recent reactions found."

        lines = []
        for entry in history[-self._reaction_limit() :]:
            action = "removed" if entry.get("removed") else "added"
            lines.append(
                f"- {entry.get('user_name') or 'unknown'} {action} "
                f"{entry.get('emoji')} on message {entry.get('message_id')} "
                f"(author: {entry.get('message_author') or 'unknown'}, "
                f"content: {str(entry.get('content') or '')[:160]})"
            )
        return "\n".join(lines)

    def _event_channel_id(self, event: AstrMessageEvent) -> str:
        raw = getattr(getattr(event, "message_obj", None), "raw_message", None)
        channel = getattr(raw, "channel", None)
        return str(getattr(channel, "id", "") or "")

    # ------------------------------------------------------------------
    # 自动反应
    # ------------------------------------------------------------------
    @filter.platform_adapter_type(filter.PlatformAdapterType.DISCORD)
    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_discord_message(self, event: AstrMessageEvent):
        self._ensure_listener(event)
        if not self._bool("enabled", True) or not self._bool("auto_enabled", True):
            return
        if not self._should_try_auto(event):
            return
        task = asyncio.create_task(self._auto_react(event))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _should_try_auto(self, event: AstrMessageEvent) -> bool:
        raw = getattr(getattr(event, "message_obj", None), "raw_message", None)
        if raw is None or not hasattr(raw, "add_reaction"):
            return False

        text = str(getattr(event, "message_str", "") or "").strip()
        min_length = self._int("min_message_length", 2, 0, 50)
        if len(text) < min_length:
            return False
        if self._bool("skip_commands", True) and text[:1] in {"/", "!"}:
            return False
        if self._bool("mention_only", False):
            checker = getattr(event, "is_mentioned", None)
            if callable(checker):
                try:
                    if not checker():
                        return False
                except Exception:
                    return False

        channel_key = self._channel_key(event)
        now = time.monotonic()
        cooldown = self._cooldown()
        last = self._last_reaction.get(channel_key, 0.0)
        if cooldown > 0 and now - last < cooldown:
            return False

        tendency = self._tendency()
        if tendency <= 0:
            return False
        if tendency < 100 and random.randint(1, 100) > tendency:
            return False

        # 先占住冷却，避免同一频道连续任务并发。
        self._last_reaction[channel_key] = now
        return True

    async def _auto_react(self, event: AstrMessageEvent) -> None:
        try:
            emoji = await self._choose_emoji(event)
            if not emoji:
                return
            await event.react(emoji)
            logger.info(
                "[discord-reaction] 自动反应 | origin=%s emoji=%s",
                getattr(event, "unified_msg_origin", ""),
                emoji,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("[discord-reaction] 自动反应失败: %s", exc)

    async def _choose_emoji(self, event: AstrMessageEvent) -> str | None:
        pool = self._pool()
        text = str(getattr(event, "message_str", "") or "").strip()
        if not text:
            return None

        provider = self._provider(event)
        if provider is None:
            return random.choice(pool)

        prompt = (
            "你在为一个 Discord 消息选择 reaction 表情。\n"
            "只输出一个 emoji，不要解释，不要标点，不要加引号。\n"
            "如果没有合适的表情，输出 NONE。\n"
            f"可选表情：{', '.join(pool)}\n"
            f"消息内容：{text[:800]}\n"
        )
        try:
            response = await asyncio.wait_for(
                provider.text_chat(
                    prompt=prompt,
                    session_id=uuid.uuid4().hex,
                    persist=False,
                ),
                timeout=12,
            )
            return self._parse_emoji(
                str(getattr(response, "completion_text", "") or ""),
                pool,
            )
        except Exception as exc:
            logger.warning("[discord-reaction] 模型选表情失败，使用随机表情: %s", exc)
            return random.choice(pool)

    def _provider(self, event: AstrMessageEvent) -> Provider | None:
        provider_id = self._str("provider_id")
        provider = None
        if provider_id:
            provider = self.context.get_provider_by_id(provider_id)
        if provider is None:
            provider = self.context.get_using_provider(
                getattr(event, "unified_msg_origin", None)
            )
        return provider if isinstance(provider, Provider) else None

    @staticmethod
    def _channel_key(event: AstrMessageEvent) -> str:
        raw = getattr(getattr(event, "message_obj", None), "raw_message", None)
        channel = getattr(raw, "channel", None)
        return str(
            getattr(channel, "id", None)
            or getattr(event, "get_group_id", lambda: "")()
            or getattr(event, "session_id", "")
        )

    # ------------------------------------------------------------------
    # LLM 工具
    # ------------------------------------------------------------------
    @filter.llm_tool(name="discord_react")
    async def discord_react(
        self,
        event: AstrMessageEvent,
        emoji: str,
        message_id: str = "",
    ) -> str:
        """给 Discord 消息添加一个 reaction 表情。你需要自己判断是否适合以及选哪个 emoji。

        Args:
            emoji(string): 要添加的单个 emoji，例如 👍、😂、🥰。不要输出解释。
            message_id(string): 可选。要反应的 Discord 消息 ID；留空表示当前消息。
        """
        if not self._bool("enabled", True) or not self._bool("tool_enabled", True):
            return "Discord reaction tool is disabled."

        cleaned = self._clean_emoji(emoji)
        if not cleaned:
            return "Invalid emoji: please provide exactly one emoji."

        raw = getattr(getattr(event, "message_obj", None), "raw_message", None)
        if raw is None or not hasattr(raw, "add_reaction"):
            return "This message is not a Discord message; reaction not supported."

        try:
            target = raw
            raw_id = str(message_id or "").strip()
            if raw_id:
                if not raw_id.isdigit():
                    return "Invalid message_id: must be a Discord message ID."
                channel = getattr(raw, "channel", None)
                if channel is None:
                    return "Cannot access the current Discord channel."
                target = await channel.fetch_message(int(raw_id))
            await target.add_reaction(cleaned)
            logger.info(
                "[discord-reaction] 工具反应 | origin=%s emoji=%s",
                getattr(event, "unified_msg_origin", ""),
                cleaned,
            )
            return f"Reaction added: {cleaned}"
        except Exception as exc:
            logger.warning("[discord-reaction] 工具反应失败: %s", exc)
            return f"Failed to add reaction: {exc}"

    # ------------------------------------------------------------------
    # emoji 工具函数
    # ------------------------------------------------------------------
    @staticmethod
    def _clean_emoji(value: str) -> str:
        text = str(value or "").strip().strip("\"'`")
        if not text or len(text) > 64 or "\n" in text or "\r" in text:
            return ""
        if CUSTOM_EMOJI_RE.match(text):
            return text
        if not SAFE_TEXT_RE.match(text):
            return ""
        # 只接受不含空白的短 token，避免模型把解释塞进 reaction。
        if any(ch.isspace() for ch in text):
            return ""
        return text

    @classmethod
    def _parse_emoji(cls, output: str, pool: list[str]) -> str | None:
        text = str(output or "").strip()
        if not text or text.upper().startswith("NONE"):
            return None
        for emoji in sorted(pool, key=len, reverse=True):
            if emoji in text:
                return emoji
        first = text.split()[0].strip("\"'`")
        cleaned = cls._clean_emoji(first)
        if cleaned and cleaned.upper() != "NONE":
            return cleaned
        return None
