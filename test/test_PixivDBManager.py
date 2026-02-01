#!C:/Python37-32/python
# -*- coding: UTF-8 -*-

import os
import unittest

import common.PixivConstant as PixivConstant
from PixivDBManager import PixivDBManager
from model.PixivListItem import PixivListItem

LIST_SIZE = 9
PixivConstant.PIXIVUTIL_LOG_FILE = "pixivutil.test.log"


class TestPixivDBManager(unittest.TestCase):
    def setUp(self):
        self.db_path = "test.db.sqlite"
        if os.path.exists(self.db_path):
            os.remove(self.db_path)

        self.DB = PixivDBManager(root_directory=".", target=self.db_path)
        self.DB.createDatabase()
        members = PixivListItem.parseList("./test_data/test.list.txt", ".")
        self.DB.importList(members)

    def tearDown(self):
        try:
            self.DB.close()
        finally:
            if os.path.exists(self.db_path):
                os.remove(self.db_path)

    def test_ImportListTxt(self):
        members = PixivListItem.parseList("./test_data/test.list.txt", ".")
        result = self.DB.importList(members)
        assert result == 0

    def test_SelectMembersByLastDownloadDate(self):
        result = self.DB.selectMembersByLastDownloadDate(7)
        assert len(result) == LIST_SIZE

    def test_SelectAllMember(self):
        result = self.DB.selectAllMember()
        assert len(result) == LIST_SIZE


# if __name__ == '__main__':
#     suite = unittest.TestLoader().loadTestsFromTestCase(TestPixivDBManager)
#     unittest.TextTestRunner(verbosity=5).run(suite)
#     print("================================================================")
