/*
 * The add-on panel: status checks, Qobuz login, Music Assistant login, the
 * players shown in the Qobuz app and the party speakers. Every URL is relative,
 * so the page also works under Home Assistant ingress
 * (<base href> = /api/hassio_ingress/<token>/).
 */
(function () {
    "use strict";

    var POLL_MS = 3000;
    var ICONS = { ok: "✓", warn: "!", error: "✕", pending: "…" };
    var TYPES = { group: "Group", stereo_pair: "Stereo pair" };
    var GROUPINGS = { music_assistant: "Music Assistant", home_assistant: "Home Assistant" };

    var last = null; // the last status from the server
    var drawn = {}; // what each section shows now, to leave unchanged ones alone
    var pollTimer = null;
    var failures = 0;
    var busy = false; // a change is being sent; polls must not undo it on screen
    var renaming = null; // the player whose speaker name is being edited
    var party = null; // the party speaker form while it is open (see openPartyForm)

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

    function playersKey(status) {
        return [status.expose_all, status.is_configured, status.connection.state, status.players];
    }

    function renderPlayers(status) {
        $("expose-all").checked = status.expose_all;
        if (renaming !== null) return; // the list waits while a name is typed
        if (!changed("players", playersKey(status))) return;
        drawPlayers(status);
    }

    function drawPlayers(status) {
        drawn.players = JSON.stringify(playersKey(status));
        var connected = status.connection.state === "connected";
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
            item.appendChild(renaming === p.player_id ? renameForm(p) : renameButton(p));
            list.appendChild(item);
        });
        var empty = "";
        if (!status.is_configured) empty = "Connect to Music Assistant to see your players.";
        else if (!status.players.length && connected) empty = "Music Assistant has no players yet.";
        else if (!status.players.length) empty = "Your players show up here once Music Assistant is connected.";
        $("players-empty").textContent = empty;
        show($("players-empty"), !!empty);
    }

    function renameButton(p) {
        var button = el("button", "link-button rename", "Rename");
        button.type = "button";
        button.addEventListener("click", function () {
            renaming = p.player_id;
            show($("players-error"), false);
            drawPlayers(last);
            $("players").querySelector(".rename-form input").focus();
        });
        return button;
    }

    function renameForm(p) {
        var form = el("form", "rename-form");
        var label = el("label", "", "Name in the Qobuz app");
        var input = el("input");
        input.type = "text";
        input.maxLength = 40;
        input.autocomplete = "off";
        input.value = p.custom_name;
        input.placeholder = p.default_name;
        label.appendChild(input);
        var save = el("button", "", "Save");
        save.type = "submit";
        var cancel = el("button", "button-secondary", "Cancel");
        cancel.type = "button";
        var buttons = el("div", "form-buttons");
        buttons.appendChild(save);
        buttons.appendChild(cancel);
        form.appendChild(label);
        form.appendChild(buttons);
        form.appendChild(el("p", "note", "Leave it empty for “" + p.default_name + "”."));
        function done() {
            renaming = null;
            drawPlayers(last);
        }
        form.addEventListener("submit", function (event) {
            event.preventDefault();
            save.disabled = true;
            var body = { player_id: p.player_id, name: input.value.trim() };
            send("PUT", "api/music-assistant/speaker-name", body, $("players-error")).then(done, function () {
                save.disabled = false;
            });
        });
        cancel.addEventListener("click", done);
        return form;
    }

    function partiesKey(status) {
        return [status.party_speakers, status.players];
    }

    function renderParties(status) {
        if (party !== null) return; // the section waits while the form is open
        if (!changed("parties", partiesKey(status))) return;
        drawParties(status);
    }

    function drawParties(status) {
        drawn.parties = JSON.stringify(partiesKey(status));
        var names = {};
        status.players.forEach(function (p) {
            names[p.player_id] = p.name;
        });
        var list = $("party-speakers");
        list.textContent = "";
        status.party_speakers.forEach(function (p) {
            var count = p.member_ids.length;
            var item = el("li");
            item.appendChild(el("strong", "party-name", p.name));
            item.appendChild(el("span", "note", "Main: " + (names[p.main_player_id] || p.main_player_id) +
                " + " + count + (count === 1 ? " speaker" : " speakers") +
                ", grouped by " + GROUPINGS[p.grouping]));
            var shown = "";
            if (!names[p.main_player_id]) {
                shown = "Not in the Qobuz app: Music Assistant no longer has its main speaker.";
            } else if (!p.speaker_name) {
                shown = "Not in the Qobuz app";
            } else if (p.speaker_name !== p.name) {
                shown = "Shows as “" + p.speaker_name + "”";
            }
            if (shown) item.appendChild(el("span", "note", shown));
            if (p.problem) item.appendChild(el("span", "warning", p.problem));
            var buttons = el("div", "party-buttons");
            buttons.appendChild(linkButton("Edit", function () {
                openPartyForm(p);
            }));
            buttons.appendChild(linkButton("Delete", function () {
                onDeleteParty(p);
            }));
            item.appendChild(buttons);
            list.appendChild(item);
        });
        show(list, true);
        show($("party-add"), status.players.length > 0);
        $("party-empty").textContent = status.players.length ? "" :
            "Connect to Music Assistant to add a party speaker.";
        show($("party-empty"), !status.players.length);
    }

    function linkButton(text, onClick) {
        var button = el("button", "link-button", text);
        button.type = "button";
        button.addEventListener("click", onClick);
        return button;
    }

    function option(value, text) {
        var node = el("option", "", text);
        node.value = value;
        return node;
    }

    function choice(name) {
        var checked = $("party-form").querySelector('input[name="' + name + '"]:checked');
        return checked ? checked.value : "";
    }

    function setChoice(name, value) {
        $("party-form").querySelectorAll('input[name="' + name + '"]').forEach(function (input) {
            input.checked = input.value === value;
        });
    }

    // p: the party speaker to edit, or null to add one
    function openPartyForm(p) {
        party = {
            id: p ? p.party_id : "",
            members: { music_assistant: [], home_assistant: [] }, // ticked, kept per grouping
            mainEntityId: p ? p.main_entity_id : "",
            homeAssistantPlayers: null, // fetched when Home Assistant is picked
            homeAssistantError: "",
        };
        if (p) party.members[p.grouping] = p.member_ids.slice();
        $("party-form").reset(); // the choices the page starts with are the defaults
        $("party-form-title").textContent = p ? "Edit “" + p.name + "”" : "Add a party speaker";
        $("party-name").value = p ? p.name : "";
        var main = $("party-main");
        main.textContent = "";
        main.appendChild(option("", "Pick a speaker"));
        last.players.forEach(function (player) {
            main.appendChild(option(player.player_id, player.name));
        });
        if (p && !last.players.some(function (player) { return player.player_id === p.main_player_id; })) {
            main.appendChild(option(p.main_player_id, p.main_player_id)); // gone from Music Assistant
        }
        main.value = p ? p.main_player_id : "";
        $("party-main-entity").textContent = ""; // filled once Home Assistant's players are in
        $("party-grouping-home-assistant").disabled = !last.home_assistant_available;
        show($("party-home-assistant-note"), !last.home_assistant_available);
        if (p) {
            setChoice("party-grouping", p.grouping);
            setChoice("party-busy", p.busy_speakers);
            setChoice("party-after", p.after_party);
            setChoice("party-leave", p.on_leave);
        }
        show($("party-error"), false);
        show($("party-form-error"), false);
        show($("party-edit-note"), !!p);
        show($("party-speakers"), false);
        show($("party-add"), false);
        show($("party-form"), true);
        onGroupingPicked();
        $("party-name").focus();
    }

    function closePartyForm() {
        party = null;
        show($("party-form"), false);
        drawParties(last);
    }

    function onGroupingPicked() {
        if (choice("party-grouping") === "home_assistant" && !party.homeAssistantPlayers) {
            loadHomeAssistantPlayers();
        }
        drawPartyMembers();
    }

    function loadHomeAssistantPlayers() {
        var form = party;
        form.homeAssistantError = "";
        request("GET", "api/music-assistant/home-assistant-players").then(function (data) {
            form.homeAssistantPlayers = data.players.filter(function (p) {
                return p.can_group;
            });
        }, function (error) {
            form.homeAssistantError = error.message;
        }).then(function () {
            if (party !== form) return;
            fillMainEntity();
            drawPartyMembers();
        });
    }

    // The main speaker as Home Assistant knows it; guessed from the main speaker's name
    function fillMainEntity() {
        var players = party.homeAssistantPlayers || [];
        var select = $("party-main-entity");
        select.textContent = "";
        select.appendChild(option("", "Pick a speaker"));
        players.forEach(function (p) {
            select.appendChild(option(p.entity_id, p.name + " (" + p.entity_id + ")"));
        });
        var id = party.mainEntityId;
        if (!id) {
            var main = $("party-main").selectedOptions[0];
            var name = main && main.value ? main.textContent.toLowerCase() : "";
            var same = players.filter(function (p) {
                return p.name.toLowerCase() === name;
            });
            if (same.length === 1) id = party.mainEntityId = same[0].entity_id;
        }
        if (id && !players.some(function (p) { return p.entity_id === id; })) {
            select.appendChild(option(id, id));
        }
        select.value = id;
    }

    function drawPartyMembers() {
        var homeAssistant = choice("party-grouping") === "home_assistant";
        show($("party-main-entity-field"), homeAssistant);
        var list = $("party-members");
        list.textContent = "";
        var note = homeAssistant ? drawHomeAssistantMembers(list) : drawMusicAssistantMembers(list);
        $("party-members-note").textContent = note;
        show($("party-members-note"), !!note);
    }

    function memberChoice(id, name, detail, ticked, disabled) {
        var box = el("input");
        box.type = "checkbox";
        box.value = id;
        box.disabled = disabled;
        box.checked = ticked && !disabled;
        var text = el("span");
        text.appendChild(el("span", "", name));
        if (detail) text.appendChild(el("span", "note", detail));
        var label = el("label", "choice");
        label.appendChild(box);
        label.appendChild(text);
        var item = el("li");
        item.appendChild(label);
        return item;
    }

    // Returns a note for under the list, or ""
    function drawMusicAssistantMembers(list) {
        var mainId = $("party-main").value;
        var main = last.players.filter(function (p) {
            return p.player_id === mainId;
        })[0];
        var syncsWith = main ? main.can_group_with : []; // empty: Music Assistant does not say
        var ticked = party.members.music_assistant;
        last.players.forEach(function (p) {
            if (p.player_id === mainId) return;
            var cannot = syncsWith.length > 0 && syncsWith.indexOf(p.player_id) < 0;
            list.appendChild(memberChoice(p.player_id, p.name, cannot ? "can't sync with " + main.name : "",
                ticked.indexOf(p.player_id) >= 0, cannot));
        });
        return list.children.length ? "" : "Music Assistant has no other players.";
    }

    function drawHomeAssistantMembers(list) {
        var players = party.homeAssistantPlayers;
        if (!players) return party.homeAssistantError || "Loading the speakers in Home Assistant…";
        var mainId = $("party-main-entity").value;
        var ticked = party.members.home_assistant;
        players.forEach(function (p) {
            if (p.entity_id === mainId) return;
            list.appendChild(memberChoice(p.entity_id, p.name, p.entity_id, ticked.indexOf(p.entity_id) >= 0, false));
        });
        // Ones it had before that Home Assistant can't group now: shown, so they can be taken off
        ticked.forEach(function (id) {
            if (id === mainId || players.some(function (p) { return p.entity_id === id; })) return;
            list.appendChild(memberChoice(id, id, "Home Assistant can't group it now", true, false));
        });
        return list.children.length ? "" : "Home Assistant has no other speakers that can group.";
    }

    function onMemberTicked(event) {
        var box = event.target;
        var ticked = party.members[choice("party-grouping")];
        var at = ticked.indexOf(box.value);
        if (box.checked && at < 0) ticked.push(box.value);
        if (!box.checked && at >= 0) ticked.splice(at, 1);
    }

    function onMainEntityPicked() {
        party.mainEntityId = $("party-main-entity").value;
        drawPartyMembers();
    }

    function onSaveParty(event) {
        event.preventDefault();
        var form = party;
        var grouping = choice("party-grouping");
        var body = {
            name: $("party-name").value.trim(),
            main_player_id: $("party-main").value,
            grouping: grouping,
            member_ids: Array.prototype.map.call($("party-members").querySelectorAll("input:checked"),
                function (box) {
                    return box.value;
                }),
            busy_speakers: choice("party-busy"),
            after_party: choice("party-after"),
            on_leave: choice("party-leave"),
        };
        if (grouping === "home_assistant") body.main_entity_id = $("party-main-entity").value;
        var sent = form.id
            ? send("PUT", "api/music-assistant/party-speakers/" + encodeURIComponent(form.id), body,
                $("party-form-error"))
            : send("POST", "api/music-assistant/party-speakers", body, $("party-form-error"));
        $("party-save").disabled = true;
        sent.then(function () {
            if (party === form) closePartyForm();
        }, function () {}).then(function () {
            $("party-save").disabled = false;
        });
    }

    function onDeleteParty(p) {
        if (window.confirm("Delete the party speaker “" + p.name + "”? It disappears from the Qobuz app " +
                "at once.")) {
            send("DELETE", "api/music-assistant/party-speakers/" + encodeURIComponent(p.party_id), undefined,
                $("party-error")).catch(function () {});
        }
    }

    function render(status) {
        last = status;
        renderChecks(status);
        renderQobuz(status);
        renderMusicAssistant(status);
        renderPlayers(status);
        renderParties(status);
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
    $("party-add").addEventListener("click", function () {
        openPartyForm(null);
    });
    $("party-form").addEventListener("submit", onSaveParty);
    $("party-cancel").addEventListener("click", closePartyForm);
    $("party-main").addEventListener("change", drawPartyMembers);
    $("party-form").querySelectorAll('input[name="party-grouping"]').forEach(function (input) {
        input.addEventListener("change", onGroupingPicked);
    });
    $("party-main-entity").addEventListener("change", onMainEntityPicked);
    $("party-members").addEventListener("change", onMemberTicked);
    // Coming back from the Qobuz login tab: refresh right away
    window.addEventListener("focus", poll);
    document.addEventListener("visibilitychange", poll);
    poll();
})();
