import asyncio
import base64
import hashlib
import os
import re
import ssl
import struct
import time
import urllib.parse
import urllib.request
from pathlib import Path
import logging

# Configuración básica: actualiza estos valores o exporta variables de entorno.
TWITCH_USER = os.getenv("TWITCH_USER", "NoPhantasm")
TWITCH_OAUTH_TOKEN = os.getenv("TWITCH_OAUTH_TOKEN", "oauth:2p52g2gvdclhzky31mny4a0nhpmfy0")
TWITCH_CHANNEL = os.getenv("TWITCH_CHANNEL", "RubenIRPG").lstrip("#")
TWITCH_CLIENT_ID = os.getenv("TWITCH_CLIENT_ID", "gp762nuuoqcoxypju8c569th9wz7q5")
RIOT_API_KEY = os.getenv("RIOT_API_KEY", "RGAPI-fceb4641-b051-4bef-8e5c-17d1710dbbfa")
SUMMONER_NAME_RAW = os.getenv("SUMMONER_NAME", "Phantasm#TWTV0")
SUMMONER_TAG = os.getenv("SUMMONER_TAG", "#TWTV0")
if "#" in SUMMONER_NAME_RAW:
    SUMMONER_NAME, SUMMONER_TAG = SUMMONER_NAME_RAW.split("#", 1)
else:
    SUMMONER_NAME = SUMMONER_NAME_RAW
 
REGION = os.getenv("REGION", "europe")
SLEEP_IN_GAME = int(os.getenv("SLEEP_IN_GAME", "10"))
SLEEP_OUT_GAME = int(os.getenv("SLEEP_OUT_GAME", "5"))
GLOBAL_CD_SECONDS = 2
PENDING_SEND_TIMEOUT = int(os.getenv("PENDING_SEND_TIMEOUT", "10"))
CONNECTION_TIMEOUT = int(os.getenv("CONNECTION_TIMEOUT", "15"))
STREAK_FILE = Path(__file__).with_name("streak_messages.json")
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

CHAT_RE = re.compile(r"^(?:@[^ ]+ )?:([^!]+)!.* PRIVMSG #[^ ]+ :(.+)$")
LOUIS_RESULT_RE = re.compile(r"\b(Win|Loss|Lose|Lost)\b", re.IGNORECASE)


class BotState:
    def __init__(self, templates=None):
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
        self.templates = templates or {}

    def format_active_streak(self):
        if not self.current_streak_type or self.current_streak_count == 0:
            return "Waiting for a streak Bro"
        
        # Obtener el mensaje personalizado del JSON
        category = f"{self.current_streak_type}_streak"
        message = choose_template(self.templates, category, self.current_streak_count)
        
        if message:
            return message
        
        # Fallback si no hay template personalizado
        suffix = "W" if self.current_streak_type == "win" else "L"
        return f"Current streak: {self.current_streak_count}{suffix}"


# Configuración del logger
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("streak_bot")


def load_streak_templates(path: Path):
    if not path.exists():
        raise FileNotFoundError(f"No se encontró {path}")

    if path.suffix.lower() == ".json":
        import json

        templates = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(templates, dict):
            raise ValueError(f"Formato JSON inválido en {path}")
        return templates

    templates = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        current = templates
        key_parts = key.strip().split(".")
        for part in key_parts[:-1]:
            current = current.setdefault(part, {})
        current[key_parts[-1]] = value.strip()
    return templates


def get_template(templates, key):
    current = templates
    for part in key.split('.'):
        if not isinstance(current, dict):
            return None
        current = current.get(part)
    return current if isinstance(current, str) else None


def choose_template(templates, category, streak):
    exact = get_template(templates, f"{category}.{streak}")
    if exact is not None:
        return exact

    if streak >= 20:
        fallback = get_template(templates, f"{category}.20+")
        if fallback is not None:
            return fallback.replace("{streak}", str(streak))

    fallback = get_template(templates, f"{category}.1")
    if fallback is not None:
        return fallback.replace("{streak}", str(streak))

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
    data = f"{line}\r\n".encode("utf-8")
    result = writer.write(data)
    if asyncio.iscoroutine(result):
        await result
    await writer.drain()


def _make_websocket_key():
    return base64.b64encode(os.urandom(16)).decode("ascii")


def _mask_payload(payload: bytes, mask: bytes) -> bytes:
    return bytes(b ^ mask[i % 4] for i, b in enumerate(payload))


class WebsocketIRCWriter:
    def __init__(self, writer):
        self._writer = writer

    async def write(self, data):
        if isinstance(data, str):
            data = data.encode("utf-8")
        payload_len = len(data)
        header = bytearray([0x81])
        if payload_len < 126:
            header.append(0x80 | payload_len)
        elif payload_len < (1 << 16):
            header.append(0x80 | 126)
            header.extend(struct.pack("!H", payload_len))
        else:
            header.append(0x80 | 127)
            header.extend(struct.pack("!Q", payload_len))
        mask_key = os.urandom(4)
        header.extend(mask_key)
        self._writer.write(header + _mask_payload(data, mask_key))
        await self._writer.drain()

    async def drain(self):
        await self._writer.drain()


