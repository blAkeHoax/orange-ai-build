use std::ffi::OsStr;
use std::os::windows::ffi::OsStrExt;
use std::ptr;
use windows_sys::Win32::Foundation::{CloseHandle, GetLastError, ERROR_ALREADY_EXISTS, HANDLE, INVALID_HANDLE_VALUE};
use windows_sys::Win32::System::Threading::CreateMutexW;

pub struct SingleInstanceGuard {
    handle: HANDLE,
}

impl SingleInstanceGuard {
    pub fn acquire(name: &str) -> Result<Self, String> {
        let wide: Vec<u16> = OsStr::new(name).encode_wide().chain(std::iter::once(0)).collect();
        let handle = unsafe { CreateMutexW(ptr::null(), 1, wide.as_ptr()) };
        if handle.is_null() || handle == INVALID_HANDLE_VALUE {
            return Err("Failed to create single instance mutex".to_string());
        }

        let last_err = unsafe { GetLastError() };
        if last_err == ERROR_ALREADY_EXISTS {
            unsafe { CloseHandle(handle) };
            return Err(format!("Another instance of {} is already running.", name));
        }

        Ok(Self { handle })
    }
}

impl Drop for SingleInstanceGuard {
    fn drop(&mut self) {
        if !self.handle.is_null() && self.handle != INVALID_HANDLE_VALUE {
            unsafe { CloseHandle(self.handle) };
        }
    }
}
