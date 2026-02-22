import datetime
import os
import unittest
from types import SimpleNamespace
from unittest import mock

import common.PixivConstant as PixivConstant
from PixivDBManager import PixivDBManager
from common import PixivConfig

import handler.PixivImageHandler as PixivImageHandler


class _DummyCaller:
    def __init__(self, db: PixivDBManager):
        self.__dbManager__ = db
        self.__blacklistMembers = set()
        self.__blacklistTags = set()
        self.__blacklistTitles = set()
        self.__suppressTags = set()
        self.__seriesDownloaded = set()
        self.__errorList = []
        self.ERROR_CODE = 0
        self.DEBUG_SKIP_DOWNLOAD_IMAGE = False
        self.DEBUG_SKIP_PROCESS_IMAGE = False

    def set_console_title(self, *args, **kwargs):
        return None


def _dummy_image(image_id: int, mode: str, urls):
    artist = SimpleNamespace(
        artistId=1,
        artistName="Artist",
        artistToken="artist_token",
        artistAvatar="",
        artistBackground="",
    )
    return SimpleNamespace(
        imageId=int(image_id),
        imageTitle=f"title-{image_id}",
        translated_work_title="",
        imageCaption="",
        imageMode=mode,
        imageUrls=list(urls),
        imageResizedUrls=list(urls),
        imageCount=len(urls),
        worksDate="2020-01-01 00:00:00",
        worksDateDateTime=datetime.datetime.now(datetime.timezone.utc),
        worksResolution="0x0",
        imageTags=[],
        tags=[],
        bookmark_count=0,
        image_response_count=0,
        originalArtist=artist,
        artist=artist,
        seriesNavData=None,
        ai_type=0,
    )


class _DummyBrowser:
    def __init__(self, image):
        self._image = image
        self.calls = 0

    def getImagePage(self, *args, **kwargs):
        self.calls += 1
        return self._image, None


def _make_test_config(root_dir: str) -> PixivConfig.PixivConfig:
    cfg = PixivConfig.PixivConfig()
    cfg.rootDirectory = root_dir
    cfg.downloadListDirectory = root_dir
    cfg.dbPath = os.path.join(root_dir, "db.sqlite")
    cfg.createPixivArchive = False
    cfg.downloadResized = False
    cfg.overwrite = False
    cfg.alwaysCheckFileSize = False
    cfg.backupOldFile = False
    cfg.retry = 0
    cfg.downloadDelay = 0
    cfg.dateDiff = 0
    cfg.aiDisplayFewer = False
    cfg.useBlacklistMembers = False
    cfg.useBlacklistTags = False
    cfg.useBlacklistTitles = False
    cfg.useSuppressTags = False
    cfg.autoAddTag = False
    cfg.autoAddSeries = False
    cfg.autoAddMember = False
    cfg.autoAddCaption = False
    cfg.writeImageInfo = False
    cfg.writeImageJSON = False
    cfg.writeImageXMP = False
    cfg.writeImageXMPPerImage = False
    cfg.writeUrlInDescription = False
    cfg.writeUgoiraInfo = False
    cfg.createUgoira = False
    return cfg


