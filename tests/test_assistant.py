import unittest
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from chat_memory import ChatMemory
import server


class AssistantTests(unittest.TestCase):
    def test_new_message_overlaps(self):
        self.assertEqual(server.new_tail(["a", "b"], ["b", "c"]), 1)
        self.assertEqual(server.new_tail(["a", "b"], ["a", "b"]), 0)
        self.assertEqual(server.new_tail(["a"], ["b"]), 0)
        self.assertEqual(server.new_tail([], ["b"]), 1)

    def test_call_detection(self):
        self.assertTrue(server.CALL.search("AI야 질문 있어"))
        self.assertTrue(server.CALL.search("@AI 알려줘"))
        self.assertFalse(server.CALL.search("AI 기술 좋아졌다"))
        self.assertTrue(server.FOLLOWUP.search("그럼 왜 그런 거야?"))

    def test_memory_retention(self):
        memory = ChatMemory(":memory:")
        try:
            at = datetime(2026, 1, 1, 12, tzinfo=ZoneInfo("Asia/Seoul"))
            stamp = at.isoformat(timespec="minutes")
            first = {"_key": "a", "speaker": "참여자 a", "time": stamp, "text": "AI야 질문"}
            second = {"_key": "b", "speaker": "AI 어시스턴트", "time": stamp, "text": "답변"}
            self.assertEqual(len(memory.ingest([first], None, at)), 1)
            self.assertEqual(memory.ingest([first], None, at), [])
            self.assertEqual(len(memory.ingest([first, second], 1, at)), 1)
            self.assertEqual([m["speaker"] for m in memory.context(at, 120, 100)],
                             ["참여자 a", "AI 어시스턴트"])
            memory.ingest([], 0, at + timedelta(hours=25))
            self.assertEqual(memory.count(), 0)
        finally:
            memory.close()

    def test_safe_default(self):
        self.assertFalse(server.DEFAULT_SETTINGS["post_to_chat"])


if __name__ == "__main__":
    unittest.main()
