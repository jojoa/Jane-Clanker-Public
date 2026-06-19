from __future__ import annotations

import argparse
import getpass
import io
import json
import os
import platform
import re
import secrets
import shutil
import shlex
import stat
import subprocess
import sys
import textwrap
import tarfile
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path
from typing import Iterable


DEFAULT_ENV_FILE = "localOnly/credentials/jane-runtime-secrets.env"
DEFAULT_TUNNEL_NAME = "jane-identity"
DEFAULT_TUNNEL_PROVIDER = "tailscale"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8791
DEFAULT_TAILSCALE_HTTPS_PORT = 443

ENV_KEY_RE = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=")


def repoRoot() -> Path:
    return Path(__file__).resolve().parents[1]


def resolveRepoPath(root: Path, rawPath: str | Path) -> Path:
    path = Path(str(rawPath)).expanduser()
    if path.is_absolute():
        return path
    return (root / path).resolve()


def parseArgs(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Prepare Jane Identity runtime env values and a tunnel runner for "
            "Jane's Roblox OAuth callback."
        )
    )
    parser.add_argument(
        "--domain",
        default="",
        help=(
            "Public Jane Identity hostname or HTTPS base URL, such as identity.example.com. "
            "Optional for Tailscale; the helper can read the host's Tailscale DNS name."
        ),
    )
    parser.add_argument("--tunnel-name", default=DEFAULT_TUNNEL_NAME)
    parser.add_argument(
        "--tunnel-provider",
        choices=("tailscale", "ngrok", "cloudflare", "none"),
        default=DEFAULT_TUNNEL_PROVIDER,
        help="Which public tunnel runner to prepare.",
    )
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--env-file", default=DEFAULT_ENV_FILE)
    parser.add_argument("--tailscale-path", default="")
    parser.add_argument(
        "--tailscale-https-port",
        type=int,
        default=DEFAULT_TAILSCALE_HTTPS_PORT,
        help="HTTPS port exposed by Tailscale Funnel.",
    )
    parser.add_argument("--ngrok-path", default="")
    parser.add_argument(
        "--no-download-ngrok",
        action="store_true",
        help="Do not download ngrok if it is not already installed.",
    )
    parser.add_argument(
        "--ngrok-authtoken",
        default=os.getenv("NGROK_AUTHTOKEN", ""),
        help="Optional ngrok agent authtoken. Also read from NGROK_AUTHTOKEN.",
    )
    parser.add_argument("--cloudflared-path", default="")
    parser.add_argument(
        "--no-download-cloudflared",
        action="store_true",
        help="Do not download cloudflared if it is not already installed.",
    )
    parser.add_argument(
        "--skip-cloudflare",
        action="store_true",
        help="Legacy alias for --tunnel-provider none.",
    )
    parser.add_argument(
        "--run-cloudflare",
        action="store_true",
        help="Run cloudflared login/create/route commands. Without this, commands are printed only.",
    )
    parser.add_argument(
        "--install-service",
        action="store_true",
        help="Install cloudflared as a service when --cloudflare-tunnel-token is supplied.",
    )
    parser.add_argument(
        "--cloudflare-tunnel-token",
        default=os.getenv("CLOUDFLARE_TUNNEL_TOKEN", ""),
        help="Optional Cloudflare dashboard tunnel token. Also read from CLOUDFLARE_TUNNEL_TOKEN.",
    )
    parser.add_argument("--roblox-client-id", default="")
    parser.add_argument("--roblox-client-secret", default="")
    parser.add_argument(
        "--prompt-roblox",
        action="store_true",
        help="Prompt for missing Roblox OAuth client credentials.",
    )
    parser.add_argument(
        "--force-api-token",
        action="store_true",
        help="Regenerate JANE_IDENTITY_API_TOKEN even if one already exists.",
    )
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args(argv)


