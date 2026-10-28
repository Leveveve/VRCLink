"""
VRChat adapter — push-to-talk bridge between VRChat and the KiraAI message pipeline.

Flow (outbound to VRChat):
    KiraAI pipeline reply -> IMCapability.send_direct_message(user_id, MessageChain)
        -> extract Text elements -> split into <=140 char segments
        -> throttled asyncio queue -> OSC /chatbox/input to 127.0.0.1:<osc_in_port>

Flow (inbound from VRChat):
    global hotkey / VR controller key (via OVR Advanced Settings Key Press,
    which synthesizes the same OS-level key) -> pynput listener
        -> record 16kHz mono wav via sounddevice
        -> speech-to-text via a KiraAI-configured cloud STT provider
        -> IMCapability.publish -> KiraMessageEvent(adapter, dm, is_mentioned=True)
"""

import asyncio
import json
import math
import os
import re
import time
import uuid
import wave
from pathlib import Path

from core.adapter.access import ListAccessPolicy
from core.adapter.base import AdapterTargetId, BaseAdapter
from core.adapter.capabilities import IMCapability
from core.adapter.context import AdapterContext
from core.adapter.message_format_metadata import MessageFormatMetadata
from core.chat import KiraIMMessage, KiraIMSentResult, KiraMessageEvent, MessageChain, User
from core.chat.message_elements import Record, Text
from core.logging_manager import get_logger
from core.utils.path_utils import get_data_path

logger = get_logger("VRChat", "purple")

CHATBOX_LIMIT = 140  # VRChat hard limit is 144; leave headroom
# Plugin data folder name under <data>/plugin_data/ (stable across bots).
PLUGIN_DATA_NAME = "vrclink"

# Pre-1.0.0 config keys removed in 1.0.0 (local ASR dropped for cloud STT).
_LEGACY_ASR_KEYS = (
    "asr_backend", "whisper_model", "whisper_language", "whisper_compute",
    "whisper_download_root", "hf_endpoint", "asr_load_timeout_seconds",
    "cloud_stt_model",
)

# mic_device 下拉列表的首个哨兵项：选中它（或配置为空串）= 不指定设备，
# sounddevice 用 Windows 系统默认输入。value 即该字符串本身。
MIC_DEVICE_DEFAULT_OPTION = "【系统默认】跟随 Windows 设置"
# 下拉项的 hostapi 前缀分隔符："{hostapi} · {name}"
_MIC_HOSTAPI_SEP = " · "


# ─────────────────────── mic_device 解析（唯一权威实现） ───────────────────────

def resolve_mic_device(value: str, devices=None):
    """把 mic_device 配置值解析成 sounddevice 输入设备 index。

    规则（与 README「故障排查」一致，所有调用点都必须走这里）：
      1. 空串 / 以「【系统默认】」开头 → (None, "", "")：不指定设备，
         sounddevice 用系统默认；
      2. 与某设备完整名精确匹配（忽略大小写与首尾空白，MME 设备名常带
         尾部空格）→ 该设备；
      3. 子串匹配（不区分大小写，要求 max_input_channels>0）：先拿原值整串
         匹配（兼容旧版手填），再拿去掉 "{hostapi} · " 前缀后的值匹配
         （兼容下拉项），返回枚举顺序里的首个命中；
      4. 都不中 → (None, "not_found", 警告文本)：回退系统默认，不抛致命错。

    devices 传入 sd.query_devices() 结果可避免重复枚举（状态页轮询用）。
    纯函数、不打日志、不开设备——警告由调用方决定是否记录。
    返回 (index_or_None, how, warning)，how ∈ {"", "exact", "substring",
    "substring_unprefixed", "not_found"}。
    """
    value = str(value or "").strip()
    if not value or value.startswith("【系统默认】"):
        return None, "", ""
    if devices is None:
        import sounddevice as sd
        devices = sd.query_devices()

    def entries():
        for dev in devices:
            try:
                if int(dev.get("max_input_channels", 0)) > 0:
                    yield int(dev.get("index", -1)), str(dev.get("name", "") or "")
            except Exception:
                continue

    want = value.strip().lower()
    for idx, name in entries():  # 规则 2：完整名精确匹配
        if name.strip().lower() == want:
            return idx, "exact", ""
    for idx, name in entries():  # 规则 3a：原值子串（旧版手填行为）
        if want in name.lower():
            return idx, "substring", ""
    stripped = value
    if _MIC_HOSTAPI_SEP in stripped:
        stripped = stripped.split(_MIC_HOSTAPI_SEP, 1)[1].strip()
    if stripped and stripped.lower() != want:
        want2 = stripped.lower()
        for idx, name in entries():  # 规则 3b：去掉 hostapi 前缀的子串（下拉项）
            if want2 in name.lower():
                return idx, "substring_unprefixed", ""
    return None, "not_found", f"未找到麦克风设备「{value}」，已回退系统默认"


def build_mic_device_options(devices: list[dict], current_values=None) -> list[str]:
    """由设备表生成 mic_device 下拉 options（哨兵恒在首位）。

    只收能以 16 kHz 打开的输入设备；标签形如 "{hostapi} · {name}"，按完整
    设备名去重（忽略大小写/首尾空白），同名保留枚举序里首个 hostapi（MME
    通常在最前）。current_values 是已保存的配置值：不在列表里则原样追加到
    末尾（不带任何后缀——value 与 label 是同一字符串，加后缀会改掉值），
    保证 WebUI 下拉与 build_fields 不会把用户已保存的设备名洗掉。
    """
    options = [MIC_DEVICE_DEFAULT_OPTION]
    seen = set()
    for d in devices or []:
        try:
            if not d.get("ok_16k"):
                continue
            name = str(d.get("name", "") or "").strip()
            if not name:
                continue
            key = name.lower()
            if key in seen:
                continue
            seen.add(key)
            hostapi = str(d.get("hostapi", "") or "").strip()
            options.append(f"{hostapi}{_MIC_HOSTAPI_SEP}{name}" if hostapi else name)
        except Exception:
            continue
    for v in current_values or []:
        v = str(v or "").strip()
        if not v or v.startswith("【系统默认】") or v in options:
            continue
        options.append(v)
    return options


def refresh_mic_device_options(schema_path, current_values=None) -> list[str]:
    """把本机真实麦克风列表写进 adapter/schema.json 的 mic_device.options。

    框架只从 <组件目录>/schema.json 静态读 schema（core/plugin/manager.py
    register_plugin_adapter → build_fields），没有动态选项机制，所以插件
    必须在注册适配器之前自行刷新这个文件；下拉列表因此要等 bot 重启（或
    插件禁用再启用）才会在 WebUI 生效。

    写入是文本外科手术：只动 mic_device 块里的 options 数组，其余内容
    逐字保持原样（文件是手写格式，json 全量回写会重排 locales 等紧凑行）。
    options 与现值相同则不写（避免无谓改动 mtime）。写回原子（tmp +
    os.replace）。返回最终 options；任何异常由调用方兜住，绝不影响初始化。
    """
    schema_path = Path(schema_path)
    text = schema_path.read_text(encoding="utf-8")
    schema = json.loads(text)
    field = schema.get("mic_device")
    if not isinstance(field, dict):
        raise ValueError(f"schema.json has no mic_device object: {schema_path}")
    if field.get("type") not in (None, "string"):
        raise ValueError(f"mic_device.type is not string: {field.get('type')!r}")
    devices = _Recorder.list_input_devices()
    usable = sum(1 for d in devices if d.get("ok_16k"))
    logger.info(
        f"mic device scan: {len(devices)} input device(s), {usable} usable @16 kHz"
    )
    defaults = []
    old_default = field.get("default")
    if isinstance(old_default, str) and old_default.strip():
        defaults.append(old_default)
    options = build_mic_device_options(devices, list(defaults) + list(current_values or []))
    if field.get("options") == options:
        return options

    new_text = _upsert_mic_device_options(text, options)
    json.loads(new_text)  # 写之前确认仍是合法 JSON
    tmp = schema_path.with_name(schema_path.name + ".tmp")
    tmp.write_text(new_text, encoding="utf-8")
    os.replace(tmp, schema_path)
    logger.info(
        f"mic_device.options updated in {schema_path.name}: "
        f"{len(options)} item(s) (重启 bot 后 WebUI 下拉生效)"
    )
    return options


