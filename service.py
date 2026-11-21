"""Retro Library: LAN-only library, missing-file recovery and verified backups.

Python 3.12 HTTP service; Pillow handles metadata images. Never executes uploaded content.
"""
import contextlib
import hashlib
import http.client
import io
import json
import mimetypes
import os
from pathlib import Path, PurePosixPath
import re
import secrets
import shutil
import tarfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import quote, unquote, urlsplit, parse_qs
import uuid
import sys

metadata_manager = None

ROOT = Path(os.getenv('LIBRARY_ROOT', '/library'))
STATE = Path(os.getenv('STATE_ROOT', '/state'))
APP = Path(__file__).parent
TARGET = os.getenv('APPLE_TV_HOST', '127.0.0.1')
INTERVAL = int(os.getenv('SYNC_INTERVAL', '60'))
PORT = int(os.getenv('PORT', '8080'))
SYSTEMS = {
    'NES': ('Nintendo - Nintendo Entertainment System', 'nestopia', 'Nintendo - NES / Famicom (Nestopia UE)', {'.nes', '.fds', '.zip'}),
    'SNES': ('Nintendo - Super Nintendo Entertainment System', 'snes9x', 'Nintendo - SNES / SFC (Snes9x)', {'.smc', '.sfc', '.zip'}),
    'Mega Drive': ('Sega - Mega Drive - Genesis', 'genesis.plus.gx', 'Sega - MS/GG/MD/CD (Genesis Plus GX)', {'.md', '.gen', '.smd', '.bin', '.zip'}),
    'PlayStation': ('Sony - PlayStation', 'pcsx.rearmed', 'Sony - PlayStation (PCSX ReARMed)', {'.cue', '.bin', '.chd', '.iso', '.pbp', '.m3u'}),
    'DOS': ('DOS', 'dosbox.pure', 'DOS (DOSBox-Pure)', {'.dosz', '.zip'}),
}
BIOS_MD5 = {'scph5500.bin': '8dd7d5296a650fac7319bce665a6a53c', 'scph5501.bin': '490f666e1afb15b7362b406ed1cea246', 'scph5502.bin': '32736f17079d0b2b7024407c39bd3050'}
lock = threading.RLock()
sync_lock = threading.Lock()
backup_lock = threading.Lock()
wake = threading.Event()
csrf = secrets.token_urlsafe(32)
status = {'online': None, 'phase': 'starting', 'checked': None, 'error': '', 'files': {}, 'events': [], 'backup': {}, 'restore': {}}
hash_cache = {}


def now():
    return time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())


def read_json(path, default):
    try:
        return json.loads(path.read_text('utf-8'))
    except FileNotFoundError:
        return default


def atomic_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + '.tmp')
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), 'utf-8')
    os.replace(tmp, path)


def digest(path, algorithm='sha256'):
    stat = path.stat()
    key = (str(path), stat.st_size, stat.st_mtime_ns, algorithm)
    if key not in hash_cache:
        with path.open('rb') as f:
            hash_cache[key] = hashlib.file_digest(f, algorithm).hexdigest()
    return hash_cache[key]


def safe_path(root, relative):
    if not relative or '\\' in relative or '\x00' in relative:
        raise ValueError('Недопустимый путь')
    parts = PurePosixPath(relative).parts
    if PurePosixPath(relative).is_absolute() or any(p in ('.', '..') or p.startswith('.') for p in parts):
        raise ValueError('Недопустимый путь')
    result = root.joinpath(*parts)
    if not result.resolve().is_relative_to(root.resolve()):
        raise ValueError('Путь выходит за пределы библиотеки')
    cursor = result
    while cursor != root:
        if cursor.is_symlink():
            raise ValueError('Символические ссылки не поддерживаются')
        cursor = cursor.parent
    return result


def filename(name):
    if not name or len(name.encode('utf-8')) > 220 or name in ('.', '..') or re.search(r'[\\/\x00-\x1f]', name) or name.startswith('.'):
        raise ValueError('Недопустимое имя файла')
    return name


def event(message):
    with lock:
        status['events'] = ([{'time': now(), 'message': message}] + status['events'])[:80]
    print(now(), message, flush=True)


def inventory():
    result = {}
    for top in ('downloads', 'system', 'playlists', 'thumbnails'):
        base = ROOT / top
        if base.exists():
            for p in base.rglob('*'):
                rel = p.relative_to(ROOT).as_posix()
                if p.is_file() and not p.is_symlink() and not any(x.startswith('.') for x in p.relative_to(ROOT).parts):
                    safe_path(ROOT, rel)
                    result[rel] = p.stat().st_size
    return result


def catalog():
    games = []
    selection = read_json(STATE / 'selection.json', {})
    metadata = read_json(STATE / 'metadata.json', {})
    for system, (db, core, core_name, extensions) in SYSTEMS.items():
        playlist = read_json(ROOT / 'playlists' / (db + '.lpl'), {'items': []})
        for item in playlist['items']:
            rel = item['path'].split('RetroArch/', 1)[-1]
            artlabel = re.sub(r'[&*/:`<>?\\|]', '_', item['label'])
            cover = 'thumbnails/' + db + '/Named_Boxarts/' + artlabel + '.png'
            dependencies = []
            if Path(rel).suffix.lower() in ('.cue', '.m3u') and (ROOT / rel).is_file():
                content = (ROOT / rel).read_text('utf-8-sig', errors='replace')
                refs = re.findall(r'FILE\s+"([^"]+)"', content, re.I) if rel.lower().endswith('.cue') else [v.strip() for v in content.splitlines() if v.strip() and not v.startswith('#')]
                dependencies = [(PurePosixPath(rel).parent / ref).as_posix() for ref in refs]
            games.append({'system': system, 'label': item['label'], 'path': rel, 'dependencies': dependencies, 'selected': selection.get(rel, True), 'metadata': metadata.get(rel, {}), 'cover': cover if (ROOT / cover).is_file() else None})
    return games


