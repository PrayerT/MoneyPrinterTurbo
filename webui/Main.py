import json
import os
import sys
import webbrowser
from uuid import UUID, uuid4

import requests
import streamlit as st
from loguru import logger

# Add the root directory of the project to the system path to allow importing modules from the project
root_dir = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
if root_dir not in sys.path:
    sys.path.append(root_dir)
    print("******** sys.path ********")
    print(sys.path)
    print("")

from app.config import config
from app.models.schema import (
    MaterialInfo,
    VideoAspect,
    VideoConcatMode,
    VideoParams,
    VideoTransitionMode,
)
from app.services import llm, voice
from app.services import task as tm
from app.utils import utils

st.set_page_config(
    page_title="MoneyPrinterTurbo",
    page_icon="🤖",
    layout="wide",
    initial_sidebar_state="auto",
    menu_items={
        "Report a bug": "https://github.com/harry0703/MoneyPrinterTurbo/issues",
        "About": "# MoneyPrinterTurbo\nSimply provide a topic or keyword for a video, and it will "
        "automatically generate the video copy, video materials, video subtitles, "
        "and video background music before synthesizing a high-definition short "
        "video.\n\nhttps://github.com/harry0703/MoneyPrinterTurbo",
    },
)


streamlit_style = """
<style>
h1 {
    padding-top: 0 !important;
}
</style>
"""
st.markdown(streamlit_style, unsafe_allow_html=True)

# 定义资源目录
font_dir = os.path.join(root_dir, "resource", "fonts")
song_dir = os.path.join(root_dir, "resource", "songs")
i18n_dir = os.path.join(root_dir, "webui", "i18n")
config_file = os.path.join(root_dir, "webui", ".streamlit", "webui.toml")
system_locale = utils.get_system_locale()
DEFAULT_CHATTERBOX_BASE_URL = "http://127.0.0.1:4123/v1"
DEFAULT_CHATTERBOX_MODEL = "chatterbox"
DEFAULT_CHATTERBOX_VOICES = ["default-Female"]


def _parse_chatterbox_voices(voices):
    # Chatterbox 是自托管服务，音色列表由用户在 WebUI 中手动输入。
    # 这里统一兼容 TOML 数组和输入框里的逗号分隔字符串，避免下拉框、
    # 试听按钮和后续生成流程使用不同格式导致状态不一致。
    if isinstance(voices, str):
        return [v.strip() for v in voices.split(",") if v.strip()]
    return [str(v).strip() for v in voices or [] if str(v).strip()]


def _sync_chatterbox_config_from_session_state():
    # Streamlit 的按钮会触发整页 rerun，而 Chatterbox 配置输入框位于
    # “试听语音合成”按钮之后。如果试听时只读取 config.chatterbox，可能拿不到
    # 用户刚在输入框里填入的 base_url/model/voices。先从 session_state 同步一次，
    # 可以保证按钮逻辑和输入框显示逻辑使用同一份最新配置。
    config.chatterbox["base_url"] = (
        st.session_state.get(
            "chatterbox_base_url_input",
            config.chatterbox.get("base_url") or DEFAULT_CHATTERBOX_BASE_URL,
        )
        or ""
    ).strip()
    config.chatterbox["api_key"] = st.session_state.get(
        "chatterbox_api_key_input", config.chatterbox.get("api_key", "")
    )
    config.chatterbox["model_id"] = (
        st.session_state.get(
            "chatterbox_model_input",
            config.chatterbox.get("model_id") or DEFAULT_CHATTERBOX_MODEL,
        )
        or DEFAULT_CHATTERBOX_MODEL
    ).strip()
    config.chatterbox["voices"] = _parse_chatterbox_voices(
        st.session_state.get(
            "chatterbox_voices_input",
            config.chatterbox.get("voices") or DEFAULT_CHATTERBOX_VOICES,
        )
    )


def _detect_audio_mime(audio_file: str, audio_bytes: bytes) -> str:
    # 有些 OpenAI-compatible TTS 服务，例如 travisvn/chatterbox-tts-api，
    # 即使请求 response_format=mp3，也会返回 WAV 内容。WebUI 试听如果固定
    # 使用 audio/mp3，浏览器可能无法播放，因此这里按文件头识别真实格式。
    header = audio_bytes[:12]
    if header.startswith(b"RIFF") and header[8:12] == b"WAVE":
        return "audio/wav"
    if header.startswith(b"ID3") or header[:2] in (b"\xff\xfb", b"\xff\xf3", b"\xff\xf2"):
        return "audio/mp3"
    if header.startswith(b"OggS"):
        return "audio/ogg"
    ext = os.path.splitext(audio_file)[1].lower()
    return {
        ".wav": "audio/wav",
        ".m4a": "audio/mp4",
        ".aac": "audio/aac",
        ".ogg": "audio/ogg",
        ".flac": "audio/flac",
    }.get(ext, "audio/mp3")


if "video_subject" not in st.session_state:
    st.session_state["video_subject"] = ""
if "video_script" not in st.session_state:
    st.session_state["video_script"] = ""
if "video_terms" not in st.session_state:
    st.session_state["video_terms"] = ""
if "video_script_prompt" not in st.session_state:
    st.session_state["video_script_prompt"] = ""
if "custom_system_prompt" not in st.session_state:
    st.session_state["custom_system_prompt"] = llm.DEFAULT_SCRIPT_SYSTEM_PROMPT
if "use_custom_system_prompt" not in st.session_state:
    st.session_state["use_custom_system_prompt"] = False
if "match_materials_to_script" not in st.session_state:
    st.session_state["match_materials_to_script"] = bool(
        config.app.get("match_materials_to_script", False)
    )
if "ui_language" not in st.session_state:
    st.session_state["ui_language"] = config.ui.get("language", system_locale)
if "local_video_materials" not in st.session_state:
    # 记住用户最近一次已经落盘的本地素材，避免仅修改文案后二次生成时丢失素材列表。
    st.session_state["local_video_materials"] = []
# AI 分镜管线(video_source == "ai")的两阶段状态。
if "ai_storyboard" not in st.session_state:
    st.session_state["ai_storyboard"] = []           # 分镜列表(生成后可被用户编辑)
if "ai_character_desc" not in st.session_state:
    st.session_state["ai_character_desc"] = ""        # 主角设定(分析得出,可编辑)
if "ai_reference_image" not in st.session_state:
    st.session_state["ai_reference_image"] = ""       # 主角图落盘路径


def _sanitize_storyboard(rows):
    """把(用户编辑过的)分镜表清洗成后端可用格式：丢空行、时长转整数、重排 index。"""
    shots = []
    for row in rows or []:
        row = dict(row)
        narration = str(row.get("narration", "") or "").strip()
        visual_prompt = str(row.get("visual_prompt", "") or "").strip()
        if not narration or not visual_prompt:
            continue
        try:
            duration = int(round(float(row.get("duration", 5))))
        except (TypeError, ValueError):
            duration = 5
        shots.append({
            "index": len(shots) + 1,
            "narration": narration,
            "visual_prompt": visual_prompt,
            "duration": max(4, min(15, duration)),
        })
    return shots


def generation_conditions(params):
    """出片前置条件清单，对齐三个内容 tab：文案 / 视频来源 / 音频字幕。
    返回 [{label, met, target}, ...]；target 指明去哪里满足：
    'script'=文案 tab / 'video'=视频 tab / 'audio'=音频字幕 tab / 'basic'=顶部基础设置。"""
    conds = []

    # 1) 文案：主题或文案二者其一
    has_subject = bool((params.video_subject or "").strip() or (params.video_script or "").strip())
    conds.append({"label": tr("Cond Subject"), "met": has_subject, "target": "script"})

    # 2) 视频来源：AI 途径把『分镜/主角图』并进本条(它们是视频来源的一部分)；
    #    图库/本地则各查各自的 Key 或素材。缺口在哪就把『前往』指向哪。
    src = params.video_source
    if src == "ai":
        has_key = bool((config.app.get("seedance_api_key") or config.app.get("volcengine_api_key") or "").strip())
        need_ref = config.app.get("seedance_consistency_mode") == "reference_image"
        has_ref = bool(st.session_state.get("ai_reference_image"))
        has_story = bool(_sanitize_storyboard(st.session_state.get("ai_storyboard", [])))
        source_met = has_key and has_story and (has_ref if need_ref else True)
        # 缺 Key 去基础设置配置，其余(分镜/主角图)都在视频 tab 里搞定。
        source_target = "basic" if not has_key else "video"
        conds.append({"label": tr("Cond Source Ready"), "met": source_met, "target": source_target})
    elif src == "pexels":
        conds.append({"label": tr("Cond Source Ready"), "met": bool(config.app.get("pexels_api_keys")), "target": "basic"})
    elif src == "pixabay":
        conds.append({"label": tr("Cond Source Ready"), "met": bool(config.app.get("pixabay_api_keys")), "target": "basic"})
    elif src == "coverr":
        conds.append({"label": tr("Cond Source Ready"), "met": bool(config.app.get("coverr_api_keys")), "target": "basic"})
    elif src == "local":
        has_local = bool(st.session_state.get("local_video_materials")) or bool(params.video_materials)
        conds.append({"label": tr("Cond Source Ready"), "met": has_local, "target": "video"})
    else:
        conds.append({"label": tr("Cond Source Ready"), "met": True, "target": "video"})

    # 3) 音频 / 字幕：合并为一条(已选配音即视为就绪，可去音频 tab 微调)
    audio_met = bool((params.voice_name or "").strip())
    conds.append({"label": tr("Cond Audio Subtitle"), "met": audio_met, "target": "audio"})

    return conds


