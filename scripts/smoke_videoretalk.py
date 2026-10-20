"""Offline regression checks: real FFmpeg media, mocked cloud, isolated local HTTP."""
from __future__ import annotations

import copy
import json
import shutil
import sys
import tempfile
import unittest
import urllib.request
import wave
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
from autovid import media as M, providers as P
from autovid.errors import AutoVidError
from autovid.config import Config
from autovid.graph import VideoFlow
from autovid.providers_registry import PRESETS, preset_options, upsert_entry, test_connection
from autovid.videoretalk import render, validate_source
import smoke_workbench as workbench_checks


class VideoRetalkTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix="retalk-", dir=ROOT / ".tmp")
        cls.root = Path(cls.temp.name)
        cls.source = cls.root / "人物底片.mp4"
        cls.result = cls.root / "result.mp4"
        for path, duration in ((cls.source, 6), (cls.result, 3)):
            M.run_ffmpeg(["-f", "lavfi", "-i", "color=c=blue:s=640x800:r=30",
                          "-t", str(duration), "-c:v", "libx264", "-pix_fmt", "yuv420p",
                          path.name], cwd=cls.root)
        cls.audio = cls.root / "voice.wav"
        cls.write_audio(cls.audio, 3)

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    @staticmethod
    def write_audio(path, duration):
        with wave.open(str(path), "wb") as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(16000)
            handle.writeframes(b"\0\0" * int(duration * 16000))

    def setUp(self):
        self.out = self.root / self._testMethodName
        self.cfg = copy.deepcopy(PRESETS["dashscope_videoretalk"]["config"])
        self.cfg.update({"poll_interval_s": 0, "timeout_s": 5})
        self.segments = [{"audio_path": str(self.audio)}]

    def call(self):
        return render(self.source, self.segments, self.out, self.cfg, "fixture-key", "avatar:test", lambda _: None)

    @staticmethod
    def success(url, payload=None, **kwargs):
        if kwargs.get("method") == "POST":
            assert payload["parameters"]["video_extension"] is False
            assert set(payload["input"]) == {"video_url", "audio_url"}
            return {"output": {"task_id": "existing-task"}}
        return {"output": {"task_status": "SUCCEEDED", "video_url": "https://fixture.invalid/video"}}

    def download(self, url, path, **kwargs):
        shutil.copy2(self.result, path)
        return path

    def test_presets_keep_original_modes(self):
        options = {item["name"]: item for item in preset_options()}
        self.assertIn("dashscope_s2v", options)
        self.assertIn("fal", options)
        self.assertIn("dashscope_videoretalk", options)
        self.assertEqual(PRESETS["dashscope_s2v"]["config"]["model"], "wan2.2-s2v")
        self.assertEqual(options["dashscope_videoretalk"]["default_model"], "videoretalk")
        self.assertFalse(options["dashscope_videoretalk"]["fields"])

    def test_real_source_validation_and_limits(self):
        info = validate_source(self.source)
        self.assertEqual(info["video_codec"], "h264")
        self.assertEqual(info["fps"], 30)
        for changes in ({"duration": 2}, {"duration": 120}, {"width": 400},
                        {"height": 2160}, {"fps": 10}, {"fps": 61},
                        {"video_codec": "mpeg4"}, {"has_video": False}):
            with self.subTest(changes=changes), patch("autovid.media.probe_media", return_value={**info, **changes}):
                with self.assertRaises(AutoVidError):
                    validate_source(self.source)

    def test_short_source_rejected_before_upload(self):
        audio = self.root / "too-long.wav"
        self.write_audio(audio, 7)
        self.segments = [{"audio_path": str(audio)}]
        with patch.object(P, "_dashscope_oss_upload") as upload:
            with self.assertRaisesRegex(P.ProviderError, "底片只有"):
                self.call()
            upload.assert_not_called()

    def test_success_reuses_download_without_network(self):
        with patch.object(P, "_dashscope_oss_upload", return_value="oss://fixture") as upload, \
             patch.object(P, "_http", side_effect=self.success) as http, \
             patch.object(P, "_http_download", side_effect=self.download):
            clips = self.call()
            self.assertEqual(upload.call_count, 2)
            self.assertEqual(http.call_count, 2)
            self.assertEqual(M.probe_media(clips[0])["duration"], 3)
            self.assertEqual(self.call(), clips)
            self.assertEqual(http.call_count, 2)
            record = (self.out / ".videoretalk_task.json").read_text(encoding="utf-8")
            self.assertNotIn("fixture-key", record)

    def test_query_failure_resumes_same_paid_job(self):
        def interrupted(url, payload=None, **kwargs):
            if kwargs.get("method") == "POST":
                return self.success(url, payload, **kwargs)
            raise P.ProviderError("query forbidden", status=403)
        with patch.object(P, "_dashscope_oss_upload", return_value="oss://fixture"), \
             patch.object(P, "_http", side_effect=interrupted):
            with self.assertRaises(P.ProviderError):
                self.call()
        with patch.object(P, "_dashscope_oss_upload") as upload, \
             patch.object(P, "_http", side_effect=self.success) as http, \
             patch.object(P, "_http_download", side_effect=self.download):
            self.call()
            upload.assert_not_called()
            self.assertEqual(http.call_count, 1)
            self.assertEqual(http.call_args.kwargs["method"], "GET")

    def test_auth_and_quota_fail_without_fallback_or_retry(self):
        for code in (401, 403):
            self.out = self.root / f"auth-{code}"
            with patch.object(P, "_dashscope_oss_upload", return_value="oss://fixture"), \
                 patch.object(P, "_http", side_effect=P.ProviderError("rejected", status=code)) as http:
                with self.assertRaises(P.ProviderError):
                    self.call()
                self.assertEqual(http.call_count, 1)
                self.assertFalse((self.out / "avatar_00.mp4").exists())

    def test_missing_job_id_blocks_blind_resubmission(self):
        with patch.object(P, "_dashscope_oss_upload", return_value="oss://fixture"), \
             patch.object(P, "_http", return_value={}):
            with self.assertRaisesRegex(P.ProviderError, "task_id"):
                self.call()
        with patch.object(P, "_http") as http:
            with self.assertRaisesRegex(P.ProviderError, "阻止重复"):
                self.call()
            http.assert_not_called()

    def test_download_failure_resumes_without_resubmission(self):
        with patch.object(P, "_dashscope_oss_upload", return_value="oss://fixture"), \
             patch.object(P, "_http", side_effect=self.success), \
             patch.object(P, "_http_download", side_effect=P.ProviderError("download denied", status=403)):
            with self.assertRaises(P.ProviderError):
                self.call()
        with patch.object(P, "_http", side_effect=self.success) as http, \
             patch.object(P, "_http_download", side_effect=self.download):
            self.call()
            self.assertEqual(http.call_count, 1)
            self.assertEqual(http.call_args.kwargs["method"], "GET")

    def test_invalid_result_not_exposed_as_completed_clip(self):
        def bad_download(url, path, **kwargs):
            path.write_bytes(b"this is not a video")
        with patch.object(P, "_dashscope_oss_upload", return_value="oss://fixture"), \
             patch.object(P, "_http", side_effect=self.success), \
             patch.object(P, "_http_download", side_effect=bad_download):
            with self.assertRaisesRegex(P.ProviderError, "视频无效"):
                self.call()
            self.assertFalse((self.out / "avatar_00.mp4").exists())

    def test_graph_avatar_node_dispatches_video_without_identity_photo(self):
        cfg = Config.load(root=self.root / "isolated-config")
        entry = upsert_entry(cfg, {"kind": "avatar", "label": "VideoRetalk fixture",
                                 "preset": "dashscope_videoretalk", "api_key": "test-only"})
        cfg = cfg.with_overrides({"steps.avatar.provider": "avatar:" + entry["id"]})
        with patch.object(P, "_dashscope_oss_upload", return_value="oss://fixture"), \
             patch.object(P, "_http", side_effect=self.success), \
             patch.object(P, "_http_download", side_effect=self.download):
            result = VideoFlow(cfg, log=lambda _: None).node_avatar({
                "source_video": str(self.source), "run_dir": str(self.out),
                "voice": {"segments": [{"audio_file": str(self.audio)}]},
            })
        self.assertTrue(result["avatar"]["uses_source_video"])
        self.assertFalse(result["avatar"]["used_portrait"])
        self.assertEqual(result["avatar"]["scene_photo"], "")
        self.assertTrue(Path(result["avatar"]["clips"][0]).exists())