def sync_sources():
    """Derive device playlists; the full NAS library is never reduced by selection."""
    files = inventory()
    selection = read_json(STATE / 'selection.json', {})
    games = catalog()
    selected, excluded = set(), set()
    for game in games:
        paths = {game['path'], *game['dependencies']}
        # Resolve nested M3U -> CUE -> BIN dependencies without following external paths.
        pending = list(paths)
        for rel in pending:
            if len(pending) > 1000:
                raise ValueError('Слишком много зависимостей диска')
            local = safe_path(ROOT, rel)
            if local.is_file() and local.suffix.lower() in ('.cue', '.m3u'):
                content = local.read_text('utf-8-sig', errors='replace')
                refs = re.findall(r'FILE\s+"([^"]+)"', content, re.I) if local.suffix.lower() == '.cue' else [l.strip() for l in content.splitlines() if l.strip() and not l.startswith('#')]
                for ref in refs:
                    dep = (PurePosixPath(rel).parent / ref).as_posix()
                    safe_path(ROOT, dep)
                    if dep not in paths:
                        paths.add(dep)
                        pending.append(dep)
        db = SYSTEMS[game['system']][0]
        label = re.sub(r'[&*/:`<>?\\|]', '_', game['label'])
        paths.update(f'thumbnails/{db}/{kind}/{label}.png' for kind in ('Named_Boxarts', 'Named_Snaps', 'Named_Titles'))
        (selected if game['selected'] else excluded).update(paths)
    excluded.difference_update(selected)  # Shared tracks stay while any selected game needs them.
    sources = {rel: safe_path(ROOT, rel) for rel in files if rel not in excluded and (rel in selected or selection.get(rel, True))}
    if games:
        sources = {rel: p for rel, p in sources.items() if not rel.startswith('thumbnails/') or rel in selected}
    for system, (db, *_rest) in SYSTEMS.items():
        rel = 'playlists/' + db + '.lpl'
        if rel not in files:
            continue
        data = read_json(ROOT / rel, {})
        data['items'] = [i for i in data['items'] if selection.get(i['path'].split('RetroArch/', 1)[-1], True)]
        target = STATE / 'device-playlists' / (db + '.lpl')
        if not target.exists() or read_json(target, {}) != data:
            atomic_json(target, data)
        sources[rel] = target
    return sources


def set_selection(paths, selected):
    if not isinstance(paths, list) or not isinstance(selected, bool):
        raise ValueError('Нужны список игр и состояние выбора')
    known = {g['path'] for g in catalog()}
    if not paths or len(paths) > 2000 or any(p not in known for p in paths):
        raise ValueError('Неизвестная игра')
    with lock:
        current = read_json(STATE / 'selection.json', {})
        current.update({p: selected for p in paths})
        atomic_json(STATE / 'selection.json', current)
    wake.set()


class DAV:
    def __init__(self):
        self.directories = {''}

    def request(self, method, path, body=None, headers=None, port=8080, allow=()):
        conn = http.client.HTTPConnection(TARGET, port, timeout=45)
        try:
            conn.request(method, quote(path, safe='/%?=&'), body=body, headers=headers or {})
            response = conn.getresponse()
            data = response.read()
            if 'RetroArch' not in response.getheader('Server', ''):
                raise RuntimeError('По адресу Apple TV отвечает другой сервер')
            if response.status >= 400 and response.status not in allow:
                raise RuntimeError(f'Apple TV: HTTP {response.status}, {method} {path}')
            return response.status, data
        finally:
            conn.close()

    def listing(self, path):
        code, body = self.request('GET', '/list?path=' + quote('/' + path.strip('/') + '/', safe=''), port=80, allow=(404,))
        if code == 404:
            return None
        entries = json.loads(body)
        if not isinstance(entries, list):
            raise RuntimeError('Некорректный ответ списка файлов Apple TV')
        for item in entries:
            if not isinstance(item, dict) or not isinstance(item.get('name'), str) or not isinstance(item.get('path'), str):
                raise RuntimeError('Некорректная запись списка Apple TV')
            filename(item['name'])
        return entries

    def mkdirs(self, relative):
        parts = PurePosixPath(relative).parts
        for i in range(1, len(parts) + 1):
            current = '/'.join(parts[:i])
            if current in self.directories:
                continue
            parent = '/'.join(parts[:i - 1])
            exists = lambda: any(e['name'] == parts[i - 1] and 'size' not in e for e in self.listing(parent) or [])
            if not exists():
                code, _ = self.request('MKCOL', '/' + current, allow=(405, 500))
                # GCDWebDAVServer returns 500 for an existing directory. Confirm
                # existence explicitly; never mask a real mkdir failure.
                if code >= 400 and not exists():
                    raise RuntimeError(f'Не удалось создать папку Apple TV: {current} (HTTP {code})')
            self.directories.add(current)

    def remote_hash(self, rel, destination=None):
        conn = http.client.HTTPConnection(TARGET, 8080, timeout=45)
        h = hashlib.sha256()
        size = 0
        try:
            conn.request('GET', '/' + quote(rel, safe='/'))
            response = conn.getresponse()
            if response.status != 200 or 'RetroArch' not in response.getheader('Server', ''):
                raise RuntimeError(f'Чтение Apple TV: HTTP {response.status}: {rel}')
            with (destination.open('wb') if destination else contextlib.nullcontext()) as f:
                while chunk := response.read(1024 * 1024):
                    h.update(chunk)
                    size += len(chunk)
                    if f:
                        f.write(chunk)
            return h.hexdigest(), size
        finally:
            conn.close()

    def publish(self, rel, local, overwrite=False):
        staging = 'downloads/RetroArch-Sync-Temp/' + hashlib.sha256(rel.encode()).hexdigest() + '.part'
        self.mkdirs(str(PurePosixPath(staging).parent))
        self.mkdirs(str(PurePosixPath(rel).parent))
        with local.open('rb') as f:
            self.request('PUT', '/' + staging, f, {'Content-Length': str(local.stat().st_size), 'Content-Type': 'application/octet-stream'})
        actual, size = self.remote_hash(staging)
        if actual != digest(local) or size != local.stat().st_size:
            raise RuntimeError('Контрольная сумма после передачи не совпадает: ' + rel)
        self.request('MOVE', '/' + staging, headers={'Destination': 'http://' + TARGET + ':8080/' + quote(rel, safe='/'), 'Overwrite': 'T' if overwrite else 'F'})


