"""
VRCLink — push-to-talk voice interface to the bot inside VRChat.

Registers the "VRChat" adapter (adapter/adapter.py):
    PTT hotkey (keyboard, or VR controller via OVR Advanced Settings Key Press)
    -> record mic -> ASR (KiraAI STT provider; cloud only since 1.0.0)
    -> message pipeline (persona, memory, tools all apply)
    -> reply Text -> OSC chatbox segments in VRChat.

Config lives on the adapter instance (WebUI -> Adapters -> VRChat instance).
"""

import html
import importlib.util
import sys
from pathlib import Path

from core.plugin import BasePlugin, logger, on, register, PluginPage, PageMenu
from core.prompt_manager import Prompt

# Injected once per LLM request so the model understands the VRChat medium:
# voice PTT (ASR noise) in, public chatbox out (whole room can read).
_VRCHAT_CONTEXT_PROMPT = """\
## VRChat 聊天环境（重要）
- 当前对话发生在 VRChat 游戏内。用户通过按住按键说话（语音转文字）与你交流，识别结果可能有错字或同音字，按语义理解即可，不要纠结错别字。
- 你的回复会发送到 VRChat 聊天框（chatbox），**全房间可见**，不是私密消息。避免只适合私聊的过度亲昵、长篇独白或敏感私事。
- 回复要快：先给一句不超过 30 字的直接回答，再按需要补一句；不要展开推理、不要铺垫、不要复述问题。
- 适合游戏内聊天框快速阅读；确实需要长内容时拆成几个自然短句（系统会自动分段发送）。
- 你看不到房间里其他玩家，也不知道具体世界/实例名称；若用户提到场景，按TA描述的内容回应即可。
"""


_PLUGIN_ROOT = Path(__file__).resolve().parent


def _load_adapter_component():
    """按路径加载 adapter/adapter.py（私有模块名），供 schema 刷新使用。

    框架注册适配器时会用自己的模块名再加载一份，两份互不影响：这里的
    副本只用于在注册前调用 refresh_mic_device_options，运行期实例由框架
    副本承载。加载失败返回 None，调用方自行兜底。
    """
    name = "_vrclink_adapter_component"
    mod = sys.modules.get(name)
    if mod is not None:
        return mod
    script = _PLUGIN_ROOT / "adapter" / "adapter.py"
    if not script.is_file():
        return None
    try:
        spec = importlib.util.spec_from_file_location(name, script)
        if spec is None or spec.loader is None:
            return None
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
        return module
    except Exception as e:
        sys.modules.pop(name, None)
        logger.debug(f"VRCLink: load adapter component for schema refresh failed: {e}")
        return None


