"""lab.yaml: a small declarative lab on top of v-EMU primitives."""

from __future__ import annotations

import json
import re
import shutil
import tarfile
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import service
from .backend import LibvirtBackend
from .chain import CHAIN_FILE
from .errors import Conflict, NotFound, ValidationError, VemuError
from .network import NetworkStore
from .yamlmini import YAMLError, dumps, load

LAB_VERSION = 1
MEMORY_RE = re.compile(r"^(\d+)\s*([MGmg])?$")


@dataclass
class LabSpec:
    path: Path
    name: str
    dest: Path
    networks: dict[str, dict[str, Any]] = field(default_factory=dict)
    vms: dict[str, dict[str, Any]] = field(default_factory=dict)
    templates_root: Path | None = None

    @property
    def directory(self) -> Path:
        return self.path.parent


def _resolve(base: Path, value: str) -> Path:
    path = Path(value).expanduser()
    return (base / path).resolve() if not path.is_absolute() else path.resolve()


def _memory_mib(value: Any) -> int:
    if isinstance(value, int):
        return value
    match = MEMORY_RE.match(str(value).strip())
    if not match:
        raise ValidationError(f"Invalid memory value: {value!r}")
    amount = int(match.group(1))
    unit = (match.group(2) or "M").lower()
    return amount * 1024 if unit == "g" else amount


def _bool(value: Any, default: bool = False) -> bool:
    return default if value is None else bool(value)


def load_lab(path: str | Path) -> LabSpec:
    lab_path = Path(path).expanduser().resolve()
    if not lab_path.is_file():
        raise NotFound(f"Lab file was not found: {lab_path}")
    try:
        data = load(lab_path)
    except YAMLError as exc:
        raise ValidationError(f"Invalid lab.yaml: {exc}") from exc
    if not isinstance(data, dict):
        raise ValidationError("lab.yaml must be a mapping.")
    version = int(data.get("version", LAB_VERSION))
    if version != LAB_VERSION:
        raise ValidationError(
            f"Unsupported lab.yaml version {version}; expected {LAB_VERSION}."
        )
    name = str(data.get("lab") or "").strip()
    if not name:
        raise ValidationError("lab.yaml is missing 'lab'.")
    dest_value = data.get("dest")
    if not dest_value:
        raise ValidationError("lab.yaml is missing 'dest'.")
    networks = data.get("networks") or data.get("network") or {}
    vms = data.get("vms") or {}
    if not isinstance(networks, dict) or not isinstance(vms, dict):
        raise ValidationError("'networks' and 'vms' must be mappings.")
    templates_root = data.get("templates_root")
    return LabSpec(
        path=lab_path,
        name=name,
        dest=_resolve(lab_path.parent, str(dest_value)),
        networks=networks,
        vms=vms,
        templates_root=(
            _resolve(lab_path.parent, str(templates_root))
            if templates_root
            else None
        ),
    )


def _template_path(spec: LabSpec, value: str) -> Path:
    path = Path(value).expanduser()
    if path.is_absolute():
        return path
    if spec.templates_root is not None:
        return (spec.templates_root / path).resolve()
    return (spec.directory / path).resolve()


def _ensure_network(
    spec: LabSpec,
    name: str,
    conf: dict[str, Any],
    network_config: str | None,
    uri: str,
    actions: list[str],
) -> None:
    store = NetworkStore(network_config)
    profile = {
        "mode": str(conf.get("mode", "nat")),
        "subnet": str(conf.get("subnet", "auto")),
        "dhcp": _bool(conf.get("dhcp"), profile_default(conf)),
        "dns_domain": str(conf.get("dns_domain", "vemu.local")),
        "description": str(conf.get("description", "")),
    }
    try:
        store.get(name)
        actions.append(f"network {name}: exists")
    except NotFound:
        service.network_add(
            name,
            profile["mode"],
            profile["subnet"],
            profile["dhcp"],
            profile["dns_domain"],
            profile["description"],
            config=network_config,
        )
        actions.append(f"network {name}: created")
    if conf.get("forwards"):
        actions.append(
            f"network {name}: forwards in lab.yaml are not applied yet; "
            "use 'vemu network forward add'"
        )


def profile_default(conf: dict[str, Any]) -> bool:
    return str(conf.get("mode", "nat")) == "nat"


def _lab_network_names(spec: LabSpec) -> list[str]:
    return list(spec.networks)


def _vm_path(spec: LabSpec, name: str) -> Path:
    return spec.dest / name


