import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile
import zlib

import metadata as m


class MetadataTests(unittest.TestCase):
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
