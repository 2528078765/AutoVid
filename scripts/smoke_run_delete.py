"""在隔离目录验证历史删除、本地文件保留和检查点清理，不操作真实运行。"""
from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from contextlib import closing
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from autovid.config import Config  # noqa: E402
from autovid.manifest import HISTORY_DELETED, RunContext  # noqa: E402
from autovid.web.server import Handler, RunSession, Workbench  # noqa: E402
from langgraph.checkpoint.base import empty_checkpoint  # noqa: E402
from langgraph.checkpoint.sqlite import SqliteSaver  # noqa: E402


class HistoryDeleteTests(unittest.TestCase):
    def setUp(self) -> None:
        (ROOT / ".tmp").mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="history-delete-", dir=ROOT / ".tmp")
        self.root = Path(self.temp.name).resolve()
        self.config = Config.load(root=self.root)
        self.old = RunContext.create(self.config, run_id="20261008-120000-旧运行")
        self.runs = self.old.dir.parent
        self.run_id = "20261008-120001-异常运行"
        self.folder = self.runs / self.run_id
        (self.folder / "voice").mkdir(parents=True)
        self.audio = self.folder / "voice" / "voice.wav"
        self.audio.write_bytes(b"fixture audio")
        (self.folder / "flow_state.json").write_text(json.dumps({
            "run_id": self.run_id, "status": "failed", "next": ["avatar"],
            "thread_id": f"flow-{self.run_id}", "topic": "异常运行",
        }), encoding="utf-8")
        self.db = self.runs / "_graph" / "flow.sqlite"
        self.db.parent.mkdir()
        with SqliteSaver.from_conn_string(str(self.db)) as saver:
            for run_id in (self.run_id, self.old.run_id):
                config = {"configurable": {"thread_id": f"flow-{run_id}", "checkpoint_ns": ""}}
                saved = saver.put(config, empty_checkpoint(), {"source": "input", "step": 0}, {})
                saver.put_writes(saved, [("message", "fixture")], "task")
        self.asset = self.root / "assets" / "voice.wav"
        self.asset.parent.mkdir()
        self.asset.write_bytes(b"reference")
        self.app = Workbench(self.config)
        handler = type("TestHandler", (Handler,), {"app": self.app})
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.server.daemon_threads = True
        self.worker = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.worker.start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.worker.join(timeout=5)
        self.assertEqual(self.root.parent, (ROOT / ".tmp").resolve())
        self.temp.cleanup()

    def request(self, path: str, payload: dict | None = None) -> tuple[int, dict]:
        request = urllib.request.Request(
            self.base + path, method="DELETE" if payload is not None else "GET",
            data=json.dumps(payload).encode() if payload is not None else None,
            headers={"Content-Type": "application/json"},
        )
        try:
            response = self.opener.open(request, timeout=10)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            return response.code, json.loads(response.read().decode("utf-8"))

    def checkpoint_count(self, run_id: str, table: str) -> int:
        self.assertIn(table, ("checkpoints", "writes"))
        with closing(sqlite3.connect(str(self.db))) as connection:
            return connection.execute(f"SELECT count(*) FROM {table} WHERE thread_id=?",
                                      (f"flow-{run_id}",)).fetchone()[0]

    def test_keep_files_and_hide_after_restart(self) -> None:
        self.app.sessions[self.run_id] = RunSession(self.run_id, finished=True, mode="flow")
        status, data = self.request("/api/runs", {"run_id": self.run_id})
        self.assertEqual(status, 200)
        self.assertFalse(data["files_deleted"])
        self.assertEqual(self.audio.read_bytes(), b"fixture audio")
        self.assertTrue((self.folder / HISTORY_DELETED).is_file())
        self.assertNotIn(self.run_id, self.app.sessions)
        restarted = Workbench(self.config)
        self.assertNotIn(self.run_id, {item["run_id"] for item in restarted.runs()})
        self.assertIsNone(restarted.run_dir(self.run_id))
        with self.assertRaisesRegex(Exception, "找不到"):
            restarted.continue_failed_flow(self.run_id)
        self.assertGreater(self.checkpoint_count(self.run_id, "checkpoints"), 0)
        self.assertEqual(self.request("/api/result?run_id=" + urllib.parse.quote(self.run_id))[0], 404)

    def test_page_exposes_history_delete_button_and_file_choice(self) -> None:
        with self.opener.open(self.base + "/", timeout=10) as response:
            html = response.read().decode("utf-8")
        for control in ("data-delete-run", "deleteRunDialog", "deleteRunFiles", "confirmDeleteRun"):
            self.assertIn(control, html)

    def test_delete_files_and_only_selected_checkpoints(self) -> None:
        status, data = self.request("/api/runs", {"run_id": self.run_id, "delete_files": True})
        self.assertEqual(status, 200)
        self.assertTrue(data["files_deleted"])
        self.assertFalse(self.folder.exists())
        for table in ("checkpoints", "writes"):
            self.assertEqual(self.checkpoint_count(self.run_id, table), 0)
            self.assertGreater(self.checkpoint_count(self.old.run_id, table), 0)
        self.assertEqual(self.asset.read_bytes(), b"reference")
        self.assertTrue(self.old.dir.exists())
        self.assertEqual(self.request("/api/runs", {"run_id": self.run_id})[0], 404)

    def test_manifest_hidden_and_latest_pointer_falls_back(self) -> None:
        newer = RunContext.create(self.config, run_id="20261008-120002-最新运行")
        self.assertEqual(self.request("/api/runs", {"run_id": newer.run_id})[0], 200)
        self.assertTrue((newer.dir / "manifest.json").is_file())
        self.assertNotIn(newer.run_id, {row["run_id"] for row in self.request("/api/runs")[1]["runs"]})
        self.assertEqual(RunContext.open_latest(self.config).run_id, self.old.run_id)

    def test_active_run_refused(self) -> None:
        self.app.sessions[self.run_id] = RunSession(self.run_id, mode="flow")
        self.app._active_run = self.run_id
        rows = self.request("/api/runs")[1]["runs"]
        self.assertTrue(next(row for row in rows if row["run_id"] == self.run_id)["running"])
        for option in (False, True):
            self.assertEqual(self.request("/api/runs", {"run_id": self.run_id, "delete_files": option})[0], 409)
        self.assertTrue(self.audio.exists())
        self.assertFalse((self.folder / HISTORY_DELETED).exists())

    def test_invalid_paths_and_options_refused(self) -> None:
        for run_id in ("", ".", "..", "_graph", "../assets", "..\\assets", str(self.root)):
            self.assertEqual(self.request("/api/runs", {"run_id": run_id, "delete_files": True})[0], 404)
        for option in ("false", 1, None):
            self.assertEqual(self.request("/api/runs", {"run_id": self.run_id, "delete_files": option})[0], 400)
        self.assertTrue(self.asset.exists())

    def test_locked_file_reports_error_and_preserves_checkpoints(self) -> None:
        with patch("autovid.web.server.shutil.rmtree", side_effect=PermissionError("文件正在使用")):
            status, data = self.request("/api/runs", {"run_id": self.run_id, "delete_files": True})
        self.assertEqual(status, 500)
        self.assertIn("文件正在使用", data["error"])
        self.assertTrue(self.audio.exists())
        self.assertGreater(self.checkpoint_count(self.run_id, "checkpoints"), 0)
        self.assertGreater(self.checkpoint_count(self.run_id, "writes"), 0)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    unittest.main(verbosity=2)
