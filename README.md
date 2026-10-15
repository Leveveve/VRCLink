# VRCLink

在 **VRChat** 游戏内与你的 **KiraAI** 数字生命对话：按住按键说话 → 语音识别 → bot 回复发送到游戏聊天框（chatbox，全房间可见）。

```
按住热键说话 ──► 录音 ──► ASR（本地 / 云端）──► KiraAI 完整管线
                                                      │
                                                      ▼
VRChat 聊天框 ◄── OSC /chatbox/input ◄── 分段 + 节流 ◄── bot 回复
```

- 版本：**1.1.1**（1.1.1 修复 CUDA DLL 预加载、本地 GPU 推理真正生效；自 1.1.0 起适配 **KiraAI v3.0.0+**；v2.x 旧核心请用 VRCLink ≤1.0.x）
- 平台：Windows（依赖 pynput 全局热键 / sounddevice / 前台窗口检测）
- 游戏：VRChat（官方 OSC 接口，UDP 本机回环，EAC 安全）
- Bot：KiraAI（插件 + 适配器实例）

---

## 功能

| 能力 | 说明 |
|------|------|
| 按键说话（PTT） | 默认 `B` 键；支持单键、组合键（`ctrl+shift+v`）、鼠标侧键（`x2`/`x3`） |
| hold / toggle | 按住说话，或点按开始 / 再点结束 |
| 仅游戏前台触发 | 可选；避免在其他软件打字时误触发 |
| 本地 ASR | faster-whisper（GPU 优先，失败自动回退 CPU） |
| 云端 ASR | 走 KiraAI 已配置的 STT；可另指定模型 |
| 完整 bot 管线 | 人设 / 记忆 / 工具全部生效（不是简单回声） |
| 聊天框智能分段 | 按句读切分，单条 ≤140 字；间隔按字数自适应 |
| 回复前缀 | 如 `yuki：`，区分 bot 发言与玩家自己打字 |
| typing 指示器 | 处理中显示「···」，并自动保活（VRChat 会超时隐藏） |
| 状态页 / 小组件 | WebUI 内查看状态、测试发送、试录识别、强制复位 |
| VR 手柄 PTT | 经 OVR Advanced Settings 的 Key Press 映射到同一键盘热键（插件无需改） |


---

## 系统要求

