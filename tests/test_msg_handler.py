import sys
import unittest
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, patch


fake_playwright_utils = ModuleType("src.utils.playwright_utils")
fake_playwright_utils.PlaywrightIpChecker = object
fake_video_manager = ModuleType("src.utils.video_manager")
fake_video_manager.video_manager = SimpleNamespace(client=None)

with patch.dict(
    sys.modules,
    {
        "src.utils.playwright_utils": fake_playwright_utils,
        "src.utils.video_manager": fake_video_manager,
    },
):
    from src.wechat import msg_handler


class QuoteMessageTest(unittest.IsolatedAsyncioTestCase):
    async def test_matched_quote_does_not_check_url_deduplication(self):
        video_id = "7680177090196514929"
        msg_data = {
            "content_xml": {
                "msg": {
                    "title": "沈阳站",
                    "refermsg": {
                        "content": (
                            "链接:"
                            f"https://www.iesdouyin.com/share/video/{video_id}"
                        )
                    },
                }
            },
            "real_content": "",
        }
        dd_sender = SimpleNamespace(
            send=AsyncMock(),
            send_mixed=AsyncMock(),
        )

        with (
            patch.object(msg_handler, "check_keyword", return_value=True),
            patch.object(
                msg_handler,
                "claim_url_if_not_processed",
                new=AsyncMock(return_value=False),
            ) as claim_url,
            patch.object(
                msg_handler,
                "capture_screenshot",
                new=AsyncMock(return_value=None),
            ),
            patch.object(
                msg_handler,
                "process_video_task",
                new=AsyncMock(),
            ),
        ):
            await msg_handler.async_process_message(msg_data, None, dd_sender)

        claim_url.assert_not_awaited()
        dd_sender.send.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
