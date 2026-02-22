import importlib.util
import json
import os
import sqlite3
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory


def _load_module(module_name: str, path: Path):
    spec = importlib.util.spec_from_file_location(module_name, str(path))
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Failed to load module spec: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)  # type: ignore[attr-defined]
    return module


class TestFollowTransferTools(unittest.TestCase):
    def setUp(self):
        self.repo_root = Path(__file__).resolve().parents[1]
        self.follow_dir = self.repo_root / "关注"
        self.export_py = self.follow_dir / "export_following_from_db.py"
        self.auto_follow_py = self.follow_dir / "auto_follow_from_txt.py"
        self.export_token_py = self.follow_dir / "export_following_from_token.py"

        self.assertTrue(self.export_py.exists())
        self.assertTrue(self.auto_follow_py.exists())
        self.assertTrue(self.export_token_py.exists())

        self.export_mod = _load_module("export_following_from_db", self.export_py)
        self.auto_mod = _load_module("auto_follow_from_txt", self.auto_follow_py)
        self.export_token_mod = _load_module("export_following_from_token", self.export_token_py)

    def test_iter_member_ids_parses_common_formats(self):
        with TemporaryDirectory() as td:
            txt = Path(td) / "ids.txt"
            txt.write_text(
                "\n".join(
                    [
                        "# comment",
                        "123",
                        "456\tname",
                        "id:789 name:abc",
                        "abc",
                        "",
                    ]
                ),
                encoding="utf-8",
            )
            ids = list(self.auto_mod._iter_member_ids(txt))  # pylint: disable=protected-access
            self.assertEqual(ids, [123, 456, 789])

    def test_extract_error_message_handles_pixiv_shapes(self):
        extract = self.auto_mod._extract_error_message  # pylint: disable=protected-access
        self.assertEqual(extract('{"error":{"message":"foo","reason":"bar"}}'), "foo")
        self.assertEqual(extract('{"errors":{"system":{"message":"system msg"}}}'), "system msg")
        self.assertEqual(extract("plain text error"), "plain text error")

    def test_already_followed_detection(self):
        looks = self.auto_mod._looks_like_already_followed  # pylint: disable=protected-access
        self.assertTrue(looks("Already followed", ""))
        self.assertTrue(looks("既にフォローしています", ""))
        self.assertFalse(looks("Some other error", ""))

    def test_export_follow_ids_writes_one_per_line(self):
        export_ids = self.export_mod._export_ids  # pylint: disable=protected-access

        with TemporaryDirectory() as td:
            td_path = Path(td)
            db_path = td_path / "test.db.sqlite"
            out_path = td_path / "out.txt"

            conn = sqlite3.connect(str(db_path))
            try:
                cur = conn.cursor()
                cur.execute(
                    """CREATE TABLE IF NOT EXISTS pixiv_follow_member (
                        member_id INTEGER PRIMARY KEY,
                        name TEXT,
                        member_token TEXT,
                        avatar_url TEXT,
                        background_url TEXT,
                        created_date DATE,
                        last_sync_date DATE
                    )"""
                )
                cur.execute("INSERT INTO pixiv_follow_member (member_id) VALUES (2)")
                cur.execute("INSERT INTO pixiv_follow_member (member_id) VALUES (1)")
                conn.commit()
            finally:
                conn.close()

            count = export_ids(str(db_path), out_path)
            self.assertEqual(count, 2)

            content = out_path.read_text(encoding="utf-8").splitlines()
            self.assertEqual(content, ["1", "2"])

            # cleanup WAL files if created
            for suffix in (".wal", ".shm"):
                p = Path(str(db_path) + suffix)
                if p.exists():
                    os.remove(p)

    def test_oauth_login_response_parsing(self):
        get_uid = self.export_token_mod._get_user_id_from_login_response  # pylint: disable=protected-access
        sample = {
            "response": {
                "access_token": "AT",
                "refresh_token": "RT",
                "user": {"id": "123", "name": "n"},
            }
        }
        self.assertEqual(get_uid(json.dumps(sample)), 123)

    def test_extract_following_user_ids(self):
        extract = self.export_token_mod._extract_following_user_ids  # pylint: disable=protected-access
        payload = {
            "user_previews": [
                {"user": {"id": 1}},
                {"user": {"id": "2"}},
                {"user": {"id": None}},
            ],
            "next_url": None,
        }
        self.assertEqual(list(extract(payload)), [1, 2])


if __name__ == "__main__":
    unittest.main()