def scan_once():
    if not sync_lock.acquire(blocking=False):
        return
    connected = False
    try:
        dav = DAV()
        with lock:
            status.update(phase='scanning', error='')
            sources = sync_sources()
            files = {rel: p.stat().st_size for rel, p in sources.items()}
        dav.listing('')  # Identity check: never interpret an unreachable server as an empty library.
        connected = True
        listing_cache = {}
        missing = []
        results = {}
        acknowledged = read_json(STATE / 'acknowledged.json', {})
        for rel, size in files.items():
            parent = str(PurePosixPath(rel).parent)
            if parent not in listing_cache:
                entries = dav.listing(parent)
                listing_cache[parent] = {x['name']: x for x in entries or []}
            remote = listing_cache[parent].get(PurePosixPath(rel).name)
            state = 'missing' if remote is None else ('present' if remote.get('size') == size else 'conflict')
            local = sources[rel]
            if rel.startswith('playlists/') and remote is not None:
                remote_digest, _ = dav.remote_hash(rel)
                with lock:
                    local_digest = digest(local)
                if remote_digest == local_digest:
                    acknowledged[rel] = local_digest
                    state = 'present'
                elif remote_digest == acknowledged.get(rel):
                    state = 'update'
                else:
                    state = 'conflict'
            results[rel] = {'state': state, 'size': size, 'remote_size': remote.get('size') if remote else None}
            if state in ('missing', 'update'):
                missing.append((rel, state))
        with lock:
            status.update(online=True, files=results, checked=now(), phase='syncing' if missing else 'idle')
        # Content first, playlists last. Existing ROMs, BIOS and artwork are never overwritten.
        for rel, previous in sorted(missing, key=lambda x: (x[0].startswith('playlists/'), x[0])):
            with lock:
                status['current'] = rel
                if rel.startswith('playlists/'):
                    # A playlist must not advertise a file that failed to upload.
                    game_paths = [i['path'].split('RetroArch/', 1)[-1] for i in read_json(sources[rel], {})['items']]
                    if any(results.get(p, {}).get('state') != 'present' for p in game_paths):
                        continue
                # Recheck selection after a long transfer before starting the next file.
                latest_sources = sync_sources()
                if rel not in latest_sources:
                    continue
                local = latest_sources[rel]
                if previous == 'update':
                    current, _ = dav.remote_hash(rel)
                    if current != acknowledged.get(rel):
                        results[rel]['state'] = 'conflict'
                        continue
                    # Retain the previously acknowledged playlist before an intentional library update.
                    backup = STATE / 'playlist-history' / (uuid.uuid4().hex + '.lpl')
                    backup.parent.mkdir(exist_ok=True)
                    dav.remote_hash(rel, backup)
                dav.publish(rel, local, overwrite=previous == 'update')
                results[rel]['state'] = 'present'
                results[rel]['remote_size'] = local.stat().st_size
                if rel.startswith('playlists/'):
                    acknowledged[rel] = digest(local)
                atomic_json(STATE / 'acknowledged.json', acknowledged)
            event(('Обновлён плейлист: ' if previous == 'update' else 'Восстановлен: ') + rel)
        with lock:
            atomic_json(STATE / 'acknowledged.json', acknowledged)
            status.update(phase='idle', current=None, checked=now(), last_success=now())
    except Exception as exc:
        with lock:
            status.update(online=connected, phase='error' if connected else 'waiting', current=None, error=str(exc), checked=now())
        event(('Ошибка синхронизации: ' if connected else 'Ожидание RetroArch: ') + str(exc))
    finally:
        sync_lock.release()


def sync_loop():
    while True:
        scan_once()
        wake.wait(INTERVAL)
        wake.clear()


def add_playlist_entry(system, name):
    db, core, core_name, _ = SYSTEMS[system]
    if system == 'PlayStation' and Path(name).suffix.lower() == '.bin':
        return  # Bin tracks launch through the matching cue, not as separate games.
    rel = f'downloads/Library/{system}/{name}'
    playlist_path = ROOT / 'playlists' / (db + '.lpl')
    core_path = f':/Frameworks/{core}.libretro.framework'
    playlist = read_json(playlist_path, {'version': '1.5', 'default_core_path': core_path, 'default_core_name': core_name, 'label_display_mode': 0, 'right_thumbnail_mode': 4, 'left_thumbnail_mode': 2, 'sort_mode': 0, 'items': []})
    if not any(i['path'].endswith('/' + rel) for i in playlist['items']):
        playlist['items'].append({'path': '~/Library/Caches/RetroArch/' + rel, 'label': Path(name).stem, 'core_path': core_path, 'core_name': core_name, 'crc32': 'DETECT', 'db_name': db + '.lpl'})
        playlist['items'].sort(key=lambda i: i['label'].casefold())
        atomic_json(playlist_path, playlist)


