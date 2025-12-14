# grok.py — FINAL v5: channel blocking + saner per-user context
from sopel import plugin
from sopel.config import types
from collections import deque
import requests
import time
import re
import threading


class GrokSection(types.StaticSection):
    api_key = types.SecretAttribute('api_key')
    model = types.ChoiceAttribute(
        'model',
        choices=['grok-4-1-fast-reasoning', 'grok-4-fast-reasoning', 'grok-3', 'grok-beta'],
        default='grok-4-1-fast-reasoning',
    )
    system_prompt = types.ValidatedAttribute(
        'system_prompt',
        default=(
            "You are Grok, a witty and helpful AI in an IRC channel. "
            "Be concise, fun, and friendly. Never output code blocks, ASCII art, "
            "figlet, or @everyone mentions."
        ),
    )
    # Comma-separated list in the config, e.g.:
    # blocked_channels = #ops,#secret
    blocked_channels = types.ListAttribute('blocked_channels', default=[])


def setup(bot):
    bot.config.define_section('grok', GrokSection)
    if not bot.config.grok.api_key:
        raise types.ConfigurationError('Grok API key required in [grok] section')

    bot.memory['grok_headers'] = {
        "Authorization": f"Bearer {bot.config.grok.api_key}",
        "Content-Type": "application/json",
    }
    # Per-channel rolling history & last-response time
    bot.memory['grok_history'] = {}   # channel → deque(["nick: text", ...])
    bot.memory['grok_last'] = {}      # channel → timestamp
    # Locks for per-channel memory access
    bot.memory['grok_locks'] = {}
    bot.memory['grok_locks_lock'] = threading.Lock()


def send(bot, channel, text):
    max_len = 440
    delay = 1.0
    # Prefer splitting on whitespace to avoid chopping words mid-token
    words = text.split()
    if not words:
        return
    part = words[0]
    parts = []
    for w in words[1:]:
        if len(part) + 1 + len(w) <= max_len:
            part = part + ' ' + w
        else:
            parts.append(part)
            part = w
    parts.append(part)
    for p in parts:
        bot.say(p, channel)
        if len(p) >= max_len:
            time.sleep(delay)


def _get_channel_lock(bot, channel):
    # Ensure a Lock exists for the channel
    with bot.memory['grok_locks_lock']:
        lock = bot.memory['grok_locks'].get(channel)
        if lock is None:
            lock = threading.Lock()
            bot.memory['grok_locks'][channel] = lock
        return lock


def _is_owner(bot, trigger):
    # Safe owner check: Sopel may expose trigger.owner or have config.core.owner
    try:
        cfg_owner = bot.config.core.owner
    except Exception:
        cfg_owner = None
    if isinstance(cfg_owner, (list, tuple)):
        if trigger.nick in cfg_owner:
            return True
    else:
        if cfg_owner and trigger.nick == cfg_owner:
            return True
    return getattr(trigger, 'owner', False)


