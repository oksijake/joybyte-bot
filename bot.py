import asyncio
import os
import re
import time
import difflib
from telethon import TelegramClient, events
from telethon.sessions import StringSession
from aiohttp import web

# --- НАСТРОЙКИ ---
API_ID = 38360905
API_HASH = '99073f3ecb25cbf13adf9391ae4190c3'

# Берем строку сессии из переменных окружения Render
SESSION_STRING = os.getenv('SESSION_STRING')

SOURCE_CHANNELS = [
    'pekagame',
    'SteamNetwork',
    'igromania',
    'oksikek',
    -1001807003554,
]

TARGET_CHANNEL = '@joybyte'

SPAM_PATTERNS = [
    r'casino', r'казино', r'рулетк', r'1win', r'1xbet', r'melbet',
    r'букмекер', r'фрибет', r'промокод на депозит', r'заработок в интернете',
    r'инвестиции', r't\.me/joinchat', r't\.me/\+',
    r'реклама', r'erid:', r'подписывайтесь на канал', r'наш партнер', r'коллеги рекомендуют'
]

# Хранилище для защиты от дубликатов из разных каналов
# Сохраняет список словарей: [{'normalized_text': '...', 'keywords': set(), 'timestamp': 1234567890}]
SENT_POSTS_HISTORY = []
DUPLICATE_TIME_WINDOW = 3 * 60 * 60  # Время хранения истории дублей (3 часа)
SIMILARITY_THRESHOLD = 0.70          # Порог похожести (70%), при котором пост считается дублем


def normalize_text(text):
    """Приводит текст к нормализованному виду для сравнения (удаляет знаки, ссылки, стоп-слова)."""
    if not text:
        return ""
    
    text = text.lower()
    # Удаляем ссылки, упоминания, теги
    text = re.sub(r'https?://\S+|www\.\S+', '', text)
    text = re.sub(r'@\w+', '', text)
    # Оставляем только буквы и цифры
    text = re.sub(r'[^\w\s]', '', text)
    
    # Удаляем короткие стоп-слова
    words = text.split()
    stop_words = {'и', 'в', 'во', 'не', 'что', 'он', 'на', 'я', 'с', 'со', 'как', 'а', 'то', 'все', 'но', 'да', 'ты', 'к', 'у', 'же', 'вы', 'за', 'бы', 'по', 'только', 'ее', 'мне', 'было', 'вот', 'от', 'меня', 'еще', 'нет', 'о', 'из', 'ему', 'теперь', 'когда', 'даже', 'ру', 'the', 'a', 'an', 'in', 'to', 'is', 'of', 'for'}
    filtered_words = [w for w in words if w not in stop_words and len(w) > 2]
    
    return " ".join(filtered_words)


def extract_keywords(text):
    """Извлекает значимые ключевые слова (имена, бренды, термины) для поиска дублей по смыслу."""
    if not text:
        return set()
    
    text = text.lower()
    text = re.sub(r'https?://\S+|www\.\S+', '', text)
    text = re.sub(r'@\w+', '', text)
    text = re.sub(r'[^\w\s]', '', text)
    
    words = text.split()
    stop_words = {
        'и', 'в', 'во', 'не', 'что', 'он', 'на', 'я', 'с', 'со', 'как', 'а', 'то', 
        'все', 'но', 'да', 'ты', 'к', 'у', 'же', 'вы', 'за', 'бы', 'по', 'только', 
        'ее', 'мне', 'было', 'вот', 'от', 'меня', 'еще', 'нет', 'о', 'из', 'ему', 
        'теперь', 'когда', 'даже', 'ру', 'the', 'a', 'an', 'in', 'to', 'is', 'of', 'for',
        'сообщает', 'данным', 'уже', 'еще', 'года', 'около', 'после'
    }
    
    filtered = {w for w in words if w not in stop_words and len(w) > 3}
    return filtered


def is_duplicate_post(raw_text):
    """Проверяет, публиковался ли похожий текст или новость на ту же тему за последние часы."""
    global SENT_POSTS_HISTORY
    current_time = time.time()
    
    # Очищаем устаревшую историю
    SENT_POSTS_HISTORY = [item for item in SENT_POSTS_HISTORY if current_time - item['timestamp'] < DUPLICATE_TIME_WINDOW]
    
    norm_new = normalize_text(raw_text)
    keywords_new = extract_keywords(raw_text)
    
    if not norm_new or len(norm_new.split()) < 4:
        return False  # Слишком короткий текст для надежного сравнения

    for item in SENT_POSTS_HISTORY:
        norm_existing = item['normalized_text']
        keywords_existing = item.get('keywords', set())
        
        # 1. Проверка по совпадению ключевых слов (новостной теме)
        if keywords_new and keywords_existing:
            common_keywords = keywords_new.intersection(keywords_existing)
            if len(common_keywords) >= 3:
                print(f"[АНТИДУБЛЬ] Найдена та же новость по ключевым словам ({common_keywords}). Пропускаем.")
                return True

        # 2. Считаем коэффициент схожести текстов (SequenceMatcher)
        similarity = difflib.SequenceMatcher(None, norm_new, norm_existing).ratio()
        
        if similarity >= SIMILARITY_THRESHOLD:
            print(f"[АНТИДУБЛЬ] Найден похожий пост (схожесть {int(similarity * 100)}%). Пропускаем.")
            return True
            
    # Если дубликат не найден, добавляем текущий в историю
    SENT_POSTS_HISTORY.append({
        'normalized_text': norm_new,
        'keywords': keywords_new,
        'timestamp': current_time
    })
    return False


