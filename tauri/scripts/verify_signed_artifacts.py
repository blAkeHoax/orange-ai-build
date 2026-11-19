"""
Verify Signed Updater Artifacts and Minisign Signatures
Validates the build outputs of `cargo tauri build` for Stage 4.5B Phase 6.
"""

import os
import sys
import glob
import json
import base64
import hashlib
import datetime

try:
    from nacl.signing import VerifyKey
    HAVE_NACL = True
except ImportError:
    HAVE_NACL = False

try:
    from cryptography.hazmat.primitives.asymmetric import ed25519
    HAVE_CRYPTO = True
except ImportError:
    HAVE_CRYPTO = False


def verify_ed25519(pub_key_32: bytes, sig_64: bytes, message_digest: bytes) -> bool:
    if HAVE_NACL:
        vk = VerifyKey(pub_key_32)
        vk.verify(message_digest, sig_64)
        return True
    elif HAVE_CRYPTO:
        pk = ed25519.Ed25519PublicKey.from_public_bytes(pub_key_32)
        pk.verify(sig_64, message_digest)
        return True
    else:
        raise RuntimeError("Neither PyNaCl nor cryptography is available for Ed25519 signature verification!")


def main():
    print("=== [GATE 3] Verifying Signed Production Build Artifacts ===")

    # 1. Read Public Key and Version from tauri.conf.json
    conf_path = "tauri/src-tauri/tauri.conf.json"
    with open(conf_path, "r", encoding="utf-8") as f:
        conf = json.load(f)

    app_version = conf.get("version", "2.0.0")
    pubkey_raw = conf.get("plugins", {}).get("updater", {}).get("pubkey", "")
    if not pubkey_raw:
        raise SystemExit("FAILED: tauri.conf.json contains an empty pubkey placeholder!")

    # 2. Locate Artifacts
    target_dir = "tauri/src-tauri/target"
    if not os.path.exists(target_dir):
        raise SystemExit(f"Target directory {target_dir} does not exist!")

    exe_cands = glob.glob(f"{target_dir}/**/bundle/nsis/*-setup.exe", recursive=True)
    zip_cands = glob.glob(f"{target_dir}/**/bundle/nsis/*.nsis.zip", recursive=True)
    sig_cands = glob.glob(f"{target_dir}/**/bundle/nsis/*.sig", recursive=True)
    json_cands = glob.glob(f"{target_dir}/**/latest.json", recursive=True)

    print(f"Found setup.exe:   {exe_cands}")
    print(f"Found .nsis.zip:   {zip_cands}")
    print(f"Found .sig:        {sig_cands}")
    print(f"Found latest.json: {json_cands}")

    if not exe_cands:
        raise SystemExit("FAILED: Standalone NSIS installer (*-setup.exe) not found!")
    if not sig_cands:
        raise SystemExit("FAILED: Minisign signature file (*.sig) not found! Ensure TAURI_SIGNING_PRIVATE_KEY was set.")

    setup_exe = os.path.abspath(exe_cands[0])

    if zip_cands:
        updater_payload = os.path.abspath(zip_cands[0])
        sig_match = [s for s in sig_cands if s.endswith('.nsis.zip.sig')]
        updater_sig = os.path.abspath(sig_match[0] if sig_match else sig_cands[0])
    else:
        updater_payload = setup_exe
        sig_match = [s for s in sig_cands if s.endswith('.exe.sig')]
        updater_sig = os.path.abspath(sig_match[0] if sig_match else sig_cands[0])

    with open(updater_sig, "r", encoding="utf-8") as f:
        sig_raw = f.read().strip()

    if not json_cands:
        # Generate updater manifest latest.json alongside the updater payload
        latest_json_path = os.path.join(os.path.dirname(updater_sig), "latest.json")
        payload_filename = os.path.basename(updater_payload).replace(" ", ".")
        manifest_data = {
            "version": app_version,
            "notes": f"Orange AI Desktop {app_version} official production release",
            "pub_date": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "platforms": {
                "windows-x86_64": {
                    "signature": sig_raw,
                    "url": f"https://github.com/blAkeHoax/orange-ai/releases/download/v{app_version}/{payload_filename}"
                }
            }
        }
        with open(latest_json_path, "w", encoding="utf-8") as f:
            json.dump(manifest_data, f, indent=2)
        print(f"Generated updater manifest at: {latest_json_path}")
        latest_json = latest_json_path
    else:
        latest_json = os.path.abspath(json_cands[0])

    print(f"\n[ARTIFACT INVENTORY]")
    print(f"  Installer:  {setup_exe} ({os.path.getsize(setup_exe):,} bytes)")
    print(f"  Payload:    {updater_payload} ({os.path.getsize(updater_payload):,} bytes)")
    print(f"  Signature:  {updater_sig} ({os.path.getsize(updater_sig):,} bytes)")
    print(f"  Manifest:   {latest_json} ({os.path.getsize(latest_json):,} bytes)")

    # 3. SHA-256 Checksums
    for name, path in [("Installer", setup_exe), ("Payload", updater_payload), ("Manifest", latest_json)]:
        with open(path, "rb") as f:
            h = hashlib.sha256(f.read()).hexdigest()
        print(f"  SHA-256 ({name}): {h}")

    print(f"\nConfigured Public Key: {pubkey_raw}")

    # Decode Minisign public key
    pub_lines = base64.b64decode(pubkey_raw).decode("utf-8").splitlines()
    pub_bytes = base64.b64decode(pub_lines[1].strip())
    algo = pub_bytes[:2]
    key_id = pub_bytes[2:10]
    ed25519_pub = pub_bytes[10:42]

    print(f"  Minisign Algorithm: {algo}")
    print(f"  Key ID:             {key_id.hex()}")

    # 4. Verify Minisign Signature over payload
    sig_lines = base64.b64decode(sig_raw).decode("utf-8").splitlines()
    sig_bytes = base64.b64decode(sig_lines[1].strip())
    sig_algo = sig_bytes[:2]
    sig_key_id = sig_bytes[2:10]
    raw_sig = sig_bytes[10:74]

    if key_id != sig_key_id:
        raise SystemExit(f"FAILED: Signature key ID {sig_key_id.hex()} does not match configured public key ID {key_id.hex()}!")

    with open(updater_payload, "rb") as f:
        payload_data = f.read()

    # Minisign prehashed with BLAKE2b-512
    h = hashlib.blake2b(payload_data).digest()

    try:
        verify_ed25519(ed25519_pub, raw_sig, h)
        print("\n[CRYPTOGRAPHIC VERIFICATION SUCCESSFUL]")
        print("  The updater payload signature is authentic and verified by the configured public key!")
    except Exception as e:
        raise SystemExit(f"FAILED: Cryptographic signature verification failed: {e}")

    # 5. Verify latest.json Manifest
    with open(latest_json, "r", encoding="utf-8") as f:
        manifest = json.load(f)

    print(f"\n[MANIFEST VERIFICATION]")
    print(f"  Version:   {manifest.get('version')}")
    print(f"  Pub Date:  {manifest.get('pub_date')}")
    print(f"  Platforms: {list(manifest.get('platforms', {}).keys())}")

    if manifest.get("version") != app_version:
        raise SystemExit(f"FAILED: Manifest version {manifest.get('version')} does not match tauri.conf.json {app_version}!")

    win_platform = manifest.get("platforms", {}).get("windows-x86_64")
    if not win_platform:
        raise SystemExit("FAILED: latest.json does not contain 'windows-x86_64' platform!")

    sig_in_manifest = win_platform.get("signature", "").strip()
    if sig_in_manifest != sig_raw:
        raise SystemExit("FAILED: Signature in latest.json does not match .nsis.zip.sig content!")

    print(f"  Signature in manifest matches .sig file exactly.")
    print(f"  Download URL: {win_platform.get('url')}")

    print("\n[GATE 3 ACCEPTANCE SUCCEEDED: All signed artifacts verified!]")


if __name__ == "__main__":
    main()
