# Network Availability Signal

**Status:** core API + Python surface + consumer wiring (connectivity
and v2transport) all landed. Default platform behavior is "always
available"; per-OS implementations (Apple / Android / Windows / Linux
desktop) are the remaining work. Single-repo (`ballistica-internal`).

**Started:** 2026-04-29

## Current state (read this first if picking up cold)

Pickup-friendly summary, organized by file. None of this is committed
yet (work-in-progress on `main`).

### C++ Platform abstraction — `src/ballistica/core/platform/platform.{h,cc}`

- `Platform::AddNetworkAvailabilityCallback(cb)` — public, non-virtual.
  Manages the callback list, change-dedup, and DEBUG-level logging on
  `ba.net`. Fires `cb` synchronously with the current value, then again
  on every change.
- `Platform::DoStartNetworkAvailabilityMonitoring()` — protected virtual
  hook, called once on first registration. Default impl is a no-op
  (value stays at `true`). Per-OS subclasses override to subscribe to
  `NWPathMonitor` / `ConnectivityManager` / `INetworkListManager` /
  NetworkManager-via-D-Bus.
- `Platform::SetNetworkAvailability(bool)` — protected, called by
  subclasses (and the debug toggler) when the OS reports a change.
  Thread-safe; can be invoked from any thread.
- **Debug toggle** — env var `BA_NETWORK_AVAILABILITY_DEBUG_TOGGLE=1`
  bypasses real platform monitoring and runs a detached thread that
  flips state every 5s, starting in `false`. Used for testing
  consumers without actually severing the network.

### Python binding — `src/ballistica/base/python/methods/python_methods_base_3.cc`

- `_babase.add_network_availability_callback(call)` — wraps the Python
  callable in a GIL-acquiring lambda before passing to `Platform`. No
  deregistration. Safe to call from any Python-running thread (no
  logic-thread precondition).

### Python public surface — `src/assets/ba_data/python/babase/_net.py`

- `NetworkSubsystem` registers an internal handler that updates
  `self._available`.
- `babase.app.net.available -> bool` is the public read-only property.
  Docstring spells out that `True` means "maybe online" — captive
  portals and ISP outages still report `True`, so callers still need
  real reachability probes; the property is really only useful for
  confirming *non-functional* states (airplane mode, ethernet
  unplugged, etc.).

### Connectivity subsystem — `src/meta/baplusmeta/pyembed/connectivity.py`

- `ConnectivityManager.network_available: bool` — local mirror,
  self-contained (subscribes via its own callback rather than reading
  `babase.app.net.available`, to avoid subsystem callback-ordering
  races).
- `_kick_event: asyncio.Event` — set on `False → True` transitions to
  wake the cycle out of its 3.85s sleep immediately.
- `_cycle_loop`'s sleep waits on shutdown OR kick OR timeout.
- `_fetch_basn_list` early-returns when `not network_available` —
  preserves the saved bootstrap address (no spurious "all-attempts-
  failed" path) and avoids per-cycle error-log spam.
- `_run_due_pings` and `_ping` also gate via the local field.

### v2transport subsystem — `src/meta/baplusmeta/pyembed/v2transport.py`

- `V2Transport.network_available: bool` — local mirror, same pattern.
- `_wake_event: asyncio.Event` — set on `False → True`.
- `_sleep_and_launch_primary_session` waits via
  `asyncio.wait_for(self._wake_event.wait(), timeout=sleep_seconds)`
  instead of plain `asyncio.sleep`. Either path falls through to the
  same "is primary still needed?" check, so the spawn logic stays in
  one place.
- `on_session_finished` only counts `_consecutive_errors += 1` when
  `network_available` is True. Gated failures don't escalate backoff —
  a brief offline window doesn't push us out of tier 0 (~2.4s base
  sleep) and into tier 1+ (5s+).
- `_establish_ws_endpoint` reads `self.parent.network_available`
  instead of `_babase.app.net.available`, for consistency with the
  gate-vs-real-failure logic upstream.

