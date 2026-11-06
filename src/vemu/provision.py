"""Provisioning helpers for new VMs (ISO install and Windows unattend)."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from xml.sax.saxutils import escape

from .errors import ExternalCommandError, NotFound, ValidationError
from .tools import find as find_tool
from .tools import which as find_which

# Image indexes in the en-US Windows Server 2025 ISO:
# 1 Standard (Core), 2 Standard (Desktop Experience),
# 3 Datacenter (Core), 4 Datacenter (Desktop Experience).
WINDOWS_EDITIONS: dict[str, tuple[int, str]] = {
    "standard-core": (1, "Windows Server 2025 Standard"),
    "standard": (2, "Windows Server 2025 Standard (Desktop Experience)"),
    "datacenter-core": (3, "Windows Server 2025 Datacenter"),
    "datacenter": (4, "Windows Server 2025 Datacenter (Desktop Experience)"),
}
DEFAULT_WINDOWS_EDITION = "standard"
DEFAULT_ADMIN_PASSWORD = "Vemu!Lab2026"
WINDOWS_PRODUCT_KEYS = {
    "standard-core": "TVRH6-WHNXV-R9WG3-9XRFY-MY832",
    "standard": "TVRH6-WHNXV-R9WG3-9XRFY-MY832",
    "datacenter-core": "D764K-2NDRG-47T6Q-P8T8W-YP6DF",
    "datacenter": "D764K-2NDRG-47T6Q-P8T8W-YP6DF",
}

# Optional performance tuning for lab guests. Set to False to keep the
# Windows default balanced power plan.
HIGH_PERFORMANCE_POWER_PLAN = True
HIGH_PERFORMANCE_POWER_PLAN_GUID = "8c5e7fda-e8bf-4a96-9a85-a6e23a8c635c"


def _high_performance_power_plan_command() -> str:
    """Return a PowerShell command that selects the high performance plan."""
    if not HIGH_PERFORMANCE_POWER_PLAN:
        return ""
    script = (
        f"try {{ $g = '{HIGH_PERFORMANCE_POWER_PLAN_GUID}'; "
        "powercfg /q $g *> $null; "
        "if ($?) { powercfg /setactive $g } } catch { }"
    )
    return (
        'powershell -NoProfile -ExecutionPolicy Bypass -Command "'
        + escape(script)
        + '"'
    )

OS_FAMILIES = {
    "windows": "Windows",
    "linux": "Linux",
    "hypervisor": "Hypervisor",
    "other": "Other / BSD / unknown",
}
OS_VARIANTS: dict[str, list[tuple[str, str]]] = {
    "windows": [
        ("windows-server-2025", "Windows Server 2025 (unattended)"),
        ("windows-server-other", "Windows Server 2022/2019/2016 (manual)"),
        ("windows-11", "Windows 11 (manual install)"),
        ("windows-10", "Windows 10 (manual install)"),
        ("windows-client", "Other Windows client (manual)"),
    ],
    "linux": [
        ("fedora", "Fedora"),
        ("rhel", "RHEL"),
        ("rocky", "Rocky Linux"),
        ("alma", "AlmaLinux"),
        ("centos", "CentOS"),
        ("ubuntu", "Ubuntu"),
        ("debian", "Debian"),
        ("kali", "Kali Linux"),
        ("opensuse", "openSUSE"),
        ("arch", "Arch Linux"),
        ("alpine", "Alpine Linux"),
        ("linux-other", "Other Linux"),
    ],
    "hypervisor": [
        ("esxi-8-9", "VMware ESXi 8/9"),
    ],
    "other": [
        ("appliance", "Network appliance / firewall (serial console)"),
        ("freebsd", "FreeBSD"),
        ("openbsd", "OpenBSD"),
        ("netbsd", "NetBSD"),
        ("other", "Other / unknown OS"),
    ],
}


def detect_iso_os(path: str) -> dict[str, str | bool]:
    """Best-effort OS family/variant detection from an ISO filename."""
    name = Path(path).name.lower()
    if "vmvisor" in name or "esxi" in name:
        return {
            "family": "hypervisor",
            "variant": "esxi-8-9",
            "label": "VMware ESXi",
            "unattend": False,
        }
    if any(
        token in name
        for token in (
            "windows",
            "win32",
            "win64",
            "win10",
            "win11",
            "win-10",
            "win-11",
            "win_10",
            "win_11",
        )
    ):
        if "server" in name and ("2025" in name or "2026" in name):
            return {
                "family": "windows",
                "variant": "windows-server-2025",
                "label": "Windows Server 2025",
                "unattend": True,
            }
        if "server" in name:
            return {
                "family": "windows",
                "variant": "windows-server-other",
                "label": "Windows Server",
                "unattend": False,
            }
        if "win11" in name:
            return {
                "family": "windows",
                "variant": "windows-11",
                "label": "Windows 11",
                "unattend": False,
            }
        if "win10" in name:
            return {
                "family": "windows",
                "variant": "windows-10",
                "label": "Windows 10",
                "unattend": False,
            }
        return {
            "family": "windows",
            "variant": "windows-client",
            "label": "Windows client",
            "unattend": False,
        }
    for pattern, variant, label in (
        ("fedora", "fedora", "Fedora"),
        ("rhel", "rhel", "RHEL"),
        ("redhat", "rhel", "RHEL"),
        ("rocky", "rocky", "Rocky Linux"),
        ("alma", "alma", "AlmaLinux"),
        ("centos", "centos", "CentOS"),
        ("ubuntu", "ubuntu", "Ubuntu"),
        ("debian", "debian", "Debian"),
        ("kali", "kali", "Kali Linux"),
        ("opensuse", "opensuse", "openSUSE"),
        ("suse", "opensuse", "openSUSE"),
        ("archlinux", "arch", "Arch Linux"),
        ("alpine", "alpine", "Alpine Linux"),
    ):
        if pattern in name:
            return {
                "family": "linux",
                "variant": variant,
                "label": label,
                "unattend": False,
            }
    for pattern, variant, label in (
        ("freebsd", "freebsd", "FreeBSD"),
        ("openbsd", "openbsd", "OpenBSD"),
        ("netbsd", "netbsd", "NetBSD"),
    ):
        if pattern in name:
            return {
                "family": "other",
                "variant": variant,
                "label": label,
                "unattend": False,
            }
    return {
        "family": "other",
        "variant": "other",
        "label": "Unknown ISO",
        "unattend": False,
    }


def edition_info(edition: str) -> tuple[int, str]:
    try:
        return WINDOWS_EDITIONS[edition]
    except KeyError as exc:
        choices = ", ".join(sorted(WINDOWS_EDITIONS))
        raise ValidationError(
            f"Unknown Windows edition '{edition}'. Choose one of: {choices}."
        ) from exc


def windows_computer_name(name: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9-]+", "-", name).strip("-")[:15]
    return (cleaned or "VEMU").upper()


def render_windows_unattend(
    *,
    computer_name: str,
    admin_password: str = DEFAULT_ADMIN_PASSWORD,
    edition: str = DEFAULT_WINDOWS_EDITION,
    virtio_iso: bool = False,
    enable_rdp: bool = True,
    timezone: str = "UTC",
    firmware: str = "bios",
) -> str:
    if firmware not in {"bios", "efi"}:
        raise ValidationError("Windows firmware must be bios or efi.")
    if len(admin_password) < 8:
        raise ValidationError(
            "Windows administrator password must be at least 8 characters."
        )
    index, display = edition_info(edition)
    product_key = WINDOWS_PRODUCT_KEYS[edition]
    computer = escape(windows_computer_name(computer_name))
    password = escape(admin_password)

    if firmware == "efi":
        disk_configuration = """
        <Disk wcm:action="add">
          <DiskID>0</DiskID>
          <WillWipeDisk>true</WillWipeDisk>
          <CreatePartitions>
            <CreatePartition wcm:action="add">
              <Order>1</Order><Type>EFI</Type><Size>512</Size>
            </CreatePartition>
            <CreatePartition wcm:action="add">
              <Order>2</Order><Type>MSR</Type><Size>128</Size>
            </CreatePartition>
            <CreatePartition wcm:action="add">
              <Order>3</Order><Type>Primary</Type><Extend>true</Extend>
            </CreatePartition>
          </CreatePartitions>
          <ModifyPartitions>
            <ModifyPartition wcm:action="add">
              <Order>1</Order><PartitionID>1</PartitionID><Format>FAT32</Format>
            </ModifyPartition>
            <ModifyPartition wcm:action="add">
              <Order>2</Order><PartitionID>2</PartitionID>
            </ModifyPartition>
            <ModifyPartition wcm:action="add">
              <Order>3</Order><PartitionID>3</PartitionID><Format>NTFS</Format>
              <Label>Windows</Label><Letter>C</Letter>
            </ModifyPartition>
          </ModifyPartitions>
        </Disk>"""
        install_partition = 3
    else:
        disk_configuration = """
        <Disk wcm:action="add">
          <DiskID>0</DiskID>
          <WillWipeDisk>true</WillWipeDisk>
          <CreatePartitions>
            <CreatePartition wcm:action="add">
              <Order>1</Order>
              <Type>Primary</Type>
              <Extend>true</Extend>
            </CreatePartition>
          </CreatePartitions>
          <ModifyPartitions>
            <ModifyPartition wcm:action="add">
              <Order>1</Order>
              <PartitionID>1</PartitionID>
              <Format>NTFS</Format>
              <Label>Windows</Label>
              <Letter>C</Letter>
              <Active>true</Active>
            </ModifyPartition>
          </ModifyPartitions>
        </Disk>"""
        install_partition = 1

    qemu_agent_command = ""
    if virtio_iso:
        qemu_agent_command = """
        <RunSynchronousCommand wcm:action="add">
          <Order>1</Order>
          <Description>Install virtio serial driver</Description>
          <Path>cmd /c for %D in (D E F G H I J K) do if exist %D:\\vioserial\\2k25\\amd64\\vioser.inf pnputil /add-driver %D:\\vioserial\\2k25\\amd64\\vioser.inf /install</Path>
        </RunSynchronousCommand>
        <RunSynchronousCommand wcm:action="add">
          <Order>2</Order>
          <Description>Install QEMU guest agent</Description>
          <Path>cmd /c for %D in (D E F G H I J K) do if exist %D:\\guest-agent\\qemu-ga-x86_64.msi msiexec /i %D:\\guest-agent\\qemu-ga-x86_64.msi /qn /norestart /l*v C:\\qemu-ga-install.log</Path>
        </RunSynchronousCommand>"""

    power_plan_first_logon = ""
    power_plan = _high_performance_power_plan_command()
    if power_plan:
        power_plan_first_logon = f"""
        <SynchronousCommand wcm:action="add">
          <Order>5</Order>
          <Description>Use the high performance power plan</Description>
          <CommandLine>{power_plan}</CommandLine>
        </SynchronousCommand>"""

    first_logon_commands = ""
    if virtio_iso or power_plan_first_logon:
        virtio_first_logon = ""
        if virtio_iso:
            virtio_first_logon = """
        <SynchronousCommand wcm:action="add">
          <Order>1</Order>
          <Description>Retry virtio serial driver</Description>
          <CommandLine>cmd /c for %D in (D E F G H I J K) do if exist %D:\\vioserial\\2k25\\amd64\\vioser.inf pnputil /add-driver %D:\\vioserial\\2k25\\amd64\\vioser.inf /install</CommandLine>
        </SynchronousCommand>
        <SynchronousCommand wcm:action="add">
          <Order>2</Order>
          <Description>Retry QEMU guest agent</Description>
          <CommandLine>cmd /c for %D in (D E F G H I J K) do if exist %D:\\guest-agent\\qemu-ga-x86_64.msi msiexec /i %D:\\guest-agent\\qemu-ga-x86_64.msi /qn /norestart /l*v C:\\qemu-ga-install.log</CommandLine>
        </SynchronousCommand>
        <SynchronousCommand wcm:action="add">
          <Order>3</Order>
          <Description>Start QEMU guest agent</Description>
          <CommandLine>cmd /c sc start qemu-ga</CommandLine>
        </SynchronousCommand>
        <SynchronousCommand wcm:action="add">
          <Order>4</Order>
          <Description>Disable console blanking</Description>
          <CommandLine>cmd /c powercfg /change monitor-timeout-ac 0 &amp; powercfg /change monitor-timeout-dc 0 &amp; powercfg /change standby-timeout-ac 0 &amp; powercfg /change standby-timeout-dc 0</CommandLine>
        </SynchronousCommand>"""
        first_logon_commands = f"""
      <FirstLogonCommands>{virtio_first_logon}{power_plan_first_logon}
      </FirstLogonCommands>"""

    rdp_commands = ""
    if enable_rdp:
        rdp_commands = """
        <RunSynchronousCommand wcm:action="add">
          <Order>4</Order>
          <Path>cmd /c reg add "HKLM\\SYSTEM\\CurrentControlSet\\Control\\Terminal Server" /v fDenyTSConnections /t REG_DWORD /d 0 /f</Path>
        </RunSynchronousCommand>
        <RunSynchronousCommand wcm:action="add">
          <Order>5</Order>
          <Path>cmd /c netsh advfirewall firewall set rule group="remote desktop" new enable=Yes</Path>
        </RunSynchronousCommand>"""

    return f"""<?xml version="1.0" encoding="utf-8"?>