def is_ad_or_pr(text):
    if not text:
        return False
    text_lower = text.lower()
    for pattern in SPAM_PATTERNS:
        if re.search(pattern, text_lower):
            return True
    return False


def clean_text_and_entities(text, entities):
    if not text:
        return "", None

    from telethon.tl import types

    safe_entities = []
    if entities:
        for e in entities:
            if isinstance(e, types.MessageEntityCustomEmoji):
                continue

            if isinstance(e, (types.MessageEntityTextUrl, types.MessageEntityUrl)):
                url = getattr(e, 'url', '')
                if 't.me' in url or 'telegram.me' in url or 'tg://' in url:
                    start = e.offset
                    length = e.length
                    text = text[:start] + "" + text[start + length:]
                    
                    diff = -length
                    for other in entities:
                        if other.offset > start:
                            other.offset += diff
                    continue

            safe_entities.append(e)

    text = text.replace('🥺', '')
    text = re.sub(r'https?://t\.me/\w+/?', '', text)
    text = re.sub(r't\.me/\w+/?', '', text)
    text = re.sub(r'@\w+', '', text)

    orphan_phrases = [
        r'скачать можно\s*[\.,!]?(?=\s*$)',
        r'взять можно\s*[\.,!]?(?=\s*$)',
        r'найти можно\s*[\.,!]?(?=\s*$)',
        r'перейти можно\s*[\.,!]?(?=\s*$)',
        r'ссылка:\s*$',
        r'источник:\s*$',
        r'наш канал:\s*$'
    ]
    for pattern in orphan_phrases:
        text = re.sub(pattern, '', text, flags=re.IGNORECASE)

    text = re.sub(r'\n{3,}', '\n\n', text).strip()
    return text, safe_entities if safe_entities else None


# Инициализируем клиент через StringSession для облачного хостинга на Render
client = TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH)
albums_cache = {}

@client.on(events.NewMessage(chats=SOURCE_CHANNELS))
async def handle_new_post(event):
    message = event.message
    text = message.raw_text or ""

    if is_ad_or_pr(text):
        print(f"[ПРОПУСК] Реклама или пиар в канале {event.chat_id}")
        return

    safe_entities = list(message.entities) if message.entities else None

    # Обработка альбомов (медиа-групп)
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

        await asyncio.sleep(1.5)
        
        if group_id in albums_cache:
            data = albums_cache.pop(group_id)
            msgs = data['messages']
            raw_txt = data['text']
            ent = data['entities']
            
            # Проверяем альбом на дубликаты по тексту и ключевым словам
            if raw_txt and is_duplicate_post(raw_txt):
                return
            
            print(f"[АЛЬБОМ] Публикуем альбом из {data['chat']}...")
            cleaned_text, final_entities = clean_text_and_entities(raw_txt, ent)
            media_list = [m.media for m in msgs if m.media]
            
            if not cleaned_text and not media_list:
                print("[ПРОПУСК] Альбом пуст после очистки.")
                return
            
            try:
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
                print("[УСПЕХ] Альбом опубликован!")
            except Exception as e:
                print(f"[ОШИБКА] Не удалось отправить альбом: {e}")
        return

    # Одиночные посты
    if text and is_duplicate_post(text):
        return

    print(f"[НОВЫЙ ПОСТ] Обрабатываем пост из {event.chat_id}...")
    cleaned_text, final_entities = clean_text_and_entities(text, safe_entities)

    if not cleaned_text and not message.media:
        print("[ПРОПУСК] Сообщение пустое.")
        return

    try:
        if message.media:
            await client.send_file(
                TARGET_CHANNEL,
                file=message.media,
                caption=cleaned_text if cleaned_text else None,
                formatting_entities=final_entities
            )
        else:
            await client.send_message(
                TARGET_CHANNEL, 
                cleaned_text, 
                formatting_entities=final_entities
            )
        print("[УСПЕХ] Пост опубликован в @joybyte!")
    except Exception as e:
        print(f"[ОШИБКА] Не удалось опубликовать: {e}")

# --- Веб-сервер для Render ---
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

async def main():
    print("Инициализация подключения к Telegram...")
    await client.connect()
    
    if not await client.is_user_authorized():
        print("Ошибка: сессия не авторизована.")
        return
        
    await start_web_server()
        
    print("Бот запущен и отслеживает посты (с улучшенной защитой от дубликатов)...")
        
    while True:
        try:
            await client.run_until_disconnected()
        except Exception as e:
            print(f"[СЕТЬ] Соединение потеряно ({e}). Переподключение через 5 секунд...")
            await asyncio.sleep(5)
            try:
                await client.connect()
            except:
                pass

if __name__ == '__main__':
    try:
        client.loop.run_until_complete(main())
    except KeyboardInterrupt:
        print("\nБот остановлен пользователем.")
