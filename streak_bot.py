import asyncio
import os
import re
import time
import urllib.parse
import urllib.request
from pathlib import Path

# Configuración básica: actualiza estos valores o exporta variables de entorno.
TWITCH_USER = os.getenv("TWITCH_USER", "NoPhantasm")
TWITCH_OAUTH_TOKEN = os.getenv("TWITCH_OAUTH_TOKEN", "oauth:2p52g2gvdclhzky31mny4a0nhpmfy0")
TWITCH_CHANNEL = os.getenv("TWITCH_CHANNEL", "Phantasm__").lstrip("#")
RIOT_API_KEY = os.getenv("RIOT_API_KEY", "RGAPI-fceb4641-b051-4bef-8e5c-17d1710dbbfa")
SUMMONER_NAME_RAW = os.getenv("SUMMONER_NAME", "Phanta#107")
SUMMONER_TAG = os.getenv("SUMMONER_TAG", "#107")
if "#" in SUMMONER_NAME_RAW:
    SUMMONER_NAME, SUMMONER_TAG = SUMMONER_NAME_RAW.split("#", 1)
else:
    SUMMONER_NAME = SUMMONER_NAME_RAW
REGION = os.getenv("REGION", "EUW1")
SLEEP_IN_GAME = int(os.getenv("SLEEP_IN_GAME", "10"))
SLEEP_OUT_GAME = int(os.getenv("SLEEP_OUT_GAME", "5"))
GLOBAL_CD_SECONDS = 2
PENDING_SEND_TIMEOUT = int(os.getenv("PENDING_SEND_TIMEOUT", "10"))
STREAK_FILE = Path(__file__).with_name("streak.messages.txt")
LOUIS_BOT_NAMES = {"louisgamedev"}
RANKED_QUEUE_IDS = {420, 440}
QUEUE_TYPE_TO_ID = {
    "RANKED_SOLO_5x5": 420,
    "RANKED_FLEX_SR": 440,
}

PLATFORM_TO_ROUTE = {
    "na1": "americas",
    "la1": "americas",
    "la2": "americas",
    "br1": "americas",
    "oc1": "americas",
    "kr": "asia",
    "jp1": "asia",
    "euw1": "europe",
    "eun1": "europe",
    "ru": "europe",
    "tr1": "europe",
}

CHAT_RE = re.compile(r"^:([^!]+)!.* PRIVMSG #[^ ]+ :(.+)$")
LOUIS_RESULT_RE = re.compile(r"\b(Win|Loss|Lose|Lost)\b", re.IGNORECASE)


class BotState:
    def __init__(self):
        self.current_streak_type = None
        self.current_streak_count = 0
        self.last_match_id = None
        self.sent_match_ids = set()
        self.last_streak_command_time = 0.0
        self.chat_send_queue = asyncio.Queue()
        self.pending_match_result = None
        self.pending_louis_event = None
        self.last_ranked_lp = {}
        self.last_ranked_queue = None

    def format_active_streak(self):
        if not self.current_streak_type or self.current_streak_count == 0:
            return "No streak active."
        suffix = "W" if self.current_streak_type == "win" else "L"
        return f"Current streak: {self.current_streak_count}{suffix}"


def load_streak_templates(path: Path):
    if not path.exists():
        raise FileNotFoundError(f"No se encontró {path}")

    templates = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        templates[key.strip()] = value.strip()
    return templates


def choose_template(templates, category, streak):
    exact_key = f"{category}.{streak}"
    if exact_key in templates:
        return templates[exact_key]

    if streak >= 20:
        fallback_key = f"{category}.20+"
        if fallback_key in templates:
            return templates[fallback_key].replace("{streak}", str(streak))

    fallback_key = f"{category}.1"
    if fallback_key in templates:
        return templates[fallback_key].replace("{streak}", str(streak))

    return None


def get_routing_region(platform: str):
    platform = platform.lower()
    if platform not in PLATFORM_TO_ROUTE:
        raise ValueError(f"Plataforma Riot desconocida: {platform}")
    return PLATFORM_TO_ROUTE[platform]


