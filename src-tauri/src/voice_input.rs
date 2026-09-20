//! Turns wheel/gamepad button presses and the global PTT hotkey into voice:press /
//! voice:release events for the webview, gated to when LMU or this app itself has the
//! foreground window (`foreground_allows`, #112) -- settable via `PollerState.focus_gate`
//! for VR, where the desktop foreground may not be the game.
//! Spec: docs/superpowers/specs/2026-09-15-voice-loop-design.md.
use std::collections::HashSet;

pub struct Edge { pub device: String, pub button: u8, pub pressed: bool }
pub trait ButtonSource: Send { fn poll(&mut self) -> Vec<Edge>; }
#[derive(Clone, Default, serde::Deserialize)]
pub struct Binds { pub wheel: Option<String>, pub gamepad: Option<String> }
#[derive(Debug)]
pub enum Transition { Press, Release }

pub fn bind_id(edge: &Edge) -> String { format!("{}:{}", edge.device, edge.button) }

#[derive(Default)]
pub struct HeldSet { held: HashSet<String> }
impl HeldSet {
    pub fn new() -> Self { Self::default() }
    pub fn apply(&mut self, edge: &Edge, binds: &Binds) -> Option<Transition> {
        let id = bind_id(edge);
        let bound = [&binds.wheel, &binds.gamepad].iter().any(|b| b.as_deref() == Some(id.as_str()));
        if !bound { return None; }
        let was = !self.held.is_empty();
        if edge.pressed { self.held.insert(id); } else { self.held.remove(&id); }
        let now = !self.held.is_empty();
        match (was, now) { (false, true) => Some(Transition::Press), (true, false) => Some(Transition::Release), _ => None }
    }
}

pub struct PollerState {
    pub binds: Binds,
    pub capture_armed: bool,
    /// PTT only fires with LMU or this app focused (#112); off for VR, where the
    /// foreground desktop window may not be the game.
    pub focus_gate: bool,
    /// Whether the hotkey's press was actually emitted (i.e. not suppressed by the
    /// gate), so its release only fires to match -- and so `step`'s focus-loss path
    /// below can also release a hotkey-held press, the same as a wheel/gamepad one.
    pub hotkey_down: bool,
}

impl Default for PollerState {
    fn default() -> Self {
        Self { binds: Binds::default(), capture_armed: false, focus_gate: true, hotkey_down: false }
    }
}

pub enum Output { None, Transitions(Vec<Transition>), Captured(String) }

/// One poll cycle. Capture mode wins over PTT: the next pressed edge (from any
/// source) is reported as `Output::Captured` and never reaches `held`, so it
/// can never leave the PTT looking held.
///
/// `focused` is injected (from `foreground_allows()`) rather than queried here, so
/// this stays pure and testable without Win32 (#112).
pub fn step(
    sources: &mut [Box<dyn ButtonSource>],
    held: &mut HeldSet,
    state: &mut PollerState,
    focused: bool,
) -> Output {
    // Losing focus must release whatever's held -- a wheel/gamepad button or the
    // keyboard hotkey -- or the mic stays open forever; a further unfocused poll
    // emits nothing since both are already clear (no release storm).
    if state.focus_gate && !focused {
        let was_active = !held.held.is_empty() || state.hotkey_down;
        held.held.clear();
        state.hotkey_down = false;
        return if was_active { Output::Transitions(vec![Transition::Release]) } else { Output::None };
    }
    // No binds and not capturing: nothing this poll could produce would matter, so
    // skip touching winmm/XInput entirely rather than paying 125 Hz for a disabled
    // feature (voice off is the common case on a sim rig).
    if !state.capture_armed && state.binds.wheel.is_none() && state.binds.gamepad.is_none() {
        return Output::None;
    }
    let mut transitions = vec![];
    for s in sources.iter_mut() {
        for e in s.poll() {
            if state.capture_armed && e.pressed {
                state.capture_armed = false;
                return Output::Captured(bind_id(&e));
            }
            if let Some(t) = held.apply(&e, &state.binds) { transitions.push(t); }
        }
    }
    if transitions.is_empty() { Output::None } else { Output::Transitions(transitions) }
}

