# grok.py — FINAL v5: channel blocking + saner per-user context
from sopel import plugin
from sopel.config import types
from collections import deque
import sqlite3
import os
import datetime
import requests
import time
import re
import threading
import random
import logging
import queue

# Tunables / constants
MAX_SEND_LEN = 440
SEND_DELAY = 1.0
CHANNEL_RATE_LIMIT = 4
REVIEW_COOLDOWN = 30
USER_SAFETY_SECONDS = 2
API_QUEUE_MAXSIZE = 50
API_WORKER_COUNT = 3

# History and review mode limits
MAX_HISTORY_PER_USER = 20
MAX_HISTORY_ENTRIES = 50
REVIEW_CHAR_BUDGET = 2000
REVIEW_MAX_ENTRIES = 200
MAX_REPLY_LENGTH = 1400
TRUNCATED_REPLY_LENGTH = 1390

# Bounded queue and worker threads to process API requests without unbounded threads
API_TASK_QUEUE = queue.Queue(maxsize=API_QUEUE_MAXSIZE)

# Emote reply mapping (used by both CTCP and secondary emote detection)
EMOTE_REPLY_MAP = {
    'pet': ['nuzzles lovingly 🥰', 'rolls over for pets 🥺', 'squeaks happily 🐾'],
    'pets': ['nuzzles lovingly 🥰', 'rolls over for pets 🥺', 'squeaks happily 🐾'],
    'pat': ['nuzzles lovingly 🥰', 'offers a happy purr 😺'],
    'pats': ['nuzzles lovingly 🥰', 'offers a happy purr 😺'],
    'hug': ['wraps you in a cozy hug 🤗', 'squeezes gently 🤗'],
    'hugs': ['wraps you in a cozy hug 🤗', 'squeezes gently 🤗'],
    'poke': ['boops playfully 👋', 'gives a tiny boop 👀'],
    'pokes': ['boops playfully 👋', 'gives a tiny boop 👀'],
    'boop': ['boops playfully 👋', 'gives a tiny boop 👀'],
    'boops': ['boops playfully 👋', 'gives a tiny boop 👀'],
    'kiss': ['sends a sweet smooch 😘', 'blows a dramatic kiss 😚'],
    'kisses': ['sends a sweet smooch 😘', 'blows a dramatic kiss 😚'],
    'stroke': ['purrs contentedly 😺', 'kneads softly 😽'],
    'strokes': ['purrs contentedly 😺', 'kneads softly 😽'],
    'nuzzle': ['nuzzles warmly 🫶', 'buries face in a soft nuzzle 🥹'],
    'nuzzles': ['nuzzles warmly 🫶', 'buries face in a soft nuzzle 🥹'],
    'snuggle': ['curls up for a snuggle 🥹', 'nestles close for warmth 🫶'],
    'snuggles': ['curls up for a snuggle 🥹', 'nestles close for warmth 🫶'],
    'cuddle': ['cuddles warmly 🤗', 'wraps you in cozy fluff 🧸'],
    'cuddles': ['cuddles warmly 🤗', 'wraps you in cozy fluff 🧸'],
    'smack': ['playfully scolds with a wag 😼', 'gives a mock scold 😼'],
    'smacks': ['playfully scolds with a wag 😼', 'gives a mock scold 😼'],
    'slap': ['gives a surprised gasp and a tut 😳', 'reacts with faux-outrage 😳'],
    'slaps': ['gives a surprised gasp and a tut 😳', 'reacts with faux-outrage 😳'],
    'bonk': ['bonks gently with a plush hammer *bonk* 🫠', 'bonks with a tiny hammer *bonk* 💥'],
    'bonks': ['bonks gently with a plush hammer *bonk* 🫠', 'bonks with a tiny hammer *bonk* 💥'],
    'kick': ['reacts with dramatic flailing 😵‍💫', 'staggers theatrically 😵‍💫'],
    'kicks': ['reacts with dramatic flailing 😵‍💫', 'staggers theatrically 😵‍💫'],
    'punch': ['grumbles and flexes 😤', 'shakes a tiny fist 😤'],
    'punches': ['grumbles and flexes 😤', 'shakes a tiny fist 😤'],
    'highfive': ['gives a triumphant high five ✋', 'slaps a perfect high five ✋'],
    'highfives': ['gives a triumphant high five ✋', 'slaps a perfect high five ✋'],
    'twirl': ['twirls with flair ✨', 'spins in a happy twirl ✨'],
    'twirls': ['twirls with flair ✨', 'spins in a happy twirl ✨'],
    'wave': ['waves cheerfully 👋', 'gives a friendly wave 👋'],
    'waves': ['waves cheerfully 👋', 'gives a friendly wave 👋'],
    'wink': ['winks mischievously 😉', 'gives a cheeky wink 😉'],
    'winks': ['winks mischievously 😉', 'gives a cheeky wink 😉'],
    'dance': ['breaks into a tiny jig 💃', 'grooves a little dance 💫'],
    'dances': ['breaks into a tiny jig 💃', 'grooves a little dance 💫'],
}


def _api_worker_loop():
    while True:
        try:
            task = API_TASK_QUEUE.get()
            if task is None:
                break
            try:
                _api_worker(*task)
            except Exception:
                logging.getLogger('Grok').exception('API worker loop task failed')
            finally:
                API_TASK_QUEUE.task_done()
        except Exception:
            logging.getLogger('Grok').exception('API worker loop crashed')


# Start worker threads
# Worker threads will be started after helper functions are defined (see bottom of file)


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
    intent_check = types.ChoiceAttribute(
        'intent_check',
        choices=['heuristic', 'off', 'model'],
        default='heuristic',
    )
    # Optional list of nicknames (nicks) who are banned from using Grok via PM
    banned_nicks = types.ListAttribute('banned_nicks', default=[])
    # Optional list of nicknames to ignore entirely (other bots, automated scripts)
    ignored_nicks = types.ListAttribute('ignored_nicks', default=[])


def setup(bot):
    bot.config.define_section('grok', GrokSection)
    if not bot.config.grok.api_key:
        raise types.ConfigurationError('Grok API key required in [grok] section')

    bot.memory['grok_headers'] = {
        "Authorization": f"Bearer {bot.config.grok.api_key}",
        "Content-Type": "application/json",
    }
    # Per-conversation rolling history & last-response time
    # Keys: (channel, nick) -> deque(["nick: text", ...])
    # Older versions may have used channel-only keys; we tolerate both when clearing.
    bot.memory['grok_history'] = {}
    bot.memory['grok_last'] = {}      # channel → timestamp
    # Locks for per-channel memory access
    bot.memory['grok_locks'] = {}
    bot.memory['grok_locks_lock'] = threading.Lock()
    # Initialize a small SQLite DB for optional persistent per-user history
    try:
        # Allow override via environment for deployments
        base_dir = os.environ.get('AI_GROK_DIR') or os.path.join(os.path.dirname(__file__), 'grok_data')
        # Ensure the folder exists (create if missing)
        try:
            os.makedirs(base_dir, exist_ok=True)
        except Exception:
            # Fallback to script dir if creation fails
            base_dir = os.path.dirname(__file__)

        db_path = os.path.join(base_dir, 'grok.sqlite3')
        bot.memory['grok_db_path'] = db_path
        # Touch the DB (creates file and tables if missing)
        _init_db(bot)
        # Load persistent admin ignore list
        try:
            _load_admin_ignored_into_memory(bot)
        except Exception:
            bot.memory['grok_admin_ignored'] = set()
    except Exception:
        _log(bot).exception('Failed to initialize Grok DB')
    
    # Start API worker threads
    try:
        _start_api_workers()
    except Exception:
        _log(bot).exception('Failed to start API worker threads')


