# Orange AI Build Repository

Private repository for building and CI verification of the Orange AI Desktop application on Windows x64.

## Repository Structure

```
orange-ai-build/
├── desktop/                  # Python Runtime, Core Brain, Tools & UI Settings
│   ├── brain/                # 28 Core AI Brain Modules (Protected Baselines)
│   ├── tools/                # Tool Registry and Handlers
│   ├── gui/                  # UI Settings and Theme Engine
│   ├── assets/               # Branding Assets & Icons
│   ├── main.py               # Standalone Engine Reference
│   ├── requirements.txt      # Python Dependencies Specification
│   ├── requirements.lock     # Hash-Pinned Dependency Lock (31 Packages)
│   └── .env.example          # Sanitized Configuration Template
│
├── tauri/                    # Tauri Desktop Application (React + Rust)
│   ├── src/                  # React + Vite Frontend
│   ├── src-tauri/            # Rust Desktop Shell & Runtime Supervisor
│   ├── runtime/              # runtime_adapter.py (JSON-RPC 2.0 Bridge)
│   ├── scripts/              # build_runtime.py & runtime_adapter.spec
│   ├── tests/                # Core Stage 4.5B Regression Test Suites
│   ├── package.json          # Node Dependencies
│   ├── package-lock.json     # Node Lockfile
│   ├── vite.config.js        # Vite Configuration
│   └── index.html            # Web Entrypoint
│
├── .gitignore                # Repository Ignore Rules
├── README.md                 # Project Overview & Build Instructions
└── STAGING_MANIFEST.md       # SHA-256 Staged File Inventory
```

## Build & Verification Instructions

### 1. Python Environment Setup
Requirements: Python 3.14.6 AMD64

```powershell
python -m venv venv
.\venv\Scripts\Activate.ps1
pip install --require-hashes -r desktop/requirements.lock
```

### 2. Frozen Runtime Build
```powershell
cd tauri
python scripts/build_runtime.py
```
This builds `runtime_adapter.exe` and outputs to `tauri/src-tauri/resources/runtime/`.

### 3. Run Regression & Verification Tests
```powershell
cd tauri
python -m unittest tests/test_stage4_5b_phase2_db_safety.py -v
python -m unittest tests/test_stage4_5b_phase3_handshake_recovery.py -v
python -m unittest tests/test_stage4_5b_phase3_failure_recovery_scenarios.py -v
python -m unittest tests/test_stage4_5b_phase3_frozen_runtime_integration.py -v
```

### 4. Packaged Tauri Desktop Build (MSVC Required)
```powershell
cd tauri
npm ci
npm run tauri build
```
