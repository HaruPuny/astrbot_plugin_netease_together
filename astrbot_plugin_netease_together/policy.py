"""Permission and direct-intent checks independent of model-supplied arguments."""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from urllib.parse import unquote, urlsplit

from .bridge_client import MusicError


def master_ids(data_root: Path) -> set[str]:
    """Follow the actual owner plugin; absence or malformed config fails closed."""
    try:
        path = data_root / "config" / "astrbot_plugin_master_aware_config.json"
        if path.stat().st_size > 1000000:
            return set()
        config = json.loads(path.read_text(encoding="utf-8-sig"))
        if not isinstance(config, dict):
            return set()
        values = config.get("master_qq", [])
        if not isinstance(values, list):
            return set()
        return {str(item) for item in values if str(item).isdecimal()}
    except (OSError, ValueError, TypeError):
        return set()


def raw_text(raw: dict) -> str | None:
    """Use the OneBot source payload, never quoted content or an expanded prompt."""
    segments = raw.get("message")
    if isinstance(segments, str):
        return None if "[CQ:" in segments else segments.strip()
    if not isinstance(segments, list) or not segments:
        return None
    if not all(isinstance(item, dict) and item.get("type") == "text" for item in segments):
        return None
    try:
        return "".join(str(item["data"]["text"]) for item in segments).strip()
    except (KeyError, TypeError):
        return None


def check_raw_identity(event, config, owners: set[str]) -> dict:
    """Require real inbound QQ private metadata as well as the registered UMO."""
    owner_umo = str(config.get("owner_umo", "")).strip()
    platform_id = str(config.get("platform_id", "")).strip()
    sender = str(event.get_sender_id())
    if owner_umo and str(event.unified_msg_origin) != owner_umo:
        raise MusicError("owner_session_required", "一起听只对配置好的主人 QQ 私聊开放。")
    if not owner_umo and not owners:
        raise MusicError("owner_session_required",
            "尚未配置主人身份：请在插件配置里填写 owner_umo，或安装 master_aware 并在其中设置主人 QQ。")
    if (sender not in owners or event.get_platform_name() != "aiocqhttp"
            or (platform_id and event.get_platform_id() != platform_id)
            or not event.is_private_chat()
            or event.get_group_id()):
        raise MusicError("owner_private_required", "一起听只对主人 QQ 私聊开放。")
    raw = getattr(event.message_obj, "raw_message", None)
    if not isinstance(raw, dict):
        raise MusicError("real_message_required", "请由主人在 QQ 私聊中直接发送请求。")
    message_id = str(getattr(event.message_obj, "message_id", "") or "")
    if (raw.get("post_type") != "message" or raw.get("message_type") != "private"
            or str(raw.get("user_id", "")) != sender or not message_id
            or str(raw.get("message_id", "")) != message_id
            or str(raw.get("self_id", "")) != str(event.get_self_id())):
        raise MusicError("real_message_required", "请由主人在 QQ 私聊中直接发送请求。")
    try:
        age = time.time() - float(raw.get("time", 0))
    except (TypeError, ValueError):
        age = 10**9
    if age < -60 or age > 600:
        raise MusicError("stale_message", "这条请求已过期，请重新直接发送。")
    # No allow/owner flags from event.extra, LLM arguments, or quoted content.
    return raw


def parse_control(text: str) -> tuple[str, str] | None:
    """A finite set of full-message intents; no substring authorization."""
    text = text.strip()
    official_share = re.fullmatch(
        r"我的耳机分你一半，和我一起听歌吧[～~]快点开看看\s+"
        r"(https://(?:st\.)?music\.163\.com/[^\s<>()（）]+)"
        r"(?:\s*[（(]@网易云音乐[）)])?", text,
    )
    if official_share:
        return "join", official_share.group(1)
    # A directly sent official invitation is itself a join request. Mixed text
    # is restricted to these full-message invitation phrases, not substrings
    # found in a quote, transcript, lyrics, or a model-supplied reason.
    invite = re.fullmatch(
        r"(?:(?:请)?(?:加入一起听|来一起听|和我一起听|陪我一起听)[：:，,\s]+)?"
        r"(https://(?:st\.)?music\.163\.com/[^\s<>()（）]+)", text,
    )
    if invite:
        return "join", invite.group(1)
    command = re.fullmatch(r"/?一起听\s+(加入|暂停|继续|下一首|退出|切歌|下首)(?:\s+(.+))?", text)
    if command:
        verb, target = command.group(1), (command.group(2) or "").strip()
        action = {"加入": "join", "暂停": "pause", "继续": "resume", "下一首": "next",
                  "退出": "leave", "切歌": "goto", "下首": "enqueue_next"}[verb]
        if action in {"pause", "resume", "next", "leave"}:
            return (action, "") if not target else None
        if action == "join":
            return (action, target) if target and len(target) <= 4000 and not re.search(r"\s", target) else None
        return (action, target) if re.fullmatch(r"[0-9]{1,30}", target) else None
    simple = {
        "暂停音乐": "pause", "暂停播放": "pause", "暂停一下音乐": "pause",
        "继续播放": "resume", "继续音乐": "resume", "恢复播放": "resume",
        "下一首": "next", "换下一首": "next", "播放下一首": "next",
        "退出一起听": "leave", "结束一起听": "leave",
    }
    return (simple[text], "") if text in simple else None


def check_invite(invite_url: str, inviter_uid: str) -> str:
    if not inviter_uid.isdecimal():
        raise MusicError("missing_inviter_uid", "请先在安全配置中填写主人的网易云 UID。")
    decoded = invite_url
    for _ in range(3):
        updated = unquote(decoded)
        if updated == decoded:
            break
        decoded = updated
    parsed = urlsplit(decoded)
    if (parsed.scheme != "https" or parsed.hostname not in {"music.163.com", "st.music.163.com"}
            or parsed.username or parsed.password or parsed.port not in {None, 443}
            or not re.search(r"listen-?together", parsed.path + "#" + parsed.fragment, re.I)
            or "multishare" in decoded.lower() or re.search(r"[?&]inviterUid=", decoded, re.I)):
        raise MusicError("unsupported_invite", "请使用网易云官方双人一起听邀请链接；新版多人链接暂不支持。")
    room = re.search(r"[?&]roomId=([^&#]+)", decoded, re.I)
    inviter = re.search(r"[?&]inviterId=([0-9]+)(?:[&#]|$)", decoded, re.I)
    if not room or not inviter or inviter.group(1) != inviter_uid:
        raise MusicError("invite_owner_mismatch", "邀请缺少双人房间参数，或邀请者与配置的主人网易云 UID 不一致。")
    return invite_url
