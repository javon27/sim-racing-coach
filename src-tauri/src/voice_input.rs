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
}
