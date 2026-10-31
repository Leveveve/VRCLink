# VRCLink

在 **VRChat** 里和你的 **KiraAI** 数字生命说话。按住热键讲话，语音识别后走 KiraAI 完整链路（人设、记忆、工具都生效），回复自动分段发到 VRChat 聊天框（chatbox，全房间可见）。

```
按住热键说话 → 录音 → 云端语音识别 → KiraAI 管线 → 分段 + 节流 → VRChat 聊天框
```

- 适配器平台：`VRChat`，版本 **1.0.0**，需要 **KiraAI v3.0.0+**
- 语音识别**只走云端**（KiraAI 里配置的 STT 模型），插件本身不带本地模型：安装体积小、不占 GPU、不吃 CPU
- 支持键盘热键，也支持 VR 手柄（用 OVR Advanced Settings 的 Key Press 映射到同一按键）

---

## 安装

1. 把 `vrclink` 整个目录放到 `<bot>/data/plugins/` 下（从商店安装则自动完成）。
2. 依赖：KiraAI 会自动执行 `requirements.txt`；手动装也可以：

   ```
   <bot-python> -m pip install -r requirements.txt
   ```

3. 重启 bot（或禁用再启用插件）。日志出现 `VRCLink plugin initialized` 即成功。

**依赖**：`python-osc`、`sounddevice`、`pynput`、`psutil`、`httpx`、`numpy`。没有 faster-whisper、没有 CUDA 库。

## 首次配置

**先配 STT**：WebUI → 配置 → 模型，确认至少有一个类型为 `stt` 的模型（也可以用 KiraAI 的全局默认 STT）。

**再建适配器实例**：WebUI → 适配器 → 新建，平台选 `VRChat`，实例名建议 `vrchat`。然后逐项设置：

| 配置项 | 说明 |
|---|---|
| 语音识别模型 `stt_model` | 留空 = 用 KiraAI 全局默认 STT（推荐）。想只让 VRChat 走某个模型就在这里指定。 |
| 麦克风设备 `mic_device` | 下拉列表由插件启动时扫描本机可用设备生成（只列能以 16 kHz 打开的），首项=跟随 Windows 默认。**选错设备是最常见的「识别不出来」原因**。 |
| 麦克风增益 `mic_gain` | 1.0=原样。笔记本内置麦小声可以试 2~4；过高会削波（状态页会显示 clipping）。 |
| 热键 `hotkey` 与 `按键模式 ptt_mode` | 默认 `b` + `hold`（按住说话）。`toggle` 是按一下开始、再按一下结束。支持组合键与鼠标侧键，见下节。 |
| 仅 VRChat 前台时触发 `hotkey_require_focus` | 开启后只有前台窗口是 `VRChat.exe` 时热键才生效，避免在别的软件里打字误触发。 |
| 识别超时 `stt_timeout_seconds` | 单次识别最长等待，默认 20 秒；超时即放弃本次、不发送任何内容。 |
| 静音跳过 `silence_skip_dbfs` | 录音峰值低于该值就不调用 STT，默认 −55 dBFS，`0` = 关闭。省额度、也避免把静音识别成幻觉文本。 |
| 回复前缀 `reply_prefix` | 例如 `yuki：`。聊天框消息显示在本机玩家名下，前缀用来区分 bot 和你的发言，**强烈建议填**。 |
| 分段与节流 | `send_interval` 两段之间最小间隔、`read_chars_per_second` 按语速切段、`max_segment_interval` 间隔上限。VRChat 限制 5 秒内最多 5 条消息。 |
| OSC 端口 `osc_in_port` | 默认 9000，和 VRChat 的 OSC 设置保持一致。 |
| 权限相关 | 本适配器只有一个输入源（本机按键说话），`permission_mode` 保持 `deny_list`、名单留空即可。 |

## 在 VRChat 里使用

1. VRChat 里开启 OSC（Action Menu → Options → OSC → Enabled）。
2. 确认游戏窗口在前台（若开了「仅 VRChat 前台时触发」）。
3. **按住热键说话 2–3 秒再松手**（`hold` 模式）。太短（< 0.5 秒）会被直接丢弃。
4. 松手后先识别、再等 bot 回复，回复会按语速分段发到聊天框。

