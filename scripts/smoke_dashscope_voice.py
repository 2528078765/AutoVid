"""Alibaba voice enrollment, synthesis and reference-cache regressions. No paid APIs."""
from __future__ import annotations

import copy
import io
import json
import math
import sys
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from autovid import media as M, providers as P
from autovid.assets import AssetStore, validate_voice_name
from autovid.config import Config
from autovid.providers_registry import PRESETS, resolve, upsert_entry, preset_options, load_entries
from autovid.dashscope_voice import MODELS, configure
from autovid.errors import AutoVidError


def wav_data(seconds=6, frequency=220):
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(24000)
        writer.writeframes(b"".join(int(12000 * math.sin(2 * math.pi * frequency * i / 24000)).to_bytes(2, "little", signed=True)
                                  for i in range(int(24000 * seconds))))
    return buffer.getvalue()


class DashscopeVoiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ali-voice-", dir=ROOT / ".tmp")
        self.root = Path(self.temp.name)
        self.config = Config.load(root=self.root)
        self.store = AssetStore(self.config)
        self.asset = self.store.create_voice("测试人声")
        self.audio = wav_data()
        self.asset = self.store.save_voice_reference(self.asset.id, self.audio, "sample.wav")
        self.cfg = copy.deepcopy(PRESETS["dashscope_qwen_voice"]["config"])
        self.cfg["workspace_id"] = "fixture-workspace"
        self.name = "voice:fixture"

    def tearDown(self):
        self.temp.cleanup()

    def register(self, key="fixture-key", cfg=None):
        return P._cloud_register_voice(self.config, cfg or self.cfg, lambda _: None,
                                       self.store.get_voice(self.asset.id), api_key=key, provider_name=self.name)

    def test_chinese_library_name_rule_and_no_files_for_invalid_names(self):
        for name in ("讲解声音", "我的声音", "繁體音色", "𠮷野", "〇号音色"):
            self.assertEqual(validate_voice_name(name), name)
        self.assertEqual(validate_voice_name(" 讲解声音 "), "讲解声音")
        before = list(self.store.voices_dir.iterdir())
        for name in ("", "  ", "csy", "讲解声音一号1", "陈 CSY", "陈 思宇", "陈·思宇", "音色_", "声音😀", "声音\n测试"):
            with self.subTest(name=name), self.assertRaises(AutoVidError):
                self.store.create_voice(name)
        self.assertEqual(before, list(self.store.voices_dir.iterdir()))

    def test_siliconflow_enrollment_uses_stable_ascii_name_not_display_name(self):
        sf = copy.deepcopy(PRESETS["siliconflow"]["config"])
        self.store.set_voice_reference_text(self.asset.id, "这是实际录音文字。")
        fields_seen = []
        original_build = P._build_multipart
        def capture(fields, *args, **kwargs):
            fields_seen.append(dict(fields))
            return original_build(fields, *args, **kwargs)
        with patch.object(P, "_build_multipart", side_effect=capture), patch.object(P, "_http_raw", return_value=b'{"uri":"speech:fixture"}') as http:
            self.assertEqual(self.register(cfg=sf), "speech:fixture")
            self.assertEqual(self.register(cfg=sf), "speech:fixture")
            self.assertEqual(http.call_count, 1)
            machine_name = fields_seen[0]["customName"]
            self.assertRegex(machine_name, r"^[A-Za-z0-9]{1,64}$")
            self.assertNotEqual(machine_name, self.asset.name)
            # Old English-named assets remain loadable and use the same cloud name.
            legacy = self.store.get_voice(self.asset.id)
            legacy.name = "CSY"
            legacy.cloud_voice_ids = {}
            legacy.cloud_voice_fingerprints = {}
            self.store._save(self.store.voice_dir(legacy.id) / "meta.json", legacy.to_dict())
            self.assertEqual(self.register(cfg=sf), "speech:fixture")
            self.assertEqual(fields_seen[-1]["customName"], machine_name)
            self.assertEqual(self.store.get_voice(legacy.id).name, "CSY")
            other = self.store.create_voice("另一个音色")
            self.store.save_voice_reference(other.id, self.audio, "sample.wav", "另一份实际录音文字。")
            P._cloud_register_voice(self.config, sf, lambda _:None, other, api_key="fixture-key", provider_name=self.name)
            self.assertNotEqual(fields_seen[-1]["customName"], machine_name)

    def test_qwen_enrollment_contract_and_cache(self):
        with patch.object(P, "_http", return_value={"output": {"voice": "qwen-cloned-id"}}) as http:
            self.assertEqual(self.register(), "qwen-cloned-id")
            request = http.call_args
            self.assertIn("fixture-workspace.cn-beijing", request.args[0])
            self.assertEqual(request.args[1]["model"], "qwen-voice-enrollment")
            data = request.args[1]["input"]
            self.assertEqual(data["target_model"], self.cfg["model"])
            self.assertTrue(data["audio"]["data"].startswith("data:audio/wav;base64,"))
            self.assertNotIn("text", data)
            self.assertEqual(self.register(), "qwen-cloned-id")
            self.assertEqual(http.call_count, 1)

    def test_new_upload_does_not_invent_text_and_clears_cached_id(self):
        self.store.set_cloud_voice_id(self.asset.id, self.name, "old-id", "old-fingerprint")
        saved = self.store.save_voice_reference(self.asset.id, wav_data(frequency=330), "updated.wav")
        self.assertEqual(saved.ref_text, "")
        self.assertEqual(saved.cloud_voice_ids, {})
        self.assertEqual(saved.cloud_voice_fingerprints, {})

    def test_changed_transcript_invalidates_only_when_changed(self):
        self.store.set_voice_reference_text(self.asset.id, "实际说的内容。")
        self.store.set_cloud_voice_id(self.asset.id, self.name, "old-id", "fingerprint")
        self.assertTrue(self.store.set_voice_reference_text(self.asset.id, "实际说的内容。").cloud_voice_ids)
        self.assertFalse(self.store.set_voice_reference_text(self.asset.id, "新的实际内容。").cloud_voice_ids)

    def test_changed_model_account_or_audio_never_reuses_wrong_voice(self):
        with patch.object(P, "_http", side_effect=[{"output":{"voice":f"id-{i}"}} for i in range(4)]) as http:
            self.register()
            self.register(key="different-account")
            changed = copy.deepcopy(self.cfg)
            changed["model"] = "fixture-other-model"
            self.register(key="different-account", cfg=changed)
            self.store.voice_reference(self.asset.id).write_bytes(wav_data(frequency=440))
            self.register(key="different-account", cfg=changed)
            self.assertEqual(http.call_count, 4)

    def test_qwen_long_reference_is_cropped_without_guessed_transcript(self):
        self.store.save_voice_reference(self.asset.id, wav_data(26), "long.wav", "不会按字数比例裁这个文本。")
        with patch.object(P, "_http", return_value={"output":{"voice":"id"}}) as http:
            self.register()
            import base64
            data_uri = http.call_args.args[1]["input"]["audio"]["data"]
            with wave.open(io.BytesIO(base64.b64decode(data_uri.partition(",")[2]))) as reader:
                self.assertEqual(reader.getnframes()/reader.getframerate(), 20)
            self.assertEqual(M.wav_duration(self.store.voice_reference(self.asset.id)), 26)

    def test_text_dependent_clone_refuses_unknown_or_proportional_text(self):
        sf = copy.deepcopy(PRESETS["siliconflow"]["config"])
        with patch.object(P, "_http_raw") as http:
            with self.assertRaisesRegex(P.ProviderError, "准确文字"):
                self.register(cfg=sf)
            self.store.save_voice_reference(self.asset.id, wav_data(26), "long.wav", "实际内容。")
            with self.assertRaisesRegex(P.ProviderError, "比例"):
                self.register(cfg=sf)
            http.assert_not_called()

    def test_clone_auth_quota_or_missing_id_stops_without_default(self):
        for code in (401, 403):
            with self.subTest(code=code), patch.object(P, "_http", side_effect=P.ProviderError("denied", status=code)):
                with self.assertRaises(P.ProviderError):
                    self.register()
            self.assertFalse(self.store.get_voice(self.asset.id).cloud_voice_ids)
        with patch.object(P, "_http", return_value={"output": {}}):
            with self.assertRaisesRegex(P.ProviderError, "音色 ID"):
                self.register()

    def test_clone_fallback_not_saved_as_success(self):
        with patch.object(P, "_http", return_value={"output": {"voice":"default", "fallback_mode": True}}):
            with self.assertRaisesRegex(P.ProviderError, "降级"):
                self.register()
            self.assertFalse(self.store.get_voice(self.asset.id).cloud_voice_ids)

    def test_real_wav_synthesis_with_custom_voice(self):
        def request(url, body, **kwargs):
            if body["model"] == "qwen-voice-enrollment":
                return {"output": {"voice": "qwen-custom"}}
            self.assertEqual(url,"https://dashscope.aliyuncs.com/api/v1/services/aigc/multimodal-generation/generation")
            self.assertEqual(body["input"]["voice"], "qwen-custom")
            self.assertEqual(body["input"]["language_type"], "Chinese")
            return {"output": {"audio": {"url":"https://fixture.invalid/audio.wav"}}}
        def download(url, out, **kwargs):
            out.write_bytes(self.audio)
        with patch.object(P, "_http", side_effect=request), patch.object(P, "_http_download", side_effect=download):
            result = P._tts_cloud(self.config, [{"text":"你好，这是我的声音。"}], self.root / "tts", lambda _: None,
                                  self.store.get_voice(self.asset.id), cfg=self.cfg, api_key="fixture-key", provider_name=self.name)
        self.assertTrue(result.cloned)
        self.assertAlmostEqual(M.wav_duration(result.parts[0]), 6)

    def url_config(self, model="cosyvoice-v3.5-flash"):
        return configure({**self.cfg, "model": model})

    def set_url(self):
        self.store.set_voice_reference_url(self.asset.id, "https://fixture.example/reference.wav?signature=private")

    def test_catalog_profiles_all_have_matching_enrollment_and_synthesis(self):
        self.set_url()
        for model in MODELS:
            with self.subTest(model=model["id"]):
                cfg = configure({**self.cfg, "model":model["id"]})
                def request(url, body, **kwargs):
                    if body["model"] == "qwen-voice-enrollment":
                        return {"output":{"voice":"qwen-custom"}}
                    if body["model"] == "voice-enrollment":
                        data = body["input"]
                        if data["action"] == "create_voice":
                            self.assertEqual(data["target_model"], model["id"])
                            self.assertIn("signature=private", data["url"])
                            self.assertNotIn("audio", data)
                            return {"output":{"voice_id":model["id"]+"-custom"}}
                        return {"output":{"status":"OK", "target_model":model["id"]}}
                    self.assertEqual(body["model"], model["id"])
                    if model["reference"] == "url":
                        self.assertTrue(url.endswith("/SpeechSynthesizer"))
                        self.assertEqual(body["input"]["voice"], model["id"]+"-custom")
                        self.assertEqual(body["input"]["format"], "wav")
                    else:
                        self.assertIn("/multimodal-generation/", url)
                    return {"output":{"audio":{"url":"https://fixture.example/audio.wav"}}}
                def download(url, out, **kwargs):
                    out.write_bytes(self.audio)
                with patch.object(P, "_http", side_effect=request), patch.object(P, "_http_download", side_effect=download):
                    result = P._tts_cloud(self.config, [{"text":"测试模型中文口播。"}],
                        self.root / model["id"], lambda _:None, self.store.get_voice(self.asset.id),
                        cfg=cfg, api_key="fixture-key", provider_name=self.name)
                self.assertTrue(result.cloned)
                self.assertAlmostEqual(M.wav_duration(result.parts[0]), 6)

    def test_a_b_a_model_switch_reuses_each_bound_voice(self):
        self.set_url()
        creates = []
        def request(url, body, **kwargs):
            if body["input"]["action"] == "create_voice":
                model = body["input"]["target_model"]
                creates.append(model)
                return {"output":{"voice_id":model+"-myvoice"}}
            return {"output":{"status":"OK"}}
        with patch.object(P, "_http", side_effect=request):
            a = self.register(cfg=self.url_config())
            b = self.register(cfg=self.url_config("qwen-audio-3.0-tts-flash"))
            self.assertEqual(self.register(cfg=self.url_config()), a)
            self.assertNotEqual(a, b)
        self.assertEqual(creates, ["cosyvoice-v3.5-flash", "qwen-audio-3.0-tts-flash"])

    def test_query_failure_preserves_registered_id_for_next_attempt(self):
        self.set_url()
        with patch.object(P, "_http", side_effect=[{"output":{"voice_id":"created-once"}},
                     P.ProviderError("query denied", status=403)]):
            with self.assertRaisesRegex(P.ProviderError, "query denied"):
                self.register(cfg=self.url_config())
        with patch.object(P, "_http", return_value={"output":{"status":"OK"}}) as http:
            self.assertEqual(self.register(cfg=self.url_config()), "created-once")
            self.assertEqual(http.call_count, 1)
            self.assertEqual(http.call_args.args[1]["input"]["action"], "query_voice")

    def test_deploying_polls_but_never_recreates(self):
        self.set_url()
        replies = [{"output":{"voice_id":"once"}}, {"output":{"status":"DEPLOYING"}},
                   {"output":{"status":"OK"}}]
        with patch.object(P, "_http", side_effect=replies) as http, patch.object(P.time, "sleep"):
            self.assertEqual(self.register(cfg=self.url_config()), "once")
            self.assertEqual(http.call_count, 3)
        for response in ({"output":{"status":"UNDEPLOYED"}}, {"output":{}},
                         {"output":{"status":"OK", "target_model":"wrong-model"}}):
            with patch.object(P, "_http", return_value=response):
                with self.assertRaises(P.ProviderError):
                    self.register(cfg=self.url_config())

    def test_missing_public_url_fails_before_any_cloud_call(self):
        with patch.object(P, "_http") as http:
            with self.assertRaisesRegex(P.ProviderError, "HTTPS 直链"):
                self.register(cfg=self.url_config())
            http.assert_not_called()

    def test_url_validation_and_changed_recording_clear_link(self):
        for url in ("D:/audio.wav", "oss://private/audio.wav", "http://example.com/a.wav",
                    "https://localhost/a", "https://127.0.0.1/a", "https://192.168.0.1/a",
                    "https://user:password@example.com/a", "https://example.com:bad/a"):
            with self.subTest(url=url), self.assertRaises(AutoVidError):
                self.store.set_voice_reference_url(self.asset.id, url)
        self.set_url()
        self.assertTrue(self.store.get_voice(self.asset.id).cloud_reference_url)
        self.store.save_voice_reference(self.asset.id, self.audio, "replacement.wav")
        self.assertEqual(self.store.get_voice(self.asset.id).cloud_reference_url, "")

    def test_snapshot_protocol_is_rebuilt_after_model_switch_and_key_retained(self):
        entry = upsert_entry(self.config, {"id":"ali", "kind":"voice", "label":"阿里云百炼",
            "preset":"dashscope_qwen_voice", "api_key":"fixture-key", "config":self.cfg})
        upsert_entry(self.config, {"id":entry["id"], "kind":"voice", "label":"阿里云百炼",
            "preset":"dashscope_qwen_voice", "model":"cosyvoice-v3.5-flash"})
        resolved = resolve(self.config, "voice:ali")
        self.assertEqual(resolved["api_key"], "fixture-key")
        self.assertTrue(resolved["cfg"]["url"].endswith("/SpeechSynthesizer"))
        self.assertNotIn("language_type", resolved["cfg"]["body"]["input"])
        self.assertEqual(resolved["cfg"]["clone"]["voice_id_path"], "output.voice_id")
        public = [p for p in preset_options() if p["name"].startswith("aliyun_voice_")]
        self.assertEqual(len(public), len(MODELS))
        self.assertTrue(all(p["fixed_model"] and len(p["models"]) == 1 for p in public))
        self.assertEqual(resolved["entry"]["preset"], "aliyun_voice_cosyvoice_v3_5_flash")
        before = load_entries(self.config)
        with self.assertRaisesRegex(ValueError, "尚未适配"):
            upsert_entry(self.config, {"id":"ali", "kind":"voice", "label":"阿里云百炼",
                "preset":"dashscope_qwen_voice", "model":"unknown-model"})
        self.assertEqual(before, load_entries(self.config))

    def test_url_only_asset_does_not_need_upload_or_oss(self):
        remote = self.store.create_voice("只用直链")
        remote = self.store.set_voice_reference_url(remote.id,"https://fixture.example/me.wav")
        cfg = self.url_config()
        self.assertIsNone(self.store.voice_reference(remote.id))
        with patch.object(P,"_http",side_effect=[{"output":{"voice_id":"url-only"}},
                    {"output":{"status":"OK","target_model":cfg["model"]}}]) as http:
            voice_id = P._cloud_register_voice(self.config,cfg,lambda _:None,remote,
                api_key="fixture-key",provider_name=self.name)
        self.assertEqual(voice_id,"url-only")
        self.assertEqual(http.call_args_list[0].args[1]["input"]["url"],"https://fixture.example/me.wav")

    def test_setup_requirements_are_separate_from_key_saved(self):
        upsert_entry(self.config,{"id":"ali", "kind":"voice", "label":"阿里云",
            "preset":"aliyun_voice_cosyvoice_v3_5_flash","api_key":"fixture-key",
            "config":{"workspace_id":"fixture-workspace"}})
        self.assertEqual(P.voice_requirements(self.config,"voice:ali")["reference_mode"],"url")
        self.assertTrue(P.voice_setup_problems(self.config,"voice:ali",self.store.get_voice(self.asset.id)))
        self.set_url()
        self.assertEqual(P.voice_setup_problems(self.config,"voice:ali",self.store.get_voice(self.asset.id)),[])

    def test_cloud_error_code_survives_strict_wrapper_without_signed_urls(self):
        upsert_entry(self.config,{"id":"ali","kind":"voice","label":"阿里云",
            "preset":"aliyun_voice_cosyvoice_v3_5_flash","api_key":"fixture-key",
            "config":{"workspace_id":"fixture-workspace"}})
        cfg=self.config.with_overrides({"steps.voice.provider":"voice:ali","steps.voice.strict":True})
        error=P.ProviderError("HTTP 404 <- fixture",status=404,
            body=json.dumps({"code":"ModelNotFound","message":"missing model https://example.com/?secret-signature=abc"}).encode())
        with patch.object(P,"_tts_cloud",side_effect=error):
            with self.assertRaises(P.ProviderError) as caught:
                P.tts_synthesize(cfg,[{"text":"测试"}],self.root/"error",lambda _:None)
        self.assertIn("ModelNotFound",str(caught.exception))
        self.assertNotIn("secret-signature",str(caught.exception))
        self.assertEqual(caught.exception.status,404)


if __name__ == "__main__":
    unittest.main(verbosity=2)
