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

from astrbot.api import logger, star
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import Provider

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

    # ------------------------------------------------------------------
    # 自动反应
    # ------------------------------------------------------------------
    @filter.platform_adapter_type(filter.PlatformAdapterType.DISCORD)
    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_discord_message(self, event: AstrMessageEvent):
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