VR 手柄：在 OVR Advanced Settings 里用 *Key Press* 把某个手柄按键映射成热键的同一个键（如 `b`）。

### 热键写法

| 写法 | 说明 |
|---|---|
| `b` | 单个字母/数字键。注意：**在 VRChat 聊天框里打字时会误触发**，建议改用下面的组合键。 |
| `ctrl+b` | 组合键。`ctrl` / `shift` / `alt` 可任意叠加，如 `ctrl+shift+v`。大小写、空格随意（`CTRL + B` 也行），也接受 `control` / `ctl` / `option` 这些别名。 |
| `ctrl+shift+f19` | 功能键 `f1`–`f24` 也可以，和 `alt` 之类的组合尤其不容易和游戏按键冲突。 |
| `space`、`tab`、`enter`、`page_up`、`up` … | 常用命名键。 |
| `x2` / `x3` | 鼠标侧键。鼠标键不支持再叠加修饰键。 |

组合键是**严格匹配**的：绑 `ctrl+b` 时，`ctrl+shift+b` 不会触发（避免和浏览器/编辑器快捷键打架）。按住顺序无所谓，先松修饰键也不会卡住录音。

写错了（例如只写 `ctrl`、写 `win+b`）不会静默失效：适配器仍会启动，日志与状态页会给出 `hotkey not armed` 的明确原因。

## 状态页

WebUI 里的「VRCLink 状态」页面可以做这些事：

- **在线状态**：适配器是否活着、VRChat 是否在运行、OSCQuery 是否可达
- **当前 STT**：实际解析到的模型（未配置时会提示「未配置（将用全局默认）」）
- **试录识别**：录一段并真的调用一次云端识别，回显文本、耗时 ms、电平、命中的模型——排查问题先看这个
- **麦克风电平**：逐设备「测电平」，直接给你 peak/rms dBFS；说话时能到 −30 ~ −10 dBFS 才好用
- **刷新麦克风列表**：把当前设备写进 schema 的 `mic_device.options`（写好之后要重启 bot 才会在配置页出现新下拉）
- **强制复位**：卡在某个状态时回到 idle

## 排查

**识别为空 / 一直没反应**

1. 状态页「试录识别」看回显的电平。−50 dBFS 以下基本等于静音 → 换麦克风设备（选真实的，不是 Pico 串流麦、虚拟声卡之类）。
2. 确认 `mic_device` 不是「系统默认」，或者系统默认设备确实是你想用的麦。
3. 说话时别只点一下热键：`hold` 模式要按住。
4. 看日志有没有 `STT 超时`、`STT 失败：…`、`未配置 STT 模型`。

**聊天框没东西**

- VRChat 里 OSC 没开 → Action Menu 里打开。
- 回复被 VRChat 丢掉：单行超过 **144 字符**会被静默丢弃（插件会自动分段，`read_chars_per_second` 越小段越短）。
- 节流太慢：`send_interval` 调小（不低于 1）。

**状态页显示 `{"detail":"Invalid token"}`**

这是 KiraAI WebUI 的会话问题，不是插件 bug：插件页（iframe）用 cookie `kira_token` 鉴权，而 cookie 只按主机名存、不分端口，多个 bot 在同一台机器上会互相覆盖。解决：在**打开插件页的同一个主机名**上重新登录一次（例如都用 `127.0.0.1:5268`），或者给不同 bot 用不同主机名（`127.0.0.1` / `localhost` / `*.localhost`）。

**多段回复把上一段挤掉**

VRChat 聊天框同时只显示最近一条（`/chatbox/input` 会替换前一条）。这是游戏本身的限制，插件只能控制节奏。

## 已知限制

- 语音识别**只有云端**：需要外网 + 已配置的 STT 模型；网络抖动会表现为识别变慢或超时（超时会明确提示，不会发送奇怪内容）。
- 聊天框单行 144 字符上限、5 秒最多 5 条，长回复会被切成多段。
- 只支持 Windows（psutil 前台进程检测、pynput 全局热键、sounddevice 设备枚举）。
- 一次只有一段录音（没有并发）。

## 更新日志

见 [CHANGELOG.md](CHANGELOG.md)。

## 许可

MIT
