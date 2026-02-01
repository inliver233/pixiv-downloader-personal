import os
import unittest

from PixivDBManager import PixivDBManager


class TestFollowIndexDB(unittest.TestCase):
    def setUp(self):
        self.db_path = "test.follow.db.sqlite"
        if os.path.exists(self.db_path):
            os.remove(self.db_path)
        self.DB = PixivDBManager(root_directory=".", target=self.db_path)
        self.DB.createDatabase()

    def tearDown(self):
        try:
            self.DB.close()
        finally:
            if os.path.exists(self.db_path):
                os.remove(self.db_path)

    def test_follow_index_roundtrip(self):
        member_id = 123
        image_id = 456

        self.DB.upsertFollowMember(
            member_id=member_id,
            name="Artist",
            member_token="artist_token",
            avatar_url="https://i.pximg.net/user-profile/img/test.png",
            background_url="https://i.pximg.net/background/img/test.jpg",
        )
        self.DB.upsertFollowImage(
            image_id=image_id,
            member_id=member_id,
            title="Title",
            caption="Caption",
            create_date="2020-01-01T00:00:00+00:00",
            page_count=2,
            mode="manga",
            bookmark_count=100,
            like_count=10,
            view_count=999,
        )
        self.DB.upsertFollowImageUrls(
            image_id=image_id,
            url_rows=[
                (
                    image_id,
                    0,
                    "https://i.pximg.net/img-original/img/2020/01/01/00/00/00/456_p0.jpg",
                    "https://i.pximg.net/img-master/img/2020/01/01/00/00/00/456_p0_master1200.jpg",
                ),
                (
                    image_id,
                    1,
                    "https://i.pximg.net/img-original/img/2020/01/01/00/00/00/456_p1.jpg",
                    "https://i.pximg.net/img-master/img/2020/01/01/00/00/00/456_p1_master1200.jpg",
                ),
            ],
        )

        summary = self.DB.selectFollowMemberSummaryById(member_id)
        self.assertIsNotNone(summary)
        self.assertEqual(int(summary[0]), member_id)
        self.assertEqual(int(summary[7]), 1)  # image_count
        self.assertEqual(int(summary[8]), 2)  # url_count

        urls = self.DB.selectFollowImageUrls(image_id)
        self.assertEqual(len(urls), 2)

        page0 = self.DB.selectFollowImagePage0Urls([image_id])
        self.assertIn(image_id, page0)