/// Maps a `KeyboardEvent.code` (as sent by the frontend's PTT-key capture) to the
/// key name the global-shortcut plugin expects: `KeyV` -> `V`, `Digit3` -> `3`,
/// anything else (already plugin-shaped, e.g. `F9`, `Space`) passed through.
pub fn hotkey_name(code: &str) -> String {
    if let Some(rest) = code.strip_prefix("Key") { return rest.to_string(); }
    if let Some(rest) = code.strip_prefix("Digit") { return rest.to_string(); }
    code.to_string()
}

// --- HID (#144): pure logic, kept free of Win32 handles so it's unit-testable. ---

/// Every usage whose pressed state changed between two reports, in a fixed (sorted)
/// order so the same two sets always produce the same edge order.
fn diff_pressed_usages(prev: &HashSet<u16>, now: &HashSet<u16>) -> Vec<(u16, bool)> {
    let mut changed: Vec<(u16, bool)> = prev
        .symmetric_difference(now)
        .map(|&u| (u, now.contains(&u)))
        .collect();
    changed.sort_by_key(|&(u, _)| u);
    changed
}

/// A usage value normalised so the device's lowest button usage is button 0 --
/// `buttonCaps` is a count of capability *ranges*, not a fixed bit offset, so the
/// button number is only ever meaningful relative to the device's own minimum usage.
fn normalize_usage(usage: u16, min_usage: u16) -> u8 {
    (usage - min_usage) as u8
}

/// Diffs two pressed-usage sets straight into `Edge`s for one HID device.
fn diff_to_edges(prev: &HashSet<u16>, now: &HashSet<u16>, device: &str, min_usage: u16) -> Vec<Edge> {
    diff_pressed_usages(prev, now)
        .into_iter()
        .map(|(usage, pressed)| Edge { device: device.to_string(), button: normalize_usage(usage, min_usage), pressed })
        .collect()
}

/// A device that vanished mid-hold must not leave `HeldSet` stuck (the same failure
/// mode #112 fixed for focus loss) -- every usage still pressed becomes a release.
fn releases_for_vanished(pressed: &HashSet<u16>, device: &str, min_usage: u16) -> Vec<Edge> {
    diff_to_edges(pressed, &HashSet::new(), device, min_usage)
}

/// Bind ids are `hid:<vid:04x>:<pid:04x>:<button>`, stable across replug and
/// power-cycle (unlike a winmm index). Parses one back to its parts.
fn parse_hid_bind(id: &str) -> Option<(u16, u16, u8)> {
    let rest = id.strip_prefix("hid:")?;
    let mut parts = rest.split(':');
    let vid = u16::from_str_radix(parts.next()?, 16).ok()?;
    let pid = u16::from_str_radix(parts.next()?, 16).ok()?;
    let button: u8 = parts.next()?.parse().ok()?;
    if parts.next().is_some() { return None; }
    Some((vid, pid, button))
}

/// Which of `seen`'s paths are newly arrived, and which of `known`'s are gone --
/// the bookkeeping for a 2s re-enumeration sweep. A path already known is neither
/// re-added nor removed.
fn diff_device_paths(known: &[String], seen: &[String]) -> (Vec<String>, Vec<String>) {
    let added = seen.iter().filter(|p| !known.contains(p)).cloned().collect();
    let removed = known.iter().filter(|p| !seen.contains(p)).cloned().collect();
    (added, removed)
}

#[cfg(windows)]
pub struct XInputSource { prev: [u16; 4] }
#[cfg(windows)]
impl XInputSource { pub fn new() -> Self { Self { prev: [0; 4] } } }
#[cfg(windows)]
impl ButtonSource for XInputSource {
    fn poll(&mut self) -> Vec<Edge> {
        use windows::Win32::UI::Input::XboxController::{XInputGetState, XINPUT_STATE};
        let mut out = vec![];
        for i in 0..4u32 {
            let mut st = XINPUT_STATE::default();
            // A device that errors (unplugged) is skipped silently -- other binds
            // keep working.
            if unsafe { XInputGetState(i, &mut st) } == 0 {
                let buttons = st.Gamepad.wButtons.0; // XINPUT_GAMEPAD_BUTTON_FLAGS newtype
                let prev = self.prev[i as usize];
                if buttons != prev {
                    let changed = buttons ^ prev;
                    for bit in 0..16u8 {
                        if changed & (1 << bit) != 0 {
                            out.push(Edge { device: format!("xinput:{i}"), button: bit, pressed: buttons & (1 << bit) != 0 });
                        }
                    }
                    self.prev[i as usize] = buttons;
                }
            }
        }
        out
    }
}