### Adjacent fixes that came out of this work

- **Silent exception swallowing in plus runtime.** `register_tenant`
  in `src/meta/baplusmeta/pyembed/plusruntime.py` was using
  `asyncio.run_coroutine_threadsafe(...)` and dropping the returned
  future. If `on_runtime_start()` raised, the exception vanished into
  the unused future. Now we attach a `done_callback` that calls
  `.result()` and logs any exception via `lifecyclelog.exception`.
  This is what hid a `BA_PRECONDITION` failure during this work; would
  have hid any other tenant-init failure too.
- **`add_network_availability_callback` precondition removed.** The
  binding originally had `BA_PRECONDITION(g_base->InLogicThread())`,
  which was an over-defensive default copied from sibling bindings.
  Registration is just storing a callable; doesn't need logic-thread
  exclusivity. Removed so subsystems running on the plus-runtime
  thread (ConnectivityManager, v2transport) can register directly.
- **Saved bootstrap address: never cleared.** Previously cleared on
  fetch failure. Removed both the call site and the underlying
  `clear_bootstrap_server_address` C++ binding + Python wrapper.
  Reasoning: the cascade entries are typically scheme variants of the
  same hostname backed by the same master server, so falling back to
  cascade on failure of the saved address buys nothing — the
  cascade fails too. Stale-after-build-update is handled by the
  `known_good in bootaddrs` check.
- **Reset triggers disabled.** `_run_cycle`'s time-jump and
  fg-state-change reset triggers are now under `if bool(False):` (only
  the initial-cycle reset fires). Comment above `_reset()` documents
  the reasoning. Re-enabling either is a one-character change.

### Connectivity-subsystem refactors that landed alongside

These weren't strictly necessary for the network-availability work but
got pulled in because the same surfaces needed touching. Worth knowing
about for future work in this area:

- **Pings in parallel.** `_run_due_pings` uses `asyncio.as_completed`
  + a `PING_CONCURRENCY=4` semaphore. Results are applied as they
  arrive, so geo-ignore-list narrowing happens during the cycle and
  not-yet-started pings see the narrowed set when they acquire the
  semaphore. Restores the "narrow as we go" property the old single-
  threaded loop got for free.
- **Two-tier ping pacing.** Replaced the previous five-tier scheme
  (1s/10s/30s/60s/120s based on ping value and success count) with
  a clean two-tier (`PING_INTERVAL_INITIAL_SECONDS=5.0` until
  `MIN_RESULT_COUNT` samples, then `PING_INTERVAL_STEADY_SECONDS=60.0`).
  Removed `NEAR_PING_CUTOFF` and the staggered-new-zone-scheduling
  code that used it.
- **Turbo mode dropped.** The 1.29s-vs-3.85s cycle cadence was needed
  back when transport startup waited on pings; that constraint is
  long gone. Single steady cadence at 3.85s.