def render_ai_workspace(params):
    """
    AI 分镜工作台（第一步设参数生成分镜 + 第二步编辑分镜表）。渲染进「视频设置」tab。
    实际出片在「生成 / 输出」tab（点分镜后去那里生成，日志/结果都在那）。
    """
    with st.container(border=True):
        st.subheader(tr("AI Storyboard Workspace"))

        # ---------- 第一步：主角/参数 + 生成分镜 ----------
        st.markdown("**" + tr("Step 1 Set Character and Params") + "**")
        col_ref, col_opt = st.columns([1, 1])
        with col_ref:
            img_types = ["jpg", "jpeg", "png", "webp", "bmp"]
            ai_ref = st.file_uploader(
                tr("Character Reference Image"),
                type=img_types + [t.upper() for t in img_types],
                accept_multiple_files=False,
            )
            if ai_ref is not None:
                ref_dir = utils.storage_dir("ai_reference", create=True)
                safe_name = os.path.basename(ai_ref.name).replace("/", "_").replace("\\", "_")
                ref_path = os.path.join(ref_dir, f"{ai_ref.file_id}_{safe_name}")
                with open(ref_path, "wb") as f:
                    f.write(ai_ref.getbuffer())
                st.session_state["ai_reference_image"] = ref_path
            if st.session_state.get("ai_reference_image") and os.path.exists(st.session_state["ai_reference_image"]):
                st.image(st.session_state["ai_reference_image"], caption=tr("Current Character Image"), width=160)
                if st.button(tr("Clear Character Image")):
                    st.session_state["ai_reference_image"] = ""
                    st.rerun()
        with col_opt:
            consistency_options = [
                (tr("Consistency None"), "none"),
                (tr("Consistency Reference Image"), "reference_image"),
                (tr("Consistency Frame Chain"), "frame_chain"),
            ]
            saved_mode = config.app.get("seedance_consistency_mode", "reference_image")
            cm_index = next((i for i, o in enumerate(consistency_options) if o[1] == saved_mode), 1)
            cm = st.selectbox(
                tr("Character Consistency"),
                options=range(len(consistency_options)),
                format_func=lambda x: consistency_options[x][0],
                index=cm_index,
            )
            config.app["seedance_consistency_mode"] = consistency_options[cm][1]

            res_options = ["480p", "720p", "1080p"]
            saved_res = config.app.get("seedance_resolution", "720p")
            res_index = res_options.index(saved_res) if saved_res in res_options else 1
            config.app["seedance_resolution"] = st.selectbox(
                tr("Resolution"), options=res_options, index=res_index
            )
            st.session_state["ai_character_desc"] = st.text_area(
                tr("Character Description"),
                value=st.session_state.get("ai_character_desc", ""),
                height=68,
            )

        if st.button(tr("Generate Storyboard"), use_container_width=True, type="primary", key="ai_gen_storyboard"):
            if not params.video_subject and not params.video_script:
                st.error(tr("Video Script and Subject Cannot Both Be Empty"))
                st.stop()
            params.seedance_reference_image = st.session_state.get("ai_reference_image", "")
            params.character_description = st.session_state.get("ai_character_desc", "")
            with st.spinner(tr("Generating Storyboard Spinner")):
                sb_task_id = str(uuid4())
                sb_result = tm.start(sb_task_id, params, stop_at="storyboard")
            if not sb_result or not sb_result.get("storyboard"):
                st.error(tr("Storyboard Generation Failed"))
            else:
                st.session_state["ai_storyboard"] = sb_result["storyboard"]
                if sb_result.get("character_description"):
                    st.session_state["ai_character_desc"] = sb_result["character_description"]
                st.success(tr("Storyboard Generated N Shots").format(n=len(sb_result["storyboard"])))
                st.rerun()

        # ---------- 第二步：编辑分镜表 ----------
        if st.session_state.get("ai_storyboard"):
            st.divider()
            st.markdown("**" + tr("Step 2 Edit Storyboard Table") + "**")
            edited = st.data_editor(
                st.session_state["ai_storyboard"],
                num_rows="dynamic",
                use_container_width=True,
                column_order=["index", "duration", "narration", "visual_prompt"],
                column_config={
                    "index": st.column_config.NumberColumn("#", disabled=True, width="small"),
                    "duration": st.column_config.NumberColumn(tr("Duration (s)"), min_value=4, max_value=15, step=1, width="small"),
                    "narration": st.column_config.TextColumn(tr("Narration"), width="medium"),
                    "visual_prompt": st.column_config.TextColumn(tr("Visual Prompt"), width="large"),
                },
                key="ai_storyboard_editor",
            )
            st.session_state["ai_storyboard"] = list(edited)
            st.success(tr("Storyboard ready, go to Output tab"))


def render_api_key_management():
    """视频素材图库(Pexels/Pixabay/Coverr) API Key 管理。并入「基础设置」内，
    因此不再自带 expander（Streamlit 不支持 expander 嵌套）。"""
    st.write("**" + tr("Manage Pexels, Pixabay and Coverr API Keys") + "**")
    col1, col2, col3 = st.tabs([
        tr("Pexels API Keys"),
        tr("Pixabay API Keys"),
        tr("Coverr API Keys"),
    ])
    providers = [
        (col1, "pexels_api_keys", "Pexels"),
        (col2, "pixabay_api_keys", "Pixabay"),
        (col3, "coverr_api_keys", "Coverr"),
    ]
    for col, cfg_key, label in providers:
        with col:
            # coverr_api_keys 是较新配置项，老 config 可能缺失，兜底为空列表。
            if not config.app.get(cfg_key):
                config.app[cfg_key] = []
            if config.app[cfg_key]:
                st.write(tr("Current Keys:"))
                for key in config.app[cfg_key]:
                    st.code(key)
            else:
                st.info(tr(f"No {label} API Keys currently"))

            new_key = st.text_input(tr(f"Add {label} API Key"), key=f"{label.lower()}_new_key", type="password")
            if st.button(tr(f"Add {label} API Key"), key=f"{label.lower()}_add_btn"):
                if new_key and new_key not in config.app[cfg_key]:
                    config.app[cfg_key].append(new_key)
                    config.save_config()
                    st.success(tr(f"{label} API Key added successfully"))
                elif new_key in config.app[cfg_key]:
                    st.warning(tr("This API Key already exists"))
                else:
                    st.error(tr("Please enter a valid API Key"))

            if config.app[cfg_key]:
                delete_key = st.selectbox(
                    tr(f"Select {label} API Key to delete"), config.app[cfg_key], key=f"{label.lower()}_delete_key"
                )
                if st.button(tr(f"Delete Selected {label} API Key"), key=f"{label.lower()}_del_btn"):
                    config.app[cfg_key].remove(delete_key)
                    config.save_config()
                    st.success(tr(f"{label} API Key deleted successfully"))


def run_generation(params, uploaded_audio_file, uploaded_files):
    """执行出片：校验 → 落盘上传素材 → tm.start → 实时日志 → 展示结果。
    渲染进「生成 / 输出」tab，日志与结果都在该 tab 内可见。"""
    config.save_config()
    task_id = str(uuid4())
    if not params.video_subject and not params.video_script:
        st.error(tr("Video Script and Subject Cannot Both Be Empty"))
        scroll_to_bottom()
        st.stop()

    if params.video_source not in ["pexels", "pixabay", "coverr", "local", "ai"]:
        st.error(tr("Please Select a Valid Video Source"))
        scroll_to_bottom()
        st.stop()

    if params.video_source == "ai":
        # AI 分镜管线：必须先生成分镜；把(编辑后的)分镜/主角图/主角设定塞进 params。
        sanitized = _sanitize_storyboard(st.session_state.get("ai_storyboard", []))
        if not sanitized:
            st.error(tr("Please generate storyboard first"))
            scroll_to_bottom()
            st.stop()
        params.video_storyboard = sanitized
        params.seedance_reference_image = st.session_state.get("ai_reference_image", "")
        params.character_description = st.session_state.get("ai_character_desc", "")

    if params.video_source == "pexels" and not config.app.get("pexels_api_keys", ""):
        st.error(tr("Please Enter the Pexels API Key"))
        scroll_to_bottom()
        st.stop()

    if params.video_source == "pixabay" and not config.app.get("pixabay_api_keys", ""):
        st.error(tr("Please Enter the Pixabay API Key"))
        scroll_to_bottom()
        st.stop()

    if params.video_source == "coverr" and not config.app.get("coverr_api_keys", ""):
        st.error(tr("Please Enter the Coverr API Key"))
        scroll_to_bottom()
        st.stop()

    if uploaded_audio_file:
        task_dir = utils.task_dir(task_id)
        _, audio_ext = os.path.splitext(os.path.basename(uploaded_audio_file.name))
        audio_ext = audio_ext.lower() or ".mp3"
        custom_audio_path = os.path.join(task_dir, f"custom-audio{audio_ext}")
        with open(custom_audio_path, "wb") as f:
            f.write(uploaded_audio_file.getbuffer())
        params.custom_audio_file = custom_audio_path

    if uploaded_files:
        local_videos_dir = utils.storage_dir("local_videos", create=True)
        params.video_materials = []
        persisted_local_materials = []
        for file in uploaded_files:
            file_path = os.path.join(local_videos_dir, f"{file.file_id}_{file.name}")
            with open(file_path, "wb") as f:
                f.write(file.getbuffer())
                m = MaterialInfo()
                m.provider = "local"
                m.url = file_path
                params.video_materials.append(m)
                persisted_local_materials.append(
                    {"provider": m.provider, "url": m.url, "duration": m.duration}
                )
        st.session_state["local_video_materials"] = persisted_local_materials
    elif params.video_source == "local" and st.session_state["local_video_materials"]:
        params.video_materials = []
        for material in st.session_state["local_video_materials"]:
            m = MaterialInfo()
            m.provider = material.get("provider", "local")
            m.url = material.get("url", "")
            m.duration = material.get("duration", 0)
            if m.url:
                params.video_materials.append(m)

    log_container = st.empty()
    log_records = []

    def log_received(msg):
        if config.ui["hide_log"]:
            return
        with log_container:
            log_records.append(msg)
            st.code("\n".join(log_records))

    logger.add(log_received)

    st.toast(tr("Generating Video"))
    logger.info(tr("Start Generating Video"))
    logger.info(utils.to_json(params))
    scroll_to_bottom()

    result = tm.start(task_id=task_id, params=params)
    if not result or "videos" not in result:
        st.error(tr("Video Generation Failed"))
        logger.error(tr("Video Generation Failed"))
        scroll_to_bottom()
        st.stop()

    video_files = result.get("videos", [])
    st.success(tr("Video Generation Completed"))
    try:
        if video_files:
            player_cols = st.columns(len(video_files) * 2 + 1)
            for i, url in enumerate(video_files):
                player_cols[i * 2 + 1].video(url)
    except Exception:
        pass

    open_task_folder(task_id)
    logger.info(tr("Video Generation Completed"))
    scroll_to_bottom()