<unattend xmlns="urn:schemas-microsoft-com:unattend">
  <settings pass="windowsPE">
    <component name="Microsoft-Windows-International-Core-WinPE" processorArchitecture="amd64" publicKeyToken="31bf3856ad364e35" language="neutral" versionScope="nonSxS" xmlns:wcm="http://schemas.microsoft.com/WMIConfig/2002/State" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
      <SetupUILanguage><UILanguage>en-US</UILanguage></SetupUILanguage>
      <InputLocale>en-US</InputLocale>
      <SystemLocale>en-US</SystemLocale>
      <UILanguage>en-US</UILanguage>
      <UserLocale>en-US</UserLocale>
    </component>
    <component name="Microsoft-Windows-Setup" processorArchitecture="amd64" publicKeyToken="31bf3856ad364e35" language="neutral" versionScope="nonSxS" xmlns:wcm="http://schemas.microsoft.com/WMIConfig/2002/State" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
      <DynamicUpdate>
        <Enable>false</Enable>
        <WillShowUI>OnError</WillShowUI>
      </DynamicUpdate>
      <DiskConfiguration>{disk_configuration}
        <WillShowUI>OnError</WillShowUI>
      </DiskConfiguration>
      <ImageInstall>
        <OSImage>
          <InstallFrom>
            <MetaData wcm:action="add">
              <Key>/IMAGE/NAME</Key>
              <Value>{escape(display)}</Value>
            </MetaData>
          </InstallFrom>
          <InstallTo>
            <DiskID>0</DiskID>
            <PartitionID>{install_partition}</PartitionID>
          </InstallTo>
          <WillShowUI>OnError</WillShowUI>
          <InstallToAvailablePartition>false</InstallToAvailablePartition>
        </OSImage>
      </ImageInstall>
      <UserData>
        <ProductKey>
          <WillShowUI>Never</WillShowUI>
          <Key>{product_key}</Key>
        </ProductKey>
        <AcceptEula>true</AcceptEula>
        <FullName>v-EMU</FullName>
        <Organization>v-EMU Lab</Organization>
      </UserData>
    </component>
  </settings>
  <settings pass="specialize">
    <component name="Microsoft-Windows-Shell-Setup" processorArchitecture="amd64" publicKeyToken="31bf3856ad364e35" language="neutral" versionScope="nonSxS" xmlns:wcm="http://schemas.microsoft.com/WMIConfig/2002/State" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
      <ComputerName>{computer}</ComputerName>
      <TimeZone>{escape(timezone)}</TimeZone>
    </component>
    <component name="Microsoft-Windows-ServerManager-SvrMgrNc" processorArchitecture="amd64" publicKeyToken="31bf3856ad364e35" language="neutral" versionScope="nonSxS" xmlns:wcm="http://schemas.microsoft.com/WMIConfig/2002/State" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
      <DoNotOpenServerManagerAtLogon>true</DoNotOpenServerManagerAtLogon>
    </component>
    <component name="Microsoft-Windows-Deployment" processorArchitecture="amd64" publicKeyToken="31bf3856ad364e35" language="neutral" versionScope="nonSxS" xmlns:wcm="http://schemas.microsoft.com/WMIConfig/2002/State" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
      <RunSynchronous>{qemu_agent_command}
        <RunSynchronousCommand wcm:action="add">
          <Order>3</Order>
          <Path>cmd /c powercfg /hibernate off</Path>
        </RunSynchronousCommand>{rdp_commands}
      </RunSynchronous>
    </component>
  </settings>
  <settings pass="oobeSystem">
    <component name="Microsoft-Windows-International-Core" processorArchitecture="amd64" publicKeyToken="31bf3856ad364e35" language="neutral" versionScope="nonSxS" xmlns:wcm="http://schemas.microsoft.com/WMIConfig/2002/State" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
      <InputLocale>en-US</InputLocale>
      <SystemLocale>en-US</SystemLocale>
      <UILanguage>en-US</UILanguage>
      <UserLocale>en-US</UserLocale>
    </component>
    <component name="Microsoft-Windows-Shell-Setup" processorArchitecture="amd64" publicKeyToken="31bf3856ad364e35" language="neutral" versionScope="nonSxS" xmlns:wcm="http://schemas.microsoft.com/WMIConfig/2002/State" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
      <OOBE>
        <HideEULAPage>true</HideEULAPage>
        <HideLocalAccountScreen>true</HideLocalAccountScreen>
        <HideOEMRegistrationScreen>true</HideOEMRegistrationScreen>
        <HideOnlineAccountScreens>true</HideOnlineAccountScreens>
        <HideWirelessSetupInOOBE>true</HideWirelessSetupInOOBE>
        <NetworkLocation>Work</NetworkLocation>
        <ProtectYourPC>3</ProtectYourPC>
      </OOBE>
      <UserAccounts>
        <AdministratorPassword>
          <Value>{password}</Value>
          <PlainText>true</PlainText>
        </AdministratorPassword>
      </UserAccounts>
      <AutoLogon>
        <Password>
          <Value>{password}</Value>
          <PlainText>true</PlainText>
        </Password>
        <Enabled>true</Enabled>
        <Username>Administrator</Username>
        <LogonCount>1</LogonCount>
      </AutoLogon>{first_logon_commands}
    </component>
  </settings>
