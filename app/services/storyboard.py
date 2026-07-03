"""
AI 分镜管线编排（video_source == "ai"）。

流程：文案 → LLM 分镜 → 逐镜头[TTS旁白 + Seedance 画面] → 汇总 → 合成。
核心约定：**镜头时长 ≥ 旁白时长**——先按正常语速合成旁白，镜头时长取
max(分镜建议, 旁白实际时长)并封顶 15s，Seedance 按此时长精确生成(不裁剪)，
旁白不足处补静音。只有旁白超过 15s 硬上限时才不得已加速。这样旁白始终
自然语速，画面与旁白逐镜头对齐。

音频拼接/补时长一律用 FFmpeg（不用 pydub，规避其在 Python 3.13 下 audioop 缺失问题）。
"""

import math
import os
import subprocess
from concurrent.futures import ThreadPoolExecutor

from loguru import logger

from app.config import config
from app.models import const
from app.models.schema import VideoAspect
from app.services import llm, video, video_gen, voice
from app.services import state as sm
from app.utils import utils

_DEFAULT_VOICE = "zh-CN-XiaoxiaoNeural-Female"
# 仅当旁白超过 Seedance 单镜头时长上限(15s)、无法靠加长镜头容纳时，
# 才退而加速重合成，语速上限。
_MAX_SPEEDUP_RATE = 1.6


# --------------------------------------------------------------------------- #
# 配置读取
# --------------------------------------------------------------------------- #
def _cfg(key: str, default):
    value = config.app.get(key)
    return default if value in (None, "") else value


# --------------------------------------------------------------------------- #
# 音频：TTS + 适配到镜头时长
# --------------------------------------------------------------------------- #
def _ffmpeg_pad_audio(in_path: str, out_path: str, duration: float) -> bool:
    """把音频精确规整到 duration 秒：不足用 apad 补静音，超出截断。"""
    cmd = [
        utils.get_ffmpeg_binary(), "-y",
        "-i", in_path,
        "-af", "apad",          # 末尾补静音（无限），配合 -t 截到目标时长
        "-t", f"{duration:.3f}",
        "-c:a", "libmp3lame", "-b:a", "192k",
        out_path,
    ]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        logger.error(f"ffmpeg pad audio failed: {(r.stderr or '').strip()[:300]}")
        return False
    return True


def build_shot_audio(shot: dict, params, work_dir: str) -> dict:
    """
    为单个镜头生成旁白音频，并把镜头时长抬到 >= 旁白时长。

    规则：先按正常语速合成旁白，镜头时长 = max(分镜建议, ceil(旁白时长))，
    封顶 Seedance 上限 15s。旁白不足镜头时长处补静音。只有旁白超过 15s
    无法容纳时才加速重合成。会把最终时长回写到 shot["duration"]，供视频生成使用。
    返回 {"index","audio_path","speak_dur"(用于字幕),"duration"(最终镜头时长)}。
    """
    index = shot["index"]
    narration = shot["narration"]
    voice_name = voice.parse_voice_name(params.voice_name or _DEFAULT_VOICE)
    rate = params.voice_rate or 1.0

    raw_path = os.path.join(work_dir, f"shot-{index}-raw.mp3")
    sub_maker = voice.tts(text=narration, voice_name=voice_name, voice_rate=rate, voice_file=raw_path)
    if sub_maker is None or not os.path.exists(raw_path):
        raise RuntimeError(f"shot {index}: TTS failed")

    speak_dur = float(voice.get_audio_duration(sub_maker)) or float(voice.get_audio_duration(raw_path))

    # 镜头时长必须 >= 旁白时长：按当前 provider 的时长档位，吸附到能容纳
    # max(分镜建议, 旁白)的档位(seedance 连续 4-15；可灵 5/10；海螺 6/10)。
    dmax = video_gen.duration_max()
    duration = video_gen.normalize_duration(max(float(shot["duration"]), speak_dur))

    if speak_dur > dmax:
        # 旁白超过该平台单镜头上限，无法靠加长容纳，退而加速塞进上限
        # （理想情况应在分镜阶段拆成多镜头，这里保证仍能出片）。
        new_rate = min(rate * (speak_dur / dmax), _MAX_SPEEDUP_RATE)
        logger.warning(
            f"shot {index}: narration {speak_dur:.1f}s exceeds {dmax}s cap, "
            f"re-tts at rate {new_rate:.2f} (consider splitting this shot)"
        )
        sub_maker2 = voice.tts(text=narration, voice_name=voice_name, voice_rate=new_rate, voice_file=raw_path)
        if sub_maker2 is not None:
            sub_maker = sub_maker2
            speak_dur = float(voice.get_audio_duration(sub_maker)) or speak_dur
        duration = video_gen.normalize_duration(max(float(shot["duration"]), speak_dur))

    shot["duration"] = duration  # 回写，供 build_shot_video 使用同一时长

    audio_path = os.path.join(work_dir, f"shot-{index}.mp3")
    if not _ffmpeg_pad_audio(raw_path, audio_path, duration):
        raise RuntimeError(f"shot {index}: audio pad failed")

    return {
        "index": index,
        "audio_path": audio_path,
        "speak_dur": min(speak_dur, duration),
        "duration": duration,
    }


