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
        -> ASR (faster-whisper locally, or KiraAI default STT provider)
        -> IMCapability.publish -> KiraMessageEvent(adapter, dm, is_mentioned=True)
"""

import asyncio
import json
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

# Whisper output sometimes hallucinates these on silence / short clips.
_HALLUCINATION_PATTERNS = (
    "谢谢观看", "感谢观看", "请订阅", "字幕由", "字幕by", "by amara.org",
    "subtitles by", "谢谢收看", "请点赞",
)

CHATBOX_LIMIT = 140  # VRChat hard limit is 144; leave headroom
_HF_MIRROR = "https://hf-mirror.com"
# Plugin data folder name under <data>/plugin_data/ (stable across bots).
PLUGIN_DATA_NAME = "vrclink"


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


def _is_hallucination(text: str) -> bool:
    low = text.lower()
    return any(p in low for p in _HALLUCINATION_PATTERNS)


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

class _Recorder:
    """16 kHz mono int16 wav recorder built on sounddevice.InputStream."""

    SAMPLE_RATE = 16000

    def __init__(self):
        self._stream = None
        self._frames: list = []

    def start(self, device_substring: str = ""):
        import sounddevice as sd

        kwargs = {
            "samplerate": self.SAMPLE_RATE,
            "channels": 1,
            "dtype": "int16",
            "callback": self._on_audio,
        }
        if device_substring:
            idx = self._find_device(sd, device_substring)
            if idx is None:
                raise ValueError(f"microphone device not found: {device_substring!r}")
            kwargs["device"] = idx
        self._frames = []
        self._stream = sd.InputStream(**kwargs)
        self._stream.start()

    @staticmethod
    def _find_device(sd, substring: str):
        try:
            for dev in sd.query_devices():
                if substring.lower() in dev["name"].lower() and dev["max_input_channels"] > 0:
                    return dev["index"]
        except Exception:
            pass
        return None

    def _on_audio(self, indata, frames, time_info, status):
        self._frames.append(indata.copy())

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


# ─────────────────────────── ASR backends ───────────────────────────

def _apply_hf_endpoint(endpoint: str):
    """Point huggingface_hub at a mirror when huggingface.co is unreachable.

    Must be set before the first snapshot_download / model load.
    Xet transport often 401s through mirrors, so disable it unless the user
    opted in via HF_HUB_DISABLE_XET=0.
    """
    endpoint = (endpoint or "").strip()
    if endpoint:
        os.environ["HF_ENDPOINT"] = endpoint.rstrip("/")
    elif "HF_ENDPOINT" not in os.environ:
        os.environ["HF_ENDPOINT"] = _HF_MIRROR
    if os.environ.get("HF_HUB_DISABLE_XET", "") not in ("0", "false", "False"):
        os.environ["HF_HUB_DISABLE_XET"] = "1"


_CUDA_DLL_PATHS_APPLIED = False

# ctranslate2 links these CUDA runtime DLLs at inference time. Names match the
# pip packages in the bot venv (nvidia-cublas-cu12 / nvidia-cudnn-cu12 /
# nvidia-cuda-nvrtc-cu12); each load checks existence first, so a version bump
# that renames a file just skips it and degrades to the CPU fallback as before.
_CTRANSLATE2_CUDA_DLLS = (
    "cublas64_12.dll",
    "cublasLt64_12.dll",
    "cudnn64_9.dll",
    "nvrtc64_120_0.dll",
)


def _ensure_cuda_dll_paths():
    """Register NVIDIA DLL dirs and preload what ctranslate2 links against.

    os.add_dll_directory alone is NOT enough: it only affects loaders using
    LOAD_LIBRARY_SEARCH_USER_DIRS (i.e. Python's own extension imports).
    ctranslate2 resolves cublas64_12.dll etc. with its own plain LoadLibrary
    call, which ignores those dirs — hence the explicit preload by full path.
    Once a DLL is loaded in-process, ctranslate2's by-name LoadLibrary finds
    it via the loaded-module cache without any path search. cudnn64_9.dll's
    sub-DLLs (cudnn_ops64_9.dll & co.) resolve from its own directory because
    WinDLL on an absolute path implies LOAD_LIBRARY_SEARCH_DLL_LOAD_DIR.
    """
    global _CUDA_DLL_PATHS_APPLIED
    if _CUDA_DLL_PATHS_APPLIED:
        return
    import ctypes
    import sys

    bins: list[Path] = []
    try:
        sp = Path(sys.prefix) / "Lib" / "site-packages" / "nvidia"
        if sp.is_dir():
            for sub in sp.iterdir():
                b = sub / "bin"
                if b.is_dir():
                    bins.append(b)
    except Exception:
        pass

    extra = os.environ.get("VRCHAT_BRIDGE_CUDA_BIN", "")
    if extra and Path(extra).is_dir():
        bins.append(Path(extra))

    for b in bins:
        try:
            os.add_dll_directory(str(b))
        except Exception as e:
            logger.debug(f"add_dll_directory({b}) failed: {e}")

    for b in bins:
        for name in _CTRANSLATE2_CUDA_DLLS:
            p = b / name
            if not p.is_file():
                continue
            try:
                ctypes.WinDLL(str(p))
                logger.debug(f"preloaded CUDA dll: {p}")
            except Exception as e:
                logger.debug(f"CUDA dll preload failed ({p}): {e}")

    if bins:
        _CUDA_DLL_PATHS_APPLIED = True
        logger.debug(f"CUDA DLL dirs registered: {[str(b) for b in bins]}")
    else:
        logger.warning("no pip NVIDIA bin dirs found; GPU inference may fail")


class _LocalWhisper:
    """faster-whisper backend with lazy model load and CUDA->CPU fallback."""

    def __init__(
        self,
        model_name: str,
        compute: str,
        language: str,
        hf_endpoint: str = _HF_MIRROR,
        download_root: str = "",
    ):
        self.model_name = model_name
        self.compute = compute
        self.language = language
        self.hf_endpoint = hf_endpoint
        self.download_root = download_root
        self._model = None
        self._device_used = ""
        self.status = "not_loaded"  # not_loaded | loading | loaded | failed
        self.last_error = ""
        self._loading = False

    def _resolve_model_path(self) -> str:
        """Local dir path, HF cache hit, or download from the Hub."""
        if self.model_name and Path(self.model_name).is_dir():
            return self.model_name

        from faster_whisper.utils import available_models

        if self.model_name not in available_models():
            return self.model_name

        try:
            import huggingface_hub
            from faster_whisper.utils import _MODELS

            repo_id = _MODELS.get(self.model_name, self.model_name)
            path = huggingface_hub.snapshot_download(
                repo_id=repo_id,
                cache_dir=self.download_root or None,
                local_files_only=True,
            )
            logger.info(f"whisper model cache hit: {path}")
            return path
        except Exception:
            pass

        from faster_whisper.utils import download_model

        try:
            return download_model(
                self.model_name,
                cache_dir=self.download_root or None,
                local_files_only=False,
            )
        except Exception as e:
            raise RuntimeError(
                f"模型下载失败（{self.model_name}）：{e}。"
                f"当前 HF_ENDPOINT={os.environ.get('HF_ENDPOINT', '')}。"
                "可把 hf_endpoint 设为 https://hf-mirror.com，"
                "或改用较小模型（small/medium），或切换 asr_backend=cloud。"
            ) from e

    def _load_on(self, path: str, device: str, compute: str):
        from faster_whisper import WhisperModel

        kwargs = {}
        if self.download_root:
            kwargs["download_root"] = self.download_root
        self._model = WhisperModel(path, device=device, compute_type=compute, **kwargs)
        self._device_used = f"{device}/{compute}"

    def _warmup(self) -> bool:
        """Tiny inference so missing CUDA libs surface at load time."""
        import struct
        import tempfile

        try:
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
                tmp = f.name
            frames = b"".join(struct.pack("<h", 0) for _ in range(int(16000 * 0.3)))
            with wave.open(tmp, "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(16000)
                w.writeframes(frames)
            segs, _ = self._model.transcribe(tmp, beam_size=1, vad_filter=False)
            list(segs)
            Path(tmp).unlink(missing_ok=True)
            return True
        except Exception as e:
            logger.warning(f"whisper warmup failed: {e}")
            return False

    def _load(self):
        if self._model is not None:
            return
        if self._loading:
            raise RuntimeError("model load already in progress")
        self._loading = True
        self.status = "loading"
        self.last_error = ""
        try:
            _apply_hf_endpoint(self.hf_endpoint)
            _ensure_cuda_dll_paths()
            path = self._resolve_model_path()
            try:
                self._load_on(path, "cuda", self.compute)
                if not self._warmup():
                    raise RuntimeError("CUDA warmup inference failed")
            except Exception as e:
                logger.warning(f"CUDA load failed ({e}); falling back to CPU int8")
                self._model = None
                self._load_on(path, "cpu", "int8")
            self.status = "loaded"
            logger.info(f"whisper model loaded: {self.model_name} on {self._device_used}")
        except Exception as e:
            self.status = "failed"
            if not self.last_error:
                self.last_error = str(e)
            self._model = None
            raise
        finally:
            self._loading = False

    def _fallback_to_cpu(self, reason: str):
        """Drop the CUDA model and reload on CPU (missing cublas etc.)."""
        logger.warning(f"switching whisper to CPU: {reason}")
        self._model = None
        self._device_used = ""
        self.status = "loading"
        try:
            _apply_hf_endpoint(self.hf_endpoint)
            _ensure_cuda_dll_paths()
            path = self._resolve_model_path()
            self._load_on(path, "cpu", "int8")
            self.status = "loaded"
            self.last_error = ""
            logger.info(f"whisper reloaded on CPU after: {reason}")
        except Exception as e:
            self.status = "failed"
            self.last_error = str(e)
            self._model = None
            raise

    async def transcribe(self, wav_path: str) -> str:
        if self._model is None:
            await asyncio.to_thread(self._load)

        def _run() -> str:
            kwargs = {"beam_size": 5, "vad_filter": True}
            if self.language and self.language.lower() != "auto":
                kwargs["language"] = self.language
            try:
                segments, _info = self._model.transcribe(wav_path, **kwargs)
                return "".join(s.text for s in segments).strip()
            except Exception as e:
                msg = str(e).lower()
                if any(k in msg for k in ("cublas", "cudnn", "cuda", "dll")):
                    self._fallback_to_cpu(str(e))
                    segments, _info = self._model.transcribe(wav_path, **kwargs)
                    return "".join(s.text for s in segments).strip()
                raise

        return await asyncio.to_thread(_run)


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
        self._whisper: _LocalWhisper | None = None
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

        if (cfg.get("asr_backend", "local")) == "local":
            self._whisper = self._make_whisper()
            self._tasks.append(asyncio.create_task(self._preload_whisper()))

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
        self._whisper = None  # free VRAM
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

    def _make_whisper(self) -> _LocalWhisper:
        cfg = self.config
        return _LocalWhisper(
            model_name=cfg.get("whisper_model", "large-v3-turbo"),
            compute=cfg.get("whisper_compute", "int8_float16"),
            language=cfg.get("whisper_language", "zh"),
            hf_endpoint=str(cfg.get("hf_endpoint", _HF_MIRROR) or ""),
            download_root=str(cfg.get("whisper_download_root", "") or ""),
        )

    def _asr_load_timeout(self) -> int:
        try:
            return max(30, min(1800, int(self.config.get("asr_load_timeout_seconds", 600))))
        except Exception:
            return 600

    async def _preload_whisper(self):
        timeout = self._asr_load_timeout()
        try:
            await asyncio.wait_for(asyncio.to_thread(self._whisper._load), timeout=timeout)
        except asyncio.TimeoutError:
            logger.error(f"whisper preload timed out after {timeout}s")
            self.last_error = f"whisper 模型加载超时（{timeout}s），首次识别时会重试"
            self._whisper._model = None
            self._whisper.status = "failed"
            self._whisper.last_error = self.last_error
        except Exception as e:
            logger.error(f"whisper preload failed (will retry on first use): {e}")
            self._whisper._model = None
            self._whisper.status = "failed"
            self._whisper.last_error = str(e)

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
            self._recorder.start(self.config.get("mic_device", ""))
        except Exception as e:
            self.last_error = f"recorder start failed: {e}"
            logger.error(self.last_error)
            return
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
            logger.info(f"ASR empty/hallucination: {text!r}")
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
        """Shared by PTT and status-page test: stop mic, save wav, ASR, cleanup.

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

        if duration < 0.5:
            Path(wav_path).unlink(missing_ok=True)
            self._set_state("idle")
            return {"ok": False, "error": "recording too short, discarded"}

        self._set_state("transcribing")
        if self._sender:
            self._sender.set_typing(True)
        try:
            text = await self._transcribe(wav_path)
        except asyncio.TimeoutError:
            self.last_error = "ASR 超时（模型下载或推理过久）"
            logger.error(self.last_error)
            self._set_state("idle")
            return {"ok": False, "error": self.last_error}
        except Exception as e:
            self.last_error = f"ASR failed: {e}"
            logger.error(self.last_error)
            self._set_state("idle")
            return {"ok": False, "error": self.last_error}
        finally:
            Path(wav_path).unlink(missing_ok=True)

        return {"ok": True, "text": _strip_noise(text), "duration": round(duration, 2)}

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
            self._recorder.start(self.config.get("mic_device", ""))
        except Exception as e:
            logger.error(f"test_record recorder start failed: {e}")
            return {"ok": False, "error": f"recorder start failed: {e}"}
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

        result = await self._stop_record_and_transcribe()
        if not result.get("ok"):
            logger.info(f"test_record failed: {result.get('error')}")
            return result

        text = result.get("text") or ""
        logger.info(f"test_record done: {text!r}")
        self._set_state("idle")
        return {"ok": True, "duration": result.get("duration", 0), "text": text}

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

    # ── ASR dispatch ──

    async def _transcribe(self, wav_path: str) -> str:
        if self.config.get("asr_backend", "local") == "cloud":
            return await self._transcribe_cloud(wav_path)
        if self._whisper is None:
            self._whisper = self._make_whisper()
        timeout = self._asr_load_timeout() if self._whisper._model is None else 120
        return await asyncio.wait_for(self._whisper.transcribe(wav_path), timeout=timeout)

    async def _transcribe_cloud(self, wav_path: str) -> str:
        # 实例级 getter 优先；为空（配置热更新重建后的新实例）回退模块级兜底
        getter = self._stt_provider or _STT_GETTER
        if not getter:
            raise RuntimeError(
                "cloud STT unavailable — restart the bot after enabling the plugin, "
                "or switch asr_backend to local"
            )
        # Prefer the per-adapter override; empty falls back to KiraAI default STT.
        preferred = str(self.config.get("cloud_stt_model", "") or "").strip()
        try:
            stt = getter(preferred)
        except TypeError:
            # Older getter with no-arg signature.
            stt = getter()
        if stt is None:
            raise RuntimeError(
                "no STT model available — configure a default STT in KiraAI "
                "(Configuration -> Models), or pick a cloud STT model in the adapter, "
                "or switch asr_backend to local"
            )
        return await stt.speech_to_text(Record(record=wav_path))

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
        model_status = ""
        model_error = ""
        if self._whisper is not None:
            model_status = getattr(self._whisper, "status", "")
            model_error = getattr(self._whisper, "last_error", "") or ""
        backend = self.config.get("asr_backend", "local")
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
            "backend": backend,
            "cloud_stt_model": self.config.get("cloud_stt_model", "") if backend == "cloud" else "",
            "whisper_device": getattr(self._whisper, "_device_used", "") if self._whisper else "",
            "whisper_model": self.config.get("whisper_model", "large-v3-turbo") if backend == "local" else "",
            "whisper_status": model_status,
            "whisper_error": model_error,
            "hf_endpoint": self.config.get("hf_endpoint", _HF_MIRROR),
            "pending_segments": self._sender.pending if self._sender else 0,
            "sent_count": self._sender.sent_count if self._sender else 0,
            "record_elapsed": record_elapsed,
            "max_record_seconds": int(self.config.get("max_record_seconds", 60)),
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
