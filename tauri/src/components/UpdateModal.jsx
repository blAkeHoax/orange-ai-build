// src/components/UpdateModal.jsx - Dedicated Option 1 Update Modal Dialog
// Implements approved sequential update lifecycle:
// - Step 1: Review notes & explicit "Download Update" approval
// - Step 2: Download progress streaming (in-memory storage only)
// - Step 3: Session-scoped deferral ("Remind Me Later")
// - Step 4: Final confirmation with approved database safety wording:
//   "Orange AI v2.0.1 is ready to install. Installing will prepare Orange AI for shutdown,
//    checkpoint its database, close the application, update the application files, and restart
//    Orange AI. The update will be aborted if the required database-safety checks fail."
// - Step 5: Pre-update database WAL checkpoint and safe installation invocation

import React from 'react';
import {
  Download,
  AlertTriangle,
  CheckCircle2,
  RefreshCw,
  X,
  ShieldCheck,
  HardDrive,
  Clock,
  ArrowRight
} from 'lucide-react';
import {
  UpdateStatus,
  downloadUpdate,
  remindMeLater,
  installAndRestart,
  closeUpdateModal,
  checkForUpdates
} from '../services/updaterService';
import '../styles/UpdateModal.css';

export default function UpdateModal({ updaterState }) {
  if (!updaterState || !updaterState.isModalVisible) {
    return null;
  }

  const {
    status,
    currentVersion,
    targetVersion,
    date,
    body,
    downloadedBytes,
    totalBytes,
    percent,
    error
  } = updaterState;

  const isDownloading = status === UpdateStatus.DOWNLOADING;
  const isPreparing = status === UpdateStatus.PREPARING_INSTALL;
  const isInstalling = status === UpdateStatus.INSTALLING;
  const isChecking = status === UpdateStatus.CHECKING;
  const canClose = !isDownloading && !isPreparing && !isInstalling;

  const handleDownload = () => {
    downloadUpdate();
  };

  const handleRemindLater = () => {
    remindMeLater();
  };

  const handleInstallAndRestart = () => {
    installAndRestart();
  };

  const handleRetryDownload = () => {
    downloadUpdate();
  };

  const handleRetryCheck = () => {
    checkForUpdates({ silent: false });
  };

  const formatBytes = (bytes) => {
    if (!bytes || bytes <= 0) return '0 MB';
    const mb = bytes / (1024 * 1024);
    return `${mb.toFixed(1)} MB`;
  };

  // Approved installation confirmation statement
  const targetVerStr = targetVersion ? `v${targetVersion}` : 'v2.0.1';
  const approvedConfirmationText = `Orange AI ${targetVerStr} is ready to install. Installing will prepare Orange AI for shutdown, checkpoint its database, close the application, update the application files, and restart Orange AI. The update will be aborted if the required database-safety checks fail.`;

  return (
    <div className="update-modal-backdrop" onClick={canClose ? closeUpdateModal : undefined}>
      <div className="update-modal-card" onClick={(e) => e.stopPropagation()}>
        {/* Header */}
        <div className="update-modal-header">
          <div className="update-modal-brand">
            <div className="update-brand-icon-wrapper">
              <Download size={18} />
            </div>
            <div className="update-modal-title-group">
              <h2 className="update-modal-title">
                {status === UpdateStatus.UP_TO_DATE && 'Orange AI is Up to Date'}
                {status === UpdateStatus.CHECKING && 'Checking for Updates...'}
                {status === UpdateStatus.CHECK_ERROR && 'Update Check Failed'}
                {[UpdateStatus.UPDATE_AVAILABLE, UpdateStatus.DEFERRED_UNFETCHED].includes(status) && 'Software Update Available'}
                {status === UpdateStatus.DOWNLOADING && 'Downloading Update...'}
                {status === UpdateStatus.DOWNLOAD_ERROR && 'Download Interrupted'}
                {[UpdateStatus.DOWNLOADED_SESSION_STAGED, UpdateStatus.DEFERRED_SESSION_STAGED].includes(status) && 'Ready to Install'}
                {status === UpdateStatus.PREPARING_INSTALL && 'Preparing Safe Shutdown...'}
                {status === UpdateStatus.INSTALL_ERROR && 'Update Aborted (Data Safe)'}
                {status === UpdateStatus.INSTALLING && 'Restarting Application...'}
              </h2>
              <span className="update-modal-subtitle">
                Orange AI Desktop 2.0 • Option 1 Safe Update Lifecycle
              </span>
            </div>
          </div>
          {canClose && (
            <button
              className="update-modal-close-btn"
              onClick={closeUpdateModal}
              title="Close modal"
              type="button"
            >
              <X size={16} />
            </button>
          )}
        </div>

        {/* Body Content */}
        <div className="update-modal-body">
          {/* STATE: UP TO DATE */}
          {status === UpdateStatus.UP_TO_DATE && (
            <div className="update-safeguard-banner success">
              <CheckCircle2 size={18} className="safeguard-icon" />
              <div className="safeguard-text">
                <strong>You are on the latest version.</strong>
                <p style={{ margin: '4px 0 0 0' }}>
                  Orange AI v{currentVersion} is currently the newest available release.
                </p>
              </div>
            </div>
          )}

          {/* STATE: CHECKING */}
          {status === UpdateStatus.CHECKING && (
            <div className="update-safeguard-banner info">
              <RefreshCw size={18} className="safeguard-icon update-spinning" />
              <div className="safeguard-text">
                <strong>Contacting update server...</strong>
                <p style={{ margin: '4px 0 0 0' }}>
                  Querying the configured release endpoint. No payload will be downloaded without your approval.
                </p>
              </div>
            </div>
          )}

          {/* STATE: CHECK ERROR */}
          {status === UpdateStatus.CHECK_ERROR && (
            <div className="update-safeguard-banner danger">
              <AlertTriangle size={18} className="safeguard-icon" />
              <div className="safeguard-text">
                <strong>Could not check for updates.</strong>
                <p style={{ margin: '4px 0 0 0' }}>
                  {error || 'Network error reaching the release manifest. Please check your internet connection.'}
                </p>
              </div>
            </div>
          )}

          {/* STATE: UPDATE AVAILABLE (0 BYTES DOWNLOADED) */}
          {[UpdateStatus.UPDATE_AVAILABLE, UpdateStatus.DEFERRED_UNFETCHED].includes(status) && (
            <>
              {/* Version Comparison */}
              <div className="update-version-banner">
                <div className="update-version-col">
                  <span className="version-col-label">Current Version</span>
                  <span className="version-col-tag">v{currentVersion}</span>
                </div>
                <ArrowRight size={16} className="update-version-arrow" />
                <div className="update-version-col">
                  <span className="version-col-label">Available Update</span>
                  <span className="version-col-tag highlight">v{targetVersion}</span>
                </div>
              </div>

              {/* Release Notes */}
              <div className="update-notes-container">
                <span className="update-notes-label">
                  Changelog & Release Notes {date ? `(${new Date(date).toLocaleDateString()})` : ''}
                </span>
                <div className="update-notes-box">
                  {body || 'No release notes provided.'}
                </div>
              </div>

              {/* Explicit Download Approval Safeguard Notice */}
              <div className="update-safeguard-banner info">
                <ShieldCheck size={18} className="safeguard-icon text-orange" />
                <div className="safeguard-text">
                  <strong>Explicit Approval Required:</strong> Orange AI never downloads or installs updates automatically. The update will be retrieved only when you click <strong>Download Update</strong> below.
                </div>
              </div>
            </>
          )}

          {/* STATE: DOWNLOADING */}
          {status === UpdateStatus.DOWNLOADING && (
            <>
              <div className="update-safeguard-banner info">
                <RefreshCw size={18} className="safeguard-icon update-spinning" />
                <div className="safeguard-text">
                  <strong>Downloading Orange AI v{targetVersion}...</strong>
                  <p style={{ margin: '4px 0 0 0' }}>
                    The update payload is held securely in session memory and will not be executed or installed without confirmation.
                  </p>
                </div>
              </div>

              <div className="update-progress-container">
                <div className="update-progress-bar-bg">
                  <div
                    className="update-progress-bar-fill"
                    style={{ width: `${percent}%` }}
                  />
                </div>
                <div className="update-progress-metrics">
                  <span>{percent}% Complete</span>
                  <span>
                    {formatBytes(downloadedBytes)} / {formatBytes(totalBytes)}
                  </span>
                </div>
              </div>
            </>
          )}

          {/* STATE: DOWNLOAD ERROR */}
          {status === UpdateStatus.DOWNLOAD_ERROR && (
            <div className="update-safeguard-banner danger">
              <AlertTriangle size={18} className="safeguard-icon" />
              <div className="safeguard-text">
                <strong>Download failed:</strong>
                <p style={{ margin: '4px 0 0 0' }}>
                  {error || 'An unexpected network error occurred while retrieving the update package.'}
                </p>
              </div>
            </div>
          )}

          {/* STATE: DOWNLOADED / READY TO INSTALL (EXACT APPROVED WORDING) */}
          {[UpdateStatus.DOWNLOADED_SESSION_STAGED, UpdateStatus.DEFERRED_SESSION_STAGED].includes(status) && (
            <>
              <div className="update-safeguard-banner success">
                <ShieldCheck size={18} className="safeguard-icon" />
                <div className="safeguard-text">
                  <strong>Update Package Verified & Held in Session Memory</strong>
                  <p style={{ margin: '4px 0 0 0' }}>
                    Orange AI v{targetVersion} has been successfully downloaded into memory.
                  </p>
                </div>
              </div>

              <div className="update-confirmation-box">
                <p className="update-confirmation-statement">
                  "{approvedConfirmationText}"
                </p>
                <p className="update-session-warning">
                  <strong>Session Notice:</strong> The downloaded update is held in session memory. If you click <em>Remind Me Later</em> and close Orange AI, the in-memory update is discarded and must be downloaded again on next launch.
                </p>
              </div>
            </>
          )}

          {/* STATE: PREPARING INSTALL */}
          {status === UpdateStatus.PREPARING_INSTALL && (
            <div className="update-safeguard-banner info">
              <HardDrive size={18} className="safeguard-icon update-spinning" />
              <div className="safeguard-text">
                <strong>Enforcing SQLite Database Safety Gate...</strong>
                <p style={{ margin: '4px 0 0 0' }}>
                  Halting background tasks, rejecting new turns, and executing <code>PRAGMA wal_checkpoint(TRUNCATE)</code>. If checkpoint verification fails, installation will be aborted immediately.
                </p>
              </div>
            </div>
          )}

          {/* STATE: INSTALL ERROR */}
          {status === UpdateStatus.INSTALL_ERROR && (
            <>
              <div className="update-safeguard-banner danger">
                <AlertTriangle size={18} className="safeguard-icon" />
                <div className="safeguard-text">
                  <strong>Update Aborted — Database Safeguard Triggered:</strong>
                  <p style={{ margin: '4px 0 0 0' }}>
                    {error || 'Pre-update safety checks could not confirm complete database truncation.'}
                  </p>
                </div>
              </div>

              <div className="update-safeguard-banner success">
                <ShieldCheck size={18} className="safeguard-icon" />
                <div className="safeguard-text">
                  <strong>Database Preserved:</strong>
                  <p style={{ margin: '4px 0 0 0' }}>
                    Your operational database in <code>%LOCALAPPDATA%\Orange AI\memory.db</code> remains untouched and safe. Orange AI prevented installation to protect your conversation records.
                  </p>
                </div>
              </div>
            </>
          )}

          {/* STATE: INSTALLING */}
          {status === UpdateStatus.INSTALLING && (
            <div className="update-safeguard-banner success">
              <RefreshCw size={18} className="safeguard-icon update-spinning" />
              <div className="safeguard-text">
                <strong>Database Checkpoint Verified. Launching Installer...</strong>
                <p style={{ margin: '4px 0 0 0' }}>
                  Orange AI will close shortly. The installer will update application files and restart Orange AI automatically.
                </p>
              </div>
            </div>
          )}
        </div>

        {/* Footer Actions */}
        <div className="update-modal-footer">
          {/* UP TO DATE */}
          {status === UpdateStatus.UP_TO_DATE && (
            <button
              className="btn-update-primary"
              onClick={closeUpdateModal}
              type="button"
            >
              OK
            </button>
          )}

          {/* CHECK ERROR */}
          {status === UpdateStatus.CHECK_ERROR && (
            <>
              <button
                className="btn-update-secondary"
                onClick={closeUpdateModal}
                type="button"
              >
                Dismiss
              </button>
              <button
                className="btn-update-primary"
                onClick={handleRetryCheck}
                type="button"
              >
                <RefreshCw size={14} />
                <span>Retry</span>
              </button>
            </>
          )}

          {/* UPDATE AVAILABLE (OPTION 1: DOWNLOAD OR REMIND LATER) */}
          {[UpdateStatus.UPDATE_AVAILABLE, UpdateStatus.DEFERRED_UNFETCHED].includes(status) && (
            <>
              <button
                className="btn-update-secondary"
                onClick={handleRemindLater}
                type="button"
              >
                <Clock size={14} style={{ marginRight: 6 }} />
                Remind Me Later
              </button>
              <button
                className="btn-update-primary"
                onClick={handleDownload}
                type="button"
              >
                <Download size={14} />
                <span>Download Update</span>
              </button>
            </>
          )}

          {/* DOWNLOADING (IN PROGRESS) */}
          {status === UpdateStatus.DOWNLOADING && (
            <button
              className="btn-update-secondary"
              disabled
              type="button"
            >
              <RefreshCw size={14} className="update-spinning" style={{ marginRight: 6 }} />
              Downloading ({percent}%)...
            </button>
          )}

          {/* DOWNLOAD ERROR */}
          {status === UpdateStatus.DOWNLOAD_ERROR && (
            <>
              <button
                className="btn-update-secondary"
                onClick={closeUpdateModal}
                type="button"
              >
                Dismiss
              </button>
              <button
                className="btn-update-primary"
                onClick={handleRetryDownload}
                type="button"
              >
                <RefreshCw size={14} />
                <span>Retry Download</span>
              </button>
            </>
          )}

          {/* READY TO INSTALL (EXPLICIT CONFIRMATION) */}
          {[UpdateStatus.DOWNLOADED_SESSION_STAGED, UpdateStatus.DEFERRED_SESSION_STAGED].includes(status) && (
            <>
              <button
                className="btn-update-secondary"
                onClick={handleRemindLater}
                type="button"
              >
                <Clock size={14} style={{ marginRight: 6 }} />
                Remind Me Later
              </button>
              <button
                className="btn-update-primary"
                onClick={handleInstallAndRestart}
                type="button"
              >
                <RefreshCw size={14} />
                <span>Install & Restart Now</span>
              </button>
            </>
          )}

          {/* PREPARING INSTALL */}
          {status === UpdateStatus.PREPARING_INSTALL && (
            <button
              className="btn-update-primary"
              disabled
              type="button"
            >
              <RefreshCw size={14} className="update-spinning" />
              <span>Checkpointing Database...</span>
            </button>
          )}

          {/* INSTALL ERROR */}
          {status === UpdateStatus.INSTALL_ERROR && (
            <button
              className="btn-update-secondary"
              onClick={closeUpdateModal}
              type="button"
            >
              Close
            </button>
          )}

          {/* INSTALLING */}
          {status === UpdateStatus.INSTALLING && (
            <button
              className="btn-update-primary"
              disabled
              type="button"
            >
              <RefreshCw size={14} className="update-spinning" />
              <span>Restarting...</span>
            </button>
          )}
        </div>
      </div>
    </div>
  );
}
