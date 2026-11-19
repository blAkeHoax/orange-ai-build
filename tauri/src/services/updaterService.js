// src/services/updaterService.js - Official Tauri 2 Updater Integration for Orange AI Desktop 2.0
// Implements approved Option 1 lifecycle:
// 1. Sequential discovery (no auto-download)
// 2. Explicit download approval & progress streaming
// 3. Session-scoped deferral (in-memory payload discarded on process exit)
// 4. Pre-update SQLite WAL truncate & database-safety verification gate
// 5. Explicit "Install & Restart Now" final confirmation

import { prepareForUpdate } from './tauriBridge.js';

export const UpdateStatus = {
  IDLE: 'IDLE',
  CHECKING: 'CHECKING',
  UP_TO_DATE: 'UP_TO_DATE',
  UPDATE_AVAILABLE: 'UPDATE_AVAILABLE',
  DEFERRED_UNFETCHED: 'DEFERRED_UNFETCHED',
  DOWNLOADING: 'DOWNLOADING',
  DOWNLOAD_ERROR: 'DOWNLOAD_ERROR',
  DOWNLOADED_SESSION_STAGED: 'DOWNLOADED_SESSION_STAGED',
  DEFERRED_SESSION_STAGED: 'DEFERRED_SESSION_STAGED',
  PREPARING_INSTALL: 'PREPARING_INSTALL',
  INSTALL_ERROR: 'INSTALL_ERROR',
  INSTALLING: 'INSTALLING',
  CHECK_ERROR: 'CHECK_ERROR',
};

// Internal Singleton State
let currentStatus = UpdateStatus.IDLE;
let updateInstance = null; // In-memory reference to Tauri Update or MockUpdate
let updateMetadata = {
  currentVersion: '2.0.0',
  targetVersion: null,
  date: null,
  body: null,
  downloadedBytes: 0,
  totalBytes: 0,
  percent: 0,
  error: null,
  lastChecked: null,
  databasePreserved: true,
};
let isModalVisible = false;
let listeners = new Set();
let periodicCheckTimer = null;
let mockUpdateFixture = null;

/**
 * Dynamically resolves the official Tauri updater plugin if available.
 */
async function getTauriUpdater() {
  if (typeof window !== 'undefined' && window.__TAURI_INTERNALS__) {
    try {
      return await import('@tauri-apps/plugin-updater');
    } catch (err) {
      console.warn('[UpdaterService] Failed to load @tauri-apps/plugin-updater:', err);
    }
  }
  return null;
}

function notifyListeners() {
  const snapshot = getUpdaterSnapshot();
  listeners.forEach((cb) => {
    try {
      cb(snapshot);
    } catch (err) {
      console.error('[UpdaterService] Listener error:', err);
    }
  });
}

/**
 * Subscribes to updater state changes. Returns an unsubscribe function.
 */
export function subscribeToUpdater(callback) {
  listeners.add(callback);
  callback(getUpdaterSnapshot());
  return () => {
    listeners.delete(callback);
  };
}

/**
 * Returns the immutable snapshot of current updater state.
 */
export function getUpdaterSnapshot() {
  return {
    status: currentStatus,
    isModalVisible,
    hasUpdateAvailable: [
      UpdateStatus.UPDATE_AVAILABLE,
      UpdateStatus.DEFERRED_UNFETCHED,
      UpdateStatus.DOWNLOADED_SESSION_STAGED,
      UpdateStatus.DEFERRED_SESSION_STAGED,
    ].includes(currentStatus),
    isDownloaded: [
      UpdateStatus.DOWNLOADED_SESSION_STAGED,
      UpdateStatus.DEFERRED_SESSION_STAGED,
    ].includes(currentStatus),
    isBusy: [
      UpdateStatus.CHECKING,
      UpdateStatus.DOWNLOADING,
      UpdateStatus.PREPARING_INSTALL,
      UpdateStatus.INSTALLING,
    ].includes(currentStatus),
    ...updateMetadata,
  };
}

/**
 * Injects a mock update fixture for testing or offline demonstrations.
 */
export function setMockUpdateFixture(fixture) {
  mockUpdateFixture = fixture;
}

/**
 * Clears the session update payload, demonstrating that in-memory downloads
 * are discarded when the application session ends.
 */
export function resetSessionState() {
  if (updateInstance && typeof updateInstance.close === 'function') {
    try {
      updateInstance.close();
    } catch (_) {}
  }
  currentStatus = UpdateStatus.IDLE;
  updateInstance = null;
  updateMetadata = {
    currentVersion: '2.0.0',
    targetVersion: null,
    date: null,
    body: null,
    downloadedBytes: 0,
    totalBytes: 0,
    percent: 0,
    error: null,
    lastChecked: null,
    databasePreserved: true,
  };
  isModalVisible = false;
  notifyListeners();
}

/**
 * Checks for updates through the configured Tauri updater endpoint or mock fixture.
 * NEVER initiates a download automatically (Option 1 Stage 1).
 */
