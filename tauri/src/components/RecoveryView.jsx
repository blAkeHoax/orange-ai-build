// src/components/RecoveryView.jsx - Dedicated Zero-Python Recovery Interface
// Stage 4.5B Phase 3: Runtime Handshake & Recovery View
import React, { useState } from 'react';
import {
  AlertTriangle,
  Download,
  Folder,
  RefreshCw,
  XCircle,
  ShieldCheck,
  ExternalLink,
  Terminal
} from 'lucide-react';
import TitleBar from './TitleBar';
import {
  openOfficialReleases,
  openDataFolder,
  closeWindow
} from '../services/tauriBridge';
import '../styles/RecoveryView.css';

export default function RecoveryView({ diagnostic, onRetry }) {
  const [isRetrying, setIsRetrying] = useState(false);
  const [retryMessage, setRetryMessage] = useState(null);

  const handleDownloadInstaller = () => {
    openOfficialReleases();
  };

  const handleOpenDataFolder = () => {
    openDataFolder();
  };

  const handleRetry = async () => {
    setIsRetrying(true);
    setRetryMessage('Retrying runtime handshake...');
    try {
      if (onRetry) {
        await onRetry();
      }
    } catch (err) {
      setRetryMessage(`Retry failed: ${String(err)}`);
    } finally {
      setIsRetrying(false);
    }
  };

  const handleExit = () => {
    closeWindow();
  };

  // Format diagnostic summary safely (zero secrets or internal paths exposed)
  const formatDiagnosticDetails = () => {
    if (!diagnostic) {
      return {
        title: 'Runtime Unavailable',
        description: 'Unable to communicate with the Python runtime engine.',
        badge: 'UNAVAILABLE'
      };
    }

    const status = diagnostic.status;
    const details = diagnostic.details || {};

    switch (status) {
      case 'IncompatibleRuntimeVersion':
        return {
          title: 'Runtime Version Incompatibility',
          description: `Desktop application version (v${details.desktop_version || '2.0.0'}) does not match Python runtime adapter version (v${details.runtime_version || 'unknown'}).`,
          badge: 'VERSION MISMATCH'
        };
      case 'UnsupportedProtocolVersion':
        return {
          title: 'Protocol Version Incompatibility',
          description: `Desktop application expected protocol v${details.expected_protocol || '1.0'}, but runtime adapter reported protocol v${details.received_protocol || 'unknown'}.`,
          badge: 'PROTOCOL MISMATCH'
        };
      case 'StartupFailed':
        return {
          title: 'Runtime Process Startup Failure',
          description: 'The Python runtime process could not be spawned. The bundled runtime executable may be missing or damaged from an interrupted update.',
          badge: 'SPAWN FAILED'
        };
      case 'Timeout':
        return {
          title: 'Startup Handshake Timeout',
          description: 'The Python runtime process was launched, but failed to complete the initialization handshake within the 5.0-second safety limit.',
          badge: 'TIMEOUT'
        };
      case 'ProcessTerminated':
        return {
          title: 'Runtime Process Terminated',
          description: 'The Python runtime process terminated unexpectedly during or immediately after the startup handshake.',
          badge: 'TERMINATED'
        };
      case 'MissingVersion':
        return {
          title: 'Missing Runtime Version',
          description: 'The runtime adapter responded to the startup handshake, but failed to declare a recognized version string.',
          badge: 'MALFORMED RESPONSE'
        };
      case 'InvalidResponse':
        return {
          title: 'Invalid Handshake Response',
          description: 'The runtime adapter returned a malformed or non-compliant JSON-RPC response during the initialization handshake.',
          badge: 'PROTOCOL ERROR'
        };
      default:
        return {
          title: 'Runtime Integrity Check Failed',
          description: typeof details.error === 'string' ? details.error : 'Startup integrity check did not pass.',
          badge: status || 'ERROR'
        };
    }
  };

  const diagInfo = formatDiagnosticDetails();

  return (
    <div className="recovery-viewport">
      <TitleBar />

      <div className="recovery-content-container">
        <div className="recovery-card">
          {/* Header & Icon */}
          <div className="recovery-header">
            <div className="recovery-icon-badge">
              <AlertTriangle size={32} className="recovery-icon" />
            </div>
            <div className="recovery-badge-tag">{diagInfo.badge}</div>
            <h1 className="recovery-title">Orange AI Recovery Mode</h1>
            <p className="recovery-subtitle">
              Orange AI detected an incomplete update or runtime mismatch. Its recovery process is
              designed to preserve your operational database. Normal application operations will
              remain blocked until runtime integrity can be established.
            </p>
          </div>

          {/* Diagnostic Details Box */}
          <div className="recovery-diagnostic-box">
            <div className="diagnostic-row header-row">
              <Terminal size={16} className="diag-icon" />
              <span className="diag-title">{diagInfo.title}</span>
            </div>
            <p className="diagnostic-description">{diagInfo.description}</p>
            {diagnostic?.details?.desktop_version && (
              <div className="diagnostic-version-grid">
                <div className="version-pill">
                  <span className="pill-label">Desktop App:</span>
                  <span className="pill-val">v{diagnostic.details.desktop_version}</span>
                </div>
                {diagnostic.details.runtime_version && (
                  <div className="version-pill">
                    <span className="pill-label">Python Runtime:</span>
                    <span className="pill-val">v{diagnostic.details.runtime_version}</span>
                  </div>
                )}
                {diagnostic.details.protocol_version && (
                  <div className="version-pill">
                    <span className="pill-label">Protocol:</span>
                    <span className="pill-val">v{diagnostic.details.protocol_version}</span>
                  </div>
                )}
              </div>
            )}
          </div>

          {/* Operational Database Safeguard Notice */}
          <div className="recovery-safeguard-box">
            <div className="safeguard-header">
              <ShieldCheck size={18} className="safeguard-icon" />
              <span className="safeguard-title">Operational Database Safeguard Notice</span>
            </div>
            <p className="safeguard-text">
              Your operational database in <code>%LOCALAPPDATA%\Orange AI\memory.db</code> is protected by pre-update shutdown and checkpoint safeguards. No automated repair will alter or reset your existing user data.
            </p>
          </div>

          {/* Designated Recovery Guidance */}
          <div className="recovery-guidance-box">
            <h3 className="guidance-heading">Designated Recovery Procedure</h3>
            <ol className="guidance-steps">
              <li>
                Visit the official Orange AI GitHub Releases page to obtain the official standalone installer.
              </li>
              <li>
                Run the standalone installer (<code>Orange AI_2.0.0_x64-setup.exe</code>) to perform a clean repair over this installation directory.
              </li>
              <li>
                Your conversational memory and local settings will remain preserved.
              </li>
            </ol>
            <p className="guidance-notice">
              * Note: The standalone installer on GitHub Releases is the designated recovery path. Releases must be officially published before installer downloads become active.
            </p>
          </div>

          {/* Feedback / Status */}
          {retryMessage && (
            <div className="recovery-feedback-message">
              {retryMessage}
            </div>
          )}

          {/* Action Buttons */}
          <div className="recovery-actions">
            <button
              className="recovery-btn btn-primary"
              onClick={handleDownloadInstaller}
              type="button"
            >
              <Download size={16} />
              <span>Download Official Installer</span>
              <ExternalLink size={14} className="btn-subicon" />
            </button>

            <button
              className="recovery-btn btn-secondary"
              onClick={handleOpenDataFolder}
              type="button"
            >
              <Folder size={16} />
              <span>Open Data Folder</span>
            </button>

            <button
              className="recovery-btn btn-secondary"
              onClick={handleRetry}
              disabled={isRetrying}
              type="button"
            >
              <RefreshCw size={16} className={isRetrying ? 'spin' : ''} />
              <span>{isRetrying ? 'Checking...' : 'Retry Connection'}</span>
            </button>

            <button
              className="recovery-btn btn-ghost-danger"
              onClick={handleExit}
              type="button"
            >
              <XCircle size={16} />
              <span>Exit Application</span>
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}