def normalizePublicBaseUrl(rawDomain: str) -> tuple[str, str, str]:
    clean = str(rawDomain or "").strip().rstrip("/")
    if not clean:
        raise ValueError("--domain is required.")
    if "://" not in clean:
        clean = f"https://{clean}"
    parsed = urllib.parse.urlparse(clean)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError(f"Invalid public URL: {rawDomain!r}")
    if parsed.scheme != "https":
        print("Warning: Roblox OAuth production callbacks should use HTTPS.")
    hostname = parsed.hostname or ""
    if not hostname:
        raise ValueError(f"Invalid hostname in public URL: {rawDomain!r}")
    baseUrl = f"{parsed.scheme}://{parsed.netloc}"
    redirectUri = f"{baseUrl}/identity/roblox/callback"
    return baseUrl, hostname, redirectUri


def parseEnvValue(rawValue: str) -> str:
    value = str(rawValue or "").strip()
    if not value:
        return ""
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return value.strip("'\"")
    return str(parsed or "")


def readEnv(path: Path) -> tuple[list[str], dict[str, str], dict[str, int]]:
    if not path.exists():
        return [], {}, {}

    lines = path.read_text(encoding="utf-8").splitlines()
    values: dict[str, str] = {}
    indexes: dict[str, int] = {}
    for index, line in enumerate(lines):
        match = ENV_KEY_RE.match(line)
        if not match:
            continue
        key = match.group(1)
        _, rawValue = line.split("=", 1)
        values[key] = parseEnvValue(rawValue)
        indexes[key] = index
    return lines, values, indexes


def formatEnvLine(key: str, value: str) -> str:
    return f"{key}={json.dumps(str(value or ''), ensure_ascii=True)}"


def writeEnvUpdates(
    path: Path,
    updates: dict[str, str],
    *,
    dryRun: bool = False,
) -> tuple[dict[str, str], list[str]]:
    lines, values, indexes = readEnv(path)
    changed: list[str] = []
    if not lines:
        lines = ["# Jane runtime secrets. This file is local-only and must not be committed."]

    missingKeys = [key for key in updates if key not in indexes]
    if missingKeys:
        if lines and lines[-1].strip():
            lines.append("")
        lines.append("# Jane Identity")

    for key, value in updates.items():
        value = str(value or "")
        nextLine = formatEnvLine(key, value)
        oldValue = values.get(key)
        if key in indexes:
            if lines[indexes[key]] != nextLine:
                lines[indexes[key]] = nextLine
                changed.append(key)
        else:
            lines.append(nextLine)
            changed.append(key)
        values[key] = value

    if not dryRun:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    return values, changed


def promptIfMissing(label: str, current: str, *, secret: bool) -> str:
    if current:
        return current
    if secret:
        return getpass.getpass(f"{label}: ").strip()
    return input(f"{label}: ").strip()


def ngrokAsset() -> tuple[str, str, str] | None:
    system = platform.system().lower()
    machine = platform.machine().lower()
    isArm64 = machine in {"arm64", "aarch64"}

    if system == "windows":
        arch = "arm64" if isArm64 else "amd64"
        return (
            "ngrok.exe",
            f"https://bin.equinox.io/c/bNyj1mQVY4c/ngrok-v3-stable-windows-{arch}.zip",
            "zip",
        )
    if system == "linux":
        arch = "arm64" if isArm64 else "amd64"
        return (
            "ngrok",
            f"https://bin.equinox.io/c/bNyj1mQVY4c/ngrok-v3-stable-linux-{arch}.tgz",
            "tgz",
        )
    return None


def _extractNgrokBinary(archiveBytes: bytes, archiveKind: str, binaryName: str) -> bytes:
    if archiveKind == "zip":
        with zipfile.ZipFile(io.BytesIO(archiveBytes)) as archive:
            for member in archive.namelist():
                if Path(member).name.lower() == binaryName.lower():
                    return archive.read(member)
    if archiveKind == "tgz":
        with tarfile.open(fileobj=io.BytesIO(archiveBytes), mode="r:gz") as archive:
            for member in archive.getmembers():
                if Path(member.name).name == binaryName and member.isfile():
                    extracted = archive.extractfile(member)
                    if extracted is not None:
                        return extracted.read()
    raise RuntimeError("Downloaded ngrok archive did not contain the expected binary.")


