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
                els.testResult.textContent = data && data.ok
                    ? "时长 " + data.duration + "s，识别：" + (data.text || "（空——请确认麦克风并再说响一点）")
                    : "失败：" + (data && data.error ? data.error : "未知错误");
                return fetchStatus();
            }).catch(function (e) {
                els.testResult.textContent = "失败：" + String(e && e.message ? e.message : e);
            }).then(function () {
                setBusy(false);
                fetchStatus();
            });
        });
    }

    init();
})();