// ponytail: winmm caps a wheel at 32 buttons / 16 devices; DirectInput8 if a Fanatec
// bind above button 31 is ever wanted.
#[cfg(windows)]
pub struct WinmmSource { prev: Vec<u32> }
#[cfg(windows)]
impl WinmmSource {
    pub fn new() -> Self {
        use windows::Win32::Media::Multimedia::joyGetNumDevs;
        let n = unsafe { joyGetNumDevs() };
        Self { prev: vec![0u32; n as usize] }
    }
}
#[cfg(windows)]
impl ButtonSource for WinmmSource {
    fn poll(&mut self) -> Vec<Edge> {
        use windows::Win32::Media::Multimedia::{joyGetPosEx, JOYINFOEX, JOY_RETURNBUTTONS};
        let mut out = vec![];
        for i in 0..self.prev.len() as u32 {
            let mut ji = JOYINFOEX {
                dwSize: std::mem::size_of::<JOYINFOEX>() as u32,
                dwFlags: JOY_RETURNBUTTONS as u32,
                ..Default::default()
            };
            // A device that errors is skipped silently -- other binds keep working.
            if unsafe { joyGetPosEx(i, &mut ji) } == 0 {
                let buttons = ji.dwButtons; // JOYINFOEX is packed: copy to a local before use
                let prev = self.prev[i as usize];
                if buttons != prev {
                    let changed = buttons ^ prev;
                    for bit in 0..32u8 {
                        if changed & (1 << bit) != 0 {
                            out.push(Edge { device: format!("winmm:{i}"), button: bit, pressed: buttons & (1 << bit) != 0 });
                        }
                    }
                    self.prev[i as usize] = buttons;
                }
            }
        }
        out
    }
}

/// True when the foreground window belongs to LMU or to this app itself (#112).
/// Cached by HWND: this runs at 125 Hz, so a process query per poll is too much --
/// only a changed foreground HWND pays for one.
#[cfg(windows)]
pub fn foreground_allows() -> bool {
    use std::cell::Cell;
    use windows::core::PWSTR;
    use windows::Win32::Foundation::CloseHandle;
    use windows::Win32::System::Threading::{
        GetCurrentProcessId, OpenProcess, QueryFullProcessImageNameW, PROCESS_NAME_WIN32,
        PROCESS_QUERY_LIMITED_INFORMATION,
    };
    use windows::Win32::UI::WindowsAndMessaging::{
        GetForegroundWindow, GetWindowTextW, GetWindowThreadProcessId,
    };

    thread_local! {
        static CACHE: Cell<(isize, bool)> = Cell::new((0, false));
    }

    let hwnd = unsafe { GetForegroundWindow() };
    if hwnd.0.is_null() {
        return false;
    }
    let key = hwnd.0 as isize;
    let (cached_hwnd, cached_allowed) = CACHE.with(|c| c.get());
    if cached_hwnd == key {
        return cached_allowed;
    }

    let mut pid = 0u32;
    unsafe { GetWindowThreadProcessId(hwnd, Some(&mut pid as *mut u32)) };
    let allowed = if pid == unsafe { GetCurrentProcessId() } {
        true // our own window (desk PTT, Settings capture flow) always allowed
    } else {
        match unsafe { OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, false, pid) } {
            Ok(handle) => {
                let mut buf = [0u16; 260];
                let mut size = buf.len() as u32;
                let matched = unsafe {
                    QueryFullProcessImageNameW(handle, PROCESS_NAME_WIN32, PWSTR(buf.as_mut_ptr()), &mut size)
                }
                .is_ok()
                    && std::path::Path::new(&String::from_utf16_lossy(&buf[..size as usize]))
                        .file_stem()
                        .and_then(|s| s.to_str())
                        .is_some_and(|s| s.eq_ignore_ascii_case("Le Mans Ultimate"));
                unsafe { let _ = CloseHandle(handle); }
                matched
            }
            // EAC may refuse OpenProcess against the game itself; fall back to the
            // foreground window's title.
            Err(_) => {
                let mut buf = [0u16; 512];
                let len = unsafe { GetWindowTextW(hwnd, &mut buf) }.max(0) as usize;
                String::from_utf16_lossy(&buf[..len])
                    .to_ascii_lowercase()
                    .contains("le mans ultimate")
            }
        }
    };

    CACHE.with(|c| c.set((key, allowed)));
    allowed
}
#[cfg(not(windows))]
pub fn foreground_allows() -> bool {
    true
}

