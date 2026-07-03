"""
Seedance 2.0 (火山方舟 / Volcengine Ark) 视频生成服务。

为 AI 分镜管线提供「文生视频 / 图生视频」能力：
  - 按分镜给定的时长精确生成，不做二次裁剪（分镜时长即主时钟）。
  - 支持三种跨镜头一致性模式（互斥，由上层选择）：
      none            纯文生视频，各镜头独立、可并发。
      reference_image 各镜头带同一张参考图，保证角色/风格一致。
      frame_chain     上一镜头尾帧作下一镜头首帧，画面无缝但强制串行。

接口为异步任务式：创建任务 -> 轮询状态 -> 取视频 URL 下载。
鉴权只支持方舟 API Key（Bearer），不支持 AK/SK。
"""

import base64
import mimetypes
import os
import time

import requests
from loguru import logger

from app.config import config
from app.utils import utils

_DEFAULT_BASE_URL = "https://ark.cn-beijing.volces.com/api/v3"
_DEFAULT_MODEL = "doubao-seedance-2-0-260128"
# Seedance 单镜头时长限制（秒，整数）。
DURATION_MIN = 4
DURATION_MAX = 15

# 一致性模式常量（供上层引用，避免各处拼写字符串）。
CONSISTENCY_NONE = "none"
CONSISTENCY_REFERENCE_IMAGE = "reference_image"
CONSISTENCY_FRAME_CHAIN = "frame_chain"


class SeedanceError(Exception):
    """Seedance 生成失败（创建/轮询/下载任一环节）。"""


def _get_api_key() -> str:
    # 视频与文本（doubao LLM）共用同一个方舟 API Key。这里优先取专用配置，
    # 未单独配置时回退到 volcengine_api_key，避免用户重复填写。
    api_key = (
        config.app.get("seedance_api_key")
        or config.app.get("volcengine_api_key")
        or ""
    ).strip()
    if not api_key:
        raise SeedanceError(
            "seedance_api_key (or volcengine_api_key) is not set in config.toml. "
            "请在火山方舟控制台『API Key 管理』创建 Bearer API Key 后填入。"
        )
    return api_key


def _get_base_url() -> str:
    return (config.app.get("seedance_base_url") or _DEFAULT_BASE_URL).rstrip("/")


def _get_model() -> str:
    return config.app.get("seedance_model") or _DEFAULT_MODEL


def _get_tls_verify() -> bool:
    tls_verify = config.app.get("tls_verify", True)
    if isinstance(tls_verify, str):
        tls_verify = tls_verify.strip().lower() not in ("0", "false", "no", "off")
    return bool(tls_verify)


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {_get_api_key()}",
        "Content-Type": "application/json",
    }


def clamp_duration(duration) -> int:
    """把分镜时长归一化为 Seedance 支持的整数秒 [4, 15]。"""
    try:
        value = int(round(float(duration)))
    except (TypeError, ValueError):
        value = DURATION_MIN
    return max(DURATION_MIN, min(DURATION_MAX, value))


def normalize_duration(seconds) -> int:
    """
    统一的时长归一化(video_gen 调度用)：向上取整到能容纳旁白的整数秒，
    并夹到 [4,15]。Seedance 时长连续，直接 ceil+clamp。
    """
    import math
    try:
        value = math.ceil(float(seconds))
    except (TypeError, ValueError):
        value = DURATION_MIN
    return max(DURATION_MIN, min(DURATION_MAX, value))


# 供 video_gen 调度识别的 provider 名。
PROVIDER = "seedance"


def image_to_data_url(path_or_url: str) -> str:
    """
    把用户提供的主角参考图规整成 Seedance 可接受的 image_url。

    - http(s):// / data: / asset:// 直接原样返回。
    - 本地文件路径 -> 读取并编码为 data:<mime>;base64,<...> 数据 URL
      （本地图片无法给出可访问 URL，用内联 base64 传给 Seedance）。
    """
    s = (path_or_url or "").strip()
    if not s:
        return ""
    if s.startswith(("http://", "https://", "data:", "asset://")):
        return s
    if not os.path.isfile(s):
        raise SeedanceError(f"reference image not found: {s}")
    mime = mimetypes.guess_type(s)[0] or "image/png"
    with open(s, "rb") as f:
        b64 = base64.b64encode(f.read()).decode("ascii")
    return f"data:{mime};base64,{b64}"


def build_content(text: str, images=None) -> list:
    """
    构造请求体的 content 数组。

    images: 可选的图片列表，每项为 {"url": str, "role": str}，
            role ∈ {"first_frame", "last_frame", "reference_image"}。
    """
    content = [{"type": "text", "text": text}]
    for image in images or []:
        url = (image or {}).get("url")
        role = (image or {}).get("role")
        if not url or not role:
            continue
        content.append(
            {
                "type": "image_url",
                "image_url": {"url": url},
                "role": role,
            }
        )
    return content