class TestPixivImageHandlerRetry(unittest.TestCase):
    def test_process_image_does_not_skip_when_db_record_missing_file(self):
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            db_path = os.path.join(td, "db.sqlite")
            db = PixivDBManager(root_directory=td, target=db_path)
            db.createDatabase()
            try:
                missing_path = os.path.join(td, "missing.jpg")
                db.insertImage(member_id=1, image_id=100, isManga="big")
                db.updateImage(100, "t", missing_path, isManga="big")

                caller = _DummyCaller(db)
                cfg = _make_test_config(td)

                image = _dummy_image(100, mode="big", urls=["http://example.com/100_p0.jpg"])
                browser = _DummyBrowser(image)

                def fake_make_filename(nameFormat, imageInfo, artistInfo=None, **kwargs):
                    file_url = os.path.basename(kwargs.get("fileUrl", "")) or "file.jpg"
                    return f"{imageInfo.imageId}{os.sep}{file_url}"

                def fake_download_image(caller, url, filename, referer, overwrite, max_retry, backup_old_file, image=None, page=None, notifier=None):
                    os.makedirs(os.path.dirname(filename), exist_ok=True)
                    with open(filename, "wb") as f:
                        f.write(b"x")
                    return PixivConstant.PIXIVUTIL_OK, filename

                with mock.patch.object(PixivImageHandler.PixivBrowserFactory, "getBrowser", return_value=browser), \
                        mock.patch.object(PixivImageHandler.PixivHelper, "make_filename", side_effect=fake_make_filename), \
                        mock.patch.object(PixivImageHandler.PixivDownloadHandler, "download_image", side_effect=fake_download_image):
                    result = PixivImageHandler.process_image(caller, cfg, artist=None, image_id=100)

                self.assertEqual(browser.calls, 1)
                self.assertEqual(result, PixivConstant.PIXIVUTIL_OK)
            finally:
                db.close()

    def test_process_image_manga_failure_does_not_save_db(self):
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            db_path = os.path.join(td, "db.sqlite")
            db = PixivDBManager(root_directory=td, target=db_path)
            db.createDatabase()
            try:
                caller = _DummyCaller(db)
                cfg = _make_test_config(td)

                image = _dummy_image(
                    200,
                    mode="manga",
                    urls=[
                        "http://example.com/200_p0.jpg",
                        "http://example.com/200_p1.jpg",
                    ],
                )
                browser = _DummyBrowser(image)
                call_count = {"n": 0}

                def fake_make_filename(nameFormat, imageInfo, artistInfo=None, **kwargs):
                    file_url = os.path.basename(kwargs.get("fileUrl", "")) or "file.jpg"
                    return f"{imageInfo.imageId}{os.sep}{file_url}"

                def fake_download_image(caller, url, filename, referer, overwrite, max_retry, backup_old_file, image=None, page=None, notifier=None):
                    call_count["n"] += 1
                    os.makedirs(os.path.dirname(filename), exist_ok=True)
                    if call_count["n"] == 1:
                        with open(filename, "wb") as f:
                            f.write(b"x")
                        return PixivConstant.PIXIVUTIL_OK, filename
                    return PixivConstant.PIXIVUTIL_NOT_OK, filename

                with mock.patch.object(PixivImageHandler.PixivBrowserFactory, "getBrowser", return_value=browser), \
                        mock.patch.object(PixivImageHandler.PixivHelper, "make_filename", side_effect=fake_make_filename), \
                        mock.patch.object(PixivImageHandler.PixivDownloadHandler, "download_image", side_effect=fake_download_image):
                    result = PixivImageHandler.process_image(caller, cfg, artist=None, image_id=200)

                self.assertEqual(browser.calls, 1)
                self.assertEqual(result, PixivConstant.PIXIVUTIL_NOT_OK)
                self.assertIsNone(db.selectImageByImageId(200, cols="save_name"))
            finally:
                db.close()

    def test_process_image_manga_db_page_missing_triggers_redownload(self):
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            db_path = os.path.join(td, "db.sqlite")
            db = PixivDBManager(root_directory=td, target=db_path)
            db.createDatabase()
            try:
                caller = _DummyCaller(db)
                cfg = _make_test_config(td)

                os.makedirs(os.path.join(td, "300"), exist_ok=True)
                master_path = os.path.join(td, "300", "master.jpg")
                page0_path = os.path.join(td, "300", "300_p0.jpg")
                page1_path = os.path.join(td, "300", "300_p1.jpg")  # intentionally missing
                with open(master_path, "wb") as f:
                    f.write(b"x")
                with open(page0_path, "wb") as f:
                    f.write(b"x")

                db.insertImage(member_id=1, image_id=300, isManga="manga")
                db.updateImage(300, "t", master_path, isManga="manga")
                db.insertMangaImages([(300, 0, page0_path), (300, 1, page1_path)])

                image = _dummy_image(
                    300,
                    mode="manga",
                    urls=[
                        "http://example.com/300_p0.jpg",
                        "http://example.com/300_p1.jpg",
                    ],
                )
                browser = _DummyBrowser(image)

                def fake_make_filename(nameFormat, imageInfo, artistInfo=None, **kwargs):
                    file_url = os.path.basename(kwargs.get("fileUrl", "")) or "file.jpg"
                    return f"{imageInfo.imageId}{os.sep}{file_url}"

                def fake_download_image(caller, url, filename, referer, overwrite, max_retry, backup_old_file, image=None, page=None, notifier=None):
                    os.makedirs(os.path.dirname(filename), exist_ok=True)
                    with open(filename, "wb") as f:
                        f.write(b"x")
                    return PixivConstant.PIXIVUTIL_OK, filename

                with mock.patch.object(PixivImageHandler.PixivBrowserFactory, "getBrowser", return_value=browser), \
                        mock.patch.object(PixivImageHandler.PixivHelper, "make_filename", side_effect=fake_make_filename), \
                        mock.patch.object(PixivImageHandler.PixivDownloadHandler, "download_image", side_effect=fake_download_image):
                    result = PixivImageHandler.process_image(caller, cfg, artist=None, image_id=300)

                self.assertEqual(browser.calls, 1)
                self.assertEqual(result, PixivConstant.PIXIVUTIL_OK)
            finally:
                db.close()

