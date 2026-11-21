import copy
import hashlib
import http.client
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.parse import urlencode

import service as s


class FakeDAV:
    data = {}
    fail = False
    publishes = []

    def listing(self, rel):
        if self.fail:
            raise ConnectionError('offline')
        prefix = rel.strip('/') + '/' if rel else ''
        return [{'name': p[len(prefix):], 'path': '/' + p, 'size': len(content)} for p, content in self.data.items() if p.startswith(prefix) and '/' not in p[len(prefix):]]

    def remote_hash(self, rel, destination=None):
        content = self.data[rel]
        if destination:
            destination.write_bytes(content)
        return hashlib.sha256(content).hexdigest(), len(content)

    def publish(self, rel, local, overwrite=False):
        if rel in self.data and not overwrite:
            raise RuntimeError('overwrite refused')
        self.data[rel] = local.read_bytes()
        self.publishes.append((rel, overwrite))


class ServiceTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.patches = [patch.object(s, 'ROOT', self.root / 'library'), patch.object(s, 'STATE', self.root / 'state')]
        for p in self.patches:
            p.start()
        s.ROOT.mkdir()
        s.STATE.mkdir()
        s.hash_cache.clear()
        s.status.update(files={}, events=[], backup={}, online=None)
        FakeDAV.data = {}
        FakeDAV.fail = False
        FakeDAV.publishes = []

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.temp.cleanup()

    def file(self, rel, data=b'ROM content'):
        p = s.ROOT / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        return p

    def test_restore_missing_without_touching_conflicts(self):
        self.file('downloads/Library/NES/new.nes')
        self.file('system/custom.bin', b'our bios')
        FakeDAV.data['system/custom.bin'] = b'other existing bios'
        with patch.object(s, 'DAV', FakeDAV):
            s.scan_once()
        self.assertEqual(FakeDAV.data['downloads/Library/NES/new.nes'], b'ROM content')
        self.assertEqual(FakeDAV.data['system/custom.bin'], b'other existing bios')
        self.assertEqual(s.status['files']['system/custom.bin']['state'], 'conflict')
        FakeDAV.data.pop('downloads/Library/NES/new.nes')
        with patch.object(s, 'DAV', FakeDAV):
            s.scan_once()
        self.assertIn('downloads/Library/NES/new.nes', FakeDAV.data)

    def test_offline_does_not_publish(self):
        self.file('downloads/Library/NES/new.nes')
        FakeDAV.fail = True
        with patch.object(s, 'DAV', FakeDAV):
            s.scan_once()
        self.assertFalse(s.status['online'])
        self.assertEqual(FakeDAV.publishes, [])

    def test_only_acknowledged_playlist_can_update(self):
        self.file('downloads/Library/NES/first.nes')
        s.add_playlist_entry('NES', 'first.nes')
        with patch.object(s, 'DAV', FakeDAV):
            s.scan_once()
            self.file('downloads/Library/NES/second.nes')
            s.add_playlist_entry('NES', 'second.nes')
            s.scan_once()
        playlist = 'playlists/' + s.SYSTEMS['NES'][0] + '.lpl'
        self.assertEqual(len(json.loads(FakeDAV.data[playlist])['items']), 2)
        self.assertEqual(FakeDAV.publishes[-1], (playlist, True))
        FakeDAV.data[playlist] = b'user edited playlist'
        self.file('downloads/Library/NES/third.nes')
        s.add_playlist_entry('NES', 'third.nes')
        with patch.object(s, 'DAV', FakeDAV):
            s.scan_once()
        self.assertEqual(FakeDAV.data[playlist], b'user edited playlist')

    def test_paths_and_symlinks(self):
        for rel in ('../escape', '/absolute', 'a/../../escape', 'a\\evil', '.hidden', 'a/.hidden'):
            with self.assertRaises(ValueError):
                s.safe_path(s.ROOT, rel)
        outside = self.root / 'outside'
        outside.mkdir()
        try:
            (s.ROOT / 'link').symlink_to(outside, target_is_directory=True)
        except OSError:
            return
        with self.assertRaises(ValueError):
            s.safe_path(s.ROOT, 'link/escape')

    def test_existing_directories_do_not_require_mkcol(self):
        dav = s.DAV()
        directories = {'': [{'name': 'downloads', 'path': '/downloads/'}], 'downloads': []}
        with patch.object(dav, 'listing', side_effect=lambda p: directories.get(p, [])), patch.object(dav, 'request', return_value=(201, b'')) as request:
            dav.mkdirs('downloads/new')
            request.assert_called_once_with('MKCOL', '/downloads/new', allow=(405, 500))
        dav = s.DAV()
        with patch.object(dav, 'listing', return_value=[]), patch.object(dav, 'request', return_value=(500, b'')):
            with self.assertRaises(RuntimeError):
                dav.mkdirs('new')

    def test_backup_is_complete_and_detects_corruption(self):
        self.file('downloads/Library/NES/one.nes', b'unique-rom-test-content')
        s.make_backup('download', capture=False)
        self.assertEqual(s.status['backup']['phase'], 'complete')
        p = s.STATE / 'backups' / s.status['backup']['name']
        manifest = s.verify_archive(p)
        self.assertIn('library/downloads/Library/NES/one.nes', manifest['files'])
        content = p.read_bytes().replace(b'unique-rom-test-content', b'broken-rom-test-content')
        p.write_bytes(content)
        with self.assertRaises(ValueError):
            s.verify_archive(p)

    def test_restore_missing_and_changed_with_rollback(self):
        first = self.file('downloads/Library/NES/one.nes', b'old content')
        second = self.file('system/custom.bin', b'original bios')
        s.make_backup('download', capture=False)
        name = s.status['backup']['name']
        first.unlink()
        second.write_bytes(b'changed bios')
        self.file('downloads/Library/NES/keep.nes', b'keep new game')
        s.prepare_restore(name)
        self.assertEqual(s.status['restore']['add'], 1)
        self.assertEqual(s.status['restore']['replace'], 1)
        s.restore_backup(name)
        self.assertEqual(s.status['restore']['phase'], 'complete')
        self.assertEqual(s.status['restore']['restored'], 2)
        self.assertEqual(first.read_bytes(), b'old content')
        self.assertEqual(second.read_bytes(), b'original bios')
        old = Path(s.status['restore']['rollback']) / 'library/system/custom.bin'
        self.assertEqual(old.read_bytes(), b'changed bios')
        self.assertTrue((s.ROOT / 'downloads/Library/NES/keep.nes').exists())
        for name in ('library/../outside', 'library/config/evil.cfg', 'apple-tv/../../outside', 'apple-tv/downloads/evil.nes'):
            with self.assertRaises(ValueError):
                s.restore_member(name)
        self.assertEqual(s.restore_member('service/service.py')[0], 'skip')

    def test_restore_saves_is_opt_in_and_preserves_current_version(self):
        save = s.STATE / 'captures/first/saves/game.srm'
        save.parent.mkdir(parents=True)
        save.write_bytes(b'old game progress')
        s.atomic_json(s.STATE / 'capture-latest.json', {'directory': 'first', 'captured': s.now(), 'files': 1})
        self.file('downloads/Library/NES/one.nes')
        s.make_backup('download', capture=False)
        name = s.status['backup']['name']
        FakeDAV.data['saves/game.srm'] = b'new game progress'
        (s.STATE / 'capture-latest.json').unlink()
        with patch.object(s, 'DAV', FakeDAV):
            s.restore_backup(name, restore_saves=False)
            self.assertEqual(FakeDAV.data['saves/game.srm'], b'new game progress')
            pointer = s.read_json(s.STATE / 'capture-latest.json', {})
            self.assertTrue((s.STATE / 'captures' / pointer['directory'] / 'saves/game.srm').is_file())
            s.restore_backup(name, restore_saves=True)
        self.assertEqual(s.status['restore']['phase'], 'complete')
        self.assertEqual(FakeDAV.data['saves/game.srm'], b'old game progress')
        old = Path(s.status['restore']['rollback']) / 'apple-tv/saves/game.srm'
        self.assertEqual(old.read_bytes(), b'new game progress')

    def test_http_upload_and_csrf_and_cue_order(self):
        server = s.ThreadingHTTPServer(('127.0.0.1', 0), s.Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        def upload(name, content, system='NES', token=s.csrf):
            conn = http.client.HTTPConnection('127.0.0.1', server.server_port)
            conn.request('POST', '/api/upload?' + urlencode({'name': name, 'kind': 'rom', 'system': system}), body=content, headers={'X-CSRF-Token': token})
            response = conn.getresponse()
            data = json.loads(response.read())
            conn.close()
            return response.status, data
        try:
            self.assertEqual(upload('a.nes', b'rom', token='bad')[0], 403)
            self.assertEqual(upload('../a.nes', b'rom')[0], 400)
            self.assertEqual(upload('a.nes', b'rom')[0], 200)
            self.assertTrue(upload('a.nes', b'rom')[1]['duplicate'])
            self.assertEqual(upload('a.nes', b'other')[0], 400)
            cue = b'FILE "disc.bin" BINARY\n  TRACK 01 MODE2/2352\n    INDEX 01 00:00:00\n'
            self.assertEqual(upload('disc.cue', cue, 'PlayStation')[0], 400)
            self.assertEqual(upload('disc.bin', b'CD track', 'PlayStation')[0], 200)
            self.assertEqual(upload('disc.cue', cue, 'PlayStation')[0], 200)
            self.assertEqual(len(s.catalog()), 2)
            self.assertEqual((s.ROOT / 'downloads/Library/NES/a.nes').read_bytes(), b'rom')
        finally:
            server.shutdown()
            server.server_close()

    def test_archive_http_import_and_invalid_archive(self):
        self.file('downloads/Library/NES/one.nes')
        s.make_backup('download', capture=False)
        archive = (s.STATE / 'backups' / s.status['backup']['name']).read_bytes()
        server = s.ThreadingHTTPServer(('127.0.0.1', 0), s.Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            for content, expected in ((b'not an archive', 400), (archive, 200)):
                conn = http.client.HTTPConnection('127.0.0.1', server.server_port)
                conn.request('POST', '/api/backup/import', body=content, headers={'X-CSRF-Token': s.csrf})
                response = conn.getresponse()
                data = json.loads(response.read())
                self.assertEqual(response.status, expected, data)
                conn.close()
            self.assertEqual(s.status['restore']['phase'], 'ready')
            self.assertEqual(s.status['restore']['unchanged'], 1)
            self.assertEqual(len(s.read_json(s.STATE / 'backups.json', [])), 2)
        finally:
            server.shutdown()
            server.server_close()

    def test_selection_filters_device_playlist_and_preserves_nas(self):
        self.file('downloads/Library/NES/one.nes')
        self.file('downloads/Library/NES/two.nes')
        s.add_playlist_entry('NES', 'one.nes')
        s.add_playlist_entry('NES', 'two.nes')
        s.set_selection(['downloads/Library/NES/two.nes'], False)
        with patch.object(s, 'DAV', FakeDAV):
            s.scan_once()
        playlist = 'playlists/' + s.SYSTEMS['NES'][0] + '.lpl'
        self.assertEqual(len(json.loads(FakeDAV.data[playlist])['items']), 1)
        self.assertEqual(len(s.read_json(s.ROOT / playlist, {})['items']), 2)
        self.assertNotIn('downloads/Library/NES/two.nes', FakeDAV.data)
        s.set_selection(['downloads/Library/NES/two.nes'], True)
        with patch.object(s, 'DAV', FakeDAV):
            s.scan_once()
        self.assertIn('downloads/Library/NES/two.nes', FakeDAV.data)
        self.assertEqual(len(json.loads(FakeDAV.data[playlist])['items']), 2)
        s.set_selection(['downloads/Library/NES/two.nes'], False)
        with patch.object(s, 'DAV', FakeDAV):
            s.scan_once()
        self.assertIn('downloads/Library/NES/two.nes', FakeDAV.data)

    def test_selected_cue_keeps_shared_track(self):
        self.file('downloads/Library/PlayStation/track.bin')
        for name in ('one.cue', 'two.cue'):
            self.file('downloads/Library/PlayStation/' + name, b'FILE "track.bin" BINARY\n TRACK 01 MODE2/2352\n')
            s.add_playlist_entry('PlayStation', name)
        s.set_selection(['downloads/Library/PlayStation/one.cue'], False)
        s.atomic_json(s.STATE / 'selection.json', {'downloads/Library/PlayStation/one.cue': False, 'downloads/Library/PlayStation/track.bin': False})
        self.assertIn('downloads/Library/PlayStation/track.bin', s.sync_sources())
        s.set_selection(['downloads/Library/PlayStation/two.cue'], False)
        self.assertNotIn('downloads/Library/PlayStation/track.bin', s.sync_sources())

    def test_backup_delete_only_registered_archive_and_busy_guard(self):
        self.file('downloads/Library/NES/one.nes')
        s.make_backup('download', capture=False)
        name = s.status['backup']['name']
        archive = s.backup_path(name)
        with self.assertRaises(ValueError):
            s.delete_backup('../library/downloads/Library/NES/one.nes')
        s.backup_lock.acquire()
        try:
            with self.assertRaises(ValueError):
                s.delete_backup(name)
        finally:
            s.backup_lock.release()
        self.assertTrue(archive.exists())
        s.delete_backup(name)
        self.assertFalse(archive.exists())
        self.assertEqual(s.read_json(s.STATE / 'backups.json', []), [])
        self.assertTrue((s.ROOT / 'downloads/Library/NES/one.nes').exists())

    def test_canonical_name_and_artwork_are_applied_together(self):
        import metadata
        self.file('downloads/Library/NES/game-file.nes')
        s.add_playlist_entry('NES', 'game-file.nes')
        manager = metadata.MetadataManager(s)
        game = s.catalog()[0]
        manager.apply_canonical(game, {'title': 'Canonical Game (USA)', 'state': 'ready'}, {'Named_Boxarts': b'cover bytes'})
        updated = s.catalog()[0]
        self.assertEqual(updated['label'], 'Canonical Game (USA)')
        self.assertEqual(updated['path'], 'downloads/Library/NES/game-file.nes')
        self.assertTrue(updated['cover'].endswith('/Canonical Game (USA).png'))
        self.assertEqual((s.ROOT / updated['cover']).read_bytes(), b'cover bytes')
        self.assertTrue(updated['metadata']['canonical_applied'])

    def test_metadata_enrichment_and_retry_keep_existing_facts(self):
        import metadata
        self.file('downloads/Library/NES/game.nes')
        s.add_playlist_entry('NES', 'game.nes')
        manager = metadata.MetadataManager(s)
        path = s.catalog()[0]['path']
        index = {'records': [{'title': 'Game (USA)', 'crcs': [], 'genre': 'Action'}], 'sources': [], 'warnings': []}
        with patch.object(manager.provider, 'index', return_value=index), patch.object(manager.provider, 'artwork', return_value=(None, None)), patch.object(manager.provider, 'description', return_value={'wikidata_id': 'Q1'}), patch.object(manager.provider, 'facts', return_value=({'year': '2000', 'genre': 'Racing', 'developer': 'Studio'}, 'https://www.wikidata.org/wiki/Q1')):
            manager.process(path)
        record = s.catalog()[0]['metadata']
        self.assertEqual((record['year'], record['genre'], record['developer']), ('2000', 'Action', 'Studio'))
        self.assertEqual(record['metadata_version'], metadata.METADATA_VERSION)
        with patch.object(manager.provider, 'index', return_value=index), patch.object(manager.provider, 'artwork', return_value=(None, None)), patch.object(manager.provider, 'description', side_effect=OSError('offline')), patch.object(manager.provider, 'facts', side_effect=OSError('offline')):
            manager.process(path)
        record = s.catalog()[0]['metadata']
        self.assertEqual((record['year'], record['developer']), ('2000', 'Studio'))
        self.assertIn('https://www.wikidata.org/wiki/Q1', record['sources'])


if __name__ == '__main__':
    unittest.main()
