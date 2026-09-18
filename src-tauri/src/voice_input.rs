//! Turns wheel/gamepad button presses into voice:press / voice:release events for
//! the webview, while LMU has focus. Spec: docs/superpowers/specs/2026-09-15-voice-loop-design.md.
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

#[derive(Default)]
pub struct PollerState { pub binds: Binds, pub capture_armed: bool }

pub enum Output { None, Transitions(Vec<Transition>), Captured(String) }

/// One poll cycle. Capture mode wins over PTT: the next pressed edge (from any
/// source) is reported as `Output::Captured` and never reaches `held`, so it
/// can never leave the PTT looking held.
pub fn step(sources: &mut [Box<dyn ButtonSource>], held: &mut HeldSet, state: &mut PollerState) -> Output {
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
            let out = {
                let mut st = state.lock().unwrap();
                step(&mut sources, &mut held, &mut st)
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
pub fn voice_set_binds(binds: Binds, state: tauri::State<std::sync::Arc<std::sync::Mutex<PollerState>>>) {
    state.lock().unwrap().binds = binds;
}

#[tauri::command]
pub fn voice_capture_next(state: tauri::State<std::sync::Arc<std::sync::Mutex<PollerState>>>) {
    state.lock().unwrap().capture_armed = true;
}

#[tauri::command]
pub fn voice_set_hotkey(app: tauri::AppHandle, key: Option<String>) -> Result<(), String> {
    use tauri::Emitter;
    use tauri_plugin_global_shortcut::{GlobalShortcutExt, ShortcutState};
    app.global_shortcut().unregister_all().map_err(|e| e.to_string())?;
    if let Some(k) = key {
        let name = hotkey_name(&k);
        app.global_shortcut()
            .on_shortcut(name.as_str(), move |app, _sc, ev| {
                let event_name = match ev.state() {
                    ShortcutState::Pressed => "voice:press",
                    ShortcutState::Released => "voice:release",
                };
                let _ = app.emit(event_name, ());
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

    #[test]
    fn step_drives_sources_through_the_held_set_across_polls() {
        let binds = Binds { wheel: Some("s:0:1".into()), gamepad: None };
        let mut st = PollerState { binds, capture_armed: false };
        let mut src: Vec<Box<dyn ButtonSource>> = vec![Box::new(Scripted(vec![
            vec![Edge { device: "s:0".into(), button: 1, pressed: true }],
            vec![],
            vec![Edge { device: "s:0".into(), button: 1, pressed: false }],
        ]))];
        let mut h = HeldSet::new();
        let mut out = vec![];
        for _ in 0..3 {
            match step(&mut src, &mut h, &mut st) {
                Output::Transitions(ts) => out.extend(ts),
                Output::None => {}
                Output::Captured(_) => panic!("not capturing"),
            }
        }
        assert!(matches!(out.as_slice(), [Transition::Press, Transition::Release]));
    }

    #[test]
    fn capture_mode_swallows_the_next_press_and_reports_its_id() {
        let binds = Binds { wheel: Some("s:0:1".into()), gamepad: None };
        let mut st = PollerState { binds, capture_armed: true };
        let mut h = HeldSet::new();
        let mut src: Vec<Box<dyn ButtonSource>> = vec![Box::new(Scripted(vec![
            vec![Edge { device: "s:0".into(), button: 1, pressed: true }],   // bound, but capture wins
        ]))];
        let out = step(&mut src, &mut h, &mut st);
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
        let mut st = PollerState { binds: Binds::default(), capture_armed: false };
        let mut h = HeldSet::new();
        let calls = std::sync::Arc::new(std::sync::atomic::AtomicU32::new(0));
        let mut src: Vec<Box<dyn ButtonSource>> = vec![Box::new(CountingSource(calls.clone()))];
        let out = step(&mut src, &mut h, &mut st);
        assert!(matches!(out, Output::None));
        assert_eq!(
            calls.load(std::sync::atomic::Ordering::SeqCst),
            0,
            "no binds and not capturing must not touch a source"
        );
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
}
