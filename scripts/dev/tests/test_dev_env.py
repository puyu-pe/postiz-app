"""Literal private development settings parsing; temporary files only."""
import tempfile
from pathlib import Path
import unittest
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import config


class DevEnvTests(unittest.TestCase):
    def parse(self, content):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / '.dev.env.local'
            path.write_text(content, encoding='utf-8')
            return config.parse_dev_file(path)

    def test_comments_quotes_empty_and_literal_expansion(self):
        self.assertEqual(self.parse("# comment\n\nJWT_SECRET='${NOT_EXPANDED}'\nNOT_SECURED=\"\"\n"),
                         {'JWT_SECRET': '${NOT_EXPANDED}', 'NOT_SECURED': ''})

    def test_rejects_malformed_duplicate_and_unsupported_without_values(self):
        cases = (('never-print-this-secret-marker', 'malformed'), ('PORT=first\nPORT=secret\n', 'duplicate'),
                 ('PATH=/private/path\n', 'unsupported'), ('export PORT=123\n', 'malformed'),
                 ('JWT_SECRET="unmatched\n', 'malformed'))
        for source, expected in cases:
            with self.subTest(source=source):
                with self.assertRaises(RuntimeError) as raised:
                    self.parse(source)
                self.assertIn(expected, str(raised.exception))
                self.assertNotIn('secret', str(raised.exception))

    def test_precedence_including_explicit_empty_and_relative_upload(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'settings'
            path.write_text('PORT=3100\nJWT_SECRET=file-value\nUPLOAD_DIRECTORY=var/uploads\n')
            effective = config.dev_environment({'PORT': '', 'JWT_SECRET': 'environment'}, path)
            self.assertEqual(effective['PORT'], '')
            self.assertEqual(config.dev_environment({'UPLOAD_DIRECTORY': ''}, path)['UPLOAD_DIRECTORY'], '')
            self.assertEqual(config.dev_environment({}, path)['UPLOAD_DIRECTORY'], str(config.ROOT / 'var/uploads'))
            path.write_text('UPLOAD_DIRECTORY=\n')
            self.assertEqual(config.dev_environment({}, path)['UPLOAD_DIRECTORY'], '')
            self.assertEqual(effective['JWT_SECRET'], 'environment')
            self.assertEqual(effective['UPLOAD_DIRECTORY'], str(config.ROOT / 'var/uploads'))

    def test_missing_file_is_environment_only(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(config.dev_environment({'PORT': '3100'}, Path(directory) / 'absent'),
                             {'PORT': '3100'})


if __name__ == '__main__':
    unittest.main()