export async function checkForUpdates(options = {}) {
  const { silent = false, mock = null } = options;

  currentStatus = UpdateStatus.CHECKING;
  updateMetadata.error = null;
  notifyListeners();

  const fixture = mock || mockUpdateFixture;

  try {
    // 1. Mock / Fixture Mode
    if (fixture !== null) {
      await new Promise((r) => setTimeout(r, 100)); // Simulate async query
      if (fixture === 'error') {
        throw new Error('Mock network error reaching update endpoint');
      }
      if (!fixture) {
        currentStatus = UpdateStatus.UP_TO_DATE;
        updateMetadata.lastChecked = new Date().toISOString();
        if (!silent) {
          isModalVisible = true;
        }
        notifyListeners();
        return null;
      }

      // Update available from fixture
      updateInstance = {
        version: fixture.version || '2.0.1',
        currentVersion: fixture.currentVersion || '2.0.0',
        date: fixture.date || new Date().toISOString(),
        body: fixture.body || '### Improvements\n- Performance enhancements\n- Security hardening',
        totalBytes: fixture.totalBytes || 27557014,
        async download(onProgress) {
          if (fixture.downloadError) {
            throw new Error(fixture.downloadError);
          }
          if (onProgress) {
            onProgress({ event: 'Started', data: { contentLength: this.totalBytes } });
            const chunks = 5;
            const chunkSize = Math.floor(this.totalBytes / chunks);
            for (let i = 1; i <= chunks; i++) {
              await new Promise((r) => setTimeout(r, 20));
              onProgress({ event: 'Progress', data: { chunkLength: chunkSize } });
            }
            onProgress({ event: 'Finished' });
          }
        },
        async install(opts) {
          if (fixture.installError) {
            throw new Error(fixture.installError);
          }
          if (typeof fixture.install === 'function') {
            return await fixture.install(opts);
          }
          // In mock mode, mark installed
          return true;
        },
        async close() {},
      };

      currentStatus = UpdateStatus.UPDATE_AVAILABLE;
      updateMetadata.currentVersion = updateInstance.currentVersion;
      updateMetadata.targetVersion = updateInstance.version;
      updateMetadata.date = updateInstance.date;
      updateMetadata.body = updateInstance.body;
      updateMetadata.downloadedBytes = 0;
      updateMetadata.totalBytes = updateInstance.totalBytes || 0;
      updateMetadata.percent = 0;
      updateMetadata.lastChecked = new Date().toISOString();

      if (!silent) {
        isModalVisible = true;
      }
      notifyListeners();
      return updateInstance;
    }

    // 2. Real Tauri Updater Integration
    const tauriUpdater = await getTauriUpdater();
    if (!tauriUpdater) {
      // Non-Tauri fallback
      currentStatus = UpdateStatus.UP_TO_DATE;
      updateMetadata.lastChecked = new Date().toISOString();
      if (!silent) {
        isModalVisible = true;
      }
      notifyListeners();
      return null;
    }

    const update = await tauriUpdater.check();
    if (!update) {
      currentStatus = UpdateStatus.UP_TO_DATE;
      updateMetadata.lastChecked = new Date().toISOString();
      if (!silent) {
        isModalVisible = true;
      }
      notifyListeners();
      return null;
    }

    // Update discovered: store in-memory reference, ZERO bytes downloaded
    updateInstance = update;
    currentStatus = UpdateStatus.UPDATE_AVAILABLE;
    updateMetadata.currentVersion = update.currentVersion || '2.0.0';
    updateMetadata.targetVersion = update.version;
    updateMetadata.date = update.date;
    updateMetadata.body = update.body || 'No release notes provided.';
    updateMetadata.downloadedBytes = 0;
    updateMetadata.totalBytes = 0;
    updateMetadata.percent = 0;
    updateMetadata.lastChecked = new Date().toISOString();

    if (!silent) {
      isModalVisible = true;
    }
    notifyListeners();
    return update;
  } catch (err) {
    console.error('[UpdaterService] Error checking for updates:', err);
    currentStatus = UpdateStatus.CHECK_ERROR;
    updateMetadata.error = err.message || String(err);
    if (!silent) {
      isModalVisible = true;
    }
    notifyListeners();
    return null;
  }
}

/**
 * Explicit user download approval (Option 1 Stage 2).
 * Initiates streaming download into Webview in-memory resources table.
 */