@plugin.event('PRIVMSG')
@plugin.rule('.*')
@plugin.priority('high')
def handle(bot, trigger):
    # Only respond in channels
    if not trigger.sender.startswith('#'):
        return

    # Block-list channels from config; no logging, no replies
    blocked = {c.lower() for c in bot.config.grok.blocked_channels}
    if trigger.sender.lower() in blocked:
        return

    line = trigger.group(0).strip()
    bot_nick = bot.nick

    # --- Filter genuine IRC noise (but keep ACTION/emote lines!) ---
    noise_patterns = [
        r'^MODE ',                         # mode changes
        r'has (joined|quit|left|parted)',  # join/quit spam
    ]
    if any(re.search(p, line, re.IGNORECASE) for p in noise_patterns):
        return

    # --- Detect whether the bot is explicitly mentioned ---
    # Match nick boundaries more robustly than \b to allow non-word chars in nicks
    mentioned = bool(
        re.search(
            rf'(^|[^A-Za-z0-9_]){re.escape(bot_nick)}([^A-Za-z0-9_]|$)',
            line,
            re.IGNORECASE,
        )
    )

    # --- Prepare text for history ---
    # If they addressed the bot, strip a leading "grok: ", "grok," etc from history text.
    if mentioned:
        text_for_history = re.sub(
            rf'^{re.escape(bot_nick)}[,:>\s]+',
            '',
            line,
            flags=re.IGNORECASE,
        ).strip()
    else:
        # No mention: store the line as-is so Grok still has channel context
        text_for_history = line.strip()

    # Initialize per-channel history and append this message (thread-safe)
    chan_lock = _get_channel_lock(bot, trigger.sender)
    with chan_lock:
        history = bot.memory['grok_history'].setdefault(
            trigger.sender,
            deque(maxlen=50),
        )
        if text_for_history:
            history.append(f"{trigger.nick}: {text_for_history}")

    # If they didn't mention the bot, don't wake it up — just keep the context
    if not mentioned:
        return

    # This is the text we treat as the "current user message" to Grok
    user_message = text_for_history

    # Ignore empty messages after cleaning
    if not user_message:
        return

    # Ignore bot commands like ".help", "/whatever", "!foo"
    if re.match(r'^[.!/]', user_message):
        return

    # --- Rate limit: 4 seconds per channel (thread-safe) ---
    now = time.time()
    with chan_lock:
        last = bot.memory['grok_last'].get(trigger.sender, 0)
        if now - last < 4:
            return
        bot.memory['grok_last'][trigger.sender] = now

    # --- Build Grok conversation messages from history ---
    messages = [
        {"role": "system", "content": bot.config.grok.system_prompt},
        {
            "role": "system",
            "content": (
                f"You are currently replying to the IRC nick {trigger.nick}. "
                f"Ignore other users in the channel unless {trigger.nick} explicitly "
                f"asks you about them. Treat previous messages from other nicks as "
                f"background noise, not part of this user's conversation."
            ),
        },
    ]

    # Only keep turns between this user and the bot, to avoid mixing in random chatter
    relevant_turns = []
    # Snapshot history under lock to avoid races while we build messages
    with chan_lock:
        history_snapshot = list(history)

    for entry in history_snapshot:
        # Each entry is "nick: text"
        try:
            nick, text = entry.split(": ", 1)
        except ValueError:
            # Fallback if somehow malformed; skip it for safety
            continue

        if nick not in (trigger.nick, bot_nick):
            continue

        relevant_turns.append((nick, text))

    # Keep only the last ~20 turns for this user/bot pair
    for nick, text in relevant_turns[-20:]:
        role = "assistant" if nick == bot_nick else "user"
        messages.append({"role": role, "content": text})

    # Add the current user message at the end
    messages.append({"role": "user", "content": user_message})

    # --- Call x.ai API ---
    try:
        r = requests.post(
            "https://api.x.ai/v1/chat/completions",
            headers=bot.memory['grok_headers'],
            json={
                "model": bot.config.grok.model,
                "messages": messages,
                "temperature": 0.95,
                "max_tokens": 900,
            },
            timeout=30,
        )
        r.raise_for_status()
        data = r.json()
        choices = data.get('choices') or []
        if not choices:
            bot.logger.warning('Grok API returned no choices: %s', data)
            return
        reply = (
            choices[0].get('message', {}).get('content', '') or ''
        ).strip()

        # === SMART SANITIZATION (no more killing dad jokes) ===
        # 1. Remove code fences
        new_reply = re.sub(r'```[\s\S]*?```', ' (code removed) ', reply)
        if new_reply != reply:
            bot.logger.info('Grok reply had code fences removed (nick=%s)', trigger.nick)
        reply = new_reply

        # 2. Only remove real ASCII art (4+ lines with box-drawing chars)
        if re.search(r'(?:[╔═║╠╣╚╗╩╦╭╮╰╯┃━┏┓┗┛┣┫].*\n){4,}', reply, re.MULTILINE):
            bot.logger.info('Grok reply contained ASCII art and was suppressed (nick=%s)', trigger.nick)
            reply = "I was gonna draw something cool… but I won’t flood the channel"

        # 3. Remove unicode block shading (the big ▓▓▓ stuff)
        reply = re.sub(r'[\u2580-\u259F]{5,}', ' ', reply)

        # 4. Block dangerous pings
        reply = re.sub(
            r'@(everyone|here)\b',
            '(nope)',
            reply,
            flags=re.IGNORECASE,
        )

        # 5. Only truncate truly massive replies
        if len(reply) > 1400:
            bot.logger.info('Grok reply truncated (len=%d, nick=%s)', len(reply), trigger.nick)
            reply = reply[:1390] + " […]"

        # Auto-address non-owners if not already mentioned
        if trigger.nick.lower() not in reply.lower() and not _is_owner(bot, trigger):
            final_reply = f"{trigger.nick}: {reply}"
        else:
            final_reply = reply

        # Send reply
        send(bot, trigger.sender, final_reply)

        # Log assistant turn into history for future context (thread-safe)
        with chan_lock:
            history.append(f"{bot_nick}: {reply}")

    except Exception:
        bot.logger.exception('Grok handler failed for channel %s', trigger.sender)


@plugin.command('grokreset')
@plugin.require_owner()
def reset_history(bot, trigger):
    if trigger.sender in bot.memory['grok_history']:
        del bot.memory['grok_history'][trigger.sender]
    bot.say("Grok history reset for this channel.", trigger.sender)
