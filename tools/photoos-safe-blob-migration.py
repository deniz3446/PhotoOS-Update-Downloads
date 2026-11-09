#!/usr/bin/env python3
"""PhotoOS: safely materialize backup blobs after a verified account transfer.

Copies, never moves/deletes, blobs from the previous user directory into the
new owner directory. Database records are read-only. Run as root on the server.
"""
import argparse
import csv
import hashlib
import io
import os
from pathlib import Path
import pwd
import shutil
import subprocess
import sys
import tempfile
import uuid

def die(message):
    raise SystemExit("STOPPED (no files deleted): " + message)

def query(sql):
    result = subprocess.run(
        ["runuser", "-u", "postgres", "--", "psql", "-X", "-v", "ON_ERROR_STOP=1",
         "-d", "photoos", "-A", "-F", "\t", "-t", "-c", sql],
        capture_output=True, text=True, check=True,
    )
    return list(csv.reader(io.StringIO(result.stdout), delimiter="\t"))

def digest(path):
    h = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(4*1024*1024), b""):
            h.update(chunk)
    return h.hexdigest()

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--device", type=uuid.UUID, required=True)
    parser.add_argument("--old-owner", type=uuid.UUID, required=True)
    parser.add_argument("--new-owner", type=uuid.UUID, required=True)
    parser.add_argument("--expected", type=int, required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    if os.geteuid() != 0: die("Run with sudo")
    if args.old_owner == args.new_owner: die("Old/new owners identical")
    if not 1 <= args.expected <= 1000000: die("Expected count invalid")
    backup = Path("/var/backups/photoos/before-melek-owner-fix.dump")
    if not backup.is_file() or backup.is_symlink() or backup.stat().st_size < 100_000:
        die("Verified database backup not found")
    if subprocess.run(["pg_restore", "-l", str(backup)], stdout=subprocess.DEVNULL).returncode:
        die("Database backup is not readable")
    device = str(args.device)
    old = str(args.old_owner)
    new = str(args.new_owner)
    owners = query("SELECT id::text,username,role FROM users WHERE id IN ('" + old + "','" + new + "')")
    if len(owners) != 2: die("Both user records must exist")
    device_rows = query(
        "SELECT d.user_id::text,j.id::text,j.user_id::text, "
        "(SELECT COUNT(*) FROM backup_file_versions v WHERE v.job_id=j.id)::text, "
        "(SELECT COUNT(*) FROM media m WHERE m.backup_job_id=j.id AND m.user_id=d.user_id)::text "
        "FROM devices d JOIN backup_jobs j ON j.device_id=d.id "
        "WHERE d.id='" + device + "'")
    if len(device_rows) != 1: die("Expected exactly one backup job for target device")
    row = device_rows[0]
    if row[0] != new or row[2] != new or int(row[3]) != args.expected or int(row[4]) != args.expected:
        die("Owner, backup job, or media record count mismatch")
    job = row[1]
    active = query("SELECT COUNT(*) FROM backup_runs WHERE job_id='" + job + "' AND status='running'")
    if active != [["0"]]: die("Backup currently running")
    blobs = query(
        "SELECT DISTINCT b.blake3,b.size_bytes::text,b.stored_relpath "
        "FROM backup_file_versions v JOIN backup_blobs b ON b.id=v.blob_id "
        "WHERE v.job_id='" + job + "' AND b.user_id='" + new + "' ORDER BY b.blake3,b.size_bytes,b.stored_relpath")
    if len(blobs) != args.expected: die("Blob count mismatch")
    root = Path("/data/users")
    src_root = root / old
    dst_root = root / new
    if not src_root.is_dir() or src_root.is_symlink():
        die("Previous owner storage directory is missing")
    if not root.is_dir() or root.is_symlink(): die("Storage root is unsafe")
    planned = []
    total = 0
    for hash_, size_, stored in blobs:
        if len(hash_) != 64 or any(ch not in "0123456789abcdef" for ch in hash_):
            die("Invalid BLAKE3 hash")
        expected_path = "backups/blobs/" + hash_[:2] + "/" + hash_
        if stored != expected_path: die("Unexpected stored path for " + hash_[:12])
        size = int(size_)
        source = src_root / stored
        target = dst_root / stored
        if not source.is_file() or source.is_symlink() or source.stat().st_size != size:
            die("Previous owner file missing or size mismatch: " + hash_[:12])
        if not source.resolve().is_relative_to(src_root.resolve()):
            die("Previous owner path escapes its root")
        if target.exists() or target.is_symlink():
            if target.is_symlink() or not target.is_file() or target.stat().st_size != size:
                die("Conflicting target file: " + hash_[:12])
            if digest(source) != digest(target):
                die("Target differs from original: " + hash_[:12])
        else:
            total += size
        planned.append((source, target, size))
    print("VERIFIED:",len(planned),"blobs; extra bytes:",total)
    print("No source file will be removed; database is read-only.")
    if not args.apply:
        print("DRY RUN only: pass --apply after review")
        return
    if shutil.disk_usage(root).free < total + 512*1024*1024:
        die("Not enough free storage for safe copy")
    account = pwd.getpwnam("photoos")
    dst_root.mkdir(mode=0o750, exist_ok=True)
    if dst_root.is_symlink(): die("Destination root is a symlink")
    os.chown(dst_root, account.pw_uid, account.pw_gid)
    copied = 0
    for index, (source, target, size) in enumerate(planned, 1):
        if target.exists(): continue
        target.parent.mkdir(mode=0o750, parents=True, exist_ok=True)
        for ancestor in (dst_root / "backups", dst_root / "backups/blobs", target.parent):
            if ancestor.is_symlink(): die("Destination contains a symlink")
            os.chown(ancestor, account.pw_uid, account.pw_gid)
            os.chmod(ancestor, 0o750)
        fd, filename = tempfile.mkstemp(prefix=".migrate-", dir=target.parent)
        temporary = Path(filename)
        try:
            os.fchmod(fd, 0o640)
            os.fchown(fd, account.pw_uid, account.pw_gid)
            with os.fdopen(fd,"wb") as out, source.open("rb") as inp:
                h = hashlib.sha256()
                while True:
                    chunk = inp.read(4*1024*1024)
                    if not chunk: break
                    out.write(chunk);h.update(chunk)
                out.flush();os.fsync(out.fileno())
            if temporary.stat().st_size != size or digest(temporary) != h.hexdigest():
                die("Copy verification failed for " + source.name[:12])
            if target.exists(): die("Target appeared during transfer")
            temporary.replace(target)
            copied += 1
        finally:
            temporary.unlink(missing_ok=True)
        if index % 50 == 0: print("Verified",index,"/",len(planned))
    print("SUCCESS: all",len(planned),"blob references now have valid owner paths; newly copied",copied)
    print("Source blobs remain untouched. Test media playback before any cleanup.")

if __name__=="__main__":
    main()
