use std::fs::File;
use std::net::TcpStream;
use std::path::PathBuf;
use std::process::{Child, Command, Stdio};
use std::sync::Mutex;
use std::time::Duration;
use tauri::Manager;
use tauri_plugin_window_state::{StateFlags, WindowExt};

/// Host:port the sidecar serves on; also used to detect an already-running instance.
const SIDECAR_ADDR: &str = "127.0.0.1:8765";

/// Holds the spawned Python sidecar so it can be killed on app exit.
struct Sidecar(Mutex<Option<Child>>);

fn repo_root() -> PathBuf {
    // src-tauri's parent directory, resolved at compile time.
    PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("..")
}

fn python_path() -> PathBuf {
    std::env::var("SIMCOACH_PYTHON")
        .map(PathBuf::from)
        .unwrap_or_else(|_| repo_root().join(".venv/Scripts/python.exe"))
}

/// The bundled sidecar exe (PyInstaller build shipped as a Tauri externalBin, so it
/// lands next to our executable in an install), or None when running from source.
fn bundled_sidecar() -> Option<PathBuf> {
    let p = std::env::current_exe().ok()?.parent()?.join("simcoach-sidecar.exe");
    p.exists().then_some(p)
}

/// How to launch the sidecar. A RELEASE (installed) build runs the bundled exe (it
/// embeds Python + simcoach and reads its data dir from APPDATA/SIMCOACH_DATA_DIR). A
/// DEBUG build (`cargo tauri dev`) runs the repo venv's `python -m simcoach.server` so
/// code changes are live — Tauri copies the externalBin into target/debug too, so
/// presence of the exe alone can't distinguish dev from installed; the build profile can.
fn sidecar_command() -> Command {
    if !cfg!(debug_assertions) {
        if let Some(exe) = bundled_sidecar() {
            return Command::new(exe);
        }
    }
    let mut c = Command::new(python_path());
    c.args(["-m", "simcoach.server"]).current_dir(repo_root());
    c
}

/// Mirrors simcoach/paths.py's app_data_dir(): SIMCOACH_DATA_DIR override, else
/// %APPDATA%\SimCoach, else %USERPROFILE%\.simcoach (home fallback).
fn app_data_dir() -> Option<PathBuf> {
    if let Ok(dir) = std::env::var("SIMCOACH_DATA_DIR") {
        let dir = dir.trim();
        if !dir.is_empty() {
            return Some(PathBuf::from(dir));
        }
    }
    if let Ok(appdata) = std::env::var("APPDATA") {
        return Some(PathBuf::from(appdata).join("SimCoach"));
    }
    dirs_home().map(|home| home.join(".simcoach"))
}

fn dirs_home() -> Option<PathBuf> {
    std::env::var("USERPROFILE")
        .or_else(|_| std::env::var("HOME"))
        .ok()
        .map(PathBuf::from)
}

/// Bootstrap log lives in app-data, not the repo root, so the working tree
/// stays clean. Falls back to the repo root only if app-data can't be resolved.
fn sidecar_log_path() -> PathBuf {
    match app_data_dir() {
        Some(dir) => {
            let logs = dir.join("logs");
            if std::fs::create_dir_all(&logs).is_ok() {
                return logs.join("sidecar-bootstrap.log");
            }
            repo_root().join("sidecar.log")
        }
        None => repo_root().join("sidecar.log"),
    }
}

/// A GUI app has no console, so without this the sidecar's stdout/stderr
/// (uvicorn banner, Python tracebacks) just vanish. Truncated on each start.
fn spawn_sidecar() -> std::io::Result<Child> {
    let log = File::create(sidecar_log_path())?;
    let log_err = log.try_clone()?;
    sidecar_command()
        .stdout(Stdio::from(log))
        .stderr(Stdio::from(log_err))
        .spawn()
}

