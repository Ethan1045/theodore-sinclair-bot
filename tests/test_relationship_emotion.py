import unittest

from relationship_emotion import (
    detect_explicit_expulsion,
    detect_explicit_reinvitation,
    is_wilted_rose_question,
    parse_relationship_directive,
    strip_relationship_directive,
)


class RelationshipEmotionTests(unittest.TestCase):
    def test_parses_and_clamps_hidden_state(self):
        raw = (
            "Reply.\n"
            '[RELATIONSHIP_STATE]{"state":"angry","unease":44,"conflict":61,'
            '"anger":120,"sadness":18,"cooling":-2,"note":"她持续轻蔑地说话"}'
            "[/RELATIONSHIP_STATE]"
        )

        parsed = parse_relationship_directive(raw)

        self.assertEqual(parsed["state"], "angry")
        self.assertEqual(parsed["anger"], 100)
        self.assertEqual(parsed["cooling"], 0)
        self.assertEqual(strip_relationship_directive(raw), "Reply.")

    def test_malformed_hidden_state_is_removed(self):
        raw = "Reply.\n[RELATIONSHIP_STATE]not-json[/RELATIONSHIP_STATE]"

        self.assertIsNone(parse_relationship_directive(raw))
        self.assertEqual(strip_relationship_directive(raw), "Reply.")
        self.assertEqual(
            strip_relationship_directive("Reply.\n[RELATIONSHIP_STATE]{truncated"),
            "Reply.",
        )

    def test_expulsion_requires_direct_language(self):
        self.assertTrue(detect_explicit_expulsion("你滚开，别再来找我"))
        self.assertTrue(detect_explicit_expulsion("不要再主动给我发消息了"))
        self.assertTrue(detect_explicit_expulsion("我们分手吧"))
        self.assertFalse(detect_explicit_expulsion("她昨天让我滚开，我很难过"))
        self.assertFalse(detect_explicit_expulsion("我没有说让你走"))

    def test_reinvitation_is_explicit(self):
        self.assertTrue(detect_explicit_reinvitation("你回来吧"))
        self.assertTrue(detect_explicit_reinvitation("刚才是气话"))
        self.assertFalse(detect_explicit_reinvitation("早上好"))

    def test_wilted_rose_question(self):
        self.assertTrue(is_wilted_rose_question("🥀是什么意思？"))
        self.assertTrue(is_wilted_rose_question("枯萎的花代表什么"))
        self.assertFalse(is_wilted_rose_question("给你一朵🥀"))


if __name__ == "__main__":
    unittest.main()