class WebsocketIRCReader:
    def __init__(self, reader, writer):
        self._reader = reader
        self._writer = writer
        self._buffer = bytearray()

    def at_eof(self):
        return self._reader.at_eof() and not self._buffer

    async def readline(self):
        while True:
            if b"\n" in self._buffer:
                line, sep, rest = self._buffer.partition(b"\n")
                self._buffer = rest
                return line + sep
            chunk = await self._read_message()
            if not chunk:
                return b""
            self._buffer.extend(chunk)

    async def _read_message(self):
        header = await self._reader.readexactly(2)
        b1, b2 = header[0], header[1]
        opcode = b1 & 0x0F
        masked = b2 >> 7
        payload_len = b2 & 0x7F
        if payload_len == 126:
            ext = await self._reader.readexactly(2)
            payload_len = struct.unpack("!H", ext)[0]
        elif payload_len == 127:
            ext = await self._reader.readexactly(8)
            payload_len = struct.unpack("!Q", ext)[0]
        mask_key = await self._reader.readexactly(4) if masked else None
        payload = await self._reader.readexactly(payload_len) if payload_len else b""
        if masked and mask_key:
            payload = _mask_payload(payload, mask_key)
        if opcode == 0x8:
            return b""
        if opcode == 0x9:
            await self._send_pong(payload)
            return b""
        if opcode == 0xA:
            return b""
        if opcode == 0x1:
            return payload
        return b""

    async def _send_pong(self, payload: bytes):
        header = bytearray([0x8A])
        payload_len = len(payload)
        if payload_len < 126:
            header.append(0x80 | payload_len)
        elif payload_len < (1 << 16):
            header.append(0x80 | 126)
            header.extend(struct.pack("!H", payload_len))
        else:
            header.append(0x80 | 127)
            header.extend(struct.pack("!Q", payload_len))
        mask_key = os.urandom(4)
        header.extend(mask_key)
        self._writer.write(header + _mask_payload(payload, mask_key))
        await self._writer.drain()