def send(bot, channel, text):
    # Prefer splitting on whitespace to avoid chopping words mid-token
    max_len = MAX_SEND_LEN
    delay = SEND_DELAY
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
    for i, p in enumerate(parts):
        try:
            bot.say(p, channel)
        except Exception:
            try:
                _log(bot).exception('Failed sending part to %s', channel)
            except Exception:
                pass
        # Sleep between parts to reduce chance of flooding; skip after last part
        if i != len(parts) - 1:
            time.sleep(delay)


def _get_channel_lock(bot, channel):
    # Ensure a Lock exists for the channel
    with bot.memory['grok_locks_lock']:
        lock = bot.memory['grok_locks'].get(channel)
        if lock is None:
            lock = threading.Lock()
            bot.memory['grok_locks'][channel] = lock
        return lock


def _log(bot):
    """Return a logger object: prefer `bot.logger` if present, else a module logger."""
    return getattr(bot, 'logger', logging.getLogger('Grok'))


def _is_owner(bot, trigger):
    # Safe owner check: Sopel may expose trigger.owner or have config.core.owner
    try:
        cfg_owner = bot.config.core.owner
    except Exception:
        cfg_owner = None
    if isinstance(cfg_owner, (list, tuple, set)):
        owners = {o.lower() for o in cfg_owner}
        if trigger.nick.lower() in owners:
            return True
    else:
        if cfg_owner and trigger.nick.lower() == str(cfg_owner).lower():
            return True
    return getattr(trigger, 'owner', False)


def _is_admin(bot, trigger):
    """Return True if the triggering nick is allowed to run admin commands."""
    if _is_owner(bot, trigger):
        return True
    if getattr(trigger, 'admin', False):
        return True

    # Sopel core config often supports an admins list
    try:
        cfg_admins = getattr(bot.config.core, 'admins', None)
    except Exception:
        cfg_admins = None

    if isinstance(cfg_admins, (list, tuple, set)):
        admins = {a.lower() for a in cfg_admins}
        return trigger.nick.lower() in admins

    if isinstance(cfg_admins, str) and cfg_admins.strip():
        admins = {a.strip().lower() for a in re.split(r'[,\s]+', cfg_admins) if a.strip()}
        return trigger.nick.lower() in admins

    return False


def _is_pm(trigger):
    """Best-effort check for private-message triggers."""
    try:
        if getattr(trigger, 'is_privmsg', False):
            return True
    except Exception:
        pass
    try:
        return not trigger.sender.startswith('#')
    except Exception:
        return False


def _is_channel_op(bot, trigger):
    """Return True if the triggering nick appears to be a channel operator in the
    channel the command was invoked from.

    This is best-effort: different Sopel versions expose channel/user state in
    different attributes, so we try several common patterns and fall back to
    False if we can't determine operator status.
    """
    try:
        chan = getattr(bot, 'channels', {}).get(trigger.sender)
        if not chan:
            return False

        # Common attribute: a mapping of nick -> privilege set/int
        privs = getattr(chan, 'privileges', None) or getattr(chan, 'privs', None)
        if isinstance(privs, dict):
            v = privs.get(trigger.nick) or privs.get(trigger.nick.lower())
            if v is None:
                # Some implementations store names as lowercased keys
                for k in privs.keys():
                    if k.lower() == trigger.nick.lower():
                        v = privs.get(k)
                        break
            if v is not None:
                # v may be a set/list of flags (e.g. {'o'}), an int bitmask, or a string
                if isinstance(v, (set, list, tuple)):
                    if 'o' in v or 'op' in v or '@' in v:
                        return True
                if isinstance(v, int):
                    # try a permissive test: non-zero likely indicates some privs
                    if v != 0:
                        return True
                if isinstance(v, str):
                    if 'o' in v or '@' in v:
                        return True

        # Some Channel objects provide helper methods
        if hasattr(chan, 'is_oper'):
            try:
                if chan.is_oper(trigger.nick):
                    return True
            except Exception:
                pass

        # Some channels expose users mapping: nick -> modes
        users = getattr(chan, 'users', None)
        if isinstance(users, dict):
            u = users.get(trigger.nick) or users.get(trigger.nick.lower())
            if isinstance(u, (set, list, tuple)) and ('o' in u or '@' in u):
                return True

    except Exception:
        return False
    return False


def _init_db(bot):
    path = bot.memory.get('grok_db_path')
    if not path:
        return
    conn = sqlite3.connect(path)
    c = conn.cursor()
    c.execute('''
        CREATE TABLE IF NOT EXISTS grok_user_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            nick TEXT NOT NULL,
            source TEXT,
            role TEXT,
            text TEXT,
            ts TEXT
        )
    ''')

    c.execute('''
        CREATE TABLE IF NOT EXISTS grok_admin_ignored_nicks (
            nick TEXT PRIMARY KEY,
            added_by TEXT,
            ts TEXT
        )
    ''')

    c.execute('''
        CREATE TABLE IF NOT EXISTS grok_user_prefs (
            nick TEXT PRIMARY KEY,
            tz_iana TEXT,
            tz_label TEXT,
            time_fmt TEXT
        )
    ''')
    conn.commit()
    conn.close()


def _db_conn(bot):
    path = bot.memory.get('grok_db_path')
    if not path:
        raise RuntimeError('DB path not set')
    return sqlite3.connect(path, check_same_thread=False)


def _db_add_turn(bot, nick, role, text, source=None):
    try:
        conn = _db_conn(bot)
        c = conn.cursor()
        c.execute(
            'INSERT INTO grok_user_history (nick, source, role, text, ts) VALUES (?, ?, ?, ?, ?)',
            (nick.lower(), source or '', role, text, datetime.datetime.utcnow().isoformat()),
        )
        conn.commit()
        conn.close()
    except Exception:
        _log(bot).exception('Failed to write grok DB entry')


def sanitize_reply(bot, trigger, reply):
    """Sanitize model reply: remove code, ascii art, large blocks, pings, and truncate."""
    # Remove code fences first (DOTALL to match newlines)
    new_reply = re.sub(r'```.*?```', ' (code removed) ', reply, flags=re.DOTALL)
    if new_reply != reply:
        try:
            _log(bot).info('Grok reply had code fences removed (nick=%s)', trigger.nick)
        except Exception:
            pass
        try:
            bot.memory.setdefault('grok_metrics', {'requests': 0, 'errors': 0, 'sanitizations': 0})['sanitizations'] += 1
        except Exception:
            pass
    reply = new_reply

    # Suppress large ASCII art blocks (4+ lines of box drawing chars)
    if re.search(r'(?:[╔═║╠╣╚╗╩╦╭╮╰╯┃━┏┓┗┛┣┫].*\n){4,}', reply, re.MULTILINE):
        try:
            _log(bot).info('Grok reply contained ASCII art and was suppressed (nick=%s)', trigger.nick)
        except Exception:
            pass
        return "I was gonna draw something cool… but I won’t flood the channel"

    # Remove unicode block shading
    reply = re.sub(r'[\u2580-\u259F]{5,}', ' ', reply)

    # Block @everyone / @here pings
    reply = re.sub(r'@(everyone|here)\b', '(nope)', reply, flags=re.IGNORECASE)

    # Truncate very large replies
    if len(reply) > MAX_REPLY_LENGTH:
        try:
            _log(bot).info('Grok reply truncated (len=%d, nick=%s)', len(reply), trigger.nick)
        except Exception:
            pass
        reply = reply[:TRUNCATED_REPLY_LENGTH] + " […]"

    return reply


# Regex to detect queries that need live web search
_SEARCH_INTENT_RE = re.compile(
    r'\b(search|news|latest|recent|today|yesterday|tonight|this week|this month|'
    r'current events?|whats? happening|headlines?|score|results?|standings?|'
    r'stock price|weather|forecast|breaking|update|election|poll|'
    r'who won|who died|who is winning|is .+ dead|did .+ happen)\b',
    re.IGNORECASE,
)