# --------------------------------------------------------------------------- #
# 视频：Seedance 生成（含三种一致性模式）
# --------------------------------------------------------------------------- #
def build_shot_video(
    shot: dict, params, work_dir: str,
    resolution: str, ratio: str, max_retries: int,
    first_frame_url: str = "", reference_image_url: str = "",
    need_last_frame: bool = False,
) -> dict:
    """生成单个镜头画面，返回 {"index","video_path","last_frame_url"}。"""
    index = shot["index"]
    save_path = os.path.join(work_dir, f"shot-{index}.mp4")

    images = []
    if first_frame_url:
        images.append({"url": first_frame_url, "role": "first_frame"})
    elif reference_image_url:
        images.append({"url": reference_image_url, "role": "reference_image"})

    result = video_gen.generate_shot(
        prompt=shot["visual_prompt"],
        duration=shot["duration"],
        save_path=save_path,
        resolution=resolution,
        ratio=ratio,
        images=images or None,
        return_last_frame=need_last_frame,
        max_retries=max_retries,
    )
    return {
        "index": index,
        "video_path": result["video_path"],
        "last_frame_url": result.get("last_frame_url"),
    }


def _generate_all_videos(shots, params, work_dir, resolution, ratio, max_retries, concurrency, mode, reference_image_url):
    """按一致性模式生成所有镜头画面，返回按 index 排序的 video 路径列表。"""
    if mode == video_gen.CONSISTENCY_FRAME_CHAIN:
        # 首尾帧衔接：强制串行，逐镜头把上一镜头尾帧作为下一镜头首帧。
        logger.info("consistency=frame_chain, generating shots serially")
        results = []
        prev_last_frame = ""
        for shot in shots:
            r = build_shot_video(
                shot, params, work_dir, resolution, ratio, max_retries,
                first_frame_url=prev_last_frame, need_last_frame=True,
            )
            prev_last_frame = r.get("last_frame_url") or ""
            results.append(r)
        return [r["video_path"] for r in results]

    # none / reference_image：可并发。
    ref = reference_image_url if mode == video_gen.CONSISTENCY_REFERENCE_IMAGE else ""
    logger.info(f"consistency={mode}, generating {len(shots)} shots concurrently (max {concurrency})")
    with ThreadPoolExecutor(max_workers=max(1, concurrency)) as ex:
        futures = [
            ex.submit(
                build_shot_video, shot, params, work_dir, resolution, ratio, max_retries,
                "", ref, False,
            )
            for shot in shots
        ]
        results = [f.result() for f in futures]  # 任一镜头抛异常 -> 整任务失败
    results.sort(key=lambda r: r["index"])
    return [r["video_path"] for r in results]