</unattend>
"""


def render_windows_oobe_unattend(
    *,
    computer_name: str,
    admin_password: str = DEFAULT_ADMIN_PASSWORD,
    timezone: str = "UTC",
) -> str:
    """First-boot answer file for a sysprepped Windows clone."""
    if len(admin_password) < 8:
        raise ValidationError(
            "Windows administrator password must be at least 8 characters."
        )
    computer = escape(windows_computer_name(computer_name))
    password = escape(admin_password)
    power_plan_oobe = ""
    power_plan = _high_performance_power_plan_command()
    if power_plan:
        power_plan_oobe = f"""
        <SynchronousCommand wcm:action="add">
          <Order>2</Order>
          <Description>Use the high performance power plan</Description>
          <CommandLine>{power_plan}</CommandLine>
        </SynchronousCommand>"""
    deployment_commands = """
    <component name="Microsoft-Windows-Deployment" processorArchitecture="amd64" publicKeyToken="31bf3856ad364e35" language="neutral" versionScope="nonSxS" xmlns:wcm="http://schemas.microsoft.com/WMIConfig/2002/State" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
      <RunSynchronous>
        <RunSynchronousCommand wcm:action="add">
          <Order>1</Order>
          <Path>cmd /c powercfg /change monitor-timeout-ac 0 &amp; powercfg /change monitor-timeout-dc 0 &amp; powercfg /change standby-timeout-ac 0 &amp; powercfg /change standby-timeout-dc 0</Path>
        </RunSynchronousCommand>"""
    if power_plan:
        deployment_commands += f"""
        <RunSynchronousCommand wcm:action="add">
          <Order>2</Order>
          <Path>{power_plan}</Path>
        </RunSynchronousCommand>"""
    deployment_commands += """
      </RunSynchronous>
    </component>"""
    first_logon_commands = f"""
      <FirstLogonCommands>
        <SynchronousCommand wcm:action="add">
          <Order>1</Order>
          <Description>Disable console blanking</Description>
          <CommandLine>cmd /c powercfg /change monitor-timeout-ac 0 &amp; powercfg /change monitor-timeout-dc 0 &amp; powercfg /change standby-timeout-ac 0 &amp; powercfg /change standby-timeout-dc 0</CommandLine>
        </SynchronousCommand>{power_plan_oobe}
      </FirstLogonCommands>"""
    return f"""<?xml version="1.0" encoding="utf-8"?>
