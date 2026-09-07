from __future__ import annotations

import unittest

from nursery.model_connection import (
    ChildConversationRequest,
    ModelConnectionRequest,
    OpenAICompatibleChildModelAdapter,
    _safe_plain_child_reply,
)


class PlainChildReplyFallbackTests(unittest.TestCase):
    def test_keeps_short_chinese_reply(self):
        self.assertEqual(_safe_plain_child_reply("好呀，我听见你啦。"), "好呀，我听见你啦。")

    def test_recovers_reply_from_truncated_json_and_refuses_non_chinese_output(self):
        self.assertEqual(_safe_plain_child_reply('{"reply":"好呀"'), "好呀")
        self.assertEqual(_safe_plain_child_reply("hello"), "")

    def test_keeps_reply_from_json_wrapper(self):
        self.assertEqual(
            _safe_plain_child_reply('{"reply":"好呀，我在呢。","intent":"talk"}'),
            "好呀，我在呢。",
        )

    def test_plain_dialogue_is_separate_from_background_extraction(self):
        contents = iter([
            "好呀，我在听。",
            '{"intent":"talk","runtime_state":{},"care_action":{"kind":"none","status":"none","object_name":"","summary":""}}',
        ])
        calls = []

        class Response:
            def __init__(self, content):
                message = type("Message", (), {"content": content})()
                self.choices = [type("Choice", (), {"message": message})()]

        class Completions:
            def create(self, **kwargs):
                calls.append(kwargs)
                return Response(next(contents))

        completions = Completions()
        client = type("Client", (), {"chat": type("Chat", (), {"completions": completions})()})()
        adapter = OpenAICompatibleChildModelAdapter(lambda **_kwargs: client)
        state = {
            "thirst": 0.8, "hunger": 0.8, "fatigue": 0.8, "comfort": 0.2,
            "connection": 0.4, "play_drive": 0.1, "unwell": 0.0,
        }
        result = adapter.respond(
            ModelConnectionRequest("openai_compatible", "https://models.example/v1", "child", "secret-key"),
            ChildConversationRequest("喝水吗？", "early_walker", state, []),
        )
        self.assertEqual(result.reply, "好呀，我在听。")
        self.assertEqual(result.care_action["kind"], "none")
        self.assertEqual(len(calls), 2)
        self.assertNotIn("response_format", calls[0])
        self.assertIn("response_format", calls[1])

    def test_failed_background_extraction_does_not_silence_plain_reply(self):
        contents = iter(["妈妈，我在搭积木呀～", "{broken analysis}"])

        class Response:
            def __init__(self, content):
                message = type("Message", (), {"content": content})()
                self.choices = [type("Choice", (), {"message": message})()]

        class Completions:
            def create(self, **_kwargs):
                return Response(next(contents))

        client = type("Client", (), {"chat": type("Chat", (), {"completions": Completions()})()})()
        adapter = OpenAICompatibleChildModelAdapter(lambda **_kwargs: client)
        state = {"thirst": .2, "hunger": .2, "fatigue": .2, "comfort": .5,
                 "connection": .5, "play_drive": .5, "unwell": 0.0}
        result = adapter.respond(
            ModelConnectionRequest("openai_compatible", "https://models.example/v1", "child", "secret-key"),
            ChildConversationRequest("你在做什么？", "early_walker", state, []),
        )
        self.assertEqual(result.reply, "妈妈，我在搭积木呀～")
        self.assertEqual(result.runtime_state, state)


if __name__ == "__main__":
    unittest.main()
