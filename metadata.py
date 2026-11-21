"""Conservative Libretro matching, cached artwork and attributed game descriptions."""
import hashlib
import io
import json
from pathlib import Path
import queue
import re
import threading
import time
import unicodedata
from urllib.error import HTTPError
from urllib.parse import quote, urlencode, urljoin, urlsplit
from urllib.request import Request, build_opener, HTTPRedirectHandler
import zipfile
import zlib

USER_AGENT = 'RetroLibrary/1.1 (+https://github.com/vakalistratov86/retro-library)'
FIELDS = ('genre', 'developer', 'publisher', 'releaseyear')
KINDS = ('Named_Boxarts', 'Named_Snaps', 'Named_Titles')
METADATA_VERSION = 3
FACTS = {'year': 'P577', 'genre': 'P136', 'developer': 'P178', 'publisher': 'P123'}
PLATFORMS = {'NES': {'Q172742'}, 'SNES': {'Q183259'}, 'Mega Drive': {'Q10676'}, 'PlayStation': {'Q10677'}, 'DOS': {'Q47604', 'Q170434'}}
ALIASES = {'battletoadsanddoubledragontheultimateteam': 'Battletoads-Double Dragon', 'supercontra': 'Super C', 'terminator2judgementday': 'Terminator 2 - Judgment Day', 'civilization': "Sid Meier's Civilization"}


def base_title(title):
    title = re.sub(r'^(?:nes|snes|gen|md)[_\s]+', '', title, flags=re.I)
    title = re.sub(r'\s*[\[(].*$', '', title).strip()
    title = re.sub(r',\s*(The|A|An)$', lambda m: '', title, flags=re.I).strip()
    return re.sub(r'^(The|A|An)\s+', '', title, flags=re.I)


def normalize(title):
    text = unicodedata.normalize('NFKD', base_title(title)).lower().replace('&', 'and')
    for roman, digit in (('iii', '3'), ('ii', '2'), ('iv', '4')):
        text = re.sub(r'\b' + roman + r'\b', digit, text)
    return re.sub(r'[^a-z0-9а-я]', '', text)


def correction_for(title, system):
    entries = json.loads(Path(__file__).with_name('metadata_corrections.json').read_text('utf-8'))
    return next((e for e in entries if e['system'] == system and normalize(title) in {normalize(t) for t in e['titles']}), {})


def missing_fields(record):
    return [field for field in FACTS if not record.get(field)]


def schedule_metadata(record, failed=False):
    record['missing_fields'] = missing_fields(record)
    record['metadata_version'] = METADATA_VERSION
    if record['missing_fields'] or failed:
        record['retry_attempts'] = record.get('retry_attempts', 0) + 1
        delay = min(86400, 3600 * 2 ** min(record['retry_attempts'] - 1, 5)) if failed else 7 * 86400
        record['next_retry'] = time.time() + delay
        record['state'] = 'partial' if record['missing_fields'] else 'ready'
    else:
        record.update(state='ready', retry_attempts=0, next_retry=None)


def artwork_label(label):
    return re.sub(r'[&*/:`<>?\\|]', '_', label)


def parse_dat(text, field):
    records = []
    for block in re.split(r'(?m)^game\s*\(', text)[1:]:
        def value(key):
            match = re.search(r'\b' + key + r'\s+"((?:\\.|[^"\\])*)"', block)
            return match.group(1).replace('\\"', '"').replace('\\\\', '\\') if match else ''
        title = value('comment') or value('name')
        content = value(field)
        checksums = re.findall(r'\bcrc\s+"?([0-9A-Fa-f]{8})\b', block)
        if title and content:
            records.append({'title': title, field: content, 'crcs': [c.upper() for c in checksums]})
    return records


def allowed_url(url):
    p = urlsplit(url)
    if p.scheme != 'https' or p.username or p.password or p.port not in (None, 443):
        return False
    if p.hostname == 'raw.githubusercontent.com':
        return p.path.startswith(('/libretro/libretro-database/', '/libretro-thumbnails/'))
    if p.hostname == 'api.github.com':
        return p.path.startswith('/repos/libretro-thumbnails/')
    return p.hostname in ('ru.wikipedia.org', 'en.wikipedia.org', 'www.wikidata.org') and p.path == '/w/api.php'