#[cfg(windows)]
fn default_sources() -> Vec<Box<dyn ButtonSource>> {
    // Both the wheel and the pad were seen through winmm at the rig (XInput printed
    // nothing for the pad); winmm is the primary source for every device, XInput
    // stays as a fallback for a pad that only registers via XInput.
    vec![Box::new(WinmmSource::new()), Box::new(XInputSource::new())]
}
#[cfg(not(windows))]
fn default_sources() -> Vec<Box<dyn ButtonSource>> { vec![] }

/// Runs the 8ms poll loop on its own thread for the life of the app, emitting
/// `voice:press` / `voice:release` / `voice:captured` to the webview.
pub fn start_poller(app: tauri::AppHandle, state: std::sync::Arc<std::sync::Mutex<PollerState>>) {
    use tauri::Emitter;
    std::thread::spawn(move || {
        let mut sources = default_sources();
        let mut held = HeldSet::new();
        loop {
            let focused = foreground_allows();
            let out = {
                let mut st = state.lock().unwrap();
                step(&mut sources, &mut held, &mut st, focused)
            };
            match out {
                Output::None => {}
                Output::Transitions(ts) => {
                    for t in ts {
                        let name = match t { Transition::Press => "voice:press", Transition::Release => "voice:release" };
                        let _ = app.emit(name, ());
                    }
                }
                Output::Captured(id) => {
                    let _ = app.emit("voice:captured", serde_json::json!({ "id": id }));
                }
            }
            std::thread::sleep(std::time::Duration::from_millis(8));
        }
    });
}

#[tauri::command]
pub fn voice_set_binds(
    binds: Binds,
    focus_gate: Option<bool>,
    state: tauri::State<std::sync::Arc<std::sync::Mutex<PollerState>>>,
) {
    let mut st = state.lock().unwrap();
    st.binds = binds;
    st.focus_gate = focus_gate.unwrap_or(true);
}

#[tauri::command]
pub fn voice_capture_next(state: tauri::State<std::sync::Arc<std::sync::Mutex<PollerState>>>) {
    state.lock().unwrap().capture_armed = true;
}

#[tauri::command]
pub fn voice_set_hotkey(
    app: tauri::AppHandle,
    key: Option<String>,
    state: tauri::State<std::sync::Arc<std::sync::Mutex<PollerState>>>,
) -> Result<(), String> {
    use tauri::Emitter;
    use tauri_plugin_global_shortcut::{GlobalShortcutExt, ShortcutState};
    app.global_shortcut().unregister_all().map_err(|e| e.to_string())?;
    if let Some(k) = key {
        let name = hotkey_name(&k);
        let poller_state = state.inner().clone();
        app.global_shortcut()
            .on_shortcut(name.as_str(), move |app, _sc, ev| {
                let mut st = poller_state.lock().unwrap();
                match ev.state() {
                    ShortcutState::Pressed => {
                        // Suppressed by the gate: emit nothing, and don't mark it down
                        // so the matching key-up doesn't emit a release either.
                        if !st.focus_gate || foreground_allows() {
                            st.hotkey_down = true;
                            let _ = app.emit("voice:press", ());
                        }
                    }
                    ShortcutState::Released => {
                        if st.hotkey_down {
                            st.hotkey_down = false;
                            let _ = app.emit("voice:release", ());
                        }
                    }
                }
            })
            .map_err(|e| e.to_string())?;
    }
    Ok(())
}

/// A poll-able device with a friendly name, for the Settings PTT-bind display (#104).
#[derive(Clone, serde::Serialize)]
pub struct DeviceInfo { pub device: String, pub name: String }