def capture_apple_tv():
    """Capture saves, states and config. A partial capture never replaces the last good one."""
    dav = DAV()
    dav.listing('')
    temp = STATE / ('capture-' + uuid.uuid4().hex)
    temp.mkdir()
    count = 0
    try:
        root_entries = dav.listing('')
        for entry in root_entries:
            if 'size' in entry and entry['name'] == 'retroarch.cfg':
                dav.remote_hash(entry['name'], temp / entry['name'])
                count += 1
        def walk(rel, depth=0):
            nonlocal count
            if depth > 20 or count > 20000:
                raise ValueError('Слишком много файлов в сохранениях')
            for entry in dav.listing(rel) or []:
                child = rel + '/' + entry['name']
                dest = safe_path(temp, child)
                if 'size' in entry:
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    dav.remote_hash(child, dest)
                    count += 1
                else:
                    walk(child, depth + 1)
        for root in ('saves', 'states', 'config', 'remaps'):
            walk(root)
        manifest = {'captured': now(), 'files': count, 'target': TARGET}
        atomic_json(temp / 'capture.json', manifest)
        final = STATE / 'captures' / (time.strftime('%Y%m%d-%H%M%S') + '-' + uuid.uuid4().hex[:6])
        final.parent.mkdir(exist_ok=True)
        os.replace(temp, final)
        atomic_json(STATE / 'capture-latest.json', {'directory': final.name, **manifest})
        return manifest
    finally:
        if temp.exists():
            shutil.rmtree(temp)


def backup_destinations():
    locations = [{'id': 'download', 'label': 'Скачать на компьютер / внешний диск', 'path': str(STATE / 'backups')}]
    # Only explicit bind mounts are writable destinations; never accept arbitrary host paths from the UI.
    for key, value in json.loads(os.getenv('BACKUP_DESTINATIONS', '{}')).items():
        p = Path(value['path'])
        if p.is_dir():
            locations.append({'id': key, 'label': value['label'], 'path': str(p)})
    for item in locations:
        p = Path(item['path'])
        p.mkdir(parents=True, exist_ok=True)
        item['free'] = shutil.disk_usage(p).free
    return locations


def backup_files():
    files = [(ROOT / rel, 'library/' + rel) for rel in inventory()]
    for p in APP.iterdir():
        if p.is_file() and (p.suffix in ('.py', '.html', '.js', '.css', '.json', '.txt', '.md', '.yaml') or p.name == 'Dockerfile'):
            files.append((p, 'service/' + p.name))
    for name in ('acknowledged.json', 'capture-latest.json', 'selection.json', 'metadata.json'):
        if (STATE / name).is_file():
            files.append((STATE / name, 'state/' + name))
    latest = read_json(STATE / 'capture-latest.json', {})
    if latest:
        base = safe_path(STATE / 'captures', latest['directory'])
        for p in base.rglob('*'):
            if p.is_file() and not p.is_symlink():
                files.append((p, 'apple-tv/' + p.relative_to(base).as_posix()))
    return files


def verify_archive(path):
    with tarfile.open(path, 'r:') as archive:
        manifest_file = archive.extractfile('manifest.json')
        if manifest_file is None:
            raise ValueError('Нет манифеста резервной копии')
        manifest = json.load(manifest_file)
        expected = manifest['files']
        seen = set()
        for member in archive:
            if member.name == 'manifest.json':
                continue
            if not member.isfile() or member.name not in expected or member.name in seen:
                raise ValueError('Некорректный состав архива')
            h = hashlib.file_digest(archive.extractfile(member), 'sha256').hexdigest()
            if h != expected[member.name]['sha256'] or member.size != expected[member.name]['size']:
                raise ValueError('Ошибка проверки: ' + member.name)
            seen.add(member.name)
        if seen != set(expected):
            raise ValueError('Архив неполон')
        return manifest


def backup_path(name):
    record = next((r for r in read_json(STATE / 'backups.json', []) if r['name'] == name), None)
    if not record:
        raise ValueError('Резервная копия не найдена')
    target = next((x for x in backup_destinations() if x['id'] == record['destination']), None)
    if not target:
        raise ValueError('Носитель с резервной копией недоступен')
    return safe_path(Path(target['path']), name)


def delete_backup(name):
    if not backup_lock.acquire(blocking=False):
        raise ValueError('Дождитесь завершения резервного копирования или восстановления')
    try:
        with lock:
            path = backup_path(name)
            # Remove only a registered archive; never accept a filesystem path from the client.
            path.unlink(missing_ok=True)
            atomic_json(STATE / 'backups.json', [b for b in read_json(STATE / 'backups.json', []) if b['name'] != name])
            if status['restore'].get('name') == name:
                status['restore'] = {}
            if status['backup'].get('name') == name:
                status['backup'] = {}
        event('Удалена резервная копия: ' + name)
    finally:
        backup_lock.release()


def restore_member(name):
    if name.startswith('library/'):
        rel = name[len('library/'):]
        safe_path(ROOT, rel)
        if PurePosixPath(rel).parts[0] not in ('downloads', 'system', 'playlists', 'thumbnails'):
            raise ValueError('Недопустимая папка библиотеки в архиве')
        return 'library', rel
    if name.startswith('apple-tv/'):
        rel = name[len('apple-tv/'):]
        safe_path(STATE, rel)
        if rel == 'capture.json':
            return 'metadata', rel
        if PurePosixPath(rel).parts[0] not in ('saves', 'states', 'config', 'remaps') and rel != 'retroarch.cfg':
            raise ValueError('Недопустимая папка сохранений в архиве')
        return 'apple-tv', rel
    return 'skip', name  # Never restore or execute service code supplied by an archive.