# 加载语言文件
locales = utils.load_locales(i18n_dir)

# 创建一个顶部栏，包含标题和语言选择
title_col, lang_col = st.columns([3, 1])

with title_col:
    st.title(f"MoneyPrinterTurbo v{config.project_version}")

with lang_col:
    display_languages = []
    selected_index = 0
    for i, code in enumerate(locales.keys()):
        display_languages.append(f"{code} - {locales[code].get('Language')}")
        if code == st.session_state.get("ui_language", ""):
            selected_index = i

    selected_language = st.selectbox(
        "Language / 语言",
        options=display_languages,
        index=selected_index,
        key="top_language_selector",
        label_visibility="collapsed",
    )
    if selected_language:
        code = selected_language.split(" - ")[0].strip()
        st.session_state["ui_language"] = code
        config.ui["language"] = code

support_locales = [
    "zh-CN",
    "zh-HK",
    "zh-TW",
    "de-DE",
    "en-US",
    "fr-FR",
    "ru-RU",
    "vi-VN",
    "th-TH",
    "tr-TR",
]


def get_all_fonts():
    fonts = []
    for root, dirs, files in os.walk(font_dir):
        for file in files:
            if file.endswith(".ttf") or file.endswith(".ttc"):
                fonts.append(file)
    fonts.sort()
    return fonts


def get_all_songs():
    songs = []
    for root, dirs, files in os.walk(song_dir):
        for file in files:
            if file.endswith(".mp3"):
                songs.append(file)
    return songs


def open_task_folder(task_id):
    try:
        # task_id 应始终是服务端生成的 UUID。这里先做格式校验，避免异常值
        # 通过路径拼接访问任务目录之外的位置，也避免后续打开目录时触发
        # 平台 shell 对特殊字符的解释。
        normalized_task_id = str(UUID(str(task_id)))
        tasks_root = os.path.abspath(os.path.join(root_dir, "storage", "tasks"))
        path = os.path.abspath(os.path.join(tasks_root, normalized_task_id))

        # 即使 UUID 校验通过，也再次确认最终路径仍在任务根目录内，避免
        # 未来调用方调整 task_id 来源时引入路径穿越风险。
        if not path.startswith(tasks_root + os.sep):
            logger.warning(f"invalid task folder path: {path}")
            return

        if os.path.isdir(path):
            webbrowser.open(f"file://{path}")
    except Exception as e:
        logger.error(e)


def scroll_to_bottom():
    js = """
    <script>
        console.log("scroll_to_bottom");
        function scroll(dummy_var_to_force_repeat_execution){
            var sections = parent.document.querySelectorAll('section.main');
            console.log(sections);
            for(let index = 0; index<sections.length; index++) {
                sections[index].scrollTop = sections[index].scrollHeight;
            }
        }
        scroll(1);
    </script>
    """
    st.components.v1.html(js, height=0, width=0)


def request_nav(kind, label=""):
    """记录一次导航请求；实际动作在页面末尾 apply_pending_nav() 统一执行(需先 rerun)。
    kind="tab" 时按标签文字点击主 tab；kind="basic" 时展开顶部『基础设置』并滚到顶部。"""
    st.session_state["_pending_nav"] = {"kind": kind, "label": label}
    if kind == "basic":
        st.session_state["open_basic_settings"] = True
    st.rerun()


def apply_pending_nav():
    """在整页渲染完成后执行挂起的导航。放在脚本末尾，确保 tab 按钮已在 DOM 中。"""
    nav = st.session_state.pop("_pending_nav", None)
    if not nav:
        return
    if nav["kind"] == "tab":
        # 页面中存在嵌套 st.tabs(基础设置内的 API 管理 / 高级设置)，不能按索引点击，
        # 只能按主 tab 的可见标签文字匹配(这些标签唯一，不会与嵌套 tab 冲突)。
        target = json.dumps(nav["label"])
        js = f"""
        <script>
            const want = {target};
            const doc = window.parent.document;
            const tabs = Array.from(doc.querySelectorAll('button[role="tab"]'));
            const btn = tabs.find(t => t.innerText.trim() === want);
            if (btn) {{ btn.click(); }}
        </script>
        """
        st.components.v1.html(js, height=0, width=0)
    elif nav["kind"] == "basic":
        js = """
        <script>
            const doc = window.parent.document;
            const secs = doc.querySelectorAll('section.main');
            for (let i = 0; i < secs.length; i++) { secs[i].scrollTop = 0; }
        </script>
        """
        st.components.v1.html(js, height=0, width=0)


def init_log():
    logger.remove()
    _lvl = "DEBUG"

    def format_record(record):
        # 获取日志记录中的文件全路径
        file_path = record["file"].path
        # 将绝对路径转换为相对于项目根目录的路径
        relative_path = os.path.relpath(file_path, root_dir)
        # 更新记录中的文件路径
        record["file"].path = f"./{relative_path}"
        # 返回修改后的格式字符串
        # 您可以根据需要调整这里的格式
        record["message"] = record["message"].replace(root_dir, ".")

        _format = (
            "<green>{time:%Y-%m-%d %H:%M:%S}</> | "
            + "<level>{level}</> | "
            + '"{file.path}:{line}":<blue> {function}</> '
            + "- <level>{message}</>"
            + "\n"
        )
        return _format

    logger.add(
        sys.stdout,
        level=_lvl,
        format=format_record,
        colorize=True,
    )


init_log()

locales = utils.load_locales(i18n_dir)


def tr(key):
    loc = locales.get(st.session_state["ui_language"], {})
    return loc.get("Translation", {}).get(key, key)

@st.cache_data(ttl=300, show_spinner=False)
def get_groq_model_ids(api_key: str, base_url: str) -> list[str]:
    if not api_key:
        return []

    normalized_base_url = (base_url or "https://api.groq.com/openai/v1").strip().rstrip("/")
    models_url = f"{normalized_base_url}/models"

    try:
        response = requests.get(
            models_url,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=10,
        )
        response.raise_for_status()
        payload = response.json()
        data = payload.get("data", [])

        model_ids = []
        for item in data:
            if isinstance(item, dict):
                model_id = item.get("id")
                if isinstance(model_id, str) and model_id.strip():
                    model_ids.append(model_id.strip())

        return sorted(set(model_ids))
    except Exception as e:
        logger.warning(f"failed to fetch groq models: {e}")
        return []

