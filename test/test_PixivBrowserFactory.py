# -*- coding: utf-8 -*-

import http.cookiejar
import unittest

from common.PixivBrowserFactory import PixivBrowser
import common.PixivConfig as PixivConfig
import common.PixivConstant as PixivConstant

PixivConstant.PIXIVUTIL_LOG_FILE = 'pixivutil.test.log'


class _DummyResponse:
    def __init__(self, text: str):
        self._data = text.encode("utf-8")

    def read(self):
        return self._data

    def close(self):
        return


class TestPixivBrowserFactory(unittest.TestCase):
    def test_getMyId_parses_next_data(self):
        cfg = PixivConfig.PixivConfig()
        br = PixivBrowser(cfg, http.cookiejar.LWPCookieJar())
        br._cache = {}

        with open('./test_data/test-homepage-next-data.html', 'r', encoding='utf-8') as f:
            html = f.read()

        br.getMyId(html)

        self.assertEqual(br._myId, 51850385)
        self.assertEqual(br._isPremium, False)
        self.assertEqual(br._xRestrict, 2)

    def test_getPixivPage_enable_cache_false_bypasses_cache(self):
        cfg = PixivConfig.PixivConfig()
        br = PixivBrowser(cfg, http.cookiejar.LWPCookieJar())

        # Avoid leaking cache state between tests (PixivBrowser._cache is a class variable).
        br._cache = {}

        url = 'https://www.pixiv.net/test-cache'
        br._put_to_cache(url, 'CACHED', expiration=3600)

        br.open_with_retry = lambda _req: _DummyResponse('FRESH')

        self.assertEqual(br.getPixivPage(url, enable_cache=True), 'CACHED')
        self.assertEqual(br.getPixivPage(url, enable_cache=False), 'FRESH')


if __name__ == '__main__':
    unittest.main()

