"""Guest clipboard bridge for Windows guests with QEMU guest agent.

QEMU's VNC clipboard is only a host/VNC-client channel; it has no guest-OS
clipboard peer.  For Windows guests v-EMU runs a small PowerShell agent in the
active user session (via WTSQueryUserToken).  Host and guest exchange UTF-8
text through the user's temp directory; clipboard content never appears in a
command line.

Linux guests need a separate user-session helper; this module deliberately
refuses to guess a display/session environment.
"""

from __future__ import annotations

import base64
import hashlib
import re
import time
import uuid
from typing import Any

from .errors import Conflict, VemuError

POWERSHELL = (
    r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"
)
CMD = r"C:\Windows\System32\cmd.exe"
WINDOWS_TEMP = r"C:\Windows\Temp"


def _valid_user(user: str) -> bool:
    """Return True when *user* is safe as one Windows path component.

    QGA reports a plain user name, but some Windows setups prefix it with a
    domain (``DOMAIN\\user``) and Microsoft accounts may contain ``@``. Reject
    path separators and control characters instead of restricting the name to
    ASCII letters.
    """
    if not user or user in {".", ".."} or user != user.strip():
        return False
    if user.endswith((".", " ")):
        return False
    forbidden = set('\\/:*?"<>|')
    return not any(char in forbidden or ord(char) < 32 for char in user)


AGENT_LAUNCHER = r'''param([string]$Script)
$ErrorActionPreference = "Stop"
Add-Type -TypeDefinition @"
using System;
using System.Runtime.InteropServices;
public class WtsStart {
 [DllImport("kernel32.dll")] static extern uint WTSGetActiveConsoleSessionId();
 [DllImport("wtsapi32.dll",SetLastError=true)] static extern bool WTSQueryUserToken(uint sid,out IntPtr tok);
 [DllImport("advapi32.dll",SetLastError=true)] static extern bool DuplicateTokenEx(IntPtr tok,uint access,IntPtr attrs,int imp,int typ,out IntPtr dup);
 [DllImport("advapi32.dll",SetLastError=true,CharSet=CharSet.Unicode)] static extern bool CreateProcessAsUser(IntPtr tok,string app,string cmd,IntPtr pa,IntPtr ta,bool inherit,uint flags,IntPtr env,string cwd,ref STARTUPINFO si,out PROCESS_INFORMATION pi);
 [DllImport("kernel32.dll",SetLastError=true)] static extern bool CloseHandle(IntPtr h);
 [StructLayout(LayoutKind.Sequential,CharSet=CharSet.Unicode)] public struct STARTUPINFO { public int cb; public string lpReserved; public string lpDesktop; public string lpTitle; public int dwX; public int dwY; public int dwXSize; public int dwYSize; public int dwXCountChars; public int dwYCountChars; public int dwFillAttribute; public int dwFlags; public short wShowWindow; public short cbReserved2; public IntPtr lpReserved2; public IntPtr hStdInput; public IntPtr hStdOutput; public IntPtr hStdError; }
 [StructLayout(LayoutKind.Sequential)] public struct PROCESS_INFORMATION { public IntPtr hProcess; public IntPtr hThread; public int dwProcessId; public int dwThreadId; }
 public static string Start(string cmd) {
  uint sid=WTSGetActiveConsoleSessionId(); IntPtr tok;
  if(!WTSQueryUserToken(sid,out tok)) return "wts "+Marshal.GetLastWin32Error();
  IntPtr dup;
  if(!DuplicateTokenEx(tok,0xF01FF,IntPtr.Zero,2,1,out dup)) return "dup "+Marshal.GetLastWin32Error();
  STARTUPINFO si=new STARTUPINFO(); si.cb=Marshal.SizeOf(si); si.lpDesktop="winsta0\\default";
  PROCESS_INFORMATION pi=new PROCESS_INFORMATION();
  bool ok=CreateProcessAsUser(dup,null,cmd,IntPtr.Zero,IntPtr.Zero,false,0x08000000,IntPtr.Zero,"C:\\Windows\\System32",ref si,out pi);
  int e=Marshal.GetLastWin32Error();
  if(!ok) return "create "+e+" sid "+sid;
  CloseHandle(pi.hThread); CloseHandle(pi.hProcess); CloseHandle(dup); CloseHandle(tok);
  return "ok";
 }
}
"@
$b64=[Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($Script))
$exe="$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe"
$cmd='"'+$exe+'" -NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -EncodedCommand '+$b64
[WtsStart]::Start($cmd)
'''