def _upsert_mic_device_options(text: str, options: list[str]) -> str:
    """在 schema 文本里只替换 mic_device 块的 options 数组，其余逐字不动。"""
    lines = text.splitlines(keepends=True)
    start = None
    indent = ""
    for i, line in enumerate(lines):
        m = re.match(r'^(\s*)"mic_device"\s*:\s*\{\s*$', line)
        if m:
            start, indent = i, m.group(1)
            break
    if start is None:
        raise ValueError("mic_device block not found in schema.json")
    close = indent + "}"
    end = None
    for j in range(start + 1, len(lines)):
        if lines[j].rstrip("\r\n") == close or lines[j].rstrip("\r\n") == close + ",":
            end = j  # mic_device 块的收尾行（可能是 "},"）
            break
    if end is None:
        raise ValueError("mic_device block not terminated in schema.json")

    item_indent = indent + "    "
    opt_lines = [f'{item_indent}"options": [']
    for k, opt in enumerate(options):
        comma = "," if k < len(options) - 1 else ""
        opt_lines.append(f'{item_indent}    {json.dumps(opt, ensure_ascii=False)}{comma}')
    opt_lines.append(f"{item_indent}],")

    # 块内已有 options：整段替换；否则插到 "type" 行之后（与其它字段的
    # name/type/options/default 顺序一致）。收尾行是首个以 "]" 开头的行
    # （每个选项行都以引号开头，字符串内的 "[" 不会成行首，不受设备名
    # 含方括号影响）。
    opts_start = opts_end = None
    for j in range(start + 1, end):
        if re.match(r'^"options"\s*:', lines[j].strip()):
            opts_start = j
            opts_end = j
            for k in range(j + 1, end):
                opts_end = k
                if lines[k].strip().startswith("]"):
                    break
            break
    if opts_start is not None:
        trailing = "\r\n" if lines[opts_end].endswith("\r\n") else "\n"
        if not lines[opts_end].rstrip("\r\n").endswith(","):
            opt_lines[-1] = opt_lines[-1].rstrip(",")
        return "".join(lines[:opts_start]) + "".join(l + trailing for l in opt_lines) + "".join(lines[opts_end + 1:])

    for j in range(start + 1, end):
        if re.match(r'^\s*"type"\s*:', lines[j]):
            trailing = "\r\n" if lines[j].endswith("\r\n") else "\n"
            return "".join(lines[:j + 1]) + "".join(l + trailing for l in opt_lines) + "".join(lines[j + 1:])
    raise ValueError('"type" line not found inside mic_device block')


# ─────────────────────────── text helpers ───────────────────────────

def _strip_noise(text: str) -> str:
    """Remove residual XML-ish tags and collapse whitespace."""
    text = re.sub(r"<[^>]+>", "", text)
    return re.sub(r"[ \t]+", " ", text).strip()


def _split_segments(text: str, limit: int = CHATBOX_LIMIT) -> list[str]:
    """Split into chatbox-sized segments: prefer newlines, then sentence
    punctuation, then commas/spaces, finally a hard cut (no broken UTF-8 —
    Python str slicing is codepoint-based, so this is safe)."""
    # reply_prefix 过长会把 limit 压到 ≤0，入口兜底防死循环
    limit = max(1, limit)
    segments: list[str] = []

    def _emit(chunk: str):
        # 硬切收敛在 _emit：任何入队的分段都必须 ≤ limit（分句后单段
        # 仍超长时在此补刀），外层不再需要专门的补刀循环
        while len(chunk) > limit:
            cut = chunk.rfind(" ", 0, limit)
            cut = cut if cut > limit // 2 else limit
            piece, chunk = chunk[:cut].strip(), chunk[cut:]
            if piece:
                segments.append(piece)
        chunk = chunk.strip()
        if chunk:
            segments.append(chunk)

    def _split_by(chunk: str, seps: str):
        parts = re.split(rf"([{seps}])", chunk)
        buf = ""
        for i in range(0, len(parts) - 1, 2):
            piece, sep = parts[i], parts[i + 1]
            if len(buf) + len(piece) + 1 > limit:
                _emit(buf)
                buf = piece + sep
            else:
                buf += piece + sep
        buf += parts[-1] if parts else ""
        return buf

    for para in text.split("\n"):
        if len(para) <= limit:
            _emit(para)
            continue
        rest = _split_by(para, "。！？!?；;")
        if len(rest) > limit:
            rest = _split_by(rest, "，,、")
        _emit(rest)

    return segments


# ASR providers occasionally emit these on silence / short clips; treat as no
# speech so the pipeline is never fed junk input.
_HALLUCINATION_PATTERNS = (
    "谢谢观看", "感谢观看", "请订阅", "字幕由", "字幕by", "by amara.org",
    "subtitles by", "谢谢收看", "请点赞",
)


def _is_hallucination(text: str) -> bool:
    low = text.lower()
    return any(p in low for p in _HALLUCINATION_PATTERNS)


def _summarize_error(e: Exception, limit: int = 200) -> str:
    """One-line readable summary of an STT exception, truncated."""
    msg = re.sub(r"\s+", " ", str(e) or type(e).__name__).strip()
    return msg[:limit]


def migrate_legacy_asr_config(config: dict) -> dict | None:
    """Drop pre-1.0.0 local-ASR keys; carry `cloud_stt_model` into `stt_model`.

    Mutates `config` in place and returns it, or None when nothing changed
    (idempotent: a second run on already-migrated config returns None).
    The plugin calls this against `ctx.config["adapters"][id]["config"]` and
    persists via `kira_config.save_config()` — no direct file writing here.
    """
    if not isinstance(config, dict):
        return None
    changed = False
    if not str(config.get("stt_model", "") or "").strip():
        legacy = str(config.get("cloud_stt_model", "") or "").strip()
        if legacy:
            config["stt_model"] = legacy
            changed = True
    if config.pop("asr_backend", None) is not None:
        changed = True
    for key in _LEGACY_ASR_KEYS:
        if key == "asr_backend":
            continue
        if key == "cloud_stt_model":
            # Remove only after its value has been carried over above.
            if config.pop("cloud_stt_model", None) is not None:
                changed = True
            continue
        if config.pop(key, None) is not None:
            changed = True
    return config if changed else None


def _foreground_process_name() -> str:
    """Return the executable name of the focused window (Windows only)."""
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return ""
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if not pid.value:
            return ""
        import psutil

        return psutil.Process(pid.value).name() or ""
    except Exception:
        return ""