# 创建基础设置折叠框
if not config.app.get("hide_config", False):
    with st.expander(tr("Basic Settings"), expanded=st.session_state.get("open_basic_settings", False)):
        config_panels = st.columns(3)
        left_config_panel = config_panels[0]
        middle_config_panel = config_panels[1]
        right_config_panel = config_panels[2]

        # 左侧面板 - 日志设置
        with left_config_panel:
            # 是否隐藏配置面板
            hide_config = st.checkbox(
                tr("Hide Basic Settings"), value=config.app.get("hide_config", False)
            )
            config.app["hide_config"] = hide_config

            # 是否禁用日志显示
            hide_log = st.checkbox(
                tr("Hide Log"), value=config.ui.get("hide_log", False)
            )
            config.ui["hide_log"] = hide_log

        # 中间面板 - LLM 设置

        with middle_config_panel:
            st.write(tr("LLM Settings"))
            # 下拉框展示文本和后端 provider id 分开维护，避免 UI 文案变化
            # 污染 `config.app["llm_provider"]` 这类稳定配置值。
            llm_provider_options = [
                ("OpenAI", "openai"),
                ("AIHubMix", "aihubmix"),
                ("AIML API", "aimlapi"),
                ("EvoLink", "evolink"),
                ("VolcEngine", "volcengine"),
                ("Moonshot", "moonshot"),
                ("Azure", "azure"),
                ("Qwen", "qwen"),
                ("DeepSeek", "deepseek"),
                ("ModelScope", "modelscope"),
                ("Gemini", "gemini"),
                ("Grok", "grok"),
                ("Groq", "groq"),
                ("Ollama", "ollama"),
                ("G4f", "g4f"),
                ("OneAPI", "oneapi"),
                ("Cloudflare", "cloudflare"),
                ("ERNIE", "ernie"),
                ("MiniMax", "minimax"),
                ("MiMo", "mimo"),
                ("Pollinations", "pollinations"),
                ("LiteLLM", "litellm"),
            ]
            llm_provider_ids = [provider_id for _, provider_id in llm_provider_options]
            llm_provider_labels = {
                provider_id: label for label, provider_id in llm_provider_options
            }
            saved_llm_provider = config.app.get("llm_provider", "openai").lower()
            if saved_llm_provider not in llm_provider_ids:
                saved_llm_provider = "openai"

            # Streamlit 会把没有 key 的 selectbox 视为一个由 label/options/index
            # 共同决定的临时控件。如果每次选择后都根据 config.app 重新计算 index，
            # 用户第一次切换 provider 后控件可能被重建，表现为“必须选择两次才生效”。
            # 这里用稳定的 provider id 作为真实选项，并给控件固定 key；展示文案只
            # 通过 format_func 转换，避免 UI 文案变化影响状态。
            if st.session_state.get("llm_provider_select") not in (
                None,
                *llm_provider_ids,
            ):
                del st.session_state["llm_provider_select"]

            llm_provider = st.selectbox(
                tr("LLM Provider"),
                options=llm_provider_ids,
                index=llm_provider_ids.index(saved_llm_provider),
                format_func=lambda provider_id: llm_provider_labels[provider_id],
                key="llm_provider_select",
            )
            llm_helper = st.container()
            config.app["llm_provider"] = llm_provider

            llm_api_key = config.app.get(f"{llm_provider}_api_key", "")
            llm_secret_key = config.app.get(
                f"{llm_provider}_secret_key", ""
            )  # only for baidu ernie
            llm_base_url = config.app.get(f"{llm_provider}_base_url", "")
            llm_model_name = config.app.get(f"{llm_provider}_model_name", "")
            llm_account_id = config.app.get(f"{llm_provider}_account_id", "")

            tips = ""
            if llm_provider == "ollama":
                if not llm_model_name:
                    llm_model_name = "qwen:7b"
                if not llm_base_url:
                    llm_base_url = config.get_default_ollama_base_url()

                with llm_helper:
                    docker_hint = ""
                    if config.is_running_in_container():
                        docker_hint = "\n                            > 检测到容器环境，未配置 Base Url 时会默认使用 `http://host.docker.internal:11434/v1`\n"
                    tips = f"""
                            ##### Ollama配置说明
                            - **API Key**: 随便填写，比如 123
                            - **Base Url**: 一般为 http://localhost:11434/v1
                                - 如果 `MoneyPrinterTurbo` 和 `Ollama` **不在同一台机器上**，需要填写 `Ollama` 机器的IP地址
                                - 如果 `MoneyPrinterTurbo` 是 `Docker` 部署，建议填写 `http://host.docker.internal:11434/v1`{docker_hint}
                            - **Model Name**: 使用 `ollama list` 查看，比如 `qwen:7b`
                            """

            if llm_provider == "openai":
                if not llm_model_name:
                    llm_model_name = "gpt-3.5-turbo"
                with llm_helper:
                    tips = """
                            ##### OpenAI 配置说明
                            > 需要VPN开启全局流量模式
                            - **API Key**: [点击到官网申请](https://platform.openai.com/api-keys)
                            - **Base Url**: 官方 OpenAI 可留空；如果使用 OpenAI 兼容供应商（例如 OpenRouter），请填写对应的兼容接口地址
                            - **Model Name**: 填写**有权限**的模型；如果使用兼容供应商，请填写该平台支持的模型 ID
                            """

            if llm_provider == "aihubmix":
                if not llm_model_name:
                    llm_model_name = "gpt-5.4-mini"
                if not llm_base_url:
                    llm_base_url = "https://aihubmix.com/v1"
                with llm_helper:
                    tips = """
                            ##### AIHubMix 配置说明
                            - **API Key**: 在 AIHubMix 控制台创建 API Key
                            - **Base Url**: 预填 https://aihubmix.com/v1
                            - **Model Name**: 默认 gpt-5.4-mini，也可以填写 AIHubMix 支持的其它模型 ID
                            """

            if llm_provider == "aimlapi":
                if not llm_model_name:
                    llm_model_name = "openai/gpt-4o-mini"
                if not llm_base_url:
                    llm_base_url = "https://api.aimlapi.com/v1"
                with llm_helper:
                    tips = """
                            ##### AIML API Configuration
                            - **API Key**: create one at https://aimlapi.com/app/keys
                            - **Base Url**: https://api.aimlapi.com/v1
                            - **Model Name**: for example `openai/gpt-4o-mini`, `openai/gpt-4o`, `anthropic/claude-sonnet-4.5`, or `google/gemini-3-flash-preview`
                            """

            if llm_provider == "evolink":
                if not llm_model_name:
                    llm_model_name = "gpt-5.5"
                if not llm_base_url:
                    llm_base_url = "https://direct.evolink.ai/v1"
                with llm_helper:
                    tips = """
                            ##### EvoLink 配置说明
                            - **API Key**: [点击到官网申请](https://evolink.ai/dashboard/keys)
                            - **Base Url**: 默认 https://direct.evolink.ai/v1
                            - **Model Name**: 默认 gpt-5.5，也可以填写 EvoLink 支持的其它模型 ID
                            """

            if llm_provider == "volcengine":
                if not llm_model_name:
                    llm_model_name = "doubao-seed-2-1-turbo-260628"
                if not llm_base_url:
                    llm_base_url = "https://ark.cn-beijing.volces.com/api/v3"
                with llm_helper:
                    tips = """
                            ##### VolcEngine Ark 配置说明
                            - **注册链接**: [点击注册 火山引擎](https://www.volcengine.com/activity/ai618?utm_campaign=hw&utm_content=hw&utm_medium=devrel_tool_web&utm_source=OWO&utm_term=MoneyPrinterTurbo)
                            - **API Key**: 在火山引擎方舟控制台创建 API Key
                            - **Base Url**: 默认 https://ark.cn-beijing.volces.com/api/v3
                            - **Model Name**: 填写 Ark 控制台已开通的模型 ID，例如 doubao-seed-2-1-turbo-260628
                            """

            if llm_provider == "moonshot":
                if not llm_model_name:
                    llm_model_name = "moonshot-v1-8k"
                with llm_helper:
                    tips = """
                            ##### Moonshot 配置说明
                            - **API Key**: [点击到官网申请](https://platform.moonshot.cn/console/api-keys)
                            - **Base Url**: 固定为 https://api.moonshot.cn/v1
                            - **Model Name**: 比如 moonshot-v1-8k，[点击查看模型列表](https://platform.moonshot.cn/docs/intro#%E6%A8%A1%E5%9E%8B%E5%88%97%E8%A1%A8)
                            """
            if llm_provider == "oneapi":
                if not llm_model_name:
                    llm_model_name = (
                        "claude-3-5-sonnet-20240620"  # 默认模型，可以根据需要调整
                    )
                with llm_helper:
                    tips = """
                        ##### OneAPI 配置说明
                        - **API Key**: 填写您的 OneAPI 密钥
                        - **Base Url**: 填写 OneAPI 的基础 URL
                        - **Model Name**: 填写您要使用的模型名称，例如 claude-3-5-sonnet-20240620
                        """

            if llm_provider == "qwen":
                if not llm_model_name:
                    llm_model_name = "qwen-max"
                with llm_helper:
                    tips = """
                            ##### 通义千问Qwen 配置说明
                            - **API Key**: [点击到官网申请](https://dashscope.console.aliyun.com/apiKey)
                            - **Base Url**: 留空
                            - **Model Name**: 比如 qwen-max，[点击查看模型列表](https://help.aliyun.com/zh/dashscope/developer-reference/model-introduction#3ef6d0bcf91wy)
                            """

            if llm_provider == "g4f":
                if not llm_model_name:
                    llm_model_name = "gpt-3.5-turbo"
                with llm_helper:
                    tips = """
                            ##### gpt4free 配置说明
                            > [GitHub开源项目](https://github.com/xtekky/gpt4free)，可以免费使用GPT模型，但是**稳定性较差**
                            - **API Key**: 随便填写，比如 123
                            - **Base Url**: 留空
                            - **Model Name**: 比如 gpt-3.5-turbo，[点击查看模型列表](https://github.com/xtekky/gpt4free/blob/main/g4f/models.py#L308)
                            """
            if llm_provider == "azure":
                with llm_helper:
                    tips = """
                            ##### Azure 配置说明
                            > [点击查看如何部署模型](https://learn.microsoft.com/zh-cn/azure/ai-services/openai/how-to/create-resource)
                            - **API Key**: [点击到Azure后台创建](https://portal.azure.com/#view/Microsoft_Azure_ProjectOxford/CognitiveServicesHub/~/OpenAI)
                            - **Base Url**: 留空
                            - **Model Name**: 填写你实际的部署名
                            """

            if llm_provider == "gemini":
                if not llm_model_name:
                    llm_model_name = "gemini-1.0-pro"

                with llm_helper:
                    tips = """
                            ##### Gemini 配置说明
                            > 需要VPN开启全局流量模式
                            - **API Key**: [点击到官网申请](https://ai.google.dev/)
                            - **Base Url**: 留空
                            - **Model Name**: 比如 gemini-1.0-pro
                            """

            if llm_provider == "grok":
                if not llm_model_name:
                    llm_model_name = "grok-4.3"
                if not llm_base_url:
                    llm_base_url = "https://api.x.ai/v1"

                with llm_helper:
                    tips = """
                            ##### Grok 配置说明
                            - **API Key**: 填写您的 GrokAPI 密钥
                            - **Base Url**: 填写 GrokAPI 的基础 URL
                            - **Model Name**: 比如 grok-4.3
                            """

            if llm_provider == "groq":
                if not llm_model_name:
                    llm_model_name = "llama-3.3-70b-versatile"
                if not llm_base_url:
                    llm_base_url = "https://api.groq.com/openai/v1"

                with llm_helper:
                    tips = """
                            ##### Groq 配置说明
                            - **API Key**: [点击到官网申请](https://console.groq.com/keys)
                            - **Base Url**: 固定为 https://api.groq.com/openai/v1
                            - **Model Name**: 比如 llama-3.3-70b-versatile
                            """

            if llm_provider == "deepseek":
                if not llm_model_name:
                    llm_model_name = "deepseek-chat"
                if not llm_base_url:
                    llm_base_url = "https://api.deepseek.com"
                with llm_helper:
                    tips = """
                            ##### DeepSeek 配置说明
                            - **API Key**: [点击到官网申请](https://platform.deepseek.com/api_keys)
                            - **Base Url**: 固定为 https://api.deepseek.com
                            - **Model Name**: 固定为 deepseek-chat
                            """

            if llm_provider == "mimo":
                if not llm_model_name:
                    llm_model_name = "mimo-v2.5-pro"
                if not llm_base_url:
                    llm_base_url = "https://api.xiaomimimo.com/v1"
                with llm_helper:
                    tips = """
                            ##### Xiaomi MiMo 配置说明
                            - **API Key**: [点击到官网申请](https://platform.xiaomimimo.com/docs/zh-CN/quick-start/first-api-call)
                            - **Base Url**: 固定为 https://api.xiaomimimo.com/v1
                            - **Model Name**: 默认 mimo-v2.5-pro，也可以按官方文档填写其它可用模型
                            """

            if llm_provider == "modelscope":
                if not llm_model_name:
                    llm_model_name = "Qwen/Qwen3-32B"
                if not llm_base_url:
                    llm_base_url = "https://api-inference.modelscope.cn/v1/"
                with llm_helper:
                    tips = """
                            ##### ModelScope 配置说明
                            - **API Key**: [点击到官网申请](https://modelscope.cn/docs/model-service/API-Inference/intro)
                            - **Base Url**: 固定为 https://api-inference.modelscope.cn/v1/
                            - **Model Name**: 比如 Qwen/Qwen3-32B，[点击查看模型列表](https://modelscope.cn/models?filter=inference_type&page=1)
                            """

            if llm_provider == "ernie":
                with llm_helper:
                    tips = """
                            ##### 百度文心一言 配置说明
                            - **API Key**: [点击到官网申请](https://console.bce.baidu.com/qianfan/ais/console/applicationConsole/application)
                            - **Secret Key**: [点击到官网申请](https://console.bce.baidu.com/qianfan/ais/console/applicationConsole/application)
                            - **Base Url**: 填写 **请求地址** [点击查看文档](https://cloud.baidu.com/doc/WENXINWORKSHOP/s/jlil56u11#%E8%AF%B7%E6%B1%82%E8%AF%B4%E6%98%8E)
                            """

            if llm_provider == "pollinations":
                if not llm_model_name:
                    llm_model_name = "default"
                with llm_helper:
                    tips = """
                            ##### Pollinations AI Configuration
                            - **API Key**: Optional - Leave empty for public access
                            - **Base Url**: Default is https://text.pollinations.ai/openai
                            - **Model Name**: Use 'openai-fast' or specify a model name
                            """

            if llm_provider == "litellm":
                if not llm_model_name:
                    llm_model_name = "openai/gpt-4o-mini"
                with llm_helper:
                    tips = """
                            ##### LiteLLM Configuration
                            > [LiteLLM](https://github.com/BerriAI/litellm) routes to 100+ LLM providers via a unified interface.
                            > Set your provider's API key as an env var: `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `GEMINI_API_KEY`, `AWS_ACCESS_KEY_ID`, etc.
                            - **Model Name**: LiteLLM format — `openai/gpt-4o`, `anthropic/claude-sonnet-4-20250514`, `bedrock/anthropic.claude-3-5-sonnet-20241022-v2:0`, `gemini/gemini-2.5-flash`. See [full provider list](https://docs.litellm.ai/docs/providers)
                            """

            if tips and config.ui["language"] == "zh":
                st.info(tips)

            st_llm_api_key = st.text_input(
                tr("API Key"), value=llm_api_key, type="password"
            )
            st_llm_base_url = st.text_input(tr("Base Url"), value=llm_base_url)
            st_llm_model_name = ""
            if llm_provider != "ernie":
                if llm_provider == "groq":
                    effective_api_key = st_llm_api_key or llm_api_key
                    effective_base_url = st_llm_base_url or llm_base_url
                    groq_models = get_groq_model_ids(
                        api_key=effective_api_key,
                        base_url=effective_base_url,
                    )

                    if groq_models:
                        selected_index = 0
                        if llm_model_name in groq_models:
                            selected_index = groq_models.index(llm_model_name)

                        st_llm_model_name = st.selectbox(
                            tr("Model Name"),
                            options=groq_models,
                            index=selected_index,
                            key="groq_model_name_select",
                        )
                    else:
                        st_llm_model_name = st.text_input(
                            tr("Model Name"),
                            value=llm_model_name,
                            key="groq_model_name_input",
                        )
                        if effective_api_key:
                            st.caption(
                                "Unable to load Groq model list right now. You can still enter a model name manually — note it won't be validated until generation."
                            )
                        else:
                            st.caption(
                                "Add a Groq API key to load available models automatically."
                            )
                else:
                    st_llm_model_name = st.text_input(
                        tr("Model Name"),
                        value=llm_model_name,
                        key=f"{llm_provider}_model_name_input",
                    )
                if st_llm_model_name:
                    config.app[f"{llm_provider}_model_name"] = st_llm_model_name
            else:
                st_llm_model_name = None

            if st_llm_api_key:
                config.app[f"{llm_provider}_api_key"] = st_llm_api_key
            if st_llm_base_url:
                config.app[f"{llm_provider}_base_url"] = st_llm_base_url
            if st_llm_model_name:
                config.app[f"{llm_provider}_model_name"] = st_llm_model_name
            if llm_provider == "ernie":
                st_llm_secret_key = st.text_input(
                    tr("Secret Key"), value=llm_secret_key, type="password"
                )
                config.app[f"{llm_provider}_secret_key"] = st_llm_secret_key

            if llm_provider == "cloudflare":
                st_llm_account_id = st.text_input(
                    tr("Account ID"), value=llm_account_id
                )
                if st_llm_account_id:
                    config.app[f"{llm_provider}_account_id"] = st_llm_account_id

        # 右侧面板 - 视频源 API 密钥
        with right_config_panel:
            st.write(tr("Video Source Settings"))

            # AI 生成 (Seedance / 火山方舟) 的 API Key。单个 Bearer key，
            # 与图库 key 一起在此管理。留空时后端回退到 volcengine_api_key。
            seedance_key = config.app.get("seedance_api_key", "") or ""
            seedance_key_new = st.text_input(
                tr("Seedance API Key"),
                value=seedance_key,
                type="password",
                key="seedance_api_key_input",
                help=tr("Seedance API Key Help"),
            )
            if seedance_key_new.strip() != seedance_key:
                config.app["seedance_api_key"] = seedance_key_new.strip()
                config.save_config()

            st.divider()
            # 图库素材 API 管理（Pexels/Pixabay/Coverr 增删列表）
            render_api_key_management()

