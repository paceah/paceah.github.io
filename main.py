import asyncio
import hashlib
import json
import logging
import os
import re
import tempfile
import threading
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from aiohttp import web
import telebot

logging.basicConfig(level=logging.INFO)
LOG = logging.getLogger('zombie-chips')
DATA = Path(os.environ.get('PACK_DATA_DIR', '/app/data/modpack' if os.name != 'nt' else 'data/modpack')).resolve()
OBJECTS = DATA / 'objects'
SNAPSHOT = DATA / 'snapshot.json'
MIRROR = os.environ.get('PACK_MIRROR', 'https://raw.githubusercontent.com/paceah/paceah.github.io/refs/heads/main').rstrip('/')
OWNER = int(os.environ.get('OWNER_TELEGRAM_ID', '0'))
TOKEN = os.environ.get('BOT_TOKEN', '')
PORT = int(os.environ.get('PORT', '8080'))
HASH = re.compile(r'^[a-f0-9]{40}$')
lock = threading.Lock()
progress = {'status': 'not started', 'error': '', 'done': 0, 'total': 0}
current = None


def request(url):
    return urlopen(Request(url, headers={'User-Agent': 'ZombieChipsPack/1.0', 'Accept-Encoding': 'identity'}), timeout=90)


def get_bytes(url):
    with request(url) as response:
        return response.read()


def catalogue(head):
    files = {}
    for category in head['policy']['categories'].values():
        for group in category.values():
            for item in group.get('files', {}).values():
                h, size = item['sha1'].lower(), int(item['size'])
                if not HASH.fullmatch(h) or size < 0 or (h in files and files[h] != size):
                    raise ValueError('Invalid file catalogue')
                files[h] = size
    return files


def valid_file(path, expected_hash, size):
    if not path.is_file() or path.stat().st_size != size:
        return False
    h = hashlib.sha1()
    with path.open('rb') as f:
        for block in iter(lambda: f.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest() == expected_hash


def download_object(h, size):
    destination = OBJECTS / h
    if valid_file(destination, h, size):
        return
    try:
        response = request(MIRROR + '/objects/' + h)
    except HTTPError as exc:
        if exc.code != 404:
            raise
        info = json.loads(get_bytes('https://api.modrinth.com/v2/version_file/' + h + '?algorithm=sha1'))
        urls = [f['url'] for f in info.get('files', []) if f.get('hashes', {}).get('sha1', '').lower() == h and f['url'].startswith('https://cdn.modrinth.com/')]
        if len(urls) != 1:
            raise ValueError('No trusted source for ' + h)
        response = request(urls[0])
    temp_name = None
    try:
        with response, tempfile.NamedTemporaryFile(dir=OBJECTS, prefix='.download-', delete=False) as tmp:
            temp_name = tmp.name
            digest = hashlib.sha1()
            length = 0
            while block := response.read(1024 * 1024):
                length += len(block)
                if length > size:
                    raise ValueError('File exceeds expected size: ' + h)
                digest.update(block)
                tmp.write(block)
        if length != size or digest.hexdigest() != h:
            raise ValueError('File checksum or length mismatch: ' + h)
        os.replace(temp_name, destination)
    finally:
        if temp_name and os.path.exists(temp_name):
            os.unlink(temp_name)


def sync_pack():
    global current
    if not lock.acquire(blocking=False):
        return False
    try:
        progress.update(status='downloading', error='', done=0, total=0)
        OBJECTS.mkdir(parents=True, exist_ok=True)
        head_raw = get_bytes(MIRROR + '/head')
        journal_raw = get_bytes(MIRROR + '/journal')
        head = json.loads(head_raw)
        files = catalogue(head)
        entries = [json.loads(line) for line in journal_raw.splitlines() if line.strip()]
        if (not entries or int(entries[-1]['seq']) != int(head['journalHead'])
                or entries[-1].get('contentToken') != head.get('contentToken')):
            raise ValueError('Head and journal do not match')
        progress['total'] = len(files)
        for h, size in files.items():
            download_object(h, size)
            progress['done'] += 1
        if get_bytes(MIRROR + '/head') != head_raw:
            raise ValueError('Pack changed during download; retry /sync')
        snapshot = {'head': head_raw.decode('utf-8'), 'journal': journal_raw.decode('utf-8'), 'files': files}
        temp = SNAPSHOT.with_suffix('.tmp')
        temp.write_text(json.dumps(snapshot, ensure_ascii=False), encoding='utf-8')
        os.replace(temp, SNAPSHOT)
        current = snapshot
        progress['status'] = 'ready'
        return True
    except Exception as exc:
        LOG.exception('Pack synchronization failed')
        progress.update(status='error', error=str(exc))
        return False
    finally:
        lock.release()


def start_sync():
    if lock.locked():
        return False
    threading.Thread(target=sync_pack, daemon=True).start()
    return True


async def serve(request_):
    snapshot = current
    if snapshot is None:
        return web.Response(status=503, text='Pack is being prepared')
    path = request_.path
    if path in ('/head', '/journal'):
        return web.Response(text=snapshot[path[1:]], content_type='application/json', headers={'Cache-Control': 'no-cache'})
    if path.startswith('/objects/'):
        h = path[len('/objects/'):]
        if HASH.fullmatch(h) and h in snapshot['files'] and (OBJECTS / h).is_file():
            return web.FileResponse(OBJECTS / h, headers={'Content-Type': 'application/octet-stream', 'Cache-Control': 'public, max-age=31536000, immutable, no-transform'})
    raise web.HTTPNotFound()


def run_bot():
    if not TOKEN or not OWNER:
        LOG.warning('BOT_TOKEN or OWNER_TELEGRAM_ID not set; Telegram management disabled')
        return
    bot = telebot.TeleBot(TOKEN)

    @bot.message_handler(commands=['start', 'status', 'sync'])
    def handle(message):
        if message.chat.type != 'private' or message.from_user.id != OWNER:
            return
        command = message.text.split()[0].split('@')[0].lower()
        if command == '/sync':
            bot.reply_to(message, 'Обновление запущено.' if start_sync() else 'Обновление уже выполняется.')
        else:
            text = f"Статус: {progress['status']}\nФайлы: {progress['done']}/{progress['total']}\nДоступна версия: {'да' if current else 'нет'}"
            if progress['error']:
                text += '\nОшибка: ' + progress['error'][:500]
            bot.reply_to(message, text + '\n\n/sync — загрузить новую версию')
    bot.infinity_polling(skip_pending=True, timeout=30, long_polling_timeout=30)


def create_app():
    app = web.Application()
    app.router.add_route('GET', '/{path:.*}', serve)
    app.router.add_route('HEAD', '/{path:.*}', serve)
    return app


if __name__ == '__main__':
    DATA.mkdir(parents=True, exist_ok=True)
    OBJECTS.mkdir(parents=True, exist_ok=True)
    if SNAPSHOT.exists():
        try:
            saved = json.loads(SNAPSHOT.read_text(encoding='utf-8'))
            if all((OBJECTS / h).is_file() and (OBJECTS / h).stat().st_size == size for h, size in saved['files'].items()):
                current = saved
                progress['status'] = 'ready'
        except (ValueError, KeyError, OSError):
            LOG.exception('Saved snapshot invalid')
    start_sync()
    threading.Thread(target=run_bot, daemon=True).start()
    web.run_app(create_app(), host='0.0.0.0', port=PORT)
