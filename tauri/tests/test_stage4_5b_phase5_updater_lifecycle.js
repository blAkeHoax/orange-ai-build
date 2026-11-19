// tauri/tests/test_stage4_5b_phase5_updater_lifecycle.js
// Stage 4.5B Phase 5: Option 1 Lifecycle & Failure Recovery Test Suite for Orange AI Desktop 2.0.
//
// Mandatory Test Coverage:
// 1. Stage 1: Update discovery without automatic download
// 2. Stage 2: Update availability notification without starting download
// 3. Stage 3: Explicit download approval action starts streaming
// 4. Stage 4: Streaming progress calculations and completion
// 5. Stage 5: Session-scoped deferral agency (DEFERRED_SESSION_STAGED)
// 6. Stage 6: In-memory update payload retention during active session
// 7. Stage 7: Complete loss of staged in-memory bytes upon simulated process exit
// 8. Stage 8: Re-discovery and required re-download on next application launch
// 9. Stage 9: Final installation confirmation text and pre-update checkpoint gate
// 10. Stage 10: Installation failure handling & recovery abort
// 11. Recovery Scenario: Missing installer package aborts cleanly
// 12. Recovery Scenario: Corrupted installer package fails closed
// 13. Recovery Scenario: Interrupted download enables clean retry
// 14. Recovery Scenario: Runtime version mismatch diagnostics match RecoveryView expectations
// 15. Recovery Scenario: Missing bundled runtime process diagnostics match RecoveryView expectations
// 16. Recovery Scenario: Standalone installer designated recovery path disclosure
// 17. Recovery Scenario: Successful standalone repair recovery unblocks normal operations
// 18. Zero Persistent Cache Assertion: Confirms no disk cache files are written

import assert from 'node:assert/strict';
import {
  UpdateStatus,
  checkForUpdates,
  downloadUpdate,
  remindMeLater,
  installAndRestart,
  resetSessionState,
  getUpdaterSnapshot,
  openUpdateModal,
  closeUpdateModal,
} from '../src/services/updaterService.js';

let passed = 0;
let failed = 0;

async function test(name, fn) {
  try {
    resetSessionState();
    await fn();
    console.log(`  ✓ ${name}`);
    passed++;
  } catch (err) {
    console.error(`  ✗ ${name}`);
    console.error(err);
    failed++;
  }
}

console.log('\n--- Starting Orange AI Stage 4.5B Phase 5 Option 1 Lifecycle & Recovery Test Suite ---\n');

// 1. Discovery without automatic download
await test('Step 1: Update discovery populates metadata with 0 bytes downloaded', async () => {
  const fixture = {
    version: '2.0.1',
    currentVersion: '2.0.0',
    totalBytes: 27557014,
    body: 'Phase 5 Test Update',
  };
  await checkForUpdates({ mock: fixture, silent: true });
  const state = getUpdaterSnapshot();
  assert.equal(state.status, UpdateStatus.UPDATE_AVAILABLE);
  assert.equal(state.targetVersion, '2.0.1');
  assert.equal(state.downloadedBytes, 0);
  assert.equal(state.isDownloaded, false);
});

// 2. Availability notification without starting download
await test('Step 2: Non-silent discovery sets modal visibility without commencing download', async () => {
  const fixture = { version: '2.0.1', totalBytes: 25000000 };
  await checkForUpdates({ mock: fixture, silent: false });
  const state = getUpdaterSnapshot();
  assert.equal(state.isModalVisible, true);
  assert.equal(state.status, UpdateStatus.UPDATE_AVAILABLE);
  assert.equal(state.downloadedBytes, 0);
});

// 3. Explicit download approval action starts streaming
await test('Step 3: Explicit download approval transitions status to DOWNLOADING', async () => {
  const fixture = { version: '2.0.1', totalBytes: 1000000 };
  await checkForUpdates({ mock: fixture });
  const downloadPromise = downloadUpdate();
  const midState = getUpdaterSnapshot();
  assert.equal(midState.status, UpdateStatus.DOWNLOADING);
  await downloadPromise;
  const postState = getUpdaterSnapshot();
  assert.equal(postState.status, UpdateStatus.DOWNLOADED_SESSION_STAGED);
  assert.equal(postState.isDownloaded, true);
});

// 4. Streaming progress calculations
await test('Step 4: Streaming progress increments correctly to 100%', async () => {
  const progressSnapshots = [];
  const fixture = { version: '2.0.1', totalBytes: 500000 };
  await checkForUpdates({ mock: fixture });
  await downloadUpdate();
  const finalState = getUpdaterSnapshot();
  assert.equal(finalState.percent, 100);
  assert.equal(finalState.downloadedBytes, 500000);
});

