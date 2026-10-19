/*
 * The add-on panel: status checks, Qobuz login, Music Assistant login and the
 * players shown in the Qobuz app. Every URL is relative, so the page also works
 * under Home Assistant ingress (<base href> = /api/hassio_ingress/<token>/).
 */
(function () {
    "use strict";

    var POLL_MS = 3000;
    var ICONS = { ok: "✓", warn: "!", error: "✕", pending: "…" };
    var TYPES = { group: "Group", stereo_pair: "Stereo pair" };

    var last = null; // the last status from the server
    var drawn = {}; // what each section shows now, to leave unchanged ones alone
    var pollTimer = null;
    var failures = 0;
    var busy = false; // a change is being sent; polls must not undo it on screen

    function $(id) {
        return document.getElementById(id);
    }

    function el(tag, className, text) {
        var node = document.createElement(tag);
        if (className) node.className = className;
        if (text) node.textContent = text;
        return node;
    }

    function show(node, visible) {
        node.hidden = !visible;
    }

    function request(method, url, body) {
        var options = { method: method, headers: { Accept: "application/json" } };
        if (body !== undefined) {
            options.headers["Content-Type"] = "application/json";
            options.body = JSON.stringify(body);
        }
        return fetch(url, options).then(function (response) {
            return response.json().catch(function () {
                return {};
            }).then(function (data) {
                if (!response.ok) throw new Error(data.error || "That did not work. Please try again.");
                return data;
            });
        }, function () {
            throw new Error("Couldn't reach the add-on. Check your connection and try again.");
        });
    }

    // The Qobuz login can't run inside Home Assistant (Qobuz refuses to be framed and the
    // callback can't come back through ingress), so it runs on the add-on's own port.
    function loginBase() {
        var url = last && last.login_url;
        if (url && /^http:\/\/[0-9.]+:[0-9]+$/.test(url)) return url;
        return "http://" + window.location.hostname + ":" + document.body.dataset.port;
    }

    function isFramed() {
        try {
            return window.self !== window.top;
        } catch (e) {
            return true;
        }
    }

    function onLogin() {
        if (isFramed()) {
            var base = loginBase();
            window.open(base + "/auth/login?origin=" + encodeURIComponent(base), "_blank");
        } else {
            var origin = document.baseURI.replace(/\/$/, "");
            window.location.href = "auth/login?origin=" + encodeURIComponent(origin);
        }
    }

    function onLogout() {
        request("POST", "api/auth/logout").then(poll, poll);
    }

    // Rebuilding a section moves keyboard focus and makes screen readers repeat it.
    function changed(section, data) {
        var key = JSON.stringify(data);
        if (drawn[section] === key) return false;
        drawn[section] = key;
        return true;
    }

    function renderChecks(st) {
        if (!changed("checks", st.checks)) return;
        var list = $("checks");
        list.textContent = "";
        st.checks.forEach(function (check) {
            var item = el("li", "check check-" + check.level);
            item.appendChild(el("span", "check-icon", ICONS[check.level]));
            var body = el("span", "check-body");
            body.appendChild(el("strong", "", check.title));
            body.appendChild(el("span", "", check.detail));
            item.appendChild(body);
            list.appendChild(item);
        });
    }

    function renderQobuz(st) {
        show($("qobuz-logged-out"), !st.qobuz.logged_in);
        show($("qobuz-logged-in"), st.qobuz.logged_in);
        $("qobuz-account").textContent = st.qobuz.account || "your Qobuz account";
        show($("qobuz-login-note"), isFramed());
        $("qobuz-login-link").textContent = loginBase();
        $("qobuz-login-link").href = loginBase() + "/";
    }

    function renderMusicAssistant(st) {
        var form = $("music-assistant-form");
        if (form.contains(document.activeElement)) return; // don't move things while typing
        var conn = st.connection;
        var connected = conn.state === "connected";
        show($("music-assistant-connected"), connected);
        show(form, !connected);
        if (connected) {
            $("music-assistant-connected-text").textContent =
                "Connected to Music Assistant " + conn.server_version + " at " + conn.url +
                (conn.username ? " as " + conn.username : "") + ".";
            return;
        }
        var message = "";
        if (st.is_configured && conn.state === "connecting") {
            message = "Connecting to Music Assistant at " + conn.url + "…";
        } else if (st.is_configured && conn.state === "disconnected") {
            message = conn.message + " The add-on keeps trying on its own; connect again only " +
                "if the address or login changed.";
        } else if (conn.state === "auth_failed" || conn.message) {
            message = conn.message;
        }
        $("music-assistant-message").textContent = message;
        show($("music-assistant-message"), !!message);
        if (!$("music-assistant-url").value) $("music-assistant-url").value = conn.url;
        if (!$("music-assistant-username").value) $("music-assistant-username").value = conn.username;
    }

    function renderPlayers(st) {
        $("expose-all").checked = st.expose_all;
        if (!changed("players", [st.expose_all, st.is_configured, st.players])) return;
        var list = $("players");
        list.textContent = "";
        st.players.forEach(function (p) {
            var shown = !!p.speaker_name;
            var box = el("input");
            box.type = "checkbox";
            box.dataset.id = p.player_id;
            box.checked = st.expose_all ? shown : p.selected;
            box.disabled = st.expose_all;
            var label = el("label", "player");
            label.appendChild(box);
            var text = el("span");
            text.appendChild(el("span", "player-name", p.name));
            if (TYPES[p.type]) text.appendChild(el("span", "badge", TYPES[p.type]));
            if (!p.available) text.appendChild(el("span", "badge", "Offline"));
            if (p.hidden) text.appendChild(el("span", "badge", "Hidden in Music Assistant"));
            text.appendChild(el("span", "note",
                shown ? "Shows as “" + p.speaker_name + "”" : "Not in the Qobuz app"));
            label.appendChild(text);
            var item = el("li");
            item.appendChild(label);
            list.appendChild(item);
        });
        var empty = "";
        if (!st.is_configured) empty = "Connect to Music Assistant to see your players.";
        else if (!st.players.length) empty = "Your players show up here once Music Assistant is connected.";
        $("players-empty").textContent = empty;
        show($("players-empty"), !!empty);
    }

    function render(st) {
        last = st;
        renderChecks(st);
        renderQobuz(st);
        renderMusicAssistant(st);
        renderPlayers(st);
    }

    function poll() {
        clearTimeout(pollTimer);
        request("GET", "api/music-assistant/status").then(function (st) {
            failures = 0;
            show($("offline"), false);
            if (!busy) render(st);
        }, function () {
            failures += 1;
            show($("offline"), failures >= 2); // add-on restarting, or the phone left the Wi-Fi
        }).then(function () {
            clearTimeout(pollTimer);
            pollTimer = setTimeout(poll, POLL_MS);
        });
    }

    // Send one change; the answer is the new status.
    function send(method, url, body, errorBox) {
        busy = true;
        show(errorBox, false);
        return request(method, url, body).then(function (st) {
            busy = false;
            render(st);
        }, function (err) {
            busy = false;
            errorBox.textContent = err.message;
            show(errorBox, true);
            drawn = {};
            if (last) render(last); // put back what the server has
            throw err;
        });
    }

    function onConnect(event) {
        event.preventDefault();
        var body = {
            url: $("music-assistant-url").value.trim(),
            username: $("music-assistant-username").value.trim(),
            password: $("music-assistant-password").value
        };
        $("music-assistant-connect").disabled = true;
        $("music-assistant-connect").textContent = "Connecting…";
        document.activeElement.blur();
        send("POST", "api/music-assistant/connect", body, $("music-assistant-error")).then(function () {
            $("music-assistant-password").value = "";
        }, function () {}).then(function () {
            $("music-assistant-connect").disabled = false;
            $("music-assistant-connect").textContent = "Connect";
        });
    }

    function onDisconnect() {
        if (window.confirm("Disconnect from Music Assistant? Your Music Assistant players " +
                "disappear from the Qobuz app until you connect again.")) {
            send("POST", "api/music-assistant/disconnect", undefined, $("music-assistant-error")).catch(function () {});
        }
    }

    function onExposeAll(event) {
        var on = event.target.checked;
        var body = { expose_all: on };
        // Switching "all" off with nothing ticked yet: start from what the Qobuz app shows now,
        // so it doesn't lose every speaker at once.
        if (!on && last && !last.players.some(function (p) { return p.selected; })) {
            body.selected = last.players.filter(function (p) {
                return p.speaker_name;
            }).map(function (p) {
                return p.player_id;
            });
        }
        send("PUT", "api/music-assistant/players", body, $("players-error")).catch(function () {});
    }

    function onPlayerTicked(event) {
        var box = event.target;
        if (!box.dataset.id || !last) return;
        var selected = last.players.filter(function (p) {
            return p.player_id === box.dataset.id ? box.checked : p.selected;
        }).map(function (p) {
            return p.player_id;
        });
        send("PUT", "api/music-assistant/players", { selected: selected }, $("players-error")).catch(function () {});
    }

    $("qobuz-login").addEventListener("click", onLogin);
    $("qobuz-logout").addEventListener("click", onLogout);
    $("music-assistant-form").addEventListener("submit", onConnect);
    $("music-assistant-disconnect").addEventListener("click", onDisconnect);
    $("expose-all").addEventListener("change", onExposeAll);
    $("players").addEventListener("change", onPlayerTicked);
    // Coming back from the Qobuz login tab: refresh right away
    window.addEventListener("focus", poll);
    poll();
})();
