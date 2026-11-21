import asyncio
import difflib
import os
import re
import time
from aiohttp import web
from telethon import TelegramClient, events
from telethon.errors import FloodWaitError
from telethon.sessions import StringSession
from telethon.tl import types
from telethon.utils import get_peer_id

# --- НАСТРОЙКИ ---
API_ID = 38360905
API_HASH = '99073f3ecb25cbf13adf9391ae4190c3'

# Берем строку сессии из переменных окружения Render
SESSION_STRING = os.getenv('SESSION_STRING')

# Базовый список каналов-источников
SOURCE_CHANNELS = [
    'oneshot_tg',
    'igromania',
    'oksikek',
]

# Названия каналов, которые бот должен сам найти среди подписок аккаунта при старте
AUTO_FIND_BY_TITLE = [
    'Пекарня',
]

TARGET_CHANNEL = '@joybyte'

SPAM_PATTERNS = [
    r'casino', r'казино', r'рулетк', r'1win', r'1xbet', r'melbet',
    r'букмекер', r'фрибет', r'промокод на депозит', r'заработок в интернете',
    r'инвестиции', r't\.me/joinchat', r't\.me/\+',
    r'реклама', r'erid:', r'подписывайтесь на канал', r'наш партнер', r'коллеги рекомендуют',
    # Рекламные розыгрыши, гивы и конкурсы
    r'розыгрыш', r'разыгрываем', r'разыграем', r'giveaway', r'гив\b', r'конкурс',
    r'условия\s+(?:розыгрыша|конкурса|участия)', r'для\s+участия\s+(?:нужно|необходимо|в\s+розыгрыше)',
    r'выберем\s+победител', r'итоги\s+(?:розыгрыша|конкурса)', r'призовой\s+фонд',
    r'рандомайзер', r'промокод', r'халяв[аыуе]',
    r'(?:подпис[а-яё]*|репост[а-яё]*).{0,40}(?:розыгрыш|приз|аккаунт|ключ|скин|деньг|руб)',
    r'(?:розыгрыш|приз|аккаунт|ключ|скин).{0,40}(?:подпис[а-яё]*|репост[а-яё]*)',
    # Подборки обоев и рабочего стола
    r'wallpaper\s*engine',
    r'(?:подборк[аиу]|собрали).{0,60}обо[её]в|обо[её]в.{0,60}(?:wallpaper\s*engine|рабочий\s*стол)',
    r'4k-?обо[её]в|живы[ех]\s+обо[её]в|обо[её]в\s+для\s+(?:пк|рабочего\s*стола)',
    # Сервисы пополнения и рекламные маркетплейсы ключей/игр
    r'ggsel', r'карт[аыу]\s+пополнен[ия]', r'сервис[а-яё]*\s+пополнен[ия]',
    r'пополн[иеить]{2,5}\s+(?:steam|стим|баланс|кошелек|кошелёк|аккаунт[а-яё]*)',
    r'залетайте\s+на\s+\S+\s+за\s+(?:игр[оойу]|ключ[а-яё]*|карт[аыу]|пополнени[а-яё]*)'
]

SENT_POSTS_HISTORY = []
DUPLICATE_TIME_WINDOW = 3 * 60 * 60  # 3 часа
SIMILARITY_THRESHOLD = 0.78          # Порог схожести текста (78%)

ACTIVE_SOURCES = list(SOURCE_CHANNELS)
ALBUM_DELAY = 1.5


def _utf16_len(s: str) -> int:
    return len(s.encode('utf-16-le')) // 2


def _utf16_to_py_index(s: str, utf16_offset: int) -> int:
    cur_utf16 = 0
    for idx, ch in enumerate(s):
        if cur_utf16 >= utf16_offset:
            return idx
        cur_utf16 += _utf16_len(ch)
    return len(s)


def _py_to_utf16_index(s: str, py_index: int) -> int:
    return _utf16_len(s[:py_index])


def normalize_text(text: str) -> str:
    if not text:
        return ""

    text = text.lower()
    text = re.sub(r'https?://\S+|www\.\S+', '', text)
    text = re.sub(r'@\w+', '', text)
    text = re.sub(r'[^\w\s]', '', text)

    words = text.split()
    stop_words = {
        'и', 'в', 'во', 'не', 'что', 'он', 'на', 'я', 'с', 'со', 'как', 'а', 'то',
        'все', 'но', 'да', 'ты', 'к', 'у', 'же', 'вы', 'за', 'бы', 'по', 'только',
        'ее', 'мне', 'было', 'вот', 'от', 'меня', 'еще', 'нет', 'о', 'из', 'ему',
        'теперь', 'когда', 'даже', 'ру', 'the', 'a', 'an', 'in', 'to', 'is', 'of', 'for'
    }
    filtered_words = [w for w in words if w not in stop_words and len(w) > 2]
    return " ".join(filtered_words)