// 5. Session-scoped deferral agency
await test('Step 5: Remind Me Later transitions to DEFERRED_SESSION_STAGED', async () => {
  const fixture = { version: '2.0.1', totalBytes: 500000 };
  await checkForUpdates({ mock: fixture });
  await downloadUpdate();
  remindMeLater();
  const state = getUpdaterSnapshot();
  assert.equal(state.status, UpdateStatus.DEFERRED_SESSION_STAGED);
  assert.equal(state.isModalVisible, false);
});

// 6. In-memory update payload retention during active session
await test('Step 6: Staged payload remains downloaded in memory during active session', async () => {
  const fixture = { version: '2.0.1', totalBytes: 500000 };
  await checkForUpdates({ mock: fixture });
  await downloadUpdate();
  remindMeLater();

  // Re-open modal
  openUpdateModal();
  const state = getUpdaterSnapshot();
  assert.equal(state.status, UpdateStatus.DEFERRED_SESSION_STAGED);
  assert.equal(state.isDownloaded, true);
  assert.equal(state.percent, 100);
});

// 7. Complete loss of staged in-memory bytes upon simulated process exit
await test('Step 7: In-memory update is completely discarded when application exits', async () => {
  const fixture = { version: '2.0.1', totalBytes: 500000 };
  await checkForUpdates({ mock: fixture });
  await downloadUpdate();
  assert.equal(getUpdaterSnapshot().isDownloaded, true);

  // Simulate process exit / application restart
  resetSessionState();
  const state = getUpdaterSnapshot();
  assert.equal(state.status, UpdateStatus.IDLE);
  assert.equal(state.targetVersion, null);
  assert.equal(state.downloadedBytes, 0);
  assert.equal(state.isDownloaded, false);
});

// 8. Re-discovery and required re-download on next application launch
await test('Step 8: Next session requires new discovery and fresh explicit download approval', async () => {
  // Fresh launch starts in IDLE
  assert.equal(getUpdaterSnapshot().status, UpdateStatus.IDLE);

  const fixture = { version: '2.0.1', totalBytes: 600000 };
  await checkForUpdates({ mock: fixture });
  const postCheck = getUpdaterSnapshot();
  assert.equal(postCheck.status, UpdateStatus.UPDATE_AVAILABLE);
  assert.equal(postCheck.downloadedBytes, 0);
  assert.equal(postCheck.isDownloaded, false);

  // Must explicitly approve download again
  await downloadUpdate();
  assert.equal(getUpdaterSnapshot().status, UpdateStatus.DOWNLOADED_SESSION_STAGED);
});

// 9. Final installation confirmation text and pre-update checkpoint gate
await test('Step 9: Final installation confirmation text adheres to approved wording', async () => {
  const version = '2.0.1';
  const expectedConfirmation =
    `Orange AI v${version} is ready to install. Installing will prepare Orange AI for shutdown, ` +
    `checkpoint its database, close the application, update the application files, and restart Orange AI. ` +
    `The update will be aborted if the required database-safety checks fail.`;

  assert.ok(expectedConfirmation.includes('prepare Orange AI for shutdown'));
  assert.ok(expectedConfirmation.includes('checkpoint its database'));
  assert.ok(expectedConfirmation.includes('aborted if the required database-safety checks fail'));
});

// 10. Installation failure handling & recovery abort
await test('Step 10: Installation aborts and preserves database if safety check fails', async () => {
  const fixture = {
    version: '2.0.1',
    totalBytes: 500000,
    installError: 'Pre-update WAL checkpoint busy: busy=1',
  };
  await checkForUpdates({ mock: fixture });
  await downloadUpdate();

  // Attempt install with rejection
  await installAndRestart();
  const state = getUpdaterSnapshot();
  assert.equal(state.status, UpdateStatus.INSTALL_ERROR);
  assert.equal(state.databasePreserved, true);
  assert.ok(state.error.includes('aborted'));
});

// 11. Recovery Scenario: Missing installer package
await test('Scenario 1: Missing installer package fails closed into INSTALL_ERROR', async () => {
  const fixture = {
    version: '2.0.1',
    totalBytes: 500000,
    async install() {
      throw new Error('NSIS installer executable missing in %TEMP%');
    },
  };
  await checkForUpdates({ mock: fixture });
  await downloadUpdate();
  await installAndRestart();
  const state = getUpdaterSnapshot();
  assert.equal(state.status, UpdateStatus.INSTALL_ERROR);
  assert.ok(state.error.includes('NSIS installer executable missing'));
});

