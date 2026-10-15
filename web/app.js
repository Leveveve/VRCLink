/* VRCLink status page — talks to the plugin via PluginPageContext. */

(function () {
    var PPC = window.PluginPageContext;
    var ready = false;
    var busy = false;
    var pollTimer = null;

    var els = {
        conn: document.getElementById("conn-pill"),
        state: document.getElementById("st-state"),
        hotkey: document.getElementById("st-hotkey"),
        vrchat: document.getElementById("st-vrchat"),
        oscq: document.getElementById("st-oscq"),
        backend: document.getElementById("st-backend"),
        model: document.getElementById("st-model"),
        sent: document.getElementById("st-sent"),
        pending: document.getElementById("st-pending"),
        recognized: document.getElementById("st-recognized"),
        reply: document.getElementById("st-reply"),
        error: document.getElementById("st-error"),
        adapter: document.getElementById("st-adapter"),
        uptime: document.getElementById("st-uptime"),
        history: document.getElementById("history-table").querySelector("tbody"),
        testResult: document.getElementById("test-result"),
        micCurrent: document.getElementById("mic-current"),
        micResolved: document.getElementById("mic-resolved"),
        micWarn: document.getElementById("mic-warn"),
        micRefreshNote: document.getElementById("mic-refresh-note"),
        deviceBody: document.getElementById("device-table").querySelector("tbody"),
        btnDevices: document.getElementById("btn-devices"),
        btnMicRefresh: document.getElementById("btn-mic-refresh"),
    };

    function setPill(kind, text) {
        els.conn.className = "pill " + kind;
        els.conn.textContent = text;
    }

    function fmtTime(ts) {
        if (!ts) return "—";
        var d = new Date(ts * 1000);
        function p(n) { return (n < 10 ? "0" : "") + n; }
        return p(d.getMonth() + 1) + "-" + p(d.getDate()) + " " +
               p(d.getHours()) + ":" + p(d.getMinutes()) + ":" + p(d.getSeconds());
    }

    function fmtUptime(sec) {
        sec = Math.max(0, Number(sec) || 0);
        var m = Math.floor(sec / 60), s = sec % 60;
        var h = Math.floor(m / 60); m = m % 60;
        if (h) return "运行 " + h + "h " + m + "m";
        if (m) return "运行 " + m + "m " + s + "s";
        return "运行 " + s + "s";
    }

    function escapeHtml(s) {
        return String(s == null ? "" : s)
            .replace(/&/g, "&amp;")
            .replace(/</g, "&lt;")
            .replace(/>/g, "&gt;")
            .replace(/"/g, "&quot;");
    }

    function fmtDb(v) {
        var n = Number(v);
        if (v == null || isNaN(n)) return "—";
        return n.toFixed(1);
    }

    var HOW_LABELS = {
        "": "跟随系统默认",
        "exact": "完整名精确匹配",
        "substring": "子串匹配",
        "substring_unprefixed": "去前缀子串匹配",
        "not_found": "未找到 · 已回退",
        "error": "解析失败",
    };

    function renderResolved(r) {
        if (!r) {
            els.micResolved.textContent = "";
            return;
        }
        var line = "mic_device：" + (r.configured ? r.configured : "（空 = 系统默认）");
        if (r.how === "error") {
            line += " · 解析失败：" + (r.warning || "?");
        } else if (r.how === "not_found") {
            line += " · ⚠ 配置的设备未找到，录音时将回退系统默认（当前默认：" + (r.name || "?") + "）";
        } else if (!r.how) {
            line += " · 跟随系统默认 → " + (r.name || "?");
        } else {
            line += " · " + (HOW_LABELS[r.how] || r.how) + " → " + (r.name || "?") +
                (r.index != null ? "（#" + r.index + "）" : "");
        }
        els.micResolved.textContent = line;
    }

    function render(data) {
        if (!data) return;

        if (!data.online) {
            setPill("pill-err", "未配置");
            els.state.textContent = "未创建";
            els.state.classList.remove("is-rec");
            els.hotkey.textContent = data.hint || "请创建适配器实例";
            els.vrchat.textContent = "—";
            els.oscq.textContent = "";
            els.backend.textContent = "—";
            els.model.textContent = "";
            els.sent.textContent = "—";
            els.pending.textContent = "";
            els.recognized.textContent = data.hint || "适配器实例未创建";
            els.reply.textContent = "—";
            els.error.textContent = "";
            els.adapter.textContent = "适配器 —";
            els.uptime.textContent = "";
            els.history.innerHTML = '<tr><td colspan="4" class="empty">暂无记录</td></tr>';
            els.micCurrent.textContent = "";
            els.micResolved.textContent = "";
            els.micWarn.style.display = "none";
            return;
        }

        setPill("pill-ok", "已连接");

        var state = data.state || "—";
        els.state.textContent = state;
        if (state === "recording") els.state.classList.add("is-rec");
        else els.state.classList.remove("is-rec");

        var mode = data.ptt_mode === "toggle" ? "toggle" : "hold";
        var rec = state === "recording" && data.record_elapsed
            ? " · 录音 " + data.record_elapsed + "s"
            : "";
        els.hotkey.textContent = "热键 " + (data.hotkey || "—") + " · " + mode + rec;

        els.vrchat.textContent = data.vrchat_running ? "在线" : "离线";
        els.oscq.textContent = "OSCQuery " + (data.oscq_reachable ? "可达" : "未响应");

        var backend = data.backend || "local";
        els.backend.textContent = backend === "cloud" ? "云端 STT" : "本地 whisper";
        var modelLine = "";
        if (backend === "local") {
            var wstat = data.whisper_status || "";
            var wdev = data.whisper_device || "";
            modelLine = (data.whisper_model || "");
            if (wdev) modelLine += " @ " + wdev;
            if (wstat === "loading") modelLine += " · 下载/加载中";
            else if (wstat === "failed") modelLine += " · 加载失败";
            else if (wstat === "loaded") modelLine += " · 就绪";
            else if (wstat) modelLine += " · " + wstat;
        } else {
            modelLine = "KiraAI 默认 STT";
        }
        els.model.textContent = modelLine;
        if (data.whisper_error && data.whisper_status === "failed") {
            els.error.textContent = data.last_error || data.whisper_error;
        }

        els.sent.textContent = data.sent_count != null ? String(data.sent_count) : "—";
        els.pending.textContent = "队列 " + (data.pending_segments || 0) +
            " · 间隔 " + (data.send_interval || 2) + "s";

        els.recognized.textContent = data.last_recognized || "（还没有识别记录）";
        els.reply.textContent = data.last_reply || "—";
        els.error.textContent = data.last_error || "";

        var micLine = "当前设备：";
        if (data.mic_device_name) {
            micLine += data.mic_device_name;
            if (data.mic_device) micLine += "（配置过滤：" + data.mic_device + "）";
        } else {
            micLine += "（还没有录过音，" + (data.mic_device ? "配置过滤：" + data.mic_device : "系统默认") + "）";
        }
        micLine += " · mic_gain " + (data.mic_gain != null ? data.mic_gain : 1);
        if (data.mic_device_name) {
            micLine += " · 最近录音 peak " + fmtDb(data.last_peak_dbfs) + " / rms " +
                fmtDb(data.last_rms_dbfs) + " dBFS";
        }
        els.micCurrent.textContent = micLine;
        renderResolved(data.mic_device_resolved);

        var silent = data.mic_device_name && data.last_peak_dbfs != null && data.last_peak_dbfs < -50;
        els.micWarn.style.display = silent ? "" : "none";
        if (silent) {
            els.micWarn.textContent = "⚠ 最近一次录音电平接近静音（peak " + fmtDb(data.last_peak_dbfs) +
                " dBFS）：多半是设备选错或系统输入音量为 0。请在下方换一个设备「测电平」" +
                "（说话时能到 -30 ~ -10 dBFS 的才好用），必要时再调大 mic_gain。";
        }

        els.adapter.textContent = "适配器 " + (data.adapter_name || "—") +
            " · " + (data.platform || "VRChat");
        els.uptime.textContent = fmtUptime(data.uptime_seconds);

        var hist = data.history || [];
        if (!hist.length) {
            els.history.innerHTML = '<tr><td colspan="4" class="empty">暂无记录</td></tr>';
        } else {
            var html = "";
            for (var i = hist.length - 1; i >= 0; i--) {
                var h = hist[i];
                html += "<tr>" +
                    "<td>" + fmtTime(h.ts) + "</td>" +
                    "<td>" + escapeHtml(h.recognized || "—") + "</td>" +
                    "<td>" + escapeHtml(h.reply || "—") + "</td>" +
                    "<td>" + (h.segments != null ? h.segments : "—") + "</td>" +
                    "</tr>";
            }
            els.history.innerHTML = html;
        }
        syncButtons(data);
    }

    function fetchStatus() {
        if (!ready || !PPC) return;
        return PPC.api.get("status").then(render).catch(function (e) {
            setPill("pill-err", "API 失败");
            els.error.textContent = String(e && e.message ? e.message : e);
        });
    }

    function setBusy(on) {
        busy = on;
        document.getElementById("btn-test-msg").disabled = on;
        document.getElementById("btn-test-rec").disabled = on;
    }

    function syncButtons(data) {
        if (busy) return;
        var st = data && data.state;
        var blocked = st && st !== "idle";
        document.getElementById("btn-test-msg").disabled = blocked;
        document.getElementById("btn-test-rec").disabled = blocked;
    }

    function init() {
        if (!PPC) {
            setPill("pill-err", "无桥接 SDK");
            return;
        }
        PPC.ready().then(function () {
            ready = true;
            setPill("pill-ok", "已连接");
            fetchStatus();
            pollTimer = setInterval(fetchStatus, 3000);
            loadDevices();
        }).catch(function (e) {
            setPill("pill-err", "桥接失败");
            els.error.textContent = String(e && e.message ? e.message : e);
        });

        document.getElementById("btn-refresh").addEventListener("click", fetchStatus);

        document.getElementById("btn-reset").addEventListener("click", function () {
            els.testResult.textContent = "复位中…";
            PPC.api.post("force-reset").then(function (data) {
                els.testResult.textContent = data && data.ok
                    ? "已复位：" + (data.previous_state || "?") + " → " + (data.state || "idle")
                    : "失败：" + (data && data.error ? data.error : "未知错误");
                return fetchStatus();
            }).catch(function (e) {
                els.testResult.textContent = "失败：" + String(e && e.message ? e.message : e);
            });
        });

        document.getElementById("btn-test-msg").addEventListener("click", function () {
            if (busy) return;
            setBusy(true);
            els.testResult.textContent = "发送中…";
            PPC.api.post("test-message").then(function (data) {
                els.testResult.textContent = data && data.ok
                    ? "已入队：" + data.text + "（" + (data.segments || []).length + " 段）"
                    : "失败：" + (data && data.error ? data.error : "未知错误");
                return fetchStatus();
            }).catch(function (e) {
                els.testResult.textContent = "失败：" + String(e && e.message ? e.message : e);
            }).then(function () {
                setBusy(false);
                fetchStatus();
            });
        });

        document.getElementById("btn-test-rec").addEventListener("click", function () {
            if (busy) return;
            var seconds = Number(document.getElementById("test-seconds").value) || 3;
            seconds = Math.max(1, Math.min(15, seconds));
            setBusy(true);
            els.testResult.textContent = "录音 " + seconds + "s 并识别中…（请对着麦克风说话）";
            // FastAPI maps non-model args on POST to query parameters
            PPC.api.post("test-record?seconds=" + seconds).then(function (data) {
                if (data && data.ok) {
                    var msg = "时长 " + data.duration + "s，识别：" + (data.text || "（空）");
                    if (data.peak_dbfs != null) {
                        msg += "\n电平：peak " + fmtDb(data.peak_dbfs) + " / rms " + fmtDb(data.rms_dbfs) +
                            " dBFS（设备：" + (data.device || "？") + "）";
                    }
                    if (data.error) msg += "\n⚠ " + data.error;
                    els.testResult.textContent = msg;
                } else {
                    els.testResult.textContent = "失败：" + (data && data.error ? data.error : "未知错误");
                }
                return fetchStatus();
            }).catch(function (e) {
                els.testResult.textContent = "失败：" + String(e && e.message ? e.message : e);
            }).then(function () {
                setBusy(false);
                fetchStatus();
            });
        });

        els.btnDevices.addEventListener("click", loadDevices);

        els.btnMicRefresh.addEventListener("click", function () {
            els.btnMicRefresh.disabled = true;
            els.micRefreshNote.style.display = "";
            els.micRefreshNote.textContent = "扫描本机麦克风并写入配置中…（逐个试开 16 kHz，稍等）";
            PPC.api.post("refresh-devices").then(function (r) {
                if (r && r.ok) {
                    els.micRefreshNote.textContent =
                        "✓ 已写入 " + ((r.options || []).length) + " 项（含系统默认哨兵）。" + (r.note || "");
                } else {
                    els.micRefreshNote.textContent =
                        "✗ " + ((r && r.error) || "未知错误") + "。" + ((r && r.note) || "");
                }
                return loadDevices();
            }).catch(function (e) {
                els.micRefreshNote.textContent = "✗ " + String(e && e.message ? e.message : e);
            }).then(function () {
                els.btnMicRefresh.disabled = false;
            });
        });
    }

    function levelVerdict(peak, clipping) {
        if (clipping) return { cls: "lv-low", text: "⚠ 触顶削波——增益过大，请调低 mic_gain" };
        if (peak >= -30) return { cls: "lv-ok", text: "良好：说话时达到这个水平很容易被识别" };
        if (peak >= -50) return { cls: "lv-mid", text: "偏安静：有点声音，说话时最好能到 -30 ~ -10 dBFS" };
        return { cls: "lv-low", text: "接近静音：设备可能选错，或系统输入音量为 0" };
    }

    function probeDevice(btn, deviceName) {
        var cell = btn.closest("tr").querySelector(".lv-cell");
        Array.prototype.forEach.call(document.querySelectorAll(".probe-btn"), function (b) {
            b.disabled = true;
        });
        cell.innerHTML = '<span class="lv-mid">探测中…录音 2s（可对着该设备说话）</span>';
        PPC.api.post("probe-device?seconds=2&device=" + encodeURIComponent(deviceName || ""))
            .then(function (r) {
                if (r && r.ok) {
                    var v = levelVerdict(Number(r.peak_dbfs), r.clipping);
                    cell.innerHTML = "peak <b>" + fmtDb(r.peak_dbfs) + "</b> / rms " + fmtDb(r.rms_dbfs) +
                        " dBFS（gain " + r.gain + "）" +
                        '<div class="' + v.cls + '">' + escapeHtml(v.text) + "</div>";
                } else {
                    cell.innerHTML = '<span class="lv-low">失败：' +
                        escapeHtml(r && r.error ? r.error : "未知错误") + "</span>";
                }
            })
            .catch(function (e) {
                cell.innerHTML = '<span class="lv-low">失败：' +
                    escapeHtml(String(e && e.message ? e.message : e)) + "</span>";
            })
            .then(function () {
                Array.prototype.forEach.call(document.querySelectorAll(".probe-btn"), function (b) {
                    b.disabled = false;
                });
                fetchStatus();
            });
    }

    function loadDevices() {
        els.deviceBody.innerHTML = '<tr><td colspan="5" class="empty">枚举设备中…（逐个试开 16 kHz，稍等）</td></tr>';
        PPC.api.get("input-devices").then(function (data) {
            var devs = (data && data.devices) || [];
            if (!data || !data.ok || !devs.length) {
                els.deviceBody.innerHTML = '<tr><td colspan="5" class="empty">' +
                    escapeHtml((data && data.error) || "没有找到输入设备") + "</td></tr>";
                return;
            }
            var html = "";
            devs.forEach(function (d) {
                var name = d.name + (d.is_default ? "（系统默认）" : "");
                html += "<tr>" +
                    '<td class="dev-name">' + escapeHtml(name) +
                        (d.ok_16k ? "" : '<div class="dev-bad">本插件用 16 kHz，此设备可能不可用</div>') +
                    "</td>" +
                    "<td>" + escapeHtml(d.hostapi || "-") + "</td>" +
                    '<td class="' + (d.ok_16k ? "dev-ok" : "dev-bad") + '">' + (d.ok_16k ? "可用" : "不可用") + "</td>" +
                    '<td class="lv-cell">—</td>' +
                    '<td><button class="btn probe-btn" data-name="' + escapeHtml(d.name) + '">测电平</button></td>' +
                    "</tr>";
            });
            els.deviceBody.innerHTML = html;
            Array.prototype.forEach.call(els.deviceBody.querySelectorAll(".probe-btn"), function (b) {
                b.addEventListener("click", function () {
                    probeDevice(b, b.getAttribute("data-name"));
                });
            });
        }).catch(function (e) {
            els.deviceBody.innerHTML = '<tr><td colspan="5" class="empty">失败：' +
                escapeHtml(String(e && e.message ? e.message : e)) + "</td></tr>";
        });
    }

    init();
})();