<unattend xmlns="urn:schemas-microsoft-com:unattend">
  <settings pass="specialize">
    <component name="Microsoft-Windows-Shell-Setup" processorArchitecture="amd64" publicKeyToken="31bf3856ad364e35" language="neutral" versionScope="nonSxS" xmlns:wcm="http://schemas.microsoft.com/WMIConfig/2002/State" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
      <ComputerName>{computer}</ComputerName>
      <TimeZone>{escape(timezone)}</TimeZone>
    </component>
    <component name="Microsoft-Windows-ServerManager-SvrMgrNc" processorArchitecture="amd64" publicKeyToken="31bf3856ad364e35" language="neutral" versionScope="nonSxS" xmlns:wcm="http://schemas.microsoft.com/WMIConfig/2002/State" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
      <DoNotOpenServerManagerAtLogon>true</DoNotOpenServerManagerAtLogon>
    </component>{deployment_commands}
  </settings>
  <settings pass="oobeSystem">
    <component name="Microsoft-Windows-International-Core" processorArchitecture="amd64" publicKeyToken="31bf3856ad364e35" language="neutral" versionScope="nonSxS" xmlns:wcm="http://schemas.microsoft.com/WMIConfig/2002/State" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
      <InputLocale>en-US</InputLocale>
      <SystemLocale>en-US</SystemLocale>
      <UILanguage>en-US</UILanguage>
      <UserLocale>en-US</UserLocale>
    </component>
    <component name="Microsoft-Windows-Shell-Setup" processorArchitecture="amd64" publicKeyToken="31bf3856ad364e35" language="neutral" versionScope="nonSxS" xmlns:wcm="http://schemas.microsoft.com/WMIConfig/2002/State" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
      <OOBE>
        <HideEULAPage>true</HideEULAPage>
        <HideLocalAccountScreen>true</HideLocalAccountScreen>
        <HideOEMRegistrationScreen>true</HideOEMRegistrationScreen>
        <HideOnlineAccountScreens>true</HideOnlineAccountScreens>
        <HideWirelessSetupInOOBE>true</HideWirelessSetupInOOBE>
        <NetworkLocation>Work</NetworkLocation>
        <ProtectYourPC>3</ProtectYourPC>
      </OOBE>
      <UserAccounts>
        <AdministratorPassword>
          <Value>{password}</Value>
          <PlainText>true</PlainText>
        </AdministratorPassword>
      </UserAccounts>
      <AutoLogon>
        <Password>
          <Value>{password}</Value>
          <PlainText>true</PlainText>
        </Password>
        <Enabled>true</Enabled>
        <Username>Administrator</Username>
        <LogonCount>1</LogonCount>
      </AutoLogon>{first_logon_commands}
    </component>
  </settings>