def needs_enrichment(record):
    if record.get('state') in (None, 'queued', 'processing'):
        return True
    if record.get('metadata_version', 0) < METADATA_VERSION:
        return True
    if record.get('next_retry'):
        return record['next_retry'] <= time.time()
    return record.get('state') == 'error' or (record.get('state') == 'ready' and not record.get('canonical_applied'))


def claim_values(claims, property_id, system):
    """Exclude deprecated/unknown values and claims for other platform versions."""
    eligible = []
    for claim in claims.get(property_id, []):
        snak = claim.get('mainsnak', {})
        if claim.get('rank') == 'deprecated' or snak.get('snaktype') != 'value':
            continue
        qualifiers = claim.get('qualifiers', {}).get('P400', [])
        platforms = {q.get('datavalue', {}).get('value', {}).get('id') for q in qualifiers}
        if qualifiers and not platforms.intersection(PLATFORMS.get(system, set())):
            continue
        eligible.append((bool(qualifiers), claim))
    # A platform-specific statement is more relevant than a generic one.
    if any(specific for specific, _ in eligible):
        eligible = [(specific, c) for specific, c in eligible if specific]
    if any(c.get('rank') == 'preferred' for _, c in eligible):
        eligible = [(s, c) for s, c in eligible if c.get('rank') == 'preferred']
    return [c['mainsnak']['datavalue']['value'] for _, c in eligible if 'datavalue' in c['mainsnak']]


def merge_facts(record, facts, source):
    for field in FACTS:
        if not record.get(field) and facts.get(field):
            record[field] = facts[field]
            record.setdefault('field_sources', {})[field] = source
            if source not in record.setdefault('sources', []):
                record['sources'].append(source)


class SafeRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not allowed_url(newurl):
            raise ValueError('Источник перенаправил запрос на недопустимый адрес')
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class Provider:
    def __init__(self, cache):
        self.cache = Path(cache)
        self.cache.mkdir(parents=True, exist_ok=True)
        self.opener = build_opener(SafeRedirect())
        self.memory = {}
        self.last_request = 0

    def fetch(self, url, limit=24 * 1024**2, ttl=7 * 86400):
        if not allowed_url(url):
            raise ValueError('Недопустимый источник метаданных')
        path = self.cache / (hashlib.sha256(url.encode()).hexdigest() + '.cache')
        if path.is_file() and time.time() - path.stat().st_mtime < ttl:
            return path.read_bytes()
        time.sleep(max(0, 0.25 - (time.monotonic() - self.last_request)))
        self.last_request = time.monotonic()
        try:
            with self.opener.open(Request(url, headers={'User-Agent': USER_AGENT, 'Accept': '*/*'}), timeout=15) as response:
                data = response.read(limit + 1)
                if len(data) > limit:
                    raise ValueError('Ответ источника слишком большой')
        except HTTPError as exc:
            if exc.code == 404:
                data = b''
            elif path.is_file():
                return path.read_bytes()  # A stale cache is preferable to losing existing metadata.
            else:
                raise
        except OSError:
            if path.is_file():
                return path.read_bytes()
            raise
        temp = path.with_suffix('.tmp')
        temp.write_bytes(data)
        temp.replace(path)
        return data

    def index(self, db):
        if db in self.memory:
            return self.memory[db]
        records = {}
        sources = []
        warnings = []
        for field in FIELDS:
            url = 'https://raw.githubusercontent.com/libretro/libretro-database/master/metadat/' + field + '/' + quote(db + '.dat')
            try:
                data = self.fetch(url)
                if not data:
                    continue
                sources.append(url)
                for row in parse_dat(data.decode('utf-8-sig'), field):
                    keys = row['crcs'] or ['title:' + row['title']]
                    for key in keys:
                        record = records.setdefault(key, {'title': row['title'], 'crcs': []})
                        record[field] = row[field]
                        record['crcs'] = sorted(set(record['crcs'] + row['crcs']))
            except (OSError, ValueError) as exc:
                warnings.append(field + ': ' + str(exc))
        repo = db.replace(' ', '_')
        tree_url = 'https://api.github.com/repos/libretro-thumbnails/' + quote(repo) + '/git/trees/master?recursive=1'
        art = {}
        try:
            try:
                tree = json.loads(self.fetch(tree_url))
                if tree.get('truncated'):
                    raise ValueError('Truncated recursive tree')
            except (OSError, ValueError):
                # Very large repositories (notably DOS) can reject recursive trees.
                base = 'https://api.github.com/repos/libretro-thumbnails/' + quote(repo) + '/git/trees/'
                root_tree = json.loads(self.fetch(base + 'master'))
                entries = []
                for directory in root_tree.get('tree', []):
                    if directory.get('path') not in KINDS or directory.get('type') != 'tree':
                        continue
                    subtree = json.loads(self.fetch(base + directory['sha']))
                    if subtree.get('truncated'):
                        raise ValueError('Каталог обложек обрезан источником')
                    entries.extend({**entry, 'path': directory['path'] + '/' + entry['path']} for entry in subtree.get('tree', []))
                tree = {'tree': entries}
            for entry in tree.get('tree', []):
                path = entry.get('path', '')
                parts = path.split('/')
                if len(parts) == 2 and parts[0] in KINDS and path.endswith('.png'):
                    name = Path(parts[1]).stem
                    art.setdefault(normalize(name), {}).setdefault(parts[0], []).append(path)
                    records.setdefault('art:' + name, {'title': name, 'crcs': []})
        except (OSError, ValueError) as exc:
            warnings.append('Обложки: ' + str(exc))
        result = {'records': list(records.values()), 'art': art, 'sources': sources, 'repo': repo, 'warnings': warnings}
        if not records:
            raise RuntimeError('Источники временно недоступны: ' + '; '.join(warnings))
        self.memory[db] = result
        return result

    @staticmethod
    def choose(index, label, crcs=(), chosen=None):
        records = index['records']
        if chosen:
            exact = [r for r in records if r['title'] == chosen]
            if not exact:
                raise ValueError('Выбранной игры нет в каталоге')
            return max(exact, key=len), 'manual', []
        checksum = [r for r in records if set(r.get('crcs', [])) & set(crcs)]
        if checksum:
            return max(checksum, key=len), 'crc32', []
        normalized = normalize(label)
        normalized = normalize(ALIASES.get(normalized, label))
        exact = [r for r in records if normalize(r['title']) == normalized]
        if exact:
            preferred = 'Japan' if '(Japan)' in label else 'Europe' if '(Europe)' in label else 'USA'
            exact.sort(key=lambda r: (preferred not in r['title'], -len(r), len(r['title'])))
            return exact[0], 'title', []
        # Fuzzy results are suggestions only; never silently confuse sequels or similarly named games.
        import difflib
        unique = {}
        for r in records:
            key = normalize(r['title'])
            if key not in unique or len(r) > len(unique[key]):
                unique[key] = r
        candidates = sorted(unique.values(), key=lambda r: difflib.SequenceMatcher(None, normalized, normalize(r['title'])).ratio(), reverse=True)
        candidates = [{'title': r['title'], 'year': r.get('releaseyear'), 'score': round(difflib.SequenceMatcher(None, normalized, normalize(r['title'])).ratio(), 2)} for r in candidates[:5]]
        return None, None, [c for c in candidates if c['score'] >= 0.55]

    def artwork(self, index, title, kind):
        choices = index['art'].get(normalize(title), {}).get(kind, [])
        choices = sorted(choices, key=lambda p: (Path(p).stem != title, ('Japan' in p) != ('Japan' in title), '(USA)' not in p, len(p)))
        for path in choices[:3]:
            url = 'https://raw.githubusercontent.com/libretro-thumbnails/' + quote(index['repo']) + '/master/' + quote(path)
            data = self.fetch(url, limit=12 * 1024**2)
            # Git repositories may store relative symlink targets instead of image bytes.
            for _ in range(4):
                if len(data) < 1024:
                    link = data.decode('utf-8', errors='replace').strip()
                    if link.lower().endswith('.png') and '\n' not in link:
                        url = urljoin(url, quote(link))
                        data = self.fetch(url, limit=12 * 1024**2)
                        continue
                break
            if not data:
                continue
            try:
                from PIL import Image
                Image.MAX_IMAGE_PIXELS = 16_000_000
                with Image.open(io.BytesIO(data)) as image:
                    image.load()
                    image.thumbnail((1600, 1600))
                    output = io.BytesIO()
                    image.convert('RGBA' if 'A' in image.getbands() else 'RGB').save(output, 'PNG')
                    return output.getvalue(), url
            except (OSError, ValueError):
                continue
        return None, None

    def description(self, title, year=None):
        query_title = base_title(title)
        failures = []
        for language in ('ru', 'en'):
            query = {'action': 'query', 'generator': 'search', 'gsrsearch': '"' + query_title.replace('"', '') + '"' + (' видеоигра' if language == 'ru' else ' video game'), 'gsrlimit': 10, 'prop': 'extracts|pageprops', 'exintro': 1, 'explaintext': 1, 'exsentences': 3, 'format': 'json', 'formatversion': 2}
            try:
                data = self.fetch('https://' + language + '.wikipedia.org/w/api.php?' + urlencode(query), limit=2 * 1024**2)
            except (OSError, ValueError) as exc:
                failures.append(str(exc))
                continue
            pages = json.loads(data).get('query', {}).get('pages', []) if data else []
            candidates = [p for p in pages if normalize(p.get('title', '')) == normalize(query_title) and self.is_game_page(p)]
            if len(candidates) > 1:
                candidates = [p for p in candidates if year and str(year) in p.get('extract', '')[:500]]
            if len(candidates) == 1:
                return self.page_description(candidates[0], language)
        if failures:
            raise OSError('; '.join(failures))
        return None

    @staticmethod
    def is_game_page(page):
        props = page.get('pageprops', {})
        if 'disambiguation' in props or re.search(r'\((?:series|franchise|серия[^)]*)\)', page.get('title', ''), re.I):
            return False
        short = props.get('wikibase-shortdesc', '')
        if re.search(r'\b(?:series|franchise)\b|серия\s+(?:компьютерных\s+)?игр', short, re.I):
            return False
        return bool(re.search(r'video\s*game|computer game|видеоигр|компьютерн\w* игр', short + ' ' + page.get('extract', ''), re.I))

    @staticmethod
    def page_description(page, language):
        return {'text': page.get('extract', '')[:1200], 'url': 'https://' + language + '.wikipedia.org/wiki/' + quote(page['title'].replace(' ', '_')), 'wikidata_id': page.get('pageprops', {}).get('wikibase_item'), 'language': language, 'license': 'CC BY-SA 4.0', 'attribution': 'Wikipedia contributors; excerpt may be shortened'}

    def identify(self, title, system, correction=None):
        """Resolve exact labels/aliases and require platform evidence, never choose a fuzzy hit."""
        if (correction or {}).get('entity'):
            return correction['entity']
        query = {'action': 'wbsearchentities', 'search': base_title(title), 'language': 'en', 'type': 'item', 'limit': 10, 'format': 'json'}
        result = json.loads(self.fetch('https://www.wikidata.org/w/api.php?' + urlencode(query), limit=2 * 1024**2))
        if 'error' in result:
            raise ValueError('Wikidata search: ' + result['error'].get('code', 'API error'))
        exact = [e['id'] for e in result.get('search', []) if re.fullmatch(r'Q[1-9][0-9]*', e.get('id', '')) and normalize(title) in {normalize(e.get('label', '')), normalize(e.get('match', {}).get('text', ''))}]
        candidates = []
        for qid, entity in (self.entities(exact, 'claims') if exact else {}).items():
            platforms = {v.get('id') for v in claim_values(entity.get('claims', {}), 'P400', system) if isinstance(v, dict)}
            if platforms.intersection(PLATFORMS.get(system, set())):
                candidates.append(qid)
        return candidates[0] if len(candidates) == 1 else None

    def entity_description(self, entity_id):
        entity = self.entities([entity_id], 'sitelinks').get(entity_id, {})
        failures = []
        for language in ('ru', 'en'):
            title = entity.get('sitelinks', {}).get(language + 'wiki', {}).get('title')
            if not title:
                continue
            query = {'action': 'query', 'titles': title, 'prop': 'extracts|pageprops', 'exintro': 1, 'explaintext': 1, 'exsentences': 3, 'format': 'json', 'formatversion': 2}
            try:
                data = json.loads(self.fetch('https://' + language + '.wikipedia.org/w/api.php?' + urlencode(query), limit=2 * 1024**2))
                for page in data.get('query', {}).get('pages', []):
                    if page.get('pageprops', {}).get('wikibase_item') == entity_id and page.get('extract') and 'disambiguation' not in page.get('pageprops', {}):
                        return self.page_description(page, language)
            except (OSError, ValueError) as exc:
                failures.append(str(exc))
        if failures:
            raise OSError('; '.join(failures))
        return None

    def entities(self, ids, props):
        query = {'action': 'wbgetentities', 'ids': '|'.join(sorted(ids)), 'props': props, 'languages': 'ru|en', 'format': 'json'}
        data = json.loads(self.fetch('https://www.wikidata.org/w/api.php?' + urlencode(query), limit=4 * 1024**2))
        if 'error' in data:
            raise ValueError('Wikidata: ' + data['error'].get('code', 'API error'))
        return data.get('entities', {})

    def facts(self, description, system):
        """Use the exact entity linked by the matched article, never fuzzy entity search."""
        entity_id = (description or {}).get('wikidata_id')
        if not isinstance(entity_id, str) or not re.fullmatch(r'Q[1-9][0-9]*', entity_id):
            return {}, None
        entity = self.entities([entity_id], 'claims').get(entity_id, {})
        claims = entity.get('claims', {})
        values = {field: claim_values(claims, prop, system) for field, prop in FACTS.items()}
        ids = {v['id'] for field, items in values.items() if field != 'year' for v in items if isinstance(v, dict) and re.fullmatch(r'Q[1-9][0-9]*', v.get('id', ''))}
        labels = self.entities(ids, 'labels|sitelinks') if ids else {}
        result = {}
        years = [int(v['time'][1:5]) for v in values['year'] if isinstance(v, dict) and v.get('precision', 0) >= 9 and re.match(r'^\+[0-9]{4}-', v.get('time', ''))]
        if years:
            result['year'] = str(min(years))
        for field in ('genre', 'developer', 'publisher'):
            names = []
            for value in values[field]:
                entry = labels.get(value.get('id'), {}).get('labels', {}) if isinstance(value, dict) else {}
                name = (entry.get('ru') or entry.get('en') or {}).get('value')
                if not name and isinstance(value, dict):
                    name = labels.get(value.get('id'), {}).get('sitelinks', {}).get('enwiki', {}).get('title')
                if name and name not in names:
                    names.append(name)
            if names:
                result[field] = ', '.join(names)
        return result, 'https://www.wikidata.org/wiki/' + entity_id


