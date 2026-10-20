"""Video-driven avatar adapter. No image fallback and no automatic motion looping."""
from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any

from . import media as M
from .errors import AutoVidError


def validate_source(path: Path | str) -> dict[str, Any]:
    """Validate local media before uploading or submitting a billable task."""
    path = Path(path)
    if not path.is_file() or path.suffix.lower() not in {".mp4", ".avi", ".mov"}:
        raise AutoVidError("请选择已保存的 MP4、MOV 或 AVI 人物视频底片")
    if path.stat().st_size > 300 * 1024 * 1024:
        raise AutoVidError("人物视频底片超过 300MB，请压缩后重新上传")
    info = M.probe_media(path)
    duration = float(info.get("duration") or 0)
    if not info.get("has_video") or not 2 < duration < 120:
        raise AutoVidError("底片必须是可解码的视频，时长需大于 2 秒、小于 120 秒")
    if not all(640 <= int(info.get(key) or 0) <= 2048 for key in ("width", "height")):
        raise AutoVidError("底片宽高都需在 640 至 2048 像素之间，建议竖屏 1080×1920")
    if not 15 <= float(info.get("fps") or 0) <= 60:
        raise AutoVidError("底片帧率需为 15 至 60 帧，建议每秒 30 帧")
    if info.get("video_codec") not in {"h264", "hevc"}:
        raise AutoVidError("底片需使用 H.264 或 H.265 编码，请导出为标准 MP4 后重试")
    return info