def create_task(
    prompt: str,
    duration,
    resolution: str = "720p",
    ratio: str = "9:16",
    images=None,
    generate_audio: bool = False,
    return_last_frame: bool = False,
    seed: int = -1,
    model: str = "",
) -> str:
    """
    创建一个视频生成任务，返回 task_id。

    duration 会被归一化到 [4, 15] 整数秒；视频按此时长精确生成，上层无需裁剪。
    """
    body = {
        "model": model or _get_model(),
        "content": build_content(prompt, images=images),
        "resolution": resolution,
        "ratio": ratio,
        "duration": clamp_duration(duration),
        "generate_audio": bool(generate_audio),
        "return_last_frame": bool(return_last_frame),
    }
    if seed is not None and seed >= 0:
        body["seed"] = int(seed)

    url = f"{_get_base_url()}/contents/generations/tasks"
    resp = requests.post(
        url,
        headers=_headers(),
        json=body,
        proxies=config.proxy,
        verify=_get_tls_verify(),
        timeout=(30, 60),
    )
    try:
        data = resp.json()
    except Exception:
        raise SeedanceError(
            f"create task: non-JSON response (HTTP {resp.status_code}): {resp.text[:300]}"
        )

    if resp.status_code != 200 or "id" not in data:
        error = data.get("error", data)
        raise SeedanceError(
            f"create task failed (HTTP {resp.status_code}): {utils.to_json(error)}"
        )

    task_id = data["id"]
    logger.info(f"seedance task created: {task_id} (duration={body['duration']}s, {resolution} {ratio})")
    return task_id


def poll_task(task_id: str, poll_interval: int = 5, timeout: int = 600) -> dict:
    """
    轮询任务直到成功/失败，返回 {"video_url": ..., "last_frame_url": ..., "usage": ...}。
    超时或失败抛 SeedanceError。
    """
    url = f"{_get_base_url()}/contents/generations/tasks/{task_id}"
    deadline = time.time() + timeout
    while time.time() < deadline:
        resp = requests.get(
            url,
            headers=_headers(),
            proxies=config.proxy,
            verify=_get_tls_verify(),
            timeout=(30, 60),
        )
        try:
            data = resp.json()
        except Exception:
            raise SeedanceError(
                f"poll task {task_id}: non-JSON response: {resp.text[:300]}"
            )

        status = data.get("status")
        if status == "succeeded":
            content = data.get("content") or {}
            video_url = content.get("video_url")
            if not video_url:
                raise SeedanceError(
                    f"task {task_id} succeeded but no video_url: {utils.to_json(data)}"
                )
            return {
                "video_url": video_url,
                "last_frame_url": content.get("last_frame_url"),
                "usage": data.get("usage"),
            }
        if status in ("failed", "expired"):
            raise SeedanceError(
                f"task {task_id} {status}: {utils.to_json(data.get('error', data))}"
            )

        logger.debug(f"seedance task {task_id} status={status}, waiting...")
        time.sleep(poll_interval)

    raise SeedanceError(f"task {task_id} timed out after {timeout}s")


def download_video(video_url: str, save_path: str) -> str:
    """下载生成的视频到 save_path。video_url 约 24 小时过期，须及时下载。"""
    save_dir = os.path.dirname(save_path)
    if save_dir and not os.path.exists(save_dir):
        os.makedirs(save_dir, exist_ok=True)

    resp = requests.get(
        video_url,
        proxies=config.proxy,
        verify=_get_tls_verify(),
        timeout=(60, 240),
    )
    if resp.status_code != 200 or not resp.content:
        raise SeedanceError(
            f"download failed (HTTP {resp.status_code}) from {video_url[:120]}"
        )
    with open(save_path, "wb") as f:
        f.write(resp.content)

    if not (os.path.exists(save_path) and os.path.getsize(save_path) > 0):
        raise SeedanceError(f"downloaded file is empty: {save_path}")
    logger.info(f"seedance video saved: {save_path} ({os.path.getsize(save_path) / 1024:.1f} KB)")
    return save_path


def generate_shot(
    prompt: str,
    duration,
    save_path: str,
    resolution: str = "720p",
    ratio: str = "9:16",
    images=None,
    return_last_frame: bool = False,
    seed: int = -1,
    max_retries: int = 2,
    poll_interval: int = 5,
    poll_timeout: int = 600,
    model: str = "",
) -> dict:
    """
    生成单个镜头（创建 -> 轮询 -> 下载），带重试。

    返回 {"video_path": save_path, "last_frame_url": ..., "usage": ...}。
    重试 max_retries 次仍失败则抛 SeedanceError（由上层决定整任务失败）。
    """
    # 命中缓存直接返回，避免重复生成（重跑同一任务时省钱省时）。
    if os.path.exists(save_path) and os.path.getsize(save_path) > 0:
        logger.info(f"seedance shot already exists, skip: {save_path}")
        return {"video_path": save_path, "last_frame_url": None, "usage": None}

    last_error = None
    for attempt in range(max_retries + 1):
        try:
            task_id = create_task(
                prompt=prompt,
                duration=duration,
                resolution=resolution,
                ratio=ratio,
                images=images,
                generate_audio=False,  # 旁白用我们自己的 TTS，关掉模型自带音频
                return_last_frame=return_last_frame,
                seed=seed,
                model=model,
            )
            result = poll_task(
                task_id, poll_interval=poll_interval, timeout=poll_timeout
            )
            download_video(result["video_url"], save_path)
            return {
                "video_path": save_path,
                "last_frame_url": result.get("last_frame_url"),
                "usage": result.get("usage"),
            }
        except SeedanceError as e:
            last_error = e
            logger.warning(
                f"seedance shot failed (attempt {attempt + 1}/{max_retries + 1}): {str(e)}"
            )

    raise SeedanceError(f"shot generation failed after retries: {str(last_error)}")