def _os_id(backend: Any, domain: str) -> str:
    payload = backend.qemu_agent_command(
        domain, {"execute": "guest-get-osinfo"}, timeout=10
    )
    if isinstance(payload, dict):
        return str(payload.get("id", ""))
    return ""


def _user_name(
    backend: Any, domain: str, os_name: str = "guest"
) -> str:
    payload = backend.qemu_agent_command(
        domain, {"execute": "guest-get-users"}, timeout=10
    )
    if isinstance(payload, list):
        users = [
            item
            for item in payload
            if isinstance(item, dict) and item.get("user")
        ]
        users.sort(
            key=lambda item: float(item.get("login-time", 0) or 0),
            reverse=True,
        )
        for item in users:
            # QGA normally separates the logon domain, but some Windows
            # versions report "DOMAIN\user" or an Azure AD UPN in this field.
            user = str(item["user"]).rsplit("\\", 1)[-1]
            if _valid_user(user):
                return user
    raise Conflict(
        f"No active {os_name} user session was found for guest clipboard."
    )


def _prefix(domain: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9._-]+", "-", domain).strip("-")[:24] or "vm"
    digest = hashlib.sha1(domain.encode("utf-8")).hexdigest()[:8]
    return f"vemu-clip-{safe}-{digest}"


def _temp_dir(user: str) -> str:
    return rf"C:\Users\{user}\AppData\Local\Temp"


def _file_write(
    backend: Any, domain: str, path: str, data: bytes
) -> None:
    handle = backend.qemu_agent_command(
        domain,
        {
            "execute": "guest-file-open",
            "arguments": {"path": path, "mode": "wb"},
        },
        timeout=15,
    )
    try:
        for offset in range(0, len(data), 64 * 1024):
            chunk = data[offset : offset + 64 * 1024]
            backend.qemu_agent_command(
                domain,
                {
                    "execute": "guest-file-write",
                    "arguments": {
                        "handle": handle,
                        "buf-b64": base64.b64encode(chunk).decode("ascii"),
                        "count": len(chunk),
                    },
                },
                timeout=15,
            )
    finally:
        backend.qemu_agent_command(
            domain,
            {"execute": "guest-file-close", "arguments": {"handle": handle}},
            timeout=10,
        )


def _file_read(backend: Any, domain: str, path: str) -> bytes:
    handle = backend.qemu_agent_command(
        domain,
        {
            "execute": "guest-file-open",
            "arguments": {"path": path, "mode": "rb"},
        },
        timeout=15,
    )
    output = bytearray()
    try:
        while True:
            payload = backend.qemu_agent_command(
                domain,
                {
                    "execute": "guest-file-read",
                    "arguments": {"handle": handle, "count": 65536},
                },
                timeout=15,
            )
            if not isinstance(payload, dict):
                break
            chunk = payload.get("buf-b64") or ""
            if chunk:
                output.extend(base64.b64decode(str(chunk)))
            if not chunk or payload.get("eof"):
                break
    finally:
        backend.qemu_agent_command(
            domain,
            {"execute": "guest-file-close", "arguments": {"handle": handle}},
            timeout=10,
        )
    return bytes(output)