# Regex to detect simple time/date queries — these bypass rate-limiting so
# repeated asks always get a fresh, up-to-date answer.
_TIME_INTENT_RE = re.compile(
    r'\b(what(?:\s+is|s|\u2019s)?\s+(the\s+)?(time|date|day)|'
    r'current\s+(time|date)|what\s+time|what\s+day|today(?:\s+is|\s+date)?|'
    r'whats?\s+today|day\s+is\s+it|time\s+is\s+it|date\s+is\s+it)\b',
    re.IGNORECASE,
)

# Map common timezone abbreviations/names to IANA zone IDs
_TZ_ABBR_MAP = {
    'EST': 'America/New_York',   'EDT': 'America/New_York',
    'ET':  'America/New_York',   'EASTERN': 'America/New_York',
    'CST': 'America/Chicago',    'CDT': 'America/Chicago',
    'CT':  'America/Chicago',    'CENTRAL': 'America/Chicago',
    'MST': 'America/Denver',     'MDT': 'America/Denver',
    'MT':  'America/Denver',     'MOUNTAIN': 'America/Denver',
    'PST': 'America/Los_Angeles','PDT': 'America/Los_Angeles',
    'PT':  'America/Los_Angeles','PACIFIC': 'America/Los_Angeles',
    'UTC': 'UTC',                'GMT': 'UTC',
}

# Detect when a user is telling the bot their timezone preference
_TZ_SET_RE = re.compile(
    r'\b(?:i(?:\'m| am)(?:\s+in)?|my\s+(?:tz|timezone|time\s*zone)\s+is|'
    r'set\s+(?:my\s+)?(?:tz|timezone|time\s*zone)\s+to|i\s+live\s+in|'
    r'i(?:\'m| am)\s+in)\b'
    r'.*?\b(EST|EDT|CST|CDT|MST|MDT|PST|PDT|ET|CT|MT|PT|UTC|GMT|eastern|central|mountain|pacific)\b',
    re.IGNORECASE,
)

# Detect when a user is setting their preferred time format
_FMT_SET_RE = re.compile(
    r'\b(?:i\s+prefer|prefer|use|set|like)\b.*?\b(12[\s\-]?h(?:r|our)?|24[\s\-]?h(?:r|our)?)\b',
    re.IGNORECASE,
)


def _call_responses_api(bot, messages, model, temp, max_toks):
    """Call the xAI Responses API (/v1/responses) with web search tool.

    Converts chat-completions-style messages to Responses API format and
    extracts the reply text from the response.

    Returns (reply_text, citations_list) or raises on failure.
    """
    # The Responses API accepts 'input' as an array of messages (same format)
    # and 'tools' for server-side tools like web_search.
    # System messages go into 'instructions'.
    instructions_parts = []
    input_messages = []
    for msg in messages:
        if msg.get('role') == 'system':
            instructions_parts.append(msg['content'])
        else:
            input_messages.append(msg)

    payload = {
        "model": model,
        "input": input_messages,
        "tools": [{"type": "web_search"}],
        "temperature": temp,
        "max_output_tokens": max_toks,
    }
    if instructions_parts:
        payload["instructions"] = " ".join(instructions_parts)

    r = requests.post(
        "https://api.x.ai/v1/responses",
        headers=bot.memory['grok_headers'],
        json=payload,
        timeout=(10, 120),
    )
    r.raise_for_status()
    data = r.json()

    # Parse the Responses API output format:
    # data.output is an array; we look for type=="message" items
    reply = ''
    for item in (data.get('output') or []):
        if item.get('type') == 'message' and item.get('role') == 'assistant':
            for content_part in (item.get('content') or []):
                if content_part.get('type') == 'output_text':
                    reply += content_part.get('text', '')

    # Extract citations if present (annotations or top-level)
    citations = []
    for item in (data.get('output') or []):
        if item.get('type') == 'message':
            for content_part in (item.get('content') or []):
                for ann in (content_part.get('annotations') or []):
                    url = ann.get('url')
                    if url:
                        citations.append(url)

    return reply.strip(), citations


def _call_chat_completions_api(bot, messages, model, temp, max_toks):
    """Call the xAI Chat Completions API (/v1/chat/completions).

    Returns (reply_text, None) or raises on failure.
    """
    payload = {
        "model": model,
        "messages": messages,
        "temperature": temp,
        "max_tokens": max_toks,
    }
    r = requests.post(
        "https://api.x.ai/v1/chat/completions",
        headers=bot.memory['grok_headers'],
        json=payload,
        timeout=(5, 90),
    )
    r.raise_for_status()
    data = r.json()

    choices = (data.get('choices') if isinstance(data, dict) else []) or []
    if not choices:
        return '', None
    reply = (choices[0].get('message', {}).get('content', '') or '').strip()
    return reply, None


def _api_worker(bot, trigger, messages, review_mode, is_pm, bot_nick, chan_lock, search_mode=False):
    """Background worker to call the API and send a reply without blocking the main handler."""
    try:
        attempts = 3
        backoff = 1.0
        reply = None
        citations = None
        temp = 0.95 if not review_mode else 0.85
        max_toks = 900 if not review_mode else 500
        model = bot.config.grok.model

        for attempt in range(1, attempts + 1):
            try:
                if search_mode:
                    # Use the Responses API with web_search tool
                    reply, citations = _call_responses_api(
                        bot, messages, model, temp, max_toks,
                    )
                else:
                    # Use the regular chat completions API
                    reply, citations = _call_chat_completions_api(
                        bot, messages, model, temp, max_toks,
                    )
                break
            except requests.exceptions.Timeout:
                try:
                    metrics = bot.memory.setdefault('grok_metrics', {'requests': 0, 'errors': 0, 'sanitizations': 0})
                    metrics['errors'] = metrics.get('errors', 0) + 1
                except Exception:
                    pass
                if attempt < attempts:
                    time.sleep(backoff + random.random() * 0.5)
                    backoff *= 2
                else:
                    _log(bot).exception('Grok API final attempt timed out')
                    try:
                        bot.say("Grok is timing out right now; please try again later.", trigger.sender)
                    except Exception:
                        pass
                    return
            except requests.exceptions.HTTPError as exc:
                resp_text = ''
                try:
                    resp_text = exc.response.text[:500] if exc.response else ''
                except Exception:
                    pass
                try:
                    metrics = bot.memory.setdefault('grok_metrics', {'requests': 0, 'errors': 0, 'sanitizations': 0})
                    metrics['errors'] = metrics.get('errors', 0) + 1
                except Exception:
                    pass
                # If search failed, fall back to chat completions without search
                if search_mode:
                    _log(bot).warning(
                        'Responses API failed (body=%s), falling back to chat completions',
                        resp_text,
                    )
                    search_mode = False
                    continue
                if attempt < attempts:
                    time.sleep(backoff + random.random() * 0.5)
                    backoff *= 2
                else:
                    _log(bot).exception('Grok API final attempt failed (HTTP error): %s', resp_text)
                    try:
                        bot.say("Grok is having trouble right now; please try again later.", trigger.sender)
                    except Exception:
                        pass
                    return
            except Exception:
                try:
                    metrics = bot.memory.setdefault('grok_metrics', {'requests': 0, 'errors': 0, 'sanitizations': 0})
                    metrics['errors'] = metrics.get('errors', 0) + 1
                except Exception:
                    pass
                if search_mode:
                    _log(bot).warning('Responses API exception, falling back to chat completions')
                    search_mode = False
                    continue
                if attempt < attempts:
                    time.sleep(backoff + random.random() * 0.5)
                    backoff *= 2
                else:
                    _log(bot).exception('Grok API final attempt failed')
                    try:
                        bot.say("Grok is timing out right now; please try again later.", trigger.sender)
                    except Exception:
                        pass
                    return

        if not reply:
            _log(bot).warning('Grok API returned empty reply')
            return

        # Sanitization
        reply = sanitize_reply(bot, trigger, reply)

        # Remove newlines to prevent multi-line responses (IRC doesn't support them)
        # Replace with space to maintain readability
        reply = ' '.join(line.strip() for line in reply.splitlines() if line.strip())

        # Strip citation markers like [1], [2] etc from the reply text
        reply = re.sub(r'\s*\[\d+\]', '', reply)

        # Per-user safety: avoid sending if user spoke very recently
        try:
            user_last = bot.memory.setdefault('grok_user_last', {}).setdefault(trigger.sender, {})
            last_user = user_last.get(trigger.nick, 0)
            if time.time() - last_user < USER_SAFETY_SECONDS:
                return
            user_last[trigger.nick] = time.time()
        except Exception:
            pass

        if review_mode:
            prefixes = ["Hmm...", "TBH,", "I'd say:", "Quick thought:", "Short take:"]
            pref = random.choice(prefixes)
            reply = f"{pref} {reply}"

        try:
            reply = re.sub(rf'^\s*{re.escape(bot_nick)}[,:>\s]+', '', reply, flags=re.IGNORECASE)
        except Exception:
            pass

        if trigger.nick.lower() not in reply.lower() and not _is_owner(bot, trigger):
            final_reply = f"{trigger.nick}: {reply}"
        else:
            final_reply = reply

        send(bot, trigger.sender, final_reply)

        # Append to history and DB under lock
        try:
            with chan_lock:
                per_conv_key = ("PM", trigger.nick.lower()) if is_pm else (trigger.sender, trigger.nick)
                history = bot.memory['grok_history'].setdefault(per_conv_key, deque(maxlen=50))
                history.append(f"{bot_nick}: {reply}")
        except Exception:
            pass
        try:
            _db_add_turn(bot, trigger.nick, 'assistant', reply, 'PM' if is_pm else trigger.sender)
        except Exception:
            pass

    except Exception:
        _log(bot).exception('Grok API worker failed for %s', trigger.sender)