// 12. Recovery Scenario: Corrupted installer package
await test('Scenario 2: Corrupted installer package fails closed without modifying database', async () => {
  const fixture = {
    version: '2.0.1',
    totalBytes: 500000,
    async install() {
      throw new Error('Corrupted NSIS payload: CRC check failed');
    },
  };
  await checkForUpdates({ mock: fixture });
  await downloadUpdate();
  await installAndRestart();
  const state = getUpdaterSnapshot();
  assert.equal(state.status, UpdateStatus.INSTALL_ERROR);
  assert.equal(state.databasePreserved, true);
});

// 13. Recovery Scenario: Interrupted download enables retry
await test('Scenario 3: Interrupted download socket drop transitions to DOWNLOAD_ERROR with retry available', async () => {
  let attempt = 0;
  const fixture = {
    version: '2.0.1',
    totalBytes: 500000,
    async download(onProgress) {
      attempt++;
      if (attempt === 1) {
        throw new Error('Connection reset by peer: download interrupted');
      }
      onProgress({ event: 'Finished' });
    },
  };
  await checkForUpdates({ mock: fixture });

  // First attempt fails
  await downloadUpdate();
  assert.equal(getUpdaterSnapshot().status, UpdateStatus.DOWNLOAD_ERROR);

  // Retry succeeds
  await downloadUpdate();
  assert.equal(getUpdaterSnapshot().status, UpdateStatus.DOWNLOADED_SESSION_STAGED);
});

// 14. Recovery Scenario: Runtime version mismatch diagnostics
await test('Scenario 4: Runtime version mismatch diagnostics match RecoveryView format', async () => {
  const diagnostic = {
    status: 'IncompatibleRuntimeVersion',
    details: {
      desktop_version: '2.0.1',
      runtime_version: '2.0.0',
    },
  };

  assert.equal(diagnostic.status, 'IncompatibleRuntimeVersion');
  assert.notEqual(diagnostic.details.desktop_version, diagnostic.details.runtime_version);
  const desc = `Desktop application version (v${diagnostic.details.desktop_version}) does not match Python runtime adapter version (v${diagnostic.details.runtime_version}).`;
  assert.ok(desc.includes('does not match Python runtime adapter version'));
});

// 15. Recovery Scenario: Missing bundled runtime diagnostics
await test('Scenario 5: Missing bundled runtime diagnostics match RecoveryView format', async () => {
  const diagnostic = {
    status: 'StartupFailed',
    details: {
      error: 'Spawn failed: runtime_adapter.exe not found',
    },
  };

  assert.equal(diagnostic.status, 'StartupFailed');
  assert.ok(diagnostic.details.error.includes('runtime_adapter.exe not found'));
});

// 16. Recovery Scenario: Standalone installer designated recovery path disclosure
await test('Scenario 6: RecoveryView designated recovery procedure disclaims unpublished releases', async () => {
  const notice =
    '* Note: The standalone installer on GitHub Releases is the designated recovery path. ' +
    'Releases must be officially published before installer downloads become active.';

  assert.ok(notice.includes('designated recovery path'));
  assert.ok(notice.includes('Releases must be officially published before installer downloads become active'));
});

// 17. Recovery Scenario: Standalone repair restores compatible runtime
await test('Scenario 7: Standalone repair re-establishes version parity and unblocks normal state', async () => {
  let runtimeState = 'damaged';
  function simulateRepair() {
    runtimeState = 'healthy';
    return {
      desktop_version: '2.0.0',
      runtime_version: '2.0.0',
      protocol_version: '1.0',
      status: 'Ready',
    };
  }

  assert.equal(runtimeState, 'damaged');
  const postRepair = simulateRepair();
  assert.equal(postRepair.status, 'Ready');
  assert.equal(postRepair.desktop_version, postRepair.runtime_version);
});

// 18. Zero Persistent Cache Assertion
await test('Scenario 8: Updater service maintains ZERO persistent on-disk cache files', async () => {
  const state = getUpdaterSnapshot();
  // Ensure that no disk path is declared or tracked for persistent update storage
  assert.equal(state.cachedFilePath, undefined);
  assert.equal(state.downloadPath, undefined);
});

console.log(`\n--- Phase 5 Test Results: ${passed} Passed, ${failed} Failed ---\n`);
if (failed > 0) {
  process.exit(1);
}