def http_get_json(url: str, headers=None):
    request = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(request, timeout=15) as response:
        content = response.read().decode("utf-8")
        return http_parse_json(content)


def http_parse_json(text: str):
    import json

    return json.loads(text)


async def fetch_json(url: str, headers=None):
    return await asyncio.to_thread(http_get_json, url, headers)


async def send_irc_line(writer, line: str):
    writer.write(f"{line}\r\n".encode("utf-8"))
    await writer.drain()


async def send_chat_message(writer, channel: str, message: str):
    await send_irc_line(writer, f"PRIVMSG #{channel} :{message}")


async def handle_privmsg(state: BotState, nick: str, message: str):
    lower_nick = nick.lower()
    lower_msg = message.strip().lower()

    if lower_msg.startswith("!streak"):
        now = time.monotonic()
        if now - state.last_streak_command_time < GLOBAL_CD_SECONDS:
            return
        state.last_streak_command_time = now
        await state.chat_send_queue.put(state.format_active_streak())
        return

    if lower_nick in LOUIS_BOT_NAMES:
        match = LOUIS_RESULT_RE.search(message)
        if not match:
            return
        result = match.group(1).lower()
        if result in {"lose", "lost"}:
            result = "loss"
        elif result == "win":
            result = "win"
        else:
            return

        state.pending_louis_event = {"result": result, "seen_at": time.monotonic()}
        await try_send_pending_streak(state)


async def try_send_pending_streak(state: BotState, force_send=False):
    if not state.pending_match_result:
        return

    if state.pending_louis_event and state.pending_louis_event["result"] != state.pending_match_result["result"]:
        # Esperamos que coincidan, pero si no, respetamos el resultado detectado.
        pass

    timed_out = time.monotonic() >= state.pending_match_result.get("send_after", 0)
    if state.pending_louis_event or force_send or timed_out:
        for line in state.pending_match_result["messages"]:
            await state.chat_send_queue.put(line)
        state.sent_match_ids.add(state.pending_match_result["match_id"])
        state.pending_match_result = None
        state.pending_louis_event = None


def build_streak_messages(templates, previous_type, previous_count, result):
    messages = []
    broken = None
    if previous_type and previous_type != result and previous_count >= 2:
        broken_key = f"streak_break.{previous_type}_broken"
        broken_template = templates.get(broken_key)
        if broken_template:
            broken = broken_template.replace("{streak}", str(previous_count))
        else:
            broken = f"{previous_type.title()} streak of {previous_count} broken!"

    if previous_type != result:
        new_count = 1
    else:
        new_count = previous_count + 1

    streak_template = choose_template(templates, f"{result}_streak", new_count)
    if streak_template is None:
        default_text = f"{new_count} {'Win' if result == 'win' else 'Loss'} streak"
        streak_template = f"🔥 {default_text}" if new_count >= 2 else default_text

    if broken:
        messages.append(broken)
    messages.append(streak_template)
    return new_count, messages