llm_provider = config.app.get("llm_provider", "").lower()
# 设置项较多，改用 tab 分组更清爽：文案 / 视频 / 音频·字幕。
# 保留 left/middle/right_panel 变量名，使下方三个 with 块无需改动。
tab_script, tab_video, tab_audio, tab_output = st.tabs(
    [tr("Tab Script"), tr("Tab Video"), tr("Tab Audio Subtitle"), tr("Tab Output")]
)
left_panel = tab_script
middle_panel = tab_video
right_panel = tab_audio

params = VideoParams(video_subject="")
params.match_materials_to_script = bool(
    st.session_state.get("match_materials_to_script", False)
)
uploaded_files = []
uploaded_audio_file = None
# video_source 在中面板才选择(渲染在左面板之后)；这里先读已保存值，用于在
# 左面板提前隐藏 AI 途径用不到的关键词控件。中面板检测到选择变化会立即 st.rerun()，
# 保证本轮左面板与新 source 一致(不会出现要切两次才生效)。
is_ai_saved = config.app.get("video_source") == "ai"

with left_panel:
    with st.container(border=True):
        st.write(tr("Video Script Settings"))
        params.video_subject = st.text_input(
            tr("Video Subject"),
            key="video_subject",
        ).strip()

        video_languages = [
            (tr("Auto Detect"), ""),
        ]
        for code in support_locales:
            video_languages.append((code, code))

        selected_index = st.selectbox(
            tr("Script Language"),
            index=0,
            options=range(
                len(video_languages)
            ),  # Use the index as the internal option value
            format_func=lambda x: video_languages[x][
                0
            ],  # The label is displayed to the user
        )
        params.video_language = video_languages[selected_index][1]

        with st.expander(tr("Advanced Script Settings"), expanded=False):
            params.paragraph_number = st.slider(
                tr("Script Paragraph Number"),
                min_value=llm.MIN_SCRIPT_PARAGRAPH_NUMBER,
                max_value=llm.MAX_SCRIPT_PARAGRAPH_NUMBER,
                value=st.session_state.get("paragraph_number_input", 1),
                key="paragraph_number_input",
            )
            params.video_script_prompt = st.text_area(
                tr("Custom Script Requirements"),
                height=100,
                max_chars=llm.MAX_SCRIPT_PROMPT_LENGTH,
                placeholder=tr("Custom Script Requirements Placeholder"),
                key="video_script_prompt",
            ).strip()

            use_custom_system_prompt = st.checkbox(
                tr("Use Custom System Prompt"),
                help=tr("Use Custom System Prompt Help"),
                key="use_custom_system_prompt",
            )

            if use_custom_system_prompt:
                custom_system_prompt = st.text_area(
                    tr("Custom System Prompt"),
                    height=240,
                    max_chars=llm.MAX_SCRIPT_SYSTEM_PROMPT_LENGTH,
                    key="custom_system_prompt",
                ).strip()
                params.custom_system_prompt = custom_system_prompt
            else:
                params.custom_system_prompt = ""

        # 生成脚本按钮：AI 途径只生成文案，不生成关键词(关键词仅用于图库素材检索)。
        script_btn_label = tr("Generate Video Script") if is_ai_saved else tr("Generate Video Script and Keywords")
        if st.button(script_btn_label, key="auto_generate_script"):
            with st.spinner(tr("Generating Video Script and Keywords")):
                script = llm.generate_script(
                    video_subject=params.video_subject,
                    language=params.video_language,
                    paragraph_number=params.paragraph_number,
                    video_script_prompt=params.video_script_prompt,
                    custom_system_prompt=params.custom_system_prompt,
                )
                if "Error: " in script:
                    st.error(tr(script))
                else:
                    st.session_state["video_script"] = script
                    if not is_ai_saved:
                        terms = llm.generate_terms(
                            params.video_subject,
                            script,
                            amount=8 if params.match_materials_to_script else 5,
                            match_script_order=params.match_materials_to_script,
                        )
                        if "Error: " in terms:
                            st.error(tr(terms))
                        else:
                            st.session_state["video_terms"] = ", ".join(terms)
        params.video_script = st.text_area(
            tr("Video Script"), value=st.session_state["video_script"], height=280
        )
        # 关键词按钮 + 关键词框：仅图库素材检索途径需要，AI 途径隐藏。
        if not is_ai_saved:
            if st.button(tr("Generate Video Keywords"), key="auto_generate_terms"):
                if not params.video_script:
                    st.error(tr("Please Enter the Video Subject"))
                    st.stop()

                with st.spinner(tr("Generating Video Keywords")):
                    terms = llm.generate_terms(
                        params.video_subject,
                        params.video_script,
                        amount=8 if params.match_materials_to_script else 5,
                        match_script_order=params.match_materials_to_script,
                    )
                    if "Error: " in terms:
                        st.error(tr(terms))
                    else:
                        st.session_state["video_terms"] = ", ".join(terms)

            params.video_terms = st.text_area(
                tr("Video Keywords"), value=st.session_state["video_terms"]
            )