# --------------------------------------------------------------------------- #
# 汇总：音频拼接 / 字幕合并
# --------------------------------------------------------------------------- #
def _concat_audio(audio_paths, out_path, work_dir) -> str:
    list_file = os.path.join(work_dir, "audio-concat-list.txt")
    with open(list_file, "w", encoding="utf-8") as f:
        for p in audio_paths:
            f.write(f"file '{os.path.abspath(p)}'\n")
    cmd = [
        utils.get_ffmpeg_binary(), "-y", "-f", "concat", "-safe", "0",
        "-i", list_file, "-c:a", "libmp3lame", "-b:a", "192k", out_path,
    ]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"audio concat failed: {(r.stderr or '').strip()[:300]}")
    return out_path


def _build_subtitle(shots, shot_audios, subtitle_path) -> str:
    """
    按镜头时间轴合并字幕：每个镜头旁白按标点切句，在该镜头的语音时长内
    按字数比例分配时间；镜头起始时间 = 之前所有镜头时长累计。
    """
    speak_by_index = {a["index"]: a["speak_dur"] for a in shot_audios}
    entries = []
    start = 0.0
    for shot in shots:
        speak = speak_by_index.get(shot["index"], shot["duration"])
        lines = [ln for ln in utils.split_string_by_punctuations(shot["narration"]) if ln.strip()]
        total_chars = sum(len(ln) for ln in lines) or 1
        t = start
        for ln in lines:
            seg = speak * (len(ln) / total_chars)
            entries.append((t, t + seg, ln.strip()))
            t += seg
        start += float(shot["duration"])  # 下一镜头从本镜头时长(含静音)之后开始

    srt = "\n".join(
        utils.text_to_srt(i + 1, text, s, e) for i, (s, e, text) in enumerate(entries)
    )
    with open(subtitle_path, "w", encoding="utf-8") as f:
        f.write(srt)
    return subtitle_path


def generate_character_preview(
    task_id: str, params, reference_image: str, test_prompt: str = "", duration: int = 4
) -> str:
    """
    用用户上传的主角图生成一个测试镜头，供用户确认主角形象后再批量出片。

    低成本档位(480p、短时长)，产物 character-preview.mp4；不参与最终合成，
    只用于确认 Seedance 是否较好地保持了主角一致性。
    """
    work_dir = utils.task_dir(task_id)
    data_url = video_gen.image_to_data_url(reference_image)
    if not data_url:
        raise RuntimeError("reference image is empty")

    ratio = _cfg("seedance_ratio", "9:16")
    max_retries = int(_cfg("seedance_max_retries", 2))
    prompt = test_prompt.strip() or "主角的中景镜头，自然的动作与表情，画面清晰"
    save_path = os.path.join(work_dir, "character-preview.mp4")
    if os.path.exists(save_path):
        os.remove(save_path)  # 每次确认都重新生成，避免看到旧图

    logger.info(f"\n\n## [AI] generating character preview (480p) for confirmation")
    result = video_gen.generate_shot(
        prompt=prompt,
        duration=duration,
        save_path=save_path,
        resolution="480p",
        ratio=ratio,
        images=[{"url": data_url, "role": "reference_image"}],
        max_retries=max_retries,
    )
    return result["video_path"]


