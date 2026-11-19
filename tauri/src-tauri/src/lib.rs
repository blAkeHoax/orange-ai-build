pub mod commands;
pub mod job_object;
pub mod python_runtime;
pub mod single_instance;

use tauri::Manager;

pub fn run() {
    // 1. Enforce single-instance mutex
    let _instance_guard = match single_instance::SingleInstanceGuard::acquire(
        "Local\\OrangeAIDesktopSingleInstanceMutex",
    ) {
        Ok(guard) => guard,
        Err(e) => {
            eprintln!("[single-instance] {}", e);
            std::process::exit(0);
        }
    };

    tauri::Builder::default()
        .plugin(tauri_plugin_updater::Builder::new().build())
        .plugin(tauri_plugin_process::init())
        .setup(|app| {
            let handle = app.handle().clone();

            // Spawn and supervise Python runtime without panicking on failure
            let supervisor = python_runtime::RuntimeSupervisor::new(handle);
            let supervisor_init = supervisor.clone();

            tauri::async_runtime::block_on(async move {
                let status = supervisor_init.initialize().await;
                eprintln!("[runtime-supervisor] Startup status: {:?}", status);
            });

            app.manage(supervisor);
            Ok(())
        })
        .invoke_handler(tauri::generate_handler![
            commands::get_handshake_status,
            commands::retry_handshake,
            commands::open_data_folder,
            commands::open_official_releases,
            commands::ping,
            commands::get_health,
            commands::get_memory,
            commands::get_jobs,
            commands::send_message,
            commands::confirm_action,
            commands::call_runtime,
            commands::shutdown,
            commands::prepare_for_update,
            commands::minimize_window,
            commands::toggle_maximize_window,
            commands::close_window,
            commands::set_window_size,
            commands::set_theme,
            commands::toggle_drawer,
            commands::select_view
        ])
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}
