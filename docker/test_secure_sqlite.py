import importlib.util
import os
from pathlib import Path
import stat
import sqlite3
import tempfile
import unittest


source = Path(__file__).resolve().with_name('secure_sqlite.py')
spec = importlib.util.spec_from_file_location('secure_sqlite', source)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class SQLitePermissionsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.database = self.root / 'private' / 'db.sqlite3'

    def test_new_database_is_private_even_with_permissive_umask(self):
        previous = os.umask(0)
        try:
            module.secure_sqlite(self.database)
        finally:
            os.umask(previous)
        self.assertEqual(stat.S_IMODE(self.database.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.database.parent.stat().st_mode), 0o700)

    def test_existing_database_and_sidecars_are_restricted_without_changing_data(self):
        self.database.parent.mkdir()
        paths = [Path(str(self.database) + suffix) for suffix in ('', '-wal', '-shm', '-journal')]
        for path in paths:
            path.write_bytes(b'permission-test fixture only')
            path.chmod(0o664)
        module.secure_sqlite(self.database)
        for path in paths:
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(path.read_bytes(), b'permission-test fixture only')

    def test_symlink_database_is_refused_and_target_unchanged(self):
        self.database.parent.mkdir()
        target = self.root / 'unrelated'
        target.write_bytes(b'fixture')
        target.chmod(0o644)
        self.database.symlink_to(target)
        with self.assertRaises(OSError):
            module.secure_sqlite(self.database)
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o644)

    def test_symlink_sidecar_is_refused(self):
        module.secure_sqlite(self.database)
        target = self.root / 'unrelated'
        target.write_bytes(b'fixture')
        target.chmod(0o644)
        Path(str(self.database) + '-wal').symlink_to(target)
        with self.assertRaises(OSError):
            module.secure_sqlite(self.database)
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o644)

    def test_hardlinked_database_is_refused(self):
        self.database.parent.mkdir()
        self.database.write_bytes(b'fixture')
        self.database.chmod(0o644)
        os.link(self.database, self.root / 'other-copy')
        with self.assertRaises(ValueError):
            module.secure_sqlite(self.database)
        self.assertEqual(stat.S_IMODE(self.database.stat().st_mode), 0o644)

    def test_directory_cannot_be_used_as_database(self):
        self.database.mkdir(parents=True)
        with self.assertRaises(OSError):
            module.secure_sqlite(self.database)

    def test_sqlite_created_wal_and_shared_memory_remain_private(self):
        module.secure_sqlite(self.database)
        previous = os.umask(0)
        connection = sqlite3.connect(self.database)
        try:
            connection.execute('PRAGMA journal_mode=WAL')
            connection.execute('CREATE TABLE fixture (value INTEGER)')
            connection.execute('INSERT INTO fixture VALUES (1)')
            connection.commit()
            for suffix in ('', '-wal', '-shm'):
                path = Path(str(self.database) + suffix)
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        finally:
            connection.close()
            os.umask(previous)


if __name__ == '__main__':
    unittest.main()
