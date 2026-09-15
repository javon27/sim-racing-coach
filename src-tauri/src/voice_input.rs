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

pub fn tick(sources: &mut [Box<dyn ButtonSource>], held: &mut HeldSet, binds: &Binds) -> Vec<Transition> {
    let mut out = vec![];
    for s in sources.iter_mut() {
        for e in s.poll() { if let Some(t) = held.apply(&e, binds) { out.push(t); } }
    }
    out
}

#[derive(Default)]
pub struct PollerState { pub binds: Binds, pub capture_armed: bool }

pub enum Output { None, Transitions(Vec<Transition>), Captured(String) }

/// One poll cycle. Capture mode wins over PTT: the next pressed edge (from any
/// source) is reported as `Output::Captured` and never reaches `held`, so it
/// can never leave the PTT looking held.
pub fn step(sources: &mut [Box<dyn ButtonSource>], held: &mut HeldSet, state: &mut PollerState) -> Output {
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
    fn tick_drives_sources_through_the_held_set() {
        let binds = Binds { wheel: Some("s:0:1".into()), gamepad: None };
        let mut src: Vec<Box<dyn ButtonSource>> = vec![Box::new(Scripted(vec![
            vec![Edge { device: "s:0".into(), button: 1, pressed: true }],
            vec![],
            vec![Edge { device: "s:0".into(), button: 1, pressed: false }],
        ]))];
        let mut h = HeldSet::new();
        let mut out = vec![];
        for _ in 0..3 { out.extend(tick(&mut src, &mut h, &binds)); }
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

    #[test]
    fn hotkey_name_maps_keyboard_event_codes() {
        assert_eq!(hotkey_name("KeyV"), "V");
        assert_eq!(hotkey_name("F9"), "F9");
        assert_eq!(hotkey_name("Digit3"), "3");
        assert_eq!(hotkey_name("Space"), "Space");
    }
}
