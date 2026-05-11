import asyncio
import unittest
import uuid
from types import SimpleNamespace
from unittest.mock import patch

from backend.models import InterviewMode, StartInterviewRequest
from backend.routers import interview


class DummyBackgroundTasks:
    def __init__(self):
        self.calls = []

    def add_task(self, func, *args, **kwargs):
        self.calls.append((func, args, kwargs))


class TopicDrillStartTests(unittest.TestCase):
    def setUp(self):
        interview._task_status.clear()
        interview._drill_sessions.clear()

    def tearDown(self):
        interview._task_status.clear()
        interview._drill_sessions.clear()

    def test_start_interview_returns_pending_task_for_topic_drill(self):
        req = StartInterviewRequest(mode=InterviewMode.TOPIC_DRILL, topic="python")
        background_tasks = DummyBackgroundTasks()

        with (
            patch("backend.routers.interview.load_topics", return_value={"python": {"name": "Python"}}),
            patch(
                "backend.routers.interview.load_user_settings",
                return_value=SimpleNamespace(num_questions=8, divergence=4),
            ),
            patch(
                "backend.routers.interview.uuid.uuid4",
                return_value=uuid.UUID("12345678-1234-5678-1234-567812345678"),
            ),
        ):
            result = asyncio.run(
                interview.start_interview(req, background_tasks=background_tasks, user_id="user-1")
            )

        self.assertEqual(
            result,
            {
                "task_id": "topic_drill_12345678",
                "mode": InterviewMode.TOPIC_DRILL.value,
                "topic": "python",
                "status": "pending",
            },
        )
        self.assertEqual(
            interview._task_status["topic_drill_12345678"],
            {"status": "pending", "type": "topic_drill_start"},
        )
        self.assertEqual(len(background_tasks.calls), 1)

        func, args, kwargs = background_tasks.calls[0]
        self.assertIs(func, interview._start_drill_background)
        self.assertEqual(args, ("topic_drill_12345678", "12345678", "python", "user-1", 8, 4))
        self.assertEqual(kwargs, {})

    def test_start_drill_background_persists_session_and_marks_task_done(self):
        questions = [{"id": 1, "question": "解释 GIL"}]

        with (
            patch("backend.routers.interview.generate_drill_questions", return_value=questions) as mock_generate,
            patch("backend.routers.interview.create_session") as mock_create_session,
        ):
            interview._start_drill_background(
                "topic_drill_12345678",
                "12345678",
                "python",
                "user-1",
                10,
                3,
            )

        mock_generate.assert_called_once_with(
            "python",
            "user-1",
            num_questions=10,
            divergence=3,
        )
        mock_create_session.assert_called_once_with(
            "12345678",
            InterviewMode.TOPIC_DRILL.value,
            "python",
            questions=questions,
            user_id="user-1",
        )
        self.assertEqual(
            interview._drill_sessions["12345678"],
            {"topic": "python", "questions": questions, "user_id": "user-1"},
        )
        self.assertEqual(
            interview._task_status["topic_drill_12345678"],
            {
                "status": "done",
                "type": "topic_drill_start",
                "result": {
                    "session_id": "12345678",
                    "mode": InterviewMode.TOPIC_DRILL.value,
                    "topic": "python",
                    "questions": questions,
                },
            },
        )

    def test_start_drill_background_marks_task_error_when_generation_fails(self):
        with (
            patch(
                "backend.routers.interview.generate_drill_questions",
                side_effect=RuntimeError("LLM provider timeout"),
            ),
            patch("backend.routers.interview.create_session") as mock_create_session,
        ):
            interview._start_drill_background(
                "topic_drill_12345678",
                "12345678",
                "python",
                "user-1",
                10,
                3,
            )

        mock_create_session.assert_not_called()
        self.assertNotIn("12345678", interview._drill_sessions)
        self.assertEqual(
            interview._task_status["topic_drill_12345678"],
            {
                "status": "error",
                "type": "topic_drill_start",
                "error": "LLM provider timeout",
            },
        )


if __name__ == "__main__":
    unittest.main()