def restore_plan(manifest):
    plan = {'add': 0, 'replace': 0, 'unchanged': 0, 'saves': 0, 'bytes': 0, 'capture': manifest.get('capture')}
    for name, item in manifest['files'].items():
        kind, rel = restore_member(name)
        if kind == 'library':
            local = safe_path(ROOT, rel)
            action = 'add' if not local.exists() else 'unchanged' if digest(local) == item['sha256'] else 'replace'
            plan[action] += 1
            plan['bytes'] += item['size']  # Complete staging area, including unchanged files.
            if action == 'replace':
                plan['bytes'] += local.stat().st_size
            elif action == 'add':
                plan['bytes'] += item['size']
        elif kind == 'apple-tv':
            plan['saves'] += 1
            plan['bytes'] += item['size']
    return plan


def prepare_restore(name):
    if not backup_lock.acquire(blocking=False):
        return
    try:
        status['restore'] = {'phase': 'checking', 'name': name}
        manifest = verify_archive(backup_path(name))
        with lock:
            plan = restore_plan(manifest)
        status['restore'] = {'phase': 'ready', 'name': name, **plan}
    except Exception as exc:
        status['restore'] = {'phase': 'error', 'error': str(exc)}
    finally:
        backup_lock.release()


def restore_backup(name, restore_saves=False):
    if not backup_lock.acquire(blocking=False):
        return
    history = STATE / 'restore-history' / (time.strftime('%Y%m%d-%H%M%S') + '-' + uuid.uuid4().hex[:6])
    staging = STATE / ('restore-' + uuid.uuid4().hex)
    try:
        status['restore'] = {'phase': 'verifying', 'name': name}
        path = backup_path(name)
        manifest = verify_archive(path)
        with sync_lock, lock:
            plan = restore_plan(manifest)
            if shutil.disk_usage(ROOT).free < plan['bytes'] + 100 * 1024**2 or shutil.disk_usage(STATE).free < plan['bytes'] + 100 * 1024**2:
                raise ValueError('Недостаточно места для восстановления и сохранения текущих версий')
            staging.mkdir()
            # Stage data only. Code and deployment settings from the archive are not applied.
            with tarfile.open(path) as archive:
                for member in archive:
                    kind, rel = restore_member(member.name)
                    if kind not in ('library', 'apple-tv'):
                        continue
                    target = safe_path(staging, kind + '/' + rel)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with archive.extractfile(member) as source, target.open('xb') as output:
                        shutil.copyfileobj(source, output)
            if restore_saves and plan['saves']:
                status['restore']['phase'] = 'capturing-current'
                capture_apple_tv()  # Required rollback copy: failure aborts before any changes.
            history.mkdir(parents=True)
            journal = {'archive': name, 'started': now(), 'changed': [], 'apple_tv': [], 'rollback': str(history)}
            atomic_json(history / 'restore.json', journal)
            status['restore'].update(phase='restoring', total=plan['add'] + plan['replace'], done=0)
            for member, info in manifest['files'].items():
                kind, rel = restore_member(member)
                if kind != 'library':
                    continue
                dest = safe_path(ROOT, rel)
                if dest.exists() and digest(dest) == info['sha256']:
                    continue
                if dest.exists():
                    old = safe_path(history, 'library/' + rel)
                    old.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(dest, old)
                dest.parent.mkdir(parents=True, exist_ok=True)
                temp = dest.with_name('.restore-' + uuid.uuid4().hex)
                try:
                    shutil.copyfile(staging / 'library' / rel, temp)
                    os.replace(temp, dest)
                finally:
                    temp.unlink(missing_ok=True)
                journal['changed'].append(rel)
                atomic_json(history / 'restore.json', journal)
                status['restore']['done'] = len(journal['changed'])
            capture_dir = staging / 'apple-tv'
            if capture_dir.exists():
                restored_capture = STATE / 'captures' / ('restored-' + uuid.uuid4().hex)
                restored_capture.parent.mkdir(exist_ok=True)
                os.replace(capture_dir, restored_capture)
                archived_capture = manifest.get('capture') or {'captured': manifest['created'], 'files': plan['saves']}
                atomic_json(restored_capture / 'capture.json', archived_capture)
                journal['restored_capture'] = str(restored_capture)
                if restore_saves:
                    status['restore']['phase'] = 'sending-saves'
                    dav = DAV()
                    for local in restored_capture.rglob('*'):
                        if not local.is_file() or local.name == 'capture.json':
                            continue
                        rel = local.relative_to(restored_capture).as_posix()
                        parent = str(PurePosixPath(rel).parent)
                        existing = next((e for e in dav.listing('' if parent == '.' else parent) or [] if e['name'] == local.name), None)
                        if existing is not None:
                            rollback = safe_path(history, 'apple-tv/' + rel)
                            rollback.parent.mkdir(parents=True, exist_ok=True)
                            remote_digest, _ = dav.remote_hash(rel, rollback)
                            if remote_digest == digest(local):
                                continue
                            if dav.remote_hash(rel)[0] != remote_digest:
                                raise RuntimeError('Файл на Apple TV изменился во время восстановления: ' + rel)
                        dav.publish(rel, local, overwrite=existing is not None)
                        journal['apple_tv'].append(rel)
                        atomic_json(history / 'restore.json', journal)
                # An imported old save never replaces the pointer to a newer live capture.
                latest = read_json(STATE / 'capture-latest.json', {})
                if latest.get('captured', '') <= archived_capture.get('captured', ''):
                    atomic_json(STATE / 'capture-latest.json', {**archived_capture, 'directory': restored_capture.name})
            journal['finished'] = now()
            atomic_json(history / 'restore.json', journal)
            status['restore'] = {'phase': 'complete', 'name': name, 'restored': len(journal['changed']), 'saves_restored': len(journal['apple_tv']), 'saved_capture': plan['saves'], 'rollback': str(history), 'finished': now()}
            atomic_json(STATE / 'last-restore.json', status['restore'])
        event(f'Восстановление завершено: {len(journal["changed"])} файлов NAS, {len(journal["apple_tv"])} файлов Apple TV')
        wake.set()
    except Exception as exc:
        status['restore'] = {'phase': 'error', 'error': str(exc), 'rollback': str(history) if history.exists() else None}
        event('Ошибка восстановления: ' + str(exc))
    finally:
        if staging.exists():
            shutil.rmtree(staging)
        backup_lock.release()