def _cleanup_old_history():
    global SENT_POSTS_HISTORY
    current_time = time.time()
    SENT_POSTS_HISTORY = [
        item for item in SENT_POSTS_HISTORY
        if current_time - item['timestamp'] < DUPLICATE_TIME_WINDOW
    ]


def is_duplicate_post(text_for_check: str) -> bool:
    _cleanup_old_history()
    norm_new = normalize_text(text_for_check)

    if not norm_new or len(norm_new.split()) < 6:
        return False

    for item in SENT_POSTS_HISTORY:
        norm_existing = item['normalized_text']
        similarity = difflib.SequenceMatcher(None, norm_new, norm_existing).ratio()
        if similarity >= SIMILARITY_THRESHOLD:
            print(f"[АНТИДУБЛЬ] Найден похожий пост (схожесть {int(similarity * 100)}%). Пропускаем.")
            return True

    return False


def record_published_post(text_for_check: str):
    _cleanup_old_history()
    norm_new = normalize_text(text_for_check)
    if norm_new and len(norm_new.split()) >= 6:
        SENT_POSTS_HISTORY.append({
            'normalized_text': norm_new,
            'timestamp': time.time()
        })


def is_ad_or_pr(text: str) -> bool:
    if not text:
        return False
    text_lower = text.lower()
    for pattern in SPAM_PATTERNS:
        if re.search(pattern, text_lower):
            return True
    return False


def _shift_py_index(idx: int, removed_spans: list) -> int:
    shifted = idx
    for start, end in removed_spans:
        if idx >= end:
            shifted -= (end - start)
        elif idx > start:
            shifted = start
    return max(0, shifted)


def _clone_entity(e, new_offset: int, new_length: int):
    try:
        if isinstance(e, types.MessageEntityTextUrl):
            return types.MessageEntityTextUrl(
                offset=new_offset,
                length=new_length,
                url=getattr(e, 'url', '') or ''
            )
        if isinstance(e, types.MessageEntityMentionName):
            return types.MessageEntityMentionName(
                offset=new_offset,
                length=new_length,
                user_id=getattr(e, 'user_id', 0)
            )
        if isinstance(e, types.MessageEntityPre):
            return types.MessageEntityPre(
                offset=new_offset,
                length=new_length,
                language=getattr(e, 'language', '') or ''
            )
        if isinstance(e, types.MessageEntitySpoiler):
            return types.MessageEntitySpoiler(
                offset=new_offset,
                length=new_length
            )
        if isinstance(e, types.MessageEntityBlockquote):
            return types.MessageEntityBlockquote(
                offset=new_offset,
                length=new_length,
                collapsed=getattr(e, 'collapsed', None)
            )

        e_copy = type(e)(offset=new_offset, length=new_length)
        for attr in ('url', 'user_id', 'language', 'document_id', 'collapsed'):
            if hasattr(e, attr) and hasattr(e_copy, attr):
                setattr(e_copy, attr, getattr(e, attr))
        return e_copy
    except Exception as err:
        print(f"[РАЗМЕТКА] Пропущена сущность {type(e).__name__}: {err}")
        return None


