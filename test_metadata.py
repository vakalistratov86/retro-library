import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile
import zlib

import metadata as m


class MetadataTests(unittest.TestCase):
    @staticmethod
    def claim(value, platform=None, rank='normal'):
        result = {'rank': rank, 'mainsnak': {'snaktype': 'value', 'datavalue': {'value': value}}}
        if platform:
            result['qualifiers'] = {'P400': [{'datavalue': {'value': {'id': platform}}}]}
        return result

    def test_wikidata_fills_four_fields_and_resolves_labels(self):
        claims = {prop: [self.claim({'id': 'Q2'})] for prop in ('P136', 'P178', 'P123')}
        claims['P577'] = [self.claim({'time': '+2000-00-00T00:00:00Z', 'precision': 9})]
        with tempfile.TemporaryDirectory() as folder:
            provider = m.Provider(folder)
            responses = [json.dumps({'entities': {'Q1': {'claims': claims}}}).encode(), json.dumps({'entities': {'Q2': {'labels': {'en': {'value': 'Example'}}}}}).encode()]
            with patch.object(provider, 'fetch', side_effect=responses) as fetch:
                facts, source = provider.facts({'wikidata_id': 'Q1'}, 'PlayStation')
            self.assertEqual(facts, dict(year='2000', genre='Example', developer='Example', publisher='Example'))
            self.assertEqual(source, 'https://www.wikidata.org/wiki/Q1')
            self.assertEqual(fetch.call_count, 2)

    def test_platform_specific_claims_and_deprecated_values(self):
        claims = {'P178': [self.claim({'id': 'Q1'}), self.claim({'id': 'Q2'}, 'Q10677'), self.claim({'id': 'Q3'}, 'Q1406', 'preferred'), self.claim({'id': 'Q4'}, 'Q10677', 'deprecated')]}
        self.assertEqual(m.claim_values(claims, 'P178', 'PlayStation'), [{'id': 'Q2'}])
        self.assertEqual(m.claim_values(claims, 'P178', 'NES'), [{'id': 'Q1'}])

    def test_fallback_preserves_existing_values_and_tracks_sources(self):
        record = {'year': '1999', 'genre': 'Action', 'sources': []}
        m.merge_facts(record, {'year': '2000', 'genre': 'Racing', 'developer': 'Studio', 'publisher': 'Company'}, 'https://www.wikidata.org/wiki/Q1')
        self.assertEqual(record['year'], '1999')
        self.assertEqual(record['genre'], 'Action')
        self.assertEqual(record['developer'], 'Studio')
        self.assertEqual(set(record['field_sources']), {'developer', 'publisher'})
        self.assertEqual(len(record['sources']), 1)
        m.merge_facts(record, {}, None)
        self.assertEqual(record['publisher'], 'Company')

    def test_migration_runs_once_even_if_provider_has_missing_fields(self):
        record = {'state': 'ready', 'canonical_applied': True}
        self.assertTrue(m.needs_enrichment(record))
        record['metadata_version'] = m.METADATA_VERSION
        self.assertFalse(m.needs_enrichment(record))
        self.assertFalse(m.needs_enrichment({'state': 'needs_review'}))

    def test_no_unlinked_or_malformed_entity_lookup(self):
        with tempfile.TemporaryDirectory() as folder:
            provider = m.Provider(folder)
            with patch.object(provider, 'fetch') as fetch:
                for description in (None, {}, {'wikidata_id': '../Q1'}, {'wikidata_id': 'Q1|Q2'}):
                    self.assertEqual(provider.facts(description, 'NES'), ({}, None))
                fetch.assert_not_called()

    def test_description_keeps_entity_link(self):
        data = {'query': {'pages': [{'title': 'Example', 'extract': 'Example is a video game.', 'pageprops': {'wikibase_item': 'Q1'}}]}}
        with tempfile.TemporaryDirectory() as folder:
            provider = m.Provider(folder)
            with patch.object(provider, 'fetch', return_value=json.dumps(data).encode()):
                self.assertEqual(provider.description('Example (USA)')['wikidata_id'], 'Q1')

    def test_dat_parser_handles_parentheses_and_crc(self):
        text = 'clrmamepro ( name "Example" )\ngame (\n comment "Adventure (USA)"\n genre "Action"\n rom ( crc ABCD1234 )\n)\n'
        self.assertEqual(m.parse_dat(text, 'genre'), [{'title': 'Adventure (USA)', 'genre': 'Action', 'crcs': ['ABCD1234']}])

    def test_matching_prefers_checksum_and_never_auto_accepts_fuzzy(self):
        index = {'records': [{'title': 'Adventure (USA)', 'crcs': ['ABCD1234'], 'genre': 'Action'}, {'title': 'Adventure II (USA)', 'crcs': []}]}
        record, kind, _ = m.Provider.choose(index, 'my_random_file', ['ABCD1234'])
        self.assertEqual((record['title'], kind), ('Adventure (USA)', 'crc32'))
        self.assertEqual(m.Provider.choose(index, 'Adventure 2 (RUS)')[0]['title'], 'Adventure II (USA)')
        record, kind, candidates = m.Provider.choose(index, 'Adventur')
        self.assertIsNone(record)
        self.assertTrue(candidates)
        with self.assertRaises(ValueError):
            m.Provider.choose(index, 'Adventure', chosen='made up title')

    def test_ines_header_and_single_rom_zip(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            data = b'NES\x1a' + b'\x00' * 12 + b'ROM payload'
            rom = root / 'game.nes'
            rom.write_bytes(data)
            expected = f'{zlib.crc32(b"ROM payload"):08X}'
            self.assertIn(expected, m.rom_crcs(rom))
            archive = root / 'game.zip'
            with zipfile.ZipFile(archive, 'w') as f:
                f.writestr('game.nes', data)
            self.assertIn(expected, m.rom_crcs(archive))
            with zipfile.ZipFile(archive, 'a') as f:
                f.writestr('another.nes', data)
            self.assertEqual(m.rom_crcs(archive), [])

    def test_provider_rejects_arbitrary_hosts_and_plain_http(self):
        for url in ('http://raw.githubusercontent.com/libretro-thumbnails/a', 'https://127.0.0.1/', 'https://example.com/', 'https://raw.githubusercontent.com/other/repo/file', 'https://en.wikipedia.org:8443/w/api.php'):
            self.assertFalse(m.allowed_url(url))
        self.assertTrue(m.allowed_url('https://en.wikipedia.org/w/api.php?action=query'))

    def test_images_are_decoded_and_converted_to_png(self):
        from PIL import Image
        data = io.BytesIO()
        Image.new('RGB', (10, 12), 'blue').save(data, 'GIF')
        with tempfile.TemporaryDirectory() as folder:
            provider = m.Provider(folder)
            index = {'repo': 'Example', 'art': {'adventure': {'Named_Boxarts': ['Named_Boxarts/Adventure.png']}}}
            with patch.object(provider, 'fetch', return_value=data.getvalue()):
                image, url = provider.artwork(index, 'Adventure', 'Named_Boxarts')
            self.assertTrue(image.startswith(b'\x89PNG\r\n\x1a\n'))
            self.assertIn('/libretro-thumbnails/', url)


if __name__ == '__main__':
    unittest.main()
