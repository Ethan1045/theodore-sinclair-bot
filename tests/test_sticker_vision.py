import asyncio
import contextlib
import io
import os
import unittest

os.environ.setdefault("DISCORD_TOKEN", "test-token")
os.environ.setdefault("OPENAI_API_KEY", "test-key")
os.environ.setdefault("PARTNER_USER_ID", "1")

from events import _media_description, _vision_parts


class _Format:
    def __init__(self, name: str):
        self.name = name


class _Sticker:
    def __init__(self, sticker_id: int, name: str, format_name: str, raw: bytes = b"png"):
        self.id = sticker_id
        self.name = name
        self.format = _Format(format_name)
        self._raw = raw
        self.read_count = 0

    async def read(self) -> bytes:
        self.read_count += 1
        return self._raw


class _Message:
    def __init__(self, stickers):
        self.stickers = stickers
        self.attachments = []


class StickerVisionTests(unittest.TestCase):
    def test_description_preserves_name_and_format(self):
        message = _Message([_Sticker(1, "hello", "png"), _Sticker(2, "wave", "lottie")])

        self.assertEqual(
            _media_description(message),
            "贴纸「hello」（png）、贴纸「wave」（lottie）",
        )

    def test_visual_formats_are_read_and_deduplicated(self):
        png = _Sticker(1, "hello", "png", b"png-bytes")
        gif = _Sticker(2, "wave", "gif", b"gif-bytes")

        parts = asyncio.run(_vision_parts([_Message([png]), _Message([png, gif])]))

        self.assertEqual(len(parts), 2)
        self.assertTrue(parts[0]["image_url"]["url"].startswith("data:image/png;base64,"))
        self.assertTrue(parts[1]["image_url"]["url"].startswith("data:image/gif;base64,"))
        self.assertEqual(png.read_count, 1)
        self.assertEqual(gif.read_count, 1)

    def test_lottie_is_described_but_not_sent_as_image(self):
        lottie = _Sticker(1, "animated", "lottie", b"json")

        # Some Windows shells still default to GBK; capture the informational
        # emoji log so the media behaviour itself is what this test exercises.
        with contextlib.redirect_stdout(io.StringIO()):
            parts = asyncio.run(_vision_parts([_Message([lottie])]))

        self.assertEqual(parts, [])
        self.assertEqual(lottie.read_count, 0)


if __name__ == "__main__":
    unittest.main()