with middle_panel:
    with st.container(border=True):
        st.write(tr("Video Settings"))
        video_concat_modes = [
            (tr("Sequential"), "sequential"),
            (tr("Random"), "random"),
        ]
        video_sources = [
            (tr("Pexels"), "pexels"),
            (tr("Pixabay"), "pixabay"),
            (tr("Coverr"), "coverr"),
            (tr("Local file"), "local"),
            (tr("AI Generation (Seedance)"), "ai"),
            (tr("TikTok"), "douyin"),
            (tr("Bilibili"), "bilibili"),
            (tr("Xiaohongshu"), "xiaohongshu"),
        ]

        saved_video_source_name = config.app.get("video_source", "pexels")
        saved_video_source_index = [v[1] for v in video_sources].index(
            saved_video_source_name
        )

        selected_index = st.selectbox(
            tr("Video Source"),
            options=range(len(video_sources)),
            format_func=lambda x: video_sources[x][0],
            index=saved_video_source_index,
        )
        params.video_source = video_sources[selected_index][1]
        config.app["video_source"] = params.video_source
        # 顶部的 is_ai_saved 在本轮之初读取的是旧的 video_source，用于左面板提前隐藏
        # AI 途径无关控件。若本轮选择发生了变化，左面板已按旧值渲染完毕，必须立刻
        # rerun 一次，让整页用新值从头渲染，否则要切两次才生效。
        if params.video_source != saved_video_source_name:
            st.rerun()
        # AI 途径不走图库检索/拼接那套，以下与素材检索相关的控件对它无意义，需隐藏。
        is_ai = params.video_source == "ai"

        if params.video_source == "local":
            # Streamlit 的文件类型校验对扩展名大小写敏感，这里同时放行大小写两种形式。
            local_file_types = ["mp4", "mov", "avi", "flv", "mkv", "jpg", "jpeg", "png"]
            uploaded_files = st.file_uploader(
                tr("Upload Local Files"),
                type=local_file_types + [file_type.upper() for file_type in local_file_types],
                accept_multiple_files=True,
            )

        # 拼接模式 / 转场模式：仅图库/本地素材途径需要；AI 按分镜顺序拼接，隐藏。
        if not is_ai:
            selected_index = st.selectbox(
                tr("Video Concat Mode"),
                index=1,
                options=range(
                    len(video_concat_modes)
                ),  # Use the index as the internal option value
                format_func=lambda x: video_concat_modes[x][
                    0
                ],  # The label is displayed to the user
            )
            params.video_concat_mode = VideoConcatMode(
                video_concat_modes[selected_index][1]
            )

            # 视频转场模式
            video_transition_modes = [
                (tr("None"), VideoTransitionMode.none.value),
                (tr("Shuffle"), VideoTransitionMode.shuffle.value),
                (tr("FadeIn"), VideoTransitionMode.fade_in.value),
                (tr("FadeOut"), VideoTransitionMode.fade_out.value),
                (tr("SlideIn"), VideoTransitionMode.slide_in.value),
                (tr("SlideOut"), VideoTransitionMode.slide_out.value),
            ]
            selected_index = st.selectbox(
                tr("Video Transition Mode"),
                options=range(len(video_transition_modes)),
                format_func=lambda x: video_transition_modes[x][0],
                index=0,
            )
            params.video_transition_mode = VideoTransitionMode(
                video_transition_modes[selected_index][1]
            )

        video_aspect_ratios = [
            (tr("Portrait"), VideoAspect.portrait.value),
            (tr("Landscape"), VideoAspect.landscape.value),
        ]
        # Coverr 库 99% 是 16:9 横屏,默认竖屏会让画面被大量黑边包围。
        # 用 source-specific widget key 让每个 source 各自记忆 aspect 选择:
        #   - 首次切到 coverr → 默认 Landscape(index=1)
        #   - 其他 source 沿用 Portrait(index=0)
        #   - 用户在某 source 下手动改过 aspect,session_state 会记住,
        #     下次回到同一 source 时尊重用户选择,不会再被强制覆盖。
        default_aspect_index = 1 if params.video_source == "coverr" else 0
        selected_index = st.selectbox(
            tr("Video Ratio"),
            options=range(
                len(video_aspect_ratios)
            ),  # Use the index as the internal option value
            format_func=lambda x: video_aspect_ratios[x][
                0
            ],  # The label is displayed to the user
            index=default_aspect_index,
            key=f"video_aspect_for_{params.video_source}",
        )
        params.video_aspect = VideoAspect(video_aspect_ratios[selected_index][1])

        # 片段时长 / 同时生成数量 / 素材顺序匹配：均为图库素材途径概念；
        # AI 途径时长由分镜决定、单条输出、无素材检索，故隐藏。
        if not is_ai:
            params.video_clip_duration = st.selectbox(
                tr("Clip Duration"), options=[2, 3, 4, 5, 6, 7, 8, 9, 10], index=1
            )
            params.video_count = st.selectbox(
                tr("Number of Videos Generated Simultaneously"),
                options=[1, 2, 3, 4, 5],
                index=0,
            )

            with st.expander(tr("Advanced Video Settings"), expanded=False):
                # 默认关闭，避免影响老用户的随机素材体验。开启后只改变关键词和素材
                # 下载/拼接顺序，用于改善画面主题早于或晚于旁白的问题。
                params.match_materials_to_script = st.checkbox(
                    tr("Match Materials to Script Order"),
                    help=tr("Match Materials to Script Order Help"),
                    key="match_materials_to_script",
                )
            config.app["match_materials_to_script"] = params.match_materials_to_script

            video_codec_options = [
                ("libx264 (CPU)", "libx264"),
                ("NVIDIA NVENC (h264_nvenc)", "h264_nvenc"),
                ("AMD AMF (h264_amf)", "h264_amf"),
                ("Intel QSV (h264_qsv)", "h264_qsv"),
                ("Windows MediaFoundation (h264_mf)", "h264_mf"),
                ("macOS VideoToolbox (h264_videotoolbox)", "h264_videotoolbox"),
            ]
            saved_video_codec = config.app.get("video_codec", "libx264")
            saved_video_codec_values = [item[1] for item in video_codec_options]
            if saved_video_codec not in saved_video_codec_values:
                saved_video_codec = "libx264"
            selected_codec_index = saved_video_codec_values.index(saved_video_codec)
            selected_codec_index = st.selectbox(
                tr("Video Encoder"),
                options=range(len(video_codec_options)),
                index=selected_codec_index,
                format_func=lambda x: video_codec_options[x][0],
                help=tr("Video Encoder Help"),
            )
            config.app["video_codec"] = video_codec_options[selected_codec_index][1]

    # AI 分镜工作台：并入「视频设置」tab（①生成分镜 + 编辑分镜表）；实际出片在「生成/输出」tab。
    if params.video_source == "ai":
        render_ai_workspace(params)