def _exec_start(
    backend: Any,
    domain: str,
    path: str,
    args: list[str],
    *,
    input_data: bytes | None = None,
) -> int:
    arguments: dict[str, Any] = {
        "path": path,
        "arg": args,
        "capture-output": True,
    }
    if input_data is not None:
        arguments["input-data"] = base64.b64encode(input_data).decode("ascii")
    payload = backend.qemu_agent_command(
        domain,
        {
            "execute": "guest-exec",
            "arguments": arguments,
        },
        timeout=15,
    )
    if not isinstance(payload, dict) or "pid" not in payload:
        raise Conflict("Guest agent did not start the clipboard helper.")
    return int(payload["pid"])


def _exec(backend: Any, domain: str, path: str, args: list[str]) -> dict:
    pid = _exec_start(backend, domain, path, args)
    deadline = time.monotonic() + 35
    while time.monotonic() < deadline:
        status = backend.qemu_agent_command(
            domain,
            {
                "execute": "guest-exec-status",
                "arguments": {"pid": pid},
            },
            timeout=10,
        )
        if isinstance(status, dict) and status.get("exited"):
            return status
        time.sleep(0.1)
    raise Conflict("Guest clipboard helper timed out.")


def _delete(backend: Any, domain: str, path: str) -> None:
    try:
        _exec(backend, domain, CMD, ["/c", "del", "/f", "/q", path])
    except Exception:  # noqa: BLE001 - cleanup is best effort
        pass


def _agent_script(user: str, domain: str) -> str:
    directory = _temp_dir(user)
    prefix = _prefix(domain)
    mutex = "Local\\" + prefix
    return rf'''$ErrorActionPreference = "SilentlyContinue"
$dir = "{directory}"
$prefix = "{prefix}"
$created = $false
$mutex = New-Object System.Threading.Mutex($true, "{mutex}", [ref]$created)
if (-not $created) {{ exit 0 }}
$out = Join-Path $dir ($prefix + "-out.txt")
$last = $null
while ($true) {{
  Get-ChildItem -LiteralPath $dir -Filter ($prefix + "-in-*.txt") -ErrorAction SilentlyContinue | ForEach-Object {{
    try {{
      $text = [System.IO.File]::ReadAllText(
        $_.FullName, [System.Text.Encoding]::UTF8)
      Set-Clipboard -Value $text
    }} catch {{}}
    Remove-Item -LiteralPath $_.FullName -Force -ErrorAction SilentlyContinue
  }}
  try {{
    $text = Get-Clipboard -Raw
    if ($null -eq $text) {{ $text = "" }}
    if ($text -ne $last) {{
      [System.IO.File]::WriteAllText(
        $out, [string]$text, (New-Object System.Text.UTF8Encoding($false)))
      $last = $text
    }}
  }} catch {{}}
  Start-Sleep -Milliseconds 250
}}
'''


def ensure_windows_clipboard_agent(backend: Any, domain: str) -> None:
    """Start the user-session clipboard agent if it is not already running."""
    if _os_id(backend, domain) != "mswindows":
        raise Conflict(
            "Guest clipboard is currently supported for Windows guests only."
        )
    user = _user_name(backend, domain, "Windows")
    agent = _agent_script(user, domain)
    launcher = rf"{WINDOWS_TEMP}\vemu-clip-agent-{uuid.uuid4().hex}.ps1"
    try:
        _file_write(backend, domain, launcher, AGENT_LAUNCHER.encode("utf-8"))
        status = _exec(
            backend,
            domain,
            POWERSHELL,
            [
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                launcher,
                "-Script",
                agent,
            ],
        )
        code = int(status.get("exitcode", 1))
        if code != 0:
            raise Conflict(
                f"Guest clipboard agent failed with exit code {code}."
            )
        out_data = status.get("out-data")
        if out_data:
            text = base64.b64decode(str(out_data)).decode(
                "utf-8", errors="replace"
            ).strip()
            if text and text != "ok":
                raise Conflict(f"Guest clipboard agent: {text}")
    finally:
        _delete(backend, domain, launcher)


def _out_path(user: str, domain: str) -> str:
    return rf"{_temp_dir(user)}\{_prefix(domain)}-out.txt"


