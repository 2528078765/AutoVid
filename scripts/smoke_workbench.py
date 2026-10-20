"""Photo reuse and workbench regression checks, isolated from user assets and cloud APIs."""
from __future__ import annotations

import json
import io
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
import wave
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from autovid.assets import AssetStore  # noqa: E402
from autovid.config import Config  # noqa: E402
from autovid.graph import VideoFlow  # noqa: E402
from autovid import providers as P  # noqa: E402
from autovid.web.server import Handler, Workbench, build_flow_result  # noqa: E402


class WorkbenchTests(unittest.TestCase):
    def setUp(self) -> None:
        (ROOT / ".tmp").mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="workbench-", dir=ROOT / ".tmp")
        self.root = Path(self.temp.name)
        self.config = Config.load(root=self.root)
        self.probe = patch("autovid.media.probe_media", return_value={"has_video": True, "width": 640, "height": 800})
        self.probe.start()
        self.store = AssetStore(self.config)
        self.avatar = self.store.create_avatar("测试人物")
        self.avatar = self.store.save_avatar_photo(self.avatar.id, b"fixture picture", "library.png")
        self.scenes = self.root / "scenes"
        self.scenes.mkdir()
        (self.scenes / "saved.png").write_bytes(b"fixture picture")
        (self.scenes / "ignore.txt").write_text("not a photo", encoding="utf-8")
        app = Workbench(self.config)
        handler = type("TestWorkbenchHandler", (Handler,), {"app": app})
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.worker = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.worker.start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.worker.join(timeout=5)
        self.probe.stop()
        self.temp.cleanup()

    def request(self, path: str, payload: dict | None = None) -> tuple[int, dict]:
        request = urllib.request.Request(self.base + path,
            data=json.dumps(payload).encode() if payload is not None else None,
            headers={"Content-Type": "application/json"})
        try:
            response = self.opener.open(request, timeout=10)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            return response.code, json.loads(response.read().decode())

    def test_lists_saved_and_library_photos_without_paths(self) -> None:
        status, data = self.request("/api/scenes")
        self.assertEqual(status, 200)
        self.assertEqual(len(data["photos"]), 2)
        self.assertTrue(all("path" not in photo for photo in data["photos"]))
        self.assertEqual({photo["name"] for photo in data["photos"]}, {"saved.png", "library.png"})

    def test_select_reuses_file_without_copying_or_changing_primary(self) -> None:
        before = list(self.scenes.iterdir())
        status, selected = self.request("/api/scene/select", {"name": "saved.png"})
        self.assertEqual(status, 200)
        self.assertEqual(Path(selected["path"]), (self.scenes / "saved.png").resolve())
        status, selected = self.request("/api/scene/select", {"avatar_id": self.avatar.id, "filename": "library.png"})
        self.assertEqual(status, 200)
        self.assertEqual(selected["avatar_id"], self.avatar.id)
        self.assertEqual(self.store.get_avatar(self.avatar.id).portrait, "library.png")
        self.assertEqual(before, list(self.scenes.iterdir()))

    def test_invalid_paths_and_stale_selection_are_rejected(self) -> None:
        for name in ("../saved.png", "..\\saved.png", "C:\\outside.png", "missing.png", "ignore.txt", ""):
            self.assertEqual(self.request("/api/scene/select", {"name": name})[0], 400)
        self.assertEqual(self.request("/api/scene/select", {"avatar_id": self.avatar.id, "filename": "other.png"})[0], 400)
        (self.scenes / "saved.png").unlink()
        self.assertEqual(self.request("/api/scene/select", {"name": "saved.png"})[0], 400)

    def test_bad_dimensions_rejected(self) -> None:
        with patch("autovid.media.probe_media", return_value={"has_video": True, "width": 100, "height": 100}):
            self.assertEqual(self.request("/api/scene/select", {"name": "saved.png"})[0], 400)

    def test_scene_only_requirement_comes_from_adapter_config(self) -> None:
        self.assertTrue(P.avatar_uses_scene_photo(self.config, "local_wav2lip"))
        self.assertFalse(P.avatar_uses_scene_photo(self.config, "comfy"))
        with patch("autovid.providers_registry.resolve", return_value={"cfg": {"use_scene_photo": True}}):
            self.assertTrue(P.avatar_uses_scene_photo(self.config, "avatar:test"))
        with patch("autovid.providers_registry.resolve", return_value={"cfg": {"use_scene_photo": False}}):
            self.assertFalse(P.avatar_uses_scene_photo(self.config, "avatar:test"))

    def test_flow_only_requires_identity_for_identity_adapters(self) -> None:
        state = {"topic": "test", "scene_photo": str(self.scenes / "saved.png"), "run_dir": str(self.root / "runs/test")}
        flow = VideoFlow(self.config, log=lambda line: None)
        with patch("autovid.providers.avatar_uses_scene_photo", return_value=True):
            errors = flow.node_preflight(state)["errors"]
            self.assertFalse(any("形象" in error for error in errors), errors)
        with patch("autovid.providers.avatar_uses_scene_photo", return_value=False):
            errors = flow.node_preflight(state)["errors"]
            self.assertTrue(any("没有选择形象" in error for error in errors), errors)

    def test_static_resources_and_scene_usage_result_flag(self) -> None:
        for path in ("/", "/static/workbench.css", "/static/workbench.js"):
            with self.opener.open(self.base + path) as response:
                self.assertEqual(response.status, 200)
                self.assertGreater(len(response.read()), 100)
        result = build_flow_result("test", self.root, {"avatar": {"uses_scene_photo": True}})
        self.assertTrue(result["avatar"]["uses_scene_photo"])

    def test_flow_plan_uses_graph_contract_without_persisting_runs(self) -> None:
        before = list((self.root / "runs").iterdir()) if (self.root / "runs").exists() else []
        with patch("autovid.providers.avatar_uses_scene_photo", return_value=True):
            status, plan = self.request("/api/plan", {"mode": "flow", "topic": "test", "scene_photo": str(self.scenes / "saved.png")})
        self.assertEqual(status, 200)
        self.assertEqual(len(plan["steps"]), 8)
        self.assertFalse(any("形象" in error for error in plan["problems"]))
        self.assertFalse(any(step["name"] == "publish" for step in plan["steps"]))
        after = list((self.root / "runs").iterdir()) if (self.root / "runs").exists() else []
        self.assertEqual(before, after)

    def test_reference_url_and_text_api_are_asset_scoped(self) -> None:
        voice = self.store.create_voice("测试人声")
        buffer = io.BytesIO()
        with wave.open(buffer, "wb") as writer:
            writer.setparams((1, 2, 24000, 0, "NONE", "not compressed"))
            writer.writeframes(b"\x00\x10" * 24000 * 6)
        with patch("autovid.media.probe_media", return_value={"has_audio":True,"duration":6}):
            self.store.save_voice_reference(voice.id, buffer.getvalue(), "sample.wav")
        status, saved = self.request("/api/assets/voice/reference-url", {
            "voice_id":voice.id, "url":"https://fixture.example/reference.wav?signature=private"})
        self.assertEqual(status, 200)
        self.assertTrue(saved["voice"]["cloud_reference_url"].startswith("https://fixture.example/"))
        status, saved = self.request("/api/assets/voice/reference-text", {
            "voice_id":voice.id, "ref_text":"这才是实际朗读的内容。"})
        self.assertEqual(status, 200)
        self.assertEqual(saved["voice"]["ref_text"], "这才是实际朗读的内容。")
        for voice_id, url in ((voice.id, "https://127.0.0.1/ref.wav"), ("../outside", "https://fixture.example/a")):
            status, result = self.request("/api/assets/voice/reference-url", {"voice_id":voice_id,"url":url})
            self.assertEqual(status, 400)
            self.assertIn("error", result)

    def test_voice_creation_api_requires_chinese_names_without_partial_assets(self) -> None:
        for name in ("csy", "声音123", "声音 测试", "声音-", "声音😀", ""):
            with self.subTest(name=name):
                before = self.store.list_voices()
                status, result = self.request("/api/assets/voice", {"name": name})
                self.assertEqual(status, 400)
                self.assertIn("中文", result["error"])
                self.assertEqual(self.store.list_voices(), before)
        status, result = self.request("/api/assets/voice", {"name": "讲解声音"})
        self.assertEqual(status, 200)
        self.assertEqual(result["voice"]["name"], "讲解声音")

    def test_subtitle_choice_reaches_plan_without_changing_defaults(self) -> None:
        status, boot = self.request("/api/bootstrap")
        self.assertEqual(status, 200)
        self.assertTrue(boot["generation_options"]["subtitles_enabled"])
        for enabled in (True,False):
            status, plan = self.request("/api/plan", {"mode":"flow", "topic":"测试",
                "overrides":{"steps.subtitles.enabled":enabled}})
            self.assertEqual(status, 200)
            subtitle = next(step for step in plan["steps"] if step["name"] == "subtitles")
            self.assertEqual(subtitle["action"], "run" if enabled else "skip")
            if not enabled:
                self.assertEqual(subtitle["reason"], "已关闭字幕")
        self.assertTrue(self.config.step_enabled("subtitles"))
        status, legacy = self.request("/api/plan", {"mode":"pipeline", "topic":"测试",
            "overrides":{"steps.subtitles.enabled":False}})
        self.assertEqual(status,200)
        self.assertNotIn("subtitles",[step["name"] for step in legacy["steps"]])


if __name__ == "__main__":
    unittest.main(verbosity=2)
