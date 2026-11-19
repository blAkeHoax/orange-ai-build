use std::ptr;
use windows_sys::Win32::Foundation::{CloseHandle, HANDLE, INVALID_HANDLE_VALUE};
use windows_sys::Win32::System::JobObjects::{
    AssignProcessToJobObject, CreateJobObjectW, SetInformationJobObject,
    JobObjectExtendedLimitInformation, JOBOBJECT_EXTENDED_LIMIT_INFORMATION,
    JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE,
};

pub struct SafeJobObject {
    handle: HANDLE,
}

unsafe impl Send for SafeJobObject {}
unsafe impl Sync for SafeJobObject {}

impl SafeJobObject {
    pub fn new() -> Result<Self, String> {
        let handle = unsafe { CreateJobObjectW(ptr::null(), ptr::null()) };
        if handle.is_null() || handle == INVALID_HANDLE_VALUE {
            return Err("Failed to create Windows Job Object".to_string());
        }

        let mut info: JOBOBJECT_EXTENDED_LIMIT_INFORMATION = unsafe { std::mem::zeroed() };
        info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;

        let res = unsafe {
            SetInformationJobObject(
                handle,
                JobObjectExtendedLimitInformation,
                &info as *const _ as *const _,
                std::mem::size_of::<JOBOBJECT_EXTENDED_LIMIT_INFORMATION>() as u32,
            )
        };

        if res == 0 {
            unsafe { CloseHandle(handle) };
            return Err("Failed to configure Job Object with KILL_ON_JOB_CLOSE".to_string());
        }

        Ok(Self { handle })
    }

    pub fn assign_process(&self, process_handle: HANDLE) -> Result<(), String> {
        let res = unsafe { AssignProcessToJobObject(self.handle, process_handle) };
        if res == 0 {
            return Err("Failed to assign child process to Job Object".to_string());
        }
        Ok(())
    }
}

impl Drop for SafeJobObject {
    fn drop(&mut self) {
        if !self.handle.is_null() && self.handle != INVALID_HANDLE_VALUE {
            unsafe { CloseHandle(self.handle) };
        }
    }
}
