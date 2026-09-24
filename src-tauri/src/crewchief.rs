//! #163: the coach holds its next sentence while CrewChief is talking -- it never
//! talks over the spotter and never ducks it. A watcher thread reads CrewChief's
//! Windows audio session peak (WASAPI, default render device) and keeps `STATE`
//! current; the webview polls it through `crewchief_active` before each sentence.
//! Any COM/HRESULT failure, no device, or no CrewChief session reads as "not
//! active", i.e. the coach behaves exactly as before.
use std::sync::atomic::{AtomicU8, Ordering};

/// PENDING RIG: session peak (0..1) at/above which CrewChief counts as talking.
pub const PEAK_THRESHOLD: f32 = 0.01;
/// PENDING RIG: CrewChief stays "active" this long after its last loud sample, so
/// the gaps between its words don't let the coach start mid-call.
pub const TAIL_MS: u64 = 300;
/// PENDING RIG: loud without a break for this long is not speech (a hum, a stuck
/// meter, music) -- it reads as quiet until the level drops once.
pub const STUCK_LOUD_MS: u64 = 10_000;
/// Meter read rate (25 Hz).
const POLL_MS: u64 = 40;
/// Re-find the session every ~2 s: CrewChief started/stopped, default device changed.
const RESCAN_EVERY: u32 = 50;

/// Watcher output: UNKNOWN until its first meter read (so the first sentence of a
/// session isn't spoken over CrewChief just because the thread only just started).
const UNKNOWN: u8 = 0;
const QUIET: u8 = 1;
const ACTIVE: u8 = 2;
static STATE: AtomicU8 = AtomicU8::new(UNKNOWN);

/// Pure: peak samples + a clock -> active. Loud now, or loud within `TAIL_MS`, unless
/// it has been loud without a break for `STUCK_LOUD_MS`.
/// (The max wait is per answer, so it lives with the waiter in the webview.)
#[derive(Default)]
pub struct Activity { last_loud_ms: Option<u64>, loud_since_ms: Option<u64> }
impl Activity {
    pub fn update(&mut self, peak: f32, now_ms: u64) -> bool {
        if peak >= PEAK_THRESHOLD {
            let since = *self.loud_since_ms.get_or_insert(now_ms);
            if now_ms.saturating_sub(since) >= STUCK_LOUD_MS {
                self.last_loud_ms = None;   // no tail after a stuck run either
                return false;
            }
            self.last_loud_ms = Some(now_ms);
        } else {
            self.loud_since_ms = None;
        }
        self.last_loud_ms.is_some_and(|t| now_ms.saturating_sub(t) < TAIL_MS)
    }
}

pub fn is_crewchief(exe_stem: &str) -> bool {
    exe_stem.eq_ignore_ascii_case("CrewChiefV4")
}

/// True while CrewChief is talking (or within its tail); None until the watcher's
/// first read. The watcher thread starts on the first call, so with the setting off
/// (never polled) nothing runs.
#[tauri::command]
pub fn crewchief_active() -> Option<bool> {
    static START: std::sync::Once = std::sync::Once::new();
    START.call_once(|| { std::thread::spawn(watch); });
    match STATE.load(Ordering::Relaxed) {
        UNKNOWN => None,
        s => Some(s == ACTIVE),
    }
}

#[cfg(not(windows))]
fn watch() { STATE.store(QUIET, Ordering::Relaxed); }

#[cfg(windows)]
fn watch() {
    use windows::Win32::System::Com::{CoInitializeEx, CoUninitialize, COINIT_MULTITHREADED};
    // Dropped last (declared first): uninitialises COM after every interface below
    // is released, and reports QUIET so a dead watcher can never hold the coach.
    struct Com;
    impl Drop for Com {
        fn drop(&mut self) {
            STATE.store(QUIET, Ordering::Relaxed);
            unsafe { CoUninitialize() };
        }
    }
    if unsafe { CoInitializeEx(None, COINIT_MULTITHREADED) }.is_err() {
        STATE.store(QUIET, Ordering::Relaxed);
        return;
    }
    let _com = Com;
    let start = std::time::Instant::now();
    let mut activity = Activity::default();
    let mut meters = Vec::new();
    let mut tick = 0u32;
    loop {
        if tick % RESCAN_EVERY == 0 { meters = find_meters().unwrap_or_default(); }
        tick = tick.wrapping_add(1);
        let peak = meters.iter()
            .filter_map(|m| unsafe { m.GetPeakValue() }.ok())
            .fold(0.0f32, f32::max);
        let active = activity.update(peak, start.elapsed().as_millis() as u64);
        STATE.store(if active { ACTIVE } else { QUIET }, Ordering::Relaxed);
        std::thread::sleep(std::time::Duration::from_millis(POLL_MS));
    }
}

