"""Offline caption toggle checks: real FFmpeg output, both composition paths."""
from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from autovid import media as M, steps as S
from autovid.config import Config
from autovid.graph import VideoFlow
from autovid.manifest import RunContext
from autovid.pipeline import Runner, get_step_def
from autovid.errors import AutoVidError


class OptionalSubtitlesTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix="captions-", dir=ROOT / ".tmp")
        cls.root = Path(cls.temp.name)
        cls.clip = cls.root / "source.mp4"
        M.run_ffmpeg(["-f", "lavfi", "-i", "color=c=blue:s=320x568:r=10", "-t", "1",
                      "-c:v", "libx264", "-pix_fmt", "yuv420p", cls.clip.name], cwd=cls.root)
        cls.wav = cls.root / "voice.wav"
        with wave.open(str(cls.wav), "wb") as writer:
            writer.setparams((1, 2, 24000, 0, "NONE", "not compressed"))
            writer.writeframes(b"\x00\x10" * 24000)

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def config(self, enabled):
        return Config.load(root=self.root).with_overrides({
            "project.require_assets":False, "steps.subtitles.enabled":enabled,
            "platform.width":320, "platform.height":568, "platform.fps":10,
            "steps.subtitles.font_size":24, "steps.subtitles.keyword_size":28,
            "steps.subtitles.margin_v":50, "steps.subtitles.keyword_margin_v":30,
        })

    def state(self, directory):
        return {"run_dir":str(directory), "trace":[],
            "script":{"topic":"测试", "title_options":["测试封面"], "hook":"字幕测试"},
            "avatar":{"clips":[str(self.clip)]}, "voice":{"wav":str(self.wav),
                "segments":[{"headline":"测试", "start":0,"end":1}],
                "utterances":[{"text":"这是字幕测试", "start":0,"end":1}]}}

    def test_graph_enabled_and_disabled_real_video(self):
        for enabled in (True, False):
            with self.subTest(enabled=enabled):
                cfg = self.config(enabled)
                base = self.root / f"graph-{enabled}"
                state = self.state(base)
                flow = VideoFlow(cfg, log=lambda _:None)
                state.update(flow.node_subtitles(state))
                with patch.object(M, "burn_subtitles", wraps=M.burn_subtitles) as burn:
                    result = flow.node_compose(state)
                final = Path(result["video"]["video"])
                self.assertTrue(M.probe_media(final)["has_audio"])
                self.assertAlmostEqual(M.probe_duration(final), 1, delta=.15)
                self.assertEqual(burn.call_count, int(enabled))
                self.assertEqual((base/"subtitles"/"subtitles.ass").exists(), enabled)
                if not enabled:
                    self.assertEqual(state["trace"][-1]["status"], "skipped")
                    self.assertEqual(final.read_bytes(), (base/"compose"/"av.mp4").read_bytes())

    def test_disabled_ignores_old_checkpoint_subtitles(self):
        flow = VideoFlow(self.config(False), log=lambda _:None)
        state = self.state(self.root/"stale-checkpoint")
        state["subtitles"] = {"file":"missing-old-subtitles.ass", "enabled":True}
        with patch.object(M, "burn_subtitles") as burn:
            result = flow.node_compose(state)
        burn.assert_not_called()
        self.assertTrue(Path(result["video"]["video"]).is_file())

    def test_enabled_missing_subtitle_is_an_error_not_silent_skip(self):
        flow = VideoFlow(self.config(True), log=lambda _:None)
        with self.assertRaisesRegex(AutoVidError, "找不到字幕"):
            flow.node_compose(self.state(self.root/"missing"))

    def test_legacy_pipeline_and_cache_dependency(self):
        for enabled in (True, False):
            with self.subTest(enabled=enabled):
                cfg = self.config(enabled)
                ctx = RunContext.create(cfg, run_id=f"legacy-{enabled}")
                shutil.copyfile(self.clip, ctx.dir/"clip.mp4")
                shutil.copyfile(self.wav, ctx.dir/"voice.wav")
                ctx.ensure_step("voice",1,"1")
                ctx.add_artifact("voice","voice",ctx.dir/"voice.wav")
                ctx.ensure_step("subtitles",2,"1")
                state = {"script":self.state(ctx.dir)["script"],
                    "voice_segments":self.state(ctx.dir)["voice"],
                    "avatar":{"clips":[{"file":"clip.mp4"}]}}
                S.step_subtitles(ctx,state,lambda _:None)
                runner = Runner(ctx)
                required = runner.requirements(get_step_def("compose"))
                self.assertEqual("subtitles" in required,enabled)
                self.assertNotEqual(runner.input_hash(get_step_def("compose")),
                    Runner(RunContext(ctx.dir,self.config(not enabled),ctx.manifest)).input_hash(get_step_def("compose")))
                ctx.ensure_step("compose",3,"1")
                with patch.object(M,"burn_subtitles",wraps=M.burn_subtitles) as burn:
                    S.step_compose(ctx,state,lambda _:None)
                self.assertTrue(ctx.artifact_path("video").is_file())
                self.assertEqual(burn.call_count,int(enabled))
                if not enabled:
                    self.assertIsNone(ctx.artifact_path("subtitles"))
                    self.assertNotIn("subtitles",runner.plan().to_run)

    def test_skipped_status_reaches_progress_events(self):
        events=[]
        flow=VideoFlow(self.config(False),log=lambda _:None,emitter=events.append)
        flow._wrapped("subtitles")({"trace":[]})
        self.assertEqual(events[-1]["status"],"skipped")

    def test_actual_export_source_and_portrait_modes(self):
        for mode, expected in (("source",(320,568,10.0)),("portrait720",(720,1280,30.0))):
            with self.subTest(mode=mode):
                cfg=self.config(False).with_overrides({"project.export_mode":mode})
                state=self.state(self.root/("export-"+mode))
                state["source_video"]=str(self.clip)
                result=VideoFlow(cfg,log=lambda _:None).node_compose(state)
                self.assertEqual(M.probe_video_spec(Path(result["video"]["video"])),expected)


if __name__ == "__main__":
    unittest.main(verbosity=2)
