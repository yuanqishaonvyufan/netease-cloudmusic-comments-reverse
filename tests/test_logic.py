import sys
import unittest
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_DIR))

import netease_comments as crawler  # noqa: E402


class InputTests(unittest.TestCase):
    def test_parse_plain_song_id(self):
        self.assertEqual(crawler.parse_song_id("204072"), 204072)

    def test_parse_song_url(self):
        self.assertEqual(
            crawler.parse_song_id("https://music.163.com/song?id=204072"),
            204072,
        )

    def test_extract_csrf_cookie(self):
        cookie = "foo=1; __csrf=abc=123; bar=2"
        self.assertEqual(crawler.extract_cookie_value(cookie, "__csrf"), "abc=123")


class PayloadTests(unittest.TestCase):
    def test_first_page_payload(self):
        payload = crawler.build_page_data(204072, 1, 20, 999, "csrf")
        self.assertEqual(payload["rid"], "R_SO_4_204072")
        self.assertEqual(payload["threadId"], "R_SO_4_204072")
        self.assertEqual(payload["cursor"], -1)
        self.assertEqual(payload["pageNo"], 1)

    def test_next_page_uses_previous_cursor(self):
        payload = crawler.build_page_data(204072, 2, 20, 123456, "")
        self.assertEqual(payload["cursor"], 123456)
        self.assertEqual(payload["pageNo"], 2)

    def test_parse_comment(self):
        result = crawler.parse_comment(
            {
                "commentId": 7,
                "content": "测试评论",
                "likedCount": 9,
                "replyCount": 2,
                "time": 1700000000000,
                "timeStr": "2023-11-14",
                "user": {"userId": 8, "nickname": "测试用户"},
                "ipLocation": {"location": "北京"},
            }
        )
        self.assertEqual(result["username"], "测试用户")
        self.assertEqual(result["content"], "测试评论")
        self.assertEqual(result["ip_location"], "北京")


class JavaScriptIntegrationTests(unittest.TestCase):
    def test_main_js_returns_expected_fields(self):
        context = crawler.load_js_context()
        payload = crawler.build_page_data(204072, 1, 20, -1, "")
        encrypted = crawler.encrypt_page(context, payload)
        self.assertTrue(encrypted["params"])
        self.assertEqual(len(encrypted["encSecKey"]), 256)


if __name__ == "__main__":
    unittest.main()