# ─────────────────────────── recorder ───────────────────────────

_DBFS_FLOOR = -120.0  # display floor for digital silence (peak 0)


def _to_dbfs(linear: float) -> float:
    """int16 full-scale-referenced dBFS with a floor for silence."""
    if linear <= 0:
        return _DBFS_FLOOR
    return max(_DBFS_FLOOR, 20.0 * math.log10(linear / 32768.0))


class _Recorder:
    """16 kHz mono int16 wav recorder built on sounddevice.InputStream.

    Applies a per-capture software gain to the int16 frames and tracks
    peak / RMS (dBFS) of the gain-applied audio — i.e. exactly what gets
    written to the wav and fed to ASR. Stats are readable via the
    last_* attributes after stop_and_save().
    """

    SAMPLE_RATE = 16000
    # A post-gain sample this close to the int16 ceiling counts as clipping.
    _CLIP_THRESHOLD = 32760.0

    def __init__(self):
        self._stream = None
        self._frames: list = []
        self._gain = 1.0
        self._device_name = ""
        self._device_index: int | None = None
        self._peak = 0.0
        self._sum_sq = 0.0
        self._sample_count = 0
        # Filled in by stop_and_save(); readable afterwards.
        self.last_peak_dbfs: float = _DBFS_FLOOR
        self.last_rms_dbfs: float = _DBFS_FLOOR
        self.last_device_name: str = ""
        self.last_device_index: int | None = None
        self.last_clipping: bool = False
        # start() 时 mic_device 解析产生的非致命警告（如配置的设备没找到、
        # 已回退系统默认）；调用方负责转存到适配器状态页。
        self.last_warning: str = ""

    def start(self, device_setting: str = "", gain: float = 1.0):
        import sounddevice as sd

        kwargs = {
            "samplerate": self.SAMPLE_RATE,
            "channels": 1,
            "dtype": "int16",
            "callback": self._on_audio,
        }
        # mic_device 值 → 设备 index 的解析规则见 resolve_mic_device（唯一
        # 权威实现）。找不到设备不抛致命错：回退系统默认并留下警告，让
        # PTT/试录继续可用（状态页会显示警告）。
        self.last_warning = ""
        device_idx: int | None = None
        setting = str(device_setting or "").strip()
        if setting:
            device_idx, _how, warning = resolve_mic_device(setting, sd.query_devices())
            if warning:
                self.last_warning = warning
                logger.warning(warning)
        if device_idx is None:
            # 未指定或回退：显式定位系统默认输入，让状态页能看到真实设备名
            try:
                device_idx = int(sd.default.device[0])
            except Exception:
                device_idx = None
        if device_idx is not None and device_idx >= 0:
            kwargs["device"] = device_idx
        self._device_name = ""
        self._device_index = None
        try:
            if device_idx is not None and device_idx >= 0:
                info = sd.query_devices(device_idx)
                self._device_name = str(info.get("name", "") or "")
                self._device_index = device_idx
        except Exception:
            pass
        if not self._device_name:
            self._device_name = setting
        self._gain = float(gain)
        self._frames = []
        self._peak = 0.0
        self._sum_sq = 0.0
        self._sample_count = 0
        self._stream = sd.InputStream(**kwargs)
        self._stream.start()

    @staticmethod
    def list_input_devices() -> list[dict]:
        """Enumerate input devices; ok_16k is verified by actually opening
        the device at 16 kHz mono int16 (hostapi alone is not reliable)."""
        import sounddevice as sd

        devices: list[dict] = []
        try:
            raw = sd.query_devices()
        except Exception as e:
            logger.warning(f"query_devices failed: {e}")
            return devices
        try:
            default_in = int(sd.default.device[0])
        except Exception:
            default_in = -1
        for dev in raw:
            try:
                if int(dev.get("max_input_channels", 0)) <= 0:
                    continue
                idx = int(dev.get("index", 0))
                hostapi = ""
                try:
                    hostapi = str(sd.query_hostapis(int(dev.get("hostapi", 0))).get("name", "") or "")
                except Exception:
                    pass
                devices.append({
                    "index": idx,
                    "name": str(dev.get("name", "") or ""),
                    "hostapi": hostapi,
                    "channels": int(dev.get("max_input_channels", 0)),
                    "default_samplerate": round(float(dev.get("default_samplerate", 0) or 0)),
                    "ok_16k": _Recorder._can_open_16k(sd, idx),
                    "is_default": idx == default_in,
                })
            except Exception as e:
                logger.debug(f"skip device entry during enumeration: {e}")
        return devices

    @staticmethod
    def _can_open_16k(sd, index: int) -> bool:
        try:
            stream = sd.InputStream(samplerate=16000, channels=1, dtype="int16", device=index)
            stream.close()
            return True
        except Exception:
            return False

    def _on_audio(self, indata, frames, time_info, status):
        import numpy as np

        if self._gain != 1.0:
            indata = np.clip(
                indata.astype(np.float32) * self._gain, -32768.0, 32767.0,
            ).astype(np.int16)
        self._frames.append(indata.copy())
        x = indata.reshape(-1).astype(np.float64)
        if x.size:
            self._peak = max(self._peak, float(np.max(np.abs(x))))
            self._sum_sq += float(np.dot(x, x))
            self._sample_count += x.size

    def stop_and_save(self, path: str) -> float:
        """Stop recording, write wav, return duration in seconds."""
        if self._stream is not None:
            self._stream.stop()
            self._stream.close()
            self._stream = None
        duration = sum(f.shape[0] for f in self._frames) / self.SAMPLE_RATE
        import numpy as np

        audio = np.concatenate(self._frames, axis=0) if self._frames else np.zeros((0, 1), dtype="int16")
        self._frames = []
        with wave.open(path, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(self.SAMPLE_RATE)
            wf.writeframes(audio.tobytes())
        self.last_device_name = self._device_name
        self.last_device_index = self._device_index
        self.last_peak_dbfs = _to_dbfs(self._peak)
        rms = math.sqrt(self._sum_sq / self._sample_count) if self._sample_count else 0.0
        self.last_rms_dbfs = _to_dbfs(rms)
        self.last_clipping = self._peak >= self._CLIP_THRESHOLD
        return duration


# ─────────────────────────── hotkey ───────────────────────────

def _parse_hotkey(spec: str):
    """'ctrl+shift+v' -> ({'ctrl','shift'}, 'v');  'b' -> (set(), 'b');
    'x2'/'x3' -> mouse side button marker."""
    parts = [p.strip().lower() for p in (spec or "").split("+") if p.strip()]
    if not parts:
        return set(), ""
    return set(parts[:-1]), parts[-1]


class _HotkeyWatcher:
    """pynput-based global key watcher with modifier support.

    Fires on_down/on_up callbacks from the pynput thread; the adapter is
    responsible for marshalling them onto the asyncio loop.
    """

    _MOD_ALIASES = {
        "ctrl": {"ctrl_l", "ctrl_r", "ctrl"},
        "shift": {"shift", "shift_l", "shift_r"},
        "alt": {"alt", "alt_l", "alt_r", "alt_gr"},
    }

    def __init__(self, spec: str, on_down, on_up):
        self.mods, self.key = _parse_hotkey(spec)
        self.is_mouse = self.key in ("x1", "x2", "x3")
        self.on_down = on_down
        self.on_up = on_up
        self._held_mods: set[str] = set()
        # 目标键当前是否按住：键盘按住不放会持续派发 on_press（自动重复），
        # 只认第一次按下，on_release 时清零
        self._pressed = False
        self._listener = None
        if not self.key:
            raise ValueError("empty hotkey")

    def start(self):
        if self.is_mouse:
            from pynput import mouse

            target = getattr(mouse.Button, self.key, None)
            if target is None:
                raise ValueError(f"unknown mouse button: {self.key}")

            def on_click(_, button, pressed):
                # 鼠标事件本身无自动重复，但与键盘共用 _pressed 状态位，
                # 顺带防御驱动/注入产生的重复 press 事件
                if button == target:
                    if pressed:
                        if not self._pressed:
                            self._pressed = True
                            self.on_down()
                    else:
                        self._pressed = False
                        self.on_up()

            self._listener = mouse.Listener(on_click=on_click)
        else:
            from pynput import keyboard

            def norm(key):
                if isinstance(key, keyboard.KeyCode):
                    return (key.char or "").lower() or f"vk{key.vk}"
                return getattr(key, "name", "") or ""

            want_ctrl = "ctrl" in self.mods
            want_shift = "shift" in self.mods
            want_alt = "alt" in self.mods

            def matches(held: set[str], key_norm: str) -> bool:
                if key_norm != self.key:
                    return False
                got_ctrl = bool(held & self._MOD_ALIASES["ctrl"])
                got_shift = bool(held & self._MOD_ALIASES["shift"])
                got_alt = bool(held & self._MOD_ALIASES["alt"])
                return got_ctrl == want_ctrl and got_shift == want_shift and got_alt == want_alt

            def on_press(key):
                n = norm(key)
                if n in self._MOD_ALIASES["ctrl"] | self._MOD_ALIASES["shift"] | self._MOD_ALIASES["alt"]:
                    self._held_mods.add(n)
                elif matches(self._held_mods, n):
                    # 自动重复的 on_press 只认第一次
                    if not self._pressed:
                        self._pressed = True
                        self.on_down()

            def on_release(key):
                n = norm(key)
                # 触发键一松开就解锁状态位：修饰键可能先松掉，此时 matches 不成立，
                # 若不解锁，下一次按下会被当成"自动重复"被吞掉（组合键失效）
                if n == self.key:
                    self._pressed = False
                if n in self._MOD_ALIASES["ctrl"] | self._MOD_ALIASES["shift"] | self._MOD_ALIASES["alt"]:
                    self._held_mods.discard(n)
                elif matches(self._held_mods, n):
                    self.on_up()

            self._listener = keyboard.Listener(on_press=on_press, on_release=on_release)
        self._listener.daemon = True
        self._listener.start()

    def stop(self):
        if self._listener is not None:
            self._listener.stop()
            self._listener = None


# ─────────────────────────── chatbox sender ───────────────────────────

class _ChatboxSender:
    """Throttled queue feeding OSC /chatbox/input (5 msgs / 5s leaky bucket
    on VRChat's side; we self-throttle to at least `interval`).

    Segment gap scales with text length so the reader can finish each line;
    typing indicator is re-sent periodically because VRChat drops "···" when
    the chatbox sees no ongoing input for a few seconds.
    """

    TYPING_REFRESH_SEC = 2.0

    def __init__(
        self,
        port: int,
        interval: float,
        typing_enabled: bool,
        on_drain=None,
        read_cps: float = 5.0,
        max_interval: float = 8.0,
    ):
        from pythonosc.udp_client import SimpleUDPClient

        self._client = SimpleUDPClient("127.0.0.1", port)
        self._interval = max(1.0, interval)
        self._read_cps = max(1.0, read_cps)
        self._max_interval = max(self._interval, max_interval)
        self._typing_enabled = typing_enabled
        self.on_drain = on_drain
        self._queue: asyncio.Queue = asyncio.Queue()
        self._typing_on = False
        self.pending = 0
        self.sent_count = 0
        self._task: asyncio.Task | None = None
        self._typing_task: asyncio.Task | None = None

    def start(self):
        self._task = asyncio.create_task(self._run())
        self._typing_task = asyncio.create_task(self._typing_keepalive())

    async def stop(self):
        for task in (self._task, self._typing_task):
            if task:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        self._task = None
        self._typing_task = None

    def _send_typing(self, on: bool):
        try:
            self._client.send_message("/chatbox/typing", [bool(on)])
        except Exception as e:
            logger.debug(f"typing indicator failed: {e}")

    def set_typing(self, on: bool):
        if not self._typing_enabled:
            return
        changed = on != self._typing_on
        self._typing_on = on
        # Always poke the OSC port on change; refresh is handled by keepalive.
        if changed:
            self._send_typing(on)

    async def _typing_keepalive(self):
        """VRChat hides "···" unless chatbox input keeps arriving — re-assert."""
        while True:
            await asyncio.sleep(self.TYPING_REFRESH_SEC)
            if self._typing_on and self._typing_enabled:
                self._send_typing(True)

    def enqueue(self, segments: list[str], prefix: str = ""):
        for seg in segments:
            text = f"{prefix}{seg}" if prefix else seg
            self._queue.put_nowait(text)
            self.pending += 1

    def clear_pending(self):
        """Drop everything queued (e.g. plugin shutting down mid-send)."""
        while not self._queue.empty():
            self._queue.get_nowait()
            self.pending = max(0, self.pending - 1)

    def _gap_for(self, seg: str) -> float:
        """Reading-time gap: short lines wait `interval`, long lines wait longer."""
        reading = len(seg) / self._read_cps
        return min(max(self._interval, reading), self._max_interval)

    async def _run(self):
        while True:
            seg = await self._queue.get()
            try:
                self._client.send_message("/chatbox/input", [seg, True, False])
                self.sent_count += 1
                logger.info(f"chatbox <- ({self.pending - 1} queued) {seg[:50]}")
            except Exception as e:
                logger.warning(f"chatbox send failed: {e}")
            finally:
                self.pending = max(0, self.pending - 1)
                if self.pending == 0:
                    self.set_typing(False)
                    if self.on_drain:
                        self.on_drain()
            await asyncio.sleep(self._gap_for(seg))


# ─────────────────────────── the adapter ───────────────────────────

# 插件 main.py 的 initialize() 注入的云端 STT getter（模块级兜底）：
# 适配器配置热更新会重建实例，实例级 _stt_provider 随之丢失，而适配器
# 模块在实例重建间被 sys.modules 缓存，新实例为空时回退到这里取。
# 签名：getter(model_uuid: str = "") -> STTModelClient | None
_STT_GETTER = None


def set_stt_getter(getter):
    global _STT_GETTER
    _STT_GETTER = getter


class VRChatIMCapability(IMCapability["VRChatAdapter"]):
    """IM operations: receive local PTT transcriptions, reply to the chatbox."""

    _SUPPORTED_ELEMENTS = ["text"]

    async def get_message_metadata(self) -> MessageFormatMetadata:
        return MessageFormatMetadata(self._supported_elements)

    def publish_transcription(self, text: str):
        """Publish an ASR result as an inbound DM from the configured owner.

        Local PTT is the only inbound source; the instance access policy
        (im.direct.receive) still gates the owner id like any other adapter.
        """
        owner_id = str(self.adapter.config.get("owner_id", "owner"))
        if not self.is_allowed(owner_id, permission="im.direct.receive"):
            logger.info(f"PTT input denied by access policy: {owner_id!r}")
            return
        self.publish(self.adapter._build_message_event(text))

    async def send_group_message(
        self, group_id: AdapterTargetId, message: MessageChain,
    ) -> KiraIMSentResult | None:
        return KiraIMSentResult(None, ok=False, err="VRChat adapter is DM-only")

    async def send_direct_message(
        self, user_id: AdapterTargetId, message: MessageChain,
    ) -> KiraIMSentResult | None:
        texts, skipped = [], 0
        for ele in message:
            t = getattr(ele, "text", None)
            if isinstance(t, str) and t.strip():
                texts.append(_strip_noise(t))
            else:
                skipped += 1
        full = " ".join(x for x in texts if x)
        if not full:
            return KiraIMSentResult(None, ok=True)
        if skipped:
            logger.debug(f"dropped {skipped} non-text element(s) from reply")
        segments = self.adapter._enqueue_reply(full)
        self.adapter.last_reply = full
        self.adapter._push_history(reply=full, segments=len(segments))
        self.adapter._log_conversation(reply=full, segments=len(segments))
        return KiraIMSentResult(str(uuid.uuid4()), ok=True)


class VRChatAdapter(BaseAdapter):
    def __init__(self, ctx: AdapterContext):
        super().__init__(ctx)
        self.im = self.register_capability(IMCapability, VRChatIMCapability(self))
        self._configure_access()
        self._tasks: list[asyncio.Task] = []
        self._hotkey: _HotkeyWatcher | None = None
        self._recorder = _Recorder()
        self._sender: _ChatboxSender | None = None
        self._state = "idle"  # idle -> recording -> transcribing -> sending
        self._state_since = time.time()
        # 状态代数：force_reset / 看门狗复位时自增；_ptt_up 进入时捕获，
        # 写 sending 前校验，不匹配说明中途被复位过，放弃本次识别结果
        self._generation = 0
        self._max_timer: asyncio.Task | None = None
        self._send_timeout_timer: asyncio.Task | None = None
        self._wav_dir = get_data_path() / "plugin_data" / PLUGIN_DATA_NAME / "tmp"
        self._log_file = get_data_path() / "plugin_data" / PLUGIN_DATA_NAME / "conversations.jsonl"
        self.vrchat_running = False
        self.oscq_reachable = False
        self.last_recognized = ""
        self.last_reply = ""
        self.last_error = ""
        self.history: list[dict] = []
        self._max_history = 20
        self._started_at = 0
        self._record_started_at = 0.0
        # Injected by the plugin in initialize() so cloud STT can reach
        # provider_mgr without a module-level PLUGIN_INSTANCE.
        # Signature: getter(model_uuid: str = "") -> STTModelClient | None
        #   empty model_uuid = KiraAI default STT; "provider_id:model_id" = specific.
        self._stt_provider = None
        # STT client resolved by the last _transcribe() call (for display).
        self._last_stt_client = None
        # start() 即运行协程（core/adapter/base.py:146-155），return 会被判定
        # run 结束并把实例从注册表摘除（adapter_registry.py:524-551），见 start() 末尾。
        self._stop_event: asyncio.Event | None = None

    def _configure_access(self) -> None:
        """Build im.* access policies from the instance config fields.

        Mirrors the schema: permission_mode + {group,user}_{allow,deny}_list.
        The shipped default (deny_list + empty lists) accepts everything —
        the only inbound source is this PC's PTT, same as v2.
        """
        mode = self.config.get("permission_mode", "deny_list")
        valid_mode = mode in ("allow_list", "deny_list")
        for scope, permission in (
            ("group", "im.group.receive"),
            ("user", "im.direct.receive"),
        ):
            allow_list = self.config.get(f"{scope}_allow_list", [])
            deny_list = self.config.get(f"{scope}_deny_list", [])
            policy = ListAccessPolicy.from_lists(
                mode if valid_mode else "deny_list",
                allow_list=allow_list if valid_mode and isinstance(allow_list, list) else [],
                deny_list=deny_list if valid_mode and isinstance(deny_list, list) else [],
            )
            self.access.set_policy(
                capability_type=IMCapability, permission=permission, policy=policy,
            )

    # ── lifecycle ──

    async def start(self):
        cfg = self.config
        self._wav_dir.mkdir(parents=True, exist_ok=True)
        self._started_at = int(time.time())
        self._sender = _ChatboxSender(
            port=int(cfg.get("osc_in_port", 9000)),
            interval=float(cfg.get("send_interval", 2)),
            typing_enabled=bool(cfg.get("typing_indicator", True)),
            on_drain=self._on_send_drain,
            read_cps=float(cfg.get("read_chars_per_second", 5) or 5),
            max_interval=float(cfg.get("max_segment_interval", 8) or 8),
        )
        self._sender.start()

        spec = cfg.get("hotkey", "b")
        loop = asyncio.get_running_loop()

        def spawn(coro):
            asyncio.create_task(coro)

        self._hotkey = _HotkeyWatcher(
            spec,
            on_down=lambda: loop.call_soon_threadsafe(spawn, self._on_hotkey_down()),
            on_up=lambda: loop.call_soon_threadsafe(spawn, self._on_hotkey_up()),
        )
        self._hotkey.start()
        logger.info(f"hotkey armed: {spec!r} ({cfg.get('ptt_mode', 'hold')} mode)")

        self._tasks.append(asyncio.create_task(self._probe_loop()))
        self._tasks.append(asyncio.create_task(self._state_watchdog()))
        logger.info("VRChat adapter started")

        # 契约：start() 返回即 run 结束，注册表会摘除实例（core/adapter/base.py:146-155、
        # adapter_registry.py:524-551）。挂起等待 stop() 置位或被管理器取消
        # （CancelledError 正常上抛）；每次 start 新建事件，防复用上一轮已 set 的对象。
        self._stop_event = asyncio.Event()
        await self._stop_event.wait()

    async def stop(self):
        if self._stop_event is not None:
            self._stop_event.set()
        for t in self._tasks:
            t.cancel()
        if self._max_timer:
            self._max_timer.cancel()
            self._max_timer = None
        if self._send_timeout_timer:
            self._send_timeout_timer.cancel()
            self._send_timeout_timer = None
        if self._hotkey:
            self._hotkey.stop()
        if self._sender:
            self._sender.set_typing(False)
            self._sender.clear_pending()
            await self._sender.stop()
        if self._state == "recording":
            try:
                self._recorder.stop_and_save(str(self._wav_dir / "abandoned.wav"))
            except Exception:
                pass
        self._set_state("idle")
        self._stt_provider = None
        logger.info("VRChat adapter stopped")

    def get_client(self):
        return self

    def set_stt_provider(self, getter):
        """Plugin injects a callable: getter(model_uuid='') -> STT client or None.

        Empty model_uuid uses KiraAI's default STT; a "provider_id:model_id"
        string selects a specific configured STT model.
        """
        self._stt_provider = getter

    def _set_state(self, state: str):
        if state != self._state:
            self._state = state
            self._state_since = time.time()

    async def _state_watchdog(self):
        """If recording/transcribing sticks around too long, force a reset."""
        while True:
            await asyncio.sleep(5)
            if self._state not in ("recording", "transcribing"):
                continue
            stuck_for = time.time() - self._state_since
            limit = 150 if self._state == "transcribing" else int(
                self.config.get("max_record_seconds", 60)
            ) + 30
            if stuck_for > limit:
                stuck = self._state
                logger.warning(
                    f"state watchdog: {stuck} stuck for {stuck_for:.0f}s, forcing reset"
                )
                # 复用 force_reset：recording 卡死时关麦、清定时器、关 typing，
                # 并自增状态代数让在途 _ptt_up 放弃晚到的识别结果
                self.force_reset()
                self.last_error = f"状态 {stuck} 超时，已自动复位"

    async def _probe_loop(self):
        import httpx

        while True:
            try:
                import psutil

                self.vrchat_running = any(
                    p.name() == "VRChat.exe" for p in psutil.process_iter(["name"])
                )
            except Exception:
                self.vrchat_running = False
            try:
                async with httpx.AsyncClient(timeout=3) as client:
                    r = await client.get("http://127.0.0.1:9002/")
                    self.oscq_reachable = r.status_code < 500
            except Exception:
                self.oscq_reachable = False
            await asyncio.sleep(30)

    # ── pipeline -> VRChat ──

    def _reply_prefix(self) -> str:
        # Chatbox messages appear under the local player's name — prefix so
        # bystanders can tell bot replies from the human's own chat.
        # Empty default: set to your bot nickname in adapter config (e.g. "yuki：").
        # 截断到 ≤100 字：前缀过长会把分段 limit（140-len）压到 ≤0 导致死循环
        return str(self.config.get("reply_prefix", "") or "")[:100]

    def _enqueue_reply(self, full: str) -> list[str]:
        prefix = self._reply_prefix()
        segments = _split_segments(full, limit=CHATBOX_LIMIT - len(prefix))
        self._sender.enqueue(segments, prefix=prefix)
        return segments

    def _build_message_event(self, text: str) -> KiraMessageEvent:
        now = int(time.time())
        return KiraMessageEvent(
            adapter=self.info,
            supported_elements=list(self.im._supported_elements),
            message=KiraIMMessage(
                message_id=str(uuid.uuid4()),
                self_id=str(self.info.name),
                chain=MessageChain([Text(text)]),
                timestamp=now,
                sender=User(
                    user_id=str(self.config.get("owner_id", "owner")),
                    nickname=self.config.get("owner_nickname", "主人"),
                ),
                is_mentioned=True,  # required by DefaultChatPlugin trigger logic
                raw_message={"source": "ptt"},
            ),
            timestamp=now,
        )

    # ── VRChat -> pipeline (PTT state machine) ──

    def _is_hold_mode(self) -> bool:
        return str(self.config.get("ptt_mode", "hold")).lower() != "toggle"

    def _hotkey_focus_ok(self) -> bool:
        """Whether the global hotkey should start recording right now."""
        if not bool(self.config.get("hotkey_require_focus", True)):
            return True
        want = str(self.config.get("hotkey_focus_process", "VRChat.exe") or "").strip()
        if not want:
            return True
        fg = _foreground_process_name()
        if not fg:
            return True  # fail open so PTT still works
        return fg.lower() == want.lower()

    async def _on_hotkey_down(self):
        if self._state == "recording" and not self._is_hold_mode():
            await self._ptt_up()
            return
        if not self._hotkey_focus_ok():
            logger.debug("hotkey ignored: foreground window is not VRChat")
            return
        if self._is_hold_mode():
            await self._ptt_down()
        else:
            if self._state == "idle":
                await self._ptt_down()
            elif self._state == "recording":
                await self._ptt_up()

    async def _on_hotkey_up(self):
        if self._is_hold_mode():
            await self._ptt_up()

    async def _ptt_down(self):
        if self._state != "idle":
            logger.debug(f"ptt_down ignored in state={self._state}")
            return
        try:
            self._recorder.start(self.config.get("mic_device", ""), gain=self._mic_gain())
        except Exception as e:
            self.last_error = f"recorder start failed: {e}"
            logger.error(self.last_error)
            return
        if self._recorder.last_warning:
            self.last_error = self._recorder.last_warning
        self._set_state("recording")
        self._record_started_at = time.time()
        timeout = int(self.config.get("max_record_seconds", 60))
        self._max_timer = asyncio.create_task(self._auto_stop(timeout))
        logger.info("recording...")

    async def _ptt_up(self):
        if self._state != "recording":
            return
        generation = self._generation
        if self._max_timer:
            self._max_timer.cancel()
            self._max_timer = None
        result = await self._stop_record_and_transcribe()
        if not result.get("ok"):
            logger.info(f"ASR skipped: {result.get('error', 'unknown')}")
            if self._sender:
                self._sender.set_typing(False)
            self._set_state("idle")
            return

        text = result.get("text") or ""
        if not text or len(text) < 2 or _is_hallucination(text):
            logger.info(f"ASR empty/hallucination: {text!r} {result.get('error', '')}".rstrip())
            if self._sender:
                self._sender.set_typing(False)
            self._set_state("idle")
            return

        if self._generation != generation:
            # 识别期间被 force_reset / 看门狗复位过：放弃本次结果，
            # 避免覆盖复位后的状态（此时可能已开始新一轮录音）
            logger.warning("ptt result dropped: state was reset during transcription")
            return

        self.last_recognized = text
        self._push_history(recognized=text)
        self._log_conversation(recognized=text)
        logger.info(f"recognized: {text}")
        self._publish_event(text)
        self._set_state("sending")
        self._arm_send_timeout()

    async def _stop_record_and_transcribe(self) -> dict:
        """Shared by PTT and status-page test: stop mic, save wav, STT, cleanup.

        Silence skip: when the recorded peak is below `silence_skip_dbfs`
        (0 disables the check) the clip is judged as no speech and the STT
        request is never sent — saves quota and avoids hallucinated text.
        On success the adapter is left in `transcribing` (caller decides next
        state). On failure the adapter is left in `idle`.
        """
        wav_path = str(self._wav_dir / f"{uuid.uuid4().hex}.wav")
        try:
            duration = self._recorder.stop_and_save(wav_path)
        except Exception as e:
            logger.error(f"recorder stop failed: {e}")
            self._set_state("idle")
            return {"ok": False, "error": f"recorder stop failed: {e}"}

        levels = {
            "peak_dbfs": round(self._recorder.last_peak_dbfs, 1),
            "rms_dbfs": round(self._recorder.last_rms_dbfs, 1),
            "device": self._recorder.last_device_name,
        }
        if duration < 0.5:
            Path(wav_path).unlink(missing_ok=True)
            self._set_state("idle")
            return {"ok": False, "error": "recording too short, discarded", **levels}

        # Silence skip: peak below threshold means nothing audible was captured.
        try:
            skip_threshold = float(self.config.get("silence_skip_dbfs", -55.0))
        except (TypeError, ValueError):
            skip_threshold = -55.0
        if skip_threshold != 0 and levels["peak_dbfs"] < skip_threshold:
            Path(wav_path).unlink(missing_ok=True)
            self.last_error = (
                f"输入电平过低（peak {levels['peak_dbfs']:.1f} dBFS），已跳过识别；"
                "请换麦克风或调大 gain"
            )
            logger.info(self.last_error)
            self._set_state("idle")
            return {"ok": False, "error": self.last_error, "skipped_silent": True, **levels}

        self._set_state("transcribing")
        if self._sender:
            self._sender.set_typing(True)
        try:
            text = await self._transcribe(wav_path)
        except asyncio.TimeoutError:
            timeout = self._stt_timeout_seconds()
            self.last_error = (
                f"STT 超时（{timeout}s）：{self._stt_display_name() or '未配置 STT 模型'}"
            )
            logger.error(self.last_error)
            self._set_state("idle")
            return {"ok": False, "error": self.last_error}
        except Exception as e:
            # All STT failures (timeout / 401 / quota / network) surface here:
            # never send anything, never guess text — log + status page only.
            self.last_error = f"STT 失败：{_summarize_error(e)}"
            logger.error(self.last_error)
            self._set_state("idle")
            return {"ok": False, "error": self.last_error}
        finally:
            Path(wav_path).unlink(missing_ok=True)

        text = _strip_noise(text)
        return {"ok": True, "text": text, "duration": round(duration, 2), **levels}

    async def _auto_stop(self, seconds: int):
        await asyncio.sleep(seconds)
        logger.warning(f"recording exceeded {seconds}s, auto-stopping")
        await self._ptt_up()

    def _arm_send_timeout(self):
        """If the pipeline never calls send_direct_message, recover to idle."""
        self._cancel_send_timeout()
        seconds = int(self.config.get("send_timeout_seconds", 120))

        async def _expire():
            await asyncio.sleep(seconds)
            if self._state == "sending":
                self.last_error = f"pipeline reply timed out after {seconds}s"
                logger.warning(self.last_error)
                if self._sender:
                    self._sender.set_typing(False)
                self._set_state("idle")

        self._send_timeout_timer = asyncio.create_task(_expire())

    def _cancel_send_timeout(self):
        if self._send_timeout_timer:
            self._send_timeout_timer.cancel()
            self._send_timeout_timer = None

    def _on_send_drain(self):
        """Chatbox queue emptied — return to idle so the next PTT is accepted."""
        self._cancel_send_timeout()
        if self._state == "sending":
            self._set_state("idle")

    def _publish_event(self, text: str):
        self.im.publish_transcription(text)

    # ── test hooks (status page) ──

    async def test_send(self, text: str = "") -> dict:
        """Push a short message into the chatbox (no LLM round-trip)."""
        msg = text or f"[VRCLink 测试] {time.strftime('%H:%M:%S')}"
        segments = self._enqueue_reply(msg)
        self.last_reply = msg
        self._log_conversation(reply=msg, segments=len(segments))
        return {"ok": True, "text": msg, "segments": segments}

    async def test_record(self, seconds: float = 3.0) -> dict:
        """Record N seconds and run ASR — does not publish to the pipeline."""
        seconds = max(1.0, min(15.0, float(seconds)))
        if self._state != "idle":
            logger.warning(f"test_record rejected: adapter busy ({self._state})")
            return {"ok": False, "error": f"adapter busy: {self._state}"}
        logger.info(f"test_record: recording {seconds}s")
        try:
            self._recorder.start(self.config.get("mic_device", ""), gain=self._mic_gain())
        except Exception as e:
            logger.error(f"test_record recorder start failed: {e}")
            return {"ok": False, "error": f"recorder start failed: {e}"}
        if self._recorder.last_warning:
            self.last_error = self._recorder.last_warning
        self._set_state("recording")
        self._record_started_at = time.time()
        try:
            await asyncio.sleep(seconds)
        except asyncio.CancelledError:
            try:
                self._recorder.stop_and_save(str(self._wav_dir / "cancelled.wav"))
            except Exception:
                pass
            self._set_state("idle")
            raise

        t0 = time.time()
        result = await self._stop_record_and_transcribe()
        elapsed_ms = int(round((time.time() - t0) * 1000))
        if not result.get("ok"):
            logger.info(f"test_record failed: {result.get('error')}")
            result.setdefault("elapsed_ms", elapsed_ms)
            result.setdefault("stt_display", self._stt_display_name())
            return result

        text = result.get("text") or ""
        logger.info(
            f"test_record done: {text!r} peak={result.get('peak_dbfs')} dBFS "
            f"stt={self._stt_display_name() or 'default'} {elapsed_ms}ms"
        )
        self._set_state("idle")
        return {
            "ok": True,
            "duration": result.get("duration", 0),
            "text": text,
            "elapsed_ms": elapsed_ms,
            "stt_display": self._stt_display_name(),
            "peak_dbfs": result.get("peak_dbfs"),
            "rms_dbfs": result.get("rms_dbfs"),
            "device": result.get("device", ""),
            "warning": self._recorder.last_warning,
            "error": result.get("error", ""),
        }

    def force_reset(self) -> dict:
        """Recover from a stuck state (status page '强制复位')."""
        prev = self._state
        if self._max_timer:
            self._max_timer.cancel()
            self._max_timer = None
        self._cancel_send_timeout()
        if prev == "recording":
            try:
                self._recorder.stop_and_save(str(self._wav_dir / "force_reset.wav"))
            except Exception:
                pass
        try:
            if self._sender:
                self._sender.set_typing(False)
        except Exception:
            pass
        # 代数 +1：让复位前进入的在途 _ptt_up 放弃本次识别结果
        self._generation += 1
        self._set_state("idle")
        self.last_error = ""
        logger.warning(f"force_reset: {prev} -> idle")
        return {"ok": True, "previous_state": prev, "state": self._state}

    # ── mic diagnostics (status page) ──

    def _mic_gain(self) -> float:
        """Configured mic gain, clamped to the schema range (0.5..20)."""
        try:
            gain = float(self.config.get("mic_gain", 1.0))
        except (TypeError, ValueError):
            gain = 1.0
        if gain <= 0:
            gain = 1.0
        return min(20.0, max(0.5, gain))

    def _mic_device_resolution(self) -> dict:
        """当前 mic_device 配置值的解析结果，供状态页展示。

        只查询设备表（不开流、不录音），因此可以随 /status 轮询；解析产生
        的「未找到设备」警告在这里只随数据返回，不打日志（避免轮询刷屏），
        真正开麦时的警告由 _Recorder.start 记录。
        """
        value = str(self.config.get("mic_device", "") or "")
        try:
            import sounddevice as sd
            devices = sd.query_devices()
        except Exception as e:
            return {"configured": value, "index": None, "name": "", "how": "error", "warning": str(e)}
        idx, how, warning = resolve_mic_device(value, devices)
        name = ""
        try:
            if idx is not None:
                name = str(sd.query_devices(idx).get("name", "") or "")
            else:
                dflt = int(sd.default.device[0])
                if dflt >= 0:
                    name = str(sd.query_devices(dflt).get("name", "") or "")
        except Exception:
            pass
        return {"configured": value, "index": idx, "name": name, "how": how, "warning": warning}

    def list_input_devices(self) -> dict:
        try:
            return {"ok": True, "devices": _Recorder.list_input_devices()}
        except Exception as e:
            logger.error(f"list_input_devices failed: {e}")
            return {"ok": False, "error": f"设备枚举失败: {e}", "devices": []}

    async def probe_device(self, device: str = "", seconds: float = 2.0, gain: float = 0.0) -> dict:
        """Record a short clip on the given device — level metering only, no ASR.

        gain <= 0 means "use the configured mic_gain".
        """
        seconds = max(1.0, min(10.0, float(seconds or 2.0)))
        if not gain or float(gain) <= 0:
            gain = self._mic_gain()
        else:
            gain = min(20.0, max(0.5, float(gain)))
        if self._state != "idle":
            return {"ok": False, "error": f"adapter busy: {self._state}", "device": device}
        # 状态页可能在适配器 start() 之前就点到「测电平」：先确保临时目录存在
        try:
            self._wav_dir.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            logger.debug(f"probe_device tmp dir ensure failed: {e}")
        rec = _Recorder()
        try:
            rec.start(device, gain=gain)
        except Exception as e:
            logger.warning(f"probe_device open failed ({device!r}): {e}")
            return {
                "ok": False,
                "device": device,
                "error": f"无法以 16 kHz 打开设备「{device or '系统默认'}」: {e}",
            }
        try:
            await asyncio.sleep(seconds)
        except asyncio.CancelledError:
            try:
                rec.stop_and_save(str(self._wav_dir / "probe_cancel.wav"))
            except Exception:
                pass
            raise
        wav_path = str(self._wav_dir / "probe.wav")
        try:
            rec.stop_and_save(wav_path)
        except Exception as e:
            logger.error(f"probe_device stop failed: {e}")
            return {"ok": False, "device": device, "error": f"recorder stop failed: {e}"}
        Path(wav_path).unlink(missing_ok=True)
        return {
            "ok": True,
            "device": rec.last_device_name,
            "device_index": rec.last_device_index,
            "seconds": round(seconds, 2),
            "gain": gain,
            "peak_dbfs": round(rec.last_peak_dbfs, 1),
            "rms_dbfs": round(rec.last_rms_dbfs, 1),
            "clipping": rec.last_clipping,
            "warning": rec.last_warning,
        }

    # ── STT dispatch (cloud only) ──

    def _stt_timeout_seconds(self) -> int:
        try:
            return max(5, min(120, int(self.config.get("stt_timeout_seconds", 20))))
        except (TypeError, ValueError):
            return 20

    def _stt_display_name(self) -> str:
        """Human-readable name of the last resolved STT client, for errors."""
        client = getattr(self, "_last_stt_client", None)
        if client is None:
            return ""
        info = getattr(client, "model", None)
        if info is None:
            return ""
        parts = [getattr(info, "model_name", "") or "", getattr(info, "provider_name", "") or ""]
        return "（".join(p for p in parts if p).join(["", "）"]) if any(parts) else ""

    def _resolve_stt_display_name(self) -> str:
        """Resolve 'Model（Provider）' once, so the status page never shows a raw uuid.

        Uses the injected getter (same path as recognition) and caches the
        first success; failures fall back to the configured value elsewhere.
        """
        cached = getattr(self, "_stt_display_cache", "")
        if cached:
            return cached
        getter = self._stt_provider or _STT_GETTER
        if not getter:
            return ""
        try:
            client = getter(str(self.config.get("stt_model", "") or "").strip())
        except Exception:
            return ""
        info = getattr(client, "model", None) if client is not None else None
        if info is None:
            return ""
        name = getattr(info, "model_name", "") or ""
        provider = getattr(info, "provider_name", "") or ""
        text = f"{name}（{provider}）" if name and provider else (name or provider)
        if text:
            self._stt_display_cache = text
        return text

    async def _transcribe(self, wav_path: str) -> str:
        # Instance-level getter first; fall back to the module-level one
        # (config hot-reload rebuilds instances and drops the instance getter).
        getter = self._stt_provider or _STT_GETTER
        if not getter:
            raise RuntimeError(
                "STT unavailable — restart the bot after enabling the plugin"
            )
        # Per-adapter override first; empty falls back to KiraAI's default STT.
        preferred = str(self.config.get("stt_model", "") or "").strip()
        try:
            stt = getter(preferred)
        except TypeError:
            stt = getter()  # older getter with no-arg signature
        if stt is None:
            raise RuntimeError(
                "no STT model — configure one in KiraAI (Configuration → Models) "
                "or pick one in this adapter"
            )
        self._last_stt_client = stt
        timeout = self._stt_timeout_seconds()
        return await asyncio.wait_for(
            stt.speech_to_text(Record(record=wav_path)), timeout=timeout,
        )

    # ── history / status ──

    def _push_history(self, recognized: str = "", reply: str = "", segments: int = 0):
        entry = {"ts": int(time.time())}
        if recognized:
            entry["recognized"] = recognized
        if reply:
            entry["reply"] = reply
            entry["segments"] = segments
        if reply and self.history and "recognized" in self.history[-1] and "reply" not in self.history[-1]:
            self.history[-1].update(entry)
        else:
            self.history.append(entry)
        if len(self.history) > self._max_history:
            self.history = self.history[-self._max_history:]

    def _log_conversation(self, recognized: str = "", reply: str = "", segments: int = 0):
        if not self.config.get("log_conversations", True):
            return
        try:
            entry = {"ts": int(time.time()), "recognized": recognized, "reply": reply, "segments": segments}
            with open(self._log_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception as e:
            logger.debug(f"conversation log failed: {e}")

    def get_status(self) -> dict:
        record_elapsed = 0.0
        if self._state == "recording" and self._record_started_at:
            record_elapsed = round(time.time() - self._record_started_at, 1)
        configured_model = str(self.config.get("stt_model", "") or "").strip()
        resolved = self._stt_display_name() or self._resolve_stt_display_name()
        if resolved:
            stt_display = resolved
        elif configured_model:
            stt_display = "已指定（尚未识别过）"
        else:
            stt_display = "未配置（将用全局默认）"
        return {
            "adapter_name": str(self.info.name),
            "platform": str(self.info.platform),
            "state": self._state,
            "ptt_mode": self.config.get("ptt_mode", "hold"),
            "hotkey": self.config.get("hotkey", "b"),
            "hotkey_require_focus": bool(self.config.get("hotkey_require_focus", True)),
            "hotkey_focus_process": self.config.get("hotkey_focus_process", "VRChat.exe"),
            "foreground_process": _foreground_process_name(),
            "vrchat_running": self.vrchat_running,
            "oscq_reachable": self.oscq_reachable,
            "stt_model": configured_model,
            "stt_display": stt_display,
            "stt_timeout_seconds": self._stt_timeout_seconds(),
            "pending_segments": self._sender.pending if self._sender else 0,
            "sent_count": self._sender.sent_count if self._sender else 0,
            "record_elapsed": record_elapsed,
            "max_record_seconds": int(self.config.get("max_record_seconds", 60)),
            "mic_device": str(self.config.get("mic_device", "") or ""),
            "mic_device_resolved": self._mic_device_resolution(),
            "mic_device_name": self._recorder.last_device_name,
            "mic_gain": self._mic_gain(),
            "last_peak_dbfs": round(self._recorder.last_peak_dbfs, 1),
            "last_rms_dbfs": round(self._recorder.last_rms_dbfs, 1),
            "last_clipping": self._recorder.last_clipping,
            "send_interval": float(self.config.get("send_interval", 2)),
            "read_chars_per_second": float(self.config.get("read_chars_per_second", 5) or 5),
            "max_segment_interval": float(self.config.get("max_segment_interval", 8) or 8),
            "reply_prefix": self._reply_prefix(),
            "last_recognized": self.last_recognized,
            "last_reply": self.last_reply,
            "last_error": self.last_error,
            "history": self.history[-5:],
            "uptime_seconds": int(time.time()) - self._started_at if self._started_at else 0,
        }
