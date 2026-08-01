import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from src.utils.video_manager import VideoManager


class _DirectParser:
    @staticmethod
    def belongto(url):
        return False


class _BuiltPlatformParser:
    @staticmethod
    def belongto(url):
        return "xiaohongshu.com" in url

    def __init__(self, parsefromurl):
        self.parsefromurl = parsefromurl


class VideoManagerParserTest(unittest.TestCase):
    def setUp(self):
        self.manager = VideoManager()
        self.previous_client = self.manager._client

    def tearDown(self):
        self.manager._client = self.previous_client

    def test_platform_parser_does_not_call_generic_browser(self):
        platform_parser = Mock(return_value=[{"source": "RednoteVideoClient"}])
        generic_parser = Mock()
        client = SimpleNamespace(
            video_clients={"RednoteVideoClient": _BuiltPlatformParser(platform_parser)},
            requests_overrides={},
            web_media_grabber=SimpleNamespace(
                source="WebMediaGrabber",
                isprobablydirectmedia=Mock(return_value=(False, None)),
                parsefromurl=generic_parser,
            ),
        )
        self.manager._client = client

        result = self.manager.parse_video_url("https://www.xiaohongshu.com/explore/abc")

        self.assertEqual(result, [{"source": "RednoteVideoClient"}])
        platform_parser.assert_called_once()
        generic_parser.assert_not_called()

    def test_unsupported_page_does_not_open_generic_browser(self):
        generic_parser = Mock()
        client = SimpleNamespace(
            video_clients={"DouyinVideoClient": {"cls": _DirectParser}},
            requests_overrides={},
            web_media_grabber=SimpleNamespace(
                source="WebMediaGrabber",
                isprobablydirectmedia=Mock(return_value=(False, None)),
                parsefromurl=generic_parser,
            ),
        )
        self.manager._client = client

        result = self.manager.parse_video_url("https://example.com/post/abc")

        self.assertEqual(result, [])
        generic_parser.assert_not_called()

    def test_direct_media_url_can_use_request_only_grabber(self):
        direct_parser = Mock(return_value=[{"source": "WebMediaGrabber"}])
        client = SimpleNamespace(
            video_clients={},
            requests_overrides={},
            web_media_grabber=SimpleNamespace(
                source="WebMediaGrabber",
                isprobablydirectmedia=Mock(return_value=(True, "video/mp4")),
                parsefromurl=direct_parser,
            ),
        )
        self.manager._client = client

        result = self.manager.parse_video_url("https://cdn.example.com/video.mp4")

        self.assertEqual(result, [{"source": "WebMediaGrabber"}])
        direct_parser.assert_called_once()


if __name__ == "__main__":
    unittest.main()
