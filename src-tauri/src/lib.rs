use std::fs::File;
use std::net::TcpStream;
use std::path::PathBuf;
use std::process::{Child, Command, Stdio};
use std::sync::Mutex;
use std::time::Duration;
use tauri::Manager;

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

fn sidecar_log_path() -> PathBuf {
    repo_root().join("sidecar.log")
}

/// A GUI app has no console, so without this the sidecar's stdout/stderr
/// (uvicorn banner, Python tracebacks) just vanish. Truncated on each start.
fn spawn_sidecar() -> std::io::Result<Child> {
    let log = File::create(sidecar_log_path())?;
    let log_err = log.try_clone()?;
    Command::new(python_path())
        .args(["-m", "simcoach.server"])
        .current_dir(repo_root())
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

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .setup(|app| {
            if cfg!(debug_assertions) {
                app.handle().plugin(
                    tauri_plugin_log::Builder::default()
                        .level(log::LevelFilter::Info)
                        .build(),
                )?;
            }

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
            }
        });
}