async def open_twitch_websocket(host, port, ssl_context):
    reader, writer = await asyncio.open_connection(host, port, ssl=ssl_context)
    websocket_key = _make_websocket_key()
    handshake = (
        f"GET / HTTP/1.1\r\n"
        f"Host: {host}:{port}\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {websocket_key}\r\n"
        "Sec-WebSocket-Version: 13\r\n"
        "Origin: https://twitch.tv\r\n"
        "User-Agent: Python/3\r\n"
        "\r\n"
    ).encode("utf-8")
    writer.write(handshake)
    await writer.drain()
    response = await reader.readuntil(b"\r\n\r\n")
    if not response.startswith(b"HTTP/1.1 101"):
        raise ConnectionError(
            f"WebSocket handshake failed: {response.splitlines()[0].decode('ascii', 'ignore')}"
        )
    lines = response.decode("ascii", "ignore").splitlines()
    accept_line = next(
        (line for line in lines if line.lower().startswith("sec-websocket-accept:")),
        None,
    )
    if accept_line is None:
        raise ConnectionError("WebSocket handshake missing Sec-WebSocket-Accept header")
    accept_value = accept_line.split(":", 1)[1].strip()
    expected = base64.b64encode(
        hashlib.sha1(
            (websocket_key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode("ascii")
        ).digest()
    ).decode("ascii")
    if accept_value != expected:
        raise ConnectionError("WebSocket handshake Sec-WebSocket-Accept mismatch")
    return WebsocketIRCReader(reader, writer), WebsocketIRCWriter(writer)


async def send_chat_message(writer, channel: str, message: str):
    logger.info("Enviando mensaje al chat #%s: %s", channel, message)
    await send_irc_line(writer, f"PRIVMSG #{channel} :{message}")


async def handle_privmsg(state: BotState, nick: str, message: str):
    lower_nick = nick.lower()
    lower_msg = message.strip().lower()

    if lower_msg.startswith("!streak"):
        logger.info("Comando !streak recibido de %s", nick)
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
            logger.info("Evento Louis detectado: %s", result)
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

    logger.info("Iniciando monitor Riot para %s en %s", SUMMONER_NAME, platform)
    summoner = await fetch_summoner(platform, headers)
    if summoner is None:
        logger.error("No se pudo cargar el invocador de Riot. Verifica REGION, SUMMONER_NAME y RIOT_API_KEY.")
        raise RuntimeError("No se pudo cargar el invocador de Riot. Verifica REGION, SUMMONER_NAME y RIOT_API_KEY.")

    summoner_id = summoner["id"]
    puuid = summoner["puuid"]
    await load_ranked_lp(state, platform, summoner_id, headers)
    await compute_initial_streak(state, platform, routing, summoner_id, puuid, headers)
    last_seen_in_game = False

    while True:
        active_game = await fetch_active_game(platform, summoner_id, headers)
        if active_game is not None:
            logger.debug("Invocador está en partida activa")
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
                logger.info("Partida finalizada %s resultado=%s lp_change=%s", match_id, result, has_lp_change)
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
                logger.info("Resultado encolado para enviar: %s (se enviará tras %s s)", match_id, PENDING_SEND_TIMEOUT)
                await try_send_pending_streak(state)
        await asyncio.sleep(SLEEP_OUT_GAME)


async def fetch_summoner(platform, headers):
    encoded_name = urllib.parse.quote(SUMMONER_NAME)
    url = f"https://{platform}.api.riotgames.com/lol/summoner/v4/summoners/by-name/{encoded_name}"
    try:
        return await fetch_json(url, headers)
    except Exception as exc:
        logger.exception(
            "Error al buscar invocador Riot %s en %s: %s",
            SUMMONER_NAME,
            platform,
            exc,
        )
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
            logger.debug("PING recibido del servidor Twitch, respondiendo PONG")
            await send_irc_line(writer, line.replace("PING", "PONG"))
            continue

        match = CHAT_RE.match(line)
        if match:
            nick, message = match.groups()
            logger.debug("Mensaje de chat de %s: %s", nick, message)
            await handle_privmsg(state, nick, message)


async def write_chat_loop(writer, channel, state: BotState):
    while True:
        message = await state.chat_send_queue.get()
        logger.debug("Preparando envío de mensaje al chat: %s", message)
        await send_chat_message(writer, channel, message)
        await asyncio.sleep(1)


async def main():
    if "your_bot_username" in TWITCH_USER or "your_twitch_oauth_token" in TWITCH_OAUTH_TOKEN:
        raise RuntimeError(
            "Configura TWITCH_USER, TWITCH_OAUTH_TOKEN, TWITCH_CHANNEL, RIOT_API_KEY, SUMMONER_NAME y REGION antes de ejecutar el bot."
        )
    templates = load_streak_templates(STREAK_FILE)

    ssl_context = ssl.create_default_context()
    connection_attempts = [
        ("irc.chat.twitch.tv", 6667, False, False),
        ("irc.chat.twitch.tv", 6697, True, False),
        ("irc-ws.chat.twitch.tv", 443, True, True),
    ]
    reader = writer = None
    last_exception = None

    for host, port, use_ssl, use_websocket in connection_attempts:
        logger.info(
            "Intentando conectar a Twitch IRC en %s:%s (SSL=%s, websocket=%s)",
            host,
            port,
            use_ssl,
            use_websocket,
        )
        try:
            if use_websocket:
                connect_coro = open_twitch_websocket(host, port, ssl_context)
            elif use_ssl:
                connect_coro = asyncio.open_connection(host, port, ssl=ssl_context)
            else:
                connect_coro = asyncio.open_connection(host, port)
            reader, writer = await asyncio.wait_for(connect_coro, timeout=CONNECTION_TIMEOUT)
            logger.info(
                "Conectado a Twitch IRC en %s:%s (SSL=%s, websocket=%s)",
                host,
                port,
                use_ssl,
                use_websocket,
            )
            break
        except asyncio.TimeoutError as exc:
            last_exception = exc
            logger.warning(
                "Tiempo de espera agotado al conectar a Twitch IRC en %s:%s (SSL=%s, websocket=%s) tras %s s",
                host,
                port,
                use_ssl,
                use_websocket,
                CONNECTION_TIMEOUT,
            )
        except Exception as exc:
            last_exception = exc
            logger.warning(
                "No se pudo conectar a Twitch IRC en %s:%s (SSL=%s, websocket=%s): %s",
                host,
                port,
                use_ssl,
                use_websocket,
                exc,
            )

    if reader is None or writer is None:
        if last_exception is not None:
            logger.error(
                "Fallo al conectar a Twitch IRC tras varios intentos: %s",
                last_exception,
                exc_info=last_exception,
            )
        else:
            logger.error("Fallo al conectar a Twitch IRC tras varios intentos: sin excepción disponible")
        raise last_exception or RuntimeError("Fallo al conectar a Twitch IRC tras varios intentos")

    await send_irc_line(writer, f"PASS {TWITCH_OAUTH_TOKEN}")
    await send_irc_line(writer, f"NICK {TWITCH_USER}")
    await send_irc_line(writer, f"JOIN #{TWITCH_CHANNEL}")
    logger.info("Conectado a Twitch IRC como %s y unido al canal #%s", TWITCH_USER, TWITCH_CHANNEL)

    state = BotState(templates)

    tasks = [
        asyncio.create_task(read_chat_loop(reader, writer, state)),
        asyncio.create_task(write_chat_loop(writer, TWITCH_CHANNEL, state)),
        asyncio.create_task(riot_poll_loop(state, templates)),
    ]

    logger.info("Iniciando tareas del bot")
    try:
        await asyncio.gather(*tasks)
    except Exception:
        logger.exception("Error en las tareas del bot")
        raise


if __name__ == "__main__":
    asyncio.run(main())