def get_windows_clipboard(backend: Any, domain: str) -> str:
    """Read the active Windows user's clipboard text from the agent file."""
    if _os_id(backend, domain) != "mswindows":
        raise Conflict(
            "Guest clipboard is currently supported for Windows guests only."
        )
    user = _user_name(backend, domain, "Windows")
    path = _out_path(user, domain)
    try:
        return _file_read(backend, domain, path).decode(
            "utf-8", errors="replace"
        )
    except VemuError:
        # First use or guest reboot: start the agent and retry once.
        ensure_windows_clipboard_agent(backend, domain)
        time.sleep(0.8)
        try:
            return _file_read(backend, domain, path).decode(
                "utf-8", errors="replace"
            )
        except VemuError:
            return ""


def set_windows_clipboard(backend: Any, domain: str, text: str) -> None:
    """Queue host text for the active Windows user's clipboard agent."""
    if _os_id(backend, domain) != "mswindows":
        raise Conflict(
            "Guest clipboard is currently supported for Windows guests only."
        )
    user = _user_name(backend, domain, "Windows")
    path = (
        rf"{_temp_dir(user)}\{_prefix(domain)}-in-"
        rf"{uuid.uuid4().hex}.txt"
    )
    try:
        _file_write(backend, domain, path, text.encode("utf-8"))
    except VemuError:
        ensure_windows_clipboard_agent(backend, domain)
        _file_write(backend, domain, path, text.encode("utf-8"))


def _linux_exec_conflict(exc: VemuError) -> Conflict:
    message = str(exc)
    if "guest-exec" in message and (
        "disabled" in message or "not allowed" in message
    ):
        return Conflict(
            "Linux guest clipboard needs the QEMU guest agent guest-exec "
            "RPC, but the guest blocks it. Allow guest-exec in the guest's "
            "qemu-ga policy and install wl-clipboard, then restart "
            "qemu-guest-agent."
        )
    return Conflict(f"Linux guest clipboard unavailable: {message}")


def _linux_wayland_display(
    backend: Any, domain: str, uid: str
) -> str:
    runtime = f"/run/user/{uid}"
    listing = _exec(
        backend,
        domain,
        "/bin/sh",
        [
            "-c",
            (
                f"find {runtime} -maxdepth 1 -type s "
                "-name 'wayland-*' -printf '%f\\n' 2>/dev/null "
                "| head -n 1"
            ),
        ],
    )
    display_raw = listing.get("out-data") or ""
    return base64.b64decode(str(display_raw)).decode(
        "utf-8", errors="replace"
    ).strip()


def _linux_wayland_session_from_find(
    backend: Any, domain: str
) -> tuple[str, str]:
    """Find the Wayland socket owner when guest-get-users has no utmp data.

    Fedora 45 does not populate utmp for the desktop session, so QGA's
    guest-get-users can be empty even while a Wayland user is logged in.
    """
    status = _exec(
        backend,
        domain,
        "/bin/sh",
        [
            "-c",
            (
                "find /run/user -maxdepth 2 -type s "
                "-name 'wayland-*' -printf '%U %f\\n' 2>/dev/null "
                "| head -n 1"
            ),
        ],
    )
    raw = base64.b64decode(status.get("out-data") or b"").decode(
        "utf-8", errors="replace"
    ).strip()
    uid, _, display = raw.partition(" ")
    return (uid, display) if uid.isdigit() and display else ("", "")


def _linux_clipboard_tools(backend: Any, domain: str) -> None:
    status = _exec(
        backend,
        domain,
        "/bin/sh",
        [
            "-c",
            (
                "command -v wl-paste >/dev/null 2>&1 && "
                "command -v wl-copy >/dev/null 2>&1"
            ),
        ],
    )
    if int(status.get("exitcode", 1)) != 0:
        raise Conflict(
            "Install wl-clipboard in the guest "
            "(for example: sudo dnf install wl-clipboard)."
        )


