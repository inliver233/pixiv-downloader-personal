# -*- coding: utf-8 -*-

import os
import tempfile
import unittest

from PixivDBManager import PixivDBManager


class TestFollowIndexIncompleteUrls(unittest.TestCase):
    def test_selectFollowImageIdsNeedingUrlIndex(self):
        fd, db_path = tempfile.mkstemp(prefix="pixivutil_follow_", suffix=".sqlite")
        os.close(fd)
        try:
            db = PixivDBManager(root_directory=".", target=db_path)
            db.createDatabase()

            # image has 3 pages but 0 urls -> should be reported
            db.upsertFollowImage(image_id=1, member_id=100, page_count=3, mode="manga")
            self.assertEqual(db.selectFollowImageIdsNeedingUrlIndex(100), [1])

            # 2 urls out of 3 -> still incomplete
            db.upsertFollowImageUrls(
                1,
                [
                    (1, 0, "ori0", "reg0"),
                    (1, 1, "ori1", "reg1"),
                ],
            )
            self.assertEqual(db.selectFollowImageIdsNeedingUrlIndex(100), [1])

            # Complete urls -> no longer reported
            db.upsertFollowImageUrls(1, [(1, 2, "ori2", "reg2")])
            self.assertEqual(db.selectFollowImageIdsNeedingUrlIndex(100), [])
        finally:
            try:
                db.close()
            except Exception:
                pass
            try:
                os.remove(db_path)
            except OSError:
                pass


if __name__ == "__main__":
    unittest.main()