def _db_get_recent(bot, nick, limit=MAX_HISTORY_PER_USER):
    try:
        conn = _db_conn(bot)
        c = conn.cursor()
        c.execute(
            'SELECT role, text FROM grok_user_history WHERE nick = ? ORDER BY id DESC LIMIT ?',
            (nick.lower(), limit),
        )
        rows = c.fetchall()
        conn.close()
        # rows are newest-first; return chronological (oldest-first)
        return list(reversed([(r[0], r[1]) for r in rows]))
    except Exception:
        return []


def _db_clear_user(bot, nick):
    try:
        conn = _db_conn(bot)
        c = conn.cursor()
        c.execute('DELETE FROM grok_user_history WHERE nick = ?', (nick.lower(),))
        conn.commit()
        conn.close()
    except Exception:
        _log(bot).exception('Failed to clear grok DB for %s', nick)


def _db_get_admin_ignored(bot):
    try:
        conn = _db_conn(bot)
        c = conn.cursor()
        c.execute('SELECT nick FROM grok_admin_ignored_nicks')
        rows = c.fetchall()
        conn.close()
        return {r[0].lower() for r in rows if r and r[0]}
    except Exception:
        return set()


def _db_add_admin_ignored(bot, nick, added_by=None):
    try:
        conn = _db_conn(bot)
        c = conn.cursor()
        c.execute(
            'INSERT OR REPLACE INTO grok_admin_ignored_nicks (nick, added_by, ts) VALUES (?, ?, ?)',
            (nick.lower(), (added_by or '').lower(), datetime.datetime.utcnow().isoformat()),
        )
        conn.commit()
        conn.close()
    except Exception:
        _log(bot).exception('Failed to add ignored nick: %s', nick)


def _db_remove_admin_ignored(bot, nick):
    try:
        conn = _db_conn(bot)
        c = conn.cursor()
        c.execute('DELETE FROM grok_admin_ignored_nicks WHERE nick = ?', (nick.lower(),))
        conn.commit()
        conn.close()
    except Exception:
        _log(bot).exception('Failed to remove ignored nick: %s', nick)


def _load_admin_ignored_into_memory(bot):
    bot.memory['grok_admin_ignored'] = _db_get_admin_ignored(bot)


def _db_get_user_pref(bot, nick):
    """Return dict with keys tz_iana, tz_label, time_fmt for nick, or {} if not set."""
    try:
        conn = _db_conn(bot)
        c = conn.cursor()
        c.execute(
            'SELECT tz_iana, tz_label, time_fmt FROM grok_user_prefs WHERE nick = ?',
            (nick.lower(),),
        )
        row = c.fetchone()
        conn.close()
        if row:
            return {'tz_iana': row[0], 'tz_label': row[1], 'time_fmt': row[2]}
        return {}
    except Exception:
        return {}


def _db_set_user_pref(bot, nick, tz=None, fmt=None):
    """Upsert per-user timezone and/or format preference.
    Pass tz=None or fmt=None to leave that field unchanged.
    """
    try:
        conn = _db_conn(bot)
        c = conn.cursor()
        # Read existing values so we only overwrite what was provided
        c.execute(
            'SELECT tz_iana, tz_label, time_fmt FROM grok_user_prefs WHERE nick = ?',
            (nick.lower(),),
        )
        row = c.fetchone()
        cur_iana  = row[0] if row else None
        cur_label = row[1] if row else None
        cur_fmt   = row[2] if row else None

        new_iana  = tz if tz is not None else cur_iana
        new_fmt   = fmt if fmt is not None else cur_fmt

        # Derive a short label from iana if we have one (e.g. 'America/Chicago' -> stored as-is;
        # the actual abbreviation like CST/CDT is computed at query time via strftime)
        new_label = new_iana  # just store the IANA name; label resolved at runtime

        c.execute(
            'INSERT OR REPLACE INTO grok_user_prefs (nick, tz_iana, tz_label, time_fmt) VALUES (?, ?, ?, ?)',
            (nick.lower(), new_iana, new_label, new_fmt),
        )
        conn.commit()
        conn.close()
    except Exception:
        _log(bot).exception('Failed to set user pref for %s', nick)


def _start_api_workers():
    """Start API worker threads. Called from setup() to ensure thread safety."""
    for _ in range(API_WORKER_COUNT):
        t = threading.Thread(target=_api_worker_loop, daemon=True)
        t.start()


def _get_emote_reply(verb, trigger_nick, bot_memory):
    """Get an emote reply for a verb, avoiding repetition for the same user.
    
    Args:
        verb: The verb/action word (e.g., 'pet', 'hug')
        trigger_nick: The nick of the user performing the action
        bot_memory: Bot's memory dict for tracking last replies
    
    Returns:
        A reply string, or None if the verb isn't recognized
    """
    val = EMOTE_REPLY_MAP.get(verb)
    short = None
    
    # Avoid repeating the same emote reply for the same user+verb
    last_map = bot_memory.setdefault('grok_emote_last', {})
    last_key = (trigger_nick.lower(), verb)
    last_choice = last_map.get(last_key)
    
    if isinstance(val, (list, tuple)) and val:
        # Prefer a different choice than last time when possible
        if last_choice and len(val) > 1:
            # Try up to len(val)*2 times to pick a different one
            for _ in range(len(val) * 2):
                cand = random.choice(val)
                if cand != last_choice:
                    short = cand
                    break
            if short is None:
                short = random.choice(val)
        else:
            short = random.choice(val)
    else:
        short = val
    
    # Store the choice for next time
    try:
        if short:
            last_map[last_key] = short
    except Exception:
        pass
    
    return short