/// winmm's `szPname` fallback: the real product name unless it's the generic driver
/// string winmm reports when no OEM name was registered, in which case a numbered
/// placeholder is more honest than repeating "Microsoft PC-joystick driver" for every
/// unbranded stick.
pub fn oem_name_from(sz_pname: &str, i: u32) -> String {
    let trimmed = sz_pname.trim_end_matches('\0').trim();
    if trimmed.is_empty() || trimmed.eq_ignore_ascii_case("Microsoft PC-joystick driver") {
        format!("Joystick {}", i + 1)
    } else {
        trimmed.to_string()
    }
}

#[cfg(windows)]
fn reg_get_string(hive: windows::Win32::System::Registry::HKEY, subkey: &str, value: &str) -> Option<String> {
    use windows::core::PCWSTR;
    use windows::Win32::System::Registry::{RegGetValueW, RRF_RT_REG_SZ};
    let subkey_w: Vec<u16> = subkey.encode_utf16().chain(std::iter::once(0)).collect();
    let value_w: Vec<u16> = value.encode_utf16().chain(std::iter::once(0)).collect();
    let mut buf = [0u16; 256];
    let mut size = (buf.len() * 2) as u32;
    let ok = unsafe {
        RegGetValueW(
            hive,
            PCWSTR(subkey_w.as_ptr()),
            PCWSTR(value_w.as_ptr()),
            RRF_RT_REG_SZ,
            None,
            Some(buf.as_mut_ptr() as *mut _),
            Some(&mut size),
        )
    };
    if ok.is_ok() {
        let chars = (size as usize / 2).min(buf.len());
        let end = buf[..chars].iter().position(|&c| c == 0).unwrap_or(chars);
        Some(String::from_utf16_lossy(&buf[..end]))
    } else {
        None
    }
}

/// Windows stores the wheel/pad's driver-registered product name in the registry,
/// keyed by USB VID/PID, under HKCU (per-user) then HKLM (machine-wide) -- winmm's own
/// `szPname` is often just "Microsoft PC-joystick driver" for a generic HID device.
#[cfg(windows)]
fn registry_oem_name(vid: u16, pid: u16) -> Option<String> {
    use windows::Win32::System::Registry::{HKEY_CURRENT_USER, HKEY_LOCAL_MACHINE};
    let subkey = format!(
        "System\\CurrentControlSet\\Control\\MediaProperties\\PrivateProperties\\Joystick\\OEM\\VID_{vid:04X}&PID_{pid:04X}"
    );
    for hive in [HKEY_CURRENT_USER, HKEY_LOCAL_MACHINE] {
        if let Some(name) = reg_get_string(hive, &subkey, "OEMName") {
            if !name.is_empty() {
                return Some(name);
            }
        }
    }
    None
}

#[cfg(windows)]
fn winmm_device_name(i: u32) -> Option<String> {
    use windows::Win32::Media::Multimedia::{joyGetDevCapsW, JOYCAPSW};
    let mut caps = JOYCAPSW::default();
    let size = std::mem::size_of::<JOYCAPSW>() as u32;
    if unsafe { joyGetDevCapsW(i as usize, &mut caps, size) } != 0 {
        return None;
    }
    if let Some(name) = registry_oem_name(caps.wMid, caps.wPid) {
        return Some(name);
    }
    let sz_pname = caps.szPname; // JOYCAPSW is packed: copy to a local before use
    let pname = String::from_utf16_lossy(&sz_pname);
    Some(oem_name_from(&pname, i))
}