def _digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def render(source: Path | None, segments, out: Path, cfg: dict, api_key: str,
           provider: str, log) -> list[Path]:
    # Import lazily to keep the provider dispatch free of circular imports.
    from . import providers as P

    if source is None:
        raise P.ProviderError("VideoRetalk 缺少人物视频底片，请上传或从素材库选择")
    info = validate_source(source)
    out.mkdir(parents=True, exist_ok=True)
    parts = [P._segment_audio(segment) for segment in segments]
    if not parts or any(not path.is_file() for path in parts):
        raise P.ProviderError("VideoRetalk 配音文件不存在，请先完成语音合成")
    audio = out / "retalk_audio.wav"
    M.concat_wav_pcm(parts, audio, gap_ms=0, trailing_gap=False)
    duration = M.wav_duration(audio)
    if not 2 < duration < 120 or audio.stat().st_size > 30 * 1024 * 1024:
        raise P.ProviderError("VideoRetalk 配音需大于 2 秒、小于 120 秒且不超过 30MB，请缩短文案")
    if duration > float(info["duration"]) + 0.01:
        raise P.ProviderError(
            f"配音 {duration:.2f} 秒，底片只有 {info['duration']:.2f} 秒。"
            "请换更长底片或缩短文案；不会倒放循环，也不会截断配音。")

    fingerprint = hashlib.sha256(json.dumps({
        "source": _digest(source), "audio": _digest(audio), "config": cfg,
    }, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    record = out / ".videoretalk_task.json"
    saved = json.loads(record.read_text(encoding="utf-8")) if record.exists() else {}
    if saved and saved.get("fingerprint") != fingerprint:
        raise P.ProviderError("底片、配音或接口配置与已提交任务不同，请新建运行，避免复用错误的付费结果")

    if saved.get("submission_uncertain") and not saved.get("job_id"):
        raise P.ProviderError("上次提交结果不确定，已阻止重复付费提交。请先在百炼查看任务，确认后新建运行")
    clip = out / "avatar_00.mp4"
    def check_result(path: Path) -> None:
        actual = M.probe_media(path)
        if (not actual.get("has_video")
                or abs(float(actual.get("duration") or 0) - duration) > 0.35):
            raise P.ProviderError("VideoRetalk 返回的视频无效或时长与配音不符，已停止合成，请查看云端任务")

    if saved.get("downloaded") and clip.is_file():
        check_result(clip)
        log("复用已完成的 VideoRetalk 片段，不重复提交付费任务")
        return [clip]

    headers = {"Authorization": f"Bearer {api_key}"}
    def persist() -> None:
        temporary = record.with_suffix(".tmp")
        temporary.write_text(json.dumps(saved, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(record)

    if not saved.get("job_id"):
        # Trim the bottom plate to the actual narration; never loop or reverse it.
        prepared = out / "retalk_source.mp4"
        M.run_ffmpeg(["-i", str(source.resolve()), "-t", f"{duration:.6f}",
                      "-map", "0:v:0", "-an", "-c:v", "libx264", "-preset", "fast",
                      "-crf", "18", "-pix_fmt", "yuv420p", "-movflags", "+faststart",
                      prepared.name], cwd=out, desc="准备人物底片（保留正向动作）")
        if prepared.stat().st_size > 300 * 1024 * 1024:
            raise P.ProviderError("处理后的视频超过 300MB，请先压缩底片")
        context = {"api_key": api_key, "model": str(cfg.get("model") or "videoretalk")}
        urls = {}
        policy = {"policy_url": cfg.get("upload_policy_url") or
                  "https://dashscope.aliyuncs.com/api/v1/uploads?action=getPolicy&model={{model}}"}
        for name, path, mime in (("video", prepared, "video/mp4"), ("audio", audio, "audio/wav")):
            log(f"上传人物{'底片' if name == 'video' else '配音'}…")
            try:
                urls[name] = P._retry(
                    lambda: P._dashscope_oss_upload(policy, path.read_bytes(), path.name, mime, context),
                    what=f"上传{name}", cfg=cfg, log=log)
            except P.ProviderError as exc:
                raise P._signed_error(exc, cfg, provider, f"上传{name}") from exc
        # Persist intent *before* POST: a process exit during submission is ambiguous too.
        saved = {"fingerprint": fingerprint, "duration": duration, "submission_uncertain": True}
        persist()
        log(f"提交 VideoRetalk 任务，配音 {duration:.2f} 秒（预计口型替换费用 {duration * 0.08:.2f} 元，以账单为准）")
        try:
            # Do not automatically retry a non-idempotent paid submission.
            submitted = P._http(str(cfg["submit"]["url"]), {
                "model": context["model"],
                "input": {"video_url": urls["video"], "audio_url": urls["audio"]},
                "parameters": {"video_extension": False},
            }, headers={**headers, "X-DashScope-Async": "enable",
                        "X-DashScope-OssResourceResolve": "enable"}, timeout_s=180, method="POST")
        except P.ProviderError as exc:
            saved["submission_uncertain"] = getattr(exc, "status", None) not in {400, 401, 402, 403, 404, 405, 422, 429}
            persist()
            raise P._signed_error(exc, cfg, provider, "提交任务（请先在百炼确认是否已创建任务，再重试）") from exc
        job_id = P._dig(submitted, "output.task_id")
        if not job_id:
            saved["submission_uncertain"] = True
            persist()
            raise P.ProviderError("VideoRetalk 提交响应缺少 output.task_id，请在百炼确认任务，避免重复扣费")
        saved["job_id"] = str(job_id)
        saved["submission_uncertain"] = False
        persist()
    log(f"查询 VideoRetalk 任务 {saved['job_id']}，续跑不会重新提交")
    deadline = time.monotonic() + float(cfg.get("timeout_s", 1800))
    url = None
    while time.monotonic() < deadline:
        target = str(cfg["query"]["url"]).replace("{{job_id}}", saved["job_id"])
        try:
            status = P._retry(lambda: P._http(target, None, headers=headers, method="GET"),
                              what="查询 VideoRetalk 任务", cfg=cfg, log=log)
        except P.ProviderError as exc:
            raise P._signed_error(exc, cfg, provider, "查询任务") from exc
        state = str(P._dig(status, "output.task_status") or "")
        if state == "SUCCEEDED":
            url = P._dig(status, "output.video_url")
            if not url:
                raise P.ProviderError("VideoRetalk 已成功，但响应缺少 output.video_url，请在百炼查看该任务")
            break
        if state in {"FAILED", "UNKNOWN", "CANCELED"}:
            code = str(P._dig(status, "output.code") or state)
            message = str(P._dig(status, "output.message") or "请在百炼查看任务详情")
            raise P.ProviderError(f"VideoRetalk 任务失败：{code}，{message[:350]}；任务 ID：{saved['job_id']}")
        if state not in {"PENDING", "PRE-PROCESSING", "RUNNING", "POST-PROCESSING"}:
            raise P.ProviderError(f"VideoRetalk 查询响应缺少有效任务状态：{state or '空'}")
        log(f"VideoRetalk {state}，等待结果…")
        time.sleep(float(cfg.get("poll_interval_s", 10)))
    if not url:
        raise P.ProviderError(f"VideoRetalk 等待超时，任务 ID：{saved['job_id']}；可从历史记录继续查询")
    partial = out / ".retalk_download"
    P._retry(lambda: P._http_download(str(url), partial, timeout_s=600),
             what="下载 VideoRetalk 成片", cfg=cfg, log=log)
    check_result(partial)
    partial.replace(clip)
    saved["downloaded"] = True
    persist()
    return [clip]