def downloadNgrok(root: Path, *, dryRun: bool) -> Path | None:
    asset = ngrokAsset()
    if asset is None:
        print("ngrok auto-download is only supported for Windows and Linux.")
        return None

    binaryName, url, archiveKind = asset
    target = root / "localOnly" / "bin" / binaryName
    if target.exists():
        return target
    if dryRun:
        print(f"Would download ngrok to {target}")
        return target
    print(f"Downloading ngrok to {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen(url, timeout=60) as response:
        binaryBytes = _extractNgrokBinary(response.read(), archiveKind, binaryName)
    target.write_bytes(binaryBytes)
    if os.name != "nt":
        target.chmod(target.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return target


def findNgrok(args: argparse.Namespace, root: Path) -> Path | None:
    if args.ngrok_path:
        return Path(args.ngrok_path).expanduser().resolve()

    localNames = ["ngrok.exe", "ngrok"]
    for name in localNames:
        local = root / "localOnly" / "bin" / name
        if local.exists():
            return local

    found = shutil.which("ngrok")
    if found:
        return Path(found).resolve()
    if args.no_download_ngrok:
        return None
    return downloadNgrok(root, dryRun=bool(args.dry_run))


def writeNgrokRunner(
    root: Path,
    ngrok: Path | None,
    baseUrl: str,
    host: str,
    port: int,
    *,
    dryRun: bool = False,
) -> Path:
    runnerPath = root / "localOnly" / "ngrok" / "run-jane-identity-tunnel.ps1"
    binary = str(ngrok or "ngrok")
    upstream = f"http://{host}:{port}"
    content = (
        "$ErrorActionPreference = 'Stop'\n"
        f"& {json.dumps(binary)} http {json.dumps(upstream)} --url {json.dumps(baseUrl)}\n"
    )
    if not dryRun:
        runnerPath.parent.mkdir(parents=True, exist_ok=True)
        runnerPath.write_text(content, encoding="utf-8")
    return runnerPath


def prepareNgrok(
    args: argparse.Namespace,
    root: Path,
    baseUrl: str,
) -> tuple[Path | None, Path]:
    ngrok = findNgrok(args, root)
    if ngrok is None:
        print("ngrok was not found. Install ngrok or put ngrok.exe in localOnly/bin.")

    binary = ngrok or Path("ngrok")
    token = str(args.ngrok_authtoken or "").strip()
    if token:
        if args.dry_run:
            print(f"$ {formatCommand([binary, 'config', 'add-authtoken', token], redacted={3})}")
        else:
            runCommand([binary, "config", "add-authtoken", token], redact={3})
    else:
        print("ngrok authtoken was not supplied. Run `ngrok config add-authtoken ...` once on the Jane host.")

    upstream = f"http://{args.host}:{args.port}"
    print("\nngrok command to run on the Jane host:")
    print(formatCommand([binary, "http", upstream, "--url", baseUrl]))

    runnerPath = writeNgrokRunner(
        root,
        ngrok,
        baseUrl,
        args.host,
        args.port,
        dryRun=bool(args.dry_run),
    )
    return ngrok, runnerPath


def findTailscale(args: argparse.Namespace, root: Path) -> Path | None:
    tailscalePath = str(getattr(args, "tailscale_path", "") or "").strip()
    if tailscalePath:
        return Path(tailscalePath).expanduser().resolve()

    localNames = ["tailscale.exe", "tailscale"]
    for name in localNames:
        local = root / "localOnly" / "bin" / name
        if local.exists():
            return local

    found = shutil.which("tailscale")
    if found:
        return Path(found).resolve()
    return None


def detectTailscaleDnsName(args: argparse.Namespace, root: Path) -> str:
    tailscale = findTailscale(args, root)
    if tailscale is None:
        return ""
    try:
        result = subprocess.run(
            [str(tailscale), "status", "--json"],
            text=True,
            capture_output=True,
            timeout=10,
        )
    except Exception:
        return ""
    if result.returncode != 0:
        return ""
    try:
        data = json.loads(result.stdout or "{}")
    except json.JSONDecodeError:
        return ""
    selfData = data.get("Self")
    if not isinstance(selfData, dict):
        return ""
    return str(selfData.get("DNSName") or "").strip().rstrip(".")


def resolvePublicDomain(args: argparse.Namespace, root: Path, provider: str) -> str:
    rawDomain = str(getattr(args, "domain", "") or "").strip()
    if rawDomain:
        return rawDomain
    if provider != "tailscale":
        raise ValueError("--domain is required unless --tunnel-provider tailscale can auto-detect it.")
    dnsName = detectTailscaleDnsName(args, root)
    if not dnsName:
        raise ValueError(
            "Could not auto-detect the Tailscale public host. Make sure Tailscale is installed and logged in "
            "on the Jane host, or pass --domain https://your-jane-host.your-tailnet.ts.net."
        )
    baseUrl = f"https://{dnsName}"
    print(f"Detected Tailscale public host: {baseUrl}")
    return baseUrl


def normalizeTailscaleHttpsPort(rawPort: object) -> int:
    port = int(rawPort or DEFAULT_TAILSCALE_HTTPS_PORT)
    if port not in {443, 8443, 10000}:
        raise ValueError("Tailscale Funnel HTTPS port must be 443, 8443, or 10000.")
    return port


def tailscaleProxyTarget(_host: str, port: int) -> str:
    # Tailscale Funnel's HTTP proxy mode is intended for local loopback services.
    return f"http://127.0.0.1:{int(port)}"


def writeTailscaleRunner(
    root: Path,
    tailscale: Path | None,
    host: str,
    port: int,
    httpsPort: int,
    *,
    dryRun: bool = False,
) -> Path:
    runnerPath = root / "localOnly" / "tailscale" / "run-jane-identity-funnel.ps1"
    binary = str(tailscale or "tailscale")
    upstream = tailscaleProxyTarget(host, port)
    content = (
        "$ErrorActionPreference = 'Stop'\n"
        f"& {json.dumps(binary)} funnel --bg --yes --https={int(httpsPort)} {json.dumps(upstream)}\n"
        f"& {json.dumps(binary)} funnel status\n"
    )
    if not dryRun:
        runnerPath.parent.mkdir(parents=True, exist_ok=True)
        runnerPath.write_text(content, encoding="utf-8")
    return runnerPath


def prepareTailscale(
    args: argparse.Namespace,
    root: Path,
) -> tuple[Path | None, Path]:
    tailscale = findTailscale(args, root)
    if tailscale is None:
        print("tailscale was not found. Install and log into Tailscale on the Jane host first.")

    binary = tailscale or Path("tailscale")
    httpsPort = normalizeTailscaleHttpsPort(args.tailscale_https_port)
    upstream = tailscaleProxyTarget(args.host, args.port)
    print("\nTailscale Funnel command to run on the Jane host:")
    print(formatCommand([binary, "funnel", "--bg", "--yes", f"--https={httpsPort}", upstream]))
    print(formatCommand([binary, "funnel", "status"]))

    runnerPath = writeTailscaleRunner(
        root,
        tailscale,
        args.host,
        args.port,
        httpsPort,
        dryRun=bool(args.dry_run),
    )
    return tailscale, runnerPath


def cloudflaredAsset() -> tuple[str, str] | None:
    system = platform.system().lower()
    machine = platform.machine().lower()
    isArm64 = machine in {"arm64", "aarch64"}

    if system == "windows":
        arch = "arm64" if isArm64 else "amd64"
        return (
            f"cloudflared-windows-{arch}.exe",
            f"https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-{arch}.exe",
        )
    if system == "linux":
        arch = "arm64" if isArm64 else "amd64"
        return (
            "cloudflared",
            f"https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-{arch}",
        )
    return None


def downloadCloudflared(root: Path, *, dryRun: bool) -> Path | None:
    asset = cloudflaredAsset()
    if asset is None:
        print("cloudflared auto-download is only supported for Windows and Linux.")
        return None

    filename, url = asset
    target = root / "localOnly" / "bin" / filename
    if target.exists():
        return target
    if dryRun:
        print(f"Would download cloudflared to {target}")
        return target
    print(f"Downloading cloudflared to {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen(url, timeout=60) as response:
        target.write_bytes(response.read())
    if os.name != "nt":
        target.chmod(target.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return target


def findCloudflared(args: argparse.Namespace, root: Path) -> Path | None:
    if args.cloudflared_path:
        return Path(args.cloudflared_path).expanduser().resolve()

    localNames = ["cloudflared.exe", "cloudflared-windows-amd64.exe", "cloudflared-windows-arm64.exe", "cloudflared"]
    for name in localNames:
        local = root / "localOnly" / "bin" / name
        if local.exists():
            return local

    found = shutil.which("cloudflared")
    if found:
        return Path(found).resolve()

    if args.no_download_cloudflared:
        return None
    return downloadCloudflared(root, dryRun=bool(args.dry_run))


def yamlQuote(value: str | Path) -> str:
    return json.dumps(str(value), ensure_ascii=True)


def writeCloudflaredConfig(
    root: Path,
    tunnelName: str,
    hostname: str,
    host: str,
    port: int,
    *,
    tunnelId: str = "",
    dryRun: bool = False,
) -> Path:
    configPath = root / "localOnly" / "cloudflared" / "jane-identity.yml"
    tunnelValue = tunnelId or tunnelName
    credentialsPath = Path.home() / ".cloudflared" / f"{tunnelId}.json" if tunnelId else None

    lines = [
        "# Generated by tools/setup_jane_identity_tunnel.py",
        f"tunnel: {yamlQuote(tunnelValue)}",
    ]
    if credentialsPath and credentialsPath.exists():
        lines.append(f"credentials-file: {yamlQuote(credentialsPath)}")
    lines.extend(
        [
            "ingress:",
            f"  - hostname: {yamlQuote(hostname)}",
            f"    service: {yamlQuote(f'http://{host}:{port}')}",
            f"  - service: {yamlQuote('http_status:404')}",
        ]
    )
    if not dryRun:
        configPath.parent.mkdir(parents=True, exist_ok=True)
        configPath.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return configPath


def writeTunnelRunner(
    root: Path,
    cloudflared: Path | None,
    configPath: Path,
    *,
    dryRun: bool = False,
) -> Path:
    runnerPath = root / "localOnly" / "cloudflared" / "run-jane-identity-tunnel.ps1"
    binary = str(cloudflared or "cloudflared")
    content = (
        "$ErrorActionPreference = 'Stop'\n"
        f"& {json.dumps(binary)} tunnel --config {json.dumps(str(configPath))} run\n"
    )
    if not dryRun:
        runnerPath.parent.mkdir(parents=True, exist_ok=True)
        runnerPath.write_text(content, encoding="utf-8")
    return runnerPath


def formatCommand(parts: Iterable[str | Path], *, redacted: set[int] | None = None) -> str:
    values = [str(part) for part in parts]
    if redacted:
        for index in redacted:
            if 0 <= index < len(values):
                values[index] = "<redacted>"
    if os.name == "nt":
        return subprocess.list2cmdline(values)
    return " ".join(shlex.quote(value) for value in values)


def runCommand(parts: list[str | Path], *, redact: set[int] | None = None) -> subprocess.CompletedProcess[str]:
    print(f"$ {formatCommand(parts, redacted=redact)}")
    return subprocess.run([str(part) for part in parts], text=True)


def runCommandCapture(parts: list[str | Path]) -> subprocess.CompletedProcess[str]:
    return subprocess.run([str(part) for part in parts], text=True, capture_output=True)


def listTunnelId(cloudflared: Path, tunnelName: str) -> str:
    result = runCommandCapture([cloudflared, "tunnel", "list", "--output", "json"])
    if result.returncode != 0:
        return ""
    try:
        tunnels = json.loads(result.stdout or "[]")
    except json.JSONDecodeError:
        return ""
    if not isinstance(tunnels, list):
        return ""
    for tunnel in tunnels:
        if not isinstance(tunnel, dict):
            continue
        name = str(tunnel.get("name") or tunnel.get("Name") or "")
        tunnelId = str(tunnel.get("id") or tunnel.get("ID") or tunnel.get("uuid") or "")
        if name == tunnelName and tunnelId:
            return tunnelId
    return ""


def prepareCloudflare(
    args: argparse.Namespace,
    root: Path,
    hostname: str,
) -> tuple[Path | None, Path | None, Path | None]:
    if args.skip_cloudflare:
        return None, None, None

    cloudflared = findCloudflared(args, root)
    if cloudflared is None:
        print("cloudflared was not found. Install it or rerun without --no-download-cloudflared.")
        configPath = writeCloudflaredConfig(
            root,
            args.tunnel_name,
            hostname,
            args.host,
            args.port,
            dryRun=bool(args.dry_run),
        )
        return None, configPath, None

    tunnelId = ""
    if args.dry_run:
        print("\nCloudflare commands to run on the Jane host:")
        print(formatCommand([cloudflared, "tunnel", "login"]))
        print(formatCommand([cloudflared, "tunnel", "create", args.tunnel_name]))
        print(formatCommand([cloudflared, "tunnel", "route", "dns", args.tunnel_name, hostname]))
    elif args.run_cloudflare:
        runCommand([cloudflared, "tunnel", "login"])
        tunnelId = listTunnelId(cloudflared, args.tunnel_name)
        if not tunnelId:
            runCommand([cloudflared, "tunnel", "create", args.tunnel_name])
            tunnelId = listTunnelId(cloudflared, args.tunnel_name)
        runCommand([cloudflared, "tunnel", "route", "dns", args.tunnel_name, hostname])
    else:
        print("\nCloudflare commands to run on the Jane host:")
        print(formatCommand([cloudflared, "tunnel", "login"]))
        print(formatCommand([cloudflared, "tunnel", "create", args.tunnel_name]))
        print(formatCommand([cloudflared, "tunnel", "route", "dns", args.tunnel_name, hostname]))
        tunnelId = listTunnelId(cloudflared, args.tunnel_name)

    configPath = writeCloudflaredConfig(
        root,
        args.tunnel_name,
        hostname,
        args.host,
        args.port,
        tunnelId=tunnelId,
        dryRun=bool(args.dry_run),
    )
    runnerPath = writeTunnelRunner(root, cloudflared, configPath, dryRun=bool(args.dry_run))

    if args.install_service:
        token = str(args.cloudflare_tunnel_token or "").strip()
        if token:
            runCommand([cloudflared, "service", "install", token], redact={3})
        else:
            print("Skipping service install: --cloudflare-tunnel-token was not supplied.")

    return cloudflared, configPath, runnerPath


def writeJohnEnv(root: Path, baseUrl: str, apiToken: str, *, dryRun: bool = False) -> Path:
    path = root / "localOnly" / "identity" / "john-identity.env"
    content = textwrap.dedent(
        f"""\
        # Copy these values into John's runtime environment when John is moved to Jane Identity.
        JANE_IDENTITY_API_BASE_URL={json.dumps(baseUrl, ensure_ascii=True)}
        JANE_IDENTITY_API_TOKEN={json.dumps(apiToken, ensure_ascii=True)}
        """
    )
    if not dryRun:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    return path


def main(argv: list[str] | None = None) -> int:
    args = parseArgs(list(argv if argv is not None else sys.argv[1:]))
    root = repoRoot()
    provider = "none" if args.skip_cloudflare else args.tunnel_provider
    publicDomain = resolvePublicDomain(args, root, provider)
    baseUrl, hostname, redirectUri = normalizePublicBaseUrl(publicDomain)
    envPath = resolveRepoPath(root, args.env_file)
    _, existingEnv, _ = readEnv(envPath)

    robloxClientId = args.roblox_client_id or existingEnv.get("ROBLOX_OAUTH_CLIENT_ID", "")
    robloxClientSecret = args.roblox_client_secret or existingEnv.get("ROBLOX_OAUTH_CLIENT_SECRET", "")
    if args.prompt_roblox:
        robloxClientId = promptIfMissing("ROBLOX_OAUTH_CLIENT_ID", robloxClientId, secret=False)
        robloxClientSecret = promptIfMissing("ROBLOX_OAUTH_CLIENT_SECRET", robloxClientSecret, secret=True)

    existingToken = existingEnv.get("JANE_IDENTITY_API_TOKEN", "")
    apiToken = secrets.token_urlsafe(48) if args.force_api_token or not existingToken else existingToken

    updates = {
        "JANE_IDENTITY_WEB_ENABLED": "true",
        "JANE_IDENTITY_WEB_HOST": args.host,
        "JANE_IDENTITY_WEB_PORT": str(args.port),
        "JANE_IDENTITY_PUBLIC_BASE_URL": baseUrl,
        "JANE_IDENTITY_REDIRECT_URI": redirectUri,
        "JANE_IDENTITY_API_TOKEN": apiToken,
    }
    if robloxClientId:
        updates["ROBLOX_OAUTH_CLIENT_ID"] = robloxClientId
    if robloxClientSecret:
        updates["ROBLOX_OAUTH_CLIENT_SECRET"] = robloxClientSecret

    _, changedKeys = writeEnvUpdates(envPath, updates, dryRun=bool(args.dry_run))
    johnEnvPath = writeJohnEnv(root, baseUrl, apiToken, dryRun=bool(args.dry_run))
    tailscale: Path | None = None
    ngrok: Path | None = None
    cloudflared: Path | None = None
    configPath: Path | None = None
    runnerPath: Path | None = None
    if provider == "tailscale":
        tailscale, runnerPath = prepareTailscale(args, root)
    elif provider == "ngrok":
        ngrok, runnerPath = prepareNgrok(args, root, baseUrl)
    elif provider == "cloudflare":
        cloudflared, configPath, runnerPath = prepareCloudflare(args, root, hostname)

    print("\nJane Identity setup summary:")
    print(f"- Env file: {envPath}")
    print(f"- Updated keys: {', '.join(changedKeys) if changedKeys else 'none'}")
    print(f"- Public base URL: {baseUrl}")
    print(f"- Roblox OAuth redirect URI: {redirectUri}")
    print(f"- John env handoff: {johnEnvPath}")
    if not robloxClientId or not robloxClientSecret:
        print("- Roblox OAuth credentials still need to be added to the env file.")
    if configPath:
        print(f"- Cloudflare config: {configPath}")
    if runnerPath and provider == "tailscale":
        print(f"- Tailscale runner: {runnerPath}")
    elif runnerPath and provider == "ngrok":
        print(f"- ngrok runner: {runnerPath}")
    elif runnerPath:
        print(f"- Tunnel runner: {runnerPath}")
    if tailscale:
        print(f"- tailscale: {tailscale}")
    if ngrok:
        print(f"- ngrok: {ngrok}")
    if cloudflared:
        print(f"- cloudflared: {cloudflared}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