# --------------------------------------------------------------------------- #
# 主编排
# --------------------------------------------------------------------------- #
def generate_ai_video(task_id: str, params, video_script: str, storyboard: list) -> dict:
    """
    从(已确定的)分镜生成最终视频。返回结果 dict 或在失败时更新任务状态并返回 None。
    """
    work_dir = utils.task_dir(task_id)
    resolution = _cfg("seedance_resolution", "720p")
    # 宽高比单一来源：由 params.video_aspect 驱动(UI 的「Video Ratio」)，
    # 保证 Seedance 生成比例与最终渲染比例一致。
    try:
        ratio = VideoAspect(params.video_aspect).value
    except Exception:
        ratio = _cfg("seedance_ratio", "9:16")
    mode = _cfg("seedance_consistency_mode", video_gen.CONSISTENCY_NONE)
    concurrency = int(_cfg("seedance_max_concurrency", 3))
    max_retries = int(_cfg("seedance_max_retries", 2))
    reference_image_url = getattr(params, "seedance_reference_image", "") or ""

    # 首尾帧衔接仅部分 provider(能返回尾帧)支持；不支持时降级。
    if mode == video_gen.CONSISTENCY_FRAME_CHAIN and not video_gen.supports_frame_chain():
        logger.warning(
            f"provider '{video_gen.provider_name()}' does not support frame_chain, "
            f"falling back to reference_image/none"
        )
        mode = video_gen.CONSISTENCY_REFERENCE_IMAGE if reference_image_url else video_gen.CONSISTENCY_NONE

    if mode == video_gen.CONSISTENCY_REFERENCE_IMAGE:
        if not reference_image_url:
            logger.warning("consistency=reference_image but no reference image provided, falling back to none")
            mode = video_gen.CONSISTENCY_NONE
        else:
            # 用户上传的本地主角图转成 data URL 供 provider 使用。
            reference_image_url = video_gen.image_to_data_url(reference_image_url)

    # 1) 旁白音频（并发，成本低）
    logger.info(f"\n\n## [AI] generating narration audio for {len(storyboard)} shots")
    try:
        with ThreadPoolExecutor(max_workers=max(1, concurrency)) as ex:
            shot_audios = list(ex.map(lambda s: build_shot_audio(s, params, work_dir), storyboard))
    except Exception as e:
        logger.error(f"[AI] audio generation failed: {str(e)}")
        sm.state.update_task(task_id, state=const.TASK_STATE_FAILED)
        return None
    shot_audios.sort(key=lambda a: a["index"])
    sm.state.update_task(task_id, progress=40)

    # 2) 镜头画面（Seedance）
    logger.info(f"\n\n## [AI] generating {len(storyboard)} shot videos via {video_gen.provider_name()} ({resolution} {ratio})")
    try:
        shot_videos = _generate_all_videos(
            storyboard, params, work_dir, resolution, ratio, max_retries,
            concurrency, mode, reference_image_url,
        )
    except Exception as e:
        logger.error(f"[AI] shot video generation failed: {str(e)}")
        sm.state.update_task(task_id, state=const.TASK_STATE_FAILED)
        return None
    sm.state.update_task(task_id, progress=80)

    # 3) 汇总：拼音频 / 拼画面 / 合字幕
    logger.info("\n\n## [AI] combining audio, video and subtitle")
    audio_file = _concat_audio([a["audio_path"] for a in shot_audios],
                               os.path.join(work_dir, "audio.mp3"), work_dir)
    combined_video = os.path.join(work_dir, "combined-1.mp4")
    video.concat_video_clips_with_ffmpeg(
        clip_files=shot_videos, output_file=combined_video,
        threads=params.n_threads, output_dir=work_dir,
    )

    subtitle_path = ""
    if params.subtitle_enabled:
        subtitle_path = _build_subtitle(storyboard, shot_audios,
                                        os.path.join(work_dir, "subtitle.srt"))

    # 4) 合成（复用现有：字幕叠加 + BGM + 编码）
    final_video_path = os.path.join(work_dir, "final-1.mp4")
    logger.info(f"\n\n## [AI] generating final video => {final_video_path}")
    video.generate_video(
        video_path=combined_video, audio_path=audio_file,
        subtitle_path=subtitle_path, output_file=final_video_path, params=params,
    )
    sm.state.update_task(task_id, progress=100)

    return {
        "videos": [final_video_path],
        "combined_videos": [combined_video],
        "script": video_script,
        "storyboard": storyboard,
        "audio_file": audio_file,
        "subtitle_path": subtitle_path,
    }


