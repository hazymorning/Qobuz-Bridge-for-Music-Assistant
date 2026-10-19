/*
 * Qobuz Bridge: the Music Assistant part of the panel (status checks, Music
 * Assistant login, which players appear in the Qobuz app).
 *
 * Same style as app.js: ES5, no framework, HTML built from escaped strings.
 * Every URL is relative so the page also works under Home Assistant ingress
 * (<base href> = /api/hassio_ingress/<token>/). app.js keeps running the Qobuz
 * login card and the "Other speakers" section; the only link between the two
 * is window.qobuzDirectBase(), which app.js uses for the login tab.
 */
(function () {
    "use strict";

    var POLL_MS = 3000;
    var DETAILS_KEY = "qobuzBridge.otherSpeakersOpen";

    var LEVELS = {
        ok: { icon: "\u2713", sr: "OK" },
        warn: { icon: "!", sr: "Needs attention" },
        error: { icon: "\u2715", sr: "Problem" },
        pending: { icon: "\u2026", sr: "Waiting" }
    };

    var DEVICE_LABELS = {
        playing: "Playing",
        paused: "Paused",
        idle: "Ready",
        starting: "Starting\u2026",
        disconnected: "Not running"
    };

    var state = {
        status: null,         // what the page shows (server status plus pending edits)
        serverStatus: null,   // the last status the server sent
        available: null,      // false once the server says MA support is absent (404)
        failures: 0,
        lastJson: null,
        mutation: 0,          // bumped on every change, so slower polls can't undo it
        connectBusy: false,
        disconnectBusy: false,
        useToken: false,
        urlEdited: false,
        usernameEdited: false,
        formOpen: false,      // user unfolded the login form of a saved login
        playerRequests: 0,
        playerFailed: false,
        pollTimer: null
    };
    var rendered = { checks: null, hint: null, ma: null, players: null };
    var putChain = null;

    // -------------------------------------------------------------------------
    // Utilities (local copies; app.js keeps its helpers private)
    // -------------------------------------------------------------------------

    function $(id) {
        return document.getElementById(id);
    }

    // Safe for text and for double- or single-quoted attribute values.
    function escapeHtml(text) {
        return String(text === null || text === undefined ? "" : text)
            .replace(/&/g, "&amp;")
            .replace(/</g, "&lt;")
            .replace(/>/g, "&gt;")
            .replace(/"/g, "&quot;")
            .replace(/'/g, "&#39;");
    }

    var escapeAttr = escapeHtml;

    // Only plain web links end up in href/src attributes.
    function safeUrl(url) {
        url = String(url || "");
        return /^https?:\/\/[^\s"'<>]+$/i.test(url) ? url : "";
    }

    function clone(obj) {
        return obj ? JSON.parse(JSON.stringify(obj)) : obj;
    }

    function requestJson(method, url, body) {
        var opts = { method: method, headers: { Accept: "application/json" }, credentials: "same-origin" };
        if (body !== undefined) {
            opts.headers["Content-Type"] = "application/json";
            opts.body = JSON.stringify(body);
        }
        return fetch(url, opts).then(function (r) {
            return r.json().then(
                function (data) { return { ok: r.ok, status: r.status, data: data }; },
                function () { return { ok: r.ok, status: r.status, data: null }; }
            );
        });
    }

    function isUnavailable(res) {
        return res.status === 404 && res.data && res.data.error === "music_assistant_unavailable";
    }

    function setText(el, text) {
        if (el.textContent !== text) el.textContent = text;
    }

    function showBox(el, text, cls) {
        el.hidden = !text;
        el.className = cls ? "ma-message " + cls : el.className;
        setText(el, text || "");
    }

    // -------------------------------------------------------------------------
    // Direct address for the Qobuz login
    // -------------------------------------------------------------------------

    // Hosts that are only reachable from the home network (or the box itself):
    // the add-on's own port on that host works for the Qobuz login tab.
    function isHomeHost(host) {
        host = String(host || "").toLowerCase();
        if (!host) return false;
        if (host === "localhost" || /\.localhost$/.test(host)) return true;
        var m = /^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})$/.exec(host);
        if (m) {
            var a = +m[1];
            var b = +m[2];
            return a === 10 || a === 127 ||
                (a === 192 && b === 168) ||
                (a === 172 && b >= 16 && b <= 31) ||
                (a === 169 && b === 254) ||
                (a === 100 && b >= 64 && b <= 127); // CGNAT range, used by Tailscale
        }
        if (host.charAt(0) === "[") {
            return host === "[::1]" || /^\[f[cd]/.test(host) || /^\[fe[89ab]/.test(host);
        }
        if (host.indexOf(".") === -1) return true; // a bare name like "homeassistant"
        return /\.(local|lan|home|internal|localdomain|home\.arpa)$/.test(host);
    }

    function directPort() {
        var port = String(window.QOBUZ_DIRECT_PORT || "");
        return /^\d+$/.test(port) ? port : "8689";
    }

    function isRemote() {
        return !isHomeHost(window.location.hostname);
    }

    function isFramed() {
        try {
            return window.self !== window.top;
        } catch (e) {
            return true;
        }
    }

    // Base URL ("http://host:port") of the add-on's own web port, or "" if unknown.
    function qobuzDirectBase() {
        if (!isRemote()) {
            return "http://" + window.location.hostname + ":" + directPort();
        }
        var st = state.serverStatus;
        return safeUrl(st && st.direct_login_url).replace(/\/+$/, "");
    }

    function renderLoginHint() {
        var el = $("ma-login-hint");
        if (!el) return;
        var framed = isFramed();
        var remote = isRemote();
        var base = qobuzDirectBase();
        var key = [framed, remote, base].join("|");
        if (key === rendered.hint) return;
        rendered.hint = key;

        // Opened straight from the home network: the login works in this tab.
        if (!framed && !remote) {
            el.hidden = true;
            el.innerHTML = "";
            return;
        }
        var link = base
            ? '<a href="' + escapeAttr(base) + '/" target="_blank" rel="noopener">' + escapeHtml(base) + "</a>"
            : "the add-on's own address (port " + escapeHtml(directPort()) + " on your Home Assistant box)";
        var html = "<p>Do this on your phone while it's on your home Wi\u2011Fi. ";
        if (framed) {
            html += "The Qobuz login opens in a new tab at " + link +
                " \u2014 it won't work through Nabu Casa or other remote access. " +
                "Come back here when you're done.</p>";
        } else {
            html += "It won't work through Nabu Casa or other remote access; log in at " + link + ".</p>";
        }
        if (remote) {
            html += '<p class="ma-hint-warn">You seem to be using remote access; open this panel ' +
                "from home Wi\u2011Fi to log in.</p>";
        }
        if (base && framed) {
            html += '<p class="ma-hint-fallback">No new tab? Open ' + link +
                " in your browser and log in there.</p>";
        }
        el.innerHTML = html;
        el.hidden = false;
    }

    // -------------------------------------------------------------------------
    // Status checks
    // -------------------------------------------------------------------------

    function renderChecks(st) {
        var checks = (st && st.checks) || [];
        var json = JSON.stringify(checks);
        if (json === rendered.checks) return;
        rendered.checks = json;

        var worst = "ok";
        var html = "";
        for (var i = 0; i < checks.length; i++) {
            var c = checks[i] || {};
            var level = LEVELS[c.level] ? c.level : "pending";
            if (level === "error" || (level === "warn" && worst !== "error") ||
                (level === "pending" && worst === "ok")) {
                worst = level;
            }
            html += '<li class="check check-' + level + '">' +
                '<span class="check-icon" aria-hidden="true">' + LEVELS[level].icon + "</span>" +
                '<span class="check-body">' +
                '<span class="check-title"><span class="ma-sr">' + LEVELS[level].sr + ": </span>" +
                escapeHtml(c.title) + "</span>" +
                '<span class="check-detail">' + escapeHtml(c.detail) + "</span>" +
                "</span></li>";
        }
        $("ma-checks").innerHTML = html;
        $("ma-status-card").className = "card ma-only check-card check-card-" + worst;
    }

    // -------------------------------------------------------------------------
    // Music Assistant login card
    // -------------------------------------------------------------------------

    // True while the user is typing in the login form.
    function formHasFocus() {
        var form = $("ma-connect-form");
        var el = document.activeElement;
        return !!(form && el && form.contains(el) && /^(INPUT|TEXTAREA)$/.test(el.tagName));
    }

    // Returns false when the card was left alone because the user is busy in it.
    function renderMA(st) {
        if (state.connectBusy || state.disconnectBusy) return false;
        var conn = st.connection || {};
        var view = !st.supported ? "unsupported" : conn.state === "connected" ? "connected" : "connect";
        if (view !== "connect") state.formOpen = false;
        // A saved login that is only (re)connecting doesn't need typing again.
        var folded = view === "connect" && !!st.configured && !state.formOpen &&
            (conn.state === "connecting" || conn.state === "disconnected");
        var key = JSON.stringify([view, st.support_error, conn, st.configured, st.suggested_url, folded]);
        if (key === rendered.ma) return true;
        var prevView = rendered.ma ? JSON.parse(rendered.ma)[0] : null;
        // Don't move the form around while someone is typing in it.
        if (view === "connect" && prevView === "connect" && formHasFocus()) return false;
        rendered.ma = key;

        $("ma-unsupported").hidden = view !== "unsupported";
        $("ma-connected").hidden = view !== "connected";
        $("ma-connect").hidden = view !== "connect";

        if (view === "unsupported") {
            setText($("ma-unsupported"), st.support_error ||
                "Music Assistant support is not installed in this build.");
            return true;
        }

        if (view === "connected") {
            var html = "Connected to Music Assistant";
            if (conn.server_version) html += " <strong>" + escapeHtml(conn.server_version) + "</strong>";
            if (conn.url) html += ' at <strong>' + escapeHtml(conn.url) + "</strong>";
            if (conn.username) html += " as <strong>" + escapeHtml(conn.username) + "</strong>";
            $("ma-connected-text").innerHTML = html + ".";
            return true;
        }

        // Not connected: explain why (if we know), then the login form.
        var msg = $("ma-conn-message");
        if (conn.state === "auth_failed") {
            showBox(msg, conn.message || "Music Assistant no longer accepts the saved login. Connect again.",
                "ma-message-error");
        } else if (conn.state === "disconnected" && st.configured) {
            showBox(msg, (conn.message || "Can't reach Music Assistant.") +
                " The add-on keeps trying on its own; you only need to connect again if the " +
                "address or login changed.",
                "ma-message-warn");
        } else if (conn.state === "connecting" && st.configured) {
            showBox(msg, "Connecting to Music Assistant" + (conn.url ? " at " + conn.url : "") +
                ". This takes a few seconds.", "ma-message-pending");
        } else {
            showBox(msg, "", "");
        }

        var urlInput = $("ma-url");
        if (!state.urlEdited && document.activeElement !== urlInput) {
            urlInput.value = st.suggested_url || conn.url || "";
        }
        var userInput = $("ma-username");
        if (!state.usernameEdited && document.activeElement !== userInput && conn.username) {
            userInput.value = conn.username;
        }
        $("ma-saved-actions").hidden = !folded;
        $("ma-connect-form").hidden = folded;
        $("ma-change-btn").setAttribute("aria-expanded", folded ? "false" : "true");
        $("ma-forget-btn").hidden = !st.configured;
        return true;
    }

    function onChangeLogin() {
        state.formOpen = true;
        rendered.ma = null;
        if (state.serverStatus) renderMA(state.serverStatus);
        $("ma-url").focus();
    }

    function showConnectError(text) {
        var el = $("ma-connect-error");
        el.hidden = !text;
        setText(el, text || "");
    }

    function setConnectBusy(busy) {
        state.connectBusy = busy;
        var btn = $("ma-connect-btn");
        btn.disabled = busy;
        btn.textContent = busy ? "Connecting\u2026" : "Connect";
        btn.setAttribute("aria-busy", busy ? "true" : "false");
        var fields = ["ma-url", "ma-username", "ma-password", "ma-token"];
        for (var i = 0; i < fields.length; i++) $(fields[i]).readOnly = busy;
        $("ma-token-toggle").disabled = busy;
    }

    function setTokenMode(on) {
        state.useToken = on;
        $("ma-cred-fields").hidden = on;
        $("ma-token-fields").hidden = !on;
        var toggle = $("ma-token-toggle");
        toggle.textContent = on ? "use username and password instead" : "use a token instead";
        toggle.setAttribute("aria-expanded", on ? "true" : "false");
        showConnectError("");
        (on ? $("ma-token") : $("ma-username")).focus();
    }

    function onConnectSubmit(ev) {
        ev.preventDefault();
        if (state.connectBusy) return;
        var body = { url: $("ma-url").value.trim() };
        if (state.useToken) {
            body.token = $("ma-token").value.trim();
            if (!body.token) {
                showConnectError("Paste your Music Assistant token.");
                $("ma-token").focus();
                return;
            }
        } else {
            body.username = $("ma-username").value.trim();
            body.password = $("ma-password").value;
            if (!body.username || !body.password) {
                showConnectError("Enter your Music Assistant username and password.");
                $(body.username ? "ma-password" : "ma-username").focus();
                return;
            }
        }
        showConnectError("");
        setConnectBusy(true);
        state.mutation++;
        requestJson("POST", "api/ma/connect", body).then(function (res) {
            setConnectBusy(false);
            if (res.ok && res.data && res.data.ok) {
                $("ma-password").value = "";
                $("ma-token").value = "";
                state.urlEdited = false;
                state.usernameEdited = false;
                rendered.ma = null;
                if (res.data.status) applyStatus(res.data.status);
                schedulePoll(0);
                return;
            }
            if (isUnavailable(res)) {
                setAvailable(false);
                return;
            }
            showConnectError((res.data && res.data.error) ||
                "Couldn't connect to Music Assistant. Please try again.");
            var retry = state.useToken ? $("ma-token") : $("ma-password");
            retry.focus();
            if (retry.select) retry.select();
        }, function () {
            setConnectBusy(false);
            showConnectError("Couldn't reach the add-on. Check your connection and try again.");
        });
    }

    function onDisconnect(ev) {
        var btn = ev.currentTarget;
        if (state.disconnectBusy) return;
        if (!window.confirm("Disconnect from Music Assistant? Your Music Assistant players disappear " +
            "from the Qobuz app until you connect again.")) {
            return;
        }
        var label = btn.textContent;
        state.disconnectBusy = true;
        state.mutation++;
        btn.disabled = true;
        btn.textContent = "Disconnecting\u2026";
        var done = function () {
            state.disconnectBusy = false;
            btn.disabled = false;
            btn.textContent = label;
            rendered.ma = null;
            state.lastJson = null;
            schedulePoll(0);
        };
        requestJson("POST", "api/ma/disconnect").then(function (res) {
            if (isUnavailable(res)) setAvailable(false);
            done();
        }, done);
    }

    // -------------------------------------------------------------------------
    // Players
    // -------------------------------------------------------------------------

    function isTicked(st, p) {
        return st.expose_all ? !!p.exposed : !!p.selected;
    }

    function badge(text, cls) {
        return '<span class="badge-ma ' + cls + '">' + escapeHtml(text) + "</span>";
    }

    function renderPlayer(st, p, index) {
        var id = "ma-player-" + index;
        var ticked = isTicked(st, p);
        var dev = p.device || null;
        var html = '<li class="ma-player' + (ticked ? " is-on" : "") + (st.expose_all ? " is-locked" : "") + '">';
        html += '<label class="ma-player-row" for="' + id + '">';
        html += '<input type="checkbox" class="ma-player-check" id="' + id + '" data-player-id="' +
            escapeAttr(p.player_id) + '"' + (ticked ? " checked" : "") + (st.expose_all ? " disabled" : "") +
            ' aria-describedby="' + id + '-sub">';

        html += '<span class="ma-player-main">';
        html += '<span class="ma-player-name">' + escapeHtml(p.ma_name || p.player_id) + "</span>";
        var badges = "";
        if (p.type === "group") badges += badge("Group", "badge-ma-group");
        if (p.type === "stereo_pair") badges += badge("Stereo pair", "badge-ma-group");
        if (p.available === false) badges += badge("Offline", "badge-ma-offline");
        if (p.hidden) badges += badge("Hidden in MA", "badge-ma-muted");
        if (p.in_yaml) badges += badge("In config.yaml", "badge-ma-muted");
        if (badges) html += '<span class="ma-badges">' + badges + "</span>";
        var sub;
        if (p.in_yaml) {
            sub = "Set up in config.yaml (see Other speakers)";
        } else if (p.exposed && p.qobuz_name) {
            sub = "Shows as \u201c" + p.qobuz_name + "\u201d";
        } else if (p.exposed) {
            sub = "Shown in the Qobuz app";
        } else {
            sub = "Not in the Qobuz app";
        }
        html += '<span class="ma-player-sub" id="' + id + '-sub">' + escapeHtml(sub) + "</span>";
        html += "</span>";

        if (dev && dev.status) {
            var label = DEVICE_LABELS[dev.status] || dev.status;
            html += '<span class="ma-player-status ma-status-' + escapeAttr(dev.status) + '">' +
                escapeHtml(label) + "</span>";
        }
        var np = dev && dev.now_playing;
        if (np && (dev.status === "playing" || dev.status === "paused") && (np.title || np.artist)) {
            var art = safeUrl(np.album_art_url);
            var full = (np.title || "") + (np.title && np.artist ? " \u2013 " : "") + (np.artist || "");
            html += '<span class="ma-np" title="' + escapeAttr(full) + '">';
            html += art
                ? '<img class="ma-np-art" src="' + escapeAttr(art) + '" alt="" loading="lazy" referrerpolicy="no-referrer">'
                : '<span class="ma-np-art ma-np-art-empty" aria-hidden="true">\u266b</span>';
            html += '<span class="ma-np-text">';
            if (np.title) html += '<span class="ma-np-title">' + escapeHtml(np.title) + "</span>";
            if (np.artist) {
                html += '<span class="ma-np-artist">' + (np.title ? '<span class="ma-sr">\u2013 </span>' : "") +
                    escapeHtml(np.artist) + "</span>";
            }
            html += "</span></span>";
        }
        html += "</label></li>";
        return html;
    }

    function renderPlayers(st) {
        var conn = st.connection || {};
        var players = st.players || [];
        // Without MA support the login card already says so; nothing to pick here.
        $("ma-players-card").hidden = !st.supported;
        $("ma-expose-all").checked = !!st.expose_all;
        $("ma-name-suffix").checked = !!st.name_suffix;

        var key = JSON.stringify([st.supported, st.configured, st.expose_all, conn.state, players]);
        if (key === rendered.players) return;
        rendered.players = key;

        var empty = "";
        if (!st.supported) {
            empty = "Music Assistant support is not installed in this build.";
        } else if (!st.configured) {
            empty = "Connect to Music Assistant to see your players.";
        } else if (!players.length) {
            empty = conn.state === "connected"
                ? "Music Assistant has no players yet."
                : "Your players show up here once Music Assistant is connected.";
        }
        var emptyEl = $("ma-players-empty");
        emptyEl.hidden = !empty;
        setText(emptyEl, empty);
        var note = "";
        if (!empty) {
            note = st.expose_all
                ? "Players hidden in Music Assistant are left out. To pick players yourself, " +
                  "switch off \u201cAll my Music Assistant players\u201d."
                : "Tick the players and groups you want in the Qobuz app.";
        }
        var noteEl = $("ma-players-note");
        noteEl.hidden = !note;
        setText(noteEl, note);

        var list = $("ma-players");
        // Keep keyboard focus on the same player across re-renders.
        var focusedId = null;
        if (document.activeElement && list.contains(document.activeElement)) {
            focusedId = document.activeElement.getAttribute("data-player-id");
        }
        var html = "";
        if (!empty) {
            for (var i = 0; i < players.length; i++) html += renderPlayer(st, players[i], i);
        }
        list.innerHTML = html;
        list.hidden = !html;
        if (focusedId !== null) {
            var boxes = list.querySelectorAll("input[data-player-id]");
            for (var j = 0; j < boxes.length; j++) {
                if (boxes[j].getAttribute("data-player-id") === focusedId) boxes[j].focus();
            }
        }
    }

    var playersErrorTimer = null;

    function showPlayersError(text) {
        var el = $("ma-players-error");
        el.hidden = !text;
        setText(el, text || "");
        if (playersErrorTimer) clearTimeout(playersErrorTimer);
        playersErrorTimer = text ? setTimeout(function () { showPlayersError(""); }, 10000) : null;
    }

    // Optimistic change: show it now, send it, put the server's view back if it fails.
    function sendExposure(patch, edit) {
        if (!state.status) return;
        var draft = clone(state.status);
        edit(draft);
        state.status = draft;
        state.playerRequests++;
        state.mutation++;
        showPlayersError("");
        renderPlayers(draft);

        var send = function () {
            return requestJson("PUT", "api/ma/players", patch).then(function (res) {
                if (res.ok && res.data && res.data.ok) {
                    if (res.data.status) state.serverStatus = res.data.status;
                    return "";
                }
                if (isUnavailable(res)) return "Music Assistant support is not available.";
                return (res.data && res.data.error) || "Couldn't save that change. Please try again.";
            }, function () {
                return "Couldn't reach the add-on, so that change was not saved.";
            });
        };
        putChain = (putChain || Promise.resolve()).then(send).then(function (error) {
            state.playerRequests--;
            if (error) {
                state.playerFailed = true;
                showPlayersError(error);
            }
            if (state.playerRequests === 0) {
                var failed = state.playerFailed;
                state.playerFailed = false;
                state.mutation++;
                state.lastJson = null;
                rendered.players = null;
                applyStatus(state.serverStatus);
                if (failed) schedulePoll(0);
            }
        });
    }

    function onPlayerChange(ev) {
        var box = ev.target;
        if (!box || !box.hasAttribute || !box.hasAttribute("data-player-id")) return;
        var st = state.status;
        if (!st || st.expose_all) {
            box.checked = !box.checked;
            return;
        }
        var pid = box.getAttribute("data-player-id");
        var on = box.checked;
        var selected = [];
        var players = st.players || [];
        for (var i = 0; i < players.length; i++) {
            var p = players[i];
            var isSel = p.player_id === pid ? on : !!p.selected;
            if (isSel) selected.push(p.player_id);
        }
        sendExposure({ selected: selected }, function (draft) {
            for (var k = 0; k < draft.players.length; k++) {
                var dp = draft.players[k];
                if (dp.player_id === pid) {
                    dp.selected = on;
                    dp.exposed = on && !dp.in_yaml;
                }
            }
        });
    }

    function onExposeAllChange(ev) {
        var on = ev.target.checked;
        var st = state.status;
        if (!st) return;
        var players = st.players || [];
        var patch = { expose_all: on };
        var anySelected = false;
        for (var i = 0; i < players.length; i++) {
            if (players[i].selected) anySelected = true;
        }
        // Switching "All" off with nothing ticked yet: start from what is shared
        // now, so the Qobuz app doesn't suddenly lose every device.
        if (!on && !anySelected) {
            patch.selected = [];
            for (var j = 0; j < players.length; j++) {
                if (players[j].exposed) patch.selected.push(players[j].player_id);
            }
        }
        sendExposure(patch, function (draft) {
            draft.expose_all = on;
            for (var k = 0; k < draft.players.length; k++) {
                var p = draft.players[k];
                if (patch.selected) p.selected = patch.selected.indexOf(p.player_id) !== -1;
                p.exposed = !p.in_yaml && (on ? !p.hidden : !!p.selected);
            }
        });
    }

    function onNameSuffixChange(ev) {
        var on = ev.target.checked;
        sendExposure({ name_suffix: on }, function (draft) {
            draft.name_suffix = on;
        });
    }

    // -------------------------------------------------------------------------
    // Polling
    // -------------------------------------------------------------------------

    function setAvailable(available) {
        if (state.available === available) return;
        state.available = available;
        var body = document.body;
        if (available) {
            body.classList.add("ma-ready");
            body.classList.remove("ma-unavailable");
        } else {
            // No Music Assistant manager: plain QobuzProxy, so show its speakers.
            body.classList.add("ma-unavailable");
            body.classList.remove("ma-ready");
            var details = $("other-speakers");
            if (details) details.open = true;
        }
    }

    function applyStatus(st) {
        if (!st) return;
        setAvailable(true);
        state.serverStatus = st;
        if (state.playerRequests === 0) state.status = st;
        var json = JSON.stringify(st);
        if (json === state.lastJson) return;
        state.lastJson = json;

        renderChecks(st);
        renderLoginHint();
        // If the MA card was left alone (user typing), try again on the next poll.
        if (!renderMA(st)) state.lastJson = null;
        if (state.playerRequests === 0) renderPlayers(state.status);
    }

    function setOffline(offline) {
        $("ma-offline").hidden = !offline;
    }

    function poll() {
        var seq = state.mutation;
        requestJson("GET", "api/ma/status").then(null, function () {
            return null; // network error (add-on restarting, Wi-Fi gone)
        }).then(function (res) {
            schedulePoll(POLL_MS);
            if (res && isUnavailable(res)) {
                state.failures = 0;
                setOffline(false);
                setAvailable(false);
                return;
            }
            if (!res || !res.ok || !res.data) {
                state.failures++;
                if (state.failures >= 2 && state.available) setOffline(true);
                return;
            }
            state.failures = 0;
            setOffline(false);
            // Something changed while this was in flight; its answer may be stale.
            if (seq !== state.mutation) return;
            applyStatus(res.data);
        });
    }

    function schedulePoll(delay) {
        if (state.pollTimer) clearTimeout(state.pollTimer);
        state.pollTimer = setTimeout(function () {
            state.pollTimer = null;
            poll();
        }, delay);
    }

    // -------------------------------------------------------------------------
    // Setup
    // -------------------------------------------------------------------------

    function restoreDetails() {
        var details = $("other-speakers");
        if (!details) return;
        try {
            if (window.localStorage.getItem(DETAILS_KEY) === "1") details.open = true;
        } catch (e) {
            // storage blocked (private mode, sandboxed frame): just start closed
        }
        details.addEventListener("toggle", function () {
            try {
                window.localStorage.setItem(DETAILS_KEY, details.open ? "1" : "0");
            } catch (e) {
                // ignore
            }
        });
    }

    function init() {
        $("ma-connect-form").addEventListener("submit", onConnectSubmit);
        $("ma-token-toggle").addEventListener("click", function () {
            setTokenMode(!state.useToken);
        });
        $("ma-url").addEventListener("input", function () {
            state.urlEdited = true;
        });
        $("ma-username").addEventListener("input", function () {
            state.usernameEdited = true;
        });
        $("ma-disconnect-btn").addEventListener("click", onDisconnect);
        $("ma-forget-btn").addEventListener("click", onDisconnect);
        $("ma-forget-btn-2").addEventListener("click", onDisconnect);
        $("ma-change-btn").addEventListener("click", onChangeLogin);
        $("ma-players").addEventListener("change", onPlayerChange);
        $("ma-expose-all").addEventListener("change", onExposeAllChange);
        $("ma-name-suffix").addEventListener("change", onNameSuffixChange);

        // Coming back from the Qobuz login tab: refresh right away.
        document.addEventListener("visibilitychange", function () {
            if (!document.hidden) schedulePoll(0);
        });
        window.addEventListener("focus", function () {
            schedulePoll(0);
        });

        restoreDetails();
        renderLoginHint();
        poll();
    }

    window.qobuzDirectBase = qobuzDirectBase;

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", init);
    } else {
        init();
    }
})();