async def riot_poll_loop(state: BotState, templates):
    platform = REGION.lower()
    routing = get_routing_region(platform)
    headers = {"X-Riot-Token": RIOT_API_KEY}

    summoner = await fetch_summoner(platform, headers)
    if summoner is None:
        raise RuntimeError("No se pudo cargar el invocador de Riot. Verifica REGION, SUMMONER_NAME y RIOT_API_KEY.")

    summoner_id = summoner["id"]
    puuid = summoner["puuid"]
    await load_ranked_lp(state, platform, summoner_id, headers)
    await compute_initial_streak(state, platform, routing, summoner_id, puuid, headers)
    last_seen_in_game = False

    while True:
        active_game = await fetch_active_game(platform, summoner_id, headers)
        if active_game is not None:
            last_seen_in_game = True
            await try_send_pending_streak(state)
            await asyncio.sleep(SLEEP_IN_GAME)
            continue

        await try_send_pending_streak(state)
        if last_seen_in_game:
            last_seen_in_game = False
            match_id = await fetch_last_match_id(routing, puuid, headers)
            if match_id and match_id != state.last_match_id and match_id not in state.sent_match_ids:
                match_info = await fetch_match_info(routing, match_id, puuid, headers)
                if match_info is None:
                    state.last_match_id = match_id
                    await asyncio.sleep(SLEEP_OUT_GAME)
                    continue

                queue_id = match_info["queueId"]
                result = match_info["result"]
                has_lp_change = await match_has_lp_change(state, platform, summoner_id, headers, queue_id)
                state.last_match_id = match_id
                if not has_lp_change:
                    await asyncio.sleep(SLEEP_OUT_GAME)
                    continue

                previous_type = state.current_streak_type
                previous_count = state.current_streak_count
                state.current_streak_type = result
                state.current_streak_count = previous_count + 1 if previous_type == result else 1
                messages = build_streak_messages(
                    templates,
                    previous_type,
                    previous_count,
                    result,
                )[1]
                state.pending_match_result = {
                    "match_id": match_id,
                    "result": result,
                    "messages": messages,
                    "force_send": False,
                    "send_after": time.monotonic() + PENDING_SEND_TIMEOUT,
                }
                await try_send_pending_streak(state)
        await asyncio.sleep(SLEEP_OUT_GAME)


async def fetch_summoner(platform, headers):
    encoded_name = urllib.parse.quote(SUMMONER_NAME)
    url = f"https://{platform}.api.riotgames.com/lol/summoner/v4/summoners/by-name/{encoded_name}"
    try:
        return await fetch_json(url, headers)
    except Exception:
        return None


async def fetch_active_game(platform, summoner_id, headers):
    url = f"https://{platform}.api.riotgames.com/lol/spectator/v4/active-games/by-summoner/{summoner_id}"
    try:
        return await fetch_json(url, headers)
    except Exception:
        return None


async def fetch_league_entries(platform, summoner_id, headers):
    url = f"https://{platform}.api.riotgames.com/lol/league/v4/entries/by-summoner/{summoner_id}"
    try:
        return await fetch_json(url, headers)
    except Exception:
        return []


def league_entries_to_lp(entries):
    lp_by_queue = {}
    for entry in entries:
        queue_type = entry.get("queueType")
        queue_id = QUEUE_TYPE_TO_ID.get(queue_type)
        if queue_id and isinstance(entry.get("leaguePoints"), int):
            lp_by_queue[queue_id] = entry["leaguePoints"]
    return lp_by_queue


async def load_ranked_lp(state: BotState, platform, summoner_id, headers):
    entries = await fetch_league_entries(platform, summoner_id, headers)
    state.last_ranked_lp = league_entries_to_lp(entries)


async def fetch_match_ids(routing, puuid, headers, count=20):
    url = f"https://{routing}.api.riotgames.com/lol/match/v5/matches/by-puuid/{puuid}/ids?start=0&count={count}"
    try:
        ids = await fetch_json(url, headers)
        if isinstance(ids, list):
            return ids
        return []
    except Exception:
        return []


async def fetch_last_match_id(routing, puuid, headers):
    ids = await fetch_match_ids(routing, puuid, headers, count=2)
    return ids[0] if ids else None


def parse_match_info(data, puuid):
    info = data.get("info", {})
    queue_id = info.get("queueId")
    if queue_id not in RANKED_QUEUE_IDS:
        return None

    if info.get("gameDuration", 0) < 300:
        return None

    participants = info.get("participants", [])
    for participant in participants:
        if participant.get("puuid") == puuid:
            return {
                "queueId": queue_id,
                "result": "win" if participant.get("win") else "loss",
            }
    return None


async def fetch_match_info(routing, match_id, puuid, headers):
    url = f"https://{routing}.api.riotgames.com/lol/match/v5/matches/{match_id}"
    try:
        data = await fetch_json(url, headers)
        return parse_match_info(data, puuid)
    except Exception:
        return None


