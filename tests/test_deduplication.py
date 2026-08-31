import unittest
from unittest.mock import AsyncMock, patch

from src.utils.deduplication import (
    PROCESSED_URLS_CACHE,
    claim_url_if_not_processed,
    get_url_dedup_key,
)


class UrlDedupKeyTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        PROCESSED_URLS_CACHE.clear()

    def tearDown(self):
        PROCESSED_URLS_CACHE.clear()

    async def test_douyin_and_iesdouyin_share_the_same_video_key(self):
        video_id = "7680177090196514929"

        douyin_key = await get_url_dedup_key(
            f"https://www.douyin.com/share/video/{video_id}"
        )
        iesdouyin_key = await get_url_dedup_key(
            f"https://www.iesdouyin.com/share/video/{video_id}"
        )

        self.assertEqual(douyin_key, f"douyin:{video_id}")
        self.assertEqual(iesdouyin_key, douyin_key)

    async def test_douyin_url_variants_cannot_both_claim_the_same_video(self):
        video_id = "7680177090196514929"

        first_claimed = await claim_url_if_not_processed(
            f"https://www.douyin.com/share/video/{video_id}"
        )
        second_claimed = await claim_url_if_not_processed(
            f"https://www.iesdouyin.com/share/video/{video_id}"
        )

        self.assertTrue(first_claimed)
        self.assertFalse(second_claimed)

    async def test_douyin_video_page_ignores_query_and_fragment(self):
        video_id = "7680177090196514929"

        key = await get_url_dedup_key(
            f"https://www.douyin.com/video/{video_id}?from=copy#detail"
        )

        self.assertEqual(key, f"douyin:{video_id}")

    async def test_douyin_short_url_uses_resolved_video_id(self):
        short_url = "https://v.douyin.com/example/"
        video_id = "7680177090196514929"

        with patch(
            "src.utils.deduplication._resolve_douyin_short_url",
            new=AsyncMock(return_value=video_id),
        ) as resolver:
            key = await get_url_dedup_key(short_url)

        self.assertEqual(key, f"douyin:{video_id}")
        resolver.assert_awaited_once_with(short_url)

    async def test_non_platform_url_keeps_its_normalized_url_key(self):
        url = "https://example.com/share/video/7680177090196514929"

        key = await get_url_dedup_key(url)

        self.assertEqual(key, url)

    async def test_existing_xhs_note_key_is_unchanged(self):
        note_id = "6a959628000000002902cd67"

        key = await get_url_dedup_key(
            f"https://www.xiaohongshu.com/explore/{note_id}?xsec_source=app_share"
        )

        self.assertEqual(key, f"xhs:{note_id}")


if __name__ == "__main__":
    unittest.main()
