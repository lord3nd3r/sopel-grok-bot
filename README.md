# Sopel Grok Bot

[![License: GPL v3](https://img.shields.io/badge/License-GPL%20v3-blue.svg)](https://www.gnu.org/licenses/gpl-3.0)

A feature-rich Sopel plugin that integrates [xAI's Grok](https://x.ai) into your IRC channels. The bot listens for addressed messages, maintains per-user/per-channel context backed by SQLite, supports live web search, handles emote actions, and replies naturally using the Grok chat completions API.

## Features

- **Addressed replies** — mention the bot (`BotNick: how are you?`) in a channel or send it a PM to get a response.
- **Live web search** — automatically detects search-intent queries ("latest news on...", "what's the score?", "weather forecast") and uses the xAI Responses API with a built-in `web_search` tool.
- **Persistent per-user history** — conversation context is saved to a local SQLite database so the bot remembers past exchanges across restarts.
- **Time & date queries** — detects time/date questions and answers with the user's saved timezone and format preference (defaults to UTC 24-hour).
- **User timezone/format preferences** — users can tell the bot their timezone (`I'm in CST`) or preferred time format (`I prefer 12-hour`) and it will remember them.
- **Channel-wide context awareness** — the bot passively reads all channel messages and uses them as background context, so you can ask it questions about what other users said (e.g. "what did KnownSyntax add to his beer?").
- **Review mode** — ask the bot for a quick opinion on the recent conversation ("Grok: what do you think?").
- **CTCP emote responses** — the bot responds to IRC `/me` actions directed at it (pets, hugs, boops, etc.) with fun random replies.
- **Admin PM commands** — bot admins can manage channels and ignore lists via PM commands.
- **Channel blocklist** — prevent the bot from responding in specified channels.
- **Nick ignore list** — configurable via both the Sopel config and admin PM commands.
- **Configurable nick ban list** — prevent specific nicks from using the bot via PM.
- **Rate limiting** — per-channel and per-user cooldowns to prevent flooding.
- **Reply sanitization** — strips code fences, blocks ASCII art floods, removes `@everyone`/`@here` pings, and truncates overly long replies.
- **Async API worker pool** — API calls are handled by a background thread pool (non-blocking) with retry/backoff logic and graceful fallback.

## Requirements

- Python 3.8+
- Sopel 8.x
- `requests` Python package
- A valid [xAI / Grok API key](https://x.ai)

## Installation

1. Copy the plugin into your Sopel scripts directory:

```bash
cp ai-grok.py ~/.sopel/scripts/
```

2. Install dependencies:

```bash
pip install sopel requests
```

3. Add a `[grok]` section to your Sopel config (usually `~/.sopel/default.cfg` or `~/.sopel/sopel.cfg`):

```ini
[grok]
api_key = your_xai_api_key_here
model = grok-4-1-fast-reasoning
system_prompt = You are Grok, a witty and helpful AI in an IRC channel. Be concise, fun, and friendly.
blocked_channels = #ops,#private
banned_nicks = spambot,annoyinguser
ignored_nicks = SomeOtherBot,AutoScript
```

4. Restart your Sopel bot. The plugin will create a `grok_data/` directory next to the script file to store its SQLite database.

## Configuration Reference

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `api_key` | secret | *(required)* | Your xAI API key. The plugin will refuse to load without this. |
| `model` | choice | `grok-4-1-fast-reasoning` | Grok model to use. Choices: `grok-4-1-fast-reasoning`, `grok-4-fast-reasoning`, `grok-3`, `grok-beta`. |
| `system_prompt` | string | *(see below)* | System-level instruction sent to Grok to shape tone and behavior. |
| `blocked_channels` | list | *(empty)* | Comma-separated channel names where the bot will not respond. |
| `banned_nicks` | list | *(empty)* | Nicks banned from using the bot via PM. |
| `ignored_nicks` | list | *(empty)* | Nicks whose messages are fully ignored (e.g. other bots). |
| `intent_check` | choice | `heuristic` | How to decide if a message is addressing the bot: `heuristic`, `model`, or `off`. |

You can also set `AI_GROK_DIR` as an environment variable to override where the SQLite database is stored.

## Usage

### Chatting with the bot

Address the bot by nick in a channel or send it a PM:

```
<you> BotNick: what do you think about Rust?
<BotNick> you: Rust is great for systems programming — strong safety guarantees …
```

### Channel context questions

The bot passively stores what everyone in the channel says. You can ask it about prior conversation:

```
<End3r> glitchy: what did KnownSyntax add to his beer?
<glitchy> End3r: KnownSyntax said to add an extra lime to it.

<End3r> glitchy: what beer was I having?
<glitchy> End3r: You were having a Corona.
```

Up to the last ~40 lines of channel activity (across all nicks) are included as context on every reply.

### Live web search

The bot automatically detects search-intent queries and fetches live results via web search:

```
<you> BotNick: what's the latest news on OpenAI?
<you> BotNick: search for current Bitcoin price
<you> BotNick: what's the weather forecast this week?
```

### Time and date queries

```
<you> BotNick: what time is it?
<BotNick> you: It's 14:32 UTC (Thursday, February 20, 2026)
```

If you've set your timezone preference, the bot will use it automatically.

### Setting your timezone/format preference

Just tell the bot naturally:

```
<you> BotNick: I'm in CST
<you> BotNick: my timezone is Pacific
<you> BotNick: I prefer 12-hour time
```

Supported timezone shortcuts: `EST`/`EDT`, `CST`/`CDT`, `MST`/`MDT`, `PST`/`PDT`, `ET`, `CT`, `MT`, `PT`, `UTC`, `GMT`, `eastern`, `central`, `mountain`, `pacific`.

### Emote responses

The bot responds to IRC `/me` actions directed at it:

```
* you pets BotNick
* BotNick rolls over for pets 🥺
```

Supported actions include: pet, hug, poke, boop, kiss, wave, dance, nuzzle, cuddle, snuggle, and more.

## Commands

### User commands

| Command | Description |
|---------|-------------|
| `$grokreset` | Reset your own conversation history with the bot (in channels or PM). |
| `$grokreset channel` | Reset the entire channel's history (requires bot admin or channel op). |

### Admin commands (PM only)

These commands must be sent to the bot via PM. Requires bot admin or owner privileges.

| Command | Description |
|---------|-------------|
| `$join #channel [key]` | Make the bot join a channel (with optional key). |
| `$part #channel` | Make the bot leave a channel. |
| `$ignore nick` | Add a nick to the admin ignore list (persisted to DB). |
| `$unignore nick` | Remove a nick from the admin ignore list. |

## Database

The bot stores data in a SQLite database (`grok_data/grok.sqlite3` by default, next to the script). Three tables are maintained:

- **`grok_user_history`** — per-user conversation history for persistent context.
- **`grok_admin_ignored_nicks`** — admin-managed ignore list, persisted across restarts.
- **`grok_user_prefs`** — per-user timezone and time format preferences.

## Architecture

- **Single-file plugin** (`ai-grok.py`) — drop into your Sopel scripts directory.
- **Async API worker pool** — API requests are handled by a configurable thread pool (default: 3 workers, queue size: 50) to avoid blocking the bot's main event loop.
- **Retry with exponential backoff** — up to 3 API attempts per request; search failures automatically fall back to the standard chat completions API.
- **Dual API support** — uses the xAI Responses API (`/v1/responses`) for web-search queries and the Chat Completions API (`/v1/chat/completions`) for regular conversations.

## Troubleshooting

- **Bot doesn't start**: Ensure `[grok]` has `api_key` set. The plugin raises a `ConfigurationError` on missing key.
- **No replies in a channel**: Check `blocked_channels`. Also verify the bot is being properly addressed (nick: message).
- **Stale context**: Use `$grokreset` to clear history. You can also delete `grok_data/grok.sqlite3` to wipe all persistent data.
- **Rate limit errors**: The bot enforces a 4s per-channel cooldown and a queue of max 50 pending API requests.
- **Time/timezone wrong**: Tell the bot your timezone (e.g. `BotNick: I'm in CST`) and it will remember it.

## Development

- All tunables (rate limits, history sizes, queue sizes, worker counts, etc.) are defined as constants near the top of `ai-grok.py`.
- To test locally, run a Sopel instance pointed at a test IRC server and watch channel behavior.
- The SQLite DB location can be overridden with the `AI_GROK_DIR` environment variable.

## License

Licensed under the [GNU General Public License v3](https://www.gnu.org/licenses/gpl-3.0). See `LICENSE` for details.

## Contributing

Contributions are welcome — open issues or PRs with small, focused improvements.

Enjoy Grok in your channel — be kind to other humans! 🤖
