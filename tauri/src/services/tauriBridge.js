// src/services/tauriBridge.js - Shared Tauri 2 IPC Integration Layer for Orange AI Desktop 2.0
// Connects React to the Rust supervisor and Python runtime adapter via JSON-RPC.

let tauriCore = null;
let tauriEvent = null;

async function getTauri() {
  if (typeof window !== 'undefined' && window.__TAURI_INTERNALS__) {
    if (!tauriCore) {
      tauriCore = await import('@tauri-apps/api/core');
    }
    if (!tauriEvent) {
      tauriEvent = await import('@tauri-apps/api/event');
    }
    return { core: tauriCore, event: tauriEvent };
  }
  return null;
}

/**
 * Low-level invocation of a Python runtime JSON-RPC method through Rust core.
 */
export async function callRuntime(method, params = {}) {
  const tauri = await getTauri();
  if (tauri) {
    try {
      return await tauri.core.invoke('call_runtime', { method, params });
    } catch (err) {
      console.error(`[TauriBridge] Error invoking '${method}':`, err);
      throw err;
    }
  }
  console.warn(`[TauriBridge Mock] callRuntime '${method}' not available in non-Tauri preview`);
  return null;
}

// ==========================================
// Stage 4.5B Phase 3: Runtime Handshake & Recovery
// ==========================================

export async function getHandshakeStatus() {
  const tauri = await getTauri();
  if (tauri) {
    try {
      return await tauri.core.invoke('get_handshake_status');
    } catch (err) {
      console.error('[TauriBridge] Error getting handshake status:', err);
      return { status: 'StartupFailed', details: { error: String(err) } };
    }
  }
  // Fallback for non-Tauri preview environments
  if (typeof window !== 'undefined' && window.location.search.includes('recovery=true')) {
    return {
      status: 'IncompatibleRuntimeVersion',
      details: {
        desktop_version: '2.0.0',
        runtime_version: '2.0.1'
      }
    };
  }
  return {
    status: 'Ready',
    details: {
      desktop_version: '2.0.0',
      runtime_version: '2.0.0',
      protocol_version: '1.0',
      pid: 1234,
      capabilities: ['chat', 'memory', 'planner', 'tools', 'pre_update_quiescence']
    }
  };
}

export async function retryHandshake() {
  const tauri = await getTauri();
  if (tauri) {
    try {
      return await tauri.core.invoke('retry_handshake');
    } catch (err) {
      console.error('[TauriBridge] Error retrying handshake:', err);
      return { status: 'StartupFailed', details: { error: String(err) } };
    }
  }
  return { status: 'Ready', details: { desktop_version: '2.0.0', runtime_version: '2.0.0', protocol_version: '1.0', pid: 1234 } };
}

export async function openDataFolder() {
  const tauri = await getTauri();
  if (tauri) {
    return await tauri.core.invoke('open_data_folder');
  }
  console.log('[Mock] openDataFolder called');
}

export async function openOfficialReleases() {
  const tauri = await getTauri();
  if (tauri) {
    return await tauri.core.invoke('open_official_releases');
  }
  window.open('https://github.com/blAkeHoax/orange-ai/releases/latest', '_blank');
}

/**
 * Pre-update SQLite database checkpoint and runtime shutdown verification gate.
 * Invokes Rust `prepare_for_update` which executes `system.shutdown_for_update`
 * and verifies that the database WAL sidecar is truncated to 0 bytes and safe.
 */
export async function prepareForUpdate(customDbPath = null) {
  const tauri = await getTauri();
  if (tauri) {
    try {
      return await tauri.core.invoke('prepare_for_update', { customDbPath });
    } catch (err) {
      console.error('[TauriBridge] Pre-update database safety check failed:', err);
      throw err;
    }
  }
  // Mock / preview mode fallback
  if (typeof window !== 'undefined' && window.__mockPreUpdateFailure) {
    throw new Error('Database WAL checkpoint busy: [1, 5, 0]');
  }
  return { status: 'verified', wal_bytes: 0, db_path: customDbPath || 'memory.db' };
}

// ==========================================
// System & Health
// ==========================================

export async function pingAdapter() {
  const tauri = await getTauri();
  if (tauri) {
    return await tauri.core.invoke('ping');
  }
  return { status: 'mock', version: '2.0.0', api_online: false };
}

export async function getHealth() {
  const tauri = await getTauri();
  if (tauri) {
    return await tauri.core.invoke('get_health');
  }
  return { health: 'ok', uptime_seconds: 0 };
}

export async function shutdownRuntime() {
  const tauri = await getTauri();
  if (tauri) {
    return await tauri.core.invoke('shutdown');
  }
}

// ==========================================
// Chat & Conversation
// ==========================================

export async function sendMessage(text) {
  const tauri = await getTauri();
  if (tauri) {
    return await tauri.core.invoke('send_message', { text });
  }
  return { reply: `Mock response to: "${text}"`, model: 'mock' };
}

export async function confirmAction(confirmationId, decision) {
  const tauri = await getTauri();
  if (tauri) {
    return await tauri.core.invoke('confirm_action', { confirmationId, decision });
  }
  return { status: 'resolved', decision };
}

export async function getChatHistory(limit = 60) {
  return await callRuntime('chat.history', { limit });
}

// ==========================================
// Agent Jobs
// ==========================================

export async function getJobs(status = null, limit = 100) {
  const res = await callRuntime('jobs.list', { status, limit });
  return Array.isArray(res) ? res : (res?.jobs || []);
}

export async function getJobStatus(jobId) {
  return await callRuntime('jobs.status', { job_id: jobId });
}

