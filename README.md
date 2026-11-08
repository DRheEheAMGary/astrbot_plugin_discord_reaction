# Discord 表情反应

让 AstrBot 在 Discord 中自己挑选 emoji 添加 reaction。

## 功能

- 自动反应：按「回复表情倾向」滑块决定是否处理新消息
- 模型选表情：命中后由当前 LLM 根据消息内容选择 emoji
- LLM 工具：`discord_react`，模型可自己选择表情和消息
- 频道冷却、最短消息长度、跳过指令、只回应 @Bot
- 表情池可配置

## 安装

把目录放到：

```text
AstrBot/data/plugins/astrbot_plugin_discord_reaction/
```

然后在 AstrBot 面板插件页重载/启用，不需要重启容器。

## 配置

- `tendency`：回复表情倾向，0～100
- `cooldown_seconds`：同频道冷却
- `min_message_length`：太短的消息不反应
- `mention_only`：只对 @Bot 的消息反应
- `tool_enabled`：是否提供 `discord_react` 工具
- `emoji_pool`：逗号分隔的候选表情