# 音频设置移入「音频 / 字幕」tab（原本与视频同列，tab 化后归到音频·字幕 tab）
with right_panel:
    with st.container(border=True):
        st.write(tr("Audio Settings"))

        # 添加TTS服务器选择下拉框
        tts_servers = [
            (voice.NO_VOICE_NAME, tr("No Voice")),
            ("azure-tts-v1", "Azure TTS V1"),
            ("azure-tts-v2", "Azure TTS V2"),
            ("siliconflow", "SiliconFlow TTS"),
            ("gemini-tts", "Google Gemini TTS"),
            ("mimo-tts", "Xiaomi MiMo TTS"),
            ("elevenlabs", "ElevenLabs TTS"),
            ("chatterbox", "Chatterbox TTS"),
        ]

        # 获取保存的TTS服务器，默认为v1
        saved_tts_server = config.ui.get("tts_server", "azure-tts-v1")
        saved_tts_server_index = 0
        for i, (server_value, _) in enumerate(tts_servers):
            if server_value == saved_tts_server:
                saved_tts_server_index = i
                break

        selected_tts_server_index = st.selectbox(
            tr("TTS Servers"),
            options=range(len(tts_servers)),
            format_func=lambda x: tts_servers[x][1],
            index=saved_tts_server_index,
        )

        selected_tts_server = tts_servers[selected_tts_server_index][0]
        config.ui["tts_server"] = selected_tts_server

        # 根据选择的TTS服务器获取声音列表
        filtered_voices = []

        if selected_tts_server == voice.NO_VOICE_NAME:
            # 无配音是显式模式，只提供一个稳定 sentinel。这样普通 TTS 的空配置
            # 不会被误判为静音，后端也能继续通过同一条音频/字幕流程生成视频。
            filtered_voices = [voice.NO_VOICE_NAME]
        elif selected_tts_server == "siliconflow":
            # 获取硅基流动的声音列表
            filtered_voices = voice.get_siliconflow_voices()
        elif selected_tts_server == "gemini-tts":
            # 获取Gemini TTS的声音列表
            filtered_voices = voice.get_gemini_voices()
        elif selected_tts_server == "mimo-tts":
            # 获取 Xiaomi MiMo TTS 的预置音色列表
            filtered_voices = voice.get_mimo_voices()
        elif selected_tts_server == "elevenlabs":
            # Read from session_state first so the API key is available before
            # the Play Voice button runs (which is earlier in the script than
            # the API key text_input widget).
            saved_elevenlabs_api_key = st.session_state.get(
                "elevenlabs_api_key_input",
                config.elevenlabs.get("api_key", ""),
            )
            if saved_elevenlabs_api_key:
                config.elevenlabs["api_key"] = saved_elevenlabs_api_key
            cache_key = f"elevenlabs_voices_{saved_elevenlabs_api_key}"
            if cache_key not in st.session_state:
                st.session_state[cache_key] = voice.get_elevenlabs_voices(
                    saved_elevenlabs_api_key
                )
            filtered_voices = st.session_state[cache_key]
        elif selected_tts_server == "chatterbox":
            # 自托管 Chatterbox 服务的预置音色（来自 [chatterbox] voices 配置）
            _sync_chatterbox_config_from_session_state()
            filtered_voices = voice.get_chatterbox_voices()
        else:
            # 获取Azure的声音列表
            all_voices = voice.get_all_azure_voices(filter_locals=None)

            # 根据选择的TTS服务器筛选声音
            for v in all_voices:
                if selected_tts_server == "azure-tts-v2":
                    # V2版本的声音名称中包含"v2"
                    if "V2" in v:
                        filtered_voices.append(v)
                else:
                    # V1版本的声音名称中不包含"v2"
                    if "V2" not in v:
                        filtered_voices.append(v)

        if selected_tts_server == voice.NO_VOICE_NAME:
            friendly_names = {voice.NO_VOICE_NAME: tr("No Voice")}
        else:
            def _friendly(v):
                if voice.is_elevenlabs_voice(v):
                    parts = v.split(":", 2)
                    return parts[2] if len(parts) >= 3 else v
                if voice.is_chatterbox_voice(v):
                    name = v.split(":", 1)[1] if ":" in v else v
                    return name.replace("-Female", "").replace("-Male", "")
                return (
                    v.replace("Female", tr("Female"))
                    .replace("Male", tr("Male"))
                    .replace("Neural", "")
                )
            friendly_names = {v: _friendly(v) for v in filtered_voices}

        saved_voice_name = config.ui.get("voice_name", "")
        saved_voice_name_index = 0

        # 检查保存的声音是否在当前筛选的声音列表中
        if saved_voice_name in friendly_names:
            saved_voice_name_index = list(friendly_names.keys()).index(saved_voice_name)
        else:
            # 如果不在，则根据当前UI语言选择一个默认声音
            for i, v in enumerate(filtered_voices):
                if v.lower().startswith(st.session_state["ui_language"].lower()):
                    saved_voice_name_index = i
                    break

        # 如果没有找到匹配的声音，使用第一个声音
        if saved_voice_name_index >= len(friendly_names) and friendly_names:
            saved_voice_name_index = 0

        # 确保有声音可选
        if friendly_names:
            selected_friendly_name = st.selectbox(
                tr("Speech Synthesis"),
                options=list(friendly_names.values()),
                index=min(saved_voice_name_index, len(friendly_names) - 1)
                if friendly_names
                else 0,
            )

            voice_name = list(friendly_names.keys())[
                list(friendly_names.values()).index(selected_friendly_name)
            ]
            params.voice_name = voice_name
            config.ui["voice_name"] = voice_name
        else:
            # 如果没有声音可选，显示提示信息
            st.warning(
                tr(
                    "No voices available for the selected TTS server. Please select another server."
                )
            )
            voice_name = ""
            params.voice_name = ""
            config.ui["voice_name"] = ""

        # 无配音模式会生成静音占位音频，不展示试听按钮，避免用户误以为需要测试声音。
        if (
            friendly_names
            and selected_tts_server != voice.NO_VOICE_NAME
            and st.button(tr("Play Voice"))
        ):
            if selected_tts_server == "chatterbox":
                _sync_chatterbox_config_from_session_state()
            play_content = params.video_subject
            if not play_content:
                play_content = params.video_script
            if not play_content:
                # For ElevenLabs voices, detect language from the display name
                # so the test text matches the voice's language.
                if voice.is_elevenlabs_voice(voice_name):
                    parts = voice_name.split(":", 2)
                    display = parts[2] if len(parts) >= 3 else ""
                    _vi_chars = set("àáâãèéêìíòóôõùúýăđơưÀÁÂÃÈÉÊÌÍÒÓÔÕÙÚÝĂĐƠƯ")
                    if any(c in _vi_chars for c in display):
                        play_content = "Xin chào, đây là đoạn âm thanh thử nghiệm giọng nói."
                    else:
                        play_content = tr("Voice Example")
                else:
                    play_content = tr("Voice Example")
            with st.spinner(tr("Synthesizing Voice")):
                temp_dir = utils.storage_dir("temp", create=True)
                audio_file = os.path.join(temp_dir, f"tmp-voice-{str(uuid4())}.mp3")
                sub_maker = voice.tts(
                    text=play_content,
                    voice_name=voice_name,
                    voice_rate=params.voice_rate,
                    voice_file=audio_file,
                    voice_volume=params.voice_volume,
                )
                # if the voice file generation failed, try again with a default content.
                if not sub_maker:
                    play_content = "This is a example voice. if you hear this, the voice synthesis failed with the original content."
                    sub_maker = voice.tts(
                        text=play_content,
                        voice_name=voice_name,
                        voice_rate=params.voice_rate,
                        voice_file=audio_file,
                        voice_volume=params.voice_volume,
                    )

                if sub_maker and os.path.exists(audio_file):
                    with open(audio_file, "rb") as f:
                        audio_bytes = f.read()
                    if audio_bytes:
                        st.audio(
                            audio_bytes,
                            format=_detect_audio_mime(audio_file, audio_bytes),
                        )
                    else:
                        logger.error(f"voice preview audio file is empty: {audio_file}")
                    if os.path.exists(audio_file):
                        os.remove(audio_file)

        # 当选择V2版本或者声音是V2声音时，显示服务区域和API key输入框
        if selected_tts_server == "azure-tts-v2" or (
            voice_name and voice.is_azure_v2_voice(voice_name)
        ):
            saved_azure_speech_region = config.azure.get("speech_region", "")
            saved_azure_speech_key = config.azure.get("speech_key", "")
            azure_speech_region = st.text_input(
                tr("Speech Region"),
                value=saved_azure_speech_region,
                key="azure_speech_region_input",
            )
            azure_speech_key = st.text_input(
                tr("Speech Key"),
                value=saved_azure_speech_key,
                type="password",
                key="azure_speech_key_input",
            )
            config.azure["speech_region"] = azure_speech_region
            config.azure["speech_key"] = azure_speech_key

        # 当选择硅基流动时，显示API key输入框和说明信息
        if selected_tts_server == "siliconflow" or (
            voice_name and voice.is_siliconflow_voice(voice_name)
        ):
            saved_siliconflow_api_key = config.siliconflow.get("api_key", "")

            siliconflow_api_key = st.text_input(
                tr("SiliconFlow API Key"),
                value=saved_siliconflow_api_key,
                type="password",
                key="siliconflow_api_key_input",
            )

            # 显示硅基流动的说明信息
            st.info(
                tr("SiliconFlow TTS Settings")
                + ":\n"
                + "- "
                + tr("Speed: Range [0.25, 4.0], default is 1.0")
                + "\n"
                + "- "
                + tr("Volume: Uses Speech Volume setting, default 1.0 maps to gain 0")
            )

            config.siliconflow["api_key"] = siliconflow_api_key

        # 当选择 Xiaomi MiMo TTS 时，复用 MiMo LLM provider 的 API Key。
        # 这样用户如果同时使用 MiMo 生成文案和语音，只需要维护一份密钥。
        if selected_tts_server == "mimo-tts" or (
            voice_name and voice.is_mimo_voice(voice_name)
        ):
            saved_mimo_api_key = config.app.get("mimo_api_key", "")

            mimo_api_key = st.text_input(
                tr("MiMo API Key"),
                value=saved_mimo_api_key,
                type="password",
                key="mimo_tts_api_key_input",
            )

            st.info(
                tr("MiMo TTS Settings")
                + ":\n"
                + "- "
                + tr("Uses Xiaomi MiMo V2.5 TTS preset voices")
                + "\n"
                + "- "
                + tr("Speed and volume are currently handled by the provider defaults")
            )

            config.app["mimo_api_key"] = mimo_api_key

        # ElevenLabs API key section
        if selected_tts_server == "elevenlabs" or (
            voice_name and voice.is_elevenlabs_voice(voice_name)
        ):
            saved_elevenlabs_api_key = config.elevenlabs.get("api_key", "")

            elevenlabs_api_key = st.text_input(
                tr("ElevenLabs API Key"),
                value=saved_elevenlabs_api_key,
                type="password",
                key="elevenlabs_api_key_input",
            )

            _elevenlabs_models = [
                "eleven_multilingual_v2",
                "eleven_flash_v2_5",
                "eleven_v3",
            ]
            saved_elevenlabs_model = config.elevenlabs.get(
                "model_id", "eleven_multilingual_v2"
            )
            if saved_elevenlabs_model not in _elevenlabs_models:
                saved_elevenlabs_model = "eleven_multilingual_v2"
            elevenlabs_model = st.selectbox(
                tr("ElevenLabs Model"),
                options=_elevenlabs_models,
                index=_elevenlabs_models.index(saved_elevenlabs_model),
                key="elevenlabs_model_select",
            )
            config.elevenlabs["model_id"] = elevenlabs_model

            st.info(
                "ElevenLabs TTS Settings:\n"
                "- Get your API key at https://elevenlabs.io/app/settings/api-keys\n"
                "- Mark voices as ★ Favorite in the ElevenLabs voice library to make them appear here"
            )

            if elevenlabs_api_key != saved_elevenlabs_api_key:
                for k in list(st.session_state.keys()):
                    if k.startswith("elevenlabs_voices_"):
                        del st.session_state[k]

            config.elevenlabs["api_key"] = elevenlabs_api_key

        # Chatterbox API settings section (self-hosted, OpenAI-compatible)
        if selected_tts_server == "chatterbox" or (
            voice_name and voice.is_chatterbox_voice(voice_name)
        ):
            chatterbox_base_url = st.text_input(
                tr("Chatterbox Base URL"),
                value=config.chatterbox.get("base_url") or DEFAULT_CHATTERBOX_BASE_URL,
                key="chatterbox_base_url_input",
                placeholder="http://localhost:4123/v1",
            )
            config.chatterbox["base_url"] = (chatterbox_base_url or "").strip()

            chatterbox_api_key = st.text_input(
                tr("Chatterbox API Key"),
                value=config.chatterbox.get("api_key", ""),
                type="password",
                key="chatterbox_api_key_input",
            )
            config.chatterbox["api_key"] = chatterbox_api_key

            chatterbox_model = st.text_input(
                tr("Chatterbox Model"),
                value=config.chatterbox.get("model_id") or DEFAULT_CHATTERBOX_MODEL,
                key="chatterbox_model_input",
            )
            config.chatterbox["model_id"] = (
                chatterbox_model or DEFAULT_CHATTERBOX_MODEL
            ).strip()

            _saved_chatterbox_voices = (
                _parse_chatterbox_voices(config.chatterbox.get("voices"))
                or DEFAULT_CHATTERBOX_VOICES
            )
            if isinstance(_saved_chatterbox_voices, list):
                _saved_chatterbox_voices = ", ".join(_saved_chatterbox_voices)
            chatterbox_voices = st.text_input(
                tr("Chatterbox Voices"),
                value=str(_saved_chatterbox_voices or ""),
                key="chatterbox_voices_input",
                placeholder="default-Female, narrator-Male",
            )
            config.chatterbox["voices"] = _parse_chatterbox_voices(chatterbox_voices)

            st.info(
                "Chatterbox TTS Settings (self-hosted):\n"
                "- Run an OpenAI-compatible Chatterbox server (e.g. "
                "devnen/Chatterbox-TTS-Server or travisvn/chatterbox-tts-api) and "
                "set Base URL to its /v1 endpoint\n"
                "- Voices is a comma-separated list of voice names your server "
                "exposes; add a -Female or -Male suffix only to label the gender "
                "in this dropdown\n"
                "- Speech Volume is not applied for Chatterbox (the OpenAI "
                "/audio/speech API has no volume field); use Speech Rate instead"
            )

        params.voice_volume = st.selectbox(
            tr("Speech Volume"),
            options=[0.6, 0.8, 1.0, 1.2, 1.5, 2.0, 3.0, 4.0, 5.0],
            index=2,
        )

        params.voice_rate = st.selectbox(
            tr("Speech Rate"),
            options=[0.8, 0.9, 1.0, 1.1, 1.2, 1.3, 1.5, 1.8, 2.0],
            index=2,
        )

        custom_audio_file_types = ["mp3", "wav", "m4a", "aac", "flac", "ogg"]
        uploaded_audio_file = st.file_uploader(
            tr("Custom Audio File"),
            type=custom_audio_file_types
            + [file_type.upper() for file_type in custom_audio_file_types],
            accept_multiple_files=False,
            key="custom_audio_file_uploader",
        )
        if uploaded_audio_file:
            st.audio(uploaded_audio_file, format="audio/mp3")
            st.info(
                tr(
                    "Custom audio will be used directly. TTS synthesis will be skipped for this task."
                )
            )

        bgm_options = [
            (tr("No Background Music"), ""),
            (tr("Random Background Music"), "random"),
            (tr("Custom Background Music"), "custom"),
        ]
        selected_index = st.selectbox(
            tr("Background Music"),
            index=1,
            options=range(
                len(bgm_options)
            ),  # Use the index as the internal option value
            format_func=lambda x: bgm_options[x][
                0
            ],  # The label is displayed to the user
        )
        # Get the selected background music type
        params.bgm_type = bgm_options[selected_index][1]

        # Show or hide components based on the selection
        if params.bgm_type == "custom":
            custom_bgm_file = st.text_input(
                tr("Custom Background Music File"), key="custom_bgm_file_input"
            )
            if custom_bgm_file:
                # 这里不直接用 os.path.exists 判断，因为用户常见输入是
                # output000.mp3，这个文件名需要由服务层映射到 resource/songs
                # 目录后再校验。服务层会统一限制目录和文件类型，避免任意路径读取。
                params.bgm_file = custom_bgm_file.strip()
                # st.write(f":red[已选择自定义背景音乐]：**{custom_bgm_file}**")
        params.bgm_volume = st.selectbox(
            tr("Background Music Volume"),
            options=[0.0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0],
            index=2,
        )