def _vm_networks(vm: dict[str, Any]) -> list[str]:
    values = vm.get("networks")
    if values is None:
        values = ["default"]
    if not isinstance(values, list) or not values:
        raise ValidationError("Each VM needs a non-empty 'networks' list.")
    return [str(value) for value in values]


def _vm_ip(vm: dict[str, Any], network: str) -> str | None:
    ip = vm.get("ip")
    if isinstance(ip, str):
        return ip
    if isinstance(ip, dict):
        value = ip.get(network)
        return str(value) if value else None
    return None


def _vm_cloud_init(
    spec: LabSpec, vm: dict[str, Any], primary_network: str
) -> dict[str, Any]:
    raw = vm.get("cloud_init")
    if not raw:
        return {"cloud_init": False}
    if not isinstance(raw, dict):
        raise ValidationError("'cloud_init' must be a mapping.")
    ssh_key = raw.get("ssh_key")
    ssh_path = None
    if ssh_key:
        ssh_path = str(_resolve(spec.directory, str(ssh_key)))
    ip = _vm_ip(vm, primary_network)
    return {
        "cloud_init": True,
        "cloud_init_user": str(raw.get("user", "lab")),
        "cloud_init_password": raw.get("password"),
        "cloud_init_ssh_key_file": ssh_path,
        "cloud_init_packages": [
            str(item) for item in (raw.get("packages") or [])
        ],
        "cloud_init_run": [
            str(item) for item in (raw.get("runcmd") or [])
        ],
        "cloud_init_ip": ip,
        "cloud_init_gateway": raw.get("gateway") or vm.get("gateway"),
        "cloud_init_dns": [
            str(item) for item in (raw.get("dns") or [])
        ],
    }


def _domain_exists(name: str, uri: str) -> bool:
    try:
        with LibvirtBackend(uri) as backend:
            backend.info(name)
        return True
    except (NotFound, VemuError):
        return False


def lab_up(
    path: str | Path,
    *,
    uri: str = "qemu:///system",
    network_config: str | None = None,
    start: bool = True,
    dry_run: bool = False,
    only: list[str] | None = None,
    networks: bool = True,
) -> dict[str, Any]:
    spec = load_lab(path)
    selected = None
    if only is not None:
        selected = {str(name) for name in only}
        unknown = sorted(selected - set(spec.vms))
        if unknown:
            raise ValidationError(
                f"Unknown lab VM(s): {', '.join(unknown)}"
            )
        if not selected:
            raise ValidationError("Select at least one VM.")
    actions: list[str] = []
    warnings: list[str] = []
    for name, conf in spec.networks.items():
        if not networks:
            break
        if not isinstance(conf, dict):
            raise ValidationError(f"Network '{name}' must be a mapping.")
        if dry_run:
            actions.append(f"network {name}: plan")
        else:
            _ensure_network(
                spec, name, conf, network_config, uri, actions
            )
    if not dry_run and spec.networks:
        service.network_apply(network_config, uri)

    for vm_name, vm in spec.vms.items():
        if selected is not None and vm_name not in selected:
            continue
        if not isinstance(vm, dict):
            raise ValidationError(f"VM '{vm_name}' must be a mapping.")
        template_value = vm.get("template")
        if not template_value:
            raise ValidationError(f"VM '{vm_name}' is missing 'template'.")
        template = _template_path(spec, str(template_value))
        networks = _vm_networks(vm)
        primary = networks[0]
        if len(networks) > 1:
            warnings.append(
                f"{vm_name}: only '{primary}' receives cloud-init static IP; "
                "extra NICs are attached after creation"
            )
        chain_dir = _vm_path(spec, vm_name)
        if chain_dir.exists():
            if not _domain_exists(vm_name, uri):
                raise Conflict(
                    f"{vm_name}: chain exists at {chain_dir} but the libvirt "
                    "domain is missing. Remove the chain or adopt the VM."
                )
            actions.append(f"{vm_name}: exists")
            if start and not dry_run:
                try:
                    with LibvirtBackend(uri) as backend:
                        if backend.info(vm_name).state != "running":
                            backend.start(vm_name)
                            actions.append(f"{vm_name}: started")
                except VemuError as exc:
                    warnings.append(f"{vm_name}: {exc}")
            continue
        if dry_run:
            actions.append(f"{vm_name}: create from {template}")
            continue
        ci = _vm_cloud_init(spec, vm, primary)
        result = service.clone_create(
            str(template),
            str(spec.dest),
            count=1,
            name=vm_name,
            define=True,
            start=start,
            memory=_memory_mib(vm.get("memory", 2048)),
            vcpus=int(vm.get("cpus", 2)),
            network=primary,
            network_config=network_config,
            uri=uri,
            reset_identity=bool(vm.get("reset_identity", False)),
            **ci,
        )
        actions.append(f"{vm_name}: created from {template}")
        for warning in result.get("warnings", []):
            warnings.append(f"{vm_name}: {warning}")
        for extra in networks[1:]:
            service.network_attach(
                vm_name,
                extra,
                config=network_config,
                uri=uri,
            )
            actions.append(f"{vm_name}: attached {extra}")
        if result["created"][0].get("cloud_init_iso"):
            actions.append(f"{vm_name}: cloud-init seed attached")
    return {
        "lab": spec.name,
        "path": str(spec.path),
        "actions": actions,
        "warnings": warnings,
        "dry_run": dry_run,
    }


