import os
import unittest

os.environ.setdefault("DISCORD_TOKEN", "test-token")
os.environ.setdefault("PARTNER_USER_ID", "1")
os.environ["DATABASE_URL"] = ""

import relationship_state


class RelationshipStateTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        relationship_state.config.DATABASE_URL = ""
        relationship_state._memory_states.clear()

    async def test_expulsion_blocks_and_reinvitation_restores_proactive_contact(self):
        state = await relationship_state.apply_relationship_boundary(
            1, "不要再主动找我了"
        )

        self.assertTrue(state["proactive_blocked"])
        self.assertTrue(await relationship_state.proactive_is_blocked(1))

        state = await relationship_state.apply_relationship_boundary(1, "你回来吧")

        self.assertFalse(state["proactive_blocked"])
        self.assertEqual(state["state"], "cooling")
        self.assertFalse(await relationship_state.proactive_is_blocked(1))

    async def test_model_update_cannot_clear_explicit_boundary(self):
        await relationship_state.apply_relationship_boundary(1, "我们分手吧")
        raw = (
            "Fine.\n"
            '[RELATIONSHIP_STATE]{"state":"normal","unease":0,"conflict":0,'
            '"anger":0,"sadness":0,"cooling":0,"note":"她打了招呼"}'
            "[/RELATIONSHIP_STATE]"
        )

        visible, state = await relationship_state.update_relationship_from_reply(1, raw)

        self.assertEqual(visible, "Fine.")
        self.assertTrue(state["proactive_blocked"])


if __name__ == "__main__":
    unittest.main()