def rom_crcs(path):
    """Compute local CRCs; ROM bytes are never sent to metadata services."""
    if not path.is_file() or path.stat().st_size > 128 * 1024**2:
        return []
    if path.suffix.lower() == '.zip':
        with zipfile.ZipFile(path) as archive:
            entries = [i for i in archive.infolist() if not i.is_dir() and Path(i.filename).suffix.lower() in ('.nes', '.sfc', '.smc', '.md', '.gen', '.smd', '.fds')]
            if len(entries) != 1 or entries[0].file_size > 128 * 1024**2:
                return []
            data = archive.read(entries[0])
    elif path.suffix.lower() in ('.nes', '.sfc', '.smc', '.md', '.gen', '.smd', '.fds'):
        data = path.read_bytes()
    else:
        return []
    values = {f'{zlib.crc32(data):08X}'}
    if data.startswith(b'NES\x1a') and len(data) > 16:
        offset = 16 + (512 if data[6] & 4 else 0)
        values.add(f'{zlib.crc32(data[offset:]):08X}')
    if len(data) % 32768 == 512:
        values.add(f'{zlib.crc32(data[512:]):08X}')
    return sorted(values)


class MetadataManager:
    def __init__(self, service):
        self.s = service
        self.provider = Provider(service.STATE / 'metadata-cache')
        self.queue = queue.Queue()
        self.pending = set()
        self.guard = threading.Lock()
        self.running = None

    def save(self, path, record):
        with self.s.lock:
            records = self.s.read_json(self.s.STATE / 'metadata.json', {})
            records[path] = record
            self.s.atomic_json(self.s.STATE / 'metadata.json', records)

    def enqueue(self, path, chosen=None):
        with self.guard:
            if path in self.pending:
                return False
            self.pending.add(path)
        with self.s.lock:
            previous = self.s.read_json(self.s.STATE / 'metadata.json', {}).get(path, {})
            self.save(path, {**previous, 'state': 'queued', 'error': ''})
        self.queue.put((path, chosen))
        return True

    def start(self):
        threading.Thread(target=self.loop, daemon=True, name='metadata').start()
        self.enqueue_due()

    def enqueue_due(self):
        for game in self.s.catalog():
            if needs_enrichment(game['metadata']):
                self.enqueue(game['path'])

    def apply_canonical(self, game, record, images):
        """Commit matching playlist labels and artwork together, without moving ROM/save files."""
        original = record.get('original_label', game['label'])
        label = record['title']
        language = re.search(r'\((RUS|RUSSIAN)\)|\[.*?Rus.*?\]', original, re.I)
        if language and not re.search(r'Russia|\bRu\b|RUS', label, re.I):
            label += ' [RUS]'
        # Distinct local variants with one database title must remain distinguishable.
        other = [g for g in self.s.catalog() if g['path'] != game['path'] and g['label'] == label]
        if other:
            label += ' [' + hashlib.sha256(game['path'].encode()).hexdigest()[:6] + ']'
        db = self.s.SYSTEMS[game['system']][0]
        with self.s.lock:
            for kind, content in images.items():
                rel = 'thumbnails/' + db + '/' + kind + '/' + artwork_label(label) + '.png'
                dest = self.s.safe_path(self.s.ROOT, rel)
                if not dest.exists():
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    temp = dest.with_name('.metadata-' + dest.name)
                    temp.write_bytes(content)
                    temp.replace(dest)
            playlist_path = self.s.ROOT / 'playlists' / (db + '.lpl')
            playlist = self.s.read_json(playlist_path, {})
            for item in playlist.get('items', []):
                if item['path'].split('RetroArch/', 1)[-1] == game['path']:
                    item['label'] = label
            playlist['items'].sort(key=lambda item: item['label'].casefold())
            self.s.atomic_json(playlist_path, playlist)
            record.update(original_label=original, canonical_applied=True, display_label=label)
            self.save(game['path'], record)

    def process(self, path, chosen=None):
        game = next((g for g in self.s.catalog() if g['path'] == path), None)
        if not game:
            return
        record = {**game['metadata'], 'state': 'processing', 'updated': self.s.now(), 'error': ''}
        self.save(path, record)
        db = self.s.SYSTEMS[game['system']][0]
        index = self.provider.index(db)
        if not chosen and record.get('match') == 'manual':
            chosen = record.get('title')
        match, method, candidates = self.provider.choose(index, game['label'], rom_crcs(self.s.safe_path(self.s.ROOT, path)), chosen)
        correction = correction_for(chosen or (match or {}).get('title') or game['label'], game['system'])
        entity_id = None
        if not match and correction.get('canonical_title'):
            match, method = {'title': correction['canonical_title']}, 'verified'
        if not match:
            entity_id = self.provider.identify(game['label'], game['system'], correction)
            if entity_id:
                match, method = {'title': base_title(game['label'])}, 'wikidata'
        if not match:
            self.save(path, {**record, 'state': 'needs_review' if candidates else 'not_found', 'candidates': candidates, 'metadata_version': METADATA_VERSION, 'missing_fields': missing_fields(record), 'next_retry': time.time() + 7 * 86400})
            return
        same_title = record.get('title') == match['title']
        previous = dict(record) if same_title else {}
        record.update(state='ready', title=match['title'], match=method, candidates=[], sources=list(index['sources']), warnings=list(index['warnings']))
        if not same_title:
            record.pop('description', None)
        record['field_sources'] = dict(previous.get('field_sources', {}))
        for field in FACTS:
            value = match.get('releaseyear' if field == 'year' else field)
            record[field] = value or previous.get(field)
            if value:
                record['field_sources'].pop(field, None)
            elif record[field] and record['field_sources'].get(field):
                record['sources'].append(record['field_sources'][field])
        # Small, reviewed platform corrections take precedence over generic original-game facts.
        for field, value in correction.get('facts', {}).items():
            record[field] = value
            record['field_sources'][field] = correction['source']
        if correction.get('facts'):
            record['sources'].append(correction['source'])
        images = {}
        for kind in KINDS:
            rel = 'thumbnails/' + db + '/' + kind + '/' + artwork_label(game['label']) + '.png'
            dest = self.s.safe_path(self.s.ROOT, rel)
            if dest.exists() and not (chosen and chosen != game['metadata'].get('title') and kind in game['metadata'].get('artwork_sources', {})):
                images[kind] = dest.read_bytes()
                continue
            try:
                data, url = self.provider.artwork(index, match['title'], kind)
                if data:
                    images[kind] = data
                    record.setdefault('artwork_sources', {})[kind] = url
            except (OSError, ValueError) as exc:
                record['warnings'].append(kind + ': ' + str(exc))
        try:
            if not record.get('description'):
                description = self.provider.description(match['title'], match.get('releaseyear'))
                if description:
                    record['description'] = description
        except (OSError, ValueError) as exc:
            record['warnings'].append('Описание временно недоступно: ' + str(exc))
        try:
            if missing_fields(record):
                # An exact platform-validated entity also works when no article/excerpt exists.
                entity_id = entity_id or self.provider.identify(match['title'], game['system'], correction)
                if not entity_id:
                    linked = (record.get('description') or {}).get('wikidata_id')
                    if linked:
                        entity = self.provider.entities([linked], 'claims').get(linked, {})
                        platforms = {v.get('id') for v in claim_values(entity.get('claims', {}), 'P400', game['system']) if isinstance(v, dict)}
                        if platforms.intersection(PLATFORMS.get(game['system'], set())):
                            entity_id = linked
                facts, source = self.provider.facts({'wikidata_id': entity_id}, game['system'])
                merge_facts(record, facts, source)
                if entity_id:
                    record['wikidata_id'] = entity_id
                    if not record.get('description'):
                        description = self.provider.entity_description(entity_id)
                        if description:
                            record['description'] = description
        except (OSError, ValueError) as exc:
            record['warnings'].append('Дополнительные сведения временно недоступны: ' + str(exc))
        record['sources'] = list(dict.fromkeys(record['sources']))
        schedule_metadata(record, failed=bool(record['warnings']))
        self.apply_canonical(game, record, images)
        self.s.wake.set()
        self.s.event('Оформление найдено: ' + game['label'])

    def loop(self):
        while True:
            try:
                path, chosen = self.queue.get(timeout=60)
            except queue.Empty:
                # Refresh in-memory indexes before due retries; disk cache expires after seven days.
                self.provider.memory.clear()
                self.enqueue_due()
                continue
            self.running = path
            try:
                self.process(path, chosen)
            except Exception as exc:
                previous = self.s.read_json(self.s.STATE / 'metadata.json', {}).get(path, {})
                schedule_metadata(previous, failed=True)
                self.save(path, {**previous, 'state': 'error', 'error': str(exc), 'updated': self.s.now()})
            finally:
                self.running = None
                with self.guard:
                    self.pending.discard(path)
                self.queue.task_done()