- **Bootstrap fetch is pure asyncio.** `_PingTargetListFetch` no
  longer spawns raw `threading.Thread`s. Each per-bootstrap-address
  attempt is an async coroutine; staggered start delays use
  `asyncio.sleep`; the blocking `urllib3.request` runs via
  `asyncio.to_thread`; the orchestrator waits for first-success-or-
  all-finished via `asyncio.wait({done_signal, all_complete},
  return_when=FIRST_COMPLETED)` (no outer timeout — each request is
  already bounded by urllib3's own 10s).

### test_game_run flag additions — `tools/batools/pcommands3.py`

- `--reset-connectivity` → sets `BA_CONNECTIVITY_RESET=1`
- `--debug-network-toggle` → sets `BA_NETWORK_AVAILABILITY_DEBUG_TOGGLE=1`

These exist as flags rather than env-var prefixes so the sandbox
permission grant for `test_game_run` persists across invocations.
The general "prefer pcommand flags over `BA_FOO=1` prefixes" pattern
is documented in `~/.claude/CLAUDE.md` and the `/baclient` skill.

### End-to-end test

`tests/test_plus/test_network_availability_gating.py` boots a fresh
headless client with `BA_NETWORK_AVAILABILITY_DEBUG_TOGGLE=1`, then
asserts on the captured log stream:

1. `Network availability changed: true` appears (debug toggle is
   working).
2. `Fetching ping-target-list` appears, after the flip and within
   2s (connectivity gating + kick worked).
3. `Trying WS connection` appears, after the flip and within 2s
   (v2transport gating + wake-event worked).
4. All pre-flip `No transport-sessions remain; will spawn new one
   in N.NNs` log entries have `N <= 3.5s` — i.e., backoff stayed
   in tier 0 across the gated period (gated failures aren't being
   counted toward `_consecutive_errors`).

Test runs in ~7.4s. Skipped under `BA_TEST_FAST_MODE=1` (so plain
`make test` skips it); runs under `make test-ex` and the live-test
paths.

## What's *not* done yet

- **Per-OS implementations.** Apple `NWPathMonitor`, Android
  `ConnectivityManager.registerDefaultNetworkCallback`, Windows
  `INetworkListManager` (or WinRT alternative), Linux NetworkManager
  via D-Bus. Apple is the obvious first target (cleanest API,
  deployment targets well-covered).
- **Hysteresis on rapid availability bounces.** YAGNI for now; cycles
  are cheap when there's nothing to do.
- **Change-event surface for casual Python consumers** (UI badges
  etc.). First cut is read-only property only. ConnectivityManager
  and v2transport handle their own subscriptions. Wait until a real
  UI consumer.
- **Defensive handling for callbacks-during-shutdown.** The
  `call_soon_threadsafe(self._kick_event.set)` /
  `call_soon_threadsafe(self._wake_event.set)` calls would raise
  `RuntimeError: Event loop is closed` if the platform fires a
  callback after the runtime loop has stopped. Currently no callback
  source can do this (the detached debug-toggle thread dies at
  process exit), but real per-OS impls might. See `docs/followups.md`
  Connectivity section for the full note.

## Goal

Provide an OS-backed "is the network path available right now?"
signal that the engine and Python can latch onto, so persistent
network activity (connectivity pings, transport agent retries, etc.)
can short-circuit when the device is clearly offline.

Primary motivation: avoid burning CPU / battery / data on doomed
network attempts. Secondary: a foundation for any future "show
offline state in UI" work, though that's not the immediate driver.

## Non-goals

- **Not** a replacement for actual reachability / health probes. The
  signal is a strict "definitely offline; don't try" gate, not an
  "internet really works" guarantee. Existing probes still run when
  the path is available.
- **Not** a UX surface (yet). Internal request-gating only.

## Key decisions

### 1. Path-only signal, not "validated internet"

Android's `NET_CAPABILITY_VALIDATED` and Windows'
`NLM_CONNECTIVITY_*_INTERNET` flags do real captive-portal probes
(Android hits a Google 204 endpoint). Tempting, but they have real
false negatives: blocked-in-China, corporate egress filters,
Pi-hole/custom DNS, validation-startup-lag.

If we suppressed requests on `validated=false`, a non-trivial slice
of users would see the app silently refuse to talk to our servers
even when it could. Path-only avoids that — it's the strict subset
that's universally true.

Validated is more useful as a **UX signal** ("limited connectivity"
badge) than a request gate. The abstraction can expose it later as
an optional second tier.

### 2. Callback-based, not polling

All four target OS APIs are callback-native:

- Apple `NWPathMonitor` — `pathUpdateHandler` block on a dispatch
  queue.
- Android `ConnectivityManager.registerDefaultNetworkCallback` —
  `onAvailable`/`onLost`/`onCapabilitiesChanged` on a Handler.
- Windows `INetworkListManager` — COM connection-point events.
  Uglier plumbing, same shape.
- Linux NetworkManager — D-Bus `StateChanged` signal.

Callbacks are essentially free — the OS already tracks this for the
radio stack. Polling would wake the process unnecessarily.

### 3. Default to "available" until proven otherwise

The signal is asymmetric — it only ever *suppresses* requests when
there's positive evidence of offline:

- Platforms not yet wired up → "available" (today's behavior
  preserved).
- At startup, before the first OS callback fires → "available."
- Errors in the platform layer → "available."

Means partial rollout doesn't regress behavior elsewhere, and any
bug in the abstraction biases toward "still try" rather than
"silently refuse."

### 4. Home in `core/platform`, not `base/app_platform`

`core/Platform` already has the closest sibling shape —
`RequestPermission`/`HavePermission`, `IsRunningOnTV`,
`GetOSVersionString` — pure OS-state queries with per-OS overrides.
Network reachability is the same shape.

`AppPlatform` is heavily app-feature stuff (login adapters,
purchases, web browser overlay) — wrong neighborhood. `core` is also
a strict dep of `base`, so anything in the engine can subscribe.

### 5. Callback contract: any thread, no deregistration

Callbacks may fire on **any thread**, including synchronously inside
the registration call. Callers handle their own thread routing.

Reasoning: `core` is below `base` and doesn't know about the logic
thread, so binding to logic-thread delivery would be a layering
violation. Per-OS impls also benefit — Apple can hand the
`pathUpdateHandler` block straight to its dispatch queue with no
extra hop.

No deregistration: registrations live for the app's lifetime.

### 6. Base class owns dispatch + logging; subclasses are minimal

`AddNetworkAvailabilityCallback` is non-virtual. Subclasses can't
intercept it; they override `DoStartNetworkAvailabilityMonitoring()`
and call `SetNetworkAvailability(value)` on each OS-reported
change. Every per-OS impl gets the change-log line, dedup, and
dispatch machinery for free.

### 7. Each subsystem mirrors availability locally

ConnectivityManager and v2transport each subscribe to the platform
callback directly and maintain their own `network_available: bool`
field — they don't read `babase.app.net.available`.

Reasoning: avoids cross-subsystem callback-ordering races. If
ConnectivityManager registers later than NetworkSubsystem, reading
the latter's property could see a stale value momentarily after a
callback. With each subsystem owning a local mirror, ordering is
irrelevant — each one's local field reflects whatever C++ delivered
to *its* callback most recently. The bool write/read is GIL-atomic;
the kick scheduling uses `call_soon_threadsafe`. Both are safe from
any thread.

`babase.app.net.available` remains as the convenient public read
for casual consumers (UI badges, etc.) that just want "right now,
is it available?" and don't need to react to changes.

### 8. Push-based wake events, not poll-based reset

When availability flips `False → True`, both subsystems' subscribed
handlers fire `_kick_event.set()` / `_wake_event.set()` to interrupt
their respective sleeps. Event-driven recovery is much faster than
waiting for the next scheduled tick (~14s worst case for connectivity,
up to ~39s for v2transport's tier-4+ backoff).

Implementation pattern (used by both subsystems):

- The "what to do on wake" is in the existing tail of the sleep
  function; the wake just changes how we exit the sleep, not what
  happens after. So the spawn / fetch logic stays in one place.
- `await asyncio.wait_for(self._event.wait(), timeout=N)` — falls
  through naturally on either timeout or event firing. Cleaner than
  cancel-and-respawn.
- Event is cleared after the wake so subsequent sleeps start fresh.

### 9. Don't count gated failures toward backoff

v2transport's `_consecutive_errors += 1` is gated on
`self.network_available`. Without this, a 30-second offline window
where the gate fails ~5 sessions would push us into tier-2 backoff
(~5s base sleep). The wake event would still interrupt that sleep on
recovery, but we'd lose any genuine error-rate signal — every offline
window would look like 5 server failures.

ConnectivityManager doesn't have a comparable `_consecutive_errors`
counter (its retry rate is just the cycle period), so this only
applies to v2transport.

### 10. Saved bootstrap address never cleared

Once a bootstrap address resolves successfully in a process, it
stays put for the lifetime of the process. The cascade entries are
scheme variants of the same hostname; if the saved one fails, the
others almost certainly fail too. Process restart starts fresh.
Stale-after-build-update is handled by the `known_good in
bootaddrs` check.

### 11. Reset disabled (kept-in-place)

`_reset()`'s time-jump and fg-state-change triggers are gated by
`if bool(False):`. Only the initial-cycle reset fires. Reasoning
captured in a block-comment above `_reset()`: the scenarios reset
handles (mid-session continent move, mid-session VPN-exit toggle,
sleep/wake onto a very different network) produce
suboptimal-but-functional latency, not breakage; geo-ignore lock-in
is the one user-visible artifact and process restart fully recovers
it. The cost of keeping reset is real complexity (it has to be
correctly threaded through any future state we add). Implementation
left in place for cheap re-enable; will likely remove fully if
ongoing experience confirms the cost-benefit.

### 12. Debug toggle is a detached thread; small shutdown race accepted

`BA_NETWORK_AVAILABILITY_DEBUG_TOGGLE=1` spawns a
`std::thread(...).detach()` running an infinite 5s-toggle loop.
Tiny shutdown-race window (5s sleep, fast process teardown) accepted
since the feature is opt-in. Comment at the spawn site documents the
joinable-thread upgrade path if we ever want to close it.

## Platform feasibility

| Platform | API | Notes |
|----------|-----|-------|
| Apple (macOS / iOS / tvOS) | `NWPathMonitor` | iOS 12+ / macOS 10.14+. Deployment targets well above. |
| Android | `ConnectivityManager.registerDefaultNetworkCallback` | API 24+. `minSdk` is 24 — exactly on the line. Manifest needs `ACCESS_NETWORK_STATE` (likely already present). |
| Windows | `INetworkListManager` + `INetworkListManagerEvents` | NLA, Vista+. Alternative: WinRT `NetworkInformation.NetworkStatusChanged` (Win 8+) for less COM plumbing. |
| Linux desktop | NetworkManager via D-Bus (`StateChanged`) | Present on virtually all desktop distros. Fallback for unusual installs: netlink (`RTM_NEWLINK`/`RTM_DELLINK`) or just default to "available." |
| Oculus / Quest | Same as Android | Runs on Android. |
| Headless Linux server | Default to "available" | Cloud VMs are essentially always online; battery/data motivation doesn't apply. |

## Anti-scope (resist drift)

- **Don't build a full network state machine** (interface type,
  expensive, constrained, etc.) until a consumer needs it. The gate
  is one bit: "definitely offline" vs. "maybe online."
- **Don't expose validated-internet as the primary signal** even if
  a platform provides it for free. Optional secondary tier only.
- **Don't add a polling fallback "just in case."** Every target
  platform has a callback API; use it.
- **Don't add deregistration unless a consumer that genuinely needs
  it appears.** Keeps the API surface and per-OS-impl machinery
  smaller.
- **Don't read `babase.app.net.available` from internal subsystems
  with reactive needs.** Subscribe + maintain a local mirror.

## References

- Conversations that produced this doc: 2026-04-29 design session,
  2026-04-30 / 2026-05-01 / 2026-05-02 implementation sessions
  (`network-available`).
- Sibling abstractions: `RequestPermission`/`HavePermission`,
  `IsRunningOnTV` in `src/ballistica/core/platform/platform.h`.
- Cross-thread Python callback pattern:
  `Python::ScopedInterpreterLock` in
  `src/ballistica/shared/python/python.h`.
- Coordinated subsystem pattern: ConnectivityManager and v2transport
  in `src/meta/baplusmeta/pyembed/`.
- End-to-end test:
  `tests/test_plus/test_network_availability_gating.py`.
