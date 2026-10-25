#!/usr/bin/env python3
"""Safely add one supported ingress rule to an existing cloudflared config."""
import argparse
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile


class ConfigError(ValueError):
    pass


def fail(message):
    raise ConfigError(message)


def configure(text, tunnel, hostname, port):
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", tunnel):
        fail("invalid tunnel identifier")
    if not re.fullmatch(r"(?=.{1,253}\Z)(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z]{2,63}", hostname):
        fail("hostname must be a fully qualified DNS name")
    if not 1 <= port <= 65535:
        fail("port must be between 1 and 65535")

    lines = text.splitlines(keepends=True)
    top_level = []
    for i, line in enumerate(lines):
        if line.strip() and not line[0].isspace() and not line.lstrip().startswith("#"):
            top_level.append((i, line.rstrip("\r\n")))
    tunnel_lines = [(i, value) for i, value in top_level if re.match(r"^tunnel\s*:", value)]
    if len(tunnel_lines) != 1:
        fail("config must contain exactly one top-level tunnel setting")
    match = re.fullmatch(r"tunnel\s*:\s*([A-Za-z0-9_-]{1,128})\s*(?:#.*)?", tunnel_lines[0][1])
    if not match or match.group(1) != tunnel:
        fail("config tunnel must be a plain existing tunnel identifier matching --tunnel")
    ingress_sections = [(i, value) for i, value in top_level if re.fullmatch(r"ingress\s*:\s*(?:#.*)?", value)]
    if len(ingress_sections) != 1:
        fail("config must contain exactly one top-level ingress section")

    section_line = ingress_sections[0][0]
    end = next((i for i, _ in top_level if i > section_line), len(lines))
    section = lines[section_line + 1:end]
    rules = []
    current = None
    for offset, line in enumerate(section):
        stripped = line.rstrip("\r\n")
        if not stripped.strip() or stripped.lstrip().startswith("#"):
            continue
        if stripped.startswith("  - "):
            if current is not None:
                rules.append(current)
            current = {"start": section_line + 1 + offset, "fields": {}}
            field = stripped[4:]
            indent = 4
        elif stripped.startswith("    ") and current is not None:
            field = stripped[4:]
            indent = 4
        else:
            fail("unsupported ingress YAML layout; expected two-space rules and four-space fields")
        if indent != 4 or ":" not in field:
            fail("unsupported ingress YAML field")
        key, value = field.split(":", 1)
        key, value = key.strip(), value.strip()
        if key not in ("hostname", "service") or key in current["fields"] or not value or value.startswith(("[", "{", "&", "*", "|", ">")):
            fail("unsupported, duplicate, or complex ingress YAML field")
        if value.startswith(("'", '"')):
            fail("quoted ingress values are unsupported; no changes made")
        if " #" in value:
            value = value.split(" #", 1)[0].rstrip()
        current["fields"][key] = value
    if current is not None:
        rules.append(current)
    if not rules:
        fail("ingress section has no supported rules")

    normalized = []
    for rule in rules:
        fields = rule["fields"]
        if set(fields) == {"hostname", "service"}:
            normalized.append((fields["hostname"], fields["service"], rule["start"]))
        elif set(fields) == {"service"} and fields["service"] == "http_status:404":
            normalized.append((None, fields["service"], rule["start"]))
        else:
            fail("each ingress rule must be hostname+service or the http_status:404 fallback")
    fallbacks = [rule for rule in normalized if rule[0] is None]
    if len(fallbacks) != 1 or normalized[-1] != fallbacks[0]:
        fail("ingress must end in exactly one http_status:404 fallback")

    requested = hostname.lower()
    existing = [(host, service) for host, service, _ in normalized if host and host.lower() == requested]
    if any(host and host.startswith("*.") and requested.endswith(host[1:].lower()) for host, _, _ in normalized):
        fail("an existing wildcard ingress rule may shadow the requested hostname")
    service = f"http://127.0.0.1:{port}"
    if existing:
        if len(existing) == 1 and existing[0][1] == service:
            return text, False
        fail("hostname already has a conflicting ingress rule")
    if any(host is None for host, _, _ in normalized[:-1]):
        fail("hostless ingress rules can shadow the requested hostname")

    insert_at = fallbacks[0][2]
    newline = "\r\n" if "\r\n" in text else "\n"
    rule = f"  - hostname: {hostname}{newline}    service: {service}{newline}"
    return "".join(lines[:insert_at]) + rule + "".join(lines[insert_at:]), True


def read_regular_nofollow(path):
    if stat.S_ISLNK(path.lstat().st_mode):
        fail("config must be an existing regular file, not a symlink")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            fail("config must be an existing regular file, not a symlink")
        with os.fdopen(fd, "r", encoding="utf-8", newline="") as stream:
            fd = -1
            return stream.read(), info
    finally:
        if fd >= 0:
            os.close(fd)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tunnel", required=True, help="existing tunnel UUID or name")
    parser.add_argument("--hostname", required=True)
    parser.add_argument("--port", required=True, type=int)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--cloudflared", default="cloudflared")
    parser.add_argument("--apply", action="store_true", help="write config after validation (otherwise review only)")
    parser.add_argument("--create-dns", action="store_true", help="explicitly request optional Cloudflare DNS route")
    parser.add_argument("--confirm-dns", action="store_true", help="confirm DNS routing (required with --create-dns)")
    args = parser.parse_args()
    if args.create_dns != args.confirm_dns:
        fail("DNS action requires both --create-dns and --confirm-dns")

    path = args.config.expanduser().absolute()
    original, info = read_regular_nofollow(path)
    candidate, changed = configure(original, args.tunnel, args.hostname, args.port)
    with tempfile.TemporaryDirectory(prefix="cloudflared-check-") as td:
        candidate_path = Path(td) / "config.yml"
        candidate_path.write_text(candidate, encoding="utf-8")
        result = subprocess.run([args.cloudflared, "tunnel", "ingress", "validate", "--config", str(candidate_path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
        if result.returncode:
            fail("cloudflared ingress validation failed; no changes made")
        if not args.apply:
            print("Validated review only; no files or DNS changed. Re-run with --apply to write.")
            return
        if changed:
            current = path.lstat()
            if stat.S_ISLNK(current.st_mode) or (current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns) != (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns):
                fail("config changed after it was read; no changes made")
            backup = path.with_name(path.name + ".bak")
            fd = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as out:
                out.write(original.encode("utf-8"))
                out.flush()
                os.fsync(out.fileno())
            tmp = None
            try:
                fd, tmpname = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
                tmp = Path(tmpname)
                os.fchmod(fd, stat.S_IMODE(info.st_mode))
                with os.fdopen(fd, "w", encoding="utf-8", newline="") as out:
                    out.write(candidate)
                    out.flush()
                    os.fsync(out.fileno())
                os.replace(tmp, path)
            except Exception:
                if tmp and tmp.exists():
                    tmp.unlink()
                raise
            print(f"Config updated; restrictive backup created at {backup}.")
        else:
            print("Config already contains the requested rule; no config change needed.")
    if args.create_dns:
        result = subprocess.run([args.cloudflared, "tunnel", "route", "dns", args.tunnel, args.hostname], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
        if result.returncode:
            fail("config is applied locally, but DNS route command failed; inspect config and DNS manually; the config was not rolled back")
        print("DNS route command succeeded. Verify DNS in Cloudflare; no tunnel or app was started.")


if __name__ == "__main__":
    try:
        main()
    except (ConfigError, OSError, subprocess.SubprocessError, UnicodeError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(2)