def lab_down(
    path: str | Path,
    *,
    uri: str = "qemu:///system",
    force: bool = False,
) -> dict[str, Any]:
    spec = load_lab(path)
    actions: list[str] = []
    warnings: list[str] = []
    with LibvirtBackend(uri) as backend:
        for vm_name in spec.vms:
            try:
                info = backend.info(vm_name)
            except NotFound:
                actions.append(f"{vm_name}: not defined")
                continue
            if info.state != "running":
                actions.append(f"{vm_name}: already stopped")
                continue
            try:
                if force:
                    backend.destroy(vm_name)
                else:
                    backend.shutdown(vm_name)
                actions.append(f"{vm_name}: {'stopped' if force else 'shutdown'}")
            except VemuError as exc:
                warnings.append(f"{vm_name}: {exc}")
    return {"lab": spec.name, "actions": actions, "warnings": warnings}


def lab_reset(
    path: str | Path,
    vms: list[str],
    *,
    force: bool = False,
    start: bool = True,
    uri: str = "qemu:///system",
    network_config: str | None = None,
) -> dict[str, Any]:
    """Recreate selected lab VMs from their templates.

    Networks are left untouched. Running VMs require ``force`` and are
    force-off before their managed chain is removed.
    """
    spec = load_lab(path)
    selected = [str(name) for name in vms]
    if not selected:
        raise ValidationError("Select at least one VM to reset.")
    unknown = sorted(name for name in selected if name not in spec.vms)
    if unknown:
        raise ValidationError(f"Unknown lab VM(s): {', '.join(unknown)}")

    actions: list[str] = []
    warnings: list[str] = []
    with LibvirtBackend(uri) as backend:
        running = []
        for name in selected:
            try:
                if backend.info(name).state == "running":
                    running.append(name)
            except VemuError:
                pass
    if running and not force:
        raise Conflict(
            "Force off the selected running VMs first or use --force: "
            + ", ".join(running)
        )

    for name in selected:
        chain_dir = _vm_path(spec, name)
        if chain_dir.exists() and not (chain_dir / CHAIN_FILE).is_file():
            raise Conflict(
                f"{name}: chain exists but is not managed by v-EMU; "
                "remove it manually before resetting."
            )
        if _domain_exists(name, uri):
            try:
                result = service.domain_delete(
                    name,
                    delete_storage=True,
                    force=force,
                    uri=uri,
                )
            except VemuError as exc:
                raise Conflict(
                    f"{name}: cannot delete existing VM: {exc}"
                ) from exc
            actions.append(f"{name}: deleted existing VM")
            warnings.extend(result.get("warnings", []))
        elif chain_dir.exists():
            shutil.rmtree(chain_dir)
            actions.append(f"{name}: removed managed chain")
        else:
            actions.append(f"{name}: not present; creating")

    created = lab_up(
        path,
        uri=uri,
        network_config=network_config,
        start=start,
        only=selected,
        networks=False,
    )
    actions.extend(created.get("actions", []))
    warnings.extend(created.get("warnings", []))
    return {
        "lab": spec.name,
        "path": str(spec.path),
        "vms": selected,
        "actions": actions,
        "warnings": warnings,
    }


def _networks_used_outside(
    uri: str, lab_vms: set[str]
) -> set[str]:
    used: set[str] = set()
    try:
        with LibvirtBackend(uri) as backend:
            for domain in backend.list_domains():
                if domain.name in lab_vms:
                    continue
                for interface in backend.interfaces(domain.name):
                    name = interface.get("network") or interface.get("bridge")
                    if name:
                        used.add(str(name))
    except VemuError:
        return used
    return used