| 项 | 要求 |
|----|------|
| 操作系统 | Windows 10/11（全局热键、前台检测、音频均为 Windows 路径） |
| KiraAI | 核心 **v3.0.0+**（VRCLink 1.1.0 改用 v3 的 BaseAdapter / IMCapability 适配器接口） |
| Python | 3.10+（KiraAI 运行环境） |
| VRChat | 已安装，游戏内开启 OSC（菜单 → OSC → Enabled） |
| 麦克风 | 系统默认或可指定设备名 |
| 可选 GPU | NVIDIA（本地 whisper 加速）；无 GPU 自动用 CPU |
| 可选 VR | SteamVR 串流 + [OVR Advanced Settings](https://github.com/OpenVR-Advanced-Settings/OpenVR-AdvancedSettings)（手柄 PTT） |

---

## 安装

### 1. 复制插件

把本目录（`vrclink/`）整份复制到 **某一个** bot 的插件目录：

```text
<bots>/data/plugins/vrclink/
```

标准 KiraAI 插件结构（与官方开发指南一致）：

```text
vrclink/
├── manifest.json       # 插件元信息（plugin_id / version / 描述）
├── main.py             # 入口：注册适配器、状态页、LLM 环境注入
├── requirements.txt    # 运行依赖
├── README.md           # 本说明
├── icon.svg
├── adapter/            # 适配器组件（目录名固定 adapter/）
│   ├── manifest.json   # {"name": "VRChat"} → 平台 ID
│   ├── adapter.py      # IMAdapter 实现
│   └── schema.json     # 适配器实例配置字段
└── web/                # WebUI 状态页（可选）
    ├── index.html
    ├── style.css
    └── app.js
```

> 一次只服务一位 bot。要给多个 bot 用，请分别复制到各自的 `data/plugins/`。

### 2. 安装依赖

在该 bot 的 Python 环境里安装（faster-whisper 较大，建议手动装）：

```powershell
# Windows 示例，请按实际 bot 的 python 路径调整
E:\KiraAI\<bot>\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

本地 GPU 推理（可选，约 2–3G 显存）：

```powershell
E:\KiraAI\<bot>\.venv\Scripts\python.exe -m pip install nvidia-cublas-cu12 nvidia-cudnn-cu12
```

云端识别则可不装 faster-whisper 与 CUDA 库（请把 `asr_backend` 设为 `cloud`）。

### 3. 启动 bot

启动 / 重启 bot，打开 WebUI → 插件页，确认 **VRCLink** 已启用。

### 4. 创建适配器实例（一次性）

WebUI → **适配器** → **新建**：

| 字段 | 值 |
|------|-----|
| 平台 | **VRChat** |
| 实例名 | `vrchat`（**不能包含冒号**） |
| 启用 | 开 |

此后所有配置都在这个适配器实例的配置页里改。

### 5. VRChat 侧

游戏内打开 **OSC**（菜单 → OSC → Enabled）。默认即可：入站 `127.0.0.1:9000`。

---

## 配置说明

配置位置：WebUI → 适配器 → 你的 VRChat 实例。

### 基础

| 配置项 | 默认 | 说明 |
|--------|------|------|
| `owner_id` | `owner` | 私聊会话 ID（会话 = `实例名:dm:此值`），一般不用改 |
| `owner_nickname` | `主人` | bot 眼中说话者的昵称（进入人设上下文） |
| `ptt_mode` | `hold` | `hold` 按住说话 / `toggle` 点按切换 |
| `hotkey` | `b` | 热键：`b`、`ctrl+shift+v`、鼠标侧键 `x2`/`x3` |
| `hotkey_require_focus` | `true` | 仅指定进程在前台时才触发 |
| `hotkey_focus_process` | `VRChat.exe` | 视为「游戏内」的进程名 |
| `mic_device` | 空 | 麦克风设备名子串；空 = 系统默认 |
| `max_record_seconds` | `60` | 单次录音上限，超时自动结束 |

### 语音识别

| 配置项 | 默认 | 说明 |
|--------|------|------|
| `asr_backend` | `local` | `local` 本机 faster-whisper / `cloud` 云端 STT |
| `cloud_stt_model` | 空 | **仅 cloud**：留空用 KiraAI 全局默认 STT；也可单独指定其他 STT 模型 |
| `whisper_model` | `large-v3-turbo` | **仅 local**：模型名或本地目录 |
| `whisper_language` | `zh` | `zh` / `auto` / `en` … |
| `whisper_compute` | `int8_float16` | GPU 精度；CUDA 缺失自动回退 CPU `int8` |
| `hf_endpoint` | `https://hf-mirror.com` | 下载 whisper 用的 Hub；可直连官方源则清空 |
| `whisper_download_root` | 空 | 模型缓存目录；空 = HuggingFace 默认 |
| `asr_load_timeout_seconds` | `600` | 首次下载/加载超时 |

**云端识别优先级**：

1. 若在适配器里选了 `cloud_stt_model` → 用该模型  
2. 否则 → 用 KiraAI **全局默认 STT**（配置 → 模型）  
3. 都没有 → 报错并提示改回 local 或去配置 STT  

### 聊天框发送

| 配置项 | 默认 | 说明 |
|--------|------|------|
| `osc_in_port` | `9000` | VRChat OSC 入站端口 |
| `reply_prefix` | 空 | 每条消息前缀，**强烈建议**设为 bot 昵称（如 `yuki：`） |
| `send_interval` | `2` | 短句最小间隔（秒） |
| `read_chars_per_second` | `5` | 阅读速度；长句间隔 ≈ 字数 ÷ 此值 |
| `max_segment_interval` | `8` | 长句间隔上限（秒） |
| `send_timeout_seconds` | `120` | 管线超时未回复则自动回 idle |
| `typing_indicator` | `true` | 处理中显示「···」并自动保活 |
| `log_conversations` | `true` | 识别与回复写入 `plugin_data/vrclink/conversations.jsonl` |

> VRChat 聊天框限制：单条 ≤144 字（我们用 140），且 5 秒内最多 5 条，超出静默丢弃。插件已自我节流。

---

## VR 手柄按键说话（可选）

VR 手柄本身不进 Windows 键盘层。用 [OVR Advanced Settings](https://store.steampowered.com/app/1009850/OVR_Advanced_Settings/)（免费）把任意手柄键映射成键盘按键，即可与键盘走同一条热键路径。

1. 安装并启动 OVRAS（可随 SteamVR 自启）。
2. **先退出 OVRAS**，编辑  
   `%AppData%\AdvancedSettings-Team\OVR Advanced Settings.ini`  
   在 `[keyboardShortcuts]` 写入（与插件 `hotkey` 一致）：

   ```ini
   keyPressSystem=b
   ```

3. 启动 SteamVR / OVRAS。
4. SteamVR → 设置 → 控制器选项 → 管理控制器绑定 → 应用选 **OVR Advanced Settings** → 自定义 → 编辑绑定 → 给手柄某键绑定 **System → Key Press System**。
5. 戴上头显，VRChat 前台，按住手柄该键说话。

要点：

- 使用 **Key Press**（按住=键按住），**不要**用 Keyboard Shortcut 宏（宏是点按，hold 模式会失效）。
- 改 INI 必须在 OVRAS **未运行**时改（只在启动时读文件）。
- `Key Press System` 始终可用；`Key Press Misc` 可能受其他绑定影响。

---

## 使用

1. 启动 VRChat，确保 OSC 已开、焦点在游戏。
2. 按住热键（默认 `B`）说话，松开结束。
3. 聊天框出现「···」→ 识别 → bot 回复（带 `reply_prefix`）。
4. 也可在 WebUI → 插件 → **VRCLink 状态** 里点「测试发送 / 试录识别」。

### 测试建议

- **测试发送**：验证 OSC 通路（不经过 LLM）。
- **试录识别**：验证麦克风 + ASR（不经过 LLM）。
- **完整对讲**：热键说一句「你好」，应有人设化回复进聊天框。

---

## 数据与配置路径

| 用途 | 路径 |
|------|------|
| 插件本体 | `data/plugins/vrclink/` |
| 适配器配置 | `data/config/system_config.json` → adapters（或 WebUI 编辑） |
| 临时录音 | `data/plugin_data/vrclink/tmp/` |
| 对话日志 | `data/plugin_data/vrclink/conversations.jsonl` |

多 bot 部署时，各 bot 的 `data/` 相互独立，请分别安装与配置。

---

## 兼容性与已知限制

| 风险 / 限制 | 影响 | 建议 |
|-------------|------|------|
| **仅 Windows** | macOS / Linux 上 pynput、前台检测、音频路径未适配 | 暂不支持 |
| **全局热键冲突** | 与游戏/其他软件的 `B` 键冲突 | 改 `hotkey`，或开「仅 VRChat 前台」 |
| **游戏内聊天框打字误触** | 在 VRChat 聊天框输入 `B` 可能触发录音 | 知悉即可；或改冷门热键 |
| **VRChat 聊天框限流** | 5 条 / 5 秒，超出静默丢弃 | 保持 `send_interval ≥ 1`；过长回复自动拆段 |
| **9000 端口占用** | 其他 OSC 工具抢入站口则收不到 | 关闭冲突工具或改 `osc_in_port` 与 VRChat 对齐 |
| **9001 出站占用** | 仅影响备用手势方案（默认不用） | v1 主路径是键盘热键，可忽略 |
| **本地 whisper 显存不足** | VR 中 GPU 被占满可能 OOM/掉帧 | 换 `medium`/`small`、改 CPU、或 `asr_backend=cloud` |
| **CUDA 依赖安装失败** | 缺失时自动回退 CPU，识别变慢 | 装 `nvidia-cublas-cu12 nvidia-cudnn-cu12`，或改云端 |
| **首次下载 whisper 模型** | 可能较久或失败（网络） | 用 `hf_endpoint` 镜像；或改云端 |
| **ASR 幻觉 / 空识别** | 静音或过短时胡乱文字 | 已有过滤；仍可在提示词里约束 |
| **回复挂本机玩家名下** | 不加前缀时像玩家自己在说话 | **务必设置 `reply_prefix`** |
| **默认消息策略 discard** | 若你自写 `@on.im_message` 且不 buffer/trigger，会拦下消息 | 只做预处理即可，勿默认 discard |
| **适配器实例名含冒号** | 会话路由失败 | 实例名用 `vrchat` 这类简单名 |
| **单 bot 单实例** | 多 bot 同时开会抢热键 / 麦克风 / OSC 端口 | 一次只开一个 VRCLink |
| **OVRAS 与 SteamVR** | 手柄 PTT 依赖 OVRAS 随 SteamVR 运行 | 设自启；Key Press 动作需绑定 |
| **KiraAI 版本变动** | 核心 API（Adapter / Prompt）可能调整 | `core_version: >=3.0.0`；异常时看日志 |

---

## 故障排查

| 现象 | 排查 |
|------|------|
| 按热键没反应 | 焦点是否 VRChat（若开了 require_focus）；实例是否已启动；日志 `hotkey armed` |
| 有录音无文字 | 试录识别；本地模型是否加载成功；或切云端 |
| 有文字无回复 | 适配器实例是否创建、`is_mentioned` 管线是否正常；看 bot 日志 |
| 有回复游戏里看不见 | OSC 是否开启；`osc_in_port` 是否 9000；VRChat 是否在跑；测试发送 |
| 回复像自己打的 | 设置 `reply_prefix` |
| 「···」闪断 | 已保活；若仍闪，查 VRChat 版本与 typing 开关 |
| 长句读不完就跳 | 调大 `read_chars_per_second` 倒数，或调大 `max_segment_interval` |
| 手柄无效 | OVRAS INI 是否 `keyPressSystem=b` 且改完重启过 OVRAS；SteamVR 是否绑了 Key Press System |
| 日志报 `cublas64_12.dll is not found` / `CUDA load failed` | ≤1.1.0 的已知问题（1.1.1 起自动预加载 CUDA DLL）；CUDA 缺失时自动回退 CPU，若仍想上 GPU 确认已装 `nvidia-cublas-cu12 nvidia-cudnn-cu12 nvidia-cuda-nvrtc-cu12` |

日志关键字：`VRChat`、`chatbox <-`、`recognized:`、`whisper`、`VRCLink`。

---

## 卸载

1. WebUI 停用 / 删除插件，或删掉适配器实例。  
2. 删除 `data/plugins/vrclink/`。  
3. 可选：删除 `data/plugin_data/vrclink/`。  
4. 若用过 OVRAS，可自行卸载或保留。  

---

## 致谢 / 参考

- [VRChat OSC 文档](https://docs.vrchat.com/docs/osc-overview)
- [OVR Advanced Settings](https://github.com/OpenVR-Advanced-Settings/OpenVR-AdvancedSettings)（键盘映射指南见其 `docs/keyboard_input_guide.md`）
- [faster-whisper](https://github.com/SYSTRAN/faster-whisper)
- 同类项目思路参考：TTS-Voice-Wizard、whisper_osc、VRCT

---

## 许可

按仓库发布时附带的许可证为准（建议 MIT）。