/// The peak meter of every CrewChief session on the default render device.
/// ponytail: default device only; CrewChief routed to another output reads as silent.
#[cfg(windows)]
fn find_meters() -> windows::core::Result<Vec<windows::Win32::Media::Audio::Endpoints::IAudioMeterInformation>> {
    use windows::core::Interface;
    use windows::Win32::Media::Audio::Endpoints::IAudioMeterInformation;
    use windows::Win32::Media::Audio::{
        eConsole, eRender, IAudioSessionControl2, IAudioSessionManager2, IMMDeviceEnumerator, MMDeviceEnumerator,
    };
    use windows::Win32::System::Com::{CoCreateInstance, CLSCTX_ALL};
    let mut out = Vec::new();
    unsafe {
        let devices: IMMDeviceEnumerator = CoCreateInstance(&MMDeviceEnumerator, None, CLSCTX_ALL)?;
        let device = devices.GetDefaultAudioEndpoint(eRender, eConsole)?;
        let manager: IAudioSessionManager2 = device.Activate(CLSCTX_ALL, None)?;
        let sessions = manager.GetSessionEnumerator()?;
        for i in 0..sessions.GetCount()? {
            let Ok(session) = sessions.GetSession(i) else { continue };
            let Ok(pid) = session.cast::<IAudioSessionControl2>().and_then(|s| s.GetProcessId()) else { continue };
            if !exe_stem(pid).is_some_and(|s| is_crewchief(&s)) { continue; }
            if let Ok(meter) = session.cast::<IAudioMeterInformation>() { out.push(meter); }
        }
    }
    Ok(out)
}

#[cfg(windows)]
fn exe_stem(pid: u32) -> Option<String> {
    use windows::core::PWSTR;
    use windows::Win32::Foundation::CloseHandle;
    use windows::Win32::System::Threading::{
        OpenProcess, QueryFullProcessImageNameW, PROCESS_NAME_WIN32, PROCESS_QUERY_LIMITED_INFORMATION,
    };
    if pid == 0 { return None; }   // the system-sounds session
    let handle = unsafe { OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, false, pid) }.ok()?;
    let mut buf = [0u16; 260];
    let mut size = buf.len() as u32;
    let ok = unsafe { QueryFullProcessImageNameW(handle, PROCESS_NAME_WIN32, PWSTR(buf.as_mut_ptr()), &mut size) }.is_ok();
    unsafe { let _ = CloseHandle(handle); }
    if !ok { return None; }
    std::path::Path::new(&String::from_utf16_lossy(&buf[..size as usize]))
        .file_stem()
        .and_then(|s| s.to_str())
        .map(str::to_string)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn quiet_until_a_peak_crosses_the_threshold() {
        let mut a = Activity::default();
        assert!(!a.update(0.0, 0));
        assert!(!a.update(PEAK_THRESHOLD * 0.5, 40));
        assert!(a.update(PEAK_THRESHOLD, 80));
    }

    #[test]
    fn stays_active_through_the_tail_then_goes_quiet() {
        let mut a = Activity::default();
        assert!(a.update(0.5, 1000));
        assert!(a.update(0.0, 1000 + TAIL_MS - 1));       // a gap between words
        assert!(!a.update(0.0, 1000 + TAIL_MS));          // tail elapsed
    }

    #[test]
    fn a_new_peak_inside_the_tail_restarts_it() {
        let mut a = Activity::default();
        a.update(0.5, 0);
        a.update(0.5, 200);
        assert!(a.update(0.0, 200 + TAIL_MS - 1));
        assert!(!a.update(0.0, 200 + TAIL_MS));
    }

    #[test]
    fn loud_without_a_break_past_the_stuck_cutoff_reads_as_quiet_until_it_drops() {
        let mut a = Activity::default();
        let mut t = 0;
        while t < STUCK_LOUD_MS { assert!(a.update(0.5, t)); t += 40; }
        assert!(!a.update(0.5, t));                        // stuck: not talking
        assert!(!a.update(0.5, t + 5_000));
        assert!(!a.update(0.0, t + 5_040));                // no tail after a stuck run
        assert!(a.update(0.5, t + 5_080));                 // a fresh call after a break counts again
    }

    #[test]
    fn gaps_between_words_restart_the_stuck_clock() {
        let mut a = Activity::default();
        let mut t = 0;
        while t < 2 * STUCK_LOUD_MS {
            assert!(a.update(if t % 2_000 == 0 { 0.0 } else { 0.5 }, t) || t == 0);
            t += 40;
        }
    }

    #[test]
    fn a_nan_peak_reads_as_quiet() {
        assert!(!Activity::default().update(f32::NAN, 0));
    }

    #[test]
    fn matches_crewchief_exe_case_insensitively_only() {
        assert!(is_crewchief("CrewChiefV4"));
        assert!(is_crewchief("crewchiefv4"));
        assert!(!is_crewchief("Le Mans Ultimate"));
        assert!(!is_crewchief("CrewChiefV4Updater"));
    }

    /// Rig probe for PEAK_THRESHOLD: `cargo test crewchief_probe -- --ignored --nocapture`
    /// with CrewChief running prints its session peak for 10 s.
    #[cfg(windows)]
    #[test]
    #[ignore]
    fn crewchief_probe() {
        use windows::Win32::System::Com::{CoInitializeEx, COINIT_MULTITHREADED};
        assert!(unsafe { CoInitializeEx(None, COINIT_MULTITHREADED) }.is_ok());
        let meters = find_meters().expect("WASAPI session enumeration failed");
        println!("CrewChief sessions on the default device: {}", meters.len());
        for _ in 0..40 {
            let peaks: Vec<f32> = meters.iter().filter_map(|m| unsafe { m.GetPeakValue() }.ok()).collect();
            println!("peak {peaks:?} (threshold {PEAK_THRESHOLD})");
            std::thread::sleep(std::time::Duration::from_millis(250));
        }
    }
}