def make_backup(destination, capture=True):
    if not backup_lock.acquire(blocking=False):
        raise ValueError('Резервное копирование уже выполняется')
    try:
        targets = {x['id']: x for x in backup_destinations()}
        if destination not in targets:
            raise ValueError('Носитель недоступен')
        with lock:
            status['backup'] = {'phase': 'capturing', 'started': now(), 'error': ''}
        capture_note = ''
        if capture:
            try:
                capture_apple_tv()
            except Exception as exc:
                capture_note = 'Свежие сохранения не получены: ' + str(exc)
        with lock:
            files = backup_files()
            size = sum(p.stat().st_size for p, _ in files)
            base = Path(targets[destination]['path'])
            if shutil.disk_usage(base).free < size + 100 * 1024 * 1024:
                raise ValueError('Недостаточно места для резервной копии')
            name = 'retro-library-' + time.strftime('%Y%m%d-%H%M%S') + '-' + uuid.uuid4().hex[:6] + '.tar'
            partial = base / (name + '.partial')
            manifest = {'version': 1, 'created': now(), 'apple_tv_host': TARGET, 'sync_interval': INTERVAL, 'capture': read_json(STATE / 'capture-latest.json', None), 'capture_note': capture_note, 'files': {}}
            status['backup'].update(phase='copying', total=len(files), done=0, note=capture_note)
            try:
                with tarfile.open(partial, 'w', format=tarfile.PAX_FORMAT) as archive:
                    for i, (p, arcname) in enumerate(files):
                        manifest['files'][arcname] = {'size': p.stat().st_size, 'sha256': digest(p)}
                        archive.add(p, arcname=arcname, recursive=False)
                        status['backup'].update(done=i + 1, current=arcname)
                    content = json.dumps(manifest, ensure_ascii=False, indent=2).encode()
                    info = tarfile.TarInfo('manifest.json')
                    info.size = len(content)
                    archive.addfile(info, io.BytesIO(content))
                status['backup']['phase'] = 'verifying'
                verify_archive(partial)
                os.replace(partial, base / name)
                record = {'name': name, 'destination': destination, 'created': now(), 'size': (base / name).stat().st_size, 'verified': True, 'capture': manifest['capture'], 'note': capture_note, 'sha256': digest(base / name)}
                history = read_json(STATE / 'backups.json', [])
                atomic_json(STATE / 'backups.json', [record] + history)
                status['backup'] = {'phase': 'complete', **record}
            finally:
                partial.unlink(missing_ok=True)
        event('Резервная копия создана и проверена: ' + name)
    except Exception as exc:
        status['backup'] = {'phase': 'error', 'error': str(exc)}
        event('Ошибка резервного копирования: ' + str(exc))
    finally:
        backup_lock.release()


