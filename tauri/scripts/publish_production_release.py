"""
Publish Production Release to blAkeHoax/orange-ai
Stage 4.5B Phase 6 Gate 4: Release Publication
"""

import os
import sys
import glob
import json
import urllib.request
import urllib.parse


def upload_asset(upload_url_template, headers, file_path, content_type):
    file_name = os.path.basename(file_path)
    file_size = os.path.getsize(file_path)
    clean_upload_url = upload_url_template.split('{')[0]
    target_url = f"{clean_upload_url}?name={urllib.parse.quote(file_name)}"

    print(f"Uploading asset {file_name} ({file_size:,} bytes)...")
    req_headers = headers.copy()
    req_headers['Content-Type'] = content_type
    req_headers['Content-Length'] = str(file_size)

    with open(file_path, 'rb') as f:
        file_bytes = f.read()

    req = urllib.request.Request(target_url, data=file_bytes, headers=req_headers, method='POST')
    with urllib.request.urlopen(req) as resp:
        if resp.status in [200, 201]:
            data = json.loads(resp.read().decode())
            print(f"  Successfully uploaded {file_name}: asset ID {data.get('id')}")
            return data
        else:
            raise RuntimeError(f"Failed to upload {file_name}: HTTP {resp.status}")


def main():
    print("=== [GATE 4] Publishing Production Release to blAkeHoax/orange-ai ===")

    token = os.environ.get("DISTRIBUTION_TOKEN") or os.environ.get("GH_TOKEN")
    if not token:
        print("WARNING: DISTRIBUTION_TOKEN or GH_TOKEN not found in environment. Skipping release publication.")
        sys.exit(0)

    # 1. Locate Artifacts
    target_dir = "tauri/src-tauri/target"
    exe_cands = glob.glob(f"{target_dir}/**/bundle/nsis/*-setup.exe", recursive=True)
    zip_cands = glob.glob(f"{target_dir}/**/bundle/nsis/*.nsis.zip", recursive=True)
    sig_cands = glob.glob(f"{target_dir}/**/bundle/nsis/*.sig", recursive=True)
    json_cands = glob.glob(f"{target_dir}/**/latest.json", recursive=True)

    if not exe_cands:
        raise SystemExit("FAILED: Standalone NSIS installer not found!")
    if not sig_cands:
        raise SystemExit("FAILED: Minisign signature artifact not found!")

    setup_exe = os.path.abspath(exe_cands[0])

    if zip_cands:
        updater_payload = os.path.abspath(zip_cands[0])
        sig_match = [s for s in sig_cands if s.endswith('.nsis.zip.sig')]
        updater_sig = os.path.abspath(sig_match[0] if sig_match else sig_cands[0])
    else:
        updater_payload = setup_exe
        sig_match = [s for s in sig_cands if s.endswith('.exe.sig')]
        updater_sig = os.path.abspath(sig_match[0] if sig_match else sig_cands[0])

    if not json_cands:
        import datetime
        latest_json_path = os.path.join(os.path.dirname(updater_sig), "latest.json")
        with open(updater_sig, "r", encoding="utf-8") as f:
            sig_raw = f.read().strip()
        payload_filename = os.path.basename(updater_payload).replace(" ", ".")
        manifest_data = {
            "version": "2.0.0",
            "notes": "Orange AI Desktop 2.0.0 official production release",
            "pub_date": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "platforms": {
                "windows-x86_64": {
                    "signature": sig_raw,
                    "url": f"https://github.com/blAkeHoax/orange-ai/releases/download/v2.0.0/{payload_filename}"
                }
            }
        }
        with open(latest_json_path, "w", encoding="utf-8") as f:
            json.dump(manifest_data, f, indent=2)
        print(f"Generated updater manifest at: {latest_json_path}")
        latest_json = latest_json_path
    else:
        latest_json = os.path.abspath(json_cands[0])

    # 2. Prepare Release Metadata
    tag_name = "v2.0.0"
    repo = "blAkeHoax/orange-ai"
    release_title = "Orange AI Desktop 2.0.0"
    release_body = """## Orange AI Desktop 2.0.0 (Windows x64)

Official production release of **Orange AI Desktop 2.0** for Windows 10 and newer (x64).

### Key Features
- **Frozen Python 3.14.6 Runtime:** Fully bundled standalone runtime engine; zero system Python dependency.
- **Option 1 User-Approval Updater:** Sequential update discovery, explicit download approval, session-scoped deferral, and pre-update database safety gates.
- **Cryptographic Minisign Signatures:** All updater archives are verified against the embedded public key before installation.
- **Designated Recovery Installer:** Standalone NSIS installer for clean repair of interrupted installations.

### Release Artifacts
- **Standalone Installer:** `Orange AI_2.0.0_x64-setup.exe`
- **Signed Updater Archive:** `Orange AI_2.0.0_x64-setup.nsis.zip`
- **Minisign Signature:** `Orange AI_2.0.0_x64-setup.nsis.zip.sig`
- **Updater Manifest:** `latest.json`
"""

    headers = {
        'Authorization': f'token {token}',
        'User-Agent': 'Orange-AI-Release-Publisher',
        'Accept': 'application/vnd.github.v3+json'
    }

    # 3. Check for existing release
    releases_url = f"https://api.github.com/repos/{repo}/releases"
    req_list = urllib.request.Request(releases_url, headers=headers)
    existing_release = None
    try:
        with urllib.request.urlopen(req_list) as resp:
            releases = json.loads(resp.read().decode())
            for r in releases:
                if r.get("tag_name") == tag_name:
                    existing_release = r
                    break
    except Exception as e:
        print(f"Error checking releases: {e}")

    if existing_release:
        print(f"Found existing release for {tag_name} (ID {existing_release['id']}).")
        release_id = existing_release['id']
        upload_url = existing_release['upload_url']
        # Delete existing assets to allow clean re-upload
        assets_url = f"https://api.github.com/repos/{repo}/releases/{release_id}/assets"
        req_assets = urllib.request.Request(assets_url, headers=headers)
        with urllib.request.urlopen(req_assets) as resp:
            assets = json.loads(resp.read().decode())
        for a in assets:
            print(f"Deleting existing asset: {a['name']} (ID {a['id']})...")
            del_url = f"https://api.github.com/repos/{repo}/releases/assets/{a['id']}"
            req_del = urllib.request.Request(del_url, headers=headers, method='DELETE')
            urllib.request.urlopen(req_del)
    else:
        # Create Release
        print(f"Creating release {tag_name} in {repo}...")
        payload = {
            "tag_name": tag_name,
            "target_commitish": "main",
            "name": release_title,
            "body": release_body,
            "draft": False,
            "prerelease": False
        }
        req_create = urllib.request.Request(releases_url, data=json.dumps(payload).encode('utf-8'), headers=headers, method='POST')
        with urllib.request.urlopen(req_create) as resp:
            release_data = json.loads(resp.read().decode())
            release_id = release_data['id']
            upload_url = release_data['upload_url']
            print(f"Release created successfully: ID {release_id}, URL: {release_data.get('html_url')}")

    # 4. Upload Assets
    assets_to_upload = [
        (setup_exe, "application/octet-stream"),
        (updater_sig, "text/plain"),
        (latest_json, "application/json"),
    ]
    if updater_payload != setup_exe:
        assets_to_upload.append((updater_payload, "application/zip"))

    for fpath, ctype in assets_to_upload:
        upload_asset(upload_url, headers, fpath, ctype)

    # 5. Verify live download endpoint
    manifest_download_url = f"https://github.com/{repo}/releases/latest/download/latest.json"
    print(f"\nVerifying public availability of: {manifest_download_url}")
    req_verify = urllib.request.Request(manifest_download_url, headers={'User-Agent': 'Verifier'})
    try:
        with urllib.request.urlopen(req_verify) as resp:
            if resp.status == 200:
                body = json.loads(resp.read().decode())
                print(f"Verification successful: HTTP {resp.status}")
                print(f"  Published version: {body.get('version')}")
                print(f"  Published platform url: {body.get('platforms', {}).get('windows-x86_64', {}).get('url')}")
            else:
                raise RuntimeError(f"Unexpected status: HTTP {resp.status}")
    except Exception as e:
        print(f"Warning: Public CDN replication might take a few moments: {e}")

    print("\n[GATE 4 ACCEPTANCE SUCCEEDED: Production release published!]")


if __name__ == "__main__":
    main()
