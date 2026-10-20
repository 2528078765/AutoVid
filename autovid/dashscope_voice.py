"""Documented Beijing speech models and their distinct HTTP contracts.

No cloud calls here. Keeping this catalog server-side makes the editor and
runtime agree, even when an older provider snapshot contains stale endpoints.
"""
from __future__ import annotations

import copy
from typing import Any

ROOT_URL = "https://{{workspace_id}}.cn-beijing.maas.aliyuncs.com/api/v1/services"
LEGACY_MODEL = "qwen3-tts-vc-2026-01-22"
MODELS = [
    {"id": LEGACY_MODEL, "label": "千问三 · 声音复刻", "reference": "local",
     "endpoint": "https://dashscope.aliyuncs.com/api/v1/services/aigc/multimodal-generation/generation",
     "hint": "支持本地参考录音；模型是否开放调用取决于账号和地域权限。"},
    {"id": "cosyvoice-v3.5-flash", "label": "CosyVoice 3.5 Flash", "reference": "url"},
    {"id": "cosyvoice-v3.5-plus", "label": "CosyVoice 3.5 Plus", "reference": "url"},
    {"id": "qwen-audio-3.0-tts-flash", "label": "Qwen-Audio 3.0 TTS Flash", "reference": "url"},
    {"id": "qwen-audio-3.0-tts-plus", "label": "Qwen-Audio 3.0 TTS Plus", "reference": "url"},
    {"id": "cosyvoice-v3-flash", "label": "CosyVoice 3 Flash", "reference": "url"},
    {"id": "cosyvoice-v3-plus", "label": "CosyVoice 3 Plus", "reference": "url"},
    {"id": "cosyvoice-v2", "label": "CosyVoice 2", "reference": "url"},
]
for _model in MODELS:
    if _model["reference"] == "url":
        _model["endpoint"] = ROOT_URL + "/audio/tts/SpeechSynthesizer"
        _model["hint"] = "音色库填写你本人参考录音的 HTTPS 直链即可，不强制使用 OSS。不能使用网盘分享页、网页或视频链接。首次使用会创建本模型的音色。"


def preset_name(model: str) -> str:
    return "aliyun_voice_" + model.replace(".", "_").replace("-", "_")


def configure(config: dict[str, Any]) -> dict[str, Any]:
    """Replace model-owned fields, never inherit another family's request body."""
    cfg = copy.deepcopy(config)
    model = str(cfg.get("model") or LEGACY_MODEL)
    info = next((item for item in MODELS if item["id"] == model), None)
    if info is None:
        raise ValueError(f"阿里云语音模型尚未适配：{model}，请从模型列表选择")
    cfg.update(model=model, url=info["endpoint"], method="POST",
               audio_path="output.audio.url", audio_encoding="url")
    headers = {"Authorization": "Bearer {{api_key}}", "Content-Type": "application/json"}
    cfg["headers"] = headers
    clone = {
        "enabled": True, "min_audio_s": 5, "max_audio_s": 20,
        "requires_reference_text": False, "cache_by_model": True,
        "url": ROOT_URL + "/audio/tts/customization", "headers": headers.copy(),
    }
    if info["reference"] == "local":
        cfg["body"] = {"model": "{{model}}", "input": {
            "text": "{{text}}", "voice": "{{voice_id}}", "language_type": "Chinese"}}
        clone.update(body={"model": "qwen-voice-enrollment", "input": {
            "action": "create", "target_model": "{{model}}", "preferred_name": "autovid",
            "audio": {"data": "data:audio/wav;base64,{{audio_base64}}"}}},
            voice_id_path="output.voice")
    else:
        clone["max_audio_s"] = 60
        cfg["body"] = {"model": "{{model}}", "input": {
            "text": "{{text}}", "voice": "{{voice_id}}", "format": "wav",
            "sample_rate": 24000, "language_hints": ["zh"]}}
        clone.update(reference_mode="public_url", body={"model": "voice-enrollment", "input": {
            "action": "create_voice", "target_model": "{{model}}", "prefix": "autovid",
            "url": "{{reference_url}}"}}, voice_id_path="output.voice_id",
            ready={"url": ROOT_URL + "/audio/tts/customization", "headers": headers.copy(),
                   "body": {"model": "voice-enrollment", "input": {
                       "action": "query_voice", "voice_id": "{{voice_id}}"}},
                   "timeout_s": 120, "interval_s": 2})
        if model not in {"cosyvoice-v3-plus", "cosyvoice-v2"}:
            clone["body"]["input"]["max_prompt_audio_length"] = 20.0
    cfg["clone"] = clone
    cfg["capabilities"] = {
        "verified": False, "source": "阿里云声音复刻及非实时语音合成 HTTP 文档；尚未真实合成验收",
        "output": {"note": "所选模型克隆音色合成中文配音，接口失败直接报错"},
        "audio": {"note": info["hint"]},
    }
    return cfg
