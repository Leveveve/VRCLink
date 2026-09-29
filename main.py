"""
VRCLink — push-to-talk voice interface to the bot inside VRChat.

Registers the "VRChat" adapter (adapter/adapter.py):
    PTT hotkey (keyboard, or VR controller via OVR Advanced Settings Key Press)
    -> record mic -> ASR (faster-whisper local / KiraAI STT cloud)
    -> message pipeline (persona, memory, tools all apply)
    -> reply Text -> OSC chatbox segments in VRChat.

Config lives on the adapter instance (WebUI -> Adapters -> VRChat instance).
"""

import html

from core.plugin import BasePlugin, logger, on, register, PluginPage, PageMenu
from core.prompt_manager import Prompt

# Injected once per LLM request so the model understands the VRChat medium:
# voice PTT (ASR noise) in, public chatbox out (whole room can read).
_VRCHAT_CONTEXT_PROMPT = """\
## VRChat 聊天环境（重要）
- 当前对话发生在 VRChat 游戏内。用户通过按住按键说话（语音转文字）与你交流，识别结果可能有错字或同音字，按语义理解即可，不要纠结错别字。
- 你的回复会发送到 VRChat 聊天框（chatbox），**全房间可见**，不是私密消息。避免只适合私聊的过度亲昵、长篇独白或敏感私事。
- 回复尽量短、口语化，适合游戏内聊天框快速阅读；长内容拆成几个自然短句（系统会自动分段发送）。
- 你看不到房间里其他玩家，也不知道具体世界/实例名称；若用户提到场景，按TA描述的内容回应即可。
"""


class VRCLinkPlugin(BasePlugin):
    async def initialize(self):
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
        req.system_prompt.append(Prompt(
            content=_VRCHAT_CONTEXT_PROMPT,
            name="vrclink_context",
            source="vrclink",
        ))

    # ── helpers ──

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