def _handle_admin_pm_commands(bot, trigger, user_message):
    """Handle PM-only admin commands.

    Commands:
      $join #channel [key]
      $part #channel
      $ignore nick
      $unignore nick
    """
    s = (user_message or '').strip()
    if not s.startswith('$'):
        return False

    parts = s.split()
    if not parts:
        return False

    cmd = parts[0].lower()
    if cmd not in ('$join', '$part', '$ignore', '$unignore'):
        return False

    if not _is_pm(trigger):
        try:
            bot.reply('Admin commands must be sent via PM.')
        except Exception:
            pass
        return True

    if not _is_admin(bot, trigger):
        try:
            bot.reply('You are not authorized to use admin commands.')
        except Exception:
            pass
        return True

    if cmd == '$join':
        if len(parts) < 2:
            bot.reply('Usage: $join #channel [key]')
            return True
        channel = parts[1]
        key = parts[2] if len(parts) >= 3 else None
        if not channel.startswith('#'):
            bot.reply('Usage: $join #channel [key]')
            return True
        try:
            bot.join(channel, key)
            bot.reply(f'Joining {channel}')
        except Exception:
            _log(bot).exception('Failed to join %s', channel)
            try:
                bot.reply(f'Failed to join {channel}')
            except Exception:
                pass
        return True

    if cmd == '$part':
        if len(parts) < 2:
            bot.reply('Usage: $part #channel')
            return True
        channel = parts[1]
        if not channel.startswith('#'):
            bot.reply('Usage: $part #channel')
            return True
        try:
            bot.part(channel)
            bot.reply(f'Parting {channel}')
        except Exception:
            _log(bot).exception('Failed to part %s', channel)
            try:
                bot.reply(f'Failed to part {channel}')
            except Exception:
                pass
        return True

    # ignore / unignore
    if len(parts) < 2:
        bot.reply(f'Usage: {cmd} nick')
        return True

    nick = parts[1].strip()
    if not nick:
        bot.reply(f'Usage: {cmd} nick')
        return True

    current = bot.memory.setdefault('grok_admin_ignored', set())

    if cmd == '$ignore':
        current.add(nick.lower())
        try:
            _db_add_admin_ignored(bot, nick, added_by=trigger.nick)
        except Exception:
            pass
        bot.reply(f'Ignored {nick}.')
        return True

    # $unignore
    current.discard(nick.lower())
    try:
        _db_remove_admin_ignored(bot, nick)
    except Exception:
        pass
    bot.reply(f'Unignored {nick}.')
    return True


def _heuristic_intent_check(bot, trigger, line, bot_nick):
    """Return True if our heuristics think the bot was intended to be addressed.

    Heuristics used (best-effort):
    - If the message starts with the bot nick (vocative), respond.
    - If the message ends with the bot nick, respond.
    - If the message contains a question mark and mentions the bot, respond.
    - Short direct messages (<=6 words) mentioning the bot are allowed.
    - Do not respond if the mention appears inside a URL, code fence, or quoted text.
    - Do not respond if multiple distinct nick-like tokens are present and bot is not first.
    """
    s = line.strip()
    lower = s.lower()
    nick = bot_nick.lower()

    # Avoid quoted lines or code blocks
    if s.startswith('>') or '```' in s:
        return False

    # Avoid URLs containing the nick
    if re.search(r'https?://[^\s]*' + re.escape(nick), lower):
        return False

    # Avoid predicative/adjectival uses like "my code is glitchy" or "it's glitchy"
    # where the nick is being used to describe something rather than addressing the bot.
    if re.search(rf'\b(?:is|are|was|were|be|being|looks|feels|seems)\b\s+{re.escape(nick)}\b', lower):
        return False

    # Possessive forms: "glitchy's output" or "glitchy’s output"
    if re.search(rf"\b{re.escape(nick)}(?:'s|’s)\b", lower):
        return False

    # Phrases that refer to saying/using the word rather than addressing the bot,
    # e.g. "if you say glitchy now", "when we call glitchy", "they'll mention glitchy".
    if re.search(rf"\b(?:if|when|you|we|they|people|someone)\b(?:\W+\w+){{0,8}}\W+\b(?:say|call|mention|use|type|write|spell|invoke)\b\W+{re.escape(nick)}", lower):
        return False

    # Vocative at start: "glitchy: do this"
    if re.match(rf'^\s*{re.escape(bot_nick)}[,:>\s]', s, re.IGNORECASE):
        return True

    # Nick at end: "can you help glitchy" or "thanks glitchy"
    if re.search(rf'{re.escape(bot_nick)}\s*\W*$', s, re.IGNORECASE):
        return True

    # If it's a clear question and mentions the nick anywhere, respond
    if '?' in s and re.search(rf'\b{re.escape(bot_nick)}\b', s, re.IGNORECASE):
        return True

    # Count words and nick-like tokens
    words = s.split()
    if len(words) <= 6 and re.search(rf'\b{re.escape(bot_nick)}\b', s, re.IGNORECASE):
        return True

    # If multiple capitalized tokens or comma-separated names exist and bot isn't first, don't respond
    # Simple heuristic for lists of nicks: look for commas or ' and '
    if re.search(r'[,@]|\band\b', s) and re.search(rf'\b{re.escape(bot_nick)}\b', s, re.IGNORECASE):
        # If bot nick not near start, assume it's being referenced among others
        if not re.match(rf'^\s*{re.escape(bot_nick)}', s, re.IGNORECASE):
            return False

    # Default: be permissive and respond
    return True