export async function downloadUpdate() {
  if (
    ![
      UpdateStatus.UPDATE_AVAILABLE,
      UpdateStatus.DEFERRED_UNFETCHED,
      UpdateStatus.DOWNLOAD_ERROR,
    ].includes(currentStatus) ||
    !updateInstance
  ) {
    console.warn('[UpdaterService] Cannot download in current state:', currentStatus);
    return;
  }

  currentStatus = UpdateStatus.DOWNLOADING;
  updateMetadata.downloadedBytes = 0;
  updateMetadata.percent = 0;
  updateMetadata.error = null;
  notifyListeners();

  try {
    let accumulatedBytes = 0;
    let expectedTotal = updateMetadata.totalBytes || 0;

    await updateInstance.download((event) => {
      if (event.event === 'Started') {
        if (event.data?.contentLength) {
          expectedTotal = event.data.contentLength;
          updateMetadata.totalBytes = expectedTotal;
        }
      } else if (event.event === 'Progress') {
        accumulatedBytes += event.data.chunkLength;
        updateMetadata.downloadedBytes = accumulatedBytes;
        if (expectedTotal > 0) {
          updateMetadata.percent = Math.min(
            100,
            Math.round((accumulatedBytes / expectedTotal) * 100)
          );
        }
        notifyListeners();
      } else if (event.event === 'Finished') {
        updateMetadata.downloadedBytes = expectedTotal || accumulatedBytes;
        updateMetadata.percent = 100;
        notifyListeners();
      }
    });

    // Download succeeded: payload now held strictly in-memory for this session
    currentStatus = UpdateStatus.DOWNLOADED_SESSION_STAGED;
    notifyListeners();
  } catch (err) {
    console.error('[UpdaterService] Download failed:', err);
    currentStatus = UpdateStatus.DOWNLOAD_ERROR;
    updateMetadata.error = err.message || String(err);
    notifyListeners();
  }
}

/**
 * Session-Scoped Deferral Agency (Option 1 Stage 3).
 * User chooses "Remind Me Later".
 * The in-memory payload is kept active for the remainder of this session.
 * If the application exits, the payload is destroyed and must be re-downloaded.
 */
export function remindMeLater() {
  if (currentStatus === UpdateStatus.DOWNLOADED_SESSION_STAGED) {
    currentStatus = UpdateStatus.DEFERRED_SESSION_STAGED;
  } else if (currentStatus === UpdateStatus.UPDATE_AVAILABLE) {
    currentStatus = UpdateStatus.DEFERRED_UNFETCHED;
  }
  isModalVisible = false;
  notifyListeners();
}

/**
 * Final Pre-Update Checkpoint and Coordinated Installation (Option 1 Stage 4).
 * Enforces database-safety gate:
 * 1. Halts turns and invokes `prepare_for_update`.
 * 2. Verifies SQLite WAL truncate (busy=0, wal_bytes=0).
 * 3. Verifies exclusive file access and connection closure.
 * 4. Only if ALL safety conditions pass, invokes official `update.install()`.
 * 5. If ANY check fails, aborts immediately and preserves operational database.
 */
export async function installAndRestart(options = {}) {
  if (
    ![
      UpdateStatus.DOWNLOADED_SESSION_STAGED,
      UpdateStatus.DEFERRED_SESSION_STAGED,
    ].includes(currentStatus) ||
    !updateInstance
  ) {
    console.warn('[UpdaterService] Cannot install in current state:', currentStatus);
    return;
  }

  currentStatus = UpdateStatus.PREPARING_INSTALL;
  updateMetadata.error = null;
  notifyListeners();

  try {
    // 1. Mandatory Pre-Update Database Safety Gate
    console.log('[UpdaterService] Initiating pre-update SQLite WAL checkpoint...');
    const safetyCheck = await prepareForUpdate(options.customDbPath || null);

    if (!safetyCheck || safetyCheck.status !== 'verified') {
      throw new Error(
        `Database safety check verification rejected: ${JSON.stringify(safetyCheck)}`
      );
    }

    console.log('[UpdaterService] Database safety verified. Checkpoint clean, WAL=0 bytes.');

    // 2. Invoking official Tauri updater installation
    currentStatus = UpdateStatus.INSTALLING;
    notifyListeners();

    // The official Tauri updater on Windows extracts NSIS setup to %TEMP%,
    // launches setup.exe /P /UPDATE /R via ShellExecuteW, and calls std::process::exit(0).
    await updateInstance.install({ restartAfterInstall: true });
  } catch (err) {
    console.error('[UpdaterService] Update aborted by database safety gate or install failure:', err);
    currentStatus = UpdateStatus.INSTALL_ERROR;
    updateMetadata.error = `Installation aborted: ${err.message || String(err)}`;
    updateMetadata.databasePreserved = true;
    notifyListeners();
  }
}

/**
 * UI Modal Visibility Management
 */
export function openUpdateModal() {
  isModalVisible = true;
  notifyListeners();
}

export function closeUpdateModal() {
  // If download is in progress, do not allow dismiss
  if (currentStatus === UpdateStatus.DOWNLOADING || currentStatus === UpdateStatus.PREPARING_INSTALL || currentStatus === UpdateStatus.INSTALLING) {
    return;
  }
  isModalVisible = false;
  notifyListeners();
}

/**
 * Background Discovery Management
 */
export function startPeriodicChecks(options = {}) {
  const { intervalMs = 6 * 3600 * 1000, initialDelayMs = 5000 } = options;
  if (periodicCheckTimer) {
    clearInterval(periodicCheckTimer);
  }

  // Initial silent discovery after startup delay
  setTimeout(() => {
    checkForUpdates({ silent: true });
  }, initialDelayMs);

  // Periodic silent discovery
  periodicCheckTimer = setInterval(() => {
    checkForUpdates({ silent: true });
  }, intervalMs);
}

export function stopPeriodicChecks() {
  if (periodicCheckTimer) {
    clearInterval(periodicCheckTimer);
    periodicCheckTimer = null;
  }
}