class VRCLinkPlugin(BasePlugin):
    async def initialize(self):
        # 1.0.0 is cloud-STT only. Migrate old adapter configs (carry
        # cloud_stt_model -> stt_model, drop local-ASR keys) BEFORE the
        # adapter instance is registered, so the new instance starts clean.
        try:
            self._migrate_legacy_asr_config()
        except Exception as e:
            logger.warning(f"VRCLink: legacy ASR config migration failed (ignored): {e}")
        # 框架只在 register_adapter 时静态读一次 schema.json（无动态选项
        # 机制），所以必须抢在注册前把本机麦克风列表写进 mic_device.options，
        # WebUI 才能把该字段渲染成下拉。失败绝不能阻断插件初始化。
        try:
            self._refresh_schema_mic_options()
        except Exception as e:
            logger.warning(f"VRCLink: refresh mic_device options failed (ignored): {e}")
        # Config hot-reload re-runs initialize(); re-registering the same
        # platform path is a no-op, only surface unexpected failures.
        try:
            await self.ctx.register_adapter("adapter")
        except ValueError as e:
            if "already" in str(e).lower():
                logger.debug(f"VRChat adapter platform already registered: {e}")
            else:
                raise
        # Adapter has no PluginContext; inject STT getter (cloud backend).
        ada = self._find_adapter()
        if ada is not None and hasattr(ada, "set_stt_provider"):
            ada.set_stt_provider(self._get_stt)
        # 同时写一份到适配器模块的模块级变量：适配器配置热更新会重建实例
        # （实例级 getter 随之丢失），新实例会回退到模块级兜底。
        if ada is not None:
            adapter_mod = sys.modules.get(type(ada).__module__)
            if adapter_mod is not None and hasattr(adapter_mod, "set_stt_getter"):
                adapter_mod.set_stt_getter(self._get_stt)
        logger.info("VRCLink plugin initialized")

    async def terminate(self):
        # The adapter registered via ctx is auto-unregistered by KiraAI.
        logger.info("VRCLink plugin terminated")

    # ── LLM context injection ──

    @on.llm_request()
    async def inject_vrchat_context(self, event, req, tag_set, *args, **kwargs):
        """Tell the model this turn is VRChat voice -> public chatbox."""
        adapter = getattr(event, "adapter", None)
        platform = str(getattr(adapter, "platform", "") or "")
        if platform != "VRChat":
            return
        # Optional tool suppression: a tool call costs a whole extra LLM
        # round-trip before any reply appears, which dominates the wait in
        # voice chat. Defaults to on; QQ and other platforms are unaffected.
        if self._tools_disabled_in_game():
            # Empty the tool list but KEEP the ToolSet object: the assemble
            # stage calls `req.tool_set.to_list()` right after this hook, so
            # setting tool_set=None raises AttributeError and the turn hangs
            # until the adapter's send timeout. Never let this break a turn.
            try:
                req.tool_choice = "none"
                tool_set = getattr(req, "tool_set", None)
                if tool_set is not None and hasattr(tool_set, "tools"):
                    tool_set.tools = []
                else:
                    req.tools = []
                req.tool_funcs = {}
            except Exception as e:  # fail open: a stuck turn is worse than tools
                logger.warning(f"disable_tools skipped: {e}")
        req.system_prompt.append(Prompt(
            content=_VRCHAT_CONTEXT_PROMPT,
            name="vrclink_context",
            source="vrclink",
        ))

    def _tools_disabled_in_game(self) -> bool:
        """Read `disable_tools` from the VRChat adapter instance (default True)."""
        ada = self._find_adapter()
        cfg = getattr(ada, "config", None) if ada is not None else None
        if not isinstance(cfg, dict):
            return True
        return bool(cfg.get("disable_tools", True))

    # ── helpers ──

    def _refresh_schema_mic_options(self):
        """把本机麦克风列表写进 adapter/schema.json 的 mic_device.options。

        必须发生在 register_adapter 之前（框架只在注册时读一次 schema）。
        写入本身是幂等的：设备没变化时不落盘。
        """
        mod = _load_adapter_component()
        refresh = getattr(mod, "refresh_mic_device_options", None)
        if refresh is None:
            logger.debug("VRCLink: adapter component unavailable, skip mic options refresh")
            return
        options = refresh(
            _PLUGIN_ROOT / "adapter" / "schema.json",
            self._collect_configured_mic_devices(),
        )
        logger.info(
            f"VRCLink: mic_device options ready ({len(options)} 项，含系统默认哨兵)；"
            "已写入 schema，重启 bot 后适配器配置页下拉更新"
        )

    def _migrate_legacy_asr_config(self):
        """Persist the 1.0.0 config migration for every VRChat instance.

        Runs before `register_adapter`, so the freshly built adapter instance
        already reads the migrated keys. Idempotent: no write when nothing
        changed (the helper returns None in that case).
        """
        ctx_cfg = getattr(self.ctx, "config", None)
        if ctx_cfg is None:
            return
        adapters = ctx_cfg.get_config("adapters", {}) or {}
        mod = _load_adapter_component()
        migrate = getattr(mod, "migrate_legacy_asr_config", None) if mod else None
        if migrate is None:
            return
        migrated = 0
        for entry in adapters.values():
            if not isinstance(entry, dict):
                continue
            if str(entry.get("platform", "")) != "VRChat":
                continue
            config = entry.get("config")
            if isinstance(config, dict) and migrate(config) is not None:
                migrated += 1
        if migrated:
            ctx_cfg.save_config()
            logger.warning(
                f"VRCLink 1.0.0: local ASR has been removed; migrated {migrated} "
                "VRChat instance(s) to cloud STT (stt_model). "
                "Make sure an STT model is configured in KiraAI."
            )

    def _collect_configured_mic_devices(self) -> list[str]:
        """已保存的适配器实例 mic_device 值，保证它们留在 options 里。

        否则 WebUI 的下拉框/保存校验会把「不在选项里的已保存值」洗成默认项。
        """
        values: list[str] = []
        try:
            config = getattr(self.ctx, "config", None) or {}
            adapters = config.get("adapters", {}) or {}
            for entry in adapters.values():
                if not isinstance(entry, dict):
                    continue
                cfg = entry.get("config")
                if not isinstance(cfg, dict):
                    continue
                v = str(cfg.get("mic_device", "") or "").strip()
                if v:
                    values.append(v)
        except Exception as e:
            logger.debug(f"VRCLink: collect configured mic_device failed: {e}")
        return values

    def _get_stt(self, model_uuid: str = ""):
        """Resolve an STT client.

        Empty model_uuid -> KiraAI default STT (preferred).
        "provider_id:model_id" -> that specific STT model, if configured.
        """
        if not self.ctx or not self.ctx.provider_mgr:
            return None
        mgr = self.ctx.provider_mgr
        model_uuid = (model_uuid or "").strip()
        if model_uuid:
            try:
                provider_id, model_id = model_uuid.split(":", 1)
                client = mgr.get_model_client(provider_id, model_id, "stt")
                if client is not None:
                    return client
                logger.warning(f"VRCLink: cloud STT model not found: {model_uuid}, falling back to default")
            except Exception as e:
                logger.warning(f"VRCLink: resolve cloud STT {model_uuid!r} failed: {e}")
        try:
            return mgr.get_default_stt()
        except Exception:
            return None

    def _find_adapter(self):
        """Return the running VRChatAdapter instance, preferring the named 'vrchat'."""
        if not self.ctx or not self.ctx.adapter_mgr:
            return None
        adapters = self.ctx.adapter_mgr.get_adapters() or {}
        named = adapters.get("vrchat")
        if named is not None and getattr(getattr(named, "info", None), "platform", "") == "VRChat":
            return named
        for ada in adapters.values():
            if getattr(ada, "info", None) and getattr(ada.info, "platform", "") == "VRChat":
                return ada
        return None

    def _status_payload(self) -> dict:
        ada = self._find_adapter()
        if ada is None:
            return {
                "online": False,
                "hint": "适配器实例未创建。请到 WebUI → 适配器 → 新建，平台选 VRChat，实例名填 vrchat。",
            }
        status = ada.get_status() if hasattr(ada, "get_status") else {}
        status["online"] = True
        return status

    def _require_adapter(self, method: str):
        ada = self._find_adapter()
        if ada is None:
            return None, {"ok": False, "error": "adapter instance not found"}
        if not hasattr(ada, method):
            return None, {"ok": False, "error": f"adapter does not support {method}"}
        return ada, None

    # ── status page (web/) ──

    @register.page(
        "/status",
        menu=PageMenu(label={"zh": "VRCLink 状态", "en": "VRCLink Status"}, icon="Monitor", order=90),
    )
    def status_page(self):
        return PluginPage.from_folder("./web")

    # ── REST API for the status page ──

    @register.api(method="GET", path="/status")
    async def api_status(self):
        return self._status_payload()

    @register.api(method="POST", path="/test-message")
    async def api_test_message(self, text: str = ""):
        ada, err = self._require_adapter("test_send")
        if err:
            return err
        return await ada.test_send(text)

    @register.api(method="POST", path="/test-record")
    async def api_test_record(self, seconds: float = 3.0):
        ada, err = self._require_adapter("test_record")
        if err:
            return err
        return await ada.test_record(seconds)

    @register.api(method="GET", path="/input-devices")
    async def api_input_devices(self):
        ada, err = self._require_adapter("list_input_devices")
        if err:
            return err
        return ada.list_input_devices()

    @register.api(method="POST", path="/refresh-devices")
    async def api_refresh_devices(self):
        """重扫本机麦克风并写入 schema 的 mic_device.options。

        schema 是适配器注册时一次性读入的，所以下拉列表要等重启 bot（或
        禁用/启用插件）才更新——note 字段里如实告知前端。
        """
        note = (
            "已把当前设备列表写入 adapter/schema.json（mic_device.options）。"
            "重启 bot（或在 WebUI 禁用再启用本插件）后，适配器配置页的下拉列表才会更新；"
            "本页的设备列表与「测电平」按钮即时可用。"
        )
        empty = {"ok": False, "options": [], "devices": [], "note": note}
        try:
            mod = _load_adapter_component()
            refresh = getattr(mod, "refresh_mic_device_options", None)
            if refresh is None:
                empty["error"] = "adapter component not loadable"
                return empty
            options = refresh(
                _PLUGIN_ROOT / "adapter" / "schema.json",
                self._collect_configured_mic_devices(),
            )
        except Exception as e:
            logger.warning(f"VRCLink: refresh-devices failed: {e}")
            empty["error"] = f"刷新失败: {e}"
            return empty
        ada, _err = self._require_adapter("list_input_devices")
        devices = []
        if ada is not None:
            try:
                devices = ada.list_input_devices().get("devices", [])
            except Exception as e:
                logger.debug(f"VRCLink: device enumeration failed: {e}")
        return {"ok": True, "options": options, "devices": devices, "note": note}

    @register.api(method="POST", path="/probe-device")
    async def api_probe_device(self, device: str = "", seconds: float = 2.0, gain: float = 0.0):
        ada, err = self._require_adapter("probe_device")
        if err:
            return err
        return await ada.probe_device(device=device, seconds=seconds, gain=gain)

    @register.api(method="POST", path="/force-reset")
    async def api_force_reset(self):
        ada, err = self._require_adapter("force_reset")
        if err:
            return err
        return ada.force_reset()

    # ── overview widgets ──

    @register.widget(
        label={"zh": "VRCLink", "en": "VRCLink"},
        icon="Monitor",
        color="blue",
        order=90,
        size="small",
    )
    def widget_vrclink(self) -> str:
        ada = self._find_adapter()
        if ada is None:
            return "未配置"
        st = ada.get_status() if hasattr(ada, "get_status") else {}
        online = "在线" if st.get("vrchat_running") else "离线"
        return f"{st.get('state', '?')} · VRChat {online}"

    @register.widget(
        label={"zh": "VRCLink 最近识别", "en": "VRCLink Last ASR"},
        icon="ChatDotRound",
        color="purple",
        order=91,
        size="wide",
    )
    def widget_vrclink_last(self) -> str:
        ada = self._find_adapter()
        if ada is None:
            return (
                "<p class='text-gray-500 dark:text-gray-400'>"
                "适配器实例未创建。到 WebUI → 适配器 → 新建 VRChat 实例。"
                "</p>"
            )
        st = ada.get_status() if hasattr(ada, "get_status") else {}
        last = html.escape(st.get("last_recognized") or "（还没有识别记录）")
        reply = html.escape(st.get("last_reply") or "")
        if len(reply) > 80:
            reply = reply[:80] + "…"
        sent = st.get("sent_count", 0)
        state = html.escape(str(st.get("state", "-")))
        online = "在线" if st.get("vrchat_running") else "离线"
        return (
            "<table style='border-collapse:collapse;width:100%'>"
            f"<tr><td style='padding:4px 12px'>状态</td>"
            f"<td style='padding:4px 12px;font-weight:600'>{state}</td>"
            f"<td style='padding:4px 12px'>VRChat</td>"
            f"<td style='padding:4px 12px;font-weight:600'>{online}</td></tr>"
            f"<tr><td style='padding:4px 12px'>最近识别</td>"
            f"<td colspan='3' style='padding:4px 12px'>{last}</td></tr>"
            f"<tr><td style='padding:4px 12px'>最近回复</td>"
            f"<td colspan='3' style='padding:4px 12px'>{reply or '-'}</td></tr>"
            f"<tr><td style='padding:4px 12px'>已发送段数</td>"
            f"<td colspan='3' style='padding:4px 12px'>{sent}</td></tr>"
            "</table>"
        )