with right_panel:
    with st.container(border=True):
        st.write(tr("Subtitle Settings"))
        params.subtitle_enabled = st.checkbox(tr("Enable Subtitles"), value=True)
        font_names = get_all_fonts()
        saved_font_name = config.ui.get("font_name", "MicrosoftYaHeiBold.ttc")
        saved_font_name_index = 0
        if saved_font_name in font_names:
            saved_font_name_index = font_names.index(saved_font_name)
        params.font_name = st.selectbox(
            tr("Font"), font_names, index=saved_font_name_index
        )
        config.ui["font_name"] = params.font_name

        subtitle_positions = [
            (tr("Top"), "top"),
            (tr("Center"), "center"),
            (tr("Bottom"), "bottom"),
            (tr("Custom"), "custom"),
        ]
        saved_subtitle_position = config.ui.get("subtitle_position", "bottom")
        saved_position_index = 2
        for i, (_, pos_value) in enumerate(subtitle_positions):
            if pos_value == saved_subtitle_position:
                saved_position_index = i
                break
        selected_index = st.selectbox(
            tr("Position"),
            index=saved_position_index,
            options=range(len(subtitle_positions)),
            format_func=lambda x: subtitle_positions[x][0],
        )
        params.subtitle_position = subtitle_positions[selected_index][1]
        config.ui["subtitle_position"] = params.subtitle_position

        if params.subtitle_position == "custom":
            saved_custom_position = config.ui.get("custom_position", 70.0)
            custom_position = st.text_input(
                tr("Custom Position (% from top)"),
                value=str(saved_custom_position),
                key="custom_position_input",
            )
            try:
                params.custom_position = float(custom_position)
                if params.custom_position < 0 or params.custom_position > 100:
                    st.error(tr("Please enter a value between 0 and 100"))
                else:
                    config.ui["custom_position"] = params.custom_position
            except ValueError:
                st.error(tr("Please enter a valid number"))

        font_cols = st.columns([0.3, 0.7])
        with font_cols[0]:
            saved_text_fore_color = config.ui.get("text_fore_color", "#FFFFFF")
            params.text_fore_color = st.color_picker(
                tr("Font Color"), saved_text_fore_color
            )
            config.ui["text_fore_color"] = params.text_fore_color

        with font_cols[1]:
            saved_font_size = config.ui.get("font_size", 60)
            params.font_size = st.slider(tr("Font Size"), 30, 100, saved_font_size)
            config.ui["font_size"] = params.font_size

        stroke_cols = st.columns([0.3, 0.7])
        with stroke_cols[0]:
            params.stroke_color = st.color_picker(tr("Stroke Color"), "#000000")
        with stroke_cols[1]:
            params.stroke_width = st.slider(tr("Stroke Width"), 0.0, 10.0, 1.5)

        subtitle_bg_cols = st.columns([0.4, 0.6])
        saved_subtitle_background_enabled = config.ui.get(
            "subtitle_background_enabled", True
        )
        with subtitle_bg_cols[0]:
            subtitle_background_enabled = st.checkbox(
                tr("Enable Subtitle Background"),
                value=saved_subtitle_background_enabled,
            )
        config.ui["subtitle_background_enabled"] = subtitle_background_enabled
        if subtitle_background_enabled:
            with subtitle_bg_cols[1]:
                saved_subtitle_background_color = config.ui.get(
                    "subtitle_background_color", "#000000"
                )
                params.text_background_color = st.color_picker(
                    tr("Subtitle Background Color"),
                    saved_subtitle_background_color,
                )
                config.ui["subtitle_background_color"] = params.text_background_color
        else:
            params.text_background_color = False

        saved_rounded_subtitle_background = config.ui.get(
            "rounded_subtitle_background", False
        )
        # 背景关闭时，圆角背景没有可渲染的底色。这里禁用控件并保留原配置，
        # 用户下次重新开启字幕背景后，可以继续使用之前保存的圆角偏好。
        params.rounded_subtitle_background = st.checkbox(
            tr("Rounded Subtitle Background"),
            value=(
                saved_rounded_subtitle_background
                if subtitle_background_enabled
                else False
            ),
            help=tr("Rounded Subtitle Background Help"),
            disabled=not subtitle_background_enabled,
        )
        if subtitle_background_enabled:
            config.ui["rounded_subtitle_background"] = (
                params.rounded_subtitle_background
            )

# ===== 生成 / 输出 tab：出片按钮 + 实时日志 + 结果视频（点击即在本 tab 内可见）=====
with tab_output:
    st.subheader(tr("Generate and Output"))
    # 出片前置条件清单：满足=绿色✅，不满足=红色❌；全部满足才可点「生成视频」。
    st.write(tr("Checklist before generating"))
    _conditions = generation_conditions(params)
    # 每个条件对应去哪里满足：'script'/'video' 跳主 tab(按标签文字)，'basic' 展开顶部基础设置。
    _target_tab_label = {
        "script": tr("Tab Script"),
        "video": tr("Tab Video"),
        "audio": tr("Tab Audio Subtitle"),
    }
    for _i, _cond in enumerate(_conditions):
        _row_txt, _row_btn = st.columns([5, 1])
        with _row_txt:
            if _cond["met"]:
                st.markdown(f"✅ :green[{_cond['label']}]")
            else:
                st.markdown(f"❌ :red[{_cond['label']}]")
        with _row_btn:
            if st.button(tr("Go To"), key=f"goto_cond_{_i}", use_container_width=True):
                if _cond["target"] == "basic":
                    request_nav("basic")
                else:
                    request_nav("tab", _target_tab_label.get(_cond["target"], ""))
    _all_met = all(_c["met"] for _c in _conditions)
    if st.button(
        tr("Generate Video"),
        use_container_width=True,
        type="primary",
        key="generate_video_btn",
        disabled=not _all_met,
    ):
        run_generation(params, uploaded_audio_file, uploaded_files)

config.save_config()

# 出片清单『前往』按钮的挂起导航，须在整页(含所有 tab 按钮)渲染完成后执行。
apply_pending_nav()