fn is_sidecar_already_running() -> bool {
    TcpStream::connect_timeout(&SIDECAR_ADDR.parse().unwrap(), Duration::from_millis(300)).is_ok()
}

/// Windows Job Object with JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE: the OS kills the
/// child when this process's handle table closes, even on a crash/hard-kill
/// (a bare `.kill()` on WindowEvent::Destroyed does not fire on a panic and
/// leaks an orphaned python holding port 8765).
#[cfg(windows)]
fn assign_kill_on_close_job(child: &Child) -> windows::core::Result<()> {
    use std::os::windows::io::AsRawHandle;
    use windows::Win32::Foundation::HANDLE;
    use windows::Win32::System::JobObjects::{
        AssignProcessToJobObject, CreateJobObjectW, JobObjectExtendedLimitInformation,
        SetInformationJobObject, JOBOBJECT_EXTENDED_LIMIT_INFORMATION,
        JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE,
    };

    unsafe {
        let job = CreateJobObjectW(None, None)?;

        let mut info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION::default();
        info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
        SetInformationJobObject(
            job,
            JobObjectExtendedLimitInformation,
            &info as *const _ as *const _,
            std::mem::size_of::<JOBOBJECT_EXTENDED_LIMIT_INFORMATION>() as u32,
        )?;

        let process = HANDLE(child.as_raw_handle());
        AssignProcessToJobObject(job, process)?;
        // Intentionally leak the job handle: it must stay alive for the life
        // of this process so KILL_ON_JOB_CLOSE fires when we exit/crash.
    }
    Ok(())
}

/// Best-effort kill of any ORPHANED bundled sidecar. `simcoach-sidecar.exe` is our
/// own unique image name, so this can never touch an unrelated process. It covers the
/// gaps the owned-child kill + job object don't: a REUSED (un-owned) sidecar, and one
/// left by a crashed or force-closed instance — either of which would keep holding
/// port 8765 and get reused by the next launch (stale code). Run at startup (clear the
/// port before we spawn) and on exit (don't leave one behind). Dev runs the venv
/// python, not this exe, so in dev it only clears a stray bundle left over from an
/// installed run — never the dev sidecar.
#[cfg(windows)]
fn kill_stray_sidecars() {
    use std::os::windows::process::CommandExt;
    const CREATE_NO_WINDOW: u32 = 0x0800_0000;
    let _ = Command::new("taskkill")
        .args(["/F", "/IM", "simcoach-sidecar.exe"])
        .creation_flags(CREATE_NO_WINDOW)
        .output();
}
#[cfg(not(windows))]
fn kill_stray_sidecars() {}

/// AABB overlap: is the window rect (x, y, w, h) touching ANY monitor rect
/// (mx, my, mw, mh)? Pure decision fn so the off-screen recovery is testable
/// without a real window/monitor.
fn is_on_screen(x: i32, y: i32, w: u32, h: u32, monitors: &[(i32, i32, u32, u32)]) -> bool {
    let (wx0, wy0) = (x, y);
    let (wx1, wy1) = (x + w as i32, y + h as i32);
    monitors.iter().any(|&(mx, my, mw, mh)| {
        let (mx1, my1) = (mx + mw as i32, my + mh as i32);
        wx0 < mx1 && wx1 > mx && wy0 < my1 && wy1 > my
    })
}

