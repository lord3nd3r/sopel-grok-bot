# Sopel Grok Bot

[![License: GPL v3](https://img.shields.io/badge/License-GPL%20v3-blue.svg)](https://www.gnu.org/licenses/gpl-3.0)

A small, focused Sopel plugin that integrates xAI's Grok into your IRC channels. The bot listens for addressed messages, maintains short per-channel/per-user context, and replies using the Grok chat completions API.

Key design points:
- Lightweight single-file plugin (`ai-grok.py`) — drop into your Sopel scripts or install as a module.
- Per-channel rolling history and per-user context to avoid cross-talk.
- Configurable model, system prompt, and channel blocklist.
- Smart sanitization: strips code fences, blocks dangerous pings and excessive ASCII art.

## Features

- Addressed replies: mention the bot (e.g. `BotNick: how are you?`) to get a response.
- Background context: non-addressed messages are stored to give Grok channel context without waking the bot.
- Per-channel rate limiting and history to reduce spam and blend with IRC flow.
- Configuration via a `[grok]` section in your Sopel config.
- Admin command: `grokreset` (owner-only) to clear channel history.

## Requirements

- Python 3.8+
- Sopel (tested with Sopel 8.x)
- `requests` Python package
- A valid Grok / x.ai API key

## Installation

1. Copy the plugin into your Sopel scripts directory (or add to your bot's plugins path):

```bash
cp ai-grok.py ~/.sopel/scripts/
```

2. Install dependencies:

```bash
pip install sopel requests
```

3. Add a `[grok]` section to your Sopel config (usually `~/.sopel/default.cfg` or `~/.sopel/sopel.cfg`). Example:

```ini
[grok]
api_key = your_xai_api_key_here
model = grok-4-1-fast-reasoning
system_prompt = You are Grok, a witty and helpful AI in an IRC channel. Be concise, fun, and friendly.
blocked_channels = #ops,#private
```

Notes:
- `api_key` is required — the plugin will raise a configuration error if missing.
- `model` can be one of: `grok-4-1-fast-reasoning`, `grok-4-fast-reasoning`, `grok-3`, `grok-beta`.
- `blocked_channels` is a comma-separated list of channel names where the bot should remain silent.

## Usage

- Trigger a reply by mentioning the bot in a channel: `BotNick: what's the weather?`.
- The bot stores non-addressed lines in per-channel history for context, but will only respond when directly addressed.
- Owner-only command to reset history in a channel:

```
.grokreset
```

## Configuration details

- `system_prompt`: A system-level instruction given to Grok to shape tone and behavior. The default prompt asks Grok to be concise, witty, and to avoid code blocks or mass pings.
- Rate-limiting: The plugin enforces a short (4s) per-channel cooldown to avoid flooding.
- Sanitization: The plugin strips code fences, removes large ASCII art, filters block pings like `@everyone`, and truncates very long replies.

## Troubleshooting

- If the bot doesn't start, ensure `[grok]` has `api_key` set. The plugin raises an error on missing key.
- If replies seem missing, check `blocked_channels` and per-channel rate limits.
- Network/API issues: the plugin uses a short HTTP timeout and silently fails on exceptions to avoid disrupting the bot.

## Development

- The primary plugin file is `ai-grok.py`. Read it to change defaults like `system_prompt`, models, or sanitization rules.
- To test locally, run a Sopel instance with your config and watch channel behavior.

## License

This project is licensed under the GPL v3. See the `LICENSE` file for details.

## Contributing

- Contributions are welcome. Open issues or PRs with small, focused improvements.

Enjoy Grok in your channel — be kind to other humans! :)
