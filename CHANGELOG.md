# Changelog

## 1.0.0 — 2026-10-28

商店首发版本。**破坏性变更**：移除本地语音识别，只保留云端 STT。

### 变更
- **移除本地 ASR**：删除 faster-whisper / ctranslate2 依赖、CUDA DLL 预加载、模型下载与预热、`hf_endpoint` 镜像设置。插件不再加载任何本地模型，安装体积与内存占用大幅下降，也不再和 VRChat / VR 串流抢 GPU。
- 配置项精简：删除 `asr_backend`、`whisper_model`、`whisper_language`、`whisper_compute`、`whisper_download_root`、`hf_endpoint`、`asr_load_timeout_seconds`。
- `cloud_stt_model` 重命名为 **`stt_model`**；旧配置启动时自动迁移（保留原选择），旧键自动清除。
- 新增 **`stt_timeout_seconds`**（默认 20 秒）：识别超时即放弃本次，不发送内容，状态页给出原因。
- 新增 **`silence_skip_dbfs`**（默认 −55 dBFS，0 = 关闭）：录音电平过低时不调用 STT，省额度、避免幻觉文本。
- 所有配置项说明文字精简为一句（详细说明移入 README 与状态页）。
- 状态页：显示实际解析到的 STT 模型与超时设置；「试录识别」增加耗时（ms）与命中的模型名。
- 识别失败（超时 / 401 / 额度 / 网络）统一处理：不发送、不臆造文本，只记日志并在状态页显示可读摘要。

### 保留
- 云端 STT 走 KiraAI 配置的模型（`stt_model` 留空 = 全局默认 STT）。
- 麦克风设备自动检测下拉、逐设备电平探测、`mic_gain` 增益。
- 热键 / VR 手柄 PTT、聊天框分段与节流、回复前缀、打字指示、对话记录。

### 升级说明
从 1.x 升级：配置会自动迁移到 `stt_model`。若原先用本地 whisper，升级后请确认 KiraAI 里已配置 STT 模型（否则状态页会显示「未配置（将用全局默认）」且无法识别）。