export async function getJobHistory(jobId) {
  return await callRuntime('jobs.history', { job_id: jobId });
}

export async function pauseJob(jobId) {
  return await callRuntime('jobs.pause', { job_id: jobId });
}

export async function resumeJob(jobId) {
  return await callRuntime('jobs.resume', { job_id: jobId });
}

export async function cancelJob(jobId) {
  return await callRuntime('jobs.cancel', { job_id: jobId });
}

export async function confirmJob(jobId, approved) {
  return await callRuntime('jobs.confirm', { job_id: jobId, approved });
}

// ==========================================
// Memory
// ==========================================

export async function getMemoryStats() {
  const res = await callRuntime('memory.stats', {});
  return res || { structured_count: 0, legacy_count: 0, total_memories: 0 };
}

export async function getStructuredMemory() {
  const res = await callRuntime('memory.structured', {});
  return Array.isArray(res) ? res : [];
}

export async function getLegacyMemory(limit = 60) {
  const res = await callRuntime('memory.legacy', { limit });
  return Array.isArray(res) ? res : [];
}

export async function retractMemory(category, key) {
  return await callRuntime('memory.retract', { category, key });
}

export async function forgetMemory(keyword) {
  return await callRuntime('memory.forget', { keyword });
}

export async function rememberFact(fact) {
  return await callRuntime('memory.remember', { fact });
}

// ==========================================
// Activity & Audits
// ==========================================

export async function getActivity() {
  const res = await callRuntime('activity.get', {});
  return res || { logs: [], job_steps: [], conversations: [] };
}

// ==========================================
// Permissions & Security
// ==========================================

export async function getPermissionsAudit() {
  const res = await callRuntime('permissions.audit', {});
  return res || { admin_rights: false, tools: [] };
}

// ==========================================
// Tools
// ==========================================

export async function getTools() {
  const res = await callRuntime('tools.list', {});
  return Array.isArray(res) ? res : [];
}

// ==========================================
// Settings
// ==========================================

export async function getSettings() {
  const res = await callRuntime('settings.get', {});
  return res || {};
}

export async function updateSettings(settings) {
  return await callRuntime('settings.update', { settings });
}

// ==========================================
// Window Controls
// ==========================================

export async function minimizeWindow() {
  const tauri = await getTauri();
  if (tauri) {
    return await tauri.core.invoke('minimize_window');
  }
}

export async function toggleMaximizeWindow() {
  const tauri = await getTauri();
  if (tauri) {
    return await tauri.core.invoke('toggle_maximize_window');
  }
}

export async function closeWindow() {
  const tauri = await getTauri();
  if (tauri) {
    return await tauri.core.invoke('close_window');
  }
}

// ==========================================
// External Navigation & Security
// ==========================================

export function openExternalUrl(url) {
  if (!url || typeof url !== 'string') return;
  const trimmed = url.trim();
  // Strictly validate protocol: only allow http:// and https://
  if (!/^https?:\/\//i.test(trimmed)) {
    console.warn('[Security] Blocked non-http(s) external URL navigation:', trimmed);
    return;
  }
  // Open safely in system default browser without internal webview navigation
  try {
    window.open(trimmed, '_blank', 'noopener,noreferrer');
  } catch (err) {
    console.error('Failed to open external URL:', err);
  }
}

// ==========================================
// Event Subscriptions & Proactive Intelligence
// ==========================================

export async function acknowledgeEvent(eventId, eventType = null) {
  return await callRuntime('events.ack', { event_id: eventId, event_type: eventType });
}

export async function getUnacknowledgedEvents(limit = 50) {
  const res = await callRuntime('events.get_unacked', { limit });
  return Array.isArray(res) ? res : [];
}

export async function dismissRecommendation(recId) {
  return await callRuntime('recommendations.dismiss', { id: recId });
}

export async function snoozeRecommendation(recId, duration = '24h') {
  return await callRuntime('recommendations.snooze', { id: recId, duration });
}

export async function executeRecommendation(recId, confirmed = null, confirmationToken = null) {
  const params = { id: recId };
  if (confirmed !== null) {
    params.confirmed = confirmed;
  }
  if (confirmationToken) {
    params.confirmation_token = confirmationToken;
  }
  return await callRuntime('recommendations.execute', params);
}

export async function getActiveRecommendations() {
  const res = await callRuntime('recommendations.get_active', {});
  return Array.isArray(res) ? res : (res?.recommendations || []);
}

export async function getLatestReconciliations(workspacePath = null) {
  const res = await callRuntime('reconciliation.get_latest', { workspace_path: workspacePath });
  return Array.isArray(res) ? res : (res?.reconciliations || []);
}

export async function getLatestBriefing() {
  return await callRuntime('briefing.get_latest', {});
}

export async function listBriefings(limit = 10) {
  const res = await callRuntime('briefing.list', { limit });
  return Array.isArray(res) ? res : (res?.briefings || []);
}

export async function onEvent(eventName, callback) {
  const tauri = await getTauri();
  if (tauri) {
    const unlisteners = [];
    const sanitized = eventName.replace(/\./g, '_');
    const u1 = await tauri.event.listen(sanitized, (event) => {
      callback(event.payload);
    });
    unlisteners.push(u1);
    if (sanitized !== eventName) {
      const u2 = await tauri.event.listen(eventName, (event) => {
        callback(event.payload);
      });
      unlisteners.push(u2);
    }
    return () => {
      unlisteners.forEach((u) => {
        try { u(); } catch (_) {}
      });
    };
  }
  return () => {};
}
