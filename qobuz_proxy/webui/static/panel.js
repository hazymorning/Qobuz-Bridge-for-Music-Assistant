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
        if (method !== "GET") {
            // The add-on only accepts JSON for changes (no cross-site form posts)
            options.headers["Content-Type"] = "application/json";
            options.body = JSON.stringify(body || {});
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

    function renderChecks(status) {
        if (!changed("checks", status.checks)) return;
        var list = $("checks");
        list.textContent = "";
        status.checks.forEach(function (check) {
            var item = el("li", "check check-" + check.level);
            item.appendChild(el("span", "check-icon", ICONS[check.level]));
            var body = el("span", "check-body");
            body.appendChild(el("strong", "", check.title));
            body.appendChild(el("span", "", check.detail));
            item.appendChild(body);
            list.appendChild(item);
        });
    }

    function renderQobuz(status) {
        if (status.qobuz.logged_in) show($("qobuz-error"), false);
        show($("qobuz-logged-out"), !status.qobuz.logged_in);
        show($("qobuz-logged-in"), status.qobuz.logged_in);
        $("qobuz-account").textContent = status.qobuz.account || "your Qobuz account";
        show($("qobuz-login-note"), isFramed());
        $("qobuz-login-link").textContent = loginBase();
        $("qobuz-login-link").href = loginBase() + "/";
    }

    function renderMusicAssistant(status) {
        var form = $("music-assistant-form");
        if (form.contains(document.activeElement)) return; // don't move things while typing
        var connection = status.connection;
        var connected = connection.state === "connected";
        show($("music-assistant-connected"), connected);
        show(form, !connected);
        if (connected) {
            $("music-assistant-connected-text").textContent =
                "Connected to Music Assistant " + connection.server_version + " at " + connection.url +
                (connection.username ? " as " + connection.username : "") + ".";
            return;
        }
        var message = "";
        if (status.is_configured && connection.state === "connecting") {
            message = "Connecting to Music Assistant at " + connection.url + "…";
        } else if (status.is_configured && connection.state === "disconnected") {
            message = connection.message + " The add-on keeps trying on its own; connect again only " +
                "if the address or login changed.";
        } else if (connection.state === "auth_failed" || connection.message) {
            message = connection.message;
        }
        $("music-assistant-message").textContent = message;
        show($("music-assistant-message"), !!message);
        if (!$("music-assistant-url").value) $("music-assistant-url").value = connection.url;
        if (!$("music-assistant-username").value) $("music-assistant-username").value = connection.username;
    }

    function renderPlayers(status) {
        var connected = status.connection.state === "connected";
        $("expose-all").checked = status.expose_all;
        if (!changed("players", [status.expose_all, status.is_configured, connected, status.players])) return;
        var list = $("players");
        list.textContent = "";
        status.players.forEach(function (p) {
            var shown = !!p.speaker_name;
            var box = el("input");
            box.type = "checkbox";
            box.dataset.id = p.player_id;
            box.checked = status.expose_all ? shown : p.selected;
            box.disabled = status.expose_all;
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
        if (!status.is_configured) empty = "Connect to Music Assistant to see your players.";
        else if (!status.players.length && connected) empty = "Music Assistant has no players yet.";
        else if (!status.players.length) empty = "Your players show up here once Music Assistant is connected.";
        $("players-empty").textContent = empty;
        show($("players-empty"), !!empty);
    }

    function render(status) {
        last = status;
        renderChecks(status);
        renderQobuz(status);
        renderMusicAssistant(status);
        renderPlayers(status);
    }

    function poll() {
        clearTimeout(pollTimer);
        if (document.hidden) return; // resumes on visibilitychange
        request("GET", "api/music-assistant/status").then(function (status) {
            failures = 0;
            show($("offline"), false);
            if (!busy) render(status);
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
        return request(method, url, body).then(function (status) {
            busy = false;
            render(status);
        }, function (error) {
            busy = false;
            errorBox.textContent = error.message;
            show(errorBox, true);
            drawn = {};
            if (last) render(last); // put back what the server has
            throw error;
        });
    }

    function usesToken() {
        return !$("music-assistant-token-login").hidden;
    }

    function onLoginSwitch() {
        var token = !usesToken();
        show($("music-assistant-token-login"), token);
        show($("music-assistant-password-login"), !token);
        $("music-assistant-login-switch").textContent =
            token ? "Use a username and password instead" : "Use a token instead";
        $(token ? "music-assistant-token" : "music-assistant-username").focus();
    }

    function onConnect(event) {
        event.preventDefault();
        var body = { url: $("music-assistant-url").value.trim() };
        if (usesToken()) {
            body.token = $("music-assistant-token").value.trim();
        } else {
            body.username = $("music-assistant-username").value.trim();
            body.password = $("music-assistant-password").value;
        }
        $("music-assistant-connect").disabled = true;
        $("music-assistant-connect").textContent = "Connecting…";
        document.activeElement.blur();
        send("POST", "api/music-assistant/connect", body, $("music-assistant-error")).then(function () {
            $("music-assistant-password").value = "";
            $("music-assistant-token").value = "";
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

    // The Qobuz login comes back with ?error=... when it failed
    if (new URLSearchParams(window.location.search).get("error")) {
        $("qobuz-error").textContent = "The Qobuz login did not finish. Please try again.";
        show($("qobuz-error"), true);
        // Keep the path: under Home Assistant ingress it is /api/hassio_ingress/<token>/
        history.replaceState(null, "", window.location.pathname + window.location.hash);
    }

    $("qobuz-login").addEventListener("click", onLogin);
    $("qobuz-logout").addEventListener("click", onLogout);
    $("music-assistant-form").addEventListener("submit", onConnect);
    $("music-assistant-login-switch").addEventListener("click", onLoginSwitch);
    $("music-assistant-disconnect").addEventListener("click", onDisconnect);
    $("expose-all").addEventListener("change", onExposeAll);
    $("players").addEventListener("change", onPlayerTicked);
    // Coming back from the Qobuz login tab: refresh right away
    window.addEventListener("focus", poll);
    document.addEventListener("visibilitychange", poll);
    poll();
})();
