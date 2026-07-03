"""
视频生成 provider 的共享基础工具（被 seedance / wan / kling / hailuo 复用）。

只放与具体平台无关的通用逻辑：TLS、下载、图片转 data URL、时长吸附。
不导入任何 provider，也不导入 video_gen，避免循环依赖。
"""

import base64
import math
import mimetypes
import os

import requests
from loguru import logger

from app.config import config


class VideoGenError(Exception):
    """视频生成失败（创建/轮询/下载任一环节），provider 通用异常。"""


def get_tls_verify() -> bool:
    tls_verify = config.app.get("tls_verify", True)
    if isinstance(tls_verify, str):
        tls_verify = tls_verify.strip().lower() not in ("0", "false", "no", "off")
    return bool(tls_verify)


def image_to_data_url(path_or_url: str) -> str:
    """
    把用户提供的参考图规整成各平台可接受的 image 输入：
    - http(s):// / data: / asset:// 原样返回。
    - 本地文件路径 -> data:<mime>;base64,<...>。
    """
    s = (path_or_url or "").strip()
    if not s:
        return ""
    if s.startswith(("http://", "https://", "data:", "asset://")):
        return s
    if not os.path.isfile(s):
        raise VideoGenError(f"reference image not found: {s}")
    mime = mimetypes.guess_type(s)[0] or "image/png"
    with open(s, "rb") as f:
        b64 = base64.b64encode(f.read()).decode("ascii")
    return f"data:{mime};base64,{b64}"


def image_to_base64(path_or_url: str) -> str:
    """返回纯 base64(无 data: 前缀)。部分平台(如 MiniMax)要求裸 base64 或 data URL。"""
    data_url = image_to_data_url(path_or_url)
    if data_url.startswith("data:"):
        return data_url.split(",", 1)[1]
    return data_url  # http url：原样返回，由调用方决定是否支持 url


def download_file(url: str, save_path: str, timeout=(60, 240)) -> str:
    save_dir = os.path.dirname(save_path)
    if save_dir and not os.path.exists(save_dir):
        os.makedirs(save_dir, exist_ok=True)
    resp = requests.get(
        url, proxies=config.proxy, verify=get_tls_verify(), timeout=timeout
    )
    if resp.status_code != 200 or not resp.content:
        raise VideoGenError(f"download failed (HTTP {resp.status_code}) from {url[:120]}")
    with open(save_path, "wb") as f:
        f.write(resp.content)
    if not (os.path.exists(save_path) and os.path.getsize(save_path) > 0):
        raise VideoGenError(f"downloaded file is empty: {save_path}")
    logger.info(f"video saved: {save_path} ({os.path.getsize(save_path) / 1024:.1f} KB)")
    return save_path


def snap_duration(seconds, allowed, max_val=None) -> int:
    """
    把目标时长吸附到平台支持的离散时长集合里：取「>= seconds 的最小允许值」，
    使视频时长 >= 旁白时长；若都比 seconds 小则取最大允许值(此时上层会加速旁白)。
    allowed: 允许的整数秒集合(如 {5,10})。
    """
    try:
        need = math.ceil(float(seconds))
    except (TypeError, ValueError):
        need = min(allowed)
    candidates = sorted(set(allowed))
    for d in candidates:
        if d >= need:
            return d
    return candidates[-1] if candidates else (max_val or need)


def first_image(images, roles=None):
    """从 images=[{url,role}] 里取第一张(可按 roles 过滤)的 url。"""
    for img in images or []:
        if not img or not img.get("url"):
            continue
        if roles and img.get("role") not in roles:
            continue
        return img["url"]
    return ""