class SourceVideoWebTests(workbench_checks.WorkbenchTests):
    def test_saved_video_upload_select_and_scope(self):
        valid = {"has_video": True, "width": 1080, "height": 1920,
                 "duration": 60, "fps": 30, "video_codec": "h264"}
        with patch("autovid.media.probe_media", return_value=valid):
            request = urllib.request.Request(self.base + "/api/source-video?filename=upper.mp4",
                data=b"fixture", headers={"Content-Type": "application/octet-stream"})
            with self.opener.open(request) as response:
                saved = json.loads(response.read())
            self.assertTrue(Path(saved["path"]).is_file())
            self.assertEqual(self.request("/api/source-videos")[1]["videos"][0]["name"], saved["name"])
            self.assertEqual(self.request("/api/source-video/select", {"name": saved["name"]})[0], 200)
            for name in ("../upper.mp4", "..\\upper.mp4", "C:\\private.mp4", "missing.mp4"):
                self.assertEqual(self.request("/api/source-video/select", {"name": name})[0], 400)
            with self.opener.open(self.base + saved["url"]) as response:
                self.assertEqual(response.read(), b"fixture")

    def test_invalid_upload_removed_and_original_photos_preserved(self):
        original = list(self.scenes.iterdir())
        request = urllib.request.Request(self.base + "/api/source-video?filename=bad.mp4", data=b"invalid")
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.opener.open(request)
        self.assertEqual(error.exception.code, 400)
        error.exception.close()
        self.assertFalse(list((self.root / "assets/source_videos").iterdir()))
        self.assertEqual(list(self.scenes.iterdir()), original)

    def test_video_preflight_does_not_require_photo_or_identity(self):
        base = self.root / "assets/source_videos"
        base.mkdir(parents=True, exist_ok=True)
        video = base / "plate.mp4"
        video.write_bytes(b"fixture")
        entry = upsert_entry(self.config, {"kind": "avatar", "label": "VideoRetalk fixture", "preset": "dashscope_videoretalk", "api_key": "test-only"})
        cfg = self.config.with_overrides({"steps.avatar.provider": "avatar:" + entry["id"]})
        valid = {"has_video": True, "width": 1080, "height": 1920,
                 "duration": 60, "fps": 30, "video_codec": "h264"}
        with patch("autovid.media.probe_media", return_value=valid):
            checked = VideoFlow(cfg, log=lambda _: None).node_preflight({"script_text": "你好",
                "source_video": str(video), "run_dir": str(self.root / "runs/test")})
        self.assertFalse(any("照片" in e or "形象" in e or "底片" in e for e in checked["errors"]))
        self.assertIn("人物视频底片", checked["preflight_info"])
        self.assertTrue(P.avatar_uses_source_video(cfg, "avatar:" + entry["id"]))

    def test_connection_probe_rejects_empty_success_body(self):
        entry = upsert_entry(self.config, {"kind": "avatar", "label": "probe fixture",
            "preset": "dashscope_videoretalk", "api_key": "fake-key"})
        class Response:
            status = 200
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read(self, size): return b"{}"
        with patch("autovid.providers_registry.urllib.request.urlopen", return_value=Response()) as request:
            checked = test_connection(self.config, entry["id"])
            self.assertFalse(checked["ok"])
            self.assertEqual(request.call_args.args[0].method, "GET")


if __name__ == "__main__":
    unittest.main(verbosity=2)