def lab_destroy(
    path: str | Path,
    *,
    force: bool = False,
    keep_networks: bool = False,
    uri: str = "qemu:///system",
    network_config: str | None = None,
) -> dict[str, Any]:
    """Remove all lab VMs, managed chains and (optionally) lab networks.

    Templates and the lab.yaml file are never touched.
    """
    spec = load_lab(path)
    actions: list[str] = []
    warnings: list[str] = []
    lab_vms = set(spec.vms)

    with LibvirtBackend(uri) as backend:
        running = []
        for name in lab_vms:
            try:
                if backend.info(name).state == "running":
                    running.append(name)
            except VemuError:
                pass
    if running and not force:
        raise Conflict(
            "Force off the running lab VMs first or use --force: "
            + ", ".join(sorted(running))
        )

    for name in spec.vms:
        chain_dir = _vm_path(spec, name)
        if _domain_exists(name, uri):
            try:
                result = service.domain_delete(
                    name,
                    delete_storage=True,
                    force=force,
                    uri=uri,
                )
            except VemuError as exc:
                warnings.append(f"{name}: {exc}")
                continue
            actions.append(f"{name}: removed")
            warnings.extend(result.get("warnings", []))
        elif chain_dir.exists():
            if (chain_dir / CHAIN_FILE).is_file():
                shutil.rmtree(chain_dir)
                actions.append(f"{name}: removed managed chain")
            else:
                warnings.append(
                    f"{name}: kept unmanaged chain at {chain_dir}"
                )
        else:
            actions.append(f"{name}: not present")

    if not keep_networks:
        used_elsewhere = _networks_used_outside(uri, lab_vms)
        store = NetworkStore(network_config)
        for name in spec.networks:
            libvirt_name = store.find_libvirt_name(name) or name
            if libvirt_name in used_elsewhere:
                warnings.append(
                    f"network {name}: kept because another VM uses it"
                )
                continue
            try:
                service.network_remove(
                    name,
                    config=network_config,
                    uri=uri,
                    purge=True,
                )
            except NotFound:
                warnings.append(f"network {name}: profile not found")
            except VemuError as exc:
                warnings.append(f"network {name}: {exc}")
            else:
                actions.append(f"network {name}: removed")

    try:
        spec.dest.rmdir()
    except OSError:
        pass

    return {
        "lab": spec.name,
        "path": str(spec.path),
        "actions": actions,
        "warnings": warnings,
        "vms": sorted(lab_vms),
        "networks": sorted(spec.networks),
    }


def lab_status(
    path: str | Path,
    *,
    uri: str = "qemu:///system",
    network_config: str | None = None,
) -> dict[str, Any]:
    spec = load_lab(path)
    profiles = {
        item["name"]: item
        for item in service.networks_list(network_config, uri)
    }
    networks = []
    for name in spec.networks:
        profile = profiles.get(name)
        networks.append(
            {
                "name": name,
                "defined": bool(profile and profile["runtime"]["defined"]),
                "active": bool(profile and profile["runtime"]["active"]),
                "error": profile["runtime"].get("error")
                if profile
                else None,
            }
        )
    vms = []
    with LibvirtBackend(uri) as backend:
        for vm_name, vm in spec.vms.items():
            try:
                info = backend.info(vm_name)
                state = info.state
            except VemuError:
                state = "missing"
            vms.append(
                {
                    "name": vm_name,
                    "state": state,
                    "networks": _vm_networks(vm),
                    "template": str(vm.get("template", "")),
                }
            )
    return {"lab": spec.name, "networks": networks, "vms": vms}


def _template_refs(spec: LabSpec) -> list[dict[str, Any]]:
    refs: list[dict[str, Any]] = []
    seen: set[str] = set()
    for vm in spec.vms.values():
        value = vm.get("template")
        if not value:
            continue
        path = _template_path(spec, str(value))
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        metadata = path / "template.json" if path.is_dir() else None
        name = path.name
        if metadata is not None and metadata.is_file():
            try:
                data = json.loads(metadata.read_text(encoding="utf-8"))
                name = str(data.get("name", name))
            except (OSError, ValueError):
                pass
        refs.append(
            {
                "name": name,
                "path": str(path),
                "metadata": bool(metadata and metadata.is_file()),
            }
        )
    return refs