def clean_text_and_entities(text: str, entities):
    if not text:
        return "", None

    original_text = text
    remove_spans = []

    if entities:
        for e in entities:
            if isinstance(e, (types.MessageEntityTextUrl, types.MessageEntityUrl)):
                py_start = _utf16_to_py_index(original_text, e.offset)
                py_end = _utf16_to_py_index(original_text, e.offset + e.length)
                url = getattr(e, 'url', '') or ''
                anchor_text = original_text[py_start:py_end]
                check_target = f"{url} {anchor_text}".lower()
                if any(k in check_target for k in ('t.me/', 'telegram.me/', 'tg://')):
                    remove_spans.append((py_start, py_end))

    merged_spans = []
    if remove_spans:
        remove_spans.sort()
        for start, end in remove_spans:
            if not merged_spans or start > merged_spans[-1][1]:
                merged_spans.append([start, end])
            else:
                merged_spans[-1][1] = max(merged_spans[-1][1], end)

        for start, end in reversed(merged_spans):
            text = text[:start] + text[end:]

    safe_entities = []
    if entities:
        for e in entities:
            if isinstance(e, types.MessageEntityCustomEmoji):
                continue
            if isinstance(e, (types.MessageEntityTextUrl, types.MessageEntityUrl)):
                url = getattr(e, 'url', '') or ''
                if any(k in url.lower() for k in ('t.me/', 'telegram.me/', 'tg://')):
                    continue

            py_start = _utf16_to_py_index(original_text, e.offset)
            py_end = _utf16_to_py_index(original_text, e.offset + e.length)

            new_py_start = _shift_py_index(py_start, merged_spans)
            new_py_end = _shift_py_index(py_end, merged_spans)

            if new_py_end <= new_py_start:
                continue

            new_offset = _py_to_utf16_index(text, new_py_start)
            new_length = _py_to_utf16_index(text, new_py_end) - new_offset
            if new_length <= 0:
                continue

            e_copy = _clone_entity(e, new_offset, new_length)
            if e_copy is not None:
                safe_entities.append(e_copy)

    text = text.replace('🥺', '')
    text = re.sub(r'https?://(?:t|telegram)\.me/\S+', '', text, flags=re.IGNORECASE)
    text = re.sub(r'(?<!\w)(?:t|telegram)\.me/\S+', '', text, flags=re.IGNORECASE)
    text = re.sub(r'@\w+', '', text)

    tail_patterns = [
        r'(?im)^[ \t]*(?:👉|➡️|▶️|🔹|🔸|📌|🔗|👇)?[ \t]*(?:источник|канал|подписаться|наш канал)\s*:?.*$',
        r'(?im)^[ \t]*👉[ \t]+\S+(?:\s+\S+)?[ \t]*$',
        r'(?is)(?:\n\s*){1,2}(?:скачать|взять|найти|перейти)\s+можно\s*[\.,!]?\s*$'
    ]
    for pattern in tail_patterns:
        text = re.sub(pattern, '', text)

    lines = text.splitlines()
    while lines and re.fullmatch(r'[\s\W_]*', lines[-1], flags=re.UNICODE):
        lines.pop()
    text = '\n'.join(lines)

    text = re.sub(r'\n{3,}', '\n\n', text).strip()

    if text != original_text:
        safe_entities = None

    return text, safe_entities if safe_entities else None


async def publish_content(media_list, cleaned_text: str, final_entities, is_album: bool):
    while True:
        try:
            if is_album:
                if media_list:
                    await client.send_file(
                        TARGET_CHANNEL,
                        file=media_list,
                        caption=cleaned_text if cleaned_text else None,
                        formatting_entities=final_entities
                    )
                else:
                    await client.send_message(
                        TARGET_CHANNEL,
                        cleaned_text,
                        formatting_entities=final_entities
                    )
            else:
                if media_list:
                    await client.send_file(
                        TARGET_CHANNEL,
                        file=media_list[0],
                        caption=cleaned_text if cleaned_text else None,
                        formatting_entities=final_entities
                    )
                else:
                    await client.send_message(
                        TARGET_CHANNEL,
                        cleaned_text,
                        formatting_entities=final_entities
                    )
            return True
        except FloodWaitError as e:
            print(f"[FLOOD_WAIT] Ожидание {e.seconds} сек...")
            await asyncio.sleep(e.seconds + 1)
        except Exception as e:
            print(f"[ОШИБКА] Не удалось опубликовать: {e}")
            return False


client = TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH)
albums_cache = {}
album_tasks = {}


async def _finalize_album(group_id: int):
    try:
        await asyncio.sleep(ALBUM_DELAY)
        data = albums_cache.pop(group_id, None)
        if not data:
            return

        msgs = data['messages']
        raw_txt = data['text']
        ent = data['entities']

        if is_ad_or_pr(raw_txt):
            print(f"[ПРОПУСК] Реклама/спам/фильтр в альбоме из канала {data['chat']}")
            return

        cleaned_text, final_entities = clean_text_and_entities(raw_txt, ent)
        if cleaned_text and is_duplicate_post(cleaned_text):
            return

        media_list = [m.media for m in msgs if m.media]
        if not cleaned_text and not media_list:
            print("[ПРОПУСК] Альбом пуст после очистки.")
            return

        print(f"[АЛЬБОМ] Публикуем альбом из {data['chat']}...")
        ok = await publish_content(media_list, cleaned_text, final_entities, is_album=True)
        if ok:
            if cleaned_text:
                record_published_post(cleaned_text)
            print("[УСПЕХ] Альбом опубликован!")
    except Exception as e:
        print(f"[ОШИБКА] Сбой при обработке альбома {group_id}: {e}")
    finally:
        album_tasks.pop(group_id, None)


def _is_tracked_chat(event) -> bool:
    chat_id = event.chat_id
    sender_username = (getattr(event.chat, 'username', None) or '').lower()
    for src in ACTIVE_SOURCES:
        if isinstance(src, int) and src == chat_id:
            return True
        if isinstance(src, str):
            clean_src = src.lstrip('@').lower()
            if sender_username and sender_username == clean_src:
                return True
    return False


