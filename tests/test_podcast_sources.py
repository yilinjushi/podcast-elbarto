import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from podcast_import import import_file
import podcast_sources
from podcast_sources import enqueue_source


class PodcastSourceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.state = self.root / "queue.json"
        self.env = patch.dict(os.environ, {"PODCAST_SOURCE_DIR": str(self.root / "sources"), "PODCAST_STATE_FILE": str(self.state)})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.source_id = "src-0123456789abcdef"
        self.content = {"title": "Title", "author": "Writer",
                        "items": [{"type": "para", "text": "Complete body with a 2026 date."}, {"type": "image", "src": "https://example.com/image.png"}]}

    def enqueue(self):
        return enqueue_source(source_id=self.source_id, **{key: self.content[key] for key in ("title", "author", "items")})

    def test_full_snapshot_and_versioned_deduplication(self):
        first = self.enqueue()
        snapshot_path = self.state.parent / first["source_path"]
        snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
        self.assertEqual(snapshot["items"], self.content["items"])
        self.assertEqual(snapshot["metadata"]["author"], "Writer")
        self.assertEqual(snapshot["text"], self.content["items"][0]["text"])
        self.assertNotIn("Writer", snapshot_path.with_suffix(".md").read_text())
        self.assertFalse(self.enqueue()["enqueued"])
        self.content["title"] = "Changed title"
        self.assertTrue(self.enqueue()["enqueued"])
        self.assertEqual(len(json.loads(self.state.read_text())["tasks"]), 2)

    def test_retired_task_stays_retired_and_unknown_status_fails_closed(self):
        task = self.enqueue()
        queue = json.loads(self.state.read_text())
        queue["tasks"][task["id"]]["status"] = "retired"
        self.state.write_text(json.dumps(queue))
        self.assertEqual(self.enqueue()["status"], "retired")
        queue["tasks"][task["id"]]["status"] = "surprise"
        self.state.write_text(json.dumps(queue))
        with self.assertRaises(ValueError):
            self.enqueue()

    def test_rejects_unsafe_identity_and_empty_text(self):
        with self.assertRaises(ValueError):
            enqueue_source(source_id="../escape", title="x", author="", items=[])
        with self.assertRaises(ValueError):
            enqueue_source(source_id=self.source_id, title="x", author="", items=[])
        self.assertFalse(self.state.exists())

    def test_failed_atomic_queue_commit_leaves_prior_queue_and_retries(self):
        self.enqueue()
        original = self.state.read_bytes()
        self.content["title"] = "Second revision"
        real_replace = os.replace

        def fail_queue_replace(source, destination):
            if Path(destination) == self.state:
                raise OSError("simulated commit failure")
            return real_replace(source, destination)

        with patch.object(podcast_sources.os, "replace", side_effect=fail_queue_replace):
            with self.assertRaises(OSError):
                self.enqueue()
        self.assertEqual(self.state.read_bytes(), original)
        self.assertEqual(list(self.root.rglob(".pending-*")), [])
        self.assertTrue(self.enqueue()["enqueued"])
        self.assertEqual(len(json.loads(self.state.read_text())["tasks"]), 2)

    def test_manual_file_import_queues_versioned_source(self):
        article = self.root / "articles" / "sample.md"
        article.parent.mkdir()
        article.write_text("# Sample\n\nA complete article.", encoding="utf-8")
        first = import_file(article, article.parent)
        self.assertTrue(first["enqueued"])
        self.assertFalse(import_file(article, article.parent)["enqueued"])
        article.write_text("# Sample\n\nA revised article.", encoding="utf-8")
        self.assertTrue(import_file(article, article.parent)["enqueued"])
        self.assertEqual(len(json.loads(self.state.read_text())["tasks"]), 2)
        with self.assertRaises(ValueError):
            import_file(self.root / "outside.txt", article.parent)


if __name__ == "__main__":
    unittest.main()
