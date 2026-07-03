"""
视频生成统一调度层（可插拔）。

按 config.app["video_provider"] 把「生成一个镜头」的请求路由到对应平台
provider。上层(storyboard)只依赖本模块，不直接依赖具体平台。

当前仅内置 seedance。后续要接入新平台(如通义万相/可灵/海螺等)：
  1. 新建 app/services/<name>.py，实现与 seedance 相同的接口：
       - 常量 PROVIDER / DURATION_MIN / DURATION_MAX
       - normalize_duration(seconds) -> int   # 把目标时长吸附到该平台档位
       - generate_shot(prompt, duration, save_path, resolution, ratio,
                       images=None, return_last_frame=False, seed=-1,
                       max_retries=2, ...) -> {"video_path","last_frame_url","usage"}
       (可复用 app/services/video_base.py 里的 下载/图片编码/时长吸附 等工具)
  2. 在下方 _PROVIDERS 注册：{"<name>": <module>}
  3. config.toml 里加该平台的 key 等配置，并把 video_provider 切过去。
  仅 seedance 因能返回尾帧而支持 frame_chain(见 supports_frame_chain)。
"""

from loguru import logger

from app.config import config
from app.services import seedance
from app.services.video_base import VideoGenError, image_to_data_url  # re-export

# 跨镜头一致性模式(与具体平台无关)。
CONSISTENCY_NONE = "none"
CONSISTENCY_REFERENCE_IMAGE = "reference_image"
CONSISTENCY_FRAME_CHAIN = "frame_chain"

# provider 注册表。新增平台在此登记即可(见模块 docstring)。
_PROVIDERS = {
    "seedance": seedance,
}

_DEFAULT_PROVIDER = "seedance"


def provider_name() -> str:
    return (config.app.get("video_provider") or _DEFAULT_PROVIDER).strip().lower()


def get_provider():
    name = provider_name()
    provider = _PROVIDERS.get(name)
    if provider is None:
        logger.warning(f"unknown video_provider '{name}', falling back to {_DEFAULT_PROVIDER}")
        return _PROVIDERS[_DEFAULT_PROVIDER]
    return provider


def normalize_duration(seconds) -> int:
    """按当前 provider 的时长档位归一化(使视频时长 >= 旁白时长)。"""
    return get_provider().normalize_duration(seconds)


def duration_max() -> int:
    return int(getattr(get_provider(), "DURATION_MAX", 15))


def supports_frame_chain() -> bool:
    """只有能返回尾帧的 provider 才支持首尾帧衔接(目前仅 seedance)。"""
    return provider_name() == "seedance"


def generate_shot(prompt, duration, save_path, resolution="720p", ratio="9:16",
                  images=None, return_last_frame=False, seed=-1, max_retries=2,
                  poll_interval=5, poll_timeout=600, model="", **kwargs) -> dict:
    """
    统一的单镜头生成接口，返回 {"video_path","last_frame_url","usage"}。
    路由到当前配置的 provider。
    """
    return get_provider().generate_shot(
        prompt=prompt, duration=duration, save_path=save_path,
        resolution=resolution, ratio=ratio, images=images,
        return_last_frame=return_last_frame, seed=seed, max_retries=max_retries,
        poll_interval=poll_interval, poll_timeout=poll_timeout, model=model, **kwargs,
    )