def resolve_character(params) -> str:
    """
    得出「主角设定」文本：优先用用户显式给的 character_description；
    否则若提供了主角参考图，用视觉模型分析该图自动得出（数量+外观）。
    无主角图也无描述时返回空串（走无主角的纯文生视频）。
    """
    described = (getattr(params, "character_description", "") or "").strip()
    if described:
        return described
    reference_image = (getattr(params, "seedance_reference_image", "") or "").strip()
    if not reference_image:
        return ""
    try:
        data_url = video_gen.image_to_data_url(reference_image)
    except Exception as e:
        logger.warning(f"[AI] cannot read reference image for analysis: {str(e)}")
        return ""
    profile = llm.analyze_reference_image(data_url)
    desc = (profile.get("description") or "").strip()
    if desc:
        logger.info(f"[AI] derived protagonist(s) from image: count={profile.get('count')}")
    return desc


def start_ai(task_id: str, params, stop_at: str = "video"):
    """
    AI 分镜管线入口（由 task.start 在 video_source=="ai" 时调用），自持完整流程：
      图片分析(主角) → 文案(围绕主角) → 分镜(每镜头带主角) → 逐镜头生成 → 合成。

    分阶段(stop_at)：
      "script"     只返回文案(+主角设定)
      "storyboard" 返回文案+分镜，供用户调整后通过 params.video_storyboard 回传续跑
      其余         一路出片
    """
    # 0. 主角设定：用户显式描述优先，否则分析参考图自动得出。回写到 params，
    #    供文案/分镜共用同一份主角设定。
    character_description = resolve_character(params)
    try:
        params.character_description = character_description
    except Exception:
        pass

    # 1. 文案：用户已给则用之，否则围绕主角生成。
    video_script = (getattr(params, "video_script", "") or "").strip()
    if not video_script:
        video_script = llm.generate_script(
            video_subject=params.video_subject,
            language=params.video_language or "",
            paragraph_number=params.paragraph_number,
            video_script_prompt=params.video_script_prompt,
            custom_system_prompt=params.custom_system_prompt,
            character_description=character_description,
        )
    if not video_script or "Error: " in video_script:
        sm.state.update_task(task_id, state=const.TASK_STATE_FAILED)
        logger.error("[AI] failed to generate script")
        return None
    sm.state.update_task(task_id, state=const.TASK_STATE_PROCESSING, progress=10)

    if stop_at == "script":
        sm.state.update_task(
            task_id, state=const.TASK_STATE_COMPLETE, progress=100,
            script=video_script, character_description=character_description,
        )
        return {"script": video_script, "character_description": character_description}

    # 2. 分镜：优先用用户回传的(已调整)分镜，否则围绕主角生成。
    storyboard = getattr(params, "video_storyboard", None)
    if not storyboard:
        storyboard = llm.generate_storyboard(
            video_script=video_script,
            video_subject=params.video_subject,
            language=params.video_language or "",
            character_description=character_description,
            max_shot_seconds=video_gen.duration_max(),
        )
    if not storyboard:
        sm.state.update_task(task_id, state=const.TASK_STATE_FAILED)
        logger.error("[AI] failed to generate storyboard")
        return None

    if stop_at == "storyboard":
        sm.state.update_task(
            task_id, state=const.TASK_STATE_COMPLETE, progress=100,
            script=video_script, storyboard=storyboard,
            character_description=character_description,
        )
        return {
            "script": video_script, "storyboard": storyboard,
            "character_description": character_description,
        }

    sm.state.update_task(task_id, state=const.TASK_STATE_PROCESSING, progress=30)
    result = generate_ai_video(task_id, params, video_script, storyboard)
    if not result:
        return None

    sm.state.update_task(task_id, state=const.TASK_STATE_COMPLETE, progress=100, **result)
    logger.success(f"[AI] task {task_id} finished")
    return result