@client.on(events.NewMessage())
async def handle_new_post(event):
    try:
        if not _is_tracked_chat(event):
            return

        message = event.message
        text = message.raw_text or ""
        safe_entities = list(message.entities) if message.entities else None

        if message.grouped_id:
            group_id = message.grouped_id
            if group_id not in albums_cache:
                albums_cache[group_id] = {
                    'messages': [],
                    'text': "",
                    'entities': None,
                    'chat': event.chat_id
                }

            albums_cache[group_id]['messages'].append(message)

            if text and not albums_cache[group_id]['text']:
                albums_cache[group_id]['text'] = text
                albums_cache[group_id]['entities'] = safe_entities

            if group_id not in album_tasks:
                album_tasks[group_id] = asyncio.create_task(_finalize_album(group_id))
            return

        if is_ad_or_pr(text):
            print(f"[ПРОПУСК] Реклама/спам/фильтр в канале {event.chat_id}")
            return

        cleaned_text, final_entities = clean_text_and_entities(text, safe_entities)
        if cleaned_text and is_duplicate_post(cleaned_text):
            return

        media_list = [message.media] if message.media else []
        if not cleaned_text and not media_list:
            print("[ПРОПУСК] Сообщение пустое.")
            return

        print(f"[НОВЫЙ ПОСТ] Обрабатываем пост из {event.chat_id}...")
        ok = await publish_content(media_list, cleaned_text, final_entities, is_album=False)
        if ok:
            if cleaned_text:
                record_published_post(cleaned_text)
            print("[УСПЕХ] Пост опубликован в @joybyte!")
    except Exception as e:
        print(f"[ОШИБКА] Сбой при обработке сообщения: {e}")


async def handle_ping(request):
    return web.Response(text="Bot is running!")


async def start_web_server():
    app = web.Application()
    app.router.add_get("/", handle_ping)
    runner = web.AppRunner(app)
    await runner.setup()

    port = int(os.environ.get("PORT", 10000))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    print(f"Веб-сервер для Render запущен на порту {port}")


async def preload_channels():
    """Выводит каналы из диалогов аккаунта (с их ID), автонаходит каналы по названию и проверяет источники."""
    global ACTIVE_SOURCES
    dialogs = await client.get_dialogs()
    print("--- КАНАЛЫ В ДИАЛОГАХ АККАУНТА (ID | НАЗВАНИЕ | @USERNAME) ---")
    channel_dialogs = []
    for d in dialogs:
        if d.is_channel:
            peer_id = get_peer_id(d.entity)
            title = getattr(d.entity, 'title', 'Без названия')
            username = getattr(d.entity, 'username', None)
            u_str = f"@{username}" if username else "приватный"
            print(f"  ID: {peer_id} | {title} | {u_str}")
            channel_dialogs.append((peer_id, title, username))
    print("-------------------------------------------------------------")

    found_sources = list(SOURCE_CHANNELS)
    for target_title in AUTO_FIND_BY_TITLE:
        t_lower = target_title.strip().lower()
        matched = False
        for peer_id, title, _ in channel_dialogs:
            if t_lower and t_lower in (title or '').lower():
                if peer_id not in found_sources:
                    found_sources.append(peer_id)
                print(f"[АВТОПОИСК] Найден канал '{title}' -> ID {peer_id}")
                matched = True
                break
        if not matched:
            print(f"[АВТОПОИСК] Канал с названием '{target_title}' не найден среди диалогов аккаунта.")

    ACTIVE_SOURCES = found_sources

    for ch in ACTIVE_SOURCES:
        try:
            entity = await client.get_entity(ch)
            print(f"[КАНАЛ ОК] {ch} -> {getattr(entity, 'title', ch)}")
        except Exception as e:
            print(f"[КАНАЛ ОШИБКА] Нет доступа к {ch}: {e}")

    try:
        target_entity = await client.get_entity(TARGET_CHANNEL)
        print(f"[ЦЕЛЬ ОК] {TARGET_CHANNEL} -> {getattr(target_entity, 'title', TARGET_CHANNEL)}")
    except Exception as e:
        print(f"[ЦЕЛЬ ОШИБКА] Нет доступа к целевому каналу {TARGET_CHANNEL}: {e}")


async def main():
    print("Инициализация подключения к Telegram...")
    if not SESSION_STRING:
        print("[ОШИБКА] Переменная окружения SESSION_STRING не задана!")
        return

    await client.connect()

    if not await client.is_user_authorized():
        print("Ошибка: сессия не авторизована.")
        return

    await preload_channels()
    await start_web_server()
    print("Бот запущен и отслеживает посты...")

    while True:
        try:
            await client.run_until_disconnected()
        except Exception as e:
            print(f"[СЕТЬ] Соединение потеряно ({e}). Переподключение через 5 секунд...")
            await asyncio.sleep(5)
            try:
                await client.connect()
            except Exception:
                pass


if __name__ == '__main__':
    try:
        client.loop.run_until_complete(main())
    except KeyboardInterrupt:
        print("\nБот остановлен пользователем.")
