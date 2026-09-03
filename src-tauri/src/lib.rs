use std::path::PathBuf;
use std::process::{Child, Command};
use std::sync::Mutex;
use tauri::Manager;

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

fn spawn_sidecar() -> std::io::Result<Child> {
    Command::new(python_path())
        .args(["-m", "simcoach.server"])
        .current_dir(repo_root())
        .spawn()
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

            let child = spawn_sidecar().expect("failed to spawn simcoach sidecar");
            #[cfg(windows)]
            if let Err(e) = assign_kill_on_close_job(&child) {
                log::warn!("failed to bind sidecar to kill-on-close job object: {e}");
            }
            app.manage(Sidecar(Mutex::new(Some(child))));

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