@plugin.event('PRIVMSG')
@plugin.rule('.*')
@plugin.priority('high')
def handle(bot, trigger):
    # Detect whether this is a private message (PM) or a channel message
    is_pm = _is_pm(trigger)

    # If PM: allow private conversations unless the user is banned
    if is_pm:
        # Gather banned nicks from config and any runtime memory key
        cfg_banned = {n.lower() for n in getattr(bot.config.grok, 'banned_nicks', [])}
        mem_banned = set()
        try:
            mem_banned = {n.lower() for n in bot.memory.get('grok_banned', [])}
        except Exception:
            mem_banned = set()
        if trigger.nick.lower() in cfg_banned or trigger.nick.lower() in mem_banned:
            try:
                bot.reply('You are banned from using Grok.')
            except Exception:
                pass
            return

    # Ignore messages originating from other automated bots/scripts
    try:
        cfg_ignored = {n.lower() for n in getattr(bot.config.grok, 'ignored_nicks', [])}
    except Exception:
        cfg_ignored = set()
    if trigger.nick.lower() in cfg_ignored:
        try:
            _log(bot).info('Ignoring message from configured ignored nick: %s', trigger.nick)
        except Exception:
            pass
        return

    # Admin ignore list: ignored from Grok in PMs and channels.
    # Owner/admins are allowed through so they can manage the ignore list (and avoid self-lockout).
    try:
        if trigger.nick.lower() in bot.memory.get('grok_admin_ignored', set()):
            if not _is_admin(bot, trigger):
                return
    except Exception:
        pass

    # Also ignore messages that originate from this bot's own nick (runtime or configured)
    try:
        cfg_core_nick = getattr(bot.config.core, 'nick', None)
    except Exception:
        cfg_core_nick = None
    own_nicks = {bot.nick.lower()}
    if cfg_core_nick:
        own_nicks.add(cfg_core_nick.lower())
    try:
        if trigger.nick.lower() in own_nicks:
            try:
                _log(bot).info('Ignoring message from self nick: %s', trigger.nick)
            except Exception:
                pass
            return
    except Exception:
        # If anything goes wrong determining self-nick, continue normally
        pass

    # Block-list channels from config; no logging, no replies (only applies to channels)
    blocked = {c.lower() for c in bot.config.grok.blocked_channels}
    if (not is_pm) and (trigger.sender.lower() in blocked):
        return

    line = trigger.group(0).strip()

    # Ignore messages that look like bot commands for other scripts (or unknown commands).
    # This prevents Grok from replying in PMs/channels when a user is trying to run a
    # different script's command.
    #
    # Exception: bot admins/owners are allowed through (so they can test/admin), and
    # we allow-list Grok's own commands so non-admins can still use them.
    try:
        bot_nick = bot.nick
        allowlisted_commands = {'grokreset', 'testemote'}
        command_prefixes = ('!', '$', '.', ':', '/', '\\')

        candidate = line.lstrip()
        # If user addresses the bot then runs a command, e.g. "glitchy: $help"
        m_addr = re.match(rf'^\s*{re.escape(bot_nick)}\s*[:,>]\s*(.+)$', line, re.IGNORECASE)
        if m_addr:
            candidate = (m_addr.group(1) or '').lstrip()

        if candidate and candidate.startswith(command_prefixes):
            # Parse command name after the prefix, e.g. "$grokreset" -> "grokreset"
            cmd = (candidate[1:].split(None, 1)[0] if len(candidate) > 1 else '').strip().lower()
            if (not _is_admin(bot, trigger)) and (cmd not in allowlisted_commands):
                return
    except Exception:
        # If anything goes wrong parsing prefixes, don't break normal chat handling
        pass

    # Ignore other bot command invocations that target a nick, e.g. "$mug glitchy".
    # This prevents Grok from replying when another bot runs a command that mentions
    # a nick. Adjust or extend the pattern if you have other command names to ignore.
    try:
        if re.match(r'^\$mug\b', line, re.IGNORECASE):
            try:
                _log(bot).info('Ignoring $mug invocation: %s', line)
            except Exception:
                pass
            return
    except Exception:
        pass
    bot_nick = bot.nick

    # --- Filter genuine IRC noise (but keep ACTION/emote lines!) ---
    noise_patterns = [
        r'^MODE ',                         # mode changes
        r'has (joined|quit|left|parted)',  # join/quit spam
    ]
    if any(re.search(p, line, re.IGNORECASE) for p in noise_patterns):
        return

    # --- Handle CTCP ACTION (/me) or simple emote lines locally ---
    # CTCP ACTION messages are wrapped like: \x01ACTION pets glitchy\x01
    # Also accept conventional '/me pets glitchy' text.
    _log(bot).debug('Checking emote for line: %r, bot_nick: %s', line, bot.nick)
    try:
        action_text = None
        m = re.match(r'^\x01ACTION\s+(.+?)\x01$', line)
        if m:
            action_text = m.group(1)
        elif line.startswith('/me '):
            action_text = line[4:]

        if action_text:
            # If the action targets this bot (mentions bot nick), respond locally
            bot_nick = bot.nick
            if re.search(rf'\b{re.escape(bot_nick)}\b', action_text, re.IGNORECASE) or re.search(rf'\b{re.escape(bot_nick)}\b', line, re.IGNORECASE):
                verb = action_text.split()[0].lower()
                short = _get_emote_reply(verb, trigger.nick, bot.memory)
                if short:
                    # Use an ACTION reply so it's an emote
                    _log(bot).debug('CTCP emote: verb=%s, reply=%s', verb, short)
                    try:
                        bot.action(f"{short} {trigger.nick}", trigger.sender)
                    except Exception:
                        pass
                    return
                else:
                    # Generic friendly emote
                    _log(bot).debug('CTCP emote: generic acknowledge')
                    try:
                        bot.action(f"acknowledges {trigger.nick} with a smile 😊", trigger.sender)
                    except Exception:
                        pass
                    return
    except Exception:
        # If emote handling fails, continue to normal flow
        pass
    # Secondary emote detection: sometimes clients print emotes without CTCP
    # e.g., "* <nick> pets glitchy" or plain text "@End3r pets glitchy".
    try:
        bot_nick = bot.nick
        # Normalize: strip leading '* ' often used by clients to show emotes
        stripped = re.sub(r'^\*\s*', '', line)
        # Match only *directed* actions at the bot, e.g. "pets glitchy".
        # Avoid false positives like "glitchy ... talking smack ..." where the
        # nick is present but the verb isn't targeting the bot.
        verbs = r'(pet|pets|pat|pats|hug|hugs|poke|pokes|kiss|kisses|stroke|strokes|smack|smacks|slap|slaps|bonk|bonks|kick|kicks|punch|punches|boop|boops|nuzzle|nuzzles|snuggle|snuggles|cuddle|cuddles|highfive|highfives|twirl|twirls|wave|waves|wink|winks|dance|dances)'
        target_re = re.compile(
            rf'\b{verbs}\b(?:\W+\w+){{0,2}}\W+@?{re.escape(bot_nick)}(?:\W|$)',
            re.IGNORECASE,
        )
        m2 = target_re.search(stripped)
        if m2:
            verb = m2.group(1).lower()
            short = _get_emote_reply(verb, trigger.nick, bot.memory) or 'acknowledges with a smile 😊'
            _log(bot).debug('Secondary emote: verb=%s, reply=%s, stripped=%r', verb, short, stripped)
            try:
                bot.action(f"{short} {trigger.nick}", trigger.sender)
            except Exception:
                pass
            return
    except Exception:
        pass

    # --- Detect whether the bot is explicitly mentioned ---
    # In PMs we treat the user message as an implicit mention (they're talking to the bot)
    if is_pm:
        mentioned = True
    else:
        # Match nick boundaries more robustly than \b to allow non-word chars in nicks
        mentioned = bool(
            re.search(
                rf'(^|[^A-Za-z0-9_]){re.escape(bot_nick)}([^A-Za-z0-9_]|$)',
                line,
                re.IGNORECASE,
            )
        )

    # Intent detection: if configured to use heuristics, perform a lightweight
    # acceptance test to avoid responding to incidental mentions.
    if (not is_pm) and mentioned and getattr(bot.config.grok, 'intent_check', 'heuristic') == 'heuristic':
        try:
            if not _heuristic_intent_check(bot, trigger, line, bot_nick):
                return
        except Exception:
            # on error, be permissive and continue
            pass

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

    # Initialize per-conversation history and append this message (thread-safe)
    # Use distinct keys/locks for PMs so each user's private convo is isolated
    if is_pm:
        lock_name = f"PM:{trigger.nick.lower()}"
        chan_lock = _get_channel_lock(bot, lock_name)
        per_conv_key = ("PM", trigger.nick.lower())
    else:
        chan_lock = _get_channel_lock(bot, trigger.sender)
        per_conv_key = (trigger.sender, trigger.nick)
    with chan_lock:
        history = bot.memory['grok_history'].setdefault(
            per_conv_key,
            deque(maxlen=MAX_HISTORY_ENTRIES),
        )
        if text_for_history:
            # If this line did not address the bot, avoid storing noisy lines
            # (URLs, single tiny tokens, or pure punctuation) which often pollute
            # future replies for simple user prompts.
            skip = False
            if not mentioned:
                if re.search(r'https?://|\S+\.(com|net|org|io|gg)\b', text_for_history, re.IGNORECASE):
                    skip = True
                if len(text_for_history.split()) <= 1 and len(text_for_history) <= 3:
                    skip = True
                if re.match(r'^[^\w\s]+$', text_for_history):
                    skip = True
            if not skip:
                # Coalesce consecutive messages from the same nick to reduce noise
                if history and history[-1].startswith(f"{trigger.nick}:"):
                    try:
                        _, last_text = history.pop().split(": ", 1)
                    except Exception:
                        last_text = ''
                    new = f"{trigger.nick}: {last_text} / {text_for_history}" if last_text else f"{trigger.nick}: {text_for_history}"
                    if len(new) > 400:
                        new = new[:390] + " […]"
                    history.append(new)
                else:
                    history.append(f"{trigger.nick}: {text_for_history}")

    # If they didn't mention the bot, don't wake it up — just keep the context
    if not mentioned:
        return

    # This is the text we treat as the "current user message" to Grok
    user_message = text_for_history

    # PM-only admin commands ($join/$part/$ignore/$unignore)
    try:
        if _handle_admin_pm_commands(bot, trigger, user_message):
            return
    except Exception:
        _log(bot).exception('Admin PM command handler failed')
        return

    # --- Silently detect and persist per-user timezone / format preferences ---
    # This runs as a side-effect; the model's normal reply handles acknowledgement.
    try:
        _pref_tz = None
        _pref_fmt = None
        _mtz = _TZ_SET_RE.search(user_message)
        if _mtz:
            _abbr = _mtz.group(1).upper()
            _iana = _TZ_ABBR_MAP.get(_abbr)
            if _iana:
                _pref_tz = _iana
        _mfmt = _FMT_SET_RE.search(user_message)
        if _mfmt:
            _raw = _mfmt.group(1).lower().replace(' ', '').replace('-', '')
            _pref_fmt = '12' if _raw.startswith('12') else '24'
        if _pref_tz or _pref_fmt:
            _db_set_user_pref(bot, trigger.nick, tz=_pref_tz, fmt=_pref_fmt)
            _log(bot).info('Saved pref for %s: tz=%s fmt=%s', trigger.nick, _pref_tz, _pref_fmt)
    except Exception:
        pass

    # Detect review trigger early so cooldowns can reference it
    review_re = re.compile(r"\b(thoughts?|opinion|what do you think|summarize|give (me )?(your )?(take|opinion)|opine)\b", re.IGNORECASE)
    review_mode = bool(review_re.search(user_message)) or (user_message.strip() == '^^')

    # Ignore empty messages after cleaning
    if not user_message:
        return

    # Ignore bot commands like ".help", "/whatever", "!foo"
    if re.match(r'^[.!/]', user_message):
        return

    # Detect time-only queries early so we can bypass rate-limiting for them
    time_mode = bool(_TIME_INTENT_RE.search(user_message))

    # --- Rate limit: 4 seconds per channel (thread-safe) ---
    # Time/date queries are exempt: repeated asks should always get the current time.
    now = time.time()
    if not time_mode:
        with chan_lock:
            last = bot.memory['grok_last'].get(trigger.sender, 0)
            if now - last < 4:
                return
            bot.memory['grok_last'][trigger.sender] = now
    else:
        # Still update the timestamp so non-time queries aren't blocked by a burst of time asks
        with chan_lock:
            bot.memory['grok_last'][trigger.sender] = now

    # Review-mode cooldown (longer): once per 30s per channel
    if review_mode:
        review_last = bot.memory.setdefault('grok_review_last', {})
        last_review = review_last.get(trigger.sender, 0)
        if now - last_review < 30:
            # ignore rapid repeated review requests
            return
        review_last[trigger.sender] = now

    # --- Build Grok conversation messages from history ---
    # Inject current date/time so Grok is never "stuck in the past"
    now_str = datetime.datetime.now(datetime.timezone.utc).strftime('%A, %B %d, %Y at %H:%M UTC')

    messages = [
        {"role": "system", "content": bot.config.grok.system_prompt},
        {
            "role": "system",
            "content": (
                f"Current date/time: {now_str}. "
                f"Your IRC nick is '{bot_nick}'. You are replying to {trigger.nick}. "
                f"If the user asks about news, current events, or anything time-sensitive, "
                f"search the web and give a substantive answer with real details. "
                f"All responses must be single-line (no newlines — this is IRC)."
            ),
        },
    ]

    # Decide whether this mention is a simple user prompt (default behavior)
    # or a channel-wide review/opinion request. If the user asks for "thoughts",
    # "opinion", "what do you think", "summarize", etc., we switch to review mode
    # and gather recent messages from the whole channel (subject to filters/budget).

    relevant_turns = []

    if not review_mode:
        # Prefer DB-backed per-user history (persists across restarts). Fall back to
        # in-memory history if DB empty.
        db_entries = _db_get_recent(bot, trigger.nick, limit=20)
        if db_entries:
            for role, text in db_entries:
                nick = bot_nick if role == 'assistant' else trigger.nick
                relevant_turns.append((nick, text))
        else:
            # Per-(channel,nick) history only (default): keep turns between this user and the bot
            # Snapshot history under lock to avoid races while we build messages
            with chan_lock:
                history_snapshot = list(history)

            for entry in history_snapshot:
                # Each entry is "nick: text"
                try:
                    nick, text = entry.split(": ", 1)
                except ValueError:
                    continue
                if nick not in (trigger.nick, bot_nick):
                    continue
                relevant_turns.append((nick, text))
    else:
        # Channel review mode: collect recent lines from all per-(channel,nick) histories
        # and any channel-only keys (backwards-compat). We'll merge them into a
        # chronological list and then apply a simple char budget.
        channel_entries = []  # (timestamp_approx, nick, text)
        # We don't store timestamps per-entry, so we treat deque order as chronological.
        # For PM review requests, only gather the PM-specific history
        if is_pm:
            with chan_lock:
                dq = bot.memory.get('grok_history', {}).get(per_conv_key, None)
                if dq:
                    for item in list(dq):
                        try:
                            nick, text = item.split(": ", 1)
                        except Exception:
                            continue
                        channel_entries.append((nick, text))
        else:
            with chan_lock:
                for k, dq in bot.memory.get('grok_history', {}).items():
                    try:
                        if isinstance(k, tuple) and k[0] == trigger.sender:
                            for item in list(dq):
                                try:
                                    nick, text = item.split(": ", 1)
                                except Exception:
                                    continue
                                channel_entries.append((nick, text))
                        elif k == trigger.sender:
                            for item in list(dq):
                                try:
                                    nick, text = item.split(": ", 1)
                                except Exception:
                                    continue
                                channel_entries.append((nick, text))
                    except Exception:
                        continue

        # Filter and keep most recent entries (already chronological by collection order)
        # Apply same noise filters as when storing: skip URLs / tiny tokens / punctuation
        filtered = []
        for nick, text in channel_entries:
            t = text.strip()
            if not t:
                continue
            if re.search(r'https?://|\S+\.(com|net|org|io|gg)\b', t, re.IGNORECASE):
                continue
            if len(t.split()) <= 1 and len(t) <= 3:
                continue
            if re.match(r'^[^\w\s]+$', t):
                continue
            filtered.append((nick, t))

        # Build a chronological list but enforce a character budget
        char_budget = REVIEW_CHAR_BUDGET
        collected = []
        total_chars = 0
        # iterate from the end (most recent) backwards to collect newest first
        for nick, text in reversed(filtered):
            l = len(text) + len(nick) + 3
            if total_chars + l > char_budget and collected:
                break
            collected.append((nick, text))
            total_chars += l

        # collected is newest-first; reverse to chronological
        collected.reverse()
        relevant_turns = collected

    if not review_mode:
        # --- Inject recent channel-wide lines as background context ---
        # Collect lines from ALL nicks in this channel (not just this user/bot pair)
        # so Grok can answer questions like "what did KnownSyntax say?" or
        # "what beer did End3r have?".
        if not is_pm:
            try:
                channel_bg = []
                with chan_lock:
                    for k, dq in bot.memory.get('grok_history', {}).items():
                        try:
                            if isinstance(k, tuple) and k[0] == trigger.sender:
                                for item in list(dq):
                                    try:
                                        n, t = item.split(": ", 1)
                                        channel_bg.append((n, t))
                                    except Exception:
                                        continue
                        except Exception:
                            continue

                # Deduplicate while preserving order (multiple per-user deques overlap)
                seen_bg = set()
                unique_bg = []
                for n, t in channel_bg:
                    key = (n.lower(), t)
                    if key not in seen_bg:
                        seen_bg.add(key)
                        unique_bg.append((n, t))

                # Keep the most recent lines within a character budget
                BG_CHAR_BUDGET = 1500
                BG_MAX_LINES = 40
                bg_collected = []
                bg_chars = 0
                for n, t in reversed(unique_bg):
                    l = len(n) + len(t) + 3
                    if bg_chars + l > BG_CHAR_BUDGET and bg_collected:
                        break
                    if len(bg_collected) >= BG_MAX_LINES:
                        break
                    bg_collected.append((n, t))
                    bg_chars += l
                bg_collected.reverse()  # back to chronological order

                if bg_collected:
                    bg_lines = [f"{n}: {t}" for n, t in bg_collected]
                    messages.append({
                        "role": "system",
                        "content": (
                            "Recent channel conversation log (each line is 'nick: message'). "
                            "When asked who said something or what a specific user said, "
                            "always answer accurately based on this log — name the correct nick. "
                            "Do not invent or attribute statements to yourself or the wrong person.\n\n"
                            + "\n".join(bg_lines)
                        ),
                    })
            except Exception:
                pass

        # Keep only the last N turns for this user/bot pair
        for nick, text in relevant_turns[-MAX_HISTORY_PER_USER:]:
            role = "assistant" if nick == bot_nick else "user"
            messages.append({"role": role, "content": text})

        # Add the current user message at the end
        messages.append({"role": "user", "content": user_message})
        # Persist this user turn to DB for future cross-channel context
        try:
            _db_add_turn(bot, trigger.nick, 'user', user_message, 'PM' if is_pm else trigger.sender)
        except Exception:
            pass
    else:
        # Review mode: give Grok an explicit review instruction and a compact background
        review_sys = (
            "You are Grok, a conversational, human-like assistant. For review requests, "
            "produce a short, opinionated response in 2-3 sentences, mention one highlight, "
            "and give one concise suggestion. Be casual and friendly. Keep it brief. "
            "Never use newlines or line breaks - keep everything in a single line."
        )
        messages.append({"role": "system", "content": review_sys})

        # Build a compact background text (chronological)
        bg_lines = []
        for nick, text in relevant_turns[-REVIEW_MAX_ENTRIES:]:
            bg_lines.append(f"{nick}: {text}")
        background = "\n".join(bg_lines)

        combined = (
            "Background conversation (most recent last):\n" + background + "\n\n"
            + "User question: " + user_message + "\n\n"
            + "Instruction: Provide a brief, human-like opinion (2-3 sentences), one highlight, and one short suggestion."
        )
        messages.append({"role": "user", "content": combined})

    # --- Call x.ai API asynchronously to avoid blocking the handler ---
    # Enqueue work for the API worker pool; fail gracefully if queue is full
    try:
        # Detect if this query needs live web search
        search_mode = bool(_SEARCH_INTENT_RE.search(user_message))
        if search_mode:
            _log(bot).info('Search intent detected for query from %s: %s', trigger.nick, user_message[:80])
            # Tell the model to stop after delivering the factual result —
            # no trailing commentary, quips, or emoji taglines.
            messages.append({
                "role": "system",
                "content": (
                    "You are performing a web search. Output ONLY the factual search result. "
                    "Do NOT add any commentary, follow-up remarks, emoji taglines, or reactions "
                    "after the result. End your response immediately after the last factual sentence."
                ),
            })
        if time_mode:
            _log(bot).info('Time/date query detected for %s', trigger.nick)
            # Load this user's saved preferences (tz + format), defaulting to UTC / 24hr.
            _uprefs = _db_get_user_pref(bot, trigger.nick)
            _pref_iana = (_uprefs.get('tz_iana') or 'UTC') if _uprefs else 'UTC'
            _pref_fmt  = (_uprefs.get('time_fmt') or '24')  if _uprefs else '24'
            _now_utc = datetime.datetime.now(datetime.timezone.utc)
            try:
                import zoneinfo
                _tz_obj = zoneinfo.ZoneInfo(_pref_iana)
            except Exception:
                _tz_obj = datetime.timezone.utc
            _dt_local = _now_utc.astimezone(_tz_obj)
            try:
                _tz_label = _dt_local.strftime('%Z')
            except Exception:
                _tz_label = _pref_iana
            if _pref_fmt == '12':
                _time_str = _dt_local.strftime('%-I:%M %p')  # e.g. 8:42 PM
                _full_str = _dt_local.strftime('%A, %B %-d, %Y %-I:%M %p') + f' {_tz_label}'
            else:
                _time_str = _dt_local.strftime('%H:%M')       # e.g. 20:42
                _full_str = _dt_local.strftime('%A, %B %-d, %Y %H:%M') + f' {_tz_label}'
            messages.append({
                "role": "system",
                "content": (
                    f"The current date and time RIGHT NOW is: {_full_str}. "
                    f"The user's preferred timezone is {_tz_label} and format is {'12-hour' if _pref_fmt == '12' else '24-hour'}. "
                    "State ONLY the requested date/time fact using the user's preferred timezone and format. "
                    "Do NOT add any commentary, emoji, or follow-up after giving it."
                ),
            })
        API_TASK_QUEUE.put_nowait((bot, trigger, messages, review_mode, is_pm, bot_nick, chan_lock, search_mode))
    except queue.Full:
        try:
            _log(bot).warning('API task queue full; rejecting request from %s', trigger.nick)
            bot.say('Grok is currently busy; please try again in a moment.', trigger.sender)
        except Exception:
            pass