</unattend>
"""


def _write_windows_answer_usb(
    chain_path: Path,
    xml_path: Path,
    *,
    image_name: str,
    answer_name: str,
) -> Path:
    """Create a small MBR/FAT16 USB image with an answer file.

    A partitioned image is required: Windows Setup does not scan a
    superfloppy (whole-device FAT12) USB disk for Autounattend.xml. The disk
    is attached as USB storage with ``removable=on``.
    """
    image = chain_path / image_name
    with image.open("wb") as stream:
        stream.truncate(64 * 1024 * 1024)
    sfdisk = find_which("sfdisk")
    parted = find_which("parted")
    mkfs = find_tool(("mkfs.fat", "mkfs.vfat"))
    mcopy = find_which("mcopy")
    if not mkfs or not mcopy or not (sfdisk or parted):
        raise NotFound(
            "Cannot build the Windows answer USB: install dosfstools, "
            "mtools and util-linux (sfdisk)."
        )
    if sfdisk:
        result = subprocess.run(
            [sfdisk, str(image)],
            input="label: dos\nstart=2048, type=6\n",
            capture_output=True,
            text=True,
            check=False,
        )
        table_cmd = [sfdisk, str(image)]
    else:
        result = subprocess.run(
            [
                parted,
                "-s",
                str(image),
                "mklabel",
                "msdos",
                "mkpart",
                "primary",
                "fat16",
                "1MiB",
                "100%",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        table_cmd = [parted, "-s", str(image), "mklabel", "msdos"]
    if result.returncode != 0:
        raise ExternalCommandError(
            " ".join(table_cmd),
            result.returncode,
            result.stderr or result.stdout,
        )
    result = subprocess.run(
        [
            mkfs,
            "-F",
            "16",
            "-n",
            "OEMDRV",
            "--offset=2048",
            str(image),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise ExternalCommandError(
            " ".join([mkfs, "-F", "16", "--offset=2048", str(image)]),
            result.returncode,
            result.stderr or result.stdout,
        )
    result = subprocess.run(
        [
            mcopy,
            "-o",
            "-i",
            f"{image}@@1048576",
            str(xml_path),
            f"::{answer_name}",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise ExternalCommandError(
            " ".join([mcopy, "-i", str(image), str(xml_path)]),
            result.returncode,
            result.stderr or result.stdout,
        )
    return image


def write_windows_oobe_floppy(
    chain_dir: str | Path,
    *,
    computer_name: str,
    admin_password: str = DEFAULT_ADMIN_PASSWORD,
) -> Path:
    chain_path = Path(chain_dir).expanduser().resolve()
    source = chain_path / "oobe"
    source.mkdir(parents=True, exist_ok=True)
    xml = render_windows_oobe_unattend(
        computer_name=computer_name,
        admin_password=admin_password,
    )
    xml_path = source / "unattend.xml"
    xml_path.write_text(xml, encoding="utf-8")
    return _write_windows_answer_usb(
        chain_path,
        xml_path,
        image_name="oobe.img",
        answer_name="UNATTEND.XML",
    )


def _iso_tool(volume_label: str = "OEMDRV") -> tuple[list[str], str]:
    xorriso = find_which("xorriso")
    if xorriso:
        return [
            xorriso,
            "-as",
            "mkisofs",
            "-quiet",
            "-volid",
            volume_label,
            "-J",
            "-R",
        ], "xorriso"
    genisoimage = find_which("genisoimage")
    if genisoimage:
        return [
            genisoimage,
            "-quiet",
            "-volid",
            volume_label,
            "-J",
            "-R",
        ], "genisoimage"
    mkisofs = find_which("mkisofs")
    if mkisofs:
        return [
            mkisofs,
            "-quiet",
            "-volid",
            volume_label,
            "-J",
            "-R",
        ], "mkisofs"
    raise NotFound(
        "Cannot build an ISO: install xorriso, genisoimage or mkisofs."
    )


def build_unattend_iso(source_dir: str | Path, output: str | Path) -> Path:
    source = Path(source_dir).expanduser().resolve()
    destination = Path(output).expanduser().resolve()
    if not (source / "autounattend.xml").is_file():
        raise NotFound(f"autounattend.xml was not found in {source}.")
    destination.parent.mkdir(parents=True, exist_ok=True)
    command, _tool = _iso_tool()
    result = subprocess.run(
        [*command, "-o", str(destination), str(source)],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise ExternalCommandError(
            " ".join([*command, "-o", str(destination), str(source)]),
            result.returncode,
            result.stderr or result.stdout,
        )
    if not destination.is_file() or destination.stat().st_size == 0:
        raise ExternalCommandError(
            " ".join([*command, "-o", str(destination), str(source)]),
            -1,
            "The ISO was not created.",
        )
    return destination


def write_windows_unattend(
    chain_dir: str | Path,
    *,
    computer_name: str,
    admin_password: str,
    edition: str,
    virtio_iso: bool,
    firmware: str = "bios",
) -> tuple[Path, Path]:
    chain_path = Path(chain_dir).expanduser().resolve()
    source = chain_path / "unattend"
    source.mkdir(parents=True, exist_ok=True)
    xml = render_windows_unattend(
        computer_name=computer_name,
        admin_password=admin_password,
        edition=edition,
        virtio_iso=virtio_iso,
        firmware=firmware,
    )
    (source / "autounattend.xml").write_text(xml, encoding="utf-8")
    output = chain_path / "unattend.iso"
    build_unattend_iso(source, output)
    return output, source / "autounattend.xml"


def write_windows_floppy(
    chain_dir: str | Path, xml_path: str | Path
) -> Path:
    """Create a small MBR/FAT16 USB image containing AUTOUNATTEND.XML.

    The image is attached as a removable USB storage device. The historical
    name is kept for callers; floppy controllers are not available on RHEL 10
    / QEMU 10.
    """
    chain_path = Path(chain_dir).expanduser().resolve()
    xml = Path(xml_path).expanduser().resolve()
    if not xml.is_file():
        raise NotFound(f"autounattend.xml was not found: {xml}")
    return _write_windows_answer_usb(
        chain_path,
        xml,
        image_name="unattend.img",
        answer_name="AUTOUNATTEND.XML",
    )