# grok.py — FINAL v3: Safe, fun, actually tells dad jokes now
from sopel import plugin
from sopel.config import types
from collections import deque
import requests
import time
import re

class GrokSection(types.StaticSection):
    api_key = types.SecretAttribute('api_key')
    model = types.ChoiceAttribute(
        'model',
        choices=['grok-4-1-fast-reasoning', 'grok-4-fast-reasoning', 'grok-3', 'grok-beta'],
        default='grok-4-1-fast-reasoning',
    )
    system_prompt = types.ValidatedAttribute(
        'system_prompt',
        default="You are Grok, a witty and helpful AI in an IRC channel. Be concise, fun, and friendly. Never output code blocks, ASCII art, figlet, or @everyone mentions."
    )

def setup(bot):
    bot.config.define_section('grok', GrokSection)
    if not bot.config.grok.api_key:
        raise types.ConfigurationError('Grok API key required in [grok] section')

    bot.memory['grok_headers'] = {
        "Authorization": f"Bearer {bot.config.grok.api_key}",
        "Content-Type": "application/json",
    }
    bot.memory['grok_history'] = {}   # channel → deque
    bot.memory['grok_last'] = {}      # channel → timestamp

def send(bot, channel, text):
    max_len = 440
    delay = 1.0
    for part in [text[i:i + max_len] for i in range(0, len(text), max_len)]:
        bot.say(part, channel)
        if len(part) == max_len:
            time.sleep(delay)

@plugin.event('PRIVMSG')
@plugin.rule('.*')
@plugin.priority('high')
def handle(bot, trigger):
    if not trigger.sender.startswith('#'):
        return

    line = trigger.group(0).strip()
    bot_nick = bot.nick

    # Require explicit mention of bot nick
    if not re.search(rf'\b{re.escape(bot_nick)}\b', line, re.IGNORECASE):
        return

    # Clean message: remove "grok:", "grok," etc.
    user_message = re.sub(rf'^{re.escape(bot_nick)}[,:>\s]+', '', line, flags=re.IGNORECASE).strip()
    if not user_message:
        return

    # Ignore bot commands
    if re.match(r'^[.!/]', user_message):
        return

    # Filter IRC noise
    noise = [
        r'^\* ', r'^\001ACTION', r'^MODE ', r'has (joined|quit|left|parted)'
    ]
    if any(re.search(p, line, re.IGNORECASE) for p in noise):
        return

    # Rate limit: 4 seconds per channel
    now = time.time()
    last = bot.memory['grok_last'].get(trigger.sender, 0)
    if now - last < 4:
        return
    bot.memory['grok_last'][trigger.sender] = now

    # History
    history = bot.memory['grok_history'].setdefault(trigger.sender, deque(maxlen=50))
    history.append(f"{trigger.nick}: {user_message}")

    # Build message list
    messages = [{"role": "system", "content": bot.config.grok.system_prompt}]
    for entry in history:
        nick, text = entry.split(": ", 1)
        role = "assistant" if nick == bot_nick else "user"
        messages.append({"role": role, "content": text})

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
        reply = r.json()["choices"][0]["message"]["content"].strip()

        # === SMART SANITIZATION (no more killing dad jokes) ===
        # 1. Remove code fences
        reply = re.sub(r'```[\s\S]*?```', ' (code removed) ', reply)

        # 2. Only remove real ASCII art (4+ lines with box-drawing chars)
        if re.search(r'(?:[╔═║╠╣╚╗╩╦╭╮╰╯┃━┏┓┗┛┣┫].*\n){4,}', reply, re.MULTILINE):
            reply = "I was gonna draw something cool… but I won’t flood the channel "

        # 3. Remove unicode block shading (the big ▓▓▓ stuff)
        reply = re.sub(r'[\u2580-\u259F]{5,}', ' ', reply)

        # 4. Block dangerous pings
        reply = re.sub(r'@(everyone|here)\b', '(nope)', reply, flags=re.IGNORECASE)

        # 5. Only truncate truly massive replies
        if len(reply) > 1400:
            reply = reply[:1390] + " […]"

        # Auto-address non-owners if not already mentioned
        if trigger.nick.lower() not in reply.lower() and not trigger.owner:
            final_reply = f"{trigger.nick}: {reply}"
        else:
            final_reply = reply

        send(bot, trigger.sender, final_reply)
        history.append(f"{bot_nick}: {reply}")

    except Exception:
        # Silent fail — bot lives on
        pass

@plugin.command('grokreset')
@plugin.require_owner()
def reset_history(bot, trigger):
    if trigger.sender in bot.memory['grok_history']:
        del bot.memory['grok_history'][trigger.sender]
    bot.say("Grok history reset for this channel.", trigger.sender)