def _linux_session(backend: Any, domain: str) -> tuple[str, str, str]:
    """Return (uid, XDG_RUNTIME_DIR, WAYLAND_DISPLAY) for the active user."""
    try:
        return _linux_session_inner(backend, domain)
    except Conflict:
        raise
    except VemuError as exc:
        raise _linux_exec_conflict(exc) from exc


def _linux_session_inner(
    backend: Any, domain: str
) -> tuple[str, str, str]:
    uid = ""
    display = ""
    try:
        user = _user_name(backend, domain, "Linux")
    except VemuError:
        # guest-get-users can be blocked or empty; the socket scan below is
        # authoritative for a running Wayland session.
        user = ""
    if user:
        uid_status = _exec(
            backend, domain, "/bin/sh", ["-c", f"id -u {user}"]
        )
        uid_raw = uid_status.get("out-data") or ""
        uid = base64.b64decode(str(uid_raw)).decode(
            "utf-8", errors="replace"
        ).strip()
        if uid.isdigit():
            display = _linux_wayland_display(backend, domain, uid)
    if not uid.isdigit() or not display:
        uid, display = _linux_wayland_session_from_find(backend, domain)
    if not uid.isdigit() or not display:
        raise Conflict(
            "Linux guest clipboard requires an active Wayland session."
        )
    _linux_clipboard_tools(backend, domain)
    return uid, f"/run/user/{uid}", display


def _linux_env(runtime: str, display: str, command: str) -> str:
    return (
        f"XDG_RUNTIME_DIR={runtime} WAYLAND_DISPLAY={display} "
        f"{command}"
    )


def get_linux_clipboard(backend: Any, domain: str) -> str:
    """Read the Wayland clipboard from the active Linux user session."""
    _uid, runtime, display = _linux_session(backend, domain)
    status = _exec(
        backend,
        domain,
        "/bin/sh",
        ["-c", _linux_env(runtime, display, "wl-paste -n")],
    )
    code = int(status.get("exitcode", 1))
    if code != 0:
        err = status.get("err-data") or ""
        message = base64.b64decode(str(err)).decode(
            "utf-8", errors="replace"
        ).strip()
        # wl-paste exits non-zero when the clipboard is empty.
        if "Nothing is copied" in message or not message:
            return ""
        raise Conflict(f"Wayland clipboard read failed: {message}")
    out = status.get("out-data") or ""
    return base64.b64decode(str(out)).decode("utf-8", errors="replace")


def set_linux_clipboard(backend: Any, domain: str, text: str) -> None:
    """Set the Wayland clipboard from the active Linux user session."""
    _uid, runtime, display = _linux_session(backend, domain)
    # Pass the text through guest-exec stdin instead of a temp file. wl-copy
    # forks and keeps owning the selection, so do not wait for it to exit.
    _exec_start(
        backend,
        domain,
        "/bin/sh",
        ["-c", _linux_env(runtime, display, "wl-copy")],
        input_data=text.encode("utf-8"),
    )


def ensure_guest_clipboard_agent(backend: Any, domain: str) -> None:
    """Start the Windows agent; Linux Wayland needs no persistent agent."""
    os_id = _os_id(backend, domain)
    if os_id == "mswindows":
        ensure_windows_clipboard_agent(backend, domain)
        return
    if not os_id:
        raise Conflict("Guest agent did not report an operating system.")
    # Probe the session now so failures surface on console open, not on the
    # first copy/paste.  Linux has no persistent helper to start.
    _linux_session(backend, domain)


def get_guest_clipboard(backend: Any, domain: str) -> str:
    os_id = _os_id(backend, domain)
    if os_id == "mswindows":
        return get_windows_clipboard(backend, domain)
    if not os_id:
        raise Conflict("Guest agent did not report an operating system.")
    return get_linux_clipboard(backend, domain)


def set_guest_clipboard(backend: Any, domain: str, text: str) -> None:
    os_id = _os_id(backend, domain)
    if os_id == "mswindows":
        set_windows_clipboard(backend, domain, text)
        return
    if not os_id:
        raise Conflict("Guest agent did not report an operating system.")
    set_linux_clipboard(backend, domain, text)