/// After the window-state plugin restores geometry, recenter on the primary
/// monitor if the restored rect lands outside every monitor (layout changed
/// since last launch). No-op if any Result fails — a missing window/monitor is
/// not worth crashing startup over.
fn recenter_if_offscreen(window: &tauri::Window) {
    if let (Ok(pos), Ok(size), Ok(monitors)) = (
        window.outer_position(),
        window.outer_size(),
        window.available_monitors(),
    ) {
        let rects: Vec<(i32, i32, u32, u32)> = monitors
            .iter()
            .map(|m| {
                (
                    m.position().x,
                    m.position().y,
                    m.size().width,
                    m.size().height,
                )
            })
            .collect();
        if !is_on_screen(pos.x, pos.y, size.width, size.height, &rects) {
            let _ = window.center();
        }
    }
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_window_state::Builder::default().build())
        // The main window is created hidden (tauri.conf.json "visible": false) so
        // restoring its saved geometry + the off-screen recovery below (commit
        // 40c18b1) happen before anything is shown -- otherwise it flashes blank
        // on the primary monitor before jumping to the saved (secondary) one.
        // This fires once the webview has actually painted (PageLoadEvent::Finished),
        // not before, then reveals the window already placed correctly.
        .on_page_load(|webview, payload| {
            if payload.event() != tauri::webview::PageLoadEvent::Finished {
                return;
            }
            let window = webview.window();
            if window.label() != "main" {
                return;
            }
            let _ = window.restore_state(StateFlags::all());
            recenter_if_offscreen(&window);
            let _ = window.show();
            let _ = window.set_focus();
        })
        .setup(|app| {
            if cfg!(debug_assertions) {
                app.handle().plugin(
                    tauri_plugin_log::Builder::default()
                        .level(log::LevelFilter::Info)
                        .build(),
                )?;
            }

            // Clear any orphaned bundled sidecar before deciding — a stale one left by
            // a crashed/force-closed run would otherwise be reused (old code) and hold 8765.
            kill_stray_sidecars();
            let child = if is_sidecar_already_running() {
                let _ = std::fs::write(sidecar_log_path(), "reusing existing server on 8765\n");
                log::info!("reusing existing server on 8765");
                None
            } else {
                match spawn_sidecar() {
                    Ok(child) => {
                        #[cfg(windows)]
                        if let Err(e) = assign_kill_on_close_job(&child) {
                            log::warn!("failed to bind sidecar to kill-on-close job object: {e}");
                        }
                        Some(child)
                    }
                    Err(e) => {
                        let _ = std::fs::write(
                            sidecar_log_path(),
                            format!("failed to spawn simcoach sidecar: {e}\n"),
                        );
                        log::error!("failed to spawn simcoach sidecar: {e}");
                        None
                    }
                }
            };
            app.manage(Sidecar(Mutex::new(child)));

            // Geometry restore + off-screen recovery + reveal now happen in
            // on_page_load (above), once the webview has actually painted.

            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("error while building tauri application")
        .run(|app_handle, event| {
            if matches!(
                event,
                tauri::RunEvent::ExitRequested { .. } | tauri::RunEvent::Exit
            ) {
                if let Some(sidecar) = app_handle.try_state::<Sidecar>() {
                    if let Some(mut child) = sidecar.0.lock().unwrap().take() {
                        let _ = child.kill();
                    }
                }
                // Backstop: also kill a REUSED (un-owned) bundled sidecar, so closing
                // the app never leaves one carried over.
                kill_stray_sidecars();
            }
        });
}

#[cfg(test)]
mod tests {
    use super::is_on_screen;

    #[test]
    fn fully_inside_one_monitor() {
        let mons = [(0, 0, 1920, 1080)];
        assert!(is_on_screen(100, 100, 800, 600, &mons));
    }

    #[test]
    fn straddling_two_monitors() {
        let mons = [(0, 0, 1920, 1080), (1920, 0, 1920, 1080)];
        // Window spans the seam at x=1920.
        assert!(is_on_screen(1800, 100, 800, 600, &mons));
    }

    #[test]
    fn entirely_off_all_monitors() {
        let mons = [(0, 0, 1920, 1080), (1920, 0, 1920, 1080)];
        // Sits far below every monitor (a disconnected display's old coords).
        assert!(!is_on_screen(500, 5000, 800, 600, &mons));
    }

    #[test]
    fn empty_monitor_list() {
        assert!(!is_on_screen(0, 0, 800, 600, &[]));
    }
}