@plugin.command('testemote')
def testemote(bot, trigger):
    bot.say('Emote plugin loaded, bot nick: ' + bot.nick)


@plugin.command('grokreset')
def grokreset(bot, trigger):
    """Reset Grok history.

    - In PMs: any user may run this to clear their own private history.
    - In channels: by default clears only the calling user's history.
      Use `$grokreset channel` (or `$grokreset all`) to clear the whole channel,
      which requires bot admin/owner or channel operator.
    """
    is_pm = _is_pm(trigger)
    try:
        arg = (trigger.group(2) or '').strip().lower()
    except Exception:
        arg = ''

    # PM: always treat as self-reset
    if is_pm:
        key = ('PM', trigger.nick.lower())
        try:
            gh = bot.memory.get('grok_history', {})
            if key in gh:
                del gh[key]
        except Exception:
            pass
        try:
            _db_clear_user(bot, trigger.nick)
        except Exception:
            pass
        try:
            bot.reply('Your Grok history has been reset.')
        except Exception:
            pass
        return

    # Channel-wide reset: restricted
    if arg in {'channel', 'chan', 'all', '*'}:
        if not (_is_admin(bot, trigger) or _is_channel_op(bot, trigger)):
            try:
                bot.say(
                    'Only a bot admin/owner or a channel operator may reset Grok history for the whole channel. '
                    'Use $grokreset (or $grokreset me) to reset only your history.',
                    trigger.sender,
                )
            except Exception:
                pass
            return

        # Remove per-(channel,nick) entries and any channel-only legacy keys
        keys = list(bot.memory.get('grok_history', {}).keys())
        for k in keys:
            try:
                if (isinstance(k, tuple) and k[0] == trigger.sender) or (k == trigger.sender):
                    del bot.memory['grok_history'][k]
            except Exception:
                continue
        try:
            bot.say('Grok history reset for this channel.', trigger.sender)
        except Exception:
            pass
        return

    # Default: self reset (works for non-ops)
    # Clear per-(channel,nick) in-memory context
    try:
        keys = list(bot.memory.get('grok_history', {}).keys())
        for k in keys:
            try:
                if isinstance(k, tuple) and k[0] == trigger.sender and str(k[1]).lower() == trigger.nick.lower():
                    del bot.memory['grok_history'][k]
            except Exception:
                continue
    except Exception:
        pass
    # Clear DB-backed per-user history too, otherwise the bot may still use stored context
    try:
        _db_clear_user(bot, trigger.nick)
    except Exception:
        pass

    try:
        bot.reply('Your Grok history has been reset.')
    except Exception:
        pass