async def fetch_current_lp(state: BotState, platform, summoner_id, headers):
    await load_ranked_lp(state, platform, summoner_id, headers)
    return state.last_ranked_lp.copy()


async def match_has_lp_change(state: BotState, platform, summoner_id, headers, queue_id):
    previous_lp = state.last_ranked_lp.get(queue_id)
    if previous_lp is None:
        await load_ranked_lp(state, platform, summoner_id, headers)
        previous_lp = state.last_ranked_lp.get(queue_id)

    for _ in range(6):
        entries = await fetch_league_entries(platform, summoner_id, headers)
        latest_lp = league_entries_to_lp(entries).get(queue_id)
        if latest_lp is None:
            await asyncio.sleep(3)
            continue
        if previous_lp is None:
            state.last_ranked_lp[queue_id] = latest_lp
            return False
        if latest_lp != previous_lp:
            state.last_ranked_lp[queue_id] = latest_lp
            return True
        await asyncio.sleep(3)
    return False


async def compute_initial_streak(state: BotState, platform, routing, summoner_id, puuid, headers, max_matches=20):
    ids = await fetch_match_ids(routing, puuid, headers, count=max_matches)
    if not ids:
        return

    first_result = None
    streak_count = 0
    for index, match_id in enumerate(ids):
        match_info = await fetch_match_info(routing, match_id, puuid, headers)
        if match_info is None:
            continue

        has_lp_change = await match_has_lp_change(state, platform, summoner_id, headers, match_info["queueId"])
        if not has_lp_change:
            continue

        first_result = match_info["result"]
        state.last_match_id = match_id
        break

    if first_result is None:
        return

    streak_count = 1
    for match_id in ids[index + 1:]:
        match_info = await fetch_match_info(routing, match_id, puuid, headers)
        if match_info is None:
            continue
        if match_info["result"] != first_result:
            continue
        if not await match_has_lp_change(state, platform, summoner_id, headers, match_info["queueId"]):
            continue
        streak_count += 1

    state.current_streak_type = first_result
    state.current_streak_count = streak_count


async def read_chat_loop(reader, writer, state: BotState):
    while not reader.at_eof():
        line = await reader.readline()
        if not line:
            break
        line = line.decode("utf-8", errors="ignore").rstrip()
        if line.startswith("PING"):
            await send_irc_line(writer, line.replace("PING", "PONG"))
            continue

        match = CHAT_RE.match(line)
        if match:
            nick, message = match.groups()
            await handle_privmsg(state, nick, message)


async def write_chat_loop(writer, channel, state: BotState):
    while True:
        message = await state.chat_send_queue.get()
        await send_chat_message(writer, channel, message)
        await asyncio.sleep(1)


async def main():
    if "your_bot_username" in TWITCH_USER or "your_twitch_oauth_token" in TWITCH_OAUTH_TOKEN:
        raise RuntimeError(
            "Configura TWITCH_USER, TWITCH_OAUTH_TOKEN, TWITCH_CHANNEL, RIOT_API_KEY, SUMMONER_NAME y REGION antes de ejecutar el bot."
        )
    templates = load_streak_templates(STREAK_FILE)

    reader, writer = await asyncio.open_connection("irc.chat.twitch.tv", 6667)
    await send_irc_line(writer, f"PASS {TWITCH_OAUTH_TOKEN}")
    await send_irc_line(writer, f"NICK {TWITCH_USER}")
    await send_irc_line(writer, f"JOIN #{TWITCH_CHANNEL}")

    state = BotState()
    state.chat_send_queue.put_nowait("Bot conectado y listo para rastrear rachas.")

    tasks = [
        asyncio.create_task(read_chat_loop(reader, writer, state)),
        asyncio.create_task(write_chat_loop(writer, TWITCH_CHANNEL, state)),
        asyncio.create_task(riot_poll_loop(state, templates)),
    ]

    await asyncio.gather(*tasks)


if __name__ == "__main__":
    asyncio.run(main())