/// Lists every winmm/XInput slot that currently answers a poll -- unplugged devices are
/// left out, same as the poller itself skips them silently.
#[tauri::command]
pub fn voice_list_devices() -> Vec<DeviceInfo> {
    let mut out = vec![];
    #[cfg(windows)]
    {
        use windows::Win32::Media::Multimedia::{joyGetNumDevs, joyGetPosEx, JOYINFOEX, JOY_RETURNBUTTONS};
        use windows::Win32::UI::Input::XboxController::{XInputGetState, XINPUT_STATE};
        let n = unsafe { joyGetNumDevs() };
        for i in 0..n {
            let mut ji = JOYINFOEX {
                dwSize: std::mem::size_of::<JOYINFOEX>() as u32,
                dwFlags: JOY_RETURNBUTTONS as u32,
                ..Default::default()
            };
            if unsafe { joyGetPosEx(i, &mut ji) } == 0 {
                let name = winmm_device_name(i).unwrap_or_else(|| format!("Joystick {}", i + 1));
                out.push(DeviceInfo { device: format!("winmm:{i}"), name });
            }
        }
        for i in 0..4u32 {
            let mut st = XINPUT_STATE::default();
            if unsafe { XInputGetState(i, &mut st) } == 0 {
                out.push(DeviceInfo { device: format!("xinput:{i}"), name: format!("Xbox controller {}", i + 1) });
            }
        }
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn press_release_on_a_bound_button_only() {
        let binds = Binds { wheel: Some("winmm:0:7".into()), gamepad: Some("xinput:0:4".into()) };
        let mut h = HeldSet::new();
        let e = |d: &str, b: u8, p: bool| Edge { device: d.into(), button: b, pressed: p };
        assert!(h.apply(&e("winmm:0", 3, true), &binds).is_none());          // unbound
        assert!(matches!(h.apply(&e("winmm:0", 7, true), &binds), Some(Transition::Press)));
        assert!(h.apply(&e("xinput:0", 4, true), &binds).is_none());         // second device: still pressed
        assert!(h.apply(&e("winmm:0", 7, false), &binds).is_none());         // one still held
        assert!(matches!(h.apply(&e("xinput:0", 4, false), &binds), Some(Transition::Release)));
    }

    #[test]
    fn repeated_press_edges_do_not_repeat_the_transition() {
        let binds = Binds { wheel: Some("winmm:0:1".into()), gamepad: None };
        let mut h = HeldSet::new();
        let e = Edge { device: "winmm:0".into(), button: 1, pressed: true };
        assert!(h.apply(&e, &binds).is_some());
        assert!(h.apply(&e, &binds).is_none());
    }

    #[test]
    fn bind_id_format() {
        assert_eq!(bind_id(&Edge { device: "xinput:2".into(), button: 9, pressed: true }), "xinput:2:9");
    }

    struct Scripted(Vec<Vec<Edge>>);
    impl ButtonSource for Scripted { fn poll(&mut self) -> Vec<Edge> { if self.0.is_empty() { vec![] } else { self.0.remove(0) } } }

    fn poller_state(binds: Binds, capture_armed: bool, focus_gate: bool) -> PollerState {
        PollerState { binds, capture_armed, focus_gate, hotkey_down: false }
    }

    /// Runs `s:0:1` press/(nothing)/release through `step` across three focused polls.
    /// Shared by the baseline test and the focus_gate-off test (#112), which must behave
    /// identically even though `focused` is false throughout.
    fn press_release_over_three_polls(focus_gate: bool, focused: bool) -> Vec<Transition> {
        let binds = Binds { wheel: Some("s:0:1".into()), gamepad: None };
        let mut st = poller_state(binds, false, focus_gate);
        let mut src: Vec<Box<dyn ButtonSource>> = vec![Box::new(Scripted(vec![
            vec![Edge { device: "s:0".into(), button: 1, pressed: true }],
            vec![],
            vec![Edge { device: "s:0".into(), button: 1, pressed: false }],
        ]))];
        let mut h = HeldSet::new();
        let mut out = vec![];
        for _ in 0..3 {
            match step(&mut src, &mut h, &mut st, focused) {
                Output::Transitions(ts) => out.extend(ts),
                Output::None => {}
                Output::Captured(_) => panic!("not capturing"),
            }
        }
        out
    }

    #[test]
    fn step_drives_sources_through_the_held_set_across_polls() {
        let out = press_release_over_three_polls(true, true);
        assert!(matches!(out.as_slice(), [Transition::Press, Transition::Release]));
    }

    #[test]
    fn step_focus_gate_off_ignores_unfocused_and_presses_still_flow() {
        // #112 escape hatch (VR): focus_gate=false must behave exactly as if always
        // focused, even though `focused` is false on every poll.
        let out = press_release_over_three_polls(false, false);
        assert!(matches!(out.as_slice(), [Transition::Press, Transition::Release]));
    }

    #[test]
    fn capture_mode_swallows_the_next_press_and_reports_its_id() {
        let binds = Binds { wheel: Some("s:0:1".into()), gamepad: None };
        let mut st = poller_state(binds, true, true);
        let mut h = HeldSet::new();
        let mut src: Vec<Box<dyn ButtonSource>> = vec![Box::new(Scripted(vec![
            vec![Edge { device: "s:0".into(), button: 1, pressed: true }],   // bound, but capture wins
        ]))];
        let out = step(&mut src, &mut h, &mut st, true);
        assert!(matches!(out, Output::Captured(ref id) if id == "s:0:1"));
        assert!(!st.capture_armed);
        assert!(h.held.is_empty(), "a captured press must not leave the PTT held");
    }

    struct CountingSource(std::sync::Arc<std::sync::atomic::AtomicU32>);
    impl ButtonSource for CountingSource {
        fn poll(&mut self) -> Vec<Edge> {
            self.0.fetch_add(1, std::sync::atomic::Ordering::SeqCst);
            vec![]
        }
    }

    #[test]
    fn step_with_no_binds_and_not_capturing_polls_no_source() {
        let mut st = poller_state(Binds::default(), false, true);
        let mut h = HeldSet::new();
        let calls = std::sync::Arc::new(std::sync::atomic::AtomicU32::new(0));
        let mut src: Vec<Box<dyn ButtonSource>> = vec![Box::new(CountingSource(calls.clone()))];
        let out = step(&mut src, &mut h, &mut st, true);
        assert!(matches!(out, Output::None));
        assert_eq!(
            calls.load(std::sync::atomic::Ordering::SeqCst),
            0,
            "no binds and not capturing must not touch a source"
        );
    }

    #[test]
    fn step_unfocused_polls_no_source_and_returns_none() {
        // Binds are set (so this isn't the no-binds fast path above), but focus is
        // denied: the gate must return before ever touching a source (#112).
        let binds = Binds { wheel: Some("s:0:1".into()), gamepad: None };
        let mut st = poller_state(binds, false, true);
        let mut h = HeldSet::new();
        let calls = std::sync::Arc::new(std::sync::atomic::AtomicU32::new(0));
        let mut src: Vec<Box<dyn ButtonSource>> = vec![Box::new(CountingSource(calls.clone()))];
        let out = step(&mut src, &mut h, &mut st, false);
        assert!(matches!(out, Output::None));
        assert_eq!(calls.load(std::sync::atomic::Ordering::SeqCst), 0, "unfocused must not poll a source");
    }

    #[test]
    fn step_focus_loss_releases_a_held_press_then_regain_resumes_normally() {
        let binds = Binds { wheel: Some("s:0:1".into()), gamepad: None };
        let mut st = poller_state(binds, false, true);
        let mut h = HeldSet::new();
        // Only the 3 *focused* polls below ever call `.poll()` -- the unfocused ones
        // return from the gate before touching a source -- so this list has exactly 3
        // entries, one per focused poll, not one per poll overall.
        let mut src: Vec<Box<dyn ButtonSource>> = vec![Box::new(Scripted(vec![
            vec![Edge { device: "s:0".into(), button: 1, pressed: true }],  // 1: press, focused
            vec![Edge { device: "s:0".into(), button: 1, pressed: true }],  // 4: press again, refocused
            vec![Edge { device: "s:0".into(), button: 1, pressed: false }], // 5: release
        ]))];

        // 1: press while focused.
        assert!(matches!(
            step(&mut src, &mut h, &mut st, true),
            Output::Transitions(ref ts) if matches!(ts.as_slice(), [Transition::Press])
        ));
        // 2: focus lost mid-press -- exactly one release, held cleared.
        assert!(matches!(
            step(&mut src, &mut h, &mut st, false),
            Output::Transitions(ref ts) if matches!(ts.as_slice(), [Transition::Release])
        ));
        assert!(h.held.is_empty());
        // 3: a further unfocused poll emits nothing -- no release storm.
        assert!(matches!(step(&mut src, &mut h, &mut st, false), Output::None));
        // 4/5: focus regained with nothing held -- normal press/release resumes.
        assert!(matches!(
            step(&mut src, &mut h, &mut st, true),
            Output::Transitions(ref ts) if matches!(ts.as_slice(), [Transition::Press])
        ));
        assert!(matches!(
            step(&mut src, &mut h, &mut st, true),
            Output::Transitions(ref ts) if matches!(ts.as_slice(), [Transition::Release])
        ));
    }

    #[test]
    fn hotkey_name_maps_keyboard_event_codes() {
        assert_eq!(hotkey_name("KeyV"), "V");
        assert_eq!(hotkey_name("F9"), "F9");
        assert_eq!(hotkey_name("Digit3"), "3");
        assert_eq!(hotkey_name("Space"), "Space");
    }

    #[test]
    fn oem_name_from_keeps_a_real_product_name() {
        assert_eq!(oem_name_from("Fanatec Podium DD1\0", 0), "Fanatec Podium DD1");
    }

    #[test]
    fn oem_name_from_replaces_the_generic_driver_name() {
        assert_eq!(oem_name_from("Microsoft PC-joystick driver\0", 2), "Joystick 3");
    }

    // --- HID (#144): pure logic around the Win32-unavoidable HidSource. ---

    #[test]
    fn diff_to_edges_tracks_a_pressed_set_across_reports_with_no_duplicate_edges() {
        let empty: HashSet<u16> = HashSet::new();
        let mut prev = empty.clone();
        let mut all = vec![];

        let now: HashSet<u16> = [3].into_iter().collect();
        all.extend(diff_to_edges(&prev, &now, "hid:046d:c276", 1));
        prev = now;

        let now: HashSet<u16> = [3, 7].into_iter().collect();
        all.extend(diff_to_edges(&prev, &now, "hid:046d:c276", 1));
        prev = now;

        // unchanged report: no duplicate edges
        all.extend(diff_to_edges(&prev, &prev.clone(), "hid:046d:c276", 1));

        let now: HashSet<u16> = empty;
        all.extend(diff_to_edges(&prev, &now, "hid:046d:c276", 1));

        let got: Vec<(u8, bool)> = all.iter().map(|e| (e.button, e.pressed)).collect();
        assert_eq!(got, vec![(2, true), (6, true), (2, false), (6, false)]);
    }

    #[test]
    fn normalize_usage_maps_the_lowest_usage_to_button_zero() {
        assert_eq!(normalize_usage(1, 1), 0);
        assert_eq!(normalize_usage(4, 1), 3);
        assert_eq!(normalize_usage(10, 10), 0);
    }

    #[test]
    fn hid_bind_id_round_trips_through_bind_id_and_parse_hid_bind() {
        let edge = Edge { device: "hid:046d:c276".into(), button: 7, pressed: true };
        let id = bind_id(&edge);
        assert_eq!(id, "hid:046d:c276:7");
        assert_eq!(parse_hid_bind(&id), Some((0x046d, 0xc276, 7)));
    }

    #[test]
    fn parse_hid_bind_rejects_non_hid_ids() {
        assert_eq!(parse_hid_bind("xinput:0:4"), None);
        assert_eq!(parse_hid_bind("hid:046d:c276"), None); // missing button
        assert_eq!(parse_hid_bind("hid:zz:c276:7"), None); // bad hex
    }

    #[test]
    fn releases_for_vanished_turns_every_still_pressed_usage_into_a_release() {
        let pressed: HashSet<u16> = [1, 4].into_iter().collect();
        let mut edges = releases_for_vanished(&pressed, "hid:046d:c276", 1);
        edges.sort_by_key(|e| e.button);
        let got: Vec<(u8, bool)> = edges.iter().map(|e| (e.button, e.pressed)).collect();
        assert_eq!(got, vec![(0, false), (3, false)]);
    }

    #[test]
    fn diff_device_paths_adds_new_devices_once_and_drops_vanished_ones() {
        let known = vec!["\\\\?\\hid#a".to_string(), "\\\\?\\hid#b".to_string()];
        let seen = vec!["\\\\?\\hid#a".to_string(), "\\\\?\\hid#c".to_string()];
        let (added, removed) = diff_device_paths(&known, &seen);
        assert_eq!(added, vec!["\\\\?\\hid#c".to_string()]);
        assert_eq!(removed, vec!["\\\\?\\hid#b".to_string()]);

        // a device already known is not re-added on the next sweep
        let (added_again, removed_again) = diff_device_paths(&seen, &seen);
        assert!(added_again.is_empty());
        assert!(removed_again.is_empty());
    }
}