def state_snapshot():
    # Reading the progress object is intentionally independent of the library lock:
    # archive creation and large uploads must not block the UI.
    snap = json.loads(json.dumps(status))
    files = inventory()
    snap.update(csrf=csrf, target=TARGET, interval=INTERVAL, systems=list(SYSTEMS), games=catalog(), total_bytes=sum(files.values()), total_files=len(files), destinations=backup_destinations(), backups=read_json(STATE / 'backups.json', []), capture=read_json(STATE / 'capture-latest.json', None))
    snap['inventory'] = [{'path': p, 'size': size, **snap['files'].get(p, {'state': 'unknown'})} for p, size in files.items()]
    snap['bios'] = [{'path': p, 'size': size, 'md5': digest(ROOT / p, 'md5'), 'known_good': digest(ROOT / p, 'md5') == BIOS_MD5.get(Path(p).name.lower()) if Path(p).name.lower() in BIOS_MD5 else None} for p, size in files.items() if p.startswith('system/')]
    snap['metadata_queue'] = metadata_manager.queue.qsize() if metadata_manager else 0
    snap['metadata_current'] = metadata_manager.running if metadata_manager else None
    return snap


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def log_message(self, fmt, *args):
        if '/api/state' not in str(args):
            super().log_message(fmt, *args)

    def send_bytes(self, code, content, content_type='application/json; charset=utf-8', extra=None):
        self.send_response(code)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(content)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(content)

    def json(self, obj, code=200):
        self.send_bytes(code, json.dumps(obj, ensure_ascii=False).encode())

    def send_file(self, path, attachment=False):
        self.send_response(200)
        self.send_header('Content-Type', mimetypes.guess_type(path.name)[0] or 'application/octet-stream')
        self.send_header('Content-Length', str(path.stat().st_size))
        self.send_header('X-Content-Type-Options', 'nosniff')
        if attachment:
            self.send_header('Content-Disposition', "attachment; filename*=UTF-8''" + quote(path.name))
        self.end_headers()
        with path.open('rb') as f:
            shutil.copyfileobj(f, self.wfile, 1024 * 1024)

    def do_GET(self):
        parsed = urlsplit(self.path)
        try:
            if parsed.path == '/api/state':
                self.json(state_snapshot())
            elif parsed.path == '/health':
                self.json({'ok': True})
            elif parsed.path == '/api/backup/download':
                name = parse_qs(parsed.query).get('name', [''])[0]
                self.send_file(backup_path(name), True)
            elif parsed.path.startswith('/cover/'):
                rel = unquote(parsed.path[len('/cover/'):])
                if not rel.startswith('thumbnails/') or not rel.endswith('.png'):
                    raise ValueError('Недопустимый путь')
                self.send_file(safe_path(ROOT, rel))
            elif parsed.path in ('/', '/app.js', '/library.js', '/style.css'):
                p = APP / ('index.html' if parsed.path == '/' else parsed.path[1:])
                self.send_bytes(200, p.read_bytes(), mimetypes.guess_type(p.name)[0] + '; charset=utf-8')
            else:
                self.json({'error': 'Не найдено'}, 404)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as exc:
            self.json({'error': str(exc)}, 400)

    def do_POST(self):
        parsed = urlsplit(self.path)
        try:
            origin = self.headers.get('Origin')
            if (origin and urlsplit(origin).netloc != self.headers.get('Host')) or self.headers.get('Sec-Fetch-Site') == 'cross-site' or not secrets.compare_digest(self.headers.get('X-CSRF-Token', ''), csrf):
                self.close_connection = True
                return self.json({'error': 'Запрос отклонён: откройте UI заново'}, 403)
            if self.headers.get('Transfer-Encoding'):
                raise ValueError('Нужен Content-Length')
            length = int(self.headers.get('Content-Length', '0'))
            if parsed.path == '/api/backup/import':
                self.import_backup(length)
                return
            if parsed.path == '/api/upload':
                if not 0 < length <= 8 * 1024**3:
                    raise ValueError('Допустимый размер файла: от 1 байта до 8 ГБ')
                self.upload(parse_qs(parsed.query), length)
                return
            if not 0 <= length <= (256 * 1024 if parsed.path == '/api/selection' else 4096):
                raise ValueError('Запрос слишком велик')
            body = json.loads(self.rfile.read(length) or b'{}')
            if parsed.path == '/api/sync':
                wake.set()
                self.json({'ok': True})
            elif parsed.path == '/api/selection':
                set_selection(body.get('paths'), body.get('selected'))
                self.json({'ok': True})
            elif parsed.path == '/api/backup/delete':
                delete_backup(body.get('name', ''))
                self.json({'ok': True})
            elif parsed.path == '/api/metadata':
                path = body.get('path')
                if not metadata_manager or path not in {g['path'] for g in catalog()}:
                    raise ValueError('Игра не найдена или поиск метаданных отключён')
                chosen = body.get('chosen')
                if chosen is not None and (not isinstance(chosen, str) or len(chosen) > 300):
                    raise ValueError('Недопустимое название')
                metadata_manager.enqueue(path, chosen)
                self.json({'ok': True}, 202)
            elif parsed.path == '/api/backup':
                if backup_lock.locked():
                    return self.json({'error': 'Копирование уже выполняется'}, 409)
                threading.Thread(target=make_backup, args=(body.get('destination', 'download'), body.get('capture', True)), daemon=True).start()
                self.json({'ok': True}, 202)
            elif parsed.path in ('/api/restore/preview', '/api/restore'):
                if backup_lock.locked():
                    return self.json({'error': 'Дождитесь завершения текущей операции'}, 409)
                name = body.get('name', '')
                backup_path(name)
                if parsed.path.endswith('/preview'):
                    threading.Thread(target=prepare_restore, args=(name,), daemon=True).start()
                else:
                    if status['restore'].get('phase') != 'ready' or status['restore'].get('name') != name:
                        raise ValueError('Сначала выполните проверку архива')
                    threading.Thread(target=restore_backup, args=(name, bool(body.get('saves'))), daemon=True).start()
                self.json({'ok': True}, 202)
            else:
                self.json({'error': 'Не найдено'}, 404)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as exc:
            self.close_connection = True
            self.json({'error': str(exc)}, 400)

    def import_backup(self, length):
        if not 0 < length <= 1024**4:
            raise ValueError('Недопустимый размер архива')
        if not backup_lock.acquire(blocking=False):
            raise ValueError('Дождитесь завершения текущей операции')
        base = STATE / 'backups'
        base.mkdir(exist_ok=True)
        name = 'imported-' + time.strftime('%Y%m%d-%H%M%S') + '-' + uuid.uuid4().hex[:6] + '.tar'
        temp = base / (name + '.partial')
        try:
            if shutil.disk_usage(base).free < length + 100 * 1024**2:
                raise ValueError('Недостаточно места для архива')
            self.connection.settimeout(90)
            status['restore'] = {'phase': 'importing', 'name': name}
            with temp.open('xb') as f:
                remaining = length
                while remaining:
                    chunk = self.rfile.read(min(1024 * 1024, remaining))
                    if not chunk:
                        raise ValueError('Загрузка архива прервана')
                    f.write(chunk)
                    remaining -= len(chunk)
            status['restore']['phase'] = 'checking'
            manifest = verify_archive(temp)
            plan = restore_plan(manifest)
            record = {'name': name, 'destination': 'download', 'created': manifest['created'], 'size': length, 'verified': True, 'capture': manifest.get('capture'), 'note': 'Загружено из внешней резервной копии', 'sha256': digest(temp)}
            os.replace(temp, base / name)
            with lock:
                atomic_json(STATE / 'backups.json', [record] + read_json(STATE / 'backups.json', []))
            status['restore'] = {'phase': 'ready', 'name': name, **plan}
            self.json({'ok': True, 'name': name})
        except Exception as exc:
            status['restore'] = {'phase': 'error', 'error': str(exc)}
            raise
        finally:
            temp.unlink(missing_ok=True)
            backup_lock.release()

    def upload(self, query, length):
        name = filename(query.get('name', [''])[0])
        kind = query.get('kind', ['rom'])[0]
        system = query.get('system', ['NES'])[0]
        if kind == 'bios':
            if Path(name).suffix.lower() not in ('.bin', '.rom', '.bios', '.dat') or length > 64 * 1024**2:
                raise ValueError('BIOS: .bin, .rom, .bios или .dat, до 64 МБ')
            relative = 'system/' + name
        elif kind == 'cover':
            if system not in SYSTEMS or Path(name).suffix.lower() != '.png' or length > 10 * 1024**2:
                raise ValueError('Обложка: PNG до 10 МБ')
            label = query.get('label', [''])[0]
            if not any(g['label'] == label and g['system'] == system for g in catalog()):
                raise ValueError('Игра для обложки не найдена')
            artlabel = filename(re.sub(r'[&*/:`<>?\\|]', '_', label))
            relative = 'thumbnails/' + SYSTEMS[system][0] + '/Named_Boxarts/' + artlabel + '.png'
        elif kind == 'rom':
            if system not in SYSTEMS or Path(name).suffix.lower() not in SYSTEMS[system][3]:
                raise ValueError('Формат не поддерживается для выбранной системы. RAR/7z распакуйте перед загрузкой.')
            relative = f'downloads/Library/{system}/{name}'
        else:
            raise ValueError('Неизвестный тип файла')
        dest = safe_path(ROOT, relative)
        temp = STATE / 'incoming' / uuid.uuid4().hex
        temp.parent.mkdir(exist_ok=True)
        if shutil.disk_usage(ROOT).free < length + 100 * 1024**2:
            raise ValueError('На NAS недостаточно места')
        self.connection.settimeout(90)
        try:
            with temp.open('xb') as f:
                remaining = length
                while remaining:
                    chunk = self.rfile.read(min(1024 * 1024, remaining))
                    if not chunk:
                        raise ValueError('Загрузка прервана')
                    f.write(chunk)
                    remaining -= len(chunk)
            if kind == 'cover' and temp.read_bytes()[:8] != b'\x89PNG\r\n\x1a\n':
                raise ValueError('Файл не является PNG')
            if kind == 'bios' and name.lower() in BIOS_MD5 and digest(temp, 'md5') != BIOS_MD5[name.lower()]:
                raise ValueError('Контрольная сумма этого PlayStation BIOS не совпадает с эталоном')
            if kind == 'rom' and Path(name).suffix.lower() in ('.cue', '.m3u'):
                text = temp.read_text('utf-8-sig')
                references = re.findall(r'FILE\s+"([^"]+)"', text, re.I) if name.lower().endswith('.cue') else [l.strip() for l in text.splitlines() if l.strip() and not l.startswith('#')]
                if not references:
                    raise ValueError('Список дисков/дорожек пуст или формат не поддерживается')
                for ref in references:
                    filename(ref)
                    if not (dest.parent / ref).is_file():
                        raise ValueError('Сначала загрузите файл дорожки/диска: ' + ref)
            with lock:
                if dest.exists():
                    if digest(dest) != digest(temp):
                        raise ValueError('Файл с этим именем уже есть и отличается. Используйте другое имя.')
                    if kind == 'rom':
                        add_playlist_entry(system, name)
                        if metadata_manager:
                            metadata_manager.enqueue(relative)
                    self.json({'ok': True, 'duplicate': True, 'path': relative})
                    return
                dest.parent.mkdir(parents=True, exist_ok=True)
                # Incoming and library may be separate mounts; copy into the destination filesystem first.
                local_temp = dest.with_name('.upload-' + uuid.uuid4().hex)
                try:
                    shutil.copyfile(temp, local_temp)
                    os.replace(local_temp, dest)
                    if kind == 'rom':
                        add_playlist_entry(system, name)
                        selection = read_json(STATE / 'selection.json', {})
                        selection[relative] = query.get('sync', ['true'])[0] == 'true'
                        atomic_json(STATE / 'selection.json', selection)
                        if metadata_manager:
                            metadata_manager.enqueue(relative)
                finally:
                    local_temp.unlink(missing_ok=True)
            wake.set()
            event('Добавлен в библиотеку: ' + relative)
            self.json({'ok': True, 'path': relative})
        finally:
            temp.unlink(missing_ok=True)


def main():
    global metadata_manager
    STATE.mkdir(parents=True, exist_ok=True)
    ROOT.mkdir(parents=True, exist_ok=True)
    status['restore'] = read_json(STATE / 'last-restore.json', {})
    if os.getenv('METADATA_ENABLED', 'true').lower() == 'true':
        from metadata import MetadataManager
        metadata_manager = MetadataManager(sys.modules[__name__])
        metadata_manager.start()
    threading.Thread(target=sync_loop, daemon=True).start()
    event(f'Retro Library запущен. Apple TV: {TARGET}; интервал: {INTERVAL} сек.')
    ThreadingHTTPServer(('0.0.0.0', PORT), Handler).serve_forever()


if __name__ == '__main__':
    main()