def lab_export(
    path: str | Path,
    destination: str | Path,
    *,
    with_templates: bool = False,
) -> dict[str, Any]:
    spec = load_lab(path)
    dest = Path(destination).expanduser().resolve()
    if dest.exists():
        raise Conflict(f"Bundle destination already exists: {dest}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    refs = _template_refs(spec)
    warnings: list[str] = []
    with tempfile.TemporaryDirectory() as tmp:
        staging = Path(tmp)
        shutil.copy2(spec.path, staging / "lab.yaml")
        manifest = {
            "version": 1,
            "lab": spec.name,
            "created_at": time.strftime(
                "%Y-%m-%dT%H:%M:%SZ", time.gmtime()
            ),
            "templates": refs,
            "with_templates": with_templates,
        }
        (staging / "manifest.json").write_text(
            json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
        )
        instructions = (
            f"# v-EMU lab bundle: {spec.name}\n\n"
            "Import with:\n\n"
            "    vemu lab import --bundle "
            f"{dest.name} --dest <directory>\n\n"
            "Then start the lab with:\n\n"
            "    vemu up <directory>/lab.yaml\n"
        )
        (staging / "README.md").write_text(instructions, encoding="utf-8")

        if with_templates:
            templates_dir = staging / "templates"
            templates_dir.mkdir()
            for ref in refs:
                source = Path(ref["path"])
                if not ref["metadata"]:
                    warnings.append(
                        f"template {ref['name']} is a bare image; "
                        "not included in the bundle"
                    )
                    continue
                target = templates_dir / ref["name"]
                if target.exists():
                    warnings.append(
                        f"template {ref['name']} is referenced twice; "
                        "included once"
                    )
                    continue
                shutil.copytree(source, target)
        with tarfile.open(dest, "w:gz") as archive:
            for item in staging.iterdir():
                archive.add(item, arcname=item.name)
    return {
        "lab": spec.name,
        "bundle": str(dest),
        "size": dest.stat().st_size,
        "templates": refs,
        "with_templates": with_templates,
        "warnings": warnings,
    }


def _safe_extract(archive: tarfile.TarFile, target: Path) -> None:
    root = target.resolve()
    for member in archive.getmembers():
        destination = (target / member.name).resolve()
        if destination != root and root not in destination.parents:
            raise ValidationError(
                f"Bundle contains an unsafe path: {member.name}"
            )
    archive.extractall(target)


def lab_import(
    bundle: str | Path,
    destination: str | Path,
    *,
    templates_root: str | Path | None = None,
    overwrite: bool = False,
) -> dict[str, Any]:
    archive_path = Path(bundle).expanduser().resolve()
    if not archive_path.is_file():
        raise NotFound(f"Bundle was not found: {archive_path}")
    dest = Path(destination).expanduser().resolve()
    if dest.exists() and any(dest.iterdir()) and not overwrite:
        raise Conflict(f"Import destination is not empty: {dest}")
    dest.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        staging = Path(tmp)
        with tarfile.open(archive_path, "r:gz") as archive:
            _safe_extract(archive, staging)
        manifest_path = staging / "manifest.json"
        lab_path = staging / "lab.yaml"
        if not lab_path.is_file() or not manifest_path.is_file():
            raise ValidationError(
                "Bundle is missing lab.yaml or manifest.json."
            )
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        data = load(lab_path)
        if not isinstance(data, dict):
            raise ValidationError("Bundle lab.yaml is not a mapping.")
        labs_dir = dest / "labs"
        templates_dir = (
            Path(templates_root).expanduser().resolve()
            if templates_root
            else dest / "templates"
        )
        data["dest"] = str(labs_dir)
        data["templates_root"] = str(templates_dir)
        labs_dir.mkdir(parents=True, exist_ok=True)
        templates_dir.mkdir(parents=True, exist_ok=True)

        included = 0
        bundle_templates = staging / "templates"
        if bundle_templates.is_dir():
            for source in bundle_templates.iterdir():
                target = templates_dir / source.name
                if target.exists():
                    if not overwrite:
                        raise Conflict(
                            f"Template already exists: {target}"
                        )
                    shutil.rmtree(target)
                shutil.copytree(source, target)
                included += 1
        (dest / "lab.yaml").write_text(dumps(data), encoding="utf-8")
        if (staging / "README.md").is_file():
            shutil.copy2(staging / "README.md", dest / "README.md")
    return {
        "lab": manifest.get("lab", data.get("lab", "")),
        "destination": str(dest),
        "lab_file": str(dest / "lab.yaml"),
        "templates_root": str(templates_dir),
        "templates_imported": included,
        "templates_required": len(manifest.get("templates", [])),
    }
