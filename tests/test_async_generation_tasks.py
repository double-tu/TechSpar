import tempfile
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from backend.models import InterviewMode, JobPrepStartRequest
from backend.routers import interview, knowledge


class DummyBackgroundTasks:
    def __init__(self):
        self.calls = []

    def add_task(self, func, *args, **kwargs):
        self.calls.append((func, args, kwargs))


class AsyncGenerationTaskTests(unittest.TestCase):
    def setUp(self):
        interview._task_status.clear()
        interview._drill_sessions.clear()
        interview._job_prep_sessions.clear()
        knowledge._task_status.clear()

    def tearDown(self):
        interview._task_status.clear()
        interview._drill_sessions.clear()
        interview._job_prep_sessions.clear()
        knowledge._task_status.clear()

    def test_job_prep_start_returns_pending_task(self):
        req = JobPrepStartRequest(
            jd_text="A" * 60,
            company="Acme",
            position="Backend",
            use_resume=True,
            preview_data={"company": "Acme"},
        )
        background_tasks = DummyBackgroundTasks()

        # Run the async route explicitly so the task scheduling path is covered.
        import asyncio

        with patch(
            "backend.routers.interview.uuid.uuid4",
            return_value=uuid.UUID("12345678-1234-5678-1234-567812345678"),
        ):
            result = asyncio.run(
                interview.job_prep_start(req, background_tasks=background_tasks, user_id="user-1")
            )

        self.assertEqual(
            result,
            {
                "task_id": "job_prep_12345678",
                "mode": InterviewMode.JD_PREP.value,
                "status": "pending",
            },
        )
        self.assertEqual(len(background_tasks.calls), 1)
        func, args, kwargs = background_tasks.calls[0]
        self.assertIs(func, interview._start_job_prep_background)
        self.assertEqual(args[0], "job_prep_12345678")
        self.assertEqual(args[1], "12345678")
        self.assertEqual(kwargs, {})

    def test_job_prep_preview_returns_pending_task(self):
        req = JobPrepStartRequest(
            jd_text="A" * 60,
            company="Acme",
            position="Backend",
            use_resume=True,
        )
        background_tasks = DummyBackgroundTasks()

        import asyncio

        with patch(
            "backend.routers.interview.uuid.uuid4",
            return_value=uuid.UUID("87654321-1234-5678-1234-567812345678"),
        ):
            result = asyncio.run(
                interview.job_prep_preview(req, background_tasks=background_tasks, user_id="user-1")
            )

        self.assertEqual(result["status"], "pending")
        self.assertEqual(len(background_tasks.calls), 1)
        func, args, kwargs = background_tasks.calls[0]
        self.assertIs(func, interview._preview_job_prep_background)
        self.assertEqual(args[0], "job_prep_preview_87654321")
        self.assertEqual(args[1], "A" * 60)
        self.assertEqual(kwargs, {})

    def test_job_prep_background_marks_done(self):
        questions = [{"id": 1, "question": "如何设计缓存"}]
        preview = {"company": "Acme", "position": "Backend"}

        with (
            patch("backend.routers.interview.generate_job_prep_questions", return_value=questions) as mock_questions,
            patch("backend.routers.interview.create_session") as mock_create_session,
        ):
            interview._start_job_prep_background(
                "job_prep_12345678",
                "12345678",
                "A" * 100,
                preview,
                "Acme",
                "Backend",
                True,
                "user-1",
            )

        mock_questions.assert_called_once()
        mock_create_session.assert_called_once()
        self.assertEqual(
            interview._task_status["job_prep_12345678"]["status"],
            "done",
        )
        self.assertEqual(
            interview._task_status["job_prep_12345678"]["result"]["session_id"],
            "12345678",
        )

    def test_job_prep_preview_background_marks_done(self):
        preview = {"company": "Acme", "position": "Backend"}

        with patch("backend.routers.interview.generate_job_prep_preview", return_value=preview):
            interview._preview_job_prep_background(
                "job_prep_preview_87654321",
                "A" * 100,
                "Acme",
                "Backend",
                True,
                "user-1",
            )

        self.assertEqual(
            interview._task_status["job_prep_preview_87654321"]["status"],
            "done",
        )
        self.assertEqual(
            interview._task_status["job_prep_preview_87654321"]["result"]["preview"],
            preview,
        )

    def test_reference_answer_returns_pending_task(self):
        session = {
            "reference_answers": {},
            "questions": [{"id": 1, "question": "什么是缓存"}],
            "topic": "python",
        }
        background_tasks = DummyBackgroundTasks()

        with patch("backend.routers.interview.get_session", return_value=session):
            import asyncio

            result = asyncio.run(
                interview.generate_reference_answer(
                    {"session_id": "sess-1", "question_id": 1},
                    background_tasks=background_tasks,
                    user_id="user-1",
                )
            )

        self.assertEqual(result["status"], "pending")
        self.assertEqual(len(background_tasks.calls), 1)
        func, args, kwargs = background_tasks.calls[0]
        self.assertIs(func, interview._generate_reference_answer_background)
        self.assertEqual(args[0], "reference_answer_sess-1_1")
        self.assertEqual(args[1], "sess-1")
        self.assertEqual(args[2], "1")
        self.assertEqual(kwargs, {})

    def test_reference_answer_background_marks_done(self):
        with (
            patch("backend.routers.interview.load_topics", return_value={"python": {"name": "Python"}}),
            patch("backend.indexer.retrieve_topic_context", return_value=["ref-1"]),
            patch(
                "backend.llm_provider.get_langchain_llm",
                return_value=SimpleNamespace(
                    invoke=lambda messages: SimpleNamespace(content="- a\n> b"),
                ),
            ),
            patch("backend.routers.interview.save_reference_answer") as mock_save,
        ):
            interview._generate_reference_answer_background(
                "reference_answer_sess-1_1",
                "sess-1",
                "1",
                "python",
                "什么是缓存",
                "user-1",
            )

        mock_save.assert_called_once_with("sess-1", "1", "- a\n> b", user_id="user-1")
        self.assertEqual(
            interview._task_status["reference_answer_sess-1_1"]["status"],
            "done",
        )

    def test_get_session_for_resume_recovers_batch_questions_from_transcript(self):
        session = {
            "mode": InterviewMode.TOPIC_DRILL.value,
            "topic": "python",
            "status": "ongoing",
            "review_error": None,
            "transcript": [
                {"role": "assistant", "content": "解释 GIL"},
                {"role": "user", "content": "答案 1"},
                {"role": "assistant", "content": "解释协程"},
            ],
            "questions": [],
            "meta": {},
            "review": None,
        }

        with patch("backend.routers.interview.get_session", return_value=session):
            import asyncio

            result = asyncio.run(
                interview.get_session_for_resume("sess-1", user_id="user-1")
            )

        self.assertEqual(
            result["questions"],
            [
                {"id": 1, "question": "解释 GIL"},
                {"id": 2, "question": "解释协程"},
            ],
        )

    def test_reference_answer_uses_recovered_batch_questions(self):
        session = {
            "reference_answers": {},
            "questions": [],
            "transcript": [
                {"role": "assistant", "content": "什么是缓存"},
                {"role": "user", "content": "答案"},
            ],
            "topic": "python",
        }
        background_tasks = DummyBackgroundTasks()

        with patch("backend.routers.interview.get_session", return_value=session):
            import asyncio

            result = asyncio.run(
                interview.generate_reference_answer(
                    {"session_id": "sess-1", "question_id": 1},
                    background_tasks=background_tasks,
                    user_id="user-1",
                )
            )

        self.assertEqual(result["status"], "pending")
        self.assertEqual(len(background_tasks.calls), 1)
        func, args, kwargs = background_tasks.calls[0]
        self.assertIs(func, interview._generate_reference_answer_background)
        self.assertEqual(args[4], "什么是缓存")
        self.assertEqual(kwargs, {})

    def test_knowledge_generate_returns_pending_task(self):
        background_tasks = DummyBackgroundTasks()

        with patch("backend.routers.knowledge.load_topics", return_value={"python": {"name": "Python", "dir": "python"}}):
            import asyncio

            result = asyncio.run(
                knowledge.generate_core_knowledge("python", background_tasks=background_tasks, user_id="user-1")
            )

        self.assertEqual(result["status"], "pending")
        self.assertEqual(len(background_tasks.calls), 1)
        func, args, kwargs = background_tasks.calls[0]
        self.assertIs(func, knowledge._generate_core_knowledge_background)
        self.assertEqual(args[0], "knowledge_generate_python_user-1")
        self.assertEqual(args[1], "python")
        self.assertEqual(kwargs, {})

    def test_knowledge_generate_background_marks_done(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            with (
                patch("backend.routers.knowledge.get_langchain_llm", return_value=SimpleNamespace(
                    invoke=lambda messages: SimpleNamespace(content="# Python\n## Cache\n- a"),
                )),
                patch.object(type(knowledge.settings), "user_knowledge_path", return_value=base / "users"),
            ):
                knowledge._generate_core_knowledge_background(
                    "knowledge_generate_python_user-1",
                    "python",
                    "Python",
                    "python",
                    "user-1",
                )

            readme = base / "users" / "python" / "README.md"
            self.assertTrue(readme.exists())
            self.assertEqual(
                knowledge._task_status["knowledge_generate_python_user-1"]["status"],
                "done",
            )


if __name__ == "__main__":
    unittest.main()
