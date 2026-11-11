import asyncio
import os
import re
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
    'Dota2',
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
            # Игнорируем только кастомные эмодзи из разметки
            if isinstance(e, types.MessageEntityCustomEmoji):
                continue

            # Фильтруем Telegram-ссылки внутри текста
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

    # Очистка только явных мусорных артефактов, не трогая цитаты и списки
    text = text.replace('🥺', '')

    # Удаляем прямые упоминания через @ и чистые t.me ссылки из текста
    text = re.sub(r'https?://t\.me/\w+/?', '', text)
    text = re.sub(r't\.me/\w+/?', '', text)
    text = re.sub(r'@\w+', '', text)

    # Подчищаем мусорные висячие фразы в конце
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

    # Обработка альбомов
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

# --- Веб-сервер для Render, чтобы он не ругался на отсутствие открытых портов ---
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
        
    # Запускаем фоновый веб-сервер
    await start_web_server()
        
    print("Бот запущен и отслеживает посты (с сохранением цитат и разметки)...")
    
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
