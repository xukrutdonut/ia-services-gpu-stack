if (window.innerWidth > 768) document.body.classList.remove("sb-closed");
else document.body.classList.add("sb-closed");

            // =================================================================
            // Type definitions (JSDoc).  ``// @ts-check`` is intentionally
            // NOT enabled at the top of the file — that would surface
            // thousands of pre-existing typing issues across 6800 lines of
            // legacy vanilla JS.  Instead, key primitives below carry
            // ``@param``/``@returns`` annotations so VS Code's IntelliSense
            // catches typos at the load-bearing surface (setState shape,
            // subscribe keys, effectiveReasoning return).  New code can
            // opt in by adding ``// @ts-check`` to the function scope.
            //
            // @typedef {Object} AuthState
            //   @property {boolean} enabled
            //   @property {?{id:string, username:string, display_name?:string, is_admin?:number}} user
            //   @property {boolean} needs_setup
            //
            // @typedef {Object} ViewState
            //   @property {"welcome"|"chat"|"settings"|"pins"} route
            //   @property {null|"settings"|"pins"} rightPanel
            //   @property {boolean} settingsOpen
            //   @property {string}  settingsTab
            //
            // @typedef {Object} ServerInfo
            //   @property {boolean} hasApiKey
            //   @property {string}  lmUrl
            //   @property {"off"|"on"|"low"|"medium"|"high"} defaultReasoning
            //
            // @typedef {Object} ChatSettings
            //   Per-chat overrides.  Empty object = "use server defaults".
            //   @property {?string}  reasoning      — "off"|"on"|"low"|"medium"|"high" or absent
            //   @property {?number}  temperature
            //   @property {?number}  top_p
            //   @property {?number}  top_k
            //   @property {?number}  min_p
            //   @property {?number}  repeat_penalty
            //   @property {?number}  max_output_tokens
            //   @property {?boolean} sc_enabled
            //   @property {?boolean} cove_enabled
            //   @property {?false}   store          — false = stateless turns; absent = use chain
            //   @property {?string}  system_prompt
            //
            // @typedef {Object} BootState
            //   @property {"init"|"auth"|"profile"|"conversations"|"ready"} phase
            //   @property {?{phase:string, message:string}} error
            //
            // @typedef {Object} Model
            //   @property {string} id
            //   @property {string} key
            //   @property {string} display_name
            //   @property {{vision:boolean, trained_for_tool_use:boolean,
            //              reasoning?:{allowed_options:string[], default:string},
            //              unsupported_params?:string[]}} capabilities
            //   @property {number} context_length
            //   @property {?{config:Object, id:string}[]} loaded_instances
            //
            // @typedef {Object} Chat
            //   @property {string} id
            //   @property {string} title
            //   @property {string} model
            //   @property {?string} response_id
            //   @property {number} updated_at
            //   @property {number} pinned
            //   @property {string} folder
            //
            // @typedef {Object} State
            //   @property {AuthState}      auth
            //   @property {ViewState}      view
            //   @property {Model[]}        models
            //   @property {Chat[]}         chats
            //   @property {?string}        activeChatId
            //   @property {ChatSettings}   chatSettings
            //   @property {ServerInfo}     serverInfo
            //   @property {Array<Object>}  mcps
            //   @property {Array<Object>}  remoteMcps
            //   @property {{sending:boolean, abortCtrl:?AbortController}} stream
            //   @property {Object<string,boolean>} busy
            //   @property {BootState}      boot
            // =================================================================

            // =================================================================
            // State machine — single source of truth.
            //
            // Every UI mutation must go through ``setState(partial)``.  Renders
            // subscribe to specific top-level keys via ``subscribe(keys, fn)``
            // and rerun on the next animation frame when any subscribed key
            // changes.  Multiple ``setState`` calls within the same tick are
            // batched into one render pass.
            //
            // ``state`` is the live object — read directly from any callsite.
            // Mutating it without ``setState`` works but skips the change
            // dispatch (renders won't update).  Treat direct mutation as a bug.
            //
            // Legacy mirror: a number of older ``let`` globals (chatSettingsCache,
            // cachedModels, MCPS, etc.) still exist and are kept in sync with
            // ``state`` via the setState reducer below.  They are removed
            // phase-by-phase as their owning surfaces migrate to subscribe().
            // =================================================================
            const state = {
                auth:         { enabled: false, user: null, needs_setup: false },
                view:         { route: "welcome", rightPanel: null, settingsOpen: false, settingsTab: "chat" },
                models:       [],
                chats:        [],
                activeChatId: null,
                chatSettings: {},          // per-chat overrides for the active chat
                serverInfo:   { hasApiKey: false, lmUrl: "", defaultReasoning: "off" },
                mcps:         [],
                remoteMcps:   [],
                stream:       { sending: false, abortCtrl: null },
                busy:         {},          // {fetchId: bool} — drives per-region spinners
                // Boot lifecycle.  ``phase`` is one of:
                //   "init"          — initial value, before checkAuth runs
                //   "auth"          — checkAuth in flight
                //   "profile"       — loading models + settings + MCPs in parallel
                //   "conversations" — loading chat list + restoring active chat
                //   "ready"         — boot complete; SPA fully interactive
                // ``error`` is null unless a phase failed irrecoverably.
                // Subscribed by the boot-status indicator + retry banner.
                boot:         { phase: "init", error: null },
            };

            // Nested objects merged shallowly so ``setState({chatSettings:
            // {reasoning: "high"}})`` updates one field without clobbering the
            // rest of chatSettings.  Other types replace.
            const _MERGEABLE_KEYS = new Set([
                "auth", "view", "chatSettings", "serverInfo", "stream", "busy", "boot",
            ]);
            const _changedKeys = new Set();
            let   _renderScheduled = false;
            const _subscriptions  = [];

            /**
             * Merge ``partial`` into ``state``.  Keys listed in
             * ``_MERGEABLE_KEYS`` shallow-merge into their existing object;
             * other keys are replaced (arrays, primitives).  Schedules a
             * batched RAF that invokes subscribers whose key set intersects
             * the changed keys.
             * @param {Partial<State>} partial
             */
            function setState(partial) {
                if (!partial || typeof partial !== "object") return;
                for (const k in partial) {
                    const next = partial[k];
                    if (_MERGEABLE_KEYS.has(k) && next && typeof next === "object" && !Array.isArray(next)) {
                        Object.assign(state[k], next);
                    } else {
                        state[k] = next;
                    }
                    _changedKeys.add(k);
                }
                if (_renderScheduled) return;
                _renderScheduled = true;
                requestAnimationFrame(() => {
                    _renderScheduled = false;
                    const keys = [..._changedKeys];
                    _changedKeys.clear();
                    for (const sub of _subscriptions) {
                        if (sub._disabled) continue;
                        if (keys.some((k) => sub.keys.has(k))) {
                            try {
                                sub.fn(state);
                                sub._consecutiveErrors = 0;
                            } catch (err) {
                                sub._consecutiveErrors = (sub._consecutiveErrors || 0) + 1;
                                console.error(
                                    `subscribe render error (count=${sub._consecutiveErrors}):`,
                                    err,
                                );
                                // Cut off a consistently-failing subscriber so
                                // it can't burn frames indefinitely.  Manual
                                // re-subscribe required after fixing.
                                if (sub._consecutiveErrors >= 5) {
                                    sub._disabled = true;
                                    console.error(
                                        "subscriber disabled after 5 consecutive errors:",
                                        sub.fn.name || "(anonymous)",
                                    );
                                }
                            }
                        }
                    }
                });
            }

            /**
             * Register ``fn`` to run on next animation frame when any of
             * ``keys`` changes via setState.  Returns an unsubscribe fn.
             * @param {string[]} keys  Top-level state keys to watch.
             * @param {(state: State) => void} fn
             * @returns {() => void} Unsubscribe.
             */
            function subscribe(keys, fn) {
                const sub = { keys: new Set(keys), fn };
                _subscriptions.push(sub);
                return () => {
                    const i = _subscriptions.indexOf(sub);
                    if (i >= 0) _subscriptions.splice(i, 1);
                };
            }

            // Single read function for reasoning effort — chat override wins,
            // else user/server default, else "off".  All three reasoning UI
            // surfaces (cycle button, chat-settings dropdown, global-settings
            // dropdown) read this — guarantees they always agree.
            //
            // Treat empty string as "no override" — the chat-settings
            // dropdown's "Global" option is encoded as ``value=""`` and the
            // saveChatSetting binding stores null OR "" depending on path;
            // ``?? ""`` would let an empty string pass as a real value.
            /**
             * Resolve the reasoning effort for the next chat send.  Single
             * read path so the cycle button, chat-settings dropdown, and
             * global-settings dropdown can never disagree with what
             * actually goes on the wire.
             * @returns {"off"|"on"|"low"|"medium"|"high"}
             */
            function effectiveReasoning() {
                const chat = state.chatSettings.reasoning;
                if (chat) return chat;  // non-empty string only
                const def = state.serverInfo.defaultReasoning;
                if (def)  return def;
                return "off";
            }

            // --- Auth state ---
            // Legacy mirror — kept in sync with state.auth via setState below.
            // New code reads state.auth.* directly.
            let AUTH_STATE = state.auth;

            async function checkAuth() {
                try {
                    const r = await fetch("/api/auth/me");
                    const d = await r.json();
                    AUTH_STATE.enabled = !!d.auth_enabled;
                    AUTH_STATE.user = d.user || null;
                    AUTH_STATE.needs_setup = !!d.needs_setup;
                } catch (e) {
                    AUTH_STATE.enabled = false;
                    AUTH_STATE.user = null;
                }
                // Broadcast — the direct mutations above wouldn't fire any
                // "auth" subscribers.  setState refreshes them.  Object
                // spread copies into the same merged target (no identity
                // change to state.auth, so AUTH_STATE alias is preserved).
                setState({ auth: { ...state.auth } });
                if (!AUTH_STATE.enabled) {
                    // Auth disabled — hide auth screen, proceed
                    document
                        .getElementById("auth-screen")
                        .classList.remove("open");
                    return true;
                }
                if (AUTH_STATE.needs_setup) {
                    showAuthScreen(true);
                    return false;
                }
                if (!AUTH_STATE.user) {
                    showAuthScreen(false);
                    return false;
                }
                // Logged in
                document.getElementById("auth-screen").classList.remove("open");
                showUserAvatar();
                return true;
            }

            function showAuthScreen(isSetup) {
                const s = document.getElementById("auth-screen");
                s.classList.add("open");
                document.getElementById("auth-title").textContent =
                    isSetup ? "Create Admin Account" : "Sign In";
                document.getElementById("auth-sub").textContent =
                    isSetup ?
                        "Set up your admin account to get started"
                    :   "Sign in to continue";
                const nameWrap = document.getElementById("a-name-wrap");
                if (isSetup) nameWrap.classList.remove("hidden");
                else nameWrap.classList.add("hidden");
                const pass2Wrap = document.getElementById("a-pass2-wrap");
                if (isSetup) pass2Wrap.classList.remove("hidden");
                else pass2Wrap.classList.add("hidden");
                document.getElementById("auth-btn").textContent =
                    isSetup ? "Create Account" : "Sign In";
                document.getElementById("auth-err").textContent = "";
                // Reset auth form to initial state (clear any TOTP step)
                document.getElementById("a-totp-wrap").classList.add("hidden");
                document.getElementById("a-user").parentElement.style.display =
                    "";
                document.getElementById("a-pass").parentElement.style.display =
                    "";
                const btn = document.getElementById("auth-btn");
                btn.onclick = function () {
                    doAuth();
                };
                btn.disabled = false;
                document.getElementById("a-user").value = "";
                document.getElementById("a-pass").value = "";
                AUTH_STATE.needs_setup = isSetup;
            }

            async function doAuth() {
                const btn = document.getElementById("auth-btn");
                const errEl = document.getElementById("auth-err");
                const showErr = (msg) => { errEl.textContent = msg; };
                const clearErr = () => { errEl.textContent = ""; };
                clearErr();
                const username = document.getElementById("a-user").value.trim();
                const password = document.getElementById("a-pass").value;

                if (!username || !password) {
                    showErr("Please fill in all fields");
                    return;
                }

                if (AUTH_STATE.needs_setup) {
                    const pass2 = document.getElementById("a-pass2").value;
                    if (password !== pass2) {
                        showErr("Passwords do not match");
                        return;
                    }
                    const display_name =
                        document.getElementById("a-name").value.trim() ||
                        username;
                    btn.disabled = true;
                    btn.textContent = "Creating...";
                    try {
                        const r = await fetch("/api/auth/setup", {
                            method: "POST",
                            headers: {
                                "Content-Type": "application/json",
                                "X-Requested-With": "lm-chat",
                            },
                            body: JSON.stringify({
                                username,
                                password,
                                display_name,
                            }),
                        });
                        const d = await r.json();
                        if (!r.ok) {
                            showErr(d.error || "Setup failed");
                            btn.disabled = false;
                            btn.textContent = "Create Account";
                            return;
                        }
                        AUTH_STATE.user = d.user;
                        AUTH_STATE.needs_setup = false;
                        document
                            .getElementById("auth-screen")
                            .classList.remove("open");
                        showUserAvatar();
                        initApp();
                    } catch (e) {
                        showErr("Connection failed");
                    } finally {
                        btn.disabled = false;
                        btn.textContent = "Create Account";
                    }
                } else {
                    btn.disabled = true;
                    btn.textContent = "Signing in...";
                    try {
                        const r = await fetch("/api/auth/login", {
                            method: "POST",
                            headers: {
                                "Content-Type": "application/json",
                                "X-Requested-With": "lm-chat",
                            },
                            body: JSON.stringify({ username, password }),
                        });
                        if (typeof _crumb === "function") {
                            _crumb("auth.login.response", {
                                status: r.status,
                                ok: r.ok,
                                // ``Set-Cookie`` is filtered out of fetch().headers for
                                // security but a "set-cookie" presence check still
                                // returns the joined value in some browsers — useful
                                // if it ever shows up.
                                setCookieHint: r.headers.get("Set-Cookie") || null,
                                visibleCookieNames: (document.cookie || "")
                                    .split(";").map(s => s.split("=")[0].trim()).filter(Boolean),
                            });
                        }
                        const d = await r.json();
                        if (!r.ok) {
                            showErr(d.error || "Login failed");
                            btn.disabled = false;
                            btn.textContent = "Sign In";
                            return;
                        }
                        if (d.needs_totp) {
                            // Show TOTP input, hide username/password
                            document.getElementById(
                                "a-totp-wrap",
                            ).classList.remove("hidden");
                            document.getElementById(
                                "a-user",
                            ).parentElement.style.display = "none";
                            document.getElementById(
                                "a-pass",
                            ).parentElement.style.display = "none";
                            document.getElementById("auth-title").textContent =
                                "Enter 2FA Code";
                            document.getElementById("auth-sub").textContent =
                                "Enter the code from your authenticator app";
                            btn.textContent = "Verify";
                            btn.onclick = async function (e) {
                                e.preventDefault();
                                const code = document
                                    .getElementById("a-totp")
                                    .value.trim();
                                clearErr();
                                if (code.length !== 6) {
                                    showErr("Enter a 6-digit code");
                                    return;
                                }
                                btn.disabled = true;
                                btn.textContent = "Verifying...";
                                try {
                                    const r2 = await fetch(
                                        "/api/auth/totp/login",
                                        {
                                            method: "POST",
                                            headers: {
                                                "Content-Type":
                                                    "application/json",
                                                "X-Requested-With": "lm-chat",
                                            },
                                            body: JSON.stringify({
                                                partial_token: d.partial_token,
                                                code,
                                            }),
                                        },
                                    );
                                    const d2 = await r2.json();
                                    if (!r2.ok) {
                                        showErr(d2.error || "Invalid code");
                                        btn.disabled = false;
                                        btn.textContent = "Verify";
                                        return;
                                    }
                                    AUTH_STATE.user = d2.user;
                                    document
                                        .getElementById("auth-screen")
                                        .classList.remove("open");
                                    showUserAvatar();
                                    await initApp();
                                } catch (e2) {
                                    showErr("Connection failed");
                                } finally {
                                    btn.disabled = false;
                                    btn.textContent = "Verify";
                                }
                            };
                            document.getElementById("a-totp").focus();
                            return;
                        }
                        AUTH_STATE.user = d.user;
                        document
                            .getElementById("auth-screen")
                            .classList.remove("open");
                        showUserAvatar();
                        initApp();
                    } catch (e) {
                        if (e.message !== "unauthorized") {
                            showErr("Connection failed");
                        }
                        btn.disabled = false;
                        btn.textContent = "Sign In";
                    }
                }
            }

            async function doLogout() {
                document.getElementById("user-dd").classList.remove("open");
                await apiFetch("/api/auth/logout", { method: "POST" });
                AUTH_STATE.user = null;
                setState({ auth: { user: null } });
                // Stop the connection-poll interval — re-login boots fresh.
                // Without this, every logout+login cycle compounds another
                // 30-second timer running checkConnection forever.
                if (_connInterval) {
                    clearInterval(_connInterval);
                    _connInterval = null;
                }
                document.getElementById("user-avatar").classList.add("hidden");
                const gear = document.getElementById("global-settings-btn");
                if (gear) gear.classList.remove("hidden");
                showAuthScreen(false);
            }

            function showUserAvatar() {
                if (!AUTH_STATE.enabled || !AUTH_STATE.user) return;
                const av = document.getElementById("user-avatar");
                const name =
                    AUTH_STATE.user.display_name ||
                    AUTH_STATE.user.username ||
                    "?";
                // Save dropdown before wiping
                const dd = document.getElementById("user-dd");
                av.textContent = name.charAt(0).toUpperCase();
                if (dd) av.appendChild(dd);
                av.classList.remove("hidden");
                document.getElementById("user-dd-name").textContent = name;
                // Hide redundant gear — user menu has Settings
                const gear = document.getElementById("global-settings-btn");
                if (gear) gear.classList.add("hidden");
            }

            function toggleUserDD() {
                const dd = document.getElementById("user-dd");
                const av = document.getElementById("user-avatar");
                const isOpen = dd.classList.toggle("open");
                av.setAttribute("aria-expanded", isOpen ? "true" : "false");
            }
            // =================================================================
            // Outside-click primitive.
            //
            // A naive ``click`` handler with ``container.contains(e.target)``
            // false-triggers on events whose target was outside but whose
            // press started inside (or vice versa) — text-selection drags
            // ending outside, devtools focus-change events, programmatic
            // clicks from extensions.  The user-visible bug: settings
            // panels won't open with devtools open, because the focus-change
            // fires a click whose target lives in the Chrome devtools DOM.
            //
            // Fix: only treat as "outside click" when BOTH mousedown AND
            // mouseup landed outside the container.  Press-inside + drag-out
            // (selecting text) no longer closes.
            //
            // ``onOutside(container, handler)`` returns an unregister fn.
            // ``container`` may be a function returning the live element
            // (handles late-mounted nodes).  Handler runs once per
            // outside-press-release pair.
            // =================================================================
            /**
             * Fire ``handler`` when both mousedown AND mouseup land outside
             * ``container``.  Eliminates spurious closes from press-inside
             * + drag-out events (text selection) and focus-change clicks
             * with mismatched targets (devtools open).
             * @param {Element|(()=>Element|null)} container
             * @param {(e: MouseEvent) => void} handler
             * @returns {() => void} Teardown.
             */
            function onOutside(container, handler) {
                let downOutside = false;
                const resolve = () => typeof container === "function" ? container() : container;
                const onDown = (e) => {
                    const c = resolve();
                    downOutside = !!(c && !c.contains(e.target));
                };
                const onUp = (e) => {
                    if (!downOutside) return;
                    downOutside = false;
                    const c = resolve();
                    if (c && !c.contains(e.target)) handler(e);
                };
                document.addEventListener("mousedown", onDown, true);
                document.addEventListener("mouseup",   onUp,   true);
                return () => {
                    document.removeEventListener("mousedown", onDown, true);
                    document.removeEventListener("mouseup",   onUp,   true);
                };
            }

            // =================================================================
            // Toast / notification system.
            //
            // Replaces ~25 silent console.error sites with a user-visible
            // surface.  Stacks top-right at --z-toast (10000) — above the
            // auth overlay so a network error during login shows.
            //
            // toast.success(msg) | toast.info(msg) | toast.warn(msg) |
            // toast.error(msg, {detail, persistent}) — error is sticky by
            // default (manual dismiss); others auto-fade after 6s.
            //
            // Network monitoring: navigator.onLine + fetch-error pattern.
            // A single offline toast pins until back online.
            //
            // ARIA: ``role="status"`` for non-errors (polite), ``role="alert"``
            // for errors (assertive).  Toasts are aria-live regions.
            // =================================================================
            const TOAST_CONTAINER_ID = "toast-container";
            const TOAST_DEFAULT_TIMEOUT = 6000;
            let _offlineToast = null;

            function _toastContainer() {
                let c = document.getElementById(TOAST_CONTAINER_ID);
                if (!c) {
                    c = document.createElement("div");
                    c.id = TOAST_CONTAINER_ID;
                    document.body.appendChild(c);
                }
                return c;
            }

            function _showToast(variant, msg, { detail, persistent } = {}) {
                if (!msg) return null;
                const t = document.createElement("div");
                t.className = `toast toast-${variant}`;
                t.setAttribute("role", variant === "error" ? "alert" : "status");
                t.setAttribute("aria-live", variant === "error" ? "assertive" : "polite");
                const text = document.createElement("div");
                text.className = "toast-text";
                text.textContent = String(msg);
                t.appendChild(text);
                if (detail) {
                    const det = document.createElement("div");
                    det.className = "toast-detail";
                    det.textContent = typeof detail === "string" ? detail : (detail.message || String(detail));
                    t.appendChild(det);
                }
                const close = document.createElement("button");
                close.className = "toast-close";
                close.setAttribute("aria-label", "Dismiss");
                close.textContent = "×";
                close.addEventListener("click", () => dismiss());
                t.appendChild(close);
                let timer = null;
                function dismiss() {
                    if (timer) clearTimeout(timer);
                    t.classList.add("toast-leaving");
                    setTimeout(() => t.remove(), 200);
                }
                _toastContainer().appendChild(t);
                const stickByDefault = variant === "error" || persistent;
                if (!stickByDefault) {
                    timer = setTimeout(dismiss, TOAST_DEFAULT_TIMEOUT);
                }
                return { dismiss, el: t };
            }

            const toast = {
                success: (m, o) => _showToast("success", m, o),
                info:    (m, o) => _showToast("info",    m, o),
                warn:    (m, o) => _showToast("warn",    m, o),
                error:   (m, o) => _showToast("error",   m, o),
            };

            // Boot-error subscriber upgrade — was console.warn in Phase 3,
            // now a real toast so users see what failed.
            subscribe(["boot"], () => {
                if (state.boot.error) {
                    toast.error(
                        `Startup error in ${state.boot.error.phase} phase`,
                        { detail: state.boot.error.message },
                    );
                    // Clear so a subsequent setState doesn't re-toast the
                    // same error.  Keep state.boot.phase as-is so any
                    // ready gate still trips.
                    state.boot.error = null;
                }
            });

            // =================================================================
            // Forensic breadcrumb logger.
            //
            // Operator-reported bug: hitting browser back then forward
            // sometimes deletes a regular persistent chat from the
            // sidebar.  Static code reading found no path that explains
            // it, so this captures every event that COULD plausibly be
            // involved — popstate transitions, applyRoute outcomes,
            // chatMeta mutations, loadChat / exitIncognito / deleteChat
            // entries.  Last 200 entries persist in localStorage so they
            // survive the reload that follows the bug.  Operator runs
            // ``window.dumpBreadcrumbs()`` in the console to dump.
            //
            // Added 0.5.10.  Remove once root cause is identified.
            const _CRUMB_KEY = "lsc-chat-breadcrumbs";
            const _CRUMB_CAP = 200;
            function _crumb(kind, detail) {
                try {
                    const arr = JSON.parse(localStorage.getItem(_CRUMB_KEY) || "[]");
                    arr.push({ t: Date.now(), kind, ...(detail || {}) });
                    while (arr.length > _CRUMB_CAP) arr.shift();
                    localStorage.setItem(_CRUMB_KEY, JSON.stringify(arr));
                } catch (e) {
                    // localStorage full / disabled / quota — drop silently.
                }
            }
            window.dumpBreadcrumbs = function dumpBreadcrumbs() {
                try {
                    const arr = JSON.parse(localStorage.getItem(_CRUMB_KEY) || "[]");
                    console.table(arr.map(e => ({
                        // Use a short ISO suffix for human-readable time
                        ts: new Date(e.t).toISOString().slice(11, 23),
                        kind: e.kind,
                        ...Object.fromEntries(Object.entries(e).filter(([k]) => k !== "t" && k !== "kind")),
                    })));
                    return arr;
                } catch (e) { return []; }
            };
            window.clearBreadcrumbs = function clearBreadcrumbs() {
                localStorage.removeItem(_CRUMB_KEY);
            };

            // Network online/offline — show a persistent offline toast
            // while disconnected, dismiss when back.
            window.addEventListener("offline", () => {
                if (_offlineToast) return;
                _offlineToast = toast.warn("You're offline.  Reconnect when ready.", { persistent: true });
            });
            window.addEventListener("online", () => {
                if (_offlineToast) { _offlineToast.dismiss(); _offlineToast = null; }
                toast.success("Back online.");
            });

            // =================================================================
            // URL router.
            //
            // Routes:
            //   /                  → welcome screen
            //   /chat/:id          → open chat ``id``
            //   /settings/:tab?    → system settings, optional tab
            //   /pins              → right-panel pins
            //
            // The SPA is single-page; routes are reflected in the URL via
            // history.pushState so the browser back/forward buttons work
            // and links to specific chats/settings can be shared.  Routes
            // are derived from + write into state.view; subscribers can
            // ignore the route detail and read state.view.* as before.
            //
            // Conventions:
            //   - ``navigate(path, {replace})``: programmatic.  Default
            //     pushes; pass replace:true for state-mirror changes that
            //     shouldn't grow the back stack.
            //   - ``popstate`` listener re-derives view from current URL.
            //   - On initial load (boot) the route is parsed once and
            //     dispatched as the SPA's first view-set action.
            // =================================================================
            const ROUTES = {
                parse(pathname) {
                    const path = (pathname || "/").replace(/\/+$/, "") || "/";
                    if (path === "/")             return { name: "welcome" };
                    let m;
                    if ((m = path.match(/^\/chat\/([^\/]+)$/)))     return { name: "chat",     chatId: m[1] };
                    if ((m = path.match(/^\/settings(?:\/([^\/]+))?$/))) return { name: "settings", tab: m[1] || "chat" };
                    if (path === "/pins")         return { name: "pins" };
                    return { name: "welcome" };  // unknown path → welcome
                },
                build(route) {
                    if (!route || route.name === "welcome") return "/";
                    if (route.name === "chat")     return `/chat/${route.chatId}`;
                    if (route.name === "settings") return route.tab && route.tab !== "chat" ? `/settings/${route.tab}` : "/settings";
                    if (route.name === "pins")     return "/pins";
                    return "/";
                },
            };

            // Apply a parsed route to SPA state by invoking the existing
            // navigation primitives (loadChat, openSettings, openRightPanel).
            // Each one already mutates the right state slices + DOM; the
            // router just dispatches.  Idempotent: if state already matches,
            // it's a cheap no-op.
            let _applyingRoute = false;
            function applyRoute(route) {
                _applyingRoute = true;
                _crumb("applyRoute.enter", { routeName: route.name, chatId: route.chatId, activeId });
                try {
                    if (route.name === "chat" && route.chatId) {
                        if (activeId !== route.chatId) {
                            if (chatMeta[route.chatId]) {
                                _crumb("applyRoute.chat.hit", { chatId: route.chatId });
                                loadChat(route.chatId);
                            } else {
                                // Chat not in memory.  Two cases:
                                //   1. Pre-boot: chats haven't loaded yet —
                                //      _bootPhaseConversations re-reads the
                                //      URL on completion and dispatches
                                //      then.  Silent retry.
                                //   2. Post-boot: chat is genuinely missing
                                //      (deleted, foreign user, typo).
                                //      Surface as a toast + welcome
                                //      redirect so the user isn't left
                                //      staring at the previous chat's
                                //      content with the wrong URL.
                                _crumb("applyRoute.chat.miss", {
                                    chatId: route.chatId,
                                    bootPhase: state.boot.phase,
                                    chatMetaCount: Object.keys(chatMeta).length,
                                });
                                if (state.boot.phase === "ready") {
                                    console.warn(`[router] chat ${route.chatId} not found`);
                                    if (typeof toast !== "undefined") {
                                        toast.warn(`Chat not found — it may have been deleted.`);
                                    }
                                    // Redirect to welcome.  replaceState
                                    // so the back button doesn't bring
                                    // us back to this dead route.
                                    _crumb("applyRoute.chat.redirect_welcome", { lostChatId: route.chatId });
                                    activeId = null;
                                    setState({ activeChatId: null });
                                    renderWelcome();
                                    history.replaceState({}, "", "/");
                                }
                            }
                        }
                    } else if (route.name === "settings") {
                        if (!settingsOpen) openSettings(route.tab);
                        else if (route.tab) {
                            switchSettingsTab(route.tab);
                        }
                    } else if (route.name === "pins") {
                        if (state.view.rightPanel !== "pins") openRightPanel("pins");
                    } else {
                        // welcome — close transient surfaces; do NOT
                        // unload the active chat (back-button to welcome
                        // is meaningful only when leaving a chat URL,
                        // which is rare in practice).
                        if (settingsOpen) closeSettings();
                        if (state.view.rightPanel) closeRightPanel();
                    }
                } finally {
                    _applyingRoute = false;
                }
            }

            function navigate(path, { replace = false } = {}) {
                if (_applyingRoute) return;  // ignore re-entry from applyRoute → state updates
                const cur = window.location.pathname;
                if (cur === path) return;
                if (replace) history.replaceState({}, "", path);
                else         history.pushState ({}, "", path);
                applyRoute(ROUTES.parse(path));
            }

            // Browser back/forward — re-derive view from URL.
            window.addEventListener("popstate", () => {
                _crumb("popstate", {
                    to: window.location.pathname,
                    activeId: activeId,
                    chatMetaCount: Object.keys(chatMeta).length,
                    activeIdInChatMeta: !!(activeId && chatMeta[activeId]),
                    incognito: incognitoMode,
                    sending: sending,
                });
                applyRoute(ROUTES.parse(window.location.pathname));
            });

            // State → URL: when activeChatId changes, sync the URL.
            // Uses ``replaceState`` rather than ``pushState`` so flipping
            // between chats doesn't fill the back stack with one entry
            // per click — chat switches collapse into a single "in this
            // app" history entry.  This is a deliberate tradeoff: users
            // who want to share a chat use the per-chat "Copy link"
            // affordance; the back button is reserved for crossing
            // surface boundaries (chat → settings → pins).
            subscribe(["activeChatId"], () => {
                if (_applyingRoute) return;
                const target = state.activeChatId ? `/chat/${state.activeChatId}` : "/";
                if (window.location.pathname !== target) {
                    history.replaceState({}, "", target);
                }
            });

            // =================================================================
            // Busy / loading-state primitive.
            //
            // ``withBusy(key, fn)`` flips ``state.busy[key] = true`` before
            // running ``fn`` (sync or async) and clears it after.  Renders
            // subscribed to ``"busy"`` can show per-region spinners by
            // reading state.busy[key].  Reentry safe — nested withBusy
            // calls with the same key count refs so the busy flag stays
            // on until all calls settle.
            //
            // Keys are arbitrary strings.  Convention: hyphenated region
            // name matching the rendered area (``sidebar-chats``,
            // ``settings-mcps``, ``model-list``).  No central registry —
            // callers and renderers must agree on the key.
            // =================================================================
            const _busyRefs = new Map();
            /**
             * Wrap ``fn`` in a busy-state flag.  Sets state.busy[key]=true
             * before, clears after.  Refcounted so nested calls keep the
             * flag on until all settle.
             * @template T
             * @param {string} key
             * @param {() => Promise<T>} fn
             * @returns {Promise<T>}
             */
            async function withBusy(key, fn) {
                const prev = _busyRefs.get(key) || 0;
                _busyRefs.set(key, prev + 1);
                if (prev === 0) setState({ busy: { [key]: true } });
                try {
                    return await fn();
                } finally {
                    const cur = (_busyRefs.get(key) || 1) - 1;
                    if (cur <= 0) {
                        _busyRefs.delete(key);
                        setState({ busy: { [key]: false } });
                    } else {
                        _busyRefs.set(key, cur);
                    }
                }
            }

            // =================================================================
            // Popover primitive.
            //
            // Wraps a "trigger + panel + close-on-outside + close-on-ESC"
            // pattern that the SPA repeats for the user dropdown, export
            // dropdown, model dropdowns, slash menu, and the share dialog.
            // Reuses ``onOutside`` so all popovers get the same press-and-
            // release semantics — no more spurious closes from focus
            // changes when devtools is open.
            //
            // ``openPopover({trigger, panel, onClose, returnFocus = true})``
            //   - trigger: the button/anchor; receives ``aria-expanded``.
            //   - panel: the popover content element; receives ``open`` class.
            //   - onClose: optional hook fired exactly once on close.
            //   - returnFocus: when true, focus returns to ``trigger`` on
            //     close (correct keyboard semantics).
            // Returns a ``close()`` function.  Calling it manually does the
            // same work as ESC or outside-press: removes class, restores
            // aria-expanded, calls onClose, optionally returns focus.
            // =================================================================
            function openPopover({ trigger, panel, onClose, returnFocus = true } = {}) {
                if (!panel) return () => {};
                let closed = false;
                let teardownOutside = null;
                let teardownEsc     = null;
                function close() {
                    if (closed) return;
                    closed = true;
                    panel.classList.remove("open");
                    if (trigger) trigger.setAttribute("aria-expanded", "false");
                    teardownOutside?.();
                    teardownEsc?.();
                    if (returnFocus && trigger && typeof trigger.focus === "function") {
                        try { trigger.focus(); } catch (_) {}
                    }
                    onClose?.();
                }
                panel.classList.add("open");
                if (trigger) trigger.setAttribute("aria-expanded", "true");
                teardownOutside = onOutside(panel, close);
                const onKey = (e) => { if (e.key === "Escape") { e.preventDefault(); close(); } };
                document.addEventListener("keydown", onKey, true);
                teardownEsc = () => document.removeEventListener("keydown", onKey, true);
                return close;
            }

            // Track teardowns for parse-time onOutside hookups.  No
            // caller invokes ``_globalTeardowns`` today — these listeners
            // live as long as the page does — but capturing the handles
            // here means a future componentization phase can call them
            // without rewriting every hookup site.  Pattern: anything
            // installed at IIFE parse time that should die with the SPA
            // pushes its teardown here.
            const _globalTeardowns = [];

            // Close user dropdown on outside press-and-release.
            _globalTeardowns.push(onOutside(
                () => document.getElementById("user-avatar"),
                () => {
                    const av = document.getElementById("user-avatar");
                    document.getElementById("user-dd")?.classList.remove("open");
                    av?.setAttribute("aria-expanded", "false");
                },
            ));

            // --- System Settings Panel ---
            let settingsOpen = false;
            let settingsTab = "chat";

            function openSettings(tab) {
                if (tab) settingsTab = tab;
                settingsOpen = true;
                $("scroll").style.display = "none";
                $("input-area").style.display = "none";
                // Scroll-bottom arrow is a sibling of #scroll and would
                // otherwise stay visible center-page when settings opens.
                const _sb = document.getElementById("scroll-bottom");
                if (_sb) _sb.style.display = "none";
                if ($("starters")) $("starters").style.display = "none";
                if ($("thinking")) $("thinking").style.display = "none";
                $("sys-settings").classList.add("open");
                renderSettingsTab();
                // URL sync — use ``pushState`` so the back button takes
                // the user out of settings back to their chat (matching
                // user expectation), not all the way out of the app.
                // For internal tab switches the URL still updates but a
                // back-button-then-forward round trip would re-traverse
                // tab changes; acceptable given tab switches are rare.
                if (!_applyingRoute) {
                    const target = settingsTab && settingsTab !== "chat"
                        ? `/settings/${settingsTab}` : "/settings";
                    if (window.location.pathname !== target) {
                        history.pushState({}, "", target);
                    }
                }
            }

            function closeSettings() {
                // Return live DOM nodes to hidden store
                const store = $("chat-settings-store");
                const c = $("chat-settings-content");
                if (c) store.appendChild(c);
                const m = $("memory-settings-content");
                if (m) store.appendChild(m);
                const s = $("starters-settings-content");
                if (s) store.appendChild(s);
                settingsOpen = false;
                $("sys-settings").classList.remove("open");
                $("sys-settings").innerHTML = "";
                $("scroll").style.display = "";
                $("input-area").style.display = "";
                const _sb = document.getElementById("scroll-bottom");
                if (_sb) _sb.style.display = "";
                if ($("starters")) $("starters").style.display = "";
                // Restore #thinking too — openSettings forced an inline
                // ``display: none`` on it, which permanently shadows the
                // ``#thinking.on { display: flex }`` rule until cleared.
                // Without this restoration the send-message indicator
                // silently never appears after a user visits Settings once.
                if ($("thinking")) $("thinking").style.display = "";
                // URL sync — when the user dismisses via the X button (not
                // browser back), step the history back so the address bar
                // matches the SPA state.  Uses history.back() when the
                // current entry is the /settings push we created in
                // openSettings; otherwise replaceState to the prior chat.
                if (!_applyingRoute && window.location.pathname.startsWith("/settings")) {
                    const target = activeId ? `/chat/${activeId}` : "/";
                    // Prefer back() so the previously-pushed entry doesn't
                    // strand in history.  replaceState is the fallback for
                    // cold deep links where no prior entry exists.
                    if (window.history.length > 1) {
                        history.back();
                    } else if (window.location.pathname !== target) {
                        history.replaceState({}, "", target);
                    }
                }
            }

            function switchSettingsTab(tab) {
                settingsTab = tab;
                renderSettingsTab();
            }

            function renderSettingsTab() {
                // Return live DOM nodes to hidden store before re-render
                const store = $("chat-settings-store");
                const chatContent = $("chat-settings-content");
                if (chatContent) store.appendChild(chatContent);
                const memContent = $("memory-settings-content");
                if (memContent) store.appendChild(memContent);
                const startersContent = $("starters-settings-content");
                if (startersContent) store.appendChild(startersContent);

                const el = $("sys-settings");
                const isAdmin = AUTH_STATE.user && AUTH_STATE.user.is_admin;
                const userTabHTML =
                    isAdmin ?
                        `<button class="sys-tab${settingsTab === "users" ? " active" : ""}" data-action="switch-tab" data-tab="users">Users</button>`
                    :   "";

                const tabDef = [
                    ["chat", "Chat"],
                    ["memory", "Memory"],
                    ["starters", "Starters"],
                    ["server", "Server"],
                    ["profile", "Profile"],
                    ["security", "Security"],
                ];
                let header = `<div class="sys-header"><button class="sys-back" data-action="close-settings">&larr;</button><h2>Settings</h2></div>`;
                let tabs =
                    '<div class="sys-tabs">' +
                    tabDef
                        .map(
                            ([k, v]) =>
                                `<button class="sys-tab${settingsTab === k ? " active" : ""}" data-action="switch-tab" data-tab="${k}">${v}</button>`,
                        )
                        .join("") +
                    userTabHTML +
                    "</div>";
                let content = '<div class="sys-content" id="sys-content">';

                if (settingsTab === "chat")
                    content += '<div id="chat-tab-slot"></div>';
                else if (settingsTab === "memory")
                    content += '<div id="memory-tab-slot"></div>';
                else if (settingsTab === "starters")
                    content += '<div id="starters-tab-slot"></div>';
                else if (settingsTab === "server") content += renderServerTab();
                else if (settingsTab === "profile")
                    content += renderProfileTab();
                else if (settingsTab === "security")
                    content += renderSecurityTab();
                else if (settingsTab === "users") content += renderUsersTab();

                content += "</div>";
                content += `<div style="text-align:center;padding:var(--sp-7) 0 var(--sp-4);font-size:0.6875rem;color:var(--faint)">LM Chat v${appVersion || "…"}</div>`;
                el.innerHTML = header + tabs + content;

                // Attach settings panel event listeners
                el.querySelector('[data-action="close-settings"]')?.addEventListener('click', closeSettings);
                el.querySelectorAll('[data-action="switch-tab"]').forEach(btn =>
                    btn.addEventListener('click', () => switchSettingsTab(btn.dataset.tab))
                );
                attachSettingsTabListeners(el, settingsTab);

                // Move live DOM nodes into their slots
                if (settingsTab === "chat") {
                    const slot = $("chat-tab-slot");
                    if (slot && chatContent) {
                        slot.appendChild(chatContent);
                        chatContent.style.display = "";
                    }
                } else if (settingsTab === "memory") {
                    const slot = $("memory-tab-slot");
                    if (slot && memContent) {
                        slot.appendChild(memContent);
                        memContent.style.display = "";
                        loadMemoryPanel();
                    }
                } else if (settingsTab === "starters") {
                    const slot = $("starters-tab-slot");
                    if (slot && startersContent) {
                        slot.appendChild(startersContent);
                        startersContent.style.display = "";
                        renderStarterSettings();
                    }
                }
            }

            function attachSettingsTabListeners(el, tab) {
                if (tab === "profile") {
                    el.querySelector('[data-action="save-profile"]')?.addEventListener('click', saveProfile);
                    el.querySelector('[data-action="change-password"]')?.addEventListener('click', doSettingsChangePassword);
                } else if (tab === "security") {
                    el.querySelector('[data-action="disable-totp"]')?.addEventListener('click', disableTotp);
                    el.querySelector('[data-action="start-totp-setup"]')?.addEventListener('click', startTotpSetup);
                } else if (tab === "users") {
                    el.querySelector('[data-action="create-user"]')?.addEventListener('click', doSettingsInvite);
                } else if (tab === "server") {
                    el.querySelector('[data-action="clear-api-key"]')?.addEventListener('click', clearApiKey);
                    el.querySelector('[data-action="save-server-settings"]')?.addEventListener('click', saveServerSettings);
                    el.querySelector('[data-action="add-remote-mcp"]')?.addEventListener('click', addRemoteMcp);
                    el.querySelector('[data-action="toggle-debug"]')?.addEventListener('change', function() { toggleDebugMode(this.checked); });
                }
            }

            function renderProfileTab() {
                const u = AUTH_STATE.user || {};
                return `
    <div class="sys-section">
      <h3>Profile</h3>
      <div class="sys-field"><label>Username</label><input type="text" value="${esc(u.username || "")}" readonly></div>
      <div class="sys-field"><label>Display Name</label><input type="text" id="sp-name" value="${esc(u.display_name || "")}"></div>
      <button class="sys-btn" data-action="save-profile">Save</button>
      <div id="sp-msg"></div>
    </div>
    <div class="sys-section">
      <h3>Change Password</h3>
      <div class="sys-field"><label>Current Password</label><input type="password" id="sp-curpw" autocomplete="current-password"></div>
      <div class="sys-field"><label>New Password</label><input type="password" id="sp-newpw" autocomplete="new-password"></div>
      <div class="sys-field"><label>Confirm New Password</label><input type="password" id="sp-confpw" autocomplete="new-password"></div>
      <button class="sys-btn" data-action="change-password">Change Password</button>
      <div id="sp-pw-msg"></div>
    </div>`;
            }

            async function saveProfile() {
                const name = $("sp-name").value.trim();
                const r = await apiFetch("/api/auth/profile", {
                    method: "PATCH",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ display_name: name }),
                });
                const d = await r.json();
                const msg = $("sp-msg");
                if (r.ok) {
                    msg.className = "sys-msg ok";
                    msg.textContent = "Saved";
                    AUTH_STATE.user.display_name = name;
                    showUserAvatar();
                } else {
                    msg.className = "sys-msg err";
                    msg.textContent = d.error || "Failed";
                }
            }

            async function doSettingsChangePassword() {
                const cur = $("sp-curpw").value;
                const np = $("sp-newpw").value;
                const conf = $("sp-confpw").value;
                const msg = $("sp-pw-msg");
                msg.textContent = "";
                msg.className = "sys-msg";
                if (np !== conf) {
                    msg.className = "sys-msg err";
                    msg.textContent = "Passwords do not match";
                    return;
                }
                // Password change invalidates the user's other sessions
                // server-side and rotates this one's cookie to a fresh
                // token.  Any background fetch (e.g. checkConnection's
                // poll) that started under the old cookie can still land
                // afterward with a 401.  Without the suppression window,
                // apiFetch would show the login screen even though the
                // change succeeded.  2 s comfortably covers the in-flight
                // window for the small set of background polls this app
                // runs; lengthen it if you add new ones.
                _suppressAuth = true;
                try {
                    const r = await fetch("/api/auth/change-password", {
                        method: "POST",
                        headers: {
                            "Content-Type": "application/json",
                            "X-Requested-With": "lm-chat",
                        },
                        body: JSON.stringify({
                            current_password: cur,
                            new_password: np,
                        }),
                    });
                    const d = await r.json();
                    if (r.ok) {
                        msg.className = "sys-msg ok";
                        msg.textContent = "Password changed";
                        $("sp-curpw").value = "";
                        $("sp-newpw").value = "";
                        $("sp-confpw").value = "";
                    } else {
                        msg.className = "sys-msg err";
                        msg.textContent = d.error || "Failed";
                    }
                } finally {
                    setTimeout(() => {
                        _suppressAuth = false;
                    }, 2000);
                }
            }

            function renderSecurityTab() {
                const u = AUTH_STATE.user || {};
                const enabled = u.totp_enabled;
                if (enabled) {
                    return `
      <div class="sys-section">
        <h3>Two-Factor Authentication</h3>
        <p style="color:var(--green);margin-bottom:var(--sp-7)">&#10003; 2FA is enabled</p>
        <div class="sys-field"><label>Enter current 2FA code to disable</label><input type="text" id="st-code" class="totp-verify-input" maxlength="6" inputmode="numeric" autocomplete="one-time-code" placeholder="000000"></div>
        <button class="sys-btn danger" data-action="disable-totp">Disable 2FA</button>
        <div id="st-msg"></div>
      </div>`;
                }
                return `
    <div class="sys-section">
      <h3>Two-Factor Authentication</h3>
      <p style="color:var(--dim);margin-bottom:var(--sp-7)">Add an extra layer of security to your account</p>
      <div id="totp-setup-area">
        <button class="sys-btn" data-action="start-totp-setup">Enable 2FA</button>
      </div>
      <div id="st-msg"></div>
    </div>`;
            }

            async function startTotpSetup() {
                const area = $("totp-setup-area");
                area.innerHTML =
                    '<p style="color:var(--dim)">Generating secret...</p>';
                const r = await apiFetch("/api/auth/totp/setup", {
                    method: "POST",
                });
                const d = await r.json();
                if (!r.ok) {
                    area.innerHTML = `<p style="color:var(--err-text)">${esc(d.error || "Failed")}</p>`;
                    return;
                }
                _totpSetupToken = d.setup_token;
                area.innerHTML = `
    <div class="totp-qr">
      ${sanitizeSvg(d.qr_svg)}
      <div style="margin-top:var(--sp-6);font-size:var(--text-xs);color:var(--dim)">Scan with your authenticator app</div>
    </div>
    <div style="margin:var(--sp-7) 0">
      <div style="font-size:var(--text-xs);color:var(--dim);margin-bottom:var(--sp-3)">Or enter this secret manually:</div>
      <div class="totp-secret" data-secret="${esc(d.secret)}" data-action="copy-totp-secret" title="Click to copy">${esc(d.secret)}</div>
    </div>
    <div style="margin-top:var(--sp-8)">
      <div style="font-size:var(--text-xs);color:var(--dim);margin-bottom:var(--sp-3)">Enter the 6-digit code from your app to verify:</div>
      <div style="display:flex;gap:var(--sp-5);align-items:center">
        <input type="text" id="st-verify-code" class="totp-verify-input" maxlength="6" inputmode="numeric" autocomplete="one-time-code" placeholder="000000" style="width:10rem">
        <button class="sys-btn" data-action="verify-totp">Verify</button>
      </div>
    </div>`;
                area.querySelector('[data-action="copy-totp-secret"]')?.addEventListener('click', function() {
                    copyToClipboard(this.dataset.secret);
                    this.style.opacity = '.6';
                    setTimeout(() => this.style.opacity = '1', 300);
                });
                area.querySelector('[data-action="verify-totp"]')?.addEventListener('click', verifyTotpSetup);
            }

            async function verifyTotpSetup() {
                const code = $("st-verify-code").value.trim();
                const msg = $("st-msg");
                if (msg) {
                    msg.textContent = "";
                    msg.className = "sys-msg";
                }
                if (code.length !== 6) {
                    if (msg) {
                        msg.className = "sys-msg err";
                        msg.textContent = "Enter a 6-digit code";
                    }
                    return;
                }
                const r = await apiFetch("/api/auth/totp/verify", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({
                        code,
                        setup_token: _totpSetupToken,
                    }),
                });
                const d = await r.json();
                if (r.ok) {
                    AUTH_STATE.user.totp_enabled = 1;
                    renderSettingsTab();
                } else {
                    if (msg) {
                        msg.className = "sys-msg err";
                        msg.textContent = d.error || "Verification failed";
                    }
                }
            }

            async function disableTotp() {
                const code = $("st-code").value.trim();
                const msg = $("st-msg");
                if (msg) {
                    msg.textContent = "";
                    msg.className = "sys-msg";
                }
                if (code.length !== 6) {
                    if (msg) {
                        msg.className = "sys-msg err";
                        msg.textContent = "Enter a 6-digit code";
                    }
                    return;
                }
                const r = await apiFetch("/api/auth/totp/disable", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ code }),
                });
                const d = await r.json();
                if (r.ok) {
                    AUTH_STATE.user.totp_enabled = 0;
                    renderSettingsTab();
                } else {
                    if (msg) {
                        msg.className = "sys-msg err";
                        msg.textContent = d.error || "Invalid code";
                    }
                }
            }
            function renderUsersTab() {
                // Trigger async user loading after render
                setTimeout(loadSettingsUsers, 0);
                return `
    <div class="sys-section">
      <h3>Users</h3>
      <div id="su-list"><p style="color:var(--dim)">Loading...</p></div>
    </div>
    <div class="sys-section">
      <h3>Invite New User</h3>
      <div class="sys-field"><label>Username</label><input type="text" id="su-user" autocomplete="off"></div>
      <div class="sys-field"><label>Display Name</label><input type="text" id="su-name" placeholder="Optional" autocomplete="off"></div>
      <div class="sys-field"><label>Password</label><input type="password" id="su-pass" autocomplete="new-password"></div>
      <button class="sys-btn" data-action="create-user">Create User</button>
      <div id="su-msg"></div>
    </div>`;
            }

            async function loadSettingsUsers() {
                const r = await apiFetch("/api/auth/users");
                if (!r.ok) return;
                const users = await r.json();
                const list = $("su-list");
                if (!list) return;
                list.innerHTML = "";
                if (!users.length) {
                    list.innerHTML = '<p style="color:var(--dim)">No users</p>';
                    return;
                }
                users.forEach((u) => {
                    const d = document.createElement("div");
                    d.className = "user-row";
                    const date = new Date(
                        u.created_at * 1000,
                    ).toLocaleDateString();
                    d.innerHTML =
                        `<div class="ur-name"><strong>${esc(u.display_name || u.username)}</strong><small>@${esc(u.username)} &middot; ${date}</small></div>` +
                        (u.is_admin ?
                            '<span class="ur-badge">admin</span>'
                        :   "");
                    if (u.id !== AUTH_STATE.user.id) {
                        const btn = document.createElement("button");
                        btn.className = "sys-btn danger";
                        btn.style.cssText = "padding:0.3125rem var(--sp-6);font-size:var(--text-xs)";
                        btn.textContent = "Delete";
                        btn.addEventListener("click", () =>
                            settingsDeleteUser(u.id, u.username),
                        );
                        d.appendChild(btn);
                    }
                    list.appendChild(d);
                });
            }

            async function settingsDeleteUser(id, name) {
                const ok = await showDialog({
                    title: "Delete User",
                    message:
                        "Delete user @" +
                        name +
                        "? This will also delete all their chats.",
                    confirmText: "Delete",
                    danger: true,
                });
                if (!ok) return;
                const r = await apiFetch("/api/auth/users/" + id, {
                    method: "DELETE",
                });
                if (r.ok) loadSettingsUsers();
            }

            async function doSettingsInvite() {
                const username = $("su-user").value.trim();
                const password = $("su-pass").value;
                const display_name = $("su-name").value.trim() || username;
                const msg = $("su-msg");
                msg.textContent = "";
                msg.className = "sys-msg";
                if (!username || !password) {
                    msg.className = "sys-msg err";
                    msg.textContent = "Fill in username and password";
                    return;
                }
                const r = await apiFetch("/api/auth/invite", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ username, password, display_name }),
                });
                const d = await r.json();
                if (!r.ok) {
                    msg.className = "sys-msg err";
                    msg.textContent = d.error || "Failed";
                    return;
                }
                msg.className = "sys-msg ok";
                msg.textContent = "User @" + username + " created";
                $("su-user").value = "";
                $("su-pass").value = "";
                $("su-name").value = "";
                loadSettingsUsers();
            }

            // --- API wrapper with 401 handling ---
            let _suppressAuth = false;
            async function apiFetch(url, opts = {}) {
                opts.headers = {
                    "X-Requested-With": "lm-chat",
                    ...(opts.headers || {}),
                };
                const resp = await fetch(url, opts);
                if (
                    resp.status === 401 &&
                    AUTH_STATE.enabled &&
                    !_suppressAuth
                ) {
                    // Forensic breadcrumb for the "Failed to load your
                    // settings — unauthorized" intermittent reported by
                    // operator.  document.cookie elides HttpOnly cookies
                    // by spec; a stale `lm_session` from a previous build
                    // CAN show up here if HttpOnly was ever missing,
                    // which is itself a useful signal.  See _crumb()
                    // for the underlying logger.
                    if (typeof _crumb === "function") {
                        _crumb("api.401", {
                            url: typeof url === "string" ? url : String(url),
                            method: opts.method || "GET",
                            authUser: AUTH_STATE.user?.username || null,
                            authEnabled: AUTH_STATE.enabled,
                            visibleCookieNames: (document.cookie || "")
                                .split(";").map(s => s.split("=")[0].trim()).filter(Boolean),
                            setCookieResp: resp.headers.get("Set-Cookie") || null,
                            bootPhase: state.boot?.phase || "?",
                        });
                    }
                    AUTH_STATE.user = null;
                    document.getElementById("user-avatar").classList.add("hidden");
                    const gear = document.getElementById("global-settings-btn");
                    if (gear) gear.classList.remove("hidden");
                    showAuthScreen(false);
                    throw new Error("unauthorized");
                }
                return resp;
            }

            let appVersion = "";

            // Enter key support on auth fields
            document.addEventListener("DOMContentLoaded", () => {
                ["a-user", "a-pass", "a-pass2", "a-name", "a-totp"].forEach(
                    (id) => {
                        const el = document.getElementById(id);
                        if (el)
                            el.addEventListener("keydown", (e) => {
                                if (e.key === "Enter") $("auth-btn").click();
                            });
                    },
                );
            });

            // MCP servers discovered from ~/.lmstudio/mcp.json via /api/mcp/servers.
            // Populated on init — all off by default, persisted per-server in localStorage.
            // Aliased to state.mcps (same array reference) — direct
            // mutations are immediately visible to anyone reading state,
            // but renders subscribed to "mcps" need an explicit
            // ``setState({mcps: state.mcps})`` after a mutation to fire.
            // Use the broadcastMcps() helper below at every write site.
            let MCPS = state.mcps;
            function broadcastMcps()      { setState({ mcps: state.mcps }); }
            function broadcastRemoteMcps(){ setState({ remoteMcps: state.remoteMcps }); }

            const STARTER_ICONS = {
                summarize:
                    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/><line x1="16" y1="13" x2="8" y2="13"/><line x1="16" y1="17" x2="8" y2="17"/><line x1="10" y1="9" x2="8" y2="9"/></svg>',
                explain:
                    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><path d="M9.09 9a3 3 0 0 1 5.83 1c0 2-3 3-3 3"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg>',
                brainstorm:
                    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M9.5 2A6.5 6.5 0 0 0 3 8.5c0 2.1 1 4 2.6 5.2.4.3.7.8.7 1.3v1a2 2 0 0 0 2 2h3.4a2 2 0 0 0 2-2v-1c0-.5.3-1 .7-1.3A6.5 6.5 0 0 0 9.5 2z"/><path d="M8 18v2"/><path d="M11 18v2"/><path d="M15 7l2-2"/><path d="M17 12h2"/><path d="M15 17l2 2"/></svg>',
                email: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><rect x="2" y="4" width="20" height="16" rx="2"/><path d="M22 7l-10 7L2 7"/></svg>',
                proscons:
                    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="3" width="18" height="18" rx="2"/><line x1="12" y1="3" x2="12" y2="21"/><line x1="8" y1="8" x2="6" y2="8"/><line x1="10" y1="12" x2="6" y2="12"/><line x1="18" y1="8" x2="14" y2="8"/><line x1="18" y1="12" x2="14" y2="12"/></svg>',
                debug: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="16 18 22 12 16 6"/><polyline points="8 6 2 12 8 18"/><line x1="14.5" y1="4" x2="9.5" y2="20"/></svg>',
            };
            const DEFAULT_STARTERS = [
                {
                    title: "Summarize",
                    prompt: "Summarize the key points of: ",
                    icon: "summarize",
                },
                {
                    title: "Explain",
                    prompt: "Explain this in simple terms: ",
                    icon: "explain",
                },
                {
                    title: "Brainstorm",
                    prompt: "Give me 10 creative ideas for: ",
                    icon: "brainstorm",
                },
                {
                    title: "Draft email",
                    prompt: "Draft a professional email about: ",
                    icon: "email",
                },
            ];
            function getStarters() {
                const c = localStorage.getItem("lsc-starters");
                if (c)
                    try {
                        return JSON.parse(c);
                    } catch {
                        // Corrupted localStorage blob — fall back to defaults
                        // silently so an old/broken value never blocks the UI.
                    }
                return DEFAULT_STARTERS;
            }
            function saveStarters(list) {
                localStorage.setItem("lsc-starters", JSON.stringify(list));
            }

            let activeId = null,
                sending = false,
                chatMeta = {},
                incognitoMode = false,
                _totpSetupToken = null;

            // Incognito session history — ephemeral, never persisted.
            // Holds {role, content} pairs so the model has context
            // without server-side response_id chaining.
            const INCOGNITO_HISTORY_MAX = 100; // max turns
            const INCOGNITO_HISTORY_MAX_CHARS = 50000; // max chars in context prefix
            let incognitoHistory = [];

            // --- Session stats (persisted in localStorage) ---
            // Cloud baseline pricing (GPT-5.5 Pro, the most expensive GPT-5.x SKU):
            // $30/1M input, $180/1M output (released April 24 2026; verified May 2026).
            const GPT5_INPUT_PER_TOKEN = 30 / 1e6,
                GPT5_OUTPUT_PER_TOKEN = 180 / 1e6;
            function loadSessionStats() {
                try {
                    return (
                        JSON.parse(
                            localStorage.getItem("lsc-session-stats"),
                        ) || { totalIn: 0, totalOut: 0, tpsSum: 0, tpsCount: 0 }
                    );
                } catch {
                    return { totalIn: 0, totalOut: 0, tpsSum: 0, tpsCount: 0 };
                }
            }
            function saveSessionStats(s) {
                localStorage.setItem("lsc-session-stats", JSON.stringify(s));
            }
            function recordTokens(inp, out, tps) {
                const s = loadSessionStats();
                s.totalIn += inp || 0;
                s.totalOut += out || 0;
                if (tps > 0) {
                    s.tpsSum += tps;
                    s.tpsCount++;
                }
                saveSessionStats(s);
                updateSidebarStats(s);
            }
            function updateSidebarStats(s) {
                if (!s) s = loadSessionStats();
                const total = s.totalIn + s.totalOut;
                const el = (id) => document.getElementById(id);
                const tf = el("sf-tokens");
                if (tf)
                    tf.textContent =
                        total >= 1e6 ? (total / 1e6).toFixed(1) + "M"
                        : total >= 1e3 ? (total / 1e3).toFixed(1) + "K"
                        : String(total);
                const tp = el("sf-tps");
                if (tp)
                    tp.textContent =
                        s.tpsCount > 0 ?
                            (s.tpsSum / s.tpsCount).toFixed(1) + " tok/s"
                        :   "—";
                const sv = el("sf-saved");
                if (sv) {
                    const cost =
                        s.totalIn * GPT5_INPUT_PER_TOKEN +
                        s.totalOut * GPT5_OUTPUT_PER_TOKEN;
                    sv.textContent = "$" + cost.toFixed(2);
                }
            }
            let pendingAttachments = [];
            const MAX_IMAGE_SIZE = 20 * 1024 * 1024;
            const ALLOWED_IMAGE_TYPES = [
                "image/jpeg",
                "image/png",
                "image/webp",
                "image/gif",
            ];

            const $ = (id) => document.getElementById(id);
            const scroll = $("scroll"),
                msgs = $("msgs"),
                input = $("input"),
                send = $("send"),
                modelSel = $("model-sel");

            // --- Server settings state ---
            let serverSettings = { hasApiKey: false, lmUrl: "" };

            function renderServerTab() {
                const statusText =
                    serverSettings.hasApiKey ?
                        "API key configured (stored server-side)"
                    :   "No API key set";
                const statusColor =
                    serverSettings.hasApiKey ? "var(--accent)" : "var(--faint)";
                const urlVal = serverSettings.lmUrl || "http://localhost:1234";
                setTimeout(() => {
                    renderModelList();
                    renderMcpList();
                    renderRemoteMcps();
                    loadDebugState();
                    checkConnection();
                }, 0);
                return `
    <div class="sys-section">
      <h3>LM Studio Connection</h3>
      <div class="sys-field"><label>Server URL</label><input type="text" id="ss-url" value="${esc(urlVal)}" placeholder="http://localhost:1234"></div>
      <div class="sys-field"><label>API Key</label><div style="display:flex;gap:var(--sp-4)"><input type="password" id="ss-apikey" placeholder="${serverSettings.hasApiKey ? "••••••••  (saved)" : "Enter API key"}" autocomplete="off" style="flex:1">${serverSettings.hasApiKey ? '<button class="sys-btn" data-action="clear-api-key" style="white-space:nowrap;background:var(--err-bg);color:var(--err-text)">Clear</button>' : ""}<button class="sys-btn" data-action="save-server-settings" style="white-space:nowrap">${serverSettings.hasApiKey ? "Update" : "Save"}</button></div></div>
      <div id="ss-status" style="font-size:var(--text-xs);color:${statusColor};margin-top:var(--sp-4)">${statusText}</div>
      <div id="ss-conn" style="margin-top:var(--sp-6)"></div>
    </div>
    <div class="sys-section">
      <h3>Loaded Models</h3>
      <div id="model-list"></div>
      <div style="font-size:var(--text-xs);color:var(--faint);margin-top:var(--sp-4)">Load and unload models in LM Studio</div>
    </div>
    <div class="sys-section">
      <h3>MCP Tools</h3>
      <div id="mcp-list"></div>
    </div>
    <div class="sys-section">
      <h3>Remote MCP Servers</h3>
      <div id="remote-mcp-list"></div>
      <button data-action="add-remote-mcp" style="background:none;border:1px dashed var(--border);color:var(--dim);width:100%;padding:var(--sp-4);border-radius:0.5rem;cursor:pointer;font-size:var(--text-sm);margin-top:var(--sp-4)">+ Add Remote Server</button>
      <div style="font-size:var(--text-xs);color:var(--faint);margin-top:var(--sp-4)">Requires "Allow per-request MCPs" in LM Studio Developer Settings</div>
    </div>
    <div class="sys-section">
      <h3>Debug Logging</h3>
      <div style="display:flex;align-items:center;gap:var(--sp-6)">
        <label class="sw"><input type="checkbox" id="ss-debug" aria-label="Verbose debug mode" data-action="toggle-debug"><span class="slider"></span></label>
        <span style="font-size:var(--text-base)">Verbose debug mode</span>
      </div>
      <div id="ss-debug-info" style="font-size:var(--text-xs);color:var(--faint);margin-top:var(--sp-4)">Logs requests, SSE events, memory operations, and tool calls to <code style="font-size:0.6875rem">logs/</code></div>
      <div id="ss-log-files" style="margin-top:var(--sp-4)"></div>
    </div>`;
            }

            async function saveServerSettings() {
                const url = $("ss-url").value.trim();
                const key = $("ss-apikey").value.trim();
                const status = $("ss-status");
                const payload = {};
                if (url) payload.lm_url = url;
                if (key) payload.lm_apikey = key;
                // Don't send lm_apikey='' when field is empty — that would clear the saved key
                // Use clearApiKey() to explicitly remove a key
                const r = await apiFetch("/api/auth/settings", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify(payload),
                });
                if (r.ok) {
                    status.textContent = "Settings saved";
                    status.style.color = "var(--accent)";
                    $("ss-apikey").value = "";
                    if (key) serverSettings.hasApiKey = true;
                    if (url) serverSettings.lmUrl = url;
                    checkConnection();
                    refreshModels();
                } else {
                    status.textContent = "Failed to save";
                    status.style.color = "var(--err-text)";
                }
            }

            async function clearApiKey() {
                const r = await apiFetch("/api/auth/settings", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ lm_apikey: "" }),
                });
                if (r.ok) {
                    serverSettings.hasApiKey = false;
                    const status = $("ss-status");
                    if (status) {
                        status.textContent = "API key cleared";
                        status.style.color = "var(--faint)";
                    }
                    openSettings("server");
                    checkConnection();
                    refreshModels();
                }
            }

            async function loadDebugState() {
                try {
                    const r = await apiFetch("/api/debug");
                    if (!r.ok) return;
                    const d = await r.json();
                    const cb = $("ss-debug");
                    if (cb) cb.checked = d.enabled;
                    const el = $("ss-log-files");
                    if (el && d.log_files && d.log_files.length) {
                        el.innerHTML = d.log_files
                            .map(
                                (f) =>
                                    `<div style="font-size:0.6875rem;color:var(--dim);padding:var(--sp-1) 0"><code>${esc(f.name)}</code> — ${(f.size / 1024).toFixed(1)} KB</div>`,
                            )
                            .join("");
                    }
                } catch (e) {
                    console.error("loadDebugState:", e);
                }
            }
            async function toggleDebugMode(on) {
                try {
                    const r = await apiFetch("/api/debug", {
                        method: "POST",
                        headers: { "Content-Type": "application/json" },
                        body: JSON.stringify({ enabled: on }),
                    });
                    if (!r.ok) {
                        const cb = $("ss-debug");
                        if (cb) cb.checked = !on;
                    } else loadDebugState();
                } catch (e) {
                    const cb = $("ss-debug");
                    if (cb) cb.checked = !on;
                }
            }

            // API key is stored server-side via saveServerSettings(), never in localStorage
            async function loadSettings() {
                try {
                    const r = await apiFetch("/api/auth/settings");
                    if (!r.ok) return;
                    const d = await r.json();
                    serverSettings.hasApiKey = !!d.lm_apikey;
                    serverSettings.lmUrl = d.lm_url || "";
                    if (d.remote_mcps) {
                        // Mutate in place to preserve the remoteMcps alias.
                        remoteMcps.length = 0;
                        d.remote_mcps.forEach((m) => remoteMcps.push(m));
                        broadcastRemoteMcps();
                        renderRemoteMcps();
                    }
                } catch (e) {
                    console.error("loadSettings:", e);
                    toast.error("Failed to load your settings.", { detail: e.message });
                }
            }

            // --- Connection status ---
            const connDot = document.querySelector("#conn-status .status-dot");
            const connLabel = $("conn-label");
            function setConn(state, text) {
                connDot.className = "status-dot " + state;
                $("conn-status").title =
                    state === "green" ? "Connected — " + text
                    : state === "yellow" ? "Connecting..."
                    : "Disconnected";
                if (connLabel) {
                    connLabel.textContent = state === "red" ? "LM Studio offline" : "";
                }
                const sc = $("ss-conn");
                if (sc) {
                    if (state === "green")
                        sc.innerHTML =
                            '<span style="color:var(--green)">&#10003; Connected to LM Studio' +
                            (text ? " — " + esc(text) : "") +
                            "</span>";
                    else if (state === "yellow")
                        sc.innerHTML =
                            '<span style="color:var(--dim)">Connecting...</span>';
                    else
                        sc.innerHTML =
                            '<span style="color:var(--err-text)">&#10007; Not connected — is LM Studio running?</span>';
                }
            }

            async function checkConnection() {
                setConn("yellow", "...");
                try {
                    await refreshModels();
                } catch (e) {
                    setConn("red", "Disconnected");
                    if (!modelSel.options.length)
                        // Empty value + disabled so any accidental send is
                        // rejected at the server with a clear "model too short"
                        // error rather than going out under a fake model id.
                        modelSel.innerHTML = '<option value="" disabled selected>No model — LM Studio offline</option>';
                }
            }
            // checkConnection interval is started inside initApp after auth check
            modelSel.onchange = () => {
                localStorage.setItem("lsc-model", modelSel.value);
                if (activeId && chatMeta[activeId])
                    chatMeta[activeId].response_id = null;
                updateModelPill();
                updateTopModelLabel();
                syncModelSettings();
            };

            function syncModelSettings() {
                const m = cachedModels.find((x) => x.id === modelSel.value);
                if (!m) return;
                // Show instance config (read-only info from LM Studio)
                const cfg = m.instance_config || {};
                const el = $("model-config-info");
                const caps = m.capabilities || {};

                // Universal capability gate driven by the server's per-model
                // ``capabilities.unsupported_params`` list — populated when
                // LM Studio rejects a parameter with "does not (support|
                // expose) ... configuration".  We disable any UI control
                // whose value lives in that list so the user can't pick a
                // setting the server will silently drop.
                //
                // Adding a new param to the gate is one entry in this map
                // below: ``param-name → [element-id, ...]``.  Keep map keys
                // matching the exact field name LM Studio reports back so
                // the lookup is direct.
                // ALL reasoning controls (form dropdowns AND the input-area
                // cycle button) are disabled when capabilities.unsupported_params
                // contains "reasoning".  Previous design left the cycle button
                // clickable for "user-explicit override" — but on models with
                // intrinsic always-on thinking (qwen3.6 etc), the param has no
                // knob to turn at all, so clicking it just wastes a 400+retry
                // round-trip with no user-visible effect.  Better UX: disable
                // the control and let the model's intrinsic reasoning run.
                // If we ever need a "try anyway" escape hatch for transient
                // mis-cached params, give it its own button — don't conflate
                // it with the normal effort-cycle UX.
                const PARAM_CONTROLS = {
                    reasoning: ["s-reasoning", "cs-reasoning", "reasoning-btn"],
                };
                const unsupported = new Set(caps.unsupported_params || []);
                Object.entries(PARAM_CONTROLS).forEach(([param, ids]) => {
                    const blocked = unsupported.has(param);
                    const tooltip = blocked
                        ? `This model does not expose ${param} configuration.`
                        : "";
                    ids.forEach((id) => {
                        const ctl = document.getElementById(id);
                        if (!ctl) return;
                        ctl.disabled = blocked;
                        ctl.title = tooltip;
                    });
                });

                // Reasoning button — sync its visible label and data-state
                // to whatever the current chat has (or "off" if not set),
                // and use the model's documented allowed_options if
                // available.  The cycle handler below uses the same
                // ordering so the button can't show a value the active
                // model doesn't accept.
                syncReasoningBtn();

                if (!el) return;
                const tags = [];
                if (caps.vision) tags.push("Vision");
                if (caps.trained_for_tool_use) tags.push("Tool Use");
                unsupported.forEach((p) => tags.push(`Fixed ${p[0].toUpperCase()}${p.slice(1)}`));
                if (cfg.flash_attention) tags.push("Flash Attn");
                if (cfg.offload_kv_cache_to_gpu) tags.push("KV→GPU");
                const parts = [];
                if (m.context_length)
                    parts.push(
                        `Context: ${(m.context_length / 1024).toFixed(0)}K`,
                    );
                if (cfg.eval_batch_size)
                    parts.push(`Eval Batch: ${cfg.eval_batch_size}`);
                el.innerHTML =
                    parts.length || tags.length ?
                        `<span class="mci-stats">${parts.join(" · ")}</span>` +
                        (tags.length ?
                            `<span class="mci-tags">${tags.map((t) => `<span class="mci-tag">${t}</span>`).join("")}</span>`
                        :   "")
                    :   "";
            }

            // ---- Reasoning effort (universal across providers) -------------
            // SPA stores a canonical value in cs.reasoning ∈
            // {"off","low","medium","high","on"}.  The payload-builder for
            // each backend translates: LM Studio native = ``reasoning`` enum
            // (current); OpenAI-compat would map to ``reasoning.effort``;
            // Anthropic would map to a thinking budget tokens int; OpenRouter
            // forwards provider-specific.  The SPA UI is one button that
            // cycles through the values the ACTIVE MODEL declares it accepts.
            const REASONING_CANONICAL = ["off", "low", "medium", "high", "on"];
            function _activeAllowedReasoning() {
                const m = cachedModels.find((x) => x.id === modelSel.value);
                const caps = (m && m.capabilities) || {};
                const opts = caps.reasoning && Array.isArray(caps.reasoning.allowed_options)
                    ? caps.reasoning.allowed_options
                    : null;
                if (!opts || !opts.length) {
                    // Capability metadata absent — fall back to the full
                    // canonical set so the button stays interactive.  The
                    // server's proactive blacklist (capabilities.unsupported_params)
                    // governs whether the button is DISABLED entirely; if
                    // we reach here at all, the param isn't blacklisted.
                    return REASONING_CANONICAL.slice();
                }
                // Preserve canonical ordering, filter to model's allowed set.
                const allowed = new Set(opts);
                return REASONING_CANONICAL.filter((v) => allowed.has(v));
            }
            // Single render function for ALL reasoning UI surfaces.  Reads
            // from state via effectiveReasoning() so the cycle button label,
            // the chat-settings dropdown, and the global-settings dropdown
            // can never disagree.  Subscribed below to state.chatSettings +
            // state.serverInfo — runs whenever either changes.
            function renderReasoningUI() {
                const value = effectiveReasoning();
                const allowed = _activeAllowedReasoning();
                const inAllowed = allowed.includes(value);
                const clamped = inAllowed ? value : (allowed[0] || "off");

                const btn = document.getElementById("reasoning-btn");
                if (btn) {
                    btn.dataset.reasoning = clamped;
                    const label = btn.querySelector(".reasoning-label");
                    if (label) label.textContent = clamped.toUpperCase();
                    btn.title = btn.disabled
                        ? "This model does not expose reasoning configuration."
                        : `Reasoning effort: ${clamped.toUpperCase()} — click to cycle`;
                }
                const chatDd = document.getElementById("cs-reasoning");
                if (chatDd) {
                    // Empty string = "use global" for the chat-settings dropdown.
                    chatDd.value = state.chatSettings.reasoning ?? "";
                }
                const globalDd = document.getElementById("s-reasoning");
                if (globalDd) {
                    globalDd.value = state.serverInfo.defaultReasoning ?? "off";
                }
            }
            // Legacy name kept as alias for any external caller.
            const syncReasoningBtn = renderReasoningUI;
            // ``models`` not subscribed — _activeAllowedReasoning() reads
            // cachedModels lazily inside cycleReasoning(), so the reasoning
            // UI doesn't need to rerender on every model-list refresh.
            subscribe(["chatSettings", "serverInfo", "activeChatId"], renderReasoningUI);

            // -----------------------------------------------------------------
            // Alias-integrity runtime guard.  Catches the pattern where a
            // developer reassigns a legacy mirror (``MCPS = [...]``) instead
            // of mutating in place — which would silently sever the alias to
            // state.mcps and starve subscribed renders.  Runs after every
            // state change in dev mode; logs at error level + offers a
            // ``console.dir(state)`` breadcrumb so the regression is loud.
            // -----------------------------------------------------------------
            function _assertAliases() {
                const bad = [];
                if (MCPS         !== state.mcps)         bad.push("MCPS / state.mcps");
                if (remoteMcps   !== state.remoteMcps)   bad.push("remoteMcps / state.remoteMcps");
                if (cachedModels !== state.models)       bad.push("cachedModels / state.models");
                if (chatSettingsCache !== state.chatSettings) bad.push("chatSettingsCache / state.chatSettings");
                if (bad.length) {
                    console.error(
                        "Alias integrity broken — these legacy mirrors diverged " +
                        "from state and will starve subscribed renders:\n  " +
                        bad.join("\n  ") +
                        "\nLikely cause: a write site reassigned the variable " +
                        "(e.g. ``MCPS = [...]``) instead of mutating in place " +
                        "(``MCPS.length = 0; MCPS.push(...)``)."
                    );
                }
            }
            subscribe(["mcps", "remoteMcps", "models", "chatSettings"], _assertAliases);

            // Toggle ``.busy`` class on regions whose state.busy[key] is true.
            // Adding a new busy region: add one selector here + a CSS rule
            // for ``[data-busy] > .busy-indicator { display: block }`` or
            // similar.  Keeps loading UX consistent and central.
            const _BUSY_REGIONS = {
                "sidebar-chats": () => document.getElementById("chat-list"),
                "model-list":    () => document.getElementById("model-dd"),
            };
            subscribe(["busy"], () => {
                for (const [key, getEl] of Object.entries(_BUSY_REGIONS)) {
                    const el = getEl();
                    if (!el) continue;
                    el.classList.toggle("busy", !!state.busy[key]);
                }
            });

            // =================================================================
            // Phase 2 — declarative render subscriptions.
            //
            // Each subscribe() below ties a render function to the state
            // keys it depends on.  When ``setState`` fires for any of those
            // keys, the render runs on the next animation frame (batched).
            // Old code keeps calling the render functions imperatively too —
            // that's fine, double-render is cheap.  Over time the imperative
            // calls disappear.
            // =================================================================
            // Renders below reference functions defined LATER in the file
            // (renderMcpList, renderRemoteMcps, renderModelList,
            // renderChatSettingsPanel, renderList).  Subscriptions register
            // at parse time but only fire on the first setState, which can't
            // happen until after the function definitions execute — safe.
            subscribe(["mcps"],         () => { try { renderMcpList(); }         catch (_) {} });
            subscribe(["remoteMcps"],   () => { try { renderRemoteMcps(); }      catch (_) {} });
            subscribe(["models"],       () => { try { renderModelList(); }       catch (_) {} });
            subscribe(["chats", "activeChatId"], () => { try { renderList(); }   catch (_) {} });
            // The chat settings panel only renders when visible — skip when
            // closed to avoid touching DOM nodes that aren't in the tree.
            subscribe(["chatSettings", "models", "view"], () => {
                if (state.view.rightPanel === "settings") {
                    try { renderChatSettingsPanel(); } catch (_) {}
                }
            });

            function cycleReasoning() {
                const btn = document.getElementById("reasoning-btn");
                if (!btn) return;
                const allowed = _activeAllowedReasoning();
                if (!allowed.length) return;
                const cur = effectiveReasoning();
                const idx = allowed.indexOf(cur);
                const next = allowed[(idx + 1) % allowed.length];
                // Single write path: setState() updates state.chatSettings;
                // chatSettingsCache is aliased to the same object so
                // legacy readers see the change too.  Subscribed
                // renderReasoningUI() runs on next RAF and reflects the
                // change across the button, chat-dropdown, and global-dropdown.
                setState({ chatSettings: { reasoning: next } });
                if (typeof saveChatSetting === "function") {
                    saveChatSetting("reasoning", next);
                }
                // Mark explicit user choice so server's proactive capability
                // gate doesn't strip the param this turn.
                if (!window._userSetParams) window._userSetParams = new Set();
                window._userSetParams.add("reasoning");
            }
            document.addEventListener("DOMContentLoaded", () => {
                const btn = document.getElementById("reasoning-btn");
                if (btn) btn.addEventListener("click", cycleReasoning);
            });

            // --- Settings (localStorage only — these are per-device prefs) ---
            $("s-sys").value = localStorage.getItem("lsc-sys") || "";
            $("s-sys").onchange = () => localStorage.setItem("lsc-sys", $("s-sys").value);

            // Load memory toggle state
            apiFetch("/api/insights/settings")
                .then((r) => r.json())
                .then((d) => {
                    const el = $("s-memory");
                    if (el) el.checked = d.memory_enabled !== "false";
                })
                .catch((e) => console.error("insight settings GET failed:", e));

            // --- System Prompt Presets ---
            const PRESETS = {
                general: `You are a sharp, knowledgeable assistant with deep technical expertise and broad intellectual curiosity. Today is {{current_date}}.

## PERSONALITY

You're the friend who happens to know a lot about everything — software, hardware, science, philosophy, music, books, life. You talk like a real person.

- Be direct and honest. If something is a bad idea, say so.
- Have opinions. Say what you actually think, then explain why.
- Push back when the user is wrong. Respect is telling someone the truth, not agreeing with them.
- Match the user's energy. Casual question gets a casual answer. Deep question gets depth.
- Be funny when it's natural, not forced.

## TECHNICAL CONVERSATIONS

- Calibrate to the user's level. Skip basics they already know.
- Explain ML, inference, GPU, and hardware concepts naturally as they come up.
- Use analogies from web dev, databases, gaming, or music production when they fit.

{{tools}}

## RESPONSE STYLE

- Answer first, then elaborate. Lead with your recommendation.
- Keep responses proportional to the question. One-line questions get concise answers.
- State what you know confidently. When uncertain, say so and look it up.
- Disagree when you have reason to. Sycophancy is not helpful.`,

                coder: `You are an expert software engineering agent. Today is {{current_date}}.

## PRINCIPLES

1. Read files before editing them. Understand existing structure first.
2. Verify API signatures against current documentation to ensure accuracy.
3. Plan the approach before writing code.
4. Write complete, working functions. Every function you produce must be ready to run.
5. Look up imports and method signatures to verify them.

{{tools}}

## WORKFLOW

1. **Understand** — Read relevant files. Learn the existing patterns.
2. **Plan** — What changes are needed? What could go wrong?
3. **Research** — Verify APIs and signatures against current docs.
4. **Implement** — Write complete code. Match existing style.
5. **Verify** — Read back modified files. Check for missing imports, edge cases.

## CODE STANDARDS

- Match the existing codebase's style and conventions.
- Write the minimum code needed to solve the problem correctly.
- Keep imports clean. Remove dead code. Avoid speculative abstractions.
- Handle errors at system boundaries, not against impossible internal states.`,

                creative: `You are a skilled writer and creative collaborator. Today is {{current_date}}.

## WHO YOU ARE

You write like someone who's read everything and remembers what worked. You know craft — structure, rhythm, voice, tension, subtext — and you deploy it without showing off. You're a co-writer, not a content generator.

## VOICE

- Write like a human. No AI slop. No "the silence was deafening." No "a testament to." If a phrase sounds like it came from a corporate blog, kill it.
- Favor concrete over abstract. "He hadn't eaten since Tuesday" hits harder than "he was consumed by hunger."
- Vary sentence length. Short sentences punch. Longer ones build rhythm and carry the reader through a thought.
- Trust the reader. Don't explain the emotion — create the conditions for it.
- Kill adverbs unless they earn their spot.

## COLLABORATION STYLE

- When the user shares writing, respond as a thoughtful workshop partner. Name what's working first, then identify what isn't and why.
- Don't rewrite their voice into yours. Strengthen THEIR voice.
- Offer specific craft suggestions, not vague praise.
- If they ask you to write something, ask about tone, audience, and intent before drafting. Then write ONE strong version.

## WHEN GENERATING

- Open strong. No throat-clearing. Start where the energy is.
- End stronger. The last line is what the reader carries away.
- Dialogue should sound like people actually talk — interruptions, deflections, things left unsaid.
- Surprise yourself. If you know where a sentence is going, the reader does too.

## STANDARDS

- Write like a human. You know the difference.
- Use metaphor sparingly. One good metaphor per page beats five per paragraph.
- Let endings be earned, not defaulted. Tidy resolutions need justification.
- Write what the story needs, including darkness.`,

                research: `You are a deep research agent. Today is {{current_date}}.

## APPROACH
Research every question using your tools before answering. Verify with current sources to ensure accuracy. Search first, synthesize second, answer third.

{{tools}}

## RESEARCH PROCESS

1. **Think first** — What do I need to find out? What are the sub-questions?
2. **Search broadly** — Multiple queries, different angles. Use every relevant search tool available.
3. **Read deeply** — Full pages, not snippets. Minimum 3 sources.
4. **Identify gaps** — What's still missing? Any contradictions?
5. **Search again** if gaps exist
6. **Synthesize** — Combine findings into a clear, organized response

## RESPONSE STYLE

- Be detailed and thorough. Depth over brevity.
- Cite your sources. Every factual claim should trace back to a source you actually read.
- Surface contradictions between sources explicitly.
- Suggest angles the user didn't think of.
- Flag when evidence is thin or inconclusive rather than presenting speculation as fact.`,

                analyst: `You are a strategic analyst. You take raw information and turn it into clear analysis and actionable plans. Today is {{current_date}}.

## YOUR ROLE

1. Read and deeply understand what you're given
2. Find the patterns, contradictions, and connections others missed
3. Form a concrete opinion — not a balanced summary
4. Formulate a clear strategy with concrete next steps

## PRINCIPLES

- Produce new insights, not summaries. Synthesis connects dots across sources.
- Identify what's missing. Surface unverified assumptions.
- Surface contradictions — that's where the interesting analysis lives.
- Rank by impact. Say what matters most and why.
- Include invalidation criteria. Every conclusion states what would prove it wrong.
- End with concrete next steps.

## ANALYTICAL FRAMEWORK

1. **Situational Assessment** — Core dynamics in 2-3 sentences
2. **Key Findings** (3-5 max) — Synthesized insights connecting dots across sources
3. **Multi-Dimensional Analysis** — Technical feasibility, economic impact, timeline dynamics, competitive landscape, dependencies
4. **Tensions and Contradictions** — Where sources conflict, assess which side has stronger evidence
5. **Gap Analysis** — What's missing? Rank by impact on conclusion
6. **Strategic Options** — 2-3 viable paths with tradeoffs and invalidation triggers
7. **Recommendation** — Pick one. Say why. First concrete step, success signal, reassess trigger.

## CONFIDENCE TAGGING

Tag every major claim: **High confidence** / **Moderate confidence** / **Low confidence / Speculative** / **Unverified assumption**

## RESPONSE STYLE

- Analyze, not summarize. Prioritize ruthlessly.
- Lead with your recommendation, then support it.
- State your confidence level. When evidence is thin, say so.`,

                architect: `You are a systems architect. You turn strategic analysis, requirements, and research findings into concrete technical plans. Today is {{current_date}}.

## YOUR ROLE

You are the bridge between "what should we do" and "how exactly do we build it." Your deliverables:
1. Architecture decisions with explicit rationale
2. Component breakdowns with clear boundaries and interfaces
3. Technology selections justified against alternatives
4. Phased implementation plans that can be handed to a developer

## PRINCIPLES

- Every decision includes rationale and what alternatives were rejected.
- Ask what the user already has before proposing architecture.
- Design for what's needed now. Over-engineering is a bug.
- Every component has a single responsibility and a defined interface.
- Name the tradeoffs. If you can't name what you're giving up, you don't understand the choice.

## ARCHITECTURAL PROCESS

1. **Constraints Inventory** — Resources, technical limits, team skills, non-negotiables
2. **System Overview** — One paragraph. What it does end to end.
3. **Component Architecture** — Name, responsibility, inputs/outputs, interface, key detail, risk
4. **Technology Selection** — What, why, what was rejected, lock-in risk, verification step
5. **Phased Roadmap** — Each phase produces something runnable with testable done criteria
6. **Risk Register** — Top 3-5 risks with likelihood, impact, and mitigation

## THINKING STYLE

- Think in interfaces, not implementations. Define boundaries first.
- Think in failure modes. What happens when this component is slow or unavailable?
- Think in iterations. The first version should be embarrassingly simple.
- Use well-known patterns. Name them so developers can look them up.

## RESPONSE STYLE

- Understand before designing. Read existing code first.
- Be precise about interfaces — format, protocol, failure behavior.
- Estimate complexity (small/medium/large), not time.`,
            };

            // --- Prompt Variables ---
            function expandVars(text) {
                const now = new Date();
                const days = [
                    "Sunday",
                    "Monday",
                    "Tuesday",
                    "Wednesday",
                    "Thursday",
                    "Friday",
                    "Saturday",
                ];
                return text
                    .replace(
                        /\{\{current_date\}\}/g,
                        now.toISOString().slice(0, 10),
                    )
                    .replace(/\{\{day_of_week\}\}/g, days[now.getDay()])
                    .replace(
                        /\{\{current_time\}\}/g,
                        now.toLocaleTimeString("en-US", {
                            hour: "2-digit",
                            minute: "2-digit",
                            hour12: true,
                        }),
                    )
                    .replace(/\{\{model\}\}/g, modelSel.value || "unknown")
                    .replace(/\{\{tools\}\}/g, "") // LM Studio injects tool schemas server-side; {{tools}} expands to empty
                    .replace(/\{\{memories\}\}/g, ""); // handled server-side
            }

            function applyPreset() {
                const v = $("s-preset").value;
                if (v === "custom") {
                    $("s-sys").value = "";
                    localStorage.setItem("lsc-sys", "");
                    localStorage.setItem("lsc-preset", "custom");
                    return;
                }
                if (PRESETS[v]) {
                    $("s-sys").value = PRESETS[v];
                    localStorage.setItem("lsc-sys", PRESETS[v]);
                    localStorage.setItem("lsc-preset", v);
                }
            }
            // Track manual edits — switch dropdown to "Custom"
            $("s-sys").addEventListener("input", () => {
                $("s-preset").value = "custom";
                localStorage.setItem("lsc-sys", $("s-sys").value);
                localStorage.setItem("lsc-preset", "custom");
            });
            // On load, restore preset selection
            (function initPreset() {
                const saved = localStorage.getItem("lsc-preset");
                if (
                    saved &&
                    Array.from($("s-preset").options).some(
                        (o) => o.value === saved,
                    )
                ) {
                    $("s-preset").value = saved;
                    if (saved !== "custom" && PRESETS[saved])
                        $("s-sys").value = PRESETS[saved];
                } else {
                    // First load — populate with default preset
                    const def = $("s-preset").value;
                    if (PRESETS[def]) $("s-sys").value = PRESETS[def];
                }
            })();
            // Seed the global-default reasoning into the state machine
            // from localStorage at boot.  Settings panel input reflects it;
            // setState broadcast triggers any subscribed reasoning renders.
            {
                const _stored = localStorage.getItem("lsc-reasoning") || "off";
                setState({ serverInfo: { defaultReasoning: _stored } });
                $("s-reasoning").value = _stored;
            }
            $("s-followups").checked =
                localStorage.getItem("lsc-followups") !== "off";
            $("s-sc").checked =
                localStorage.getItem("lsc-sc") === "1";
            $("s-cove").checked =
                localStorage.getItem("lsc-cove") === "1";
            // --- MCP toggles (populated from /api/mcp/servers) ---
            // Comma-separated tool whitelist per integration.  Stored per
            // plugin id in localStorage; remote MCPs round-trip via the
            // server (encrypted at rest alongside auth).  Empty list ==
            // "use every tool the server exposes" — that's the bare-string
            // shape we keep emitting in that case.
            function _allowedToolsKey(id) { return "lsc-mcp-tools-" + id; }
            function _readAllowedTools(id) {
                const v = localStorage.getItem(_allowedToolsKey(id));
                if (!v) return [];
                try { const arr = JSON.parse(v); return Array.isArray(arr) ? arr : []; }
                catch (_) { return []; }
            }
            function _writeAllowedTools(id, arr) {
                if (arr && arr.length) {
                    localStorage.setItem(_allowedToolsKey(id), JSON.stringify(arr));
                } else {
                    localStorage.removeItem(_allowedToolsKey(id));
                }
            }
            async function editAllowedTools(label, current, onSave) {
                // Dialog: comma-separated tool names.  We have no
                // upstream tools/list discovery yet (intentionally —
                // adding an MCP JSON-RPC client to a stdlib-only server
                // is out of scope for this delta), so the user types
                // the names.  Empty submission clears the whitelist.
                const val = await showDialog({
                    title: `Allowed tools for ${label}`,
                    message: "Comma-separated tool names. Leave blank to allow all tools the server exposes.",
                    input: true,
                    placeholder: "search_web, fetch_url, ...",
                    defaultValue: (current || []).join(", "),
                    confirmText: "Save",
                });
                if (val === null) return;
                const list = val.split(",").map((t) => t.trim()).filter(Boolean);
                onSave(list);
            }
            function renderMcpList() {
                const list = $("mcp-list");
                if (!list) return;
                list.innerHTML = "";
                if (!MCPS.length) {
                    list.innerHTML = '<div style="color:var(--dim);font-size:var(--text-sm)">No MCP servers found in ~/.lmstudio/mcp.json</div>';
                    return;
                }
                MCPS.forEach((s, i) => {
                    const d = document.createElement("div");
                    d.className = "mcp-i";
                    const toolCount = (s.allowed_tools && s.allowed_tools.length) || 0;
                    const toolBadge = toolCount
                        ? `<span style="color:var(--accent);font-size:0.625rem;margin-left:var(--sp-3)" title="${toolCount} allowed tool${toolCount === 1 ? "" : "s"}">&#9881; ${toolCount}</span>`
                        : "";
                    d.innerHTML =
                        `<input type=checkbox ${s.on ? "checked" : ""}>` +
                        `<span>${esc(s.name)}</span>${toolBadge}` +
                        `<button data-action="edit-tools" data-idx="${i}" style="margin-left:auto;background:none;border:none;color:var(--dim);cursor:pointer;font-size:var(--text-xs);padding:0 var(--sp-2)" title="Allowed tools">tools</button>`;
                    d.querySelector("input").onchange = (e) => {
                        MCPS[i].on = e.target.checked;
                        localStorage.setItem(
                            "lsc-mcp-" + s.id,
                            e.target.checked ? "1" : "0",
                        );
                        broadcastMcps();
                    };
                    d.querySelector('[data-action="edit-tools"]').addEventListener("click", () => {
                        editAllowedTools(s.name, MCPS[i].allowed_tools, (list) => {
                            MCPS[i].allowed_tools = list;
                            _writeAllowedTools(s.id, list);
                            broadcastMcps();
                            renderMcpList();
                        });
                    });
                    list.appendChild(d);
                });
            }
            // Fetch available MCP servers from the server (reads ~/.lmstudio/mcp.json)
            apiFetch("/api/mcp/servers").then(async (r) => {
                if (!r.ok) return;
                const d = await r.json();
                // Mutate the existing array in place so the MCPS alias to
                // state.mcps survives, then setState to broadcast.
                MCPS.length = 0;
                (d.servers || []).forEach((s) => MCPS.push({
                    ...s,
                    on: localStorage.getItem("lsc-mcp-" + s.id) !== "0",
                    allowed_tools: _readAllowedTools(s.id),
                }));
                broadcastMcps();
                renderMcpList();
            }).catch((e) => console.error("mcp servers GET failed:", e));
            renderMcpList();

            // --- Remote MCP servers (H4: auth stored server-side) ---
            // Aliased to state.remoteMcps — see MCPS alias comment above.
            // Mutations go through ``broadcastRemoteMcps()`` to fire
            // subscribers.  Phase 2+ subscribed renders use state.remoteMcps.
            let remoteMcps = state.remoteMcps;
            function saveRemoteMcps() {
                // Save to server — send label/url/on/allowed_tools but
                // NOT auth (auth only sent when explicitly set via the key
                // dialog so a stale-state refresh can't unset a stored
                // token).
                const payload = remoteMcps.map((s) => ({
                    label: s.label,
                    url: s.url,
                    on: s.on,
                    allowed_tools: s.allowed_tools || [],
                }));
                apiFetch("/api/auth/settings", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ remote_mcps: payload }),
                }).catch((e) => console.error("remote_mcps toggle save failed:", e));
            }
            function renderRemoteMcps() {
                const list = $("remote-mcp-list");
                if (!list) return;
                list.innerHTML = "";
                remoteMcps.forEach((s, i) => {
                    const d = document.createElement("div");
                    d.className = "mcp-i";
                    const authBadge =
                        s.has_auth ?
                            '<span style="color:var(--accent);font-size:0.625rem;margin-left:var(--sp-3)" title="Auth token configured">&#128274;</span>'
                        :   "";
                    const toolCount = (s.allowed_tools && s.allowed_tools.length) || 0;
                    const toolBadge = toolCount
                        ? `<span style="color:var(--accent);font-size:0.625rem;margin-left:var(--sp-3)" title="${toolCount} allowed tool${toolCount === 1 ? "" : "s"}">&#9881; ${toolCount}</span>`
                        : "";
                    d.innerHTML = `<input type=checkbox ${s.on ? "checked" : ""}><span>${esc(s.label)}</span>${authBadge}${toolBadge}<span style="color:var(--faint);font-size:0.6875rem;margin-left:auto">${esc(s.url)}</span><button data-action="edit-remote-tools" data-idx="${i}" style="background:none;border:none;color:var(--dim);cursor:pointer;font-size:var(--text-xs);padding:0 var(--sp-2)" title="Allowed tools">tools</button><button data-action="set-mcp-auth" data-idx="${i}" style="background:none;border:none;color:var(--dim);cursor:pointer;font-size:var(--text-xs);padding:0 var(--sp-2)" title="Set auth token">&#128273;</button><button data-action="remove-remote-mcp" data-idx="${i}" style="background:none;border:none;color:var(--dim);cursor:pointer;font-size:var(--text-lg);padding:0 var(--sp-2)" title="Remove">&times;</button>`;
                    d.querySelector('[data-action="set-mcp-auth"]').addEventListener('click', () => setMcpAuth(i));
                    d.querySelector('[data-action="remove-remote-mcp"]').addEventListener('click', () => removeRemoteMcp(i));
                    d.querySelector('[data-action="edit-remote-tools"]').addEventListener('click', () => {
                        editAllowedTools(s.label, remoteMcps[i].allowed_tools, (list) => {
                            remoteMcps[i].allowed_tools = list;
                            broadcastRemoteMcps();
                            saveRemoteMcps();
                            renderRemoteMcps();
                        });
                    });
                    d.querySelector("input").onchange = (e) => {
                        remoteMcps[i].on = e.target.checked;
                        broadcastRemoteMcps();
                        saveRemoteMcps();
                    };
                    list.appendChild(d);
                });
            }
            async function addRemoteMcp() {
                const vals = await showDialog({
                    title: "Add Remote MCP Server",
                    fields: [
                        {
                            label: "Label",
                            placeholder: "e.g. my-tools",
                            required: true,
                        },
                        {
                            label: "Server URL",
                            placeholder: "e.g. https://my-server.com/mcp",
                            required: true,
                        },
                        {
                            label: "Authorization header (optional)",
                            placeholder: "Bearer ...",
                        },
                    ],
                    confirmText: "Add",
                });
                if (!vals || !vals[0] || !vals[1]) return;
                const [label, url, auth] = vals;
                remoteMcps.push({ label, url, on: true, has_auth: !!auth, allowed_tools: [] });
                broadcastRemoteMcps();
                const payload = remoteMcps.map((s) => ({
                    label: s.label,
                    url: s.url,
                    on: s.on,
                    allowed_tools: s.allowed_tools || [],
                }));
                payload[payload.length - 1].auth = auth || "";
                apiFetch("/api/auth/settings", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ remote_mcps: payload }),
                })
                    .then(async (r) => {
                        if (r.ok) {
                            const d = await r.json();
                            if (d.remote_mcps) {
                                remoteMcps.length = 0;
                                d.remote_mcps.forEach((m) => remoteMcps.push(m));
                                broadcastRemoteMcps();
                            }
                            renderRemoteMcps();
                        }
                    })
                    .catch((e) => console.error("remote_mcps add/edit save failed:", e));
                renderRemoteMcps();
            }
            async function setMcpAuth(i) {
                const s = remoteMcps[i];
                const auth = await showDialog({
                    title: "Set Auth Token",
                    message:
                        'Set auth token for "' +
                        s.label +
                        '" (leave empty to clear):',
                    input: true,
                    placeholder: "Bearer ...",
                    confirmText: "Save",
                });
                if (auth === null) return;
                // Send full list with auth only for the changed entry
                const payload = remoteMcps.map((m, j) => ({
                    label: m.label,
                    url: m.url,
                    on: m.on,
                    allowed_tools: m.allowed_tools || [],
                    ...(j === i ? { auth } : {}),
                }));
                apiFetch("/api/auth/settings", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ remote_mcps: payload }),
                })
                    .then(async (r) => {
                        if (r.ok) {
                            const d = await r.json();
                            if (d.remote_mcps) {
                                remoteMcps.length = 0;
                                d.remote_mcps.forEach((m) => remoteMcps.push(m));
                                broadcastRemoteMcps();
                            }
                            renderRemoteMcps();
                        }
                    })
                    .catch((e) => console.error("remote_mcps auth save failed:", e));
            }
            function removeRemoteMcp(i) {
                remoteMcps.splice(i, 1);
                broadcastRemoteMcps();
                saveRemoteMcps();
                renderRemoteMcps();
            }
            renderRemoteMcps();

            // --- Chat Search ---
            function filterChats() {
                const q = $("chat-search").value.toLowerCase();
                $("chat-search-clear").style.display = q ? "block" : "none";
                const items = $("chat-list").children;
                let visible = 0;
                for (const item of items) {
                    if (item.classList.contains("sidebar-section")) {
                        item.style.display = q ? "none" : "";
                        continue;
                    }
                    const title =
                        item
                            .querySelector("span")
                            ?.textContent?.toLowerCase() || "";
                    const show = !q || title.includes(q);
                    item.style.display = show ? "" : "none";
                    if (show) visible++;
                }
                $("chat-no-match").style.display =
                    q && !visible ? "block" : "none";
            }
            let searchMode = "text";

            $("chat-search").addEventListener("keydown", async (e) => {
                if (e.key === "Enter" && searchMode === "semantic") {
                    e.preventDefault();
                    const q = $("chat-search").value.trim();
                    if (!q) return;
                    try {
                        const r = await apiFetch("/api/search", {
                            method: "POST",
                            headers: { "Content-Type": "application/json" },
                            body: JSON.stringify({ query: q }),
                        });
                        const d = await r.json();
                        if (d.mode === "unavailable") {
                            searchMode = "text";
                            filterChats();
                            return;
                        }
                        if (d.mode === "text_fallback") {
                            // Semantic degraded to LIKE because LM Studio
                            // doesn't have an embedding model loaded (or
                            // the embedding endpoint failed).  Tell the
                            // user once so they don't think LIKE results
                            // are semantic ones.
                            if (!window._semanticDegradedToastShown) {
                                window._semanticDegradedToastShown = true;
                                if (typeof toast?.warn === "function") {
                                    toast.warn("Semantic search unavailable", {
                                        detail: d.reason || "load an embedding model in LM Studio (e.g. nomic-embed-text-v1.5)",
                                    });
                                } else {
                                    console.warn("Semantic search degraded:", d.reason);
                                }
                            }
                        }
                        renderSearchResults(d.results || []);
                    } catch (e) {
                        filterChats();
                    }
                }
            });

            function renderSearchResults(results) {
                const list = $("chat-list");
                list.innerHTML = "";
                if (!results.length) {
                    $("chat-no-match").style.display = "block";
                    $("chat-no-match").textContent = "No semantic matches";
                    return;
                }
                $("chat-no-match").style.display = "none";
                results.forEach((r) => {
                    const d = document.createElement("div");
                    d.className = "sr";
                    d.innerHTML = `<div class="sr-title">${esc(r.chat_title || "Untitled")}<span class="sr-score">${(r.score * 100).toFixed(0)}%</span></div><div class="sr-text">${esc(r.content)}</div>`;
                    d.onclick = () => {
                        loadChat(r.chat_id);
                        clearChatSearch();
                    };
                    list.appendChild(d);
                });
            }

            function toggleSearchMode() {
                searchMode = searchMode === "text" ? "semantic" : "text";
                $("search-mode").textContent =
                    searchMode === "semantic" ?
                        "Semantic search (Enter to search)"
                    :   "Text search (Enter for semantic)";
                if (searchMode === "text") filterChats();
            }

            function clearChatSearch() {
                $("chat-search").value = "";
                searchMode = "text";
                $("search-mode").textContent =
                    "Text search (Enter for semantic)";
                filterChats();
                $("chat-search").focus();
            }

            // --- Folder context menu ---
            $("chat-list").addEventListener("contextmenu", async (e) => {
                const ci = e.target.closest(".ci");
                if (!ci) return;
                e.preventDefault();
                const id = ci.dataset.id;
                const current = (chatMeta[id] || {}).folder || "";
                const name = await showDialog({
                    title: "Set Folder",
                    input: true,
                    placeholder: "Folder name (empty to remove)",
                    inputValue: current,
                    confirmText: "Save",
                });
                if (name === null) return;
                apiFetch("/api/chats/" + id + "/folder", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ folder: name }),
                })
                    .then((r) => r.json())
                    .then((data) => {
                        if (chatMeta[id]) chatMeta[id].folder = data.folder;
                        broadcastChats();
                        renderList();
                    });
            });

            // --- Sidebar ---
            function openSB() {
                ignoreScrollEvent = true;
                document.body.classList.remove("sb-closed");
                if (window.innerWidth <= 768)
                    $("sb-overlay").style.display = "block";
                setTimeout(() => {
                    ignoreScrollEvent = false;
                }, 100);
            }
            function closeSB() {
                ignoreScrollEvent = true;
                document.body.classList.add("sb-closed");
                $("sb-overlay").style.display = "none";
                setTimeout(() => {
                    ignoreScrollEvent = false;
                }, 100);
            }
            window.addEventListener("resize", () => {
                if (window.innerWidth > 768)
                    $("sb-overlay").style.display = "none";
            });

            // --- Chat list (from server) ---
            // Broadcast helper: ``state.chats`` is the array form; legacy
            // ``chatMeta`` is the keyed-by-id object that most call sites
            // still read.  Call this after any chatMeta mutation that
            // changes membership (add, remove, rename, pin, etc.).
            function broadcastChats() {
                setState({ chats: Object.values(chatMeta) });
            }

            async function loadChatList() {
                return withBusy("sidebar-chats", async () => {
                    try {
                        const prevIds = Object.keys(chatMeta);
                        const resp = await apiFetch("/api/chats");
                        if (!resp.ok) throw new Error("Failed to load chats");
                        const chats = await resp.json();
                        _crumb("chatMeta.reset", {
                            reason: "loadChatList",
                            prevCount: prevIds.length,
                            newCount: chats.length,
                            activeId,
                            activeStillPresent: !!chats.find(c => c.id === activeId),
                        });
                        chatMeta = {};
                        chats.forEach((c) => (chatMeta[c.id] = c));
                        broadcastChats();
                        renderList();
                    } catch (e) {
                        console.error("loadChatList:", e);
                        const el = $("chat-list");
                        el.innerHTML =
                            '<div style="padding:var(--sp-7);color:var(--err-text);font-size:var(--text-sm)">Failed to load chats. <button class="retry-btn" style="color:var(--accent);background:none;border:none;cursor:pointer;text-decoration:underline">Retry</button></div>';
                        el.querySelector(".retry-btn")?.addEventListener(
                            "click",
                            () => loadChatList(),
                        );
                    }
                });
            }

            function renderList() {
                const list = $("chat-list");
                list.innerHTML = "";
                const chats = Object.values(chatMeta)
                    .filter((c) => !c._incognito)
                    .sort((a, b) => {
                        if ((a.pinned || 0) !== (b.pinned || 0))
                            return (b.pinned || 0) - (a.pinned || 0);
                        return (b.updated_at || 0) - (a.updated_at || 0);
                    });
                const pinned = chats.filter((c) => c.pinned);
                const folders = {};
                const unfiled = [];
                chats
                    .filter((c) => !c.pinned)
                    .forEach((c) => {
                        if (c.folder)
                            (folders[c.folder] = folders[c.folder] || []).push(
                                c,
                            );
                        else unfiled.push(c);
                    });
                function addItem(c) {
                    const d = document.createElement("div");
                    d.className = "ci" + (c.id === activeId ? " active" : "");
                    d.dataset.id = c.id;
                    const pinCls = c.pinned ? "pin pinned" : "pin";
                    const pinIcon = c.pinned ? "\u2605" : "\u2606";
                    d.innerHTML = `<span>${esc(c.title)}</span><button class="${pinCls}" title="${c.pinned ? "Unpin" : "Pin"}">${pinIcon}</button><button class="del">&times;</button>`;
                    d.querySelector(".pin").onclick = (e) => {
                        e.stopPropagation();
                        togglePin(c.id);
                    };
                    d.querySelector(".del").onclick = (e) => {
                        e.stopPropagation();
                        deleteChat(c.id);
                    };
                    d.setAttribute("role", "button");
                    d.setAttribute("tabindex", "0");
                    d.onclick = () => loadChat(c.id);
                    d.addEventListener("keydown", (e) => {
                        if (e.key === "Enter" || e.key === " ") {
                            e.preventDefault();
                            loadChat(c.id);
                        }
                    });
                    list.appendChild(d);
                }
                if (pinned.length) {
                    const h = document.createElement("div");
                    h.className = "sidebar-section";
                    h.textContent = "Pinned";
                    list.appendChild(h);
                    pinned.forEach(addItem);
                }
                Object.keys(folders)
                    .sort()
                    .forEach((name) => {
                        const collapsed =
                            localStorage.getItem("lsc-folder-" + name) === "1";
                        const h = document.createElement("div");
                        h.className = "sidebar-section folder-hdr";
                        h.setAttribute("role", "button");
                        h.setAttribute("tabindex", "0");
                        h.innerHTML =
                            '<span class="folder-name">' +
                            esc(name) +
                            "</span>" +
                            '<span class="folder-arrow">' +
                            (collapsed ? "\u25B8" : "\u25BE") +
                            "</span>";
                        h.onclick = () => toggleFolder(name);
                        h.onkeydown = (e) => {
                            if (e.key === "Enter" || e.key === " ") {
                                e.preventDefault();
                                toggleFolder(name);
                            }
                        };
                        list.appendChild(h);
                        if (!collapsed) folders[name].forEach(addItem);
                    });
                if (unfiled.length) {
                    if (pinned.length || Object.keys(folders).length) {
                        const h = document.createElement("div");
                        h.className = "sidebar-section";
                        h.textContent = "Recent";
                        list.appendChild(h);
                    }
                    unfiled.forEach(addItem);
                }
                if (!chats.length) {
                    const empty = document.createElement("div");
                    empty.style.cssText =
                        "padding:1.5rem var(--sp-6);text-align:center;font-size:var(--text-sm);color:var(--faint)";
                    empty.textContent = "No conversations yet";
                    list.appendChild(empty);
                }
                filterChats();
            }

            async function togglePin(id) {
                const res = await apiFetch("/api/chats/" + id + "/pin", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                });
                const data = await res.json();
                chatMeta[id].pinned = data.pinned;
                broadcastChats();
                renderList();
            }

            function toggleFolder(name) {
                const key = "lsc-folder-" + name;
                localStorage.setItem(
                    key,
                    localStorage.getItem(key) === "1" ? "0" : "1",
                );
                renderList();
            }

            // --- Memory: distillation ---
            const _distilling = new Set();

            async function distillChat(chatId) {
                if (
                    !chatId ||
                    _distilling.has(chatId) ||
                    incognitoMode ||
                    (chatId && chatId.startsWith("incog_"))
                )
                    return;
                const meta = chatMeta[chatId];
                if (!meta) return;
                _distilling.add(chatId);
                try {
                    const resp = await apiFetch("/api/insights/distill", {
                        method: "POST",
                        headers: { "Content-Type": "application/json" },
                        body: JSON.stringify({
                            chat_id: chatId,
                            model: modelSel.value,
                        }),
                    });
                    if (!resp.ok) return;
                    const data = await resp.json();
                    if (data.insights && data.insights.length > 0) {
                        showMemoryBadge(data.insights.length);
                    }
                } catch (e) {
                    console.error("distill:", e);
                } finally {
                    _distilling.delete(chatId);
                }
            }

            let _badgeTimer = null;
            function showMemoryBadge(count) {
                let badge = $("memory-badge");
                if (!badge) {
                    badge = document.createElement("div");
                    badge.id = "memory-badge";
                    badge.onclick = () => {
                        openSettings("memory");
                        badge.remove();
                    };
                    document.querySelector(".topbar-r").prepend(badge);
                }
                badge.textContent =
                    count + " new insight" + (count !== 1 ? "s" : "");
                badge.classList.add("visible");
                if (_badgeTimer) clearTimeout(_badgeTimer);
                _badgeTimer = setTimeout(() => {
                    if (badge) badge.remove();
                    _badgeTimer = null;
                }, 8000);
            }

            // --- Memory panel ---
            async function loadMemoryPanel() {
                const list = $("memory-list");
                if (!list) return;
                try {
                    const resp = await apiFetch("/api/insights");
                    if (!resp.ok) return;
                    const insights = await resp.json();
                    if (!insights.length) {
                        list.innerHTML =
                            '<div style="padding:var(--sp-6);color:var(--faint);font-size:var(--text-xs);text-align:center">No memories yet. Chat a bit and switch chats to start building memory.</div>';
                        return;
                    }
                    list.innerHTML = "";
                    function renderInsight(i, container, faded) {
                        const d = document.createElement("div");
                        d.className = "mem-item";
                        if (faded) d.style.opacity = ".5";
                        const cat = document.createElement("span");
                        cat.className = "mem-cat " + i.category;
                        cat.textContent = i.category;
                        const txt = document.createElement("span");
                        txt.className = "mem-text";
                        txt.textContent = i.content;
                        const del = document.createElement("button");
                        del.className = "mem-del";
                        del.title = "Delete";
                        del.textContent = "\u00d7";
                        del.addEventListener("click", () =>
                            deleteInsight(i.id),
                        );
                        d.append(cat, txt, del);
                        container.appendChild(d);
                    }
                    insights
                        .filter((i) => i.state === "active")
                        .forEach((i) => renderInsight(i, list, false));
                    const faded = insights.filter((i) => i.state === "faded");
                    if (faded.length) {
                        const hdr = document.createElement("div");
                        hdr.style.cssText =
                            "font-size:0.625rem;color:var(--faint);padding:var(--sp-4) var(--sp-5) var(--sp-2);text-transform:uppercase;letter-spacing:.5px";
                        hdr.textContent = `Faded (${faded.length})`;
                        list.appendChild(hdr);
                        faded.forEach((i) => renderInsight(i, list, true));
                    }
                } catch (e) {
                    list.innerHTML =
                        '<div style="color:var(--err-text);font-size:var(--text-xs)">Failed to load</div>';
                }
            }

            async function deleteInsight(id) {
                try {
                    const resp = await apiFetch(
                        `/api/insights/${encodeURIComponent(id)}`,
                        { method: "DELETE" },
                    );
                    if (!resp.ok) {
                        addErr("Failed to delete memory.");
                        return;
                    }
                } catch (e) {
                    addErr("Failed to delete memory.");
                    return;
                }
                loadMemoryPanel();
            }

            async function clearInsights() {
                if (
                    !(await showDialog({
                        title: "Clear Memories",
                        message: "Delete all memories? This cannot be undone.",
                        confirmText: "Delete All",
                        danger: true,
                    }))
                )
                    return;
                try {
                    const resp = await apiFetch("/api/insights", {
                        method: "DELETE",
                    });
                    if (!resp.ok) {
                        addErr("Failed to clear memories.");
                        return;
                    }
                } catch (e) {
                    addErr("Failed to clear memories.");
                    return;
                }
                loadMemoryPanel();
            }

            async function addInsightPrompt() {
                const text = await showDialog({
                    title: "Add Memory",
                    input: true,
                    placeholder: 'e.g., "My name is Alex"',
                    confirmText: "Add",
                });
                if (!text) return;
                try {
                    const resp = await apiFetch("/api/insights", {
                        method: "POST",
                        headers: { "Content-Type": "application/json" },
                        body: JSON.stringify({ content: text }),
                    });
                    if (!resp.ok) {
                        addErr("Failed to add memory.");
                        return;
                    }
                } catch (e) {
                    addErr("Failed to add memory.");
                    return;
                }
                loadMemoryPanel();
            }

            async function refineInsights(btn) {
                btn.disabled = true;
                btn.textContent = "Refining...";
                try {
                    const resp = await apiFetch("/api/insights/refine", {
                        method: "POST",
                        headers: { "Content-Type": "application/json" },
                        body: JSON.stringify({ model: modelSel.value }),
                    });
                    if (!resp.ok) {
                        const err = await resp.json().catch(() => ({}));
                        console.error(
                            "refine error:",
                            err.error || resp.status,
                        );
                    }
                    loadMemoryPanel();
                } catch (e) {
                    console.error("refine:", e);
                }
                btn.disabled = false;
                btn.textContent = "Refine";
            }

            async function toggleMemory(enabled) {
                try {
                    const resp = await apiFetch("/api/insights/settings", {
                        method: "PATCH",
                        headers: { "Content-Type": "application/json" },
                        body: JSON.stringify({
                            memory_enabled: enabled ? "true" : "false",
                        }),
                    });
                    if (!resp.ok) throw new Error(resp.status);
                } catch (e) {
                    console.error("toggle memory failed:", e);
                    const cb = $("s-memory");
                    if (cb) cb.checked = !enabled;
                }
            }

            // --- Incognito mode ---
            function toggleIncognito() {
                if (!incognitoMode) {
                    // Enter incognito: start a fresh ephemeral chat
                    if (activeId && !activeId.startsWith("incog_"))
                        distillChat(activeId);
                    incognitoMode = true;
                    incognitoHistory = [];
                    document.body.classList.add("incognito");
                    $("incognito-btn").classList.add("active");
                    activeId = "incog_" + Date.now().toString(36);
                    setState({ activeChatId: activeId });
                    chatMeta[activeId] = {
                        id: activeId,
                        title: "Incognito",
                        model: modelSel.value,
                        response_id: null,
                        updated_at: Date.now() / 1000,
                        pinned: 0,
                        folder: "",
                        _incognito: true,
                    };
                    broadcastChats();
                    msgs.innerHTML = "";
                    renderList();
                    updateExportBtn();
                    $("share-btn").classList.add("hidden");
                    input.focus();
                } else {
                    exitIncognito();
                    activeId = null;
                    setState({ activeChatId: null });
                    renderWelcome();
                    renderList();
                    updateExportBtn();
                    input.focus();
                }
            }
            function exitIncognito() {
                const oldId = activeId;
                _crumb("exitIncognito", { oldId, willDelete: !!(oldId && chatMeta[oldId] && chatMeta[oldId]._incognito) });
                incognitoMode = false;
                incognitoHistory = [];
                document.body.classList.remove("incognito");
                $("incognito-btn").classList.remove("active");
                if (oldId && chatMeta[oldId] && chatMeta[oldId]._incognito) {
                    _crumb("chatMeta.delete", { id: oldId, reason: "exitIncognito" });
                    delete chatMeta[oldId];
                    broadcastChats();
                }
            }

            // --- Chat CRUD ---
            async function newChat() {
                if (settingsOpen) closeSettings();
                // Exit incognito if active
                if (incognitoMode) {
                    toggleIncognito();
                    return;
                }
                // Distill insights from the chat we're leaving
                if (activeId) distillChat(activeId);
                const resp = await apiFetch("/api/chats", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({
                        title: "New chat",
                        model: modelSel.value,
                    }),
                });
                if (!resp.ok) {
                    const err = await resp.json().catch(() => ({}));
                    throw new Error(err.error || "Failed to create chat");
                }
                const data = await resp.json();
                const id = data.id;
                chatMeta[id] = {
                    id,
                    title: "New chat",
                    model: modelSel.value,
                    response_id: null,
                    updated_at: Date.now() / 1000,
                    pinned: 0,
                    folder: "",
                };
                activeId = id;
                setState({ activeChatId: id });
                // Fresh chat has no _activePreset; re-derive the cmd-badge
                // visibility so the previous chat's mode chip doesn't bleed
                // into the new one's input cluster.  (Test:
                // test_agent_mode_resets_badge_on_new_chat.)
                if (typeof updateCmdBadge === "function") updateCmdBadge();
                broadcastChats();
                renderList();
                renderWelcome();
                resetCtxGauge();
                if (window.innerWidth <= 768) closeSB();
                updateExportBtn();
                input.focus();
            }

            async function loadChat(id) {
                const prevId = activeId;
                const wasStreaming = sending;
                _crumb("loadChat.enter", {
                    id, prevId, wasStreaming,
                    incognito: incognitoMode,
                    inChatMeta: !!chatMeta[id],
                });
                if (sending) stopStream();
                // Close settings if open
                if (settingsOpen) closeSettings();
                // Exit incognito if active
                if (incognitoMode) exitIncognito();
                // Distill insights from the chat we're leaving
                if (activeId && activeId !== id) distillChat(activeId);
                activeId = id;
                setState({ activeChatId: id });
                // Switching chats refreshes the cmd-badge from the new
                // chat's meta._activePreset (or hides it).  Keeps the
                // mode chip honest across loadChat transitions.
                if (typeof updateCmdBadge === "function") updateCmdBadge();
                const meta = chatMeta[id];
                if (
                    meta?.model &&
                    [...modelSel.options].some((o) => o.value === meta.model)
                )
                    modelSel.value = meta.model;
                renderList();
                resetCtxGauge();
                try {
                    const resp = await apiFetch(`/api/chats/${id}/messages`);
                    if (!resp.ok) throw new Error("Failed to load chat");
                    const messages = await resp.json();
                    renderMessages(messages);
                    // Estimate context usage for the gauge (use real token_count when available, heuristic fallback)
                    if (messages.length) {
                        let totalTokens = 0;
                        for (const m of messages) {
                            if (m.token_count) {
                                totalTokens += m.token_count;
                            } else {
                                const text =
                                    (m.content || "") +
                                    (typeof m.output === "string" ?
                                        m.output
                                    :   "");
                                if (text) {
                                    totalTokens += estimateTokens(text);
                                }
                            }
                        }
                        if (totalTokens > 0)
                            updateCtxGauge(
                                totalTokens,
                                (cachedModels.find((x) => x.id === modelSel.value)?.context_length) || 16000,
                            );
                    }
                } catch (e) {
                    console.error("loadChat:", e);
                    msgs.innerHTML =
                        '<div style="padding:var(--sp-10) 1.5rem;text-align:center;color:var(--err-text);font-size:var(--text-base)">Failed to load chat. <button class="retry-btn" style="color:var(--accent);background:none;border:none;cursor:pointer;text-decoration:underline;font-size:var(--text-base)">Retry</button></div>';
                    msgs.querySelector(".retry-btn")?.addEventListener(
                        "click",
                        () => loadChat(id),
                    );
                }
                if (window.innerWidth <= 768) closeSB();
                // If we aborted a stream, the previous chat's response_id may have been
                // persisted server-side but never reached the client via chat.end.
                // Refresh after a short delay to give the server time to persist.
                if (wasStreaming && prevId && prevId !== id && chatMeta[prevId]) {
                    setTimeout(async () => {
                        try {
                            const listResp = await apiFetch("/api/chats");
                            if (listResp.ok) {
                                const chats = await listResp.json();
                                const prev = chats.find(c => c.id === prevId);
                                if (prev && chatMeta[prevId]) {
                                    chatMeta[prevId].response_id = prev.response_id;
                                }
                            }
                        } catch (e) {
                            console.error("Failed to restore response_id for chat", prevId, e);
                        }
                    }, 1000);
                }
                await loadChatSettings(id);
                await loadPinNavigator(id);
                updateExportBtn();
            }

            async function deleteChat(id) {
                if (
                    !(await showDialog({
                        title: "Delete Chat",
                        message: "Delete this chat?",
                        confirmText: "Delete",
                        danger: true,
                    }))
                )
                    return;
                try {
                    _crumb("api.deleteChat", { id, fromActive: activeId === id });
                    const r = await apiFetch(`/api/chats/${id}`, { method: "DELETE" });
                    if (!r.ok) throw new Error(`${r.status}`);
                } catch (e) {
                    addErr("Failed to delete chat.");
                    return;
                }
                _crumb("chatMeta.delete", { id, reason: "deleteChat" });
                delete chatMeta[id];
                if (activeId === id) {
                    activeId = null;
                    setState({ activeChatId: null });
                    renderWelcome();
                    updateExportBtn();
                }
                broadcastChats();
                renderList();
            }

            async function deleteAll() {
                if (
                    !(await showDialog({
                        title: "Delete All Chats",
                        message: "Delete ALL chats? This cannot be undone.",
                        confirmText: "Delete All",
                        danger: true,
                    }))
                )
                    return;
                try {
                    const results = await Promise.allSettled(
                        Object.keys(chatMeta).map((id) =>
                            apiFetch(`/api/chats/${id}`, { method: "DELETE" }),
                        ),
                    );
                    const failed = results.filter(
                        (r) => r.status === "rejected" || (r.value && !r.value.ok),
                    );
                    if (failed.length) addErr(`${failed.length} chat(s) could not be deleted.`);
                } catch (e) {
                    addErr("Failed to delete chats.");
                    return;
                }
                _crumb("chatMeta.reset", { reason: "deleteAll", prevCount: Object.keys(chatMeta).length });
                chatMeta = {};
                activeId = null;
                setState({ activeChatId: null });
                broadcastChats();
                renderList();
                renderWelcome();
                updateExportBtn();
                closeSettings();
            }

            // --- Render ---
            function renderWelcome() {
                msgs.innerHTML =
                    '<div class="welcome"><img src="/lm-chat-logo.svg" alt="" style="width:3rem;height:3rem;opacity:.45;margin-bottom:var(--sp-7)"><h2>LM Chat</h2><p>Local AI, deeply integrated with LM Studio.<br>MCP tools, context management, and more.</p><div class="w-hint"><span>Type a message or use <kbd>/</kbd> for commands</span><span><kbd>Cmd+N</kbd> new chat &nbsp; <kbd>Cmd+,</kbd> settings</span></div></div>';
                renderStarters();
            }

            function renderStarters() {
                const el = $("starters");
                if (!el) return;
                const starters = getStarters();
                el.innerHTML = starters
                    .map((s, i) => {
                        const svg = STARTER_ICONS[s.icon];
                        return `<button class="starter-card" data-action="use-starter" data-idx="${i}"><span class="starter-icon">${svg || esc(s.icon || "💬")}</span><span class="starter-title">${esc(s.title)}</span></button>`;
                    })
                    .join("");
                el.querySelectorAll('[data-action="use-starter"]').forEach(btn =>
                    btn.addEventListener('click', () => useStarter(parseInt(btn.dataset.idx)))
                );
            }
            function hideStarters() {
                const el = $("starters");
                if (el) el.innerHTML = "";
            }

            function useStarter(i) {
                const s = getStarters()[i];
                if (!s) return;
                input.value = s.prompt;
                input.focus();
                input.selectionStart = input.selectionEnd = s.prompt.length;
                input.dispatchEvent(new Event("input"));
            }

            function renderStarterSettings() {
                const el = $("starters-list");
                if (!el) return;
                const list = getStarters();
                el.innerHTML = list
                    .map(
                        (s, i) =>
                            `<div style="display:flex;gap:var(--sp-3);margin-bottom:var(--sp-2);align-items:center" data-starter-row="${i}"><input data-field="icon" value="${esc(s.icon || "💬")}" style="width:2rem;text-align:center;background:var(--surface);border:1px solid var(--border);border-radius:0.25rem;padding:0.1875rem;font-size:var(--text-base)"><input data-field="title" value="${esc(s.title)}" placeholder="Title" style="width:4.375rem;background:var(--surface);border:1px solid var(--border);border-radius:0.25rem;padding:0.1875rem var(--sp-3);font-size:var(--text-xs);color:var(--text)"><input data-field="prompt" value="${esc(s.prompt)}" placeholder="Prompt text..." style="flex:1;background:var(--surface);border:1px solid var(--border);border-radius:0.25rem;padding:0.1875rem var(--sp-3);font-size:var(--text-xs);color:var(--text)"><button data-action="remove-starter" style="background:none;border:none;color:var(--faint);cursor:pointer;font-size:var(--text-base)">&times;</button></div>`,
                    )
                    .join("");
                el.querySelectorAll('[data-starter-row]').forEach(row => {
                    const idx = parseInt(row.dataset.starterRow);
                    row.querySelectorAll('input[data-field]').forEach(inp =>
                        inp.addEventListener('change', () => updateStarter(idx, inp.dataset.field, inp.value))
                    );
                    row.querySelector('[data-action="remove-starter"]')?.addEventListener('click', () => removeStarter(idx));
                });
            }

            function updateStarter(i, field, value) {
                const list = getStarters();
                list[i][field] = value;
                saveStarters(list);
                renderStarters();
            }
            function addStarter() {
                const list = getStarters();
                list.push({ title: "New", prompt: "", icon: "💬" });
                saveStarters(list);
                renderStarterSettings();
            }
            function removeStarter(i) {
                const list = getStarters();
                list.splice(i, 1);
                saveStarters(list);
                renderStarterSettings();
                renderStarters();
            }
            function resetStarters() {
                localStorage.removeItem("lsc-starters");
                renderStarterSettings();
                renderStarters();
            }

            function renderMessages(list) {
                msgs.innerHTML = "";
                if (!list.length) {
                    renderWelcome();
                    return;
                }
                hideStarters();
                list.forEach((m) => {
                    if (m.role === "user") addUser(m.content, m.id);
                    else if (m.role === "assistant") {
                        let c = m.content || "";
                        // Interrupted streams persist as assistant rows
                        // with status="interrupted" and (possibly) empty
                        // content.  Render with a Retry affordance so the
                        // user can re-send the previous turn rather than
                        // staring at silence.
                        if (m.status === "interrupted") {
                            addInterruptedAsst(c, { msgId: m.id });
                            return;
                        }
                        const tm = c.match(/<think>([\s\S]*?)<\/think>/);
                        if (tm) {
                            c = c
                                .replace(/<think>[\s\S]*?<\/think>/, "")
                                .trim();
                            // Wrap think + response in a group div so they stay together
                            const grp = document.createElement("div");
                            grp.className = "msg-group";
                            getMsgTarget().appendChild(grp);
                            // Add think block inside group
                            const uid =
                                "th" + Math.random().toString(36).slice(2, 8);
                            const td = document.createElement("div");
                            td.className = "m-think";
                            td.innerHTML = thinkHtml(uid, "Show thinking", esc(tm[1].trim()), false);
                            bindThinkToggle(td);
                            grp.appendChild(td);
                            // Add response inside group
                            if (c) {
                                const { text: cleanC } = extractFollowups(c);
                                const ad = document.createElement("div");
                                ad.className = "m-asst";
                                if (m.id) ad.dataset.msgId = m.id;
                                const bub = document.createElement("div");
                                bub.className = "bub";
                                bub.innerHTML = md(cleanC);
                                if (window.hljs) bub.querySelectorAll('pre code').forEach(b => window.hljs.highlightElement(b));
                                ad.appendChild(bub);
                                ad.appendChild(buildMsgRow({
                                    role: "assistant",
                                    msgId: m.id,
                                    feedback: m.feedback ?? null,
                                    isPinned: false,
                                }));
                                grp.appendChild(ad);
                            }
                        } else if (c) {
                            const { text: cleanC } = extractFollowups(c);
                            addAsst(cleanC, {
                                msgId: m.id,
                                feedback: m.feedback ?? null,
                                isPinned: false,
                            });
                        }
                    } else if (m.role === "tool")
                        addTool(m.name, m.args, m.output);
                    else if (m.role === "error") addErr(m.content);
                });
                addCopyButtons();
                addRegenButton();
                scroll.scrollTop = scroll.scrollHeight;
            }

            function getMsgTarget() {
                const m = chatMeta[activeId];
                return (m && m._subchatTarget && m._subchatTarget.isConnected) ? m._subchatTarget : msgs;
            }
            function addUser(t, msgId, attachments) {
                const d = document.createElement("div");
                d.className = "m-user";
                if (msgId) d.dataset.msgId = msgId;
                d.dataset.text = t;
                const bub = document.createElement("div");
                bub.className = "bub";
                bub.innerHTML = esc(t);
                d.appendChild(bub);
                if (attachments && attachments.length) {
                    const imgBox = document.createElement("div");
                    imgBox.style.cssText =
                        "display:flex;gap:var(--sp-3);margin-top:var(--sp-4);flex-wrap:wrap";
                    attachments
                        .filter((a) => a.type === "image")
                        .forEach((a) => {
                            const img = document.createElement("img");
                            img.src = a.data_url;
                            img.alt = a.name || "image";
                            img.style.cssText =
                                "max-height:7.5rem;border-radius:var(--r-sm);cursor:pointer";
                            img.onclick = () =>
                                window.open(a.data_url, "_blank");
                            imgBox.appendChild(img);
                        });
                    attachments
                        .filter((a) => a.type === "file")
                        .forEach((a) => {
                            const tag = document.createElement("span");
                            tag.style.cssText =
                                "display:inline-flex;align-items:center;gap:var(--sp-2);background:var(--surface);border:1px solid var(--border);border-radius:var(--r-sm);padding:var(--sp-2) var(--sp-4);font-size:var(--text-xs);color:var(--dim)";
                            tag.textContent = a.name;
                            imgBox.appendChild(tag);
                        });
                    if (imgBox.children.length) bub.appendChild(imgBox);
                }
                d.appendChild(buildMsgRow({ role: "user", msgId }));
                getMsgTarget().appendChild(d);
            }
            // Renders an interrupted assistant turn — the stream cut
            // before chat.end, so we have partial text (or none) plus a
            // user-actionable Retry button that re-sends the previous
            // user turn.  Server marks these with status="interrupted".
            function addInterruptedAsst(partialText, opts = {}) {
                const d = document.createElement("div");
                d.className = "m-asst interrupted";
                if (opts.msgId) d.dataset.msgId = opts.msgId;
                const bub = document.createElement("div");
                bub.className = "bub";
                const partial = partialText
                    ? md(partialText)
                    : "<em style='color:var(--dim)'>(no content)</em>";
                bub.innerHTML =
                    partial +
                    '<div style="margin-top:var(--sp-3);padding-top:var(--sp-3);' +
                    'border-top:1px solid var(--border);' +
                    'color:var(--err-text);font-size:var(--text-sm);' +
                    'display:flex;align-items:center;gap:var(--sp-3)">' +
                    '<span>Response interrupted.</span>' +
                    '<button data-action="retry-interrupted" ' +
                    'style="color:var(--accent);background:none;border:none;' +
                    'cursor:pointer;text-decoration:underline;font-size:inherit">' +
                    'Retry' +
                    '</button></div>';
                if (window.hljs) bub.querySelectorAll("pre code").forEach((b) => window.hljs.highlightElement(b));
                d.appendChild(bub);
                bub.querySelector('[data-action="retry-interrupted"]')
                   ?.addEventListener("click", retryLast);
                getMsgTarget().appendChild(d);
            }

            function addAsst(t, opts = {}) {
                // opts: { msgId, feedback, isPinned }
                const d = document.createElement("div");
                d.className = "m-asst";
                if (opts.msgId) d.dataset.msgId = opts.msgId;
                const bub = document.createElement("div");
                bub.className = "bub";
                bub.innerHTML = md(t);
                if (window.hljs) bub.querySelectorAll('pre code').forEach(b => window.hljs.highlightElement(b));
                d.appendChild(bub);
                d.appendChild(buildMsgRow({
                    role: "assistant",
                    msgId: opts.msgId,
                    feedback: opts.feedback,
                    isPinned: opts.isPinned
                }));
                getMsgTarget().appendChild(d);
                return d;
            }
            function toolLabel(raw) {
                if (!raw) return "Used a tool";
                const friendly = {
                    brave_web_search: "Searched the web",
                    brave_local_search: "Searched locally",
                    memory_store: "Stored a memory",
                    memory_retrieve: "Retrieved memories",
                    memory_search: "Searched memories",
                    sequential_thinking: "Reasoned step by step",
                    resolve_library_id: "Looked up docs",
                    get_library_docs: "Retrieved docs",
                    search_papers: "Searched papers",
                    search_arxiv: "Searched arXiv",
                    search_semantic_scholar: "Searched papers",
                    search_google_scholar: "Searched papers",
                };
                if (friendly[raw]) return friendly[raw];
                return "Used " + raw.replace(/_/g, " ");
            }
            function toolPreview(raw) {
                // Extract the interesting bit from tool args for inline display
                if (!raw) return "";
                try {
                    const j = JSON.parse(raw);
                    return (
                        j.query ||
                        j.q ||
                        j.search ||
                        j.text ||
                        j.thought ||
                        j.content ||
                        j.key ||
                        j.input ||
                        ""
                    );
                } catch (e) {
                    return raw.length > 80 ? raw.slice(0, 80) + "…" : raw;
                }
            }
            function addTool(name, args, out) {
                const d = document.createElement("div");
                d.className = "m-tool";
                const uid = "tl" + Math.random().toString(36).slice(2, 8);
                const label = toolLabel(name);
                // ``args`` and ``out`` come from the DB column populated by
                // server.py:_persist_chat_messages, which JSON-encodes the
                // value (with a ``{truncated:true,preview,original_size}``
                // envelope when the original exceeded the persist cap).
                // Decode here so the live-stream render and the history
                // render look identical, and so the truncation envelope
                // doesn't show through as raw JSON.
                const argsStr  = renderStoredTool(args);
                const preview  = toolPreview(argsStr);
                let bodyContent = "";
                if (args)
                    bodyContent += `<div class="t-args">${esc(argsStr)}</div>`;
                if (out) {
                    const o = renderStoredTool(out);
                    if (o)
                        bodyContent += `<div class="t-out">${esc(o.length > 1000 ? o.slice(0, 1000) + "…" : o)}</div>`;
                }
                const hasBody = !!bodyContent;
                d.innerHTML =
                    `<span class="t-name" style="display:none">${esc(name || "tool")}</span><div class="t-toggle" role="button" tabindex="0" ${hasBody ? `data-action="toggle-tool" data-uid="${uid}"` : ""}>` +
                    `<span class="t-arrow"${hasBody ? "" : ' style="visibility:hidden"'}>&#9656;</span> ${esc(label)}${preview ? `<span class="t-preview">${esc(preview)}</span>` : ""}</div>${hasBody ? `<div class="t-body" id="${uid}">${bodyContent}</div>` : ""}`;
                if (hasBody) {
                    const toggle = d.querySelector('[data-action="toggle-tool"]');
                    toggle.addEventListener('click', function() {
                        const b = document.getElementById(this.dataset.uid);
                        const a = this.querySelector('.t-arrow');
                        b.classList.toggle('open');
                        a.classList.toggle('open');
                    });
                    toggle.addEventListener('keydown', function(event) {
                        if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); this.click(); }
                    });
                }
                getMsgTarget().appendChild(d);
            }
            function addThink(t) {
                const uid = "th" + Math.random().toString(36).slice(2, 8);
                const d = document.createElement("div");
                d.className = "m-think";
                d.innerHTML = thinkHtml(uid, "Show thinking", esc(t), false);
                bindThinkToggle(d);
                getMsgTarget().appendChild(d);
            }
            function addErr(t) {
                const d = document.createElement("div");
                d.className = "m-err";
                d.innerHTML = `<div class="bub">${esc(t)}</div>`;
                getMsgTarget().appendChild(d);
            }
            function addErrRetry(t) {
                const d = document.createElement("div");
                d.className = "m-err";
                d.innerHTML = `<div class="bub">${esc(t)} <button data-action="retry-last" style="color:var(--accent);background:none;border:none;cursor:pointer;text-decoration:underline;font-size:inherit;margin-left:var(--sp-3)">Retry</button></div>`;
                d.querySelector('[data-action="retry-last"]').addEventListener('click', retryLast);
                getMsgTarget().appendChild(d);
            }
            async function retryLast() {
                const userEls = [...msgs.querySelectorAll(".m-user")];
                if (!userEls.length) return;
                const lastText = userEls[userEls.length - 1].dataset.text;
                if (!lastText) return;
                // DELETE the last assistant row (including any interrupted
                // stub) + tool calls + user row on the SERVER first, so the
                // stale rows don't accumulate in DB.  resendText re-inserts
                // the user row via /api/chat/stream.  Skip for incognito
                // (no DB rows to clean).
                if (activeId && !incognitoMode && !activeId.startsWith("incog_")) {
                    try {
                        await apiFetch(`/api/chats/${activeId}/messages/last`, { method: "DELETE" });
                    } catch (e) {
                        console.error("Failed to clean last turn before retry:", e);
                        // Continue anyway — DOM clean still happens.  Worst
                        // case the user sees a duplicate user message which
                        // we re-render below.
                    }
                }
                while (
                    msgs.lastChild &&
                    !(msgs.lastChild.className || "").includes("m-user")
                )
                    msgs.lastChild.remove();
                if (
                    msgs.lastChild &&
                    (msgs.lastChild.className || "").includes("m-user")
                )
                    msgs.lastChild.remove();
                if (activeId && chatMeta[activeId])
                    chatMeta[activeId].response_id = null;
                resendText(lastText);
            }

            // --- Send ---
            const sendIcon =
                '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M5 12h14M13 6l6 6-6 6"/></svg>';
            const stopIcon =
                '<svg viewBox="0 0 24 24" fill="currentColor"><rect x="6" y="6" width="12" height="12" rx="2"/></svg>';
            let abortCtrl = null;

            function updateSendBtn() {
                send.disabled =
                    !input.value.trim() &&
                    !pendingAttachments.length &&
                    !sending;
            }
            input.addEventListener("input", () => {
                requestAnimationFrame(() => {
                    input.style.height = "auto";
                    input.style.height =
                        Math.min(input.scrollHeight, 160) + "px";
                });
                updateSendBtn();
                updateSlashMenu();
                // Draft format: ``{preset, text}`` when a command is
                // locked in, plain string otherwise.  Restore site
                // accepts both shapes for backward compatibility with
                // drafts saved before the lock-in pattern landed.
                const draftBlob =
                    pendingPreset ?
                        JSON.stringify({ preset: pendingPreset, text: input.value })
                    :   input.value;
                localStorage.setItem("lsc-draft", draftBlob);
            });
            input.addEventListener("keydown", (e) => {
                if (slashKeyNav(e)) return;
                // Backspace at the start of the input "deletes into" the
                // locked-in command badge — symmetric with how typing
                // ``/research<space>`` locked it in.  Empty-input case is
                // handled too so the user can clear without selecting.
                if (
                    e.key === "Backspace" &&
                    pendingPreset &&
                    input.selectionStart === 0 &&
                    input.selectionEnd === 0
                ) {
                    e.preventDefault();
                    pendingPreset = null;
                    updateCmdBadge();
                    return;
                }
                if (e.key === "Enter" && !e.shiftKey) {
                    e.preventDefault();
                    sendMsg();
                }
            });
            input.addEventListener("blur", (e) => {
                // Don't close the slash menu if focus moved to the slash
                // button, the menu itself, OR back to the input.  On mobile
                // the dedicated #slash-btn click handler refocuses the
                // input AFTER the button's mousedown blurred it; without
                // re-allowing input as a valid focus target, this deferred
                // close race-fired ~200ms later and hid the menu the user
                // had just opened (the "menu flashes" bug).
                setTimeout(() => {
                    const active = document.activeElement;
                    const menu = $("slash-menu");
                    if (active && (active === input || active.id === "slash-btn" || menu.contains(active))) return;
                    menu.classList.remove("open");
                }, 200);
            });
            send.onclick = () => {
                if (sending) {
                    stopStream();
                } else {
                    sendMsg();
                }
            };

            // --- Attachments: drag & drop, paste, file picker ---
            const mainEl = $("main");
            mainEl.addEventListener("dragover", (e) => {
                e.preventDefault();
                mainEl.classList.add("drag-over");
            });
            mainEl.addEventListener("dragleave", (e) => {
                if (!mainEl.contains(e.relatedTarget))
                    mainEl.classList.remove("drag-over");
            });
            mainEl.addEventListener("drop", (e) => {
                e.preventDefault();
                mainEl.classList.remove("drag-over");
                handleFiles(e.dataTransfer.files);
            });

            document.addEventListener("paste", (e) => {
                if (sending) return;
                const items = e.clipboardData?.items;
                if (!items) return;
                const files = [];
                for (const item of items) {
                    if (item.kind === "file") {
                        const f = item.getAsFile();
                        if (f) files.push(f);
                    }
                }
                if (files.length) {
                    e.preventDefault();
                    handleFiles(files);
                }
            });

            function handleFiles(fileList) {
                for (const file of fileList) {
                    if (ALLOWED_IMAGE_TYPES.includes(file.type)) {
                        if (file.size > MAX_IMAGE_SIZE) {
                            addErr(
                                "Image too large: " +
                                    file.name +
                                    " (max 20 MB)",
                            );
                            continue;
                        }
                        const r = new FileReader();
                        r.onload = () => {
                            pendingAttachments.push({
                                type: "image",
                                data_url: r.result,
                                name: file.name,
                            });
                            renderAttachments();
                            updateSendBtn();
                        };
                        r.onerror = () => {
                            addErr("Failed to read image: " + file.name);
                        };
                        r.readAsDataURL(file);
                    } else if (
                        file.type.startsWith("text/") ||
                        /\.(txt|md|csv|json|xml|yaml|yml|py|js|ts|html|css|log|sh|sql|toml|ini|cfg|conf|rb|go|rs|java|c|cpp|h|hpp)$/i.test(
                            file.name,
                        )
                    ) {
                        if (file.size > 1024 * 1024) {
                            addErr(
                                "File too large: " +
                                    file.name +
                                    " (max 1 MB for text)",
                            );
                            continue;
                        }
                        const r = new FileReader();
                        r.onload = () => {
                            pendingAttachments.push({
                                type: "file",
                                content: r.result,
                                name: file.name,
                            });
                            renderAttachments();
                            updateSendBtn();
                        };
                        r.onerror = () => {
                            addErr("Failed to read file: " + file.name);
                        };
                        r.readAsText(file);
                    } else {
                        addErr("Unsupported file type: " + file.name);
                    }
                }
            }

            function renderAttachments() {
                const el = $("attachments");
                el.innerHTML = "";
                pendingAttachments.forEach((att, i) => {
                    const div = document.createElement("div");
                    div.className = "att";
                    if (att.type === "image") {
                        const img = document.createElement("img");
                        img.src = att.data_url;
                        img.alt = att.name;
                        const btn = document.createElement("button");
                        btn.className = "att-x";
                        btn.textContent = "\u00d7";
                        btn.onclick = () => removeAttachment(i);
                        div.appendChild(img);
                        div.appendChild(btn);
                    } else {
                        div.innerHTML =
                            '<div class="att-file"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><path d="M14 2v6h6"/></svg><span>' +
                            esc(att.name) +
                            '</span></div><button class="att-x" data-action="remove-attachment">&times;</button>';
                        div.querySelector('[data-action="remove-attachment"]').addEventListener('click', () => removeAttachment(i));
                    }
                    el.appendChild(div);
                });
            }

            function removeAttachment(i) {
                pendingAttachments.splice(i, 1);
                renderAttachments();
                updateSendBtn();
            }

            const FOLLOWUP_SUFFIX =
                '\n\nAfter your response, suggest 2-3 natural follow-up questions the user might ask. Format them on the LAST line of your response as: <!--followups:["question 1","question 2","question 3"]-->';

            function extractFollowups(text) {
                const match = text.match(
                    /<!--followups:?\s*(\[.*?\])\s*-->\s*$/s,
                );
                if (!match) return { text, followups: [] };
                try {
                    const followups = JSON.parse(match[1]);
                    if (
                        Array.isArray(followups) &&
                        followups.length &&
                        followups.every((q) => typeof q === "string")
                    )
                        return {
                            text: text.slice(0, match.index).trimEnd(),
                            followups,
                        };
                } catch {
                    // Malformed JSON in the assistant's followups block —
                    // the model didn't follow the format.  Fall through to
                    // returning the original text with no followups.
                }
                return { text, followups: [] };
            }

            function renderFollowups(questions, msgEl) {
                document
                    .querySelectorAll(".followups")
                    .forEach((el) => el.remove());
                const container = document.createElement("div");
                container.className = "followups";
                questions.slice(0, 3).forEach((q) => {
                    const btn = document.createElement("button");
                    btn.className = "followup-chip";
                    btn.textContent = q;
                    btn.onclick = () => {
                        input.value = q;
                        container.remove();
                        sendMsg();
                    };
                    container.appendChild(btn);
                });
                msgEl.after(container);
                scroll.scrollTop = scroll.scrollHeight;
            }

            function buildChatBody(text, systemPrompt, attachments) {
                // Plugin (local) MCPs go out as the bare-string shorthand
                // unless the user has set an ``allowed_tools`` whitelist,
                // in which case we emit the documented object form so the
                // whitelist can ride along.  Remote (ephemeral_mcp) MCPs
                // are always objects; allowed_tools is included when set.
                const integrations = [
                    ...MCPS.filter((s) => s.on).map((s) => {
                        const at = (s.allowed_tools || []).filter(Boolean);
                        if (at.length) {
                            return { type: "plugin", id: s.id, allowed_tools: at };
                        }
                        return s.id;
                    }),
                    ...remoteMcps
                        .filter((s) => s.on)
                        .map((s) => {
                            const obj = {
                                type: "ephemeral_mcp",
                                server_label: s.label,
                                server_url: s.url,
                            };
                            const at = (s.allowed_tools || []).filter(Boolean);
                            if (at.length) obj.allowed_tools = at;
                            return obj;
                        }),
                ];
                const meta = chatMeta[activeId] || {};
                const model = modelSel.value || cachedModels[0]?.id || "";
                let inputVal = text;
                if (attachments && attachments.length) {
                    const inputArr = [];
                    const textParts = [];
                    if (text.trim()) textParts.push(text);
                    attachments
                        .filter((a) => a.type === "file")
                        .forEach((a) => {
                            textParts.push(
                                '\n\n<file name="' +
                                    a.name.replace(/"/g, '\\"') +
                                    '">\n' +
                                    a.content +
                                    "\n</file>",
                            );
                        });
                    if (textParts.length)
                        inputArr.push({
                            type: "text",
                            content: textParts.join(""),
                        });
                    attachments
                        .filter((a) => a.type === "image")
                        .forEach((a) => {
                            inputArr.push({ type: "image", data_url: a.data_url });
                        });
                    inputVal = inputArr;
                }
                // In incognito mode, prepend conversation history so the
                // model has context without any server-side state.
                // History is plain text only, held in JS memory, never persisted.
                if (incognitoMode && incognitoHistory.length > 1) {
                    // Build context from all turns except the last (which is
                    // the current user message already in inputVal).
                    // Trim oldest turns to stay within char budget.
                    const prior = incognitoHistory.slice(0, -1);
                    let ctx = "";
                    for (let i = prior.length - 1; i >= 0; i--) {
                        const line = (prior[i].role === "user" ? "User" : "Assistant") + ": " + prior[i].content;
                        if (ctx.length + line.length + 2 > INCOGNITO_HISTORY_MAX_CHARS) break;
                        ctx = line + (ctx ? "\n\n" + ctx : "");
                    }
                    const prefix = "[Conversation history]\n" + ctx + "\n\n[Current message]\n";
                    if (typeof inputVal === "string") {
                        inputVal = prefix + inputVal;
                    } else if (Array.isArray(inputVal)) {
                        // Multimodal input array — prepend history as a text part
                        inputVal.unshift({ type: "text", content: prefix });
                    }
                }
                const body = { model, input: inputVal, integrations };
                if (incognitoMode) {
                    body.incognito = true;
                } else {
                    body.chat_id = activeId;
                }
                const followupsEnabled =
                    localStorage.getItem("lsc-followups") !== "off";
                if (systemPrompt) {
                    body.system_prompt =
                        followupsEnabled ?
                            systemPrompt + FOLLOWUP_SUFFIX
                        :   systemPrompt;
                }
                if (!incognitoMode && meta.response_id && (chatSettingsCache?.store !== false))
                    body.previous_response_id = meta.response_id;
                // Per-chat "Stateless turns" toggle sets store:false so
                // LM Studio doesn't allocate response-store state for this
                // turn.  Server-side incognito sets the same flag; UI
                // setting is an explicit opt-in for non-incognito chats
                // where the user just doesn't want chained context.
                if (chatSettingsCache?.store === false) body.store = false;
                // user_set_params: params the user just explicitly chose via
                // an inline control (currently the reasoning cycle button).
                // The server treats these as a one-shot bypass of the
                // proactive capability blacklist — the user asked for it,
                // let LM Studio decide.  Cleared on every send so a stale
                // override doesn't survive the next turn.
                if (window._userSetParams && window._userSetParams.size) {
                    body.user_set_params = [...window._userSetParams];
                    window._userSetParams.clear();
                }
                // Sampling params
                // Auto-temperature: override based on active preset mode
                const PRESET_TEMPS = {
                    coder: 0.1,
                    creative: 0.9,
                    research: 0.4,
                    analyst: 0.3,
                    architect: 0.2,
                };
                const activePreset = (chatMeta[activeId] || {})._activePreset;
                const cs = chatSettingsCache || {};
                // Per-chat overrides only — no global fallbacks.
                // When no per-chat override, omit the param entirely and let LM Studio
                // use the model's instance_config defaults (temp, top_k, context, etc.).
                const temp =
                    activePreset && PRESET_TEMPS[activePreset] !== undefined ?
                        PRESET_TEMPS[activePreset]
                    : cs.temperature != null ? cs.temperature
                    : null;
                if (temp != null && !isNaN(temp)) body.temperature = temp;
                if (cs.top_p != null && !isNaN(cs.top_p)) body.top_p = cs.top_p;
                if (cs.top_k != null && !isNaN(cs.top_k)) body.top_k = cs.top_k;
                if (cs.min_p != null && !isNaN(cs.min_p)) body.min_p = cs.min_p;
                if (cs.repeat_penalty != null && !isNaN(cs.repeat_penalty))
                    body.repeat_penalty = cs.repeat_penalty;
                if (cs.max_output_tokens != null && !isNaN(cs.max_output_tokens) && cs.max_output_tokens > 0)
                    body.max_output_tokens = cs.max_output_tokens;
                // Reasoning: chat override → user/global default → "off".
                // Single read path via effectiveReasoning() so the cycle
                // button, the chat-settings dropdown, and the global-settings
                // dropdown can never disagree with what actually goes on the
                // wire.  Always sent explicitly — omitting lets LM Studio use
                // the model's default, which may be "on" and burn tool-call
                // budget unnecessarily.
                body.reasoning = effectiveReasoning();
                // context_length is a load-time parameter — sending it per-request
                // triggers JIT model reloads in LM Studio. Read-only from instance config.
                // SC/CoVe: per-chat override, falling back to global setting.
                const globalSc   = localStorage.getItem("lsc-sc") === "1";
                const globalCove = localStorage.getItem("lsc-cove") === "1";
                body.sc_enabled   = cs.sc_enabled   != null ? cs.sc_enabled   : globalSc;
                body.cove_enabled = cs.cove_enabled != null ? cs.cove_enabled : globalCove;
                return body;
            }

            function setSendMode(streaming) {
                if (streaming) {
                    send.innerHTML = stopIcon;
                    send.classList.add("stop");
                    send.disabled = false;
                    send.setAttribute("aria-label", "Stop generation");
                } else {
                    send.innerHTML = sendIcon;
                    send.classList.remove("stop");
                    updateSendBtn();
                    send.setAttribute("aria-label", "Send message");
                }
            }

            function stopStream() {
                if (abortCtrl) {
                    abortCtrl.abort();
                    abortCtrl = null;
                }
            }

            async function sendMsg() {
                if (sending) return;
                const text = input.value.trim();
                if (!text && !pendingAttachments.length) return;
                localStorage.removeItem("lsc-draft");
                document
                    .querySelectorAll(".followups")
                    .forEach((el) => el.remove());
                const sentAttachments =
                    pendingAttachments.length ? [...pendingAttachments] : null;
                pendingAttachments = [];
                renderAttachments();
                sending = true;
                setSendMode(true);

                // Handle slash commands.  If a lock-in is active
                // (``pendingPreset``), synthesize the prefixed form so
                // the rest of the pipeline (parse, meta._activePreset
                // sticky propagation, sub-chat label) works unchanged.
                let effectiveText = text;
                if (pendingPreset) {
                    const lockMatch = SLASH_MENU_ITEMS.find(
                        (s) => s.preset === pendingPreset,
                    );
                    if (lockMatch) {
                        effectiveText = `${lockMatch.cmd} ${text}`.trimEnd();
                    }
                }
                const slashCmd = parseSlashCmd(effectiveText);
                if (slashCmd && slashCmd.type === "help") {
                    cancelSend(sentAttachments);
                    if (!activeId) await newChat();
                    showSlashHelp();
                    return;
                }
                if (slashCmd && slashCmd.type === "compact") {
                    cancelSend(sentAttachments);
                    triggerCompact();
                    return;
                }

                const actualText = slashCmd ? slashCmd.rest : text;
                if (slashCmd && !actualText) {
                    cancelSend();
                    addErr("Please provide a query after the command.");
                    return;
                }
                if (!actualText && !sentAttachments) {
                    cancelSend();
                    return;
                }

                if (!activeId) {
                    if (incognitoMode) {
                        activeId = "incog_" + Date.now().toString(36);
                        setState({ activeChatId: activeId });
                        chatMeta[activeId] = {
                            id: activeId,
                            title: "Incognito",
                            model: modelSel.value,
                            response_id: null,
                            updated_at: Date.now() / 1000,
                            pinned: 0,
                            folder: "",
                            _incognito: true,
                        };
                        broadcastChats();
                    } else {
                        try {
                            await newChat();
                        } catch (e) {
                            cancelSend();
                            addErr("Failed to create chat: " + e.message);
                            return;
                        }
                    }
                }

                const meta = chatMeta[activeId];
                const isFirstMsg =
                    meta.title === "New chat" || meta.title === "Incognito";
                if (isFirstMsg && !incognitoMode) {
                    // Set a temporary short title; auto-title will overwrite it
                    const titleText = actualText || "Image attachment";
                    meta.title =
                        titleText.slice(0, 50) +
                        (titleText.length > 50 ? "..." : "");
                    meta.model = modelSel.value;
                    apiFetch(`/api/chats/${activeId}/title`, {
                        method: "PATCH",
                        headers: { "Content-Type": "application/json" },
                        body: JSON.stringify({ title: meta.title }),
                    });
                    renderList();
                    // Fire background auto-title request
                    autoTitle(activeId, actualText || titleText);
                }

                input.value = "";
                input.style.height = "auto";
                // pendingPreset was a one-shot lock-in for compose-time;
                // sticky-mode now propagates via ``meta._activePreset`` and
                // the badge re-derives from that on the next render.
                pendingPreset = null;

                // Slash commands: sticky until user sends a plain message (no slash prefix)
                if (slashCmd && PRESETS[slashCmd.preset]) {
                    meta._activePreset = slashCmd.preset;
                } else if (!slashCmd) {
                    delete meta._activePreset;
                }
                updateCmdBadge();
                const presetKey = meta._activePreset;

                // Sub-chat breakout room for command modes
                const modeLabel =
                    slashCmd ? slashCmd.label
                    : presetKey && PRESETS[presetKey] ?
                        SLASH_MENU_ITEMS.find((s) => s.preset === presetKey)
                            ?.desc || presetKey
                    :   null;
                // Clear welcome screen and starters if present
                const welcomeEl = msgs.querySelector('.welcome');
                if (welcomeEl) { msgs.innerHTML = ''; hideStarters(); }

                if (modeLabel) {
                    const frame = document.createElement("div");
                    frame.className = "subchat-frame";
                    frame.innerHTML = `<div class="subchat-label">${esc(slashCmd ? modeLabel : "Continuing · " + modeLabel)}</div>`;
                    msgs.appendChild(frame);
                    meta._subchatTarget = frame;
                } else {
                    delete meta._subchatTarget;
                }

                addUser(actualText, null, sentAttachments);
                scroll.scrollTop = scroll.scrollHeight;
                $("thinking").classList.add("on");

                // Track user message for incognito context
                if (incognitoMode) {
                    incognitoHistory.push({ role: "user", content: actualText });
                    if (incognitoHistory.length > INCOGNITO_HISTORY_MAX)
                        incognitoHistory.splice(0, incognitoHistory.length - INCOGNITO_HISTORY_MAX);
                }

                const sys =
                    presetKey && PRESETS[presetKey] ?
                        expandVars(PRESETS[presetKey])
                    : chatSettingsCache?.system_prompt ?
                        expandVars(chatSettingsCache.system_prompt)
                    :   expandVars($("s-sys").value.trim());
                const body = buildChatBody(
                    actualText,
                    sys || undefined,
                    sentAttachments,
                );

                await streamRequest(body, meta);
            }

            async function streamRequest(body, meta) {
                abortCtrl = new AbortController();
                try {
                    let resp = await apiFetch("/api/chat/stream", {
                        method: "POST",
                        headers: { "Content-Type": "application/json" },
                        body: JSON.stringify(body),
                        signal: abortCtrl.signal,
                    });
                    if (!resp.ok) {
                        const err = await resp
                            .json()
                            .catch(() => ({ error: "Stream failed" }));
                        const errObj =
                            (err && typeof err.error === "object" && err.error) ||
                            {};
                        const errMsg =
                            errObj.message ||
                            (typeof err.error === "string" ? err.error : null) ||
                            JSON.stringify(err);
                        // Universal "model rejects this param" detection.
                        // The server normally retries on its own and caches
                        // the verdict for next time — this is the fallback
                        // path for when the server's retry didn't fire
                        // (older server, non-stream path, or a fresh model
                        // whose first request races us).  Prefer the
                        // structured ``error.param`` field; fall back to the
                        // message-substring check for older LM Studio builds.
                        const msgLower = (errMsg || "").toLowerCase();
                        const looksUnsupported =
                            msgLower.includes("does not support") ||
                            msgLower.includes("does not expose") ||
                            msgLower.includes("not configurable");
                        let badParam = typeof errObj.param === "string" ? errObj.param : null;
                        if (!badParam && looksUnsupported) {
                            // Recover the param name from the message when
                            // the server didn't include the structured field.
                            // Add new param names here as they surface.
                            for (const candidate of ["reasoning"]) {
                                if (msgLower.includes(candidate)) {
                                    badParam = candidate;
                                    break;
                                }
                            }
                        }
                        if (badParam && looksUnsupported && badParam in body) {
                            delete body[badParam];
                            resp = await apiFetch("/api/chat/stream", {
                                method: "POST",
                                headers: { "Content-Type": "application/json" },
                                body: JSON.stringify(body),
                                signal: abortCtrl.signal,
                            });
                            if (!resp.ok) {
                                const err2 = await resp
                                    .json()
                                    .catch(() => ({ error: "Stream failed" }));
                                addErr(
                                    err2.error?.message ||
                                        err2.error ||
                                        JSON.stringify(err2),
                                );
                                finishStream(meta);
                                return;
                            }
                            // The server-side cache now knows about this
                            // (model, param) pair — refresh the models list
                            // so the UI control disables for future picks
                            // without waiting for the next page load.
                            refreshModels().catch(() => {});
                        } else {
                            addErr(errMsg);
                            finishStream(meta);
                            return;
                        }
                    }
                    const ct = resp.headers.get("content-type") || "";
                    if (!ct.includes("text/event-stream")) {
                        const data = await resp.json();
                        handleSyncResponse(data, meta);
                        finishStream(meta);
                        return;
                    }
                    await readSSE(resp.body, meta);
                } catch (e) {
                    if (e.name !== "AbortError")
                        addErrRetry("Connection failed: " + e.message);
                }
                finishStream(meta);
            }

            function finishStream(meta) {
                meta.updated_at = Date.now() / 1000;
                sending = false;
                abortCtrl = null;
                if (streamMdTimer) {
                    cancelAnimationFrame(streamMdTimer);
                    streamMdTimer = null;
                }
                setSendMode(false);
                $("thinking").classList.remove("on");
                // Track assistant response for incognito context.
                // If stream produced no content (error/abort), pop the
                // orphaned user turn so it doesn't confuse future context.
                if (incognitoMode) {
                    if (streamState.content) {
                        incognitoHistory.push({ role: "assistant", content: streamState.content });
                        if (incognitoHistory.length > INCOGNITO_HISTORY_MAX)
                            incognitoHistory.splice(0, incognitoHistory.length - INCOGNITO_HISTORY_MAX);
                    } else if (incognitoHistory.length && incognitoHistory[incognitoHistory.length - 1].role === "user") {
                        incognitoHistory.pop();
                    }
                }
                // Final markdown render
                const asstWrap =
                    streamState.bub ? streamState.bub.closest(".m-asst") : null;
                if (streamState.bub) {
                    streamState.bub.classList.remove("streaming");
                    const { text: cleanText, followups } =
                        extractFollowups(streamState.content);
                    if (followups.length) {
                        streamState.content = cleanText;
                        streamState.bub.innerHTML = md(cleanText);
                    } else {
                        streamState.bub.innerHTML = md(streamState.content);
                    }
                    if (window.hljs) streamState.bub.querySelectorAll('pre code').forEach(b => window.hljs.highlightElement(b));
                    streamState.bub = null;
                    streamState.content = "";
                    if (followups.length)
                        renderFollowups(followups, streamState.group || asstWrap);
                }
                // Append bottom row to just-completed response.
                // asstWrap was captured before streamState.bub was nullified — safe to use here.
                if (asstWrap && !asstWrap.querySelector(".msg-row")) {
                    asstWrap.appendChild(buildMsgRow({ role: "assistant" }));
                }
                // Add token stats after the assistant message
                if (streamState.deltaCount > 0) addMsgStats(streamState.group || asstWrap);
                addCopyButtons();
                addRegenButton();
                patchMsgIds();
                userScrolledUp = false;
                hideScrollBtn();
                scroll.scrollTop = scroll.scrollHeight;
                input.focus();
            }

            const streamState = {
                bub: null,
                content: "",
                inThink: false,
                thinkBuf: "",
                thinkBody: null,
                inReasoning: false,
                group: null,
                startTime: 0,
                firstTokenTime: 0,
                deltaCount: 0,
                endStats: null,
                liveStatsEl: null,
                deltaTimestamps: [],
            };
            function resetStreamState() {
                streamState.bub = null;
                streamState.content = "";
                streamState.inThink = false;
                streamState.thinkBuf = "";
                streamState.thinkBody = null;
                streamState.inReasoning = false;
                streamState.group = null;
                streamState.startTime = 0;
                streamState.firstTokenTime = 0;
                streamState.deltaCount = 0;
                streamState.endStats = null;
                streamState.liveStatsEl = null;
                streamState.deltaTimestamps = [];
            }

            async function readSSE(body, meta) {
                const reader = body.getReader();
                const dec = new TextDecoder();
                let buf = "";
                resetStreamState();
                streamState.startTime = performance.now();

                try {
                    while (true) {
                        const { done, value } = await reader.read();
                        if (done) break;
                        buf += dec.decode(value, { stream: true });

                        // Process complete SSE blocks (separated by double newline)
                        let idx;
                        while ((idx = buf.indexOf("\n\n")) !== -1) {
                            const block = buf.slice(0, idx);
                            buf = buf.slice(idx + 2);
                            processSSEBlock(block, meta);
                        }
                    }
                    // Process any remaining data
                    if (buf.trim()) processSSEBlock(buf, meta);
                } catch (e) {
                    if (e.name !== "AbortError") throw e;
                }
            }

            function processSSEBlock(block, meta) {
                let event = "",
                    data = "";
                for (const line of block.split("\n")) {
                    if (line.startsWith("event:")) event = line.slice(6).trim();
                    else if (line.startsWith("data:"))
                        data += (data ? "\n" : "") + line.slice(5).trim();
                }
                if (!event) return;

                let parsed = {};
                if (data) {
                    try {
                        parsed = JSON.parse(data);
                    } catch (e) {
                        // SSE frame with non-JSON payload — event handlers
                        // below check for the fields they need on `parsed`
                        // and tolerate the empty object, so leaving parsed={}
                        // is the right fallback.  Logging would spam the
                        // console on every keep-alive comment.
                    }
                }

                switch (event) {
                    case "chat.start":
                        // No UI change; the request is acknowledged and
                        // model_load / prompt_processing follow.  Recorded
                        // explicitly so the default-case warning doesn't
                        // fire for this documented event.
                        break;
                    case "model_load.start":
                        $("thinking").querySelector("span").textContent =
                            "Loading model...";
                        break;
                    case "model_load.progress": {
                        // LM Studio sends progress as a 0..1 float.
                        const pct = Math.round((parsed.progress || 0) * 100);
                        $("thinking").querySelector("span").textContent =
                            `Loading model ${pct}%`;
                        break;
                    }
                    case "model_load.end":
                        $("thinking").querySelector("span").textContent =
                            "Processing...";
                        break;
                    case "prompt_processing.start":
                        $("thinking").querySelector("span").textContent =
                            "Processing...";
                        break;
                    case "prompt_processing.progress": {
                        const pct = Math.round((parsed.progress || 0) * 100);
                        $("thinking").querySelector("span").textContent =
                            `Processing prompt ${pct}%`;
                        break;
                    }
                    case "prompt_processing.end":
                        $("thinking").querySelector("span").textContent =
                            "Generating...";
                        break;
                    case "message.start":
                        $("thinking").querySelector("span").textContent =
                            "Generating...";
                        break;
                    case "message.end":
                        // Documented per /docs/developer/rest/streaming-events
                        // and observed firing once per content segment in
                        // multi-segment conversations (see /tmp/probe-tool2.txt
                        // — 5 message.end events in a 4-tool conversation).
                        // Treat each one as "this segment is complete, the
                        // model is about to either invoke tools or end the
                        // turn."  Flush the markdown render so the user
                        // sees the final state of THIS segment before any
                        // tool-call pause, and switch the indicator back to
                        // a neutral wait state.
                        if (streamState.bub && streamState.content) {
                            streamState.bub.innerHTML = md(streamState.content);
                        }
                        $("thinking").querySelector("span").textContent =
                            "Thinking...";
                        break;
                    case "reasoning.start":
                    case "reasoning.delta": {
                        if (!streamState.inReasoning) {
                            streamState.inReasoning = true;
                            // Create wrapper group for reasoning + response if not yet created
                            if (!streamState.group) {
                                streamState.group = document.createElement("div");
                                streamState.group.className = "msg-group";
                                getMsgTarget().appendChild(streamState.group);
                            }
                            const uid =
                                "th" + Math.random().toString(36).slice(2, 8);
                            const d = document.createElement("div");
                            d.className = "m-think";
                            d.innerHTML = thinkHtml(uid, "Thinking...", "", true);
                            bindThinkToggle(d);
                            streamState.group.appendChild(d);
                            streamState.thinkBody = d.querySelector(".think-body");
                            streamState.thinkBuf = "";
                        }
                        if (event === "reasoning.delta") {
                            streamState.thinkBuf += parsed.content || "";
                            if (streamState.thinkBody) streamState.thinkBody.textContent = streamState.thinkBuf;
                            autoScroll();
                        }
                        break;
                    }
                    case "reasoning.end":
                        if (streamState.inReasoning) {
                            streamState.inReasoning = false;
                            if (streamState.thinkBody) {
                                streamState.thinkBody.classList.remove("open");
                                streamState.thinkBody.parentElement.previousElementSibling.textContent =
                                    "Show thinking";
                            }
                        }
                        break;
                    case "error": {
                        // Documented error.type enum (live docs 2026-05-15):
                        //   invalid_request | unknown | mcp_connection_error |
                        //   plugin_connection_error | not_implemented |
                        //   model_not_found | job_not_found | internal_error
                        // Branch per type so the user sees an actionable
                        // message instead of "Unknown" + retry for cases
                        // where retry is wrong (job_not_found, not_implemented)
                        // or where the right recovery is to refresh
                        // something (model_not_found -> models list) rather
                        // than re-send the same request.
                        const e   = parsed.error || {};
                        const etype = e.type || "";
                        const emsg  = e.message || etype || "Unknown streaming error";
                        const eparam = e.param ? ` [${e.param}]` : "";
                        switch (etype) {
                            case "model_not_found":
                                addErrRetry(`Model not found${eparam}. Refreshing model list...`);
                                // Force a model-list refresh so the next
                                // retry attempt sees the current set.
                                try { refreshModels && refreshModels(); } catch (_) { /* not loaded yet */ }
                                break;
                            case "job_not_found":
                                // This request is no longer addressable on
                                // the server — retrying with the same
                                // previous_response_id will keep failing.
                                // Show a plain error without a retry button.
                                addErr(`Request invalidated: ${emsg}${eparam}`);
                                if (activeId && chatMeta[activeId]) {
                                    chatMeta[activeId].response_id = null;
                                }
                                break;
                            case "not_implemented":
                                addErr(`Not implemented by this LM Studio: ${emsg}${eparam}`);
                                break;
                            case "mcp_connection_error":
                            case "plugin_connection_error":
                                addErrRetry(
                                    `Integration unreachable: ${emsg}${eparam}. ` +
                                    `Toggle the integration off and retry, or check the MCP server.`,
                                );
                                break;
                            case "invalid_request":
                                // A bug in our payload assembly — log to
                                // console so we notice it in dev, but still
                                // surface a retry option since transient
                                // body issues (e.g. a 0-length input on a
                                // double-Enter) can be fixed by editing
                                // and resending.
                                console.warn("invalid_request from LM Studio:", e);
                                addErrRetry(`Invalid request: ${emsg}${eparam}`);
                                break;
                            case "internal_error":
                            case "unknown":
                            case "":
                            default:
                                addErrRetry(`${emsg}${eparam}`);
                                break;
                        }
                        $("thinking").classList.remove("on");
                        break;
                    }
                    case "message.delta": {
                        const delta = parsed.content || "";
                        if (!delta) break;

                        // Close reasoning block when content starts arriving
                        if (streamState.inReasoning) {
                            streamState.inReasoning = false;
                            if (streamState.thinkBody) {
                                streamState.thinkBody.classList.remove("open");
                                streamState.thinkBody.parentElement.previousElementSibling.textContent =
                                    "Show thinking";
                            }
                        }

                        // Track token stats
                        const now = performance.now();
                        streamState.deltaCount++;
                        if (!streamState.firstTokenTime) streamState.firstTokenTime = now;
                        streamState.deltaTimestamps.push(now);
                        // Keep rolling window of last 2 seconds
                        while (
                            streamState.deltaTimestamps.length > 1 &&
                            streamState.deltaTimestamps[0] < now - 2000
                        )
                            streamState.deltaTimestamps.shift();
                        // Update live stats display
                        if (streamState.deltaTimestamps.length > 1) {
                            const windowSec =
                                (streamState.deltaTimestamps[streamState.deltaTimestamps.length - 1] -
                                    streamState.deltaTimestamps[0]) /
                                1000;
                            if (windowSec > 0) {
                                const tps = (
                                    (streamState.deltaTimestamps.length - 1) / windowSec
                                ).toFixed(1);
                                updateLiveStats(tps + " tok/s");
                            }
                        }

                        // Handle <think> tags in streaming content
                        const combined = (streamState.inThink ? "" : streamState.content) + delta;
                        if (!streamState.inThink && !streamState.bub) {
                            // Check if content starts with <think>
                            if (combined.trimStart().startsWith("<think>")) {
                                streamState.inThink = true;
                                streamState.thinkBuf = combined.trimStart().slice(7);
                                // Create think element
                                const uid =
                                    "th" +
                                    Math.random().toString(36).slice(2, 8);
                                const d = document.createElement("div");
                                d.className = "m-think";
                                d.innerHTML = thinkHtml(uid, "Thinking...", "", true);
                                bindThinkToggle(d);
                                getMsgTarget().appendChild(d);
                                streamState.thinkBody = d.querySelector(".think-body");
                                streamState.thinkBody.textContent = streamState.thinkBuf;
                                autoScroll();
                                break;
                            }
                        }
                        if (streamState.inThink) {
                            streamState.thinkBuf += delta;
                            // Check for closing </think>
                            const closeIdx = streamState.thinkBuf.indexOf("</think>");
                            if (closeIdx !== -1) {
                                const thinkText = streamState.thinkBuf
                                    .slice(0, closeIdx)
                                    .trim();
                                if (streamState.thinkBody) {
                                    streamState.thinkBody.textContent = thinkText;
                                    streamState.thinkBody.classList.remove("open");
                                    streamState.thinkBody.parentElement.previousElementSibling.textContent =
                                        "Show thinking";
                                }
                                streamState.inThink = false;
                                // Remainder after </think> is normal content
                                const remainder = streamState.thinkBuf.slice(closeIdx + 8);
                                streamState.content = remainder;
                                if (remainder.trim()) {
                                    ensureStreamBub();
                                }
                            } else {
                                if (streamState.thinkBody) streamState.thinkBody.textContent = streamState.thinkBuf;
                            }
                            autoScroll();
                            break;
                        }

                        streamState.content += delta;
                        ensureStreamBub();
                        // Markdown rendering handled by streamMdTimer interval
                        autoScroll();
                        break;
                    }
                    case "tool_call.start":
                        $("thinking").querySelector("span").textContent =
                            `Using tools...`;
                        // Real LM Studio's ``tool_call.start`` payload is
                        // empty ({"type":"tool_call.start"}) — id, name,
                        // and arguments all arrive in later events.  Older
                        // LM Studio builds + the mock pre-fill them here,
                        // so pass through whatever's present.
                        addToolStream(
                            parsed.id,
                            parsed.tool || parsed.tool_name,
                            parsed.arguments,
                        );
                        break;
                    case "tool_call.name":
                        // 2026-05 LM Studio split tool name out of start;
                        // patch the running tool card's header in place.
                        setToolName(
                            parsed.tool_name || parsed.tool,
                            (parsed.provider_info && parsed.provider_info.plugin_id) || "",
                        );
                        break;
                    case "tool_call.arguments":
                        // Complete snapshot (dict), not a delta — overwrite.
                        // ``setToolArgs`` normalises the dict to JSON for
                        // display.  Prior code called the long-deleted
                        // ``updateToolArgs`` here, producing a silent
                        // ReferenceError that left ``.t-args`` empty.
                        setToolArgs(parsed.arguments);
                        break;
                    case "tool_call.success":
                        updateToolResult(parsed.id, parsed.output, true);
                        $("thinking").querySelector("span").textContent =
                            "Generating...";
                        break;
                    case "tool_call.failure":
                        updateToolResult(
                            parsed.id,
                            parsed.error || "Tool call failed",
                            false,
                        );
                        $("thinking").querySelector("span").textContent =
                            "Generating...";
                        break;
                    case "chat.end": {
                        // LM Studio native API nests response data under 'result'
                        const res = parsed.result || {};
                        const respId = parsed.response_id || res.response_id;
                        if (respId) meta.response_id = respId;
                        // Merge result stats into parsed for streamState.endStats consumers
                        const st = res.stats || {};
                        streamState.endStats = {
                            ...parsed,
                            stats: st,
                            usage: {
                                input_tokens: st.input_tokens || 0,
                                output_tokens: st.total_output_tokens || 0,
                            },
                        };
                        // Update context gauge
                        const inpTok = st.input_tokens || 0;
                        const ctxLen = (cachedModels.find((x) => x.id === modelSel.value)?.context_length) || 16000;
                        if (inpTok) updateCtxGauge(inpTok, ctxLen);
                        $("thinking").classList.remove("on");
                        break;
                    }
                    case "usage":
                        // Dedicated usage event from server with real token counts
                        {
                            const uInp =
                                parsed.input_tokens ||
                                parsed.prompt_tokens ||
                                0;
                            const uCtx = (cachedModels.find((x) => x.id === modelSel.value)?.context_length) || 16000;
                            if (uInp) updateCtxGauge(uInp, uCtx);
                        }
                        break;
                    case "status": {
                        const text = parsed.text || "";
                        const span = $("thinking")?.querySelector("span");
                        if (span) span.textContent = text;
                        break;
                    }
                }
            }

            function updateLiveStats(text) {
                if (!streamState.liveStatsEl) {
                    streamState.liveStatsEl = document.createElement("div");
                    streamState.liveStatsEl.className = "live-stats";
                    (streamState.group || getMsgTarget()).appendChild(streamState.liveStatsEl);
                }
                streamState.liveStatsEl.textContent = text;
            }

            function addMsgStats(container) {
                if (streamState.liveStatsEl) {
                    streamState.liveStatsEl.remove();
                    streamState.liveStatsEl = null;
                }
                const parts = [];
                const s = streamState.endStats || {};
                const stats = s.stats || {};
                const usage = s.usage || {};
                const tps = stats.tokens_per_second ||
                    (streamState.deltaCount > 2 && streamState.firstTokenTime ?
                        streamState.deltaCount / ((performance.now() - streamState.firstTokenTime) / 1000) : 0);
                if (tps > 0) parts.push(tps.toFixed(1) + " tok/s");
                const ttft = stats.time_to_first_token_seconds ||
                    (streamState.firstTokenTime && streamState.startTime ?
                        (streamState.firstTokenTime - streamState.startTime) / 1000 : 0);
                if (ttft > 0) parts.push("TTFT: " + ttft.toFixed(2) + "s");
                const inp = usage.input_tokens || usage.prompt_tokens || 0;
                const out = usage.output_tokens || usage.completion_tokens || streamState.deltaCount;
                if (inp || out) parts.push((inp || "?") + "\u2192" + out + " tokens");
                recordTokens(inp, out, tps);
                if (!parts.length) return;
                const statsText = parts.join(" \u00b7 ");
                const target = container || streamState.group || getMsgTarget();
                const statsEl = target?.querySelector(".msg-row-stats");
                if (statsEl) {
                    statsEl.textContent = statsText;
                }
                // All assistant messages include .msg-row-stats; no fallback path needed.
            }

            let streamMdTimer = null;
            function ensureStreamBub() {
                if (streamState.bub) return;
                const d = document.createElement("div");
                d.className = "m-asst";
                d.innerHTML = '<div class="bub streaming"></div>';
                // Append inside streamState.group if reasoning created one, otherwise directly to target
                (streamState.group || getMsgTarget()).appendChild(d);
                streamState.bub = d.querySelector(".bub");
                // Clear any orphaned timer before creating a new one
                if (streamMdTimer) cancelAnimationFrame(streamMdTimer);
                // Throttled markdown rendering during stream (~10fps)
                let lastRenderTime = 0;
                function streamRender() {
                    if (!streamState.bub || !streamState.content) return;
                    const now = performance.now();
                    if (now - lastRenderTime > 100) {
                        streamState.bub.innerHTML = md(streamState.content);
                        autoScroll();
                        lastRenderTime = now;
                    }
                    if (sending) streamMdTimer = requestAnimationFrame(streamRender);
                }
                streamMdTimer = requestAnimationFrame(streamRender);
            }

            let userScrolledUp = false,
                ignoreScrollEvent = false;
            scroll.addEventListener("scroll", () => {
                if (ignoreScrollEvent) return;
                const atBottom =
                    scroll.scrollHeight -
                        scroll.scrollTop -
                        scroll.clientHeight <
                    80;
                if (atBottom) {
                    userScrolledUp = false;
                    hideScrollBtn();
                } else {
                    userScrolledUp = sending;
                    showScrollBtn();
                }
            });
            function autoScroll() {
                if (userScrolledUp) return;
                scroll.scrollTop = scroll.scrollHeight;
            }
            function showScrollBtn() {
                const b = $("scroll-bottom");
                if (b) b.classList.add("visible");
            }
            function hideScrollBtn() {
                const b = $("scroll-bottom");
                if (b) b.classList.remove("visible");
            }
            function scrollToBottom() {
                userScrolledUp = false;
                scroll.scrollTop = scroll.scrollHeight;
                hideScrollBtn();
            }

            // Tool streaming state
            let curToolEl = null,
                curToolArgs = "";
            // Render-side helper — turn whatever LM Studio sent for
            // ``arguments`` into a human-readable string.  Real LM Studio
            // sends a dict like ``{query: "..."}``; older builds + the mock
            // can send strings (JSON-encoded or otherwise).  Without this
            // normaliser the dict gets template-stringified to
            // "[object Object]" — the bug behind the visible glitch.
            function argsToText(args) {
                if (args == null || args === "") return "";
                if (typeof args === "string") return args;
                try { return JSON.stringify(args, null, 2); }
                catch { return String(args); }
            }
            function addToolStream(id, name, args) {
                curToolArgs = args ?? "";
                const label = toolLabel(name);
                const uid = "tl" + Math.random().toString(36).slice(2, 8);
                const d = document.createElement("div");
                d.className = "m-tool";
                d.dataset.toolId = id || "";
                d.dataset.toolName = name || "";
                d.innerHTML = `<span class="t-name" style="display:none">${esc(name || "tool")}</span><div class="t-toggle" role="button" tabindex="0"><span class="t-arrow">&#9656;</span> ${esc(label)}<span class="t-preview"></span><span class="t-dots"><i></i><i></i><i></i></span></div><div class="t-body" id="${uid}"><div class="t-args"></div></div>`;
                // Append tool calls in order; text bubble will be re-appended after tools
                const target = streamState.group || getMsgTarget();
                ignoreScrollEvent = true;
                target.appendChild(d);
                // Move existing text bubble to end so it stays below tool calls
                const streamEl = streamState.bub && streamState.bub.closest(".m-asst");
                if (streamEl && streamEl.parentElement === target)
                    target.appendChild(streamEl);
                ignoreScrollEvent = false;
                curToolEl = d;
                if (curToolArgs !== "") {
                    curToolEl.querySelector(".t-args").textContent = argsToText(curToolArgs);
                    updateToolPreview();
                }
                autoScroll();
            }
            function updateToolPreview() {
                if (!curToolEl) return;
                const p = toolPreview(argsToText(curToolArgs));
                const el = curToolEl.querySelector(".t-preview");
                if (el) el.textContent = p;
            }
            function setToolArgs(args) {
                if (!curToolEl) return;
                // Real LM Studio sends ``tool_call.arguments`` as a COMPLETE
                // snapshot per call, not a delta — overwrite instead of
                // concatenating (the old delta-style ``+=`` produced
                // "[object Object][object Object]..." when args was a dict).
                curToolArgs = args ?? "";
                curToolEl.querySelector(".t-args").textContent = argsToText(curToolArgs);
                updateToolPreview();
                autoScroll();
            }
            function setToolName(name, provider) {
                // 2026-05 LM Studio emits tool_call.name AFTER the empty
                // ``tool_call.start`` marker — patch the header in place.
                // ``provider`` (the plugin id like ``mcp/searxng``) goes
                // into a dataset attribute so a future styling hook can
                // surface it without needing another DOM walk.
                //
                // ORDERING ASSUMPTION (LM Studio v0.4.x verified 2026-05):
                //   tool_call.start  →  tool_call.name  →  tool_call.arguments
                // ``curToolEl`` is the element that ``addToolStream`` created
                // on tool_call.start.  If LM Studio ever emits name BEFORE
                // start (no documented case, but possible), curToolEl is
                // null here and this becomes a silent no-op.  The name is
                // re-attempted at .arguments and .success/.failure, where
                // it would land on the element addToolStream eventually
                // creates — but the visible header would stay generic
                // until then.  Worth a probe if LM Studio changes order.
                if (!curToolEl || !name) return;
                curToolEl.dataset.toolName = name;
                if (provider) curToolEl.dataset.toolProvider = provider;
                const nameEl = curToolEl.querySelector(".t-name");
                if (nameEl) nameEl.textContent = name;
                const toggle = curToolEl.querySelector(".t-toggle");
                if (toggle) {
                    // Re-render the visible label.  Keep the existing
                    // arrow + preview + dots elements; replace only the
                    // text node between the arrow and the preview span.
                    const arrow = toggle.querySelector(".t-arrow");
                    const preview = toggle.querySelector(".t-preview");
                    if (arrow && preview) {
                        let n = arrow.nextSibling;
                        while (n && n !== preview) {
                            const next = n.nextSibling;
                            toggle.removeChild(n);
                            n = next;
                        }
                        toggle.insertBefore(
                            document.createTextNode(" " + toolLabel(name)),
                            preview,
                        );
                    }
                }
            }
            function extractToolOutput(output) {
                // MCP tools return [{type:"text",text:"..."}] — extract the text
                if (Array.isArray(output)) {
                    const texts = output
                        .filter((b) => b && b.type === "text" && b.text)
                        .map((b) => b.text);
                    if (texts.length) return texts.join("\n");
                }
                if (typeof output === "string") return output;
                if (output && typeof output === "object")
                    return JSON.stringify(output, null, 2);
                return "";
            }
            function renderStoredTool(raw) {
                // The DB column stores JSON-encoded values written by
                // server.py:_persist_chat_messages (json.dumps).  On
                // history reload we get the JSON text back; parse it and
                // delegate to ``extractToolOutput`` for the rendered form.
                // If the parsed value is the truncation envelope the
                // server uses for oversize rows, surface the preview plus
                // a "[truncated NB]" marker rather than the raw envelope.
                if (raw == null) return "";
                if (typeof raw !== "string") return extractToolOutput(raw);
                let parsed;
                try { parsed = JSON.parse(raw); }
                catch (_) { return raw; }
                if (parsed && typeof parsed === "object" && parsed.truncated === true
                    && typeof parsed.preview === "string") {
                    const dropped = (parsed.original_size || 0) - parsed.preview.length;
                    return parsed.preview + ` …[truncated ${dropped}B of ${parsed.original_size}B]`;
                }
                return extractToolOutput(parsed);
            }
            function updateToolResult(id, output, success) {
                if (!curToolEl) return;
                // Remove animated dots
                const dots = curToolEl.querySelector(".t-dots");
                if (dots) dots.remove();
                // Add result to body
                const o = extractToolOutput(output);
                const body = curToolEl.querySelector(".t-body");
                if (o && body) {
                    const out = document.createElement("div");
                    out.className = "t-out";
                    if (o.length > 1000) {
                        out.textContent = o.slice(0, 1000) + "…";
                        out.style.cursor = "pointer";
                        out.title = "Click to expand full output";
                        out.onclick = () => {
                            if (out.dataset.expanded) {
                                out.textContent = o.slice(0, 1000) + "…";
                                delete out.dataset.expanded;
                            } else {
                                out.textContent = o;
                                out.dataset.expanded = "1";
                            }
                        };
                    } else {
                        out.textContent = o;
                    }
                    body.appendChild(out);
                }
                // Make toggle clickable
                const toggle = curToolEl.querySelector(".t-toggle");
                const arrow = curToolEl.querySelector(".t-arrow");
                if (toggle && body) {
                    toggle.style.cursor = "pointer";
                    toggle.onclick = () => {
                        body.classList.toggle("open");
                        arrow.classList.toggle("open");
                    };
                    toggle.onkeydown = (e) => {
                        if (e.key === "Enter" || e.key === " ") {
                            e.preventDefault();
                            toggle.click();
                        }
                    };
                }
                if (!success && toggle) toggle.style.color = "var(--err-text)";
                curToolEl = null;
                curToolArgs = "";
                autoScroll();
            }

            function handleSyncResponse(data, meta) {
                if (data.error) {
                    addErr(data.error.message || JSON.stringify(data.error));
                    return;
                }
                const syncRes = data.result || {};
                const syncRespId = data.response_id || syncRes.response_id;
                if (syncRespId) meta.response_id = syncRespId;
                if (Array.isArray(data.output))
                    data.output.forEach((item) => {
                        if (item.type === "tool_call")
                            addTool(item.tool, item.arguments, item.output);
                    });
                let content = extractContent(data);
                if (content) {
                    const tm = content.match(/<think>([\s\S]*?)<\/think>/);
                    if (tm) {
                        addThink(tm[1].trim());
                        content = content
                            .replace(/<think>[\s\S]*?<\/think>/, "")
                            .trim();
                    }
                    if (content) {
                        const { text: cleanText, followups } =
                            extractFollowups(content);
                        addAsst(cleanText);
                        if (followups.length) {
                            const lastAsst = getMsgTarget().querySelector(
                                ".m-asst:last-of-type",
                            );
                            if (lastAsst) renderFollowups(followups, lastAsst);
                        }
                    }
                }
                addCopyButtons();
            }

            function extractContent(d) {
                if (d.content) return d.content;
                if (d.output_text) return d.output_text;
                if (typeof d.output === "string") return d.output;
                if (Array.isArray(d.output))
                    return d.output
                        .filter((i) => i.type === "message")
                        .map((i) => i.content || "")
                        .join("\n");
                if (d.choices) return d.choices[0]?.message?.content || "";
                return "";
            }

            // --- Utils ---
            function thinkHtml(uid, label, content, startOpen) {
                const openClass = startOpen ? " open" : "";
                const style = startOpen ? ' style="display:block"' : "";
                return `<div class="think-toggle" role="button" tabindex="0" data-action="toggle-think" data-uid="${uid}">${label}</div><div class="bub"><div id="${uid}" class="think-body${openClass}"${style}>${content}</div></div>`;
            }
            function bindThinkToggle(container) {
                const toggle = container.querySelector('[data-action="toggle-think"]');
                if (!toggle) return;
                toggle.addEventListener('click', function() {
                    const b = document.getElementById(this.dataset.uid);
                    b.classList.toggle('open');
                    this.textContent = b.classList.contains('open') ? 'Hide thinking' : 'Show thinking';
                });
                toggle.addEventListener('keydown', function(event) {
                    if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); this.click(); }
                });
            }
            function cancelSend(restoreAttachments) {
                sending = false;
                setSendMode(false);
                if (restoreAttachments) {
                    pendingAttachments = restoreAttachments;
                    renderAttachments();
                }
                input.value = "";
                input.style.height = "auto";
            }
            function estimateTokens(text) {
                const words = text.split(/\s+/).length;
                const special = (
                    text.match(/[{}\[\]()<>=/\\|@#$%^&*;:,.]/g) || []
                ).length;
                return Math.round(words * 1.3 + special * 0.5);
            }
            function esc(s) {
                return String(s)
                    .replace(/&/g, "&amp;")
                    .replace(/</g, "&lt;")
                    .replace(/>/g, "&gt;")
                    .replace(/"/g, "&quot;")
                    .replace(/'/g, "&#39;");
            }
            function _execCopy(text) {
                const ta = document.createElement('textarea');
                ta.value = text;
                ta.style.cssText = 'position:fixed;top:0;left:0;opacity:0;';
                document.body.appendChild(ta);
                ta.focus();
                ta.select();
                document.execCommand('copy');
                document.body.removeChild(ta);
            }
            async function copyToClipboard(text) {
                try {
                    if (navigator.clipboard && navigator.clipboard.writeText) {
                        await navigator.clipboard.writeText(text);
                    } else {
                        _execCopy(text);
                    }
                } catch {
                    _execCopy(text);
                }
            }
            function showDialog(opts) {
                return new Promise((resolve) => {
                    const el = document.createElement("div");
                    el.className = "share-dialog";
                    el.setAttribute("role", "dialog");
                    el.setAttribute("aria-modal", "true");
                    const fields = (opts.fields || [])
                        .map(
                            (f, i) =>
                                `<div style="margin-top:var(--sp-5)"><label style="font-size:var(--text-sm);color:var(--dim);display:block;margin-bottom:var(--sp-2)">${esc(f.label)}</label><input id="dlg-f-${i}" type="${f.type || "text"}" placeholder="${esc(f.placeholder || "")}" value="${esc(f.value || "")}" style="width:100%;background:var(--surface);border:1px solid var(--border);border-radius:var(--r-md);padding:var(--sp-4) var(--sp-5);font-size:var(--text-base);color:var(--text);font-family:inherit;outline:none;box-sizing:border-box" ${f.required ? "required" : ""}></div>`,
                        )
                        .join("");
                    const singleInput =
                        opts.input ?
                            `<div style="margin-top:var(--sp-5)"><input id="dlg-input" type="text" placeholder="${esc(opts.placeholder || "")}" value="${esc(opts.inputValue || "")}" style="width:100%;background:var(--surface);border:1px solid var(--border);border-radius:var(--r-md);padding:var(--sp-4) var(--sp-5);font-size:var(--text-base);color:var(--text);font-family:inherit;outline:none;box-sizing:border-box"></div>`
                        :   "";
                    const dangerStyle =
                        opts.danger ?
                            "background:var(--err-bg,rgba(220,38,38,.15));color:var(--err-text);border:1px solid var(--err-border)"
                        :   "";
                    el.innerHTML = `<div class="share-content" style="animation:ddIn 150ms var(--ease)"><h3 style="margin:0 0 var(--sp-3)">${esc(opts.title)}</h3>${opts.message ? `<p style="color:var(--dim);font-size:var(--text-base);margin:var(--sp-2) 0 0">${esc(opts.message)}</p>` : ""}${singleInput}${fields}<div style="display:flex;gap:var(--sp-4);margin-top:var(--sp-7);justify-content:flex-end"><button id="dlg-cancel" class="sys-btn" style="background:var(--surface);border:1px solid var(--border);color:var(--dim)">${esc(opts.cancelText || "Cancel")}</button><button id="dlg-ok" class="sys-btn" style="${dangerStyle}">${esc(opts.confirmText || "OK")}</button></div></div>`;
                    document.body.appendChild(el);
                    const close = (val) => {
                        el.remove();
                        resolve(val);
                    };
                    el.querySelector("#dlg-cancel").onclick = () => close(null);
                    el.querySelector("#dlg-ok").onclick = () => {
                        if (opts.fields) {
                            const vals = opts.fields.map(
                                (_, i) => el.querySelector("#dlg-f-" + i).value,
                            );
                            close(vals);
                        } else if (opts.input)
                            close(el.querySelector("#dlg-input").value);
                        else close(true);
                    };
                    el.addEventListener("click", (e) => {
                        if (e.target === el) close(null);
                    });
                    el.addEventListener("keydown", (e) => {
                        if (e.key === "Escape") close(null);
                    });
                    const firstInput =
                        el.querySelector("input") ||
                        el.querySelector("#dlg-ok");
                    setTimeout(() => firstInput.focus(), 10);
                });
            }
            function sanitizeSvg(svg) {
                if (!svg) return "";
                // Parse in isolated document (no script execution) via DOMParser
                const doc = new DOMParser().parseFromString(
                    svg,
                    "image/svg+xml",
                );
                const el = doc.querySelector("svg");
                if (!el || doc.querySelector("parsererror")) return "";
                el.querySelectorAll(
                    "script,foreignObject,animate,set,style,use[href]",
                ).forEach((e) => e.remove());
                el.querySelectorAll("*").forEach((node) => {
                    [...node.attributes].forEach((attr) => {
                        if (
                            attr.name.startsWith("on") ||
                            attr.name === "style" ||
                            (attr.name === "href" && node.tagName !== "use")
                        )
                            node.removeAttribute(attr.name);
                    });
                });
                return el.outerHTML;
            }
            function md(text) {
                // Strip followup comments before rendering (extracted separately in finishStream).
                // First: complete comment. Second: partial comment still being emitted mid-stream.
                text = text.replace(/<!--followups:?\s*\[.*?\]\s*-->\s*$/s, "");
                text = text.replace(/<!--followups:?[\s\S]*$/s, "");
                // Extract fenced + inline code BEFORE escaping/transforms so
                // other markdown regexes never mutate code content.  Without
                // this, ``**kwargs`` inside a Python fence becomes
                // ``<strong>kwargs</strong>`` INSIDE the <code>, which is
                // what triggers highlight.js's "unescaped HTML" warnings.
                // Placeholders use control characters that survive esc()
                // unchanged and won't collide with user content.
                const _fenced = [];
                text = text.replace(/```(\w*)\n([\s\S]*?)```/g, (_m, lang, code) => {
                    const i = _fenced.push({ lang, code }) - 1;
                    return `CODE${i}`;
                });
                const _inlines = [];
                text = text.replace(/`([^`\n]+)`/g, (_m, code) => {
                    const i = _inlines.push(code) - 1;
                    return `INLINE${i}`;
                });
                let h = esc(text);
                h = h.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
                h = h.replace(/(?<!\*)\*([^*]+)\*(?!\*)/g, "<em>$1</em>");
                h = h.replace(/^### (.+)$/gm, "<h3>$1</h3>");
                h = h.replace(/^## (.+)$/gm, "<h2>$1</h2>");
                h = h.replace(/^# (.+)$/gm, "<h1>$1</h1>");
                h = h.replace(/^\&gt; (.+)$/gm, "<blockquote>$1</blockquote>");
                h = h.replace(/\[([^\]]+)\]\(([^)]+)\)/g, (m, text, url) => {
                    const raw = url
                        .replace(/&amp;/g, "&")
                        .replace(/&lt;/g, "<")
                        .replace(/&gt;/g, ">")
                        .replace(/&quot;/g, '"')
                        .replace(/&#39;/g, "'");
                    return /^https?:\/\//i.test(raw) || /^mailto:/i.test(raw) ?
                            `<a href="${url.replace(/"/g, "&quot;")}" target="_blank" rel="noopener">${text}</a>`
                        :   text;
                });
                // GFM pipe tables
                h = h.replace(
                    /((?:^|\n)\|.+\|[ ]*\n\|[\s:|-]+\|[ ]*\n(?:\|.+\|[ ]*\n?)+)/g,
                    (tbl) => {
                        const rows = tbl
                            .trim()
                            .split("\n")
                            .filter((r) => r.trim());
                        if (rows.length < 2) return tbl;
                        const parseRow = (r) =>
                            r
                                .replace(/^\|/, "")
                                .replace(/\|$/, "")
                                .split("|")
                                .map((c) => c.trim());
                        const hdr = parseRow(rows[0]);
                        const body = rows.slice(2).map(parseRow);
                        return (
                            "</p><table><thead><tr>" +
                            hdr.map((c) => "<th>" + c + "</th>").join("") +
                            "</tr></thead><tbody>" +
                            body
                                .map(
                                    (r) =>
                                        "<tr>" +
                                        r
                                            .map(
                                                (c) =>
                                                    "<td>" + c + "</td>",
                                            )
                                            .join("") +
                                        "</tr>",
                                )
                                .join("") +
                            "</tbody></table><p>"
                        );
                    },
                );
                h = h.replace(/\n\n/g, "</p><p>");
                h = h.replace(/\n/g, "<br>");
                // Restore code placeholders.  Re-escape the original code
                // content so highlight.js sees textContent only (no real
                // <strong>, <em>, or other tags inside <code>).
                h = h.replace(/\x01INLINE(\d+)\x01/g, (_m, i) =>
                    `<code>${esc(_inlines[+i])}</code>`);
                h = h.replace(/\x01CODE(\d+)\x01/g, (_m, i) => {
                    const { lang, code } = _fenced[+i];
                    const langAttr = lang ? ` data-lang="${esc(lang)}"` : "";
                    return `<pre${langAttr}><code>${esc(code)}</code></pre>`;
                });
                return "<p>" + h + "</p>";
            }

            // --- Copy buttons on code blocks ---
            function addCopyButtons() {
                msgs.querySelectorAll("pre:not(.has-copy)").forEach((pre) => {
                    pre.classList.add("has-copy");
                    const btn = document.createElement("button");
                    btn.className = "copy-btn";
                    btn.textContent = "Copy";
                    btn.onclick = async () => {
                        const code = pre.querySelector("code");
                        await copyToClipboard(code ? code.textContent : pre.textContent);
                        btn.textContent = "Copied!";
                        setTimeout(() => (btn.textContent = "Copy"), 1500);
                    };
                    pre.appendChild(btn);
                });
            }

            // --- Regenerate button ---
            function addRegenButton() {
                if (sending || !activeId) return;
                const assts = msgs.querySelectorAll(".m-asst");
                if (!assts.length) return;
                const lastAsst = assts[assts.length - 1];
                const regenBtn = lastAsst.querySelector(".regen-btn");
                if (regenBtn) {
                    regenBtn.disabled = false;
                }
            }

            async function patchMsgIds() {
                if (!activeId) return;
                try {
                    const resp = await apiFetch(
                        `/api/chats/${activeId}/messages`,
                    );
                    const list = await resp.json();
                    const userMsgs = [...msgs.querySelectorAll(".m-user")];
                    const dbUsers = list.filter((m) => m.role === "user");
                    userMsgs.forEach((el, i) => {
                        if (dbUsers[i] && dbUsers[i].id)
                            el.dataset.msgId = dbUsers[i].id;
                    });
                    // Also patch assistant messages for forking
                    const asstMsgs = [...msgs.querySelectorAll(".m-asst")];
                    const dbAssts = list.filter((m) => m.role === "assistant");
                    asstMsgs.forEach((el, i) => {
                        if (dbAssts[i] && dbAssts[i].id)
                            el.dataset.msgId = dbAssts[i].id;
                    });
                } catch (e) {
                    console.error("patchMsgIds:", e);
                }
            }

            async function regenerate() {
                if (sending || !activeId) return;
                // Disable the regen button immediately (re-enabled by addRegenButton after stream completes)
                const assts = msgs.querySelectorAll(".m-asst");
                if (assts.length) {
                    const regenBtn = assts[assts.length - 1].querySelector(".regen-btn");
                    if (regenBtn) regenBtn.disabled = true;
                }
                // Incognito: re-send last user message from DOM (no server history)
                if (incognitoMode) {
                    const userEls = [...msgs.querySelectorAll(".m-user")];
                    if (!userEls.length) return;
                    const lastText = userEls[userEls.length - 1].dataset.text;
                    // Remove last assistant + tool + stats messages
                    while (msgs.lastChild) {
                        const cls = msgs.lastChild.className || "";
                        if (cls.includes("m-user")) break;
                        msgs.lastChild.remove();
                    }
                    if (
                        msgs.lastChild &&
                        (msgs.lastChild.className || "").includes("m-user")
                    )
                        msgs.lastChild.remove();
                    chatMeta[activeId].response_id = null;
                    await resendText(lastText);
                    return;
                }
                // Call server to delete last response
                let data;
                try {
                    const resp = await apiFetch(
                        `/api/chats/${activeId}/messages/last`,
                        { method: "DELETE" },
                    );
                    if (!resp.ok) { addErr("Failed to remove last message."); return; }
                    data = await resp.json();
                } catch (e) {
                    addErr("Failed to regenerate: " + e.message);
                    return;
                }
                if (!data.user_content) return;
                // Remove last assistant + tool + stats/indicator messages from DOM (walk backwards until user msg)
                while (msgs.lastChild) {
                    const cls = msgs.lastChild.className || "";
                    if (cls.includes("m-user")) break;
                    msgs.lastChild.remove();
                }
                // Also remove the last user message (server deleted it, resendText will re-add)
                if (
                    msgs.lastChild &&
                    (msgs.lastChild.className || "").includes("m-user")
                )
                    msgs.lastChild.remove();
                // Reset response_id since server cleared it
                chatMeta[activeId].response_id = null;
                // Resend the user message through streaming
                await resendText(data.user_content);
            }

            // --- Edit user message ---
            function startEdit(el) {
                if (sending) return;
                const text = el.dataset.text;
                const bub = el.querySelector(".bub");
                const editBtn = el.querySelector(".edit-btn");
                editBtn.style.display = "none";
                bub.style.display = "none";
                const area = document.createElement("div");
                area.className = "edit-area";
                area.innerHTML = `<textarea>${esc(text)}</textarea><div class="edit-btns"><button class="cancel" data-action="cancel-edit">Cancel</button><button class="save" data-action="save-edit">Save &amp; Send</button></div>`;
                area.querySelector('[data-action="cancel-edit"]').addEventListener('click', function() { cancelEdit(this); });
                area.querySelector('[data-action="save-edit"]').addEventListener('click', function() { saveEdit(this); });
                el.appendChild(area);
                const ta = area.querySelector("textarea");
                ta.style.height = Math.max(60, ta.scrollHeight) + "px";
                ta.focus();
            }

            function cancelEdit(btn) {
                const el = btn.closest(".m-user");
                el.querySelector(".edit-area").remove();
                el.querySelector(".bub").style.display = "";
                el.querySelector(".edit-btn").style.display = "";
            }

            async function saveEdit(btn) {
                if (sending) return;
                const el = btn.closest(".m-user");
                const newText = el
                    .querySelector(".edit-area textarea")
                    .value.trim();
                if (!newText) {
                    cancelEdit(btn);
                    return;
                }
                const msgId = el.dataset.msgId;
                // Truncate from this message onward in DB
                if (msgId && activeId) {
                    const r = await apiFetch(`/api/chats/${activeId}/messages/truncate`, {
                        method: "POST",
                        headers: { "Content-Type": "application/json" },
                        body: JSON.stringify({
                            from_message_id: parseInt(msgId),
                        }),
                    });
                    if (!r.ok) {
                        cancelEdit(btn);
                        addErr("Failed to edit message — please try again.");
                        return;
                    }
                    chatMeta[activeId].response_id = null;
                }
                // Remove this message and everything after it from DOM
                while (msgs.lastChild && msgs.lastChild !== el)
                    msgs.lastChild.remove();
                el.remove();
                // Resend edited text
                await resendText(newText);
            }

            async function resendText(text) {
                if (!activeId) return;
                const meta = chatMeta[activeId];
                addUser(text);
                scroll.scrollTop = scroll.scrollHeight;
                sending = true;
                setSendMode(true);
                $("thinking").classList.add("on");

                const presetKey = (chatMeta[activeId] || {})._activePreset;
                const sys =
                    presetKey && PRESETS[presetKey] ?
                        expandVars(PRESETS[presetKey])
                    : chatSettingsCache?.system_prompt ?
                        expandVars(chatSettingsCache.system_prompt)
                    :   expandVars($("s-sys").value.trim());
                const body = buildChatBody(text, sys || undefined);

                await streamRequest(body, meta);
            }

            // --- Model management ---
            // Aliased to state.models — same pattern as MCPS/remoteMcps.
            // ``broadcastModels()`` after any in-place mutation fires the
            // "models" subscribers (renderModelList, chat settings panel,
            // reasoning button capability check, etc.).
            let cachedModels = state.models;
            function broadcastModels() { setState({ models: state.models }); }
            const ICON_VISION_PATH =
                "M10 4C5.6 4 2 7 .5 10c1.5 3 5.1 6 9.5 6s8-3 9.5-6c-1.5-3-5.1-6-9.5-6zm0 10a4 4 0 1 1 0-8 4 4 0 0 1 0 8zm0-6.5a2.5 2.5 0 1 0 0 5 2.5 2.5 0 0 0 0-5z";
            const ICON_TOOLS_PATH =
                "M17.4 4.2L14.5 7l-1.8-1.8L15.4 2A5.2 5.2 0 0 0 9 3c-.8.9-1.2 2-1.1 3.2L2.6 11.5a2 2 0 0 0 0 2.8l1.1 1.1a2 2 0 0 0 2.8 0l5.3-5.3c1.2.1 2.3-.3 3.2-1.1a5.2 5.2 0 0 0 1-6.4l-.1-.1zM5.5 14.2a.8.8 0 1 1 0-1.6.8.8 0 0 1 0 1.6z";
            function iconCopy() {
                return `<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="9" width="13" height="13" rx="2" ry="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>`;
            }
            function iconFork() {
                return `<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="6" y1="3" x2="6" y2="15"/><circle cx="18" cy="6" r="3"/><circle cx="6" cy="18" r="3"/><path d="M18 9a9 9 0 0 1-9 9"/></svg>`;
            }
            function iconRegen() {
                return `<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="1 4 1 10 7 10"/><path d="M3.51 15a9 9 0 1 0 .49-4.32"/></svg>`;
            }
            function iconThumbUp() {
                return `<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M14 9V5a3 3 0 0 0-3-3l-4 9v11h11.28a2 2 0 0 0 2-1.7l1.38-9a2 2 0 0 0-2-2.3z"/><path d="M7 22H4a2 2 0 0 1-2-2v-7a2 2 0 0 1 2-2h3"/></svg>`;
            }
            function iconThumbDown() {
                return `<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M10 15v4a3 3 0 0 0 3 3l4-9V2H5.72a2 2 0 0 0-2 1.7l-1.38 9a2 2 0 0 0 2 2.3z"/><path d="M17 2h2.67A2.31 2.31 0 0 1 22 4v7a2.31 2.31 0 0 1-2.33 2H17"/></svg>`;
            }
            function iconEdit() {
                return `<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"/><path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z"/></svg>`;
            }
            function iconPin() {
                return `<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="12" y1="17" x2="12" y2="22"/><path d="M5 17h14v-1.76a2 2 0 0 0-1.11-1.79l-1.78-.9A2 2 0 0 1 15 10.76V6h1a2 2 0 0 0 0-4H8a2 2 0 0 0 0 4h1v4.76a2 2 0 0 1-1.11 1.79l-1.78.9A2 2 0 0 0 5 15.24Z"/></svg>`;
            }

            function buildMsgRow(opts) {
                // opts: { role, msgId, statsText, feedback, isPinned }
                const row = document.createElement("div");
                row.className = "msg-row";

                if (opts.role === "assistant") {
                    const stats = document.createElement("div");
                    stats.className = "msg-row-stats";
                    stats.textContent = opts.statsText || "";
                    row.appendChild(stats);

                    const actions = document.createElement("div");
                    actions.className = "msg-row-actions";

                    const copyBtn = document.createElement("button");
                    copyBtn.className = "msg-action-btn";
                    copyBtn.title = "Copy";
                    copyBtn.innerHTML = iconCopy();
                    copyBtn.onclick = () => {
                        const bub = copyBtn.closest(".m-asst, .msg-group")?.querySelector(".bub");
                        if (bub) copyToClipboard(bub.innerText || bub.textContent);
                    };
                    actions.appendChild(copyBtn);

                    const forkBtn = document.createElement("button");
                    forkBtn.className = "msg-action-btn fork-btn";
                    forkBtn.title = "Fork from here";
                    forkBtn.innerHTML = iconFork();
                    forkBtn.onclick = function() { forkFromMsg(this); };
                    actions.appendChild(forkBtn);

                    const regenBtn = document.createElement("button");
                    regenBtn.className = "msg-action-btn regen-btn";
                    regenBtn.title = "Regenerate";
                    regenBtn.innerHTML = iconRegen();
                    regenBtn.disabled = true;
                    regenBtn.onclick = () => regenerate();
                    actions.appendChild(regenBtn);

                    const sep = document.createElement("span");
                    sep.style.cssText = "width:1px;height:12px;background:var(--border);margin:0 2px";
                    actions.appendChild(sep);

                    const upBtn = document.createElement("button");
                    upBtn.className = "msg-action-btn thumb-up" + (opts.feedback === 1 ? " voted-up" : "");
                    upBtn.title = "Helpful";
                    upBtn.innerHTML = iconThumbUp();
                    upBtn.onclick = () => {
                        const msgId = upBtn.closest("[data-msg-id]")?.dataset.msgId;
                        if (!msgId) return;
                        submitFeedback(msgId, upBtn.classList.contains("voted-up") ? 0 : 1, row);
                    };
                    actions.appendChild(upBtn);

                    const downBtn = document.createElement("button");
                    downBtn.className = "msg-action-btn thumb-down" + (opts.feedback === -1 ? " voted-down" : "");
                    downBtn.title = "Not helpful";
                    downBtn.innerHTML = iconThumbDown();
                    downBtn.onclick = () => {
                        const msgId = downBtn.closest("[data-msg-id]")?.dataset.msgId;
                        if (!msgId) return;
                        submitFeedback(msgId, downBtn.classList.contains("voted-down") ? 0 : -1, row);
                    };
                    actions.appendChild(downBtn);

                    if (opts.isPinned) {
                        const pinBtn = document.createElement("button");
                        pinBtn.className = "msg-action-btn pin-active";
                        pinBtn.title = "Pinned — click to view navigator";
                        pinBtn.innerHTML = iconPin();
                        pinBtn.onclick = () => openPinNavigator();
                        actions.appendChild(pinBtn);
                    }

                    // Hover-only pin action (for unpinned messages)
                    if (!opts.isPinned) {
                        const hoverPinBtn = document.createElement("button");
                        hoverPinBtn.className = "msg-action-btn hover-pin-btn";
                        hoverPinBtn.title = "Pin this response";
                        hoverPinBtn.innerHTML = iconPin();
                        hoverPinBtn.onclick = () => {
                            const msgId = hoverPinBtn.closest("[data-msg-id]")?.dataset.msgId;
                            if (!msgId) return;
                            pinMessage(msgId, hoverPinBtn.closest(".msg-row"));
                        };
                        actions.appendChild(hoverPinBtn);
                    }

                    row.appendChild(actions);
                } else if (opts.role === "user") {
                    const spacer = document.createElement("div");
                    spacer.style.flex = "1";
                    row.appendChild(spacer);

                    const actions = document.createElement("div");
                    actions.className = "msg-row-actions";

                    const copyBtn = document.createElement("button");
                    copyBtn.className = "msg-action-btn";
                    copyBtn.title = "Copy";
                    copyBtn.innerHTML = iconCopy();
                    copyBtn.onclick = () => {
                        const bub = copyBtn.closest(".m-user")?.querySelector(".bub");
                        if (bub) copyToClipboard(bub.innerText || bub.textContent);
                    };
                    actions.appendChild(copyBtn);

                    const forkBtn = document.createElement("button");
                    forkBtn.className = "msg-action-btn fork-btn";
                    forkBtn.title = "Fork from here";
                    forkBtn.innerHTML = iconFork();
                    forkBtn.onclick = function() { forkFromMsg(this); };
                    actions.appendChild(forkBtn);

                    const editBtn = document.createElement("button");
                    editBtn.className = "msg-action-btn edit-btn";
                    editBtn.title = "Edit";
                    editBtn.innerHTML = iconEdit();
                    editBtn.onclick = function() { startEdit(this.closest(".m-user")); };
                    actions.appendChild(editBtn);

                    row.appendChild(actions);
                }

                return row;
            }

            async function pinMessage(msgId, rowEl) {
                try {
                    const r = await apiFetch(`/api/messages/${msgId}/pin`, {
                        method: "POST",
                        headers: { "Content-Type": "application/json" },
                        body: "{}"
                    });
                    if (!r.ok) throw new Error("pin failed");
                    const data = await r.json();
                    const actions = rowEl?.querySelector(".msg-row-actions");
                    if (actions) {
                        actions.querySelector(".hover-pin-btn")?.remove();
                        const pinBtn = document.createElement("button");
                        pinBtn.className = "msg-action-btn pin-active";
                        pinBtn.title = "Pinned";
                        pinBtn.innerHTML = iconPin();
                        pinBtn.dataset.pinId = data.id;
                        pinBtn.onclick = () => openPinNavigator();
                        actions.appendChild(pinBtn);
                    }
                    await loadPinNavigator(activeId);
                } catch(e) {
                    console.error("Pin error:", e);
                }
            }

            async function unpinMessage(pinId, pinBtnEl) {
                try {
                    const r = await apiFetch(`/api/pins/${pinId}`, { method: "DELETE" });
                    if (!r.ok) throw new Error("unpin failed");
                    const actions = pinBtnEl?.closest(".msg-row-actions");
                    if (actions) {
                        // Read msgId before detaching — closest() returns null on detached nodes
                        const msgId = pinBtnEl.closest("[data-msg-id]")?.dataset.msgId;
                        pinBtnEl.remove();
                        if (msgId) {
                            const hoverBtn = document.createElement("button");
                            hoverBtn.className = "msg-action-btn hover-pin-btn";
                            hoverBtn.title = "Pin this response";
                            hoverBtn.innerHTML = iconPin();
                            hoverBtn.onclick = () => pinMessage(msgId, hoverBtn.closest(".msg-row"));
                            actions.appendChild(hoverBtn);
                        }
                    }
                    await loadPinNavigator(activeId);
                } catch(e) {
                    console.error("Unpin error:", e);
                }
            }

            async function submitFeedback(msgId, rating, rowEl) {
                const upBtn = rowEl?.querySelector(".thumb-up");
                const downBtn = rowEl?.querySelector(".thumb-down");
                const wasUp = upBtn?.classList.contains("voted-up") ?? false;
                const wasDown = downBtn?.classList.contains("voted-down") ?? false;
                if (upBtn) upBtn.classList.toggle("voted-up", rating === 1);
                if (downBtn) downBtn.classList.toggle("voted-down", rating === -1);
                try {
                    const r = await apiFetch(`/api/messages/${msgId}/feedback`, {
                        method: "POST",
                        headers: { "Content-Type": "application/json" },
                        body: JSON.stringify({ rating })
                    });
                    if (!r.ok) throw new Error("feedback failed");
                } catch(e) {
                    if (upBtn) upBtn.classList.toggle("voted-up", wasUp);
                    if (downBtn) downBtn.classList.toggle("voted-down", wasDown);
                    console.error("Feedback error:", e);
                }
            }
            function openPinNavigator() {
                $("pin-nav")?.classList.remove("collapsed");
                $("pin-nav")?.scrollIntoView({ behavior: "smooth", block: "nearest" });
            }

            // Right panel state: null | "settings" | "pins"
            let rightPanelState = null;

            let _rpOpener = null;
            function openRightPanel(mode) {
                if (rightPanelState === mode) {
                    closeRightPanel();
                    return;
                }
                _rpOpener = document.activeElement;
                rightPanelState = mode;
                setState({ view: { rightPanel: mode } });
                document.body.classList.add("has-right-panel");
                $("right-panel").classList.add("open");
                $("right-panel").removeAttribute("aria-hidden");
                $("right-panel-overlay").classList.add("open");
                if (mode === "settings") {
                    $("right-panel-title").textContent = "Chat Settings";
                    const actionBtn = $("right-panel-action-btn");
                    actionBtn.classList.remove("hidden");
                    actionBtn.textContent = "Reset";
                    actionBtn.onclick = resetChatSettings;
                    renderChatSettingsPanel();
                } else if (mode === "pins") {
                    $("right-panel-title").textContent = "Pinned";
                    $("right-panel-action-btn").classList.add("hidden");
                    renderGlobalPinsPanel();
                }
                // Move focus into panel after render
                const panel = $("right-panel");
                const first = panel.querySelector('button:not([disabled]), select, textarea, input, [tabindex="0"]');
                (first || panel).focus();
            }

            function closeRightPanel() {
                const wasPins = rightPanelState === "pins";
                rightPanelState = null;
                setState({ view: { rightPanel: null } });
                document.body.classList.remove("has-right-panel");
                $("right-panel").classList.remove("open");
                $("right-panel").setAttribute("aria-hidden", "true");
                $("right-panel-overlay").classList.remove("open");
                _rpOpener?.focus();
                _rpOpener = null;
                // URL sync — restore the active chat's path (or /) when
                // dismissing the pins panel.  Matches closeSettings'
                // semantics: explicit-dismiss returns the URL to the
                // underlying surface so the address bar doesn't strand
                // at /pins after the panel is gone.
                if (wasPins && !_applyingRoute && window.location.pathname === "/pins") {
                    const target = activeId ? `/chat/${activeId}` : "/";
                    if (window.history.length > 1) {
                        history.back();
                    } else if (window.location.pathname !== target) {
                        history.replaceState({}, "", target);
                    }
                }
            }
            $("pins-btn")?.addEventListener("click", () => openRightPanel("pins"));

            // Chat settings: per-chat overrides state
            // Legacy alias: chatSettingsCache and state.chatSettings now
            // point at the SAME object reference.  Direct mutation of
            // chatSettingsCache.x bypasses setState's dispatch, but the read
            // is consistent.  All NEW writes route through setState.  When
            // every reader migrates to state.chatSettings, this alias goes
            // away.
            let chatSettingsCache = state.chatSettings;
            let chatSettingsDebounce = null;
            let chatSettingsPending = {};

            // Wipe state.chatSettings keys directly then merge the new ones
            // via setState — preserves the object identity (chatSettingsCache
            // is aliased to state.chatSettings) and broadcasts one event.
            function _commitChatSettings(next) {
                for (const k of Object.keys(state.chatSettings)) delete state.chatSettings[k];
                setState({ chatSettings: next || {} });
            }

            async function loadChatSettings(chatId, { refreshPanel = true } = {}) {
                const btn = $("chat-settings-btn");
                if (!chatId) {
                    _commitChatSettings({});
                    if (btn) btn.classList.remove("has-overrides");
                    return;
                }
                try {
                    const r = await apiFetch(`/api/chats/${chatId}/settings`);
                    if (chatId !== activeId) return;  // navigated away while loading — discard
                    if (!r.ok) {
                        _commitChatSettings({});
                        if (btn) btn.classList.remove("has-overrides");
                        return;
                    }
                    const loaded = await r.json();
                    _commitChatSettings(loaded);
                    if (btn) btn.classList.toggle("has-overrides", Object.keys(loaded).length > 0);
                } catch(e) {
                    if (chatId !== activeId) return;  // stale error — don't wipe current chat's cache
                    _commitChatSettings({});
                    if (btn) btn.classList.remove("has-overrides");
                }
                // Re-render panel if open and caller requested it (skipped after saves to preserve focus).
                if (refreshPanel && rightPanelState === "settings") renderChatSettingsPanel();
                // renderReasoningUI() runs automatically on the next RAF via
                // its subscription to state.chatSettings — no manual call needed.
            }

            function renderChatSettingsPanel() {
                const body = $("right-panel-body");
                const s = chatSettingsCache;
                // Placeholders show LM Studio's model-level defaults (instance_config).
                // Leave empty when not known — LM Studio governs these at the model level.
                const activeModel = cachedModels.find((x) => x.id === modelSel.value);
                const mc = activeModel?.instance_config || {};
                const gTemp    = mc.temperature     != null ? String(mc.temperature)     : "";
                const gTopP    = mc.top_p           != null ? String(mc.top_p)           : "";
                const gTopK    = mc.top_k           != null ? String(mc.top_k)           : "";
                const gMinP    = mc.min_p           != null ? String(mc.min_p)           : "";
                const gRepPen  = mc.repeat_penalty  != null ? String(mc.repeat_penalty)  : "";
                const gMaxTok  = mc.max_tokens      != null ? String(mc.max_tokens)      : "";
                // Detect which preset the current system_prompt matches (for dropdown state)
                const currentSys = s.system_prompt || "";
                // Default to general when no per-chat override — that's the app default
                const detectedPreset = currentSys
                    ? (Object.entries(PRESETS).find(([,v]) => v === currentSys)?.[0] || "custom")
                    : "general";
                const globalScOn   = localStorage.getItem("lsc-sc") === "1";
                const globalCoveOn = localStorage.getItem("lsc-cove") === "1";
                const scOn   = s.sc_enabled   != null ? s.sc_enabled   : globalScOn;
                const coveOn = s.cove_enabled != null ? s.cove_enabled : globalCoveOn;
                body.innerHTML = `
                    <div class="sg cs-primary">
                        <label>System Prompt</label>
                        <select id="cs-preset">
                            <option value="general" ${detectedPreset==="general"?"selected":""}>General Assistant</option>
                            <option value="coder" ${detectedPreset==="coder"?"selected":""}>Coding Agent</option>
                            <option value="creative" ${detectedPreset==="creative"?"selected":""}>Creative Writing</option>
                            <option value="research" ${detectedPreset==="research"?"selected":""}>Deep Research</option>
                            <option value="analyst" ${detectedPreset==="analyst"?"selected":""}>Strategic Analyst</option>
                            <option value="architect" ${detectedPreset==="architect"?"selected":""}>Systems Architect</option>
                            <option value="custom" ${detectedPreset==="custom"?"selected":""}>Custom</option>
                        </select>
                        <textarea id="cs-sys" rows="4" placeholder="Custom system prompt…"></textarea>
                        <div class="var-help">{{current_date}} {{day_of_week}} {{current_time}} {{model}} — replaced on send</div>
                    </div>
                    <div class="sg">
                        <label>Temperature</label>
                        <input type="number" id="cs-temp" min="0" max="2" step="0.1" placeholder="${gTemp||"LM Studio default"}" value="${s.temperature ?? ""}">
                    </div>
                    <div class="cs-adv-row">
                        <button class="cs-adv-btn" id="cs-adv-btn" aria-expanded="false" aria-controls="cs-adv-body">
                            <svg width="12" height="12" viewBox="0 0 12 12" fill="none" aria-hidden="true"><path d="M2 4l4 4 4-4" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/></svg>
                            Advanced settings
                        </button>
                        <span class="cs-adv-hint">Defaults from LM Studio</span>
                    </div>
                    <div id="cs-adv-body" class="cs-adv-body" hidden>
                        <div class="sg sg-row">
                            <div class="sg-half"><label>Top P</label>
                                <input type="number" id="cs-top-p" min="0" max="1" step="0.05" placeholder="${gTopP||"default"}" value="${s.top_p ?? ""}"></div>
                            <div class="sg-half"><label>Top K</label>
                                <input type="number" id="cs-top-k" min="0" max="500" step="1" placeholder="${gTopK||"default"}" value="${s.top_k ?? ""}"></div>
                        </div>
                        <div class="sg sg-row">
                            <div class="sg-half"><label>Min P</label>
                                <input type="number" id="cs-min-p" min="0" max="1" step="0.01" placeholder="${gMinP||"default"}" value="${s.min_p ?? ""}"></div>
                            <div class="sg-half"><label>Repeat Penalty</label>
                                <input type="number" id="cs-repeat-pen" min="0" max="3" step="0.05" placeholder="${gRepPen||"default"}" value="${s.repeat_penalty ?? ""}"></div>
                        </div>
                        <div class="sg"><label>Max Output Tokens</label>
                            <input type="number" id="cs-max-tokens" min="-1" max="32768" step="256" placeholder="${gMaxTok||"unlimited"}" value="${s.max_output_tokens ?? ""}">
                        </div>
                        <div class="sg"><label>Reasoning</label>
                            <select id="cs-reasoning">
                                <option value="" ${!s.reasoning?"selected":""}>Global (${state.serverInfo.defaultReasoning || "off"})</option>
                                <option value="off" ${s.reasoning==="off"?"selected":""}>Off</option>
                                <option value="medium" ${s.reasoning==="medium"?"selected":""}>Medium</option>
                                <option value="high" ${s.reasoning==="high"?"selected":""}>High</option>
                            </select>
                        </div>
                    </div>
                    <div class="cs-quality">
                        <div class="toggle-row">
                            <div>
                                <span>Self-Consistency</span>
                                <div class="cs-quality-hint">Sample multiple responses, synthesize the best</div>
                            </div>
                            <label class="sw"><input type="checkbox" id="cs-sc" aria-label="Self-Consistency" ${scOn?"checked":""}><span class="slider"></span></label>
                        </div>
                        <div class="toggle-row">
                            <div>
                                <span>Chain of Verification</span>
                                <div class="cs-quality-hint">Fact-check via verification questions</div>
                            </div>
                            <label class="sw"><input type="checkbox" id="cs-cove" aria-label="Chain of Verification" ${coveOn?"checked":""}><span class="slider"></span></label>
                        </div>
                        <div class="toggle-row">
                            <div>
                                <span>Stateless turns</span>
                                <div class="cs-quality-hint">Don't ask LM Studio to keep server-side response state (no <code>previous_response_id</code> chain)</div>
                            </div>
                            <label class="sw"><input type="checkbox" id="cs-stateless" aria-label="Stateless turns" ${s.store === false ? "checked" : ""}><span class="slider"></span></label>
                        </div>
                    </div>
                `;
                const ta = $("cs-sys");
                if (ta) ta.value = s.system_prompt || (detectedPreset !== "custom" && PRESETS[detectedPreset] ? PRESETS[detectedPreset] : "");

                // Advanced settings toggle
                const advBtn  = $("cs-adv-btn");
                const advBody = $("cs-adv-body");
                if (advBtn && advBody) {
                    // Auto-expand if any advanced field has a per-chat override
                    const hasAdv = [s.top_p, s.top_k, s.min_p, s.repeat_penalty, s.max_output_tokens, s.reasoning].some(v => v != null);
                    if (hasAdv) {
                        advBody.hidden = false;
                        advBtn.setAttribute("aria-expanded", "true");
                        advBtn.classList.add("open");
                    }
                    advBtn.addEventListener("click", () => {
                        const open = advBody.hidden;
                        advBody.hidden = !open;
                        advBtn.setAttribute("aria-expanded", String(open));
                        advBtn.classList.toggle("open", open);
                    });
                }

                // Preset dropdown: populate textarea when a preset is chosen
                const presetSel = $("cs-preset");
                if (presetSel) {
                    presetSel.addEventListener("change", () => {
                        const v = presetSel.value;
                        if (v === "custom") {
                            // leave textarea as-is
                        } else if (PRESETS[v]) {
                            ta.value = PRESETS[v];
                            saveChatSetting("system_prompt", PRESETS[v]);
                        }
                    });
                }

                const fields = [
                    ["cs-sys",          "system_prompt",    (v) => v || null,               "textarea"],
                    ["cs-temp",         "temperature",      (v) => v===""?null:+v,          "input"],
                    ["cs-top-p",        "top_p",            (v) => v===""?null:+v,          "input"],
                    ["cs-top-k",        "top_k",            (v) => v===""?null:parseInt(v), "input"],
                    ["cs-min-p",        "min_p",            (v) => v===""?null:+v,          "input"],
                    ["cs-repeat-pen",   "repeat_penalty",   (v) => v===""?null:+v,          "input"],
                    ["cs-max-tokens",   "max_output_tokens",(v) => v===""?null:parseInt(v), "input"],
                    ["cs-reasoning",    "reasoning",        (v) => v||null,                 "select"],
                    ["cs-sc",           "sc_enabled",       (v) => v,                       "checkbox"],
                    ["cs-cove",         "cove_enabled",     (v) => v,                       "checkbox"],
                    // Stateless turns: persisted as ``store === false``
                    // so the on-the-wire body matches LM Studio's
                    // ``store`` param directly (no rename layer).
                    ["cs-stateless",    "store",            (v) => v ? false : null,         "checkbox"],
                ];
                fields.forEach(([id, key, transform, type]) => {
                    const el = $(id);
                    if (!el) return;
                    el.addEventListener("change", () => {
                        const raw = type === "checkbox" ? el.checked : el.value;
                        saveChatSetting(key, transform(raw));
                    });
                    if (type !== "textarea") return;
                    el.addEventListener("input", () => {
                        if (presetSel) presetSel.value = el.value ? "custom" : "general";
                        saveChatSetting(key, transform(el.value));
                    });
                });
            }

            function saveChatSetting(key, value) {
                if (!activeId) return;
                // Optimistic local update — setState dispatches and the
                // aliased chatSettingsCache reflects the change for legacy
                // readers in the same tick.  PATCH failure reverts to prev.
                const prev = state.chatSettings[key];
                setState({ chatSettings: { [key]: value } });
                chatSettingsPending[key] = value;
                if (chatSettingsDebounce) clearTimeout(chatSettingsDebounce);
                chatSettingsDebounce = setTimeout(async () => {
                    const payload = { ...chatSettingsPending };
                    chatSettingsPending = {};
                    try {
                        await apiFetch(`/api/chats/${activeId}/settings`, {
                            method: "PATCH",
                            headers: { "Content-Type": "application/json" },
                            body: JSON.stringify(payload)
                        });
                        await loadChatSettings(activeId, { refreshPanel: false });
                    } catch(e) {
                        console.error("Failed to save chat setting:", e);
                        toast.error(`Couldn't save "${key}" — reverted.`, { detail: e.message });
                        setState({ chatSettings: { [key]: prev } });
                    }
                }, 400);
            }

            async function resetChatSettings() {
                if (!activeId) return;
                try {
                    await apiFetch(`/api/chats/${activeId}/settings`, { method: "DELETE" });
                    _commitChatSettings({});
                    renderChatSettingsPanel();
                    const btn = $("chat-settings-btn");
                    if (btn) btn.classList.remove("has-overrides");
                } catch(e) {
                    console.error("Failed to reset chat settings:", e);
                    toast.error("Couldn't reset chat settings.", { detail: e.message });
                }
            }

            async function renderGlobalPinsPanel() {
                const body = $("right-panel-body");
                body.innerHTML = '<div style="color:var(--dim);font-size:var(--text-sm)">Loading...</div>';
                try {
                    const r = await apiFetch("/api/pins");
                    if (!r.ok) throw new Error("Failed to load pins");
                    const pins = await r.json();
                    if (!pins.length) {
                        body.innerHTML = '<div style="color:var(--faint);font-size:var(--text-sm);text-align:center;padding:var(--sp-8)">No pins yet</div>';
                        return;
                    }
                    body.innerHTML = "";
                    pins.forEach(p => {
                        const item = document.createElement("div");
                        item.style.cssText = "padding:var(--sp-4) 0;border-bottom:1px solid var(--border)";
                        item.innerHTML = `
                            <div style="font-size:var(--text-sm);color:var(--text);margin-bottom:var(--sp-2)">${esc(p.pin_title || p.preview)}</div>
                            <div style="font-size:0.65rem;color:var(--faint)">${esc(p.chat_title)} · ${new Date(p.pinned_at * 1000).toLocaleDateString()}</div>
                        `;
                        const delBtn = document.createElement("button");
                        delBtn.className = "msg-action-btn";
                        delBtn.title = "Unpin";
                        delBtn.style.cssText = "float:right;color:var(--faint);margin-left:var(--sp-3)";
                        delBtn.innerHTML = `<svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>`;
                        delBtn.onclick = (e) => {
                            e.stopPropagation();
                            apiFetch(`/api/pins/${p.id}`, { method: "DELETE" })
                                .then(() => {
                                    renderGlobalPinsPanel();
                                    if (activeId) loadPinNavigator(activeId);
                                })
                                .catch((err) => console.error("Failed to unpin:", err));
                        };
                        item.appendChild(delBtn);
                        item.style.cursor = "pointer";
                        item.onclick = async () => {
                            if (p.chat_id && p.message_id) {
                                await loadChat(p.chat_id);
                                scrollToMessage(p.message_id);
                                closeRightPanel();
                            } else {
                                const existing = item.querySelector(".pin-content-expanded");
                                if (!existing) {
                                    const expanded = document.createElement("div");
                                    expanded.className = "pin-content-expanded";
                                    expanded.style.cssText = "margin-top:var(--sp-3);font-size:var(--text-xs);color:var(--dim);white-space:pre-wrap;border-top:1px solid var(--border);padding-top:var(--sp-3)";
                                    expanded.textContent = p.preview || p.content || "(no content)";
                                    item.appendChild(expanded);
                                } else {
                                    existing.remove();
                                }
                            }
                        };
                        body.appendChild(item);
                    });
                } catch(e) {
                    body.innerHTML = '<div style="color:var(--faint);font-size:var(--text-sm)">Failed to load pins</div>';
                }
            }

            function togglePinNavigator() {
                $("pin-nav")?.classList.toggle("collapsed");
            }

            async function loadPinNavigator(chatId) {
                if (!chatId) return;
                try {
                    const r = await apiFetch(`/api/chats/${chatId}/pins`);
                    if (!r.ok) return;
                    const pins = await r.json();
                    const nav = $("pin-nav");
                    const list = $("pin-nav-list");
                    if (!pins.length) {
                        nav.classList.remove("has-pins");
                        return;
                    }
                    nav.classList.add("has-pins");
                    list.innerHTML = "";
                    pins.forEach(p => {
                        const item = document.createElement("div");
                        item.className = "pin-nav-item";
                        item.innerHTML = `
                            <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="var(--accent)" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                                <line x1="12" y1="17" x2="12" y2="22"/><path d="M5 17h14v-1.76a2 2 0 0 0-1.11-1.79l-1.78-.9A2 2 0 0 1 15 10.76V6h1a2 2 0 0 0 0-4H8a2 2 0 0 0 0 4h1v4.76a2 2 0 0 1-1.11 1.79l-1.78.9A2 2 0 0 0 5 15.24Z"/>
                            </svg>
                            <span>${esc(p.pin_title || "Pinned response")}</span>
                        `;
                        item.onclick = () => scrollToMessage(p.message_id);
                        list.appendChild(item);
                    });

                    // Reconcile isPinned indicators on already-rendered message rows
                    // Build both a set (for fast lookup) and a map (msgId → pinId for dataset)
                    const pinnedIds = new Set(pins.map(p => String(p.message_id)).filter(Boolean));
                    const pinIdByMsgId = new Map(pins.filter(p => p.message_id).map(p => [String(p.message_id), p.id]));

                    // Remove stale .pin-active buttons
                    msgs.querySelectorAll(".pin-active").forEach(btn => {
                        const id = btn.closest("[data-msg-id]")?.dataset.msgId;
                        if (!id || !pinnedIds.has(id)) btn.remove();
                    });

                    for (const id of pinnedIds) {
                        const msgEl = msgs.querySelector(`[data-msg-id="${id}"]`);
                        if (!msgEl) continue;
                        const actions = msgEl.querySelector(".msg-row-actions");
                        if (!actions || actions.querySelector(".pin-active")) continue;
                        actions.querySelector(".hover-pin-btn")?.remove();
                        const pinBtn = document.createElement("button");
                        pinBtn.className = "msg-action-btn pin-active";
                        pinBtn.title = "Pinned — click to view navigator";
                        pinBtn.innerHTML = iconPin();
                        pinBtn.dataset.pinId = pinIdByMsgId.get(id) ?? "";
                        pinBtn.onclick = () => openPinNavigator();
                        actions.appendChild(pinBtn);
                    }
                } catch(e) {
                    console.error("Load pin navigator:", e);
                }
            }

            function scrollToMessage(msgId) {
                if (!msgId) return;
                const el = msgs.querySelector(`[data-msg-id="${msgId}"]`);
                if (el) el.scrollIntoView({ behavior: "smooth", block: "center" });
            }

            function capIcon(path, fill) {
                return (
                    '<svg class="cap-icon" viewBox="0 0 20 20"><path fill="' +
                    fill +
                    '" d="' +
                    path +
                    '"/></svg>'
                );
            }

            function modelDisplayName(m) {
                return m.identifier || m.id;
            }
            function modelLabelHtml(m) {
                const caps = m.capabilities || {};
                let h = esc(modelDisplayName(m));
                if (caps.vision) h += capIcon(ICON_VISION_PATH, "var(--accent)");
                if (caps.trained_for_tool_use)
                    h += capIcon(ICON_TOOLS_PATH, "var(--green)");
                return h;
            }
            function modelLabel(m) {
                const caps = m.capabilities || {};
                let label = modelDisplayName(m);
                if (caps.vision) label += " ◉";
                if (caps.trained_for_tool_use) label += " ⚒";
                return label;
            }

            async function refreshModels() {
                try {
                    const r = await apiFetch("/api/models");
                    if (!r.ok) throw new Error("bad status");
                    const d = await r.json();
                    const raw = d.data || d.models || d || [];
                    // Normalize: LM Studio native API uses 'key' not 'id'
                    // In-place wipe preserves the alias to state.models so
                    // subscribers (Phase 2) fire on broadcastModels() below.
                    cachedModels.length = 0;
                    raw.forEach((m) => {
                        const key = m.id || m.key || "";
                        if (!key || key.includes("embed") || key.includes("arena"))
                            return;
                        const inst = (m.loaded_instances || [])[0];
                        const instCfg = inst?.config || {};
                        const instCtx = instCfg.context_length || 0;
                        const maxCtx = m.max_context_length || 0;
                        // Use loaded instance id (user nickname) for API routing so
                        // LM Studio matches the already-loaded instance instead of
                        // JIT-loading a new one on every request.
                        const identifier = inst?.id || m.display_name || "";
                        const id = inst?.id || key;
                        // Use instance context if reasonable (>=1024), otherwise fall back to model max
                        cachedModels.push({
                            ...m,
                            id,
                            key,
                            identifier,
                            context_length: instCtx >= 1024 ? instCtx : maxCtx,
                            max_context_length: maxCtx,
                            instance_config: instCfg,
                        });
                    });
                    // Sort: loaded models first
                    cachedModels.sort((a, b) => {
                        const al =
                                (a.loaded_instances || []).length > 0 ? 0 : 1,
                            bl = (b.loaded_instances || []).length > 0 ? 0 : 1;
                        return al - bl;
                    });
                    // Broadcast so Phase 2 subscribers (model list, chat
                    // settings panel, reasoning button capability check)
                    // pick up the fresh model set without an explicit call.
                    broadcastModels();
                    renderModelList();
                    // Update topbar selector
                    const prev = modelSel.value;
                    modelSel.innerHTML = "";
                    cachedModels.forEach((m) => {
                        const o = document.createElement("option");
                        o.value = m.id;
                        o.textContent = modelLabel(m);
                        modelSel.appendChild(o);
                    });
                    const saved = localStorage.getItem("lsc-model");
                    // Find the currently loaded model in LM Studio
                    const loaded = cachedModels.find(
                        (m) => (m.loaded_instances || []).length > 0,
                    );
                    if (
                        saved &&
                        [...modelSel.options].some((o) => o.value === saved)
                    )
                        modelSel.value = saved;
                    else if (
                        loaded &&
                        [...modelSel.options].some((o) => o.value === loaded.id)
                    )
                        modelSel.value = loaded.id;
                    else if (
                        prev &&
                        [...modelSel.options].some((o) => o.value === prev)
                    )
                        modelSel.value = prev;
                    else if (modelSel.options.length)
                        modelSel.value = modelSel.options[0].value;
                    // Clear stale localStorage if saved model no longer exists
                    if (
                        saved &&
                        ![...modelSel.options].some((o) => o.value === saved)
                    )
                        localStorage.removeItem("lsc-model");
                    setConn(
                        "green",
                        cachedModels.length +
                            " model" +
                            (cachedModels.length !== 1 ? "s" : ""),
                    );
                    updateModelPill();
                    updateTopModelLabel();
                    syncModelSettings();
                } catch (e) {
                    setConn("red", "Disconnected");
                    renderModelList();
                    updateModelPill();
                    updateTopModelLabel();
                }
            }

            function renderModelList() {
                const list = $("model-list");
                if (!list) return;
                list.innerHTML = "";
                if (!cachedModels.length) {
                    list.innerHTML =
                        '<div class="model-empty">No models loaded</div>';
                    return;
                }
                cachedModels.forEach((m) => {
                    const isLoaded = (m.loaded_instances || []).length > 0;
                    const inst = (m.loaded_instances || [])[0];
                    const ctx = inst?.config?.context_length || m.context_length || "";
                    const d = document.createElement("div");
                    d.className = "model-item" + (isLoaded ? "" : " unloaded");
                    d.innerHTML = `<span class="mi-name" title="${esc(m.key)}">${modelLabelHtml(m)}</span><span class="mi-status ${isLoaded ? "loaded" : "unloaded"}">${isLoaded ? "Loaded" : "Idle"}</span>${ctx ? `<span class="mi-ctx">${ctx}</span>` : ""}`;
                    list.appendChild(d);
                });
            }

            // --- Keyboard shortcuts ---
            let _kbOpener = null;
            function showKBShortcuts() {
                _kbOpener = document.activeElement;
                $("kb-modal").classList.add("open");
                $("kb-overlay").classList.add("open");
                $("kb-modal").focus();
            }
            function hideKBShortcuts() {
                $("kb-modal").classList.remove("open");
                $("kb-overlay").classList.remove("open");
                _kbOpener?.focus();
                _kbOpener = null;
            }

            document.addEventListener("keydown", (e) => {
                const mod = e.metaKey || e.ctrlKey;
                const tag = e.target.tagName;
                const inInput =
                    tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT";

                // Trap Tab inside kb-modal (no interactive children, just loop focus back)
                if (e.key === "Tab" && $("kb-modal").classList.contains("open")) {
                    e.preventDefault();
                    $("kb-modal").focus();
                    return;
                }

                // Escape always works
                if (e.key === "Escape") {
                    e.preventDefault();
                    if ($("export-dd").classList.contains("open")) {
                        $("export-dd").classList.remove("open");
                        return;
                    }
                    if ($("kb-modal").classList.contains("open")) {
                        hideKBShortcuts();
                        return;
                    }
                    if (settingsOpen) {
                        closeSettings();
                        return;
                    }
                    // Cancel edit mode if active
                    const editArea = msgs.querySelector(".edit-area");
                    if (editArea) {
                        cancelEdit(editArea.querySelector(".cancel"));
                        return;
                    }
                    // Close sidebar on mobile
                    if (
                        !document.body.classList.contains("sb-closed") &&
                        window.innerWidth <= 768
                    ) {
                        closeSB();
                        return;
                    }
                    return;
                }

                // Don't fire other shortcuts when typing
                if (inInput) return;

                if (mod && e.key === "n") {
                    e.preventDefault();
                    newChat().catch((e) => addErr("Failed to create chat: " + e.message));
                    return;
                }
                if (mod && e.shiftKey && (e.key === "S" || e.key === "s")) {
                    e.preventDefault();
                    toggleSidebar();
                    return;
                }
                if (mod && e.key === ",") {
                    e.preventDefault();
                    if (settingsOpen) closeSettings();
                    else openSettings();
                    return;
                }
                if (mod && e.shiftKey && (e.key === "E" || e.key === "e")) {
                    e.preventDefault();
                    exportChat("md");
                    return;
                }
            });

            function toggleSidebar() {
                if (document.body.classList.contains("sb-closed")) openSB();
                else closeSB();
            }

            // --- Export chat ---
            function toggleExportDD() {
                const isOpen = $("export-dd").classList.toggle("open");
                $("export-btn").setAttribute("aria-expanded", isOpen ? "true" : "false");
            }
            // Close export dropdown on outside press-and-release.
            _globalTeardowns.push(onOutside(
                () => $("export-wrap"),
                () => {
                    $("export-dd")?.classList.remove("open");
                    $("export-btn")?.setAttribute("aria-expanded", "false");
                },
            ));

            function updateExportBtn() {
                if (activeId) {
                    $("export-btn").classList.remove("hidden");
                    $("share-btn").classList.remove("hidden");
                } else {
                    $("export-btn").classList.add("hidden");
                    $("share-btn").classList.add("hidden");
                }
            }

            async function shareChat() {
                if (!activeId) return;
                let data;
                try {
                    const res = await apiFetch(
                        "/api/chats/" + activeId + "/share",
                        {
                            method: "POST",
                            headers: { "Content-Type": "application/json" },
                            body: "{}",
                        },
                    );
                    if (!res.ok) { addErr("Failed to create share link."); return; }
                    data = await res.json();
                } catch (e) {
                    addErr("Failed to create share link.");
                    return;
                }
                const url = location.origin + data.url;
                const dialog = document.createElement("div");
                dialog.className = "share-dialog";
                dialog.innerHTML =
                    '<div class="share-content"><h3>Share Conversation</h3><p style="color:var(--dim);font-size:var(--text-sm);margin:var(--sp-4) 0">Anyone with this link can view a read-only snapshot of this conversation.</p><div style="display:flex;gap:var(--sp-3)"><input id="share-url" value="' +
                    esc(url) +
                    '" readonly style="flex:1;background:var(--surface);border:1px solid var(--border);border-radius:var(--r-sm);padding:var(--sp-3) var(--sp-5);font-size:var(--text-sm);color:var(--text);font-family:inherit"><button data-action="copy-share-url" class="sys-btn" style="white-space:nowrap">Copy</button></div><div style="display:flex;gap:var(--sp-4);margin-top:var(--sp-6);justify-content:flex-end"><button data-action="delete-share-link" data-chat-id="' +
                    activeId +
                    '" style="background:none;border:1px solid var(--err-border);color:var(--err-text);padding:var(--sp-2) var(--sp-6);border-radius:var(--r-sm);font-size:var(--text-xs);cursor:pointer;font-family:inherit">Delete Link</button><button data-action="close-share-dialog" class="sys-btn">Done</button></div></div>';
                dialog.querySelector('[data-action="copy-share-url"]').addEventListener('click', function() {
                    copyToClipboard(document.getElementById('share-url').value);
                    this.textContent = 'Copied!';
                    setTimeout(() => this.textContent = 'Copy', 1500);
                });
                dialog.querySelector('[data-action="delete-share-link"]').addEventListener('click', function() {
                    unshareChat(this.dataset.chatId, this.closest('.share-dialog'));
                });
                dialog.querySelector('[data-action="close-share-dialog"]').addEventListener('click', function() {
                    this.closest('.share-dialog').remove();
                });
                document.body.appendChild(dialog);
                // Close on backdrop click
                dialog.addEventListener("click", (e) => {
                    if (e.target === dialog) dialog.remove();
                });
            }

            async function unshareChat(chatId, dialog) {
                try {
                    const r = await apiFetch("/api/chats/" + chatId + "/share", {
                        method: "DELETE",
                    });
                    if (!r.ok) throw new Error(`${r.status}`);
                } catch (e) {
                    addErr("Failed to delete share link.");
                    return;
                }
                if (dialog) dialog.remove();
            }

            function slugify(s) {
                return (
                    s
                        .toLowerCase()
                        .replace(/[^a-z0-9]+/g, "-")
                        .replace(/^-|-$/g, "") || "chat"
                );
            }

            function exportChat(fmt) {
                $("export-dd").classList.remove("open");
                if (!activeId) return;
                const meta = chatMeta[activeId];
                const title = meta?.title || "Chat";
                const slug = slugify(title);

                // Gather messages from the DOM
                const items = [];
                msgs.querySelectorAll(
                    ".m-user,.m-asst,.m-tool,.m-think,.m-err",
                ).forEach((el) => {
                    if (el.classList.contains("m-user")) {
                        items.push({
                            role: "user",
                            content:
                                el.dataset.text ||
                                el.querySelector(".bub")?.textContent ||
                                "",
                        });
                    } else if (el.classList.contains("m-asst")) {
                        items.push({
                            role: "assistant",
                            content:
                                el.querySelector(".bub")?.textContent || "",
                        });
                    } else if (el.classList.contains("m-tool")) {
                        const name =
                            el.querySelector(".t-name")?.textContent || "";
                        const args =
                            el.querySelector(".t-args")?.textContent || "";
                        const out =
                            el.querySelector(".t-out")?.textContent || "";
                        items.push({ role: "tool", name, args, output: out });
                    } else if (el.classList.contains("m-think")) {
                        items.push({
                            role: "thinking",
                            content:
                                el.querySelector(".think-body")?.textContent ||
                                "",
                        });
                    } else if (el.classList.contains("m-err")) {
                        items.push({
                            role: "error",
                            content:
                                el.querySelector(".bub")?.textContent || "",
                        });
                    }
                });

                let blob, filename;
                if (fmt === "json") {
                    blob = new Blob(
                        [JSON.stringify({ title, messages: items }, null, 2)],
                        { type: "application/json" },
                    );
                    filename = slug + ".json";
                } else {
                    let lines = ["# " + title, ""];
                    items.forEach((m) => {
                        if (m.role === "user") {
                            lines.push("**You:** " + m.content, "");
                            lines.push("---", "");
                        } else if (m.role === "assistant") {
                            lines.push("**Assistant:** " + m.content, "");
                            lines.push("---", "");
                        } else if (m.role === "tool") {
                            lines.push(
                                "> **" +
                                    m.name +
                                    "**(" +
                                    m.args +
                                    ") → " +
                                    m.output,
                                "",
                            );
                        } else if (m.role === "thinking") {
                            lines.push("> *Thinking:* " + m.content, "");
                        } else if (m.role === "error") {
                            lines.push("> **Error:** " + m.content, "");
                        }
                    });
                    blob = new Blob([lines.join("\n")], {
                        type: "text/markdown",
                    });
                    filename = slug + ".md";
                }

                const url = URL.createObjectURL(blob);
                const a = document.createElement("a");
                a.href = url;
                a.download = filename;
                document.body.appendChild(a);
                a.click();
                document.body.removeChild(a);
                URL.revokeObjectURL(url);
            }

            // --- Slash Commands ---
            const SLASH_MENU_ITEMS = [
                {
                    cmd: "/research",
                    desc: "Deep Research mode",
                    preset: "research",
                },
                { cmd: "/code", desc: "Coding Agent mode", preset: "coder" },
                {
                    cmd: "/write",
                    desc: "Creative Writing mode",
                    preset: "creative",
                },
                {
                    cmd: "/analyze",
                    desc: "Strategic Analyst mode",
                    preset: "analyst",
                },
                {
                    cmd: "/architect",
                    desc: "Systems Architect mode",
                    preset: "architect",
                },
                { cmd: "/compact", desc: "Summarize and compact context" },
                { cmd: "/help", desc: "Show available commands" },
            ];
            const SLASH_CMDS = Object.fromEntries(
                SLASH_MENU_ITEMS.filter((s) => s.preset).map((s) => [
                    s.cmd.slice(1),
                    { preset: s.preset, label: s.desc },
                ]),
            );

            function parseSlashCmd(text) {
                if (!text.startsWith("/")) return null;
                const spaceIdx = text.indexOf(" ");
                const cmd =
                    spaceIdx > 0 ? text.slice(1, spaceIdx) : text.slice(1);
                const rest =
                    spaceIdx > 0 ? text.slice(spaceIdx + 1).trim() : "";
                if (cmd === "help") return { type: "help" };
                if (cmd === "compact") return { type: "compact" };
                if (SLASH_CMDS[cmd])
                    return {
                        type: "cmd",
                        cmd,
                        rest,
                        label: SLASH_CMDS[cmd].label,
                        preset: SLASH_CMDS[cmd].preset,
                    };
                return null;
            }

            function showSlashHelp() {
                const lines = [
                    "**Available Commands:**",
                    "",
                    "`/research <query>` — Deep Research mode",
                    "`/code <query>` — Coding Agent mode",
                    "`/write <query>` — Creative Writing mode",
                    "`/analyze <query>` — Strategic Analyst mode",
                    "`/architect <query>` — Systems Architect mode",
                    "`/compact` — Summarize and compact conversation context",
                    "`/help` — Show this help",
                    "",
                    "Commands temporarily switch the system prompt for one message.",
                ];
                addAsst(lines.join("\n"));
                addCopyButtons();
                scroll.scrollTop = scroll.scrollHeight;
            }

            // --- Slash Command Popup ---
            //
            // When the user picks ``/research`` (or types ``/research <space>``)
            // we move the command out of ``input.value`` and into
            // ``pendingPreset``.  The badge then represents the mode and the
            // textarea only carries the user's actual text — the "[Research]
            // /research my query" double-render that motivated this change.
            //
            // After a successful send, ``meta._activePreset`` on the active
            // chat becomes the source of truth for sticky-mode display
            // (continuing turns).  ``pendingPreset`` is one-shot — cleared
            // after send so the next pick re-locks.
            let slashIdx = -1;
            let pendingPreset = null;

            function setPendingPreset(preset) {
                pendingPreset = preset || null;
                updateCmdBadge();
            }

            function clearActiveMode() {
                pendingPreset = null;
                if (activeId && chatMeta[activeId]) {
                    delete chatMeta[activeId]._activePreset;
                }
                updateCmdBadge();
            }

            function updateSlashMenu() {
                const menu = $("slash-menu");
                const val = input.value;
                // Lock-in detection: if the input now starts with a real
                // preset command followed by a space, hoist the preset
                // into ``pendingPreset`` and strip the prefix from the
                // textarea.  Non-preset commands (``/help``, ``/compact``,
                // unknown) fall through to the legacy send-time parse.
                if (val.startsWith("/") && val.includes(" ")) {
                    const spaceIdx = val.indexOf(" ");
                    const cmdName = val.slice(1, spaceIdx).toLowerCase();
                    const matched = SLASH_MENU_ITEMS.find(
                        (s) => s.preset && s.cmd.slice(1) === cmdName,
                    );
                    if (matched) {
                        pendingPreset = matched.preset;
                        input.value = val.slice(spaceIdx + 1);
                        input.style.height = "auto";
                        input.style.height = Math.min(input.scrollHeight, 160) + "px";
                    }
                    menu.classList.remove("open");
                    slashIdx = -1;
                    updateCmdBadge();
                    return;
                }
                updateCmdBadge();
                if (!val.startsWith("/")) {
                    menu.classList.remove("open");
                    slashIdx = -1;
                    return;
                }
                const filter = val.slice(1).toLowerCase();
                const matches = SLASH_MENU_ITEMS.filter((s) =>
                    s.cmd.slice(1).startsWith(filter),
                );
                if (!matches.length) {
                    menu.classList.remove("open");
                    slashIdx = -1;
                    return;
                }
                menu.innerHTML = "";
                matches.forEach((s, i) => {
                    const btn = document.createElement("button");
                    btn.innerHTML = `<span class="sc-cmd">${esc(s.cmd)}</span><span class="sc-desc">${esc(s.desc)}</span>`;
                    if (i === slashIdx) btn.classList.add("sel");
                    btn.onmousedown = (e) => {
                        e.preventDefault();
                        if (s.preset) {
                            // Preset commands lock-in immediately — the
                            // badge represents the mode, the input is
                            // cleared for the user's actual prompt.
                            pendingPreset = s.preset;
                            input.value = "";
                        } else {
                            // ``/help`` / ``/compact`` stay literal so a
                            // single Enter triggers them via the existing
                            // parse path in sendMsg.
                            input.value = s.cmd + " ";
                        }
                        input.style.height = "auto";
                        menu.classList.remove("open");
                        slashIdx = -1;
                        input.focus();
                        updateCmdBadge();
                    };
                    menu.appendChild(btn);
                });
                menu.classList.add("open");
            }

            function updateCmdBadge() {
                const badge = $("cmd-badge");
                let preset = pendingPreset;
                if (!preset && activeId && chatMeta[activeId]) {
                    preset = chatMeta[activeId]._activePreset || null;
                }
                if (!preset) {
                    badge.classList.remove("visible");
                    return;
                }
                const match = SLASH_MENU_ITEMS.find((s) => s.preset === preset);
                if (!match) {
                    badge.classList.remove("visible");
                    return;
                }
                const name = match.cmd.slice(1);
                badge.textContent =
                    name.charAt(0).toUpperCase() + name.slice(1);
                badge.classList.add("visible");
            }

            function slashKeyNav(e) {
                const menu = $("slash-menu");
                if (!menu.classList.contains("open")) return false;
                const btns = menu.querySelectorAll("button");
                if (e.key === "ArrowDown") {
                    e.preventDefault();
                    slashIdx = Math.min(slashIdx + 1, btns.length - 1);
                    btns.forEach((b, i) =>
                        b.classList.toggle("sel", i === slashIdx),
                    );
                    return true;
                }
                if (e.key === "ArrowUp") {
                    e.preventDefault();
                    slashIdx = Math.max(slashIdx - 1, 0);
                    btns.forEach((b, i) =>
                        b.classList.toggle("sel", i === slashIdx),
                    );
                    return true;
                }
                if (e.key === "Tab" || (e.key === "Enter" && !e.shiftKey)) {
                    const target =
                        slashIdx >= 0 && btns[slashIdx] ?
                            btns[slashIdx]
                        :   btns[0];
                    if (target) {
                        e.preventDefault();
                        target.onmousedown(e);
                        return true;
                    }
                }
                if (e.key === "Escape") {
                    menu.classList.remove("open");
                    slashIdx = -1;
                    return true;
                }
                return false;
            }

            // --- Auto-Title via Model ---
            function autoTitle(chatId, userMessage) {
                const body = {
                    model: modelSel.value,
                    input:
                        "Title this conversation in 3-5 words. No quotes, no punctuation, just the title. The message: " +
                        userMessage,
                    temperature: 0.3,
                    integrations: [],
                    incognito: true,
                };
                apiFetch("/api/chat", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify(body),
                })
                    .then((r) => r.json())
                    .then((data) => {
                        let title = extractContent(data) || "";
                        title = title
                            .trim()
                            .replace(/^["']|["']$/g, "")
                            .replace(/\.$/, "")
                            .slice(0, 60);
                        if (!title || title.length < 2) return;
                        if (chatMeta[chatId]) {
                            chatMeta[chatId].title = title;
                            apiFetch(`/api/chats/${chatId}/title`, {
                                method: "PATCH",
                                headers: { "Content-Type": "application/json" },
                                body: JSON.stringify({ title }),
                            });
                            renderList();
                        }
                    })
                    .catch(() => {}); // silent fail — keep fallback title
            }

            // --- Context Window Gauge ---
            function resetCtxGauge() {
                const bar = $("ctx-bar");
                if (bar) { bar.style.width = "0%"; bar.style.background = "var(--accent)"; }
                const lbl = $("ctx-label");
                if (lbl) lbl.textContent = "Context ready";
            }

            function updateCtxGauge(inputTokens, ctxLength) {
                if (!inputTokens || !ctxLength) return;
                const pct = Math.min((inputTokens / ctxLength) * 100, 100);
                const bar = $("ctx-bar");
                bar.style.width = pct + "%";
                // Color: accent(low) → amber(70%) → rose(85%)
                if (pct < 70) bar.style.background = "var(--accent)";
                else if (pct < 85)
                    bar.style.background =
                        "linear-gradient(90deg,var(--accent),var(--accent-hover))";
                else
                    bar.style.background =
                        "linear-gradient(90deg,var(--accent-hover),var(--gauge-warn))";
                $("ctx-label").textContent =
                    inputTokens.toLocaleString() +
                    " / " +
                    ctxLength.toLocaleString() +
                    " tokens (" +
                    Math.round(pct) +
                    "%)";
            }

            async function triggerCompact() {
                if (!activeId) {
                    addErr("No active conversation to compact.");
                    return;
                }
                const model = modelSel.value;
                addAsst("Compacting conversation...");
                try {
                    const resp = await apiFetch(
                        `/api/chats/${activeId}/compact`,
                        {
                            method: "POST",
                            headers: { "Content-Type": "application/json" },
                            body: JSON.stringify({ model }),
                        },
                    );
                    if (!resp.ok) {
                        const text = await resp.text();
                        try {
                            const j = JSON.parse(text);
                            addErr(j.error || `Compact failed (${resp.status})`);
                        } catch {
                            addErr(`Compact failed (${resp.status})`);
                        }
                        return;
                    }
                    const data = await resp.json();
                    addAsst(
                        `**Context compacted.** Summarized ${data.messages_summarized} messages (deleted ${data.messages_deleted}).\n\n> ${data.summary}`,
                    );
                    addCopyButtons();
                    scroll.scrollTop = scroll.scrollHeight;
                } catch (e) {
                    addErr("Compact failed: " + e.message);
                }
            }

            // --- Conversation Forking ---
            async function forkFromMsg(el) {
                if (!activeId) return;
                // Find the message element
                const msgEl = el.closest(".m-user,.m-asst");
                if (!msgEl) return;
                const msgId = msgEl.dataset.msgId;
                if (!msgId) {
                    // If no msgId, need to find the last known msgId at or before this point
                    // Walk backwards through siblings to find one with a msgId
                    let node = msgEl;
                    let foundId = null;
                    while (node) {
                        if (node.dataset && node.dataset.msgId) {
                            foundId = node.dataset.msgId;
                            break;
                        }
                        node = node.previousElementSibling;
                    }
                    if (!foundId) {
                        addErr("Cannot fork: message not yet saved");
                        return;
                    }
                    await doFork(foundId);
                } else {
                    await doFork(msgId);
                }
            }

            async function doFork(upToMsgId) {
                try {
                    const meta = chatMeta[activeId];
                    const resp = await apiFetch(`/api/chats/${activeId}/fork`, {
                        method: "POST",
                        headers: { "Content-Type": "application/json" },
                        body: JSON.stringify({
                            up_to_message_id: parseInt(upToMsgId),
                            model: modelSel.value,
                        }),
                    });
                    const data = await resp.json();
                    if (data.error) {
                        addErr(data.error);
                        return;
                    }
                    // Add to chatMeta and switch to it
                    chatMeta[data.id] = {
                        id: data.id,
                        title: data.title,
                        model: modelSel.value,
                        response_id: data.response_id ?? null,
                        updated_at: Date.now() / 1000,
                        pinned: 0,
                        folder: "",
                    };
                    renderList();
                    await loadChat(data.id);
                } catch (e) {
                    addErr("Fork failed: " + e.message);
                }
            }

            // --- Quick Model Pill ---
            function getModelShortName(id) {
                if (!id) return "?";
                const m = cachedModels.find((x) => x.id === id);
                if (m?.identifier) return m.identifier;
                const parts = id.split("/");
                return parts[parts.length - 1];
            }

            function updateModelPill() {
                const m = cachedModels.find((x) => x.id === modelSel.value);
                const pill = $("model-pill");
                if (m) {
                    const caps = m.capabilities || {};
                    let icons = "";
                    if (caps.vision)
                        icons += capIcon(ICON_VISION_PATH, "var(--accent)");
                    if (caps.trained_for_tool_use)
                        icons += capIcon(ICON_TOOLS_PATH, "var(--green)");
                    pill.innerHTML = esc(getModelShortName(m.id)) + icons;
                    pill.style.display = "";
                } else if (modelSel.value) {
                    pill.textContent = getModelShortName(modelSel.value);
                    pill.style.display = "";
                } else {
                    pill.style.display = "none";
                }
                pill.title =
                    modelSel.value ?
                        "Model: " + modelSel.value
                    :   "No model selected";
            }

            function renderModelDD(ddId) {
                const dd = $(ddId);
                if (!dd) return;
                const triggerId = ddId === "model-dd" ? "model-pill" : "model-sel-wrap";
                const trigger = $(triggerId);
                if (dd.classList.contains("open")) {
                    dd.classList.remove("open");
                    if (trigger) trigger.setAttribute("aria-expanded", "false");
                    return;
                }
                dd.innerHTML = "";
                if (!cachedModels.length) {
                    dd.innerHTML =
                        '<div class="mdd-empty">No models loaded</div>';
                    dd.classList.add("open");
                    if (trigger) trigger.setAttribute("aria-expanded", "true");
                    return;
                }
                cachedModels.forEach((m) => {
                    const isLoaded = (m.loaded_instances || []).length > 0;
                    const caps = m.capabilities || {};
                    const item = document.createElement("button");
                    item.className =
                        "mdd-item" +
                        (m.id === modelSel.value ? " active" : "") +
                        (isLoaded ? "" : " unloaded");
                    let meta = "";
                    if (caps.vision)
                        meta +=
                            '<span class="mdd-cap"><svg viewBox="0 0 20 20"><path fill="currentColor" d="' +
                            ICON_VISION_PATH +
                            '"/></svg>Vision</span>';
                    if (caps.trained_for_tool_use)
                        meta +=
                            '<span class="mdd-cap"><svg viewBox="0 0 20 20"><path fill="currentColor" d="' +
                            ICON_TOOLS_PATH +
                            '"/></svg>Tools</span>';
                    if (isLoaded)
                        meta += '<span class="mdd-status loaded">Loaded</span>';
                    item.innerHTML =
                        '<span class="mdd-name">' +
                        esc(modelDisplayName(m)) +
                        "</span>" +
                        (meta ?
                            '<div class="mdd-meta">' + meta + "</div>"
                        :   "");
                    item.onclick = (e) => {
                        e.stopPropagation();
                        modelSel.value = m.id;
                        localStorage.setItem("lsc-model", m.id);
                        updateModelPill();
                        updateTopModelLabel();
                        syncModelSettings();
                        dd.classList.remove("open");
                        if (trigger) trigger.setAttribute("aria-expanded", "false");
                    };
                    dd.appendChild(item);
                });
                dd.classList.add("open");
                if (trigger) trigger.setAttribute("aria-expanded", "true");
            }
            function toggleModelDD() {
                renderModelDD("model-dd");
            }
            function toggleTopModelDD() {
                renderModelDD("top-model-dd");
            }
            function updateTopModelLabel() {
                const m = cachedModels.find((x) => x.id === modelSel.value);
                $("model-sel-label").innerHTML =
                    m ?
                        modelLabelHtml(m)
                    :   esc(modelSel.value || "Select model");
            }
            // Close model dropdowns on outside press-and-release.
            _globalTeardowns.push(onOutside(
                () => $("model-pill"),
                () => {
                    $("model-dd")?.classList.remove("open");
                    $("model-pill")?.setAttribute("aria-expanded", "false");
                },
            ));
            _globalTeardowns.push(onOutside(
                () => $("model-sel-wrap"),
                () => {
                    $("top-model-dd")?.classList.remove("open");
                    $("model-sel-wrap")?.setAttribute("aria-expanded", "false");
                },
            ));

            // --- Init ---
            // Connection-check interval handle — tracked so logout can stop
            // it before re-entering boot (otherwise every logout+re-login
            // session compounds another 30-second timer).
            let _connInterval = null;

            // Subscribe to state.boot so any phase failure is at least
            // surfaced in the console until Phase 7 builds the visible
            // banner.  Otherwise users + dev testers debug an empty shell
            // with zero diagnostic signal.
            subscribe(["boot"], () => {
                if (state.boot.error) {
                    console.warn(
                        `[boot] phase "${state.boot.error.phase}" failed:`,
                        state.boot.error.message,
                    );
                }
            });

            // -----------------------------------------------------------------
            // Phased boot.  Each phase advances state.boot.phase before its
            // work starts and only completes when all of its async work has
            // settled.  Phase failures are captured on state.boot.error so
            // the SPA can render a banner with a retry, instead of silently
            // showing an empty shell.  The previous initApp() was a serial
            // pile of awaits that buried per-step failures in console.error
            // calls.  Now each step is named and the user sees outcomes.
            // -----------------------------------------------------------------
            async function _bootPhaseProfile() {
                // Models, server settings, connection status — all parallel
                // because they're independent.  Promise.allSettled means one
                // failure doesn't poison the others; we collect the bad ones
                // and surface a banner.
                const tasks = [
                    { name: "connection", run: () => checkConnection() },
                    { name: "settings",   run: () => loadSettings() },
                    { name: "version",    run: async () => {
                        try {
                            const h = await apiFetch("/api/health");
                            const j = await h.json();
                            appVersion = j.version || "";
                        } catch (e) {
                            console.error("Failed to fetch app version:", e);
                        }
                    } },
                ];
                const results = await Promise.allSettled(tasks.map((t) => t.run()));
                const failed = results
                    .map((r, i) => r.status === "rejected" ? tasks[i].name : null)
                    .filter(Boolean);
                if (failed.length) {
                    throw new Error(`Profile phase failed: ${failed.join(", ")}`);
                }
            }

            async function _bootPhaseConversations() {
                await loadChatList();
                // Honour the URL on first paint.  A user landing on
                // /chat/abc123 should see that chat, not a default
                // "open the latest" override.  Falls back to latest-chat
                // when the route is welcome AND the user has chats.
                const initial = ROUTES.parse(window.location.pathname);
                const ids = Object.keys(chatMeta);
                if (initial.name === "chat" && initial.chatId && chatMeta[initial.chatId]) {
                    await loadChat(initial.chatId);
                } else if (initial.name === "chat" && initial.chatId && !chatMeta[initial.chatId]) {
                    // Deep link to a chat that's been deleted (or never
                    // belonged to this user).  Show welcome + a clear
                    // toast so the user understands why the URL didn't
                    // land them where they expected.
                    renderWelcome();
                    history.replaceState({}, "", "/");
                    toast.error("That chat doesn't exist or has been deleted.");
                } else if (initial.name === "settings") {
                    if (ids.length > 0) {
                        const latest = ids.sort(
                            (a, b) =>
                                (chatMeta[b].updated_at || 0) -
                                (chatMeta[a].updated_at || 0),
                        )[0];
                        await loadChat(latest);
                    }
                    openSettings(initial.tab);
                } else if (initial.name === "pins") {
                    if (ids.length > 0) {
                        const latest = ids.sort(
                            (a, b) =>
                                (chatMeta[b].updated_at || 0) -
                                (chatMeta[a].updated_at || 0),
                        )[0];
                        await loadChat(latest);
                    }
                    openRightPanel("pins");
                } else if (ids.length > 0) {
                    const latest = ids.sort(
                        (a, b) =>
                            (chatMeta[b].updated_at || 0) -
                            (chatMeta[a].updated_at || 0),
                    )[0];
                    await loadChat(latest);
                } else {
                    renderWelcome();
                }
            }

            async function initApp() {
                try {
                    setState({ boot: { phase: "profile", error: null } });
                    await _bootPhaseProfile();
                    setState({ boot: { phase: "conversations" } });
                    await _bootPhaseConversations();
                } catch (err) {
                    console.error("Boot phase failed:", err);
                    setState({ boot: { error: { phase: state.boot.phase, message: err.message || String(err) } } });
                    // Don't return — best-effort continue.  The user can
                    // see the partial state and retry the failed phase via
                    // the banner once Phase 7 (toast/error system) lands;
                    // for now the console-error is the breadcrumb.
                }
                // Best-effort post-boot work — none of this is required for
                // the SPA to be usable.
                const draft = localStorage.getItem("lsc-draft");
                if (draft) {
                    // Lock-in drafts are JSON-encoded ({preset, text}).
                    // Plain strings remain backward-compatible.
                    if (draft.startsWith("{")) {
                        try {
                            const parsed = JSON.parse(draft);
                            if (parsed && typeof parsed === "object") {
                                input.value = parsed.text || "";
                                if (parsed.preset) {
                                    setPendingPreset(parsed.preset);
                                }
                            } else {
                                input.value = draft;
                            }
                        } catch {
                            input.value = draft;
                        }
                    } else {
                        input.value = draft;
                    }
                    input.style.height = "auto";
                    input.style.height =
                        Math.min(input.scrollHeight, 160) + "px";
                    updateSendBtn();
                }
                updateSidebarStats();
                // Track the connection-check interval so logout can stop it.
                // Without this, every logout+re-login session compounds an
                // additional 30-second polling timer.
                if (_connInterval) clearInterval(_connInterval);
                _connInterval = setInterval(checkConnection, 30000);
                // "ready" means async boot work has completed — NOT that
                // the DOM is fully settled.  Some setState calls from the
                // conversations phase (e.g. loadChat → renderList) may
                // still have pending RAF subscribers when this flips.
                // Don't gate flash-of-empty-shell UI on this; use the
                // specific data keys (state.chats, state.models) instead.
                setState({ boot: { phase: "ready" } });
            }

            (async function boot() {
                setState({ boot: { phase: "auth" } });
                const authed = await checkAuth();
                if (authed) await initApp();
                else setState({ boot: { phase: "ready" } });  // login screen owns the UI
            })();
            if ("serviceWorker" in navigator)
                // SW registration failures (file 404, scope mismatch,
                // private-window restrictions) must not break the page —
                // the app works fine without the PWA install prompt.
                navigator.serviceWorker.register("/sw.js").catch(() => {});

// --- Expose functions needed by dynamically-rendered HTML (innerHTML) ---
Object.assign(window, {
    doAuth, closeSB, openSB, newChat, closeSettings, openSettings,
    filterChats, clearChatSearch, toggleSearchMode, toggleIncognito,
    showKBShortcuts, hideKBShortcuts, toggleExportDD, exportChat,
    shareChat, scrollToBottom, useStarter, switchSettingsTab,
    applyPreset, toggleMemory, addInsightPrompt, refineInsights,
    clearInsights, deleteAll, saveServerSettings, clearApiKey,
    toggleDebugMode, addRemoteMcp, removeRemoteMcp, setMcpAuth,
    addStarter, updateStarter, removeStarter, resetStarters,
    saveProfile, doSettingsChangePassword, doSettingsInvite,
    doLogout, startTotpSetup, verifyTotpSetup, disableTotp,
    toggleModelDD, toggleTopModelDD, toggleUserDD, startEdit,
    saveEdit, cancelEdit, forkFromMsg, retryLast, regenerate,
    triggerCompact, handleFiles, removeAttachment, unshareChat,
    closeRightPanel, openRightPanel, togglePinNavigator,
    pinMessage, unpinMessage, loadPinNavigator, scrollToMessage,
    toast, navigate, withBusy, onOutside, openPopover,
    // Slash-command lock-in: the cmd-badge click handler in
    // ``initEventHandlers`` calls this to exit the active mode.
    clearActiveMode,
    // State primitives — exposed for E2E tests + console debugging.
    // Production reads should still go through subscribe(); window
    // access bypasses the dispatch contract.
    state, setState, subscribe, effectiveReasoning,
});

function initEventHandlers() {
    // Auth
    document.getElementById('auth-btn')?.addEventListener('click', () => doAuth());
    document.getElementById('a-pass')?.addEventListener('keydown', e => { if (e.key === 'Enter') doAuth(); });

    // Sidebar
    document.getElementById('sb-overlay')?.addEventListener('click', () => closeSB());
    document.getElementById('new-chat')?.addEventListener('click', () => newChat().catch((e) => addErr("Failed to create chat: " + e.message)));
    document.getElementById('close-sb')?.addEventListener('click', () => closeSB());
    document.getElementById('chat-search')?.addEventListener('input', () => filterChats());
    document.getElementById('chat-search-clear')?.addEventListener('click', () => clearChatSearch());
    document.getElementById('search-mode')?.addEventListener('click', () => toggleSearchMode());

    // Context gauge
    const ctxGauge = document.getElementById('ctx-gauge');
    ctxGauge?.addEventListener('click', () => triggerCompact());
    ctxGauge?.addEventListener('keydown', e => {
        if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); triggerCompact(); }
    });

    // Topbar
    document.getElementById('open-sb')?.addEventListener('click', () => openSB());
    const modelSelWrap = document.getElementById('model-sel-wrap');
    modelSelWrap?.addEventListener('click', () => toggleTopModelDD());
    modelSelWrap?.addEventListener('keydown', e => {
        if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); toggleTopModelDD(); }
    });

    // Export
    document.getElementById('export-btn')?.addEventListener('click', () => toggleExportDD());
    document.querySelector('[data-action="export-md"]')?.addEventListener('click', () => exportChat('md'));
    document.querySelector('[data-action="export-json"]')?.addEventListener('click', () => exportChat('json'));

    // Topbar actions
    document.getElementById('incognito-btn')?.addEventListener('click', () => toggleIncognito());
    document.getElementById('share-btn')?.addEventListener('click', () => shareChat());
    document.querySelector('.tb[title="Keyboard shortcuts"]')?.addEventListener('click', () => showKBShortcuts());
    document.getElementById('chat-settings-btn')?.addEventListener('click', () => openRightPanel('settings'));
    document.getElementById('global-settings-btn')?.addEventListener('click', () => openSettings());

    // User avatar/dropdown.
    // Only toggle when the click TARGET is the avatar itself — without
    // this, clicks on the Settings/Sign Out buttons inside #user-dd
    // (which is a child of #user-avatar) bubble up here and re-open
    // the dropdown immediately after the inner button handler closed it.
    const userAvatar = document.getElementById('user-avatar');
    userAvatar?.addEventListener('click', (e) => {
        if (e.target === userAvatar) toggleUserDD();
    });
    userAvatar?.addEventListener('keydown', e => {
        if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); toggleUserDD(); }
    });
    // User DD buttons
    document.querySelector('#user-dd button:first-of-type')?.addEventListener('click', () => {
        document.getElementById('user-dd')?.classList.remove('open');
        openSettings();
    });
    document.querySelector('#user-dd button:last-child')?.addEventListener('click', () => doLogout());

    // Scroll
    document.getElementById('scroll-bottom')?.addEventListener('click', () => scrollToBottom());

    // Model pill in input area
    const modelPill = document.getElementById('model-pill');
    modelPill?.addEventListener('click', () => toggleModelDD());
    modelPill?.addEventListener('keydown', e => {
        if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); toggleModelDD(); }
    });

    // File input
    document.querySelector('#attach-btn input[type="file"]')?.addEventListener('change', function() {
        handleFiles(this.files);
        this.value = '';
    });

    // Slash button
    document.getElementById('slash-btn')?.addEventListener('click', () => {
        const inp = document.getElementById('input');
        if (inp) {
            inp.value = '/';
            inp.dispatchEvent(new Event('input', { bubbles: true }));
            inp.focus();
        }
    });

    // Command badge — clicking it exits the active mode (clears both the
    // compose-time ``pendingPreset`` and the sticky ``_activePreset`` on
    // the active chat).  A subsequent ``/cmd`` would re-enter the mode.
    document.getElementById('cmd-badge')?.addEventListener('click', () => {
        clearActiveMode();
        document.getElementById('input')?.focus();
    });

    // KB modal
    document.getElementById('kb-overlay')?.addEventListener('click', () => hideKBShortcuts());

    // Settings form
    document.getElementById('s-preset')?.addEventListener('change', () => applyPreset());
    document.getElementById('s-reasoning')?.addEventListener('change', function() {
        // Global default — persisted to localStorage AND broadcast via
        // setState so the in-chat cycle button + per-chat dropdown both
        // update on the next animation frame.
        localStorage.setItem('lsc-reasoning', this.value);
        setState({ serverInfo: { defaultReasoning: this.value } });
    });
    document.getElementById('s-followups')?.addEventListener('change', function() {
        localStorage.setItem('lsc-followups', this.checked ? 'on' : 'off');
    });
    document.getElementById('s-sc')?.addEventListener('change', function() {
        localStorage.setItem('lsc-sc', this.checked ? '1' : '0');
    });
    document.getElementById('s-cove')?.addEventListener('change', function() {
        localStorage.setItem('lsc-cove', this.checked ? '1' : '0');
    });
    document.getElementById('s-memory')?.addEventListener('change', function() {
        toggleMemory(this.checked);
    });
    document.querySelector('.danger[data-action="delete-all"]')?.addEventListener('click', () => deleteAll());

    // Memory actions
    document.getElementById('btn-refine')?.addEventListener('click', function() { refineInsights(this); });
    document.querySelector('[data-action="add-insight"]')?.addEventListener('click', () => addInsightPrompt());
    document.querySelector('[data-action="clear-insights"]')?.addEventListener('click', () => clearInsights());

    // Starters
    document.querySelector('[data-action="add-starter"]')?.addEventListener('click', () => addStarter());
    document.querySelector('[data-action="reset-starters"]')?.addEventListener('click', () => resetStarters());

    // Right panel
    document.querySelector('#right-panel .tb[title="Close"]')?.addEventListener('click', () => closeRightPanel());
    document.getElementById('right-panel-overlay')?.addEventListener('click', () => closeRightPanel());
}

document.addEventListener('DOMContentLoaded', initEventHandlers);

// iOS PWA: safe-area-inset-bottom stays fixed when keyboard opens — toggle via JS.
if (navigator.standalone && window.visualViewport) {
    const inputArea = document.getElementById('input-area');
    const naturalHeight = () => window.innerWidth > window.innerHeight
        ? Math.min(screen.width, screen.height)
        : Math.max(screen.width, screen.height);
    const applySab = () => {
        const keyboardActive = window.visualViewport.height < naturalHeight() - 40;
        inputArea.style.paddingBottom = keyboardActive ? '0px' : 'env(safe-area-inset-bottom, 0px)';
    };
    window.visualViewport.addEventListener('resize', applySab);
    applySab();
}

