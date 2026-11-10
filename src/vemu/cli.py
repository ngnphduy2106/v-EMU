"""Command line interface for v-EMU."""

from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from .virtcheck import host_arch

from . import __version__
from . import diagnose
from . import lab
from . import service
from .backend import LibvirtBackend
from .chain import ChainManager, find_commit_journals
from .client import DaemonClient, DaemonError, socket_available
from .errors import NotFound, ValidationError, VemuError
from .storage import validate_role
from .template import list_templates, slugify
from .provision import DEFAULT_ADMIN_PASSWORD, WINDOWS_EDITIONS


def _print_table(headers: list[str], rows: list[list[str]]) -> None:
    widths = [len(header) for header in headers]
    for row in rows:
        for index, value in enumerate(row):
            widths[index] = max(widths[index], len(value))
    fmt = "  ".join(f"{{:<{width}}}" for width in widths)
    print(fmt.format(*headers))
    print(fmt.format(*["-" * width for width in widths]))
    for row in rows:
        print(fmt.format(*row))


def _emit(payload: dict[str, Any], as_json: bool, text: str) -> None:
    if as_json:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        print(text)


def _daemon_enabled(args: argparse.Namespace) -> bool:
    if getattr(args, "direct", False):
        return False
    if getattr(args, "socket", None) or os.environ.get("VEMU_SOCKET"):
        return True
    return socket_available()


def _daemon_call(args: argparse.Namespace, method: str, params: dict[str, Any]) -> Any:
    # The daemon owns the libvirt URI. Do not send a client-selected one over
    # the socket; --uri remains meaningful only for --direct development runs.
    params = {key: value for key, value in params.items() if key != "uri"}
    return DaemonClient(getattr(args, "socket", None)).call(method, params)


def _print_created(created: list[dict[str, Any]], as_json: bool) -> None:
    if as_json:
        print(json.dumps({"created": created}, indent=2, ensure_ascii=False))
        return
    for item in created:
        suffix = ""
        if item.get("identity_reset"):
            suffix += " · identity reset"
        if item.get("defined"):
            suffix += " · domain defined"
        if item.get("started"):
            suffix += " · started"
        print(f"Created {item['name']}: {item['disk']}{suffix}")


def _cmd_list(args: argparse.Namespace) -> int:
    if _daemon_enabled(args):
        domains = _daemon_call(args, "domains.list", {"uri": args.uri})
        if args.json:
            print(json.dumps({"domains": domains}, indent=2, ensure_ascii=False))
            return 0
        if not domains:
            print("No domains found.")
            return 0
        rows = [
            [
                domain["name"],
                domain["state"],
                "on" if domain.get("autostart") else "off",
                ", ".join(domain["disks"]) or "-",
            ]
            for domain in domains
        ]
        _print_table(["NAME", "STATE", "AUTOSTART", "DISKS"], rows)
        return 0

    with LibvirtBackend(args.uri) as backend:
        domains = backend.list_domains()
    payload = {
        "domains": [
            {
                "name": domain.name,
                "uuid": domain.uuid,
                "state": domain.state,
                "disks": domain.disks,
                "autostart": domain.autostart,
            }
            for domain in domains
        ]
    }
    if args.json:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return 0
    if not domains:
        print("No domains found.")
        return 0
    rows = [
        [domain.name, domain.state, ", ".join(domain.disks) or "-"]
        for domain in domains
    ]
    _print_table(["NAME", "STATE", "DISKS"], rows)
    return 0


def _cmd_clone(args: argparse.Namespace) -> int:
    if args.start and not args.define:
        raise ValidationError("--start requires --define.")
    reset_identity = not args.no_identity_reset

    if _daemon_enabled(args):
        result = _daemon_call(
            args,
            "clone.create",
            {
                "template": args.template,
                "dest": args.dest,
                "count": args.count,
                "name": args.name,
                "start_at": args.start_at,
                "define": args.define,
                "start": args.start,
                "memory": args.memory,
                "vcpus": args.vcpus,
                "network": args.network,
                "network_config": args.network_config,
                "firmware": args.firmware,
                "uri": args.uri,
                "reset_identity": reset_identity,
                "cloud_init": args.cloud_init,
                "cloud_init_user": args.ci_user,
                "cloud_init_password": args.ci_password,
                "cloud_init_ssh_key_file": args.ci_ssh_key,
                "cloud_init_packages": args.ci_package,
                "cloud_init_run": args.ci_run,
                "cloud_init_ip": args.ci_ip,
                "cloud_init_gateway": args.ci_gateway,
                "cloud_init_dns": args.ci_dns,
                "windows_oobe": args.windows_oobe,
                "windows_admin_password": args.windows_admin_password,
            },
        )
    else:
        result = service.clone_create(
            args.template,
            args.dest,
            count=args.count,
            name=args.name,
            start_at=args.start_at,
            define=args.define,
            start=args.start,
            memory=args.memory,
            vcpus=args.vcpus,
            network=args.network,
            network_config=args.network_config,
            firmware=args.firmware,
            uri=args.uri,
            reset_identity=reset_identity,
            cloud_init=args.cloud_init,
            cloud_init_user=args.ci_user,
            cloud_init_password=args.ci_password,
            cloud_init_ssh_key_file=args.ci_ssh_key,
            cloud_init_packages=args.ci_package,
            cloud_init_run=args.ci_run,
            cloud_init_ip=args.ci_ip,
            cloud_init_gateway=args.ci_gateway,
            cloud_init_dns=args.ci_dns,
            windows_oobe=args.windows_oobe,
            windows_admin_password=args.windows_admin_password,
        )
    _print_created(result["created"], args.json)
    if not args.json:
        for warning in result.get("warnings", []):
            print(f"Warning: {warning}")
    return 0


def _print_chain_info(info: dict[str, Any], as_json: bool) -> None:
    if as_json:
        print(json.dumps(info, indent=2, ensure_ascii=False))
        return
    print(f"Base:   {info['base']}")
    print(f"Active: {info['active']}")
    print(f"Disk:   {info['active_disk']}")
    print()
    rows = []
    for layer in info["layers"]:
        marker = "*" if layer["active"] else " "
        size_mb = layer["actual_size"] / (1024 * 1024)
        rows.append(
            [
                marker,
                layer["id"],
                layer["label"] or "-",
                layer["parent"],
                f"{size_mb:.1f} MB",
            ]
        )
    _print_table(["", "ID", "LABEL", "PARENT", "SIZE"], rows)


def _cmd_up(args: argparse.Namespace) -> int:
    params = {
        "path": args.path,
        "uri": args.uri,
        "config": args.network_config,
        "network_config": args.network_config,
        "start": not args.no_start,
        "dry_run": args.dry_run,
    }
    if _daemon_enabled(args):
        result = _daemon_call(args, "lab.up", params)
    else:
        result = lab.lab_up(
            args.path,
            uri=args.uri,
            network_config=args.network_config,
            start=not args.no_start,
            dry_run=args.dry_run,
        )
    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0
    print(f"Lab: {result['lab']}")
    for action in result["actions"]:
        print(f"  {action}")
    for warning in result["warnings"]:
        print(f"  warning: {warning}")
    return 0


def _cmd_down(args: argparse.Namespace) -> int:
    params = {
        "path": args.path,
        "uri": args.uri,
        "force": args.force,
    }
    if _daemon_enabled(args):
        result = _daemon_call(args, "lab.down", params)
    else:
        result = lab.lab_down(
            args.path, uri=args.uri, force=args.force
        )
    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0
    print(f"Lab: {result['lab']}")
    for action in result["actions"]:
        print(f"  {action}")
    for warning in result["warnings"]:
        print(f"  warning: {warning}")
    return 0


def _cmd_init(args: argparse.Namespace) -> int:
    path = Path(args.path).expanduser().resolve()
    if path.exists() and not args.force:
        raise ValidationError(
            f"{path} already exists; use --force to overwrite it."
        )
    name = args.name or path.stem or "lab"
    text = f"""\
version: 1
lab: {json.dumps(name)}
dest: {json.dumps(args.dest)}
networks:
  default:
    mode: nat
    subnet: auto
vms:
  node1:
    template: {json.dumps(args.template)}
    cpus: 2
    memory: 2G
    networks: [default]
"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    print(f"Created {path}")
    print("Next: edit the template name, then run 'vemu up " + str(path) + "'.")
    return 0


def _topology(args: argparse.Namespace) -> dict[str, Any]:
    config = getattr(args, "config", None) or getattr(
        args, "network_config", None
    )
    if _daemon_enabled(args):
        return _daemon_call(
            args,
            "network.topology",
            {
                "uri": getattr(args, "uri", "qemu:///system"),
                "config": config,
            },
        )
    return service.network_topology(
        config=config,
        uri=getattr(args, "uri", "qemu:///system"),
    )


def _vm_guest_ip(args: argparse.Namespace, domain: str) -> str | None:
    for vm in _topology(args).get("vms", []):
        if str(vm.get("name", "")) != domain:
            continue
        for interface in vm.get("interfaces") or []:
            ips = interface.get("ips") or []
            for ip in ips:
                if ip and not str(ip).startswith("127."):
                    return str(ip)
    return None


def _cmd_ssh(args: argparse.Namespace) -> int:
    ip = args.ip or _vm_guest_ip(args, args.domain)
    if not ip:
        raise ValidationError(
            f"No guest IP for '{args.domain}'. Wait for qemu-guest-agent or "
            "use --ip."
        )
    command = ["ssh"]
    if args.key:
        command.extend(["-i", str(Path(args.key).expanduser())])
    if args.port:
        command.extend(["-p", str(args.port)])
    command.append(f"{args.user}@{ip}")
    if args.remote_command:
        command.append(args.remote_command)
    if args.dry_run:
        print(shlex.join(command))
        return 0
    return subprocess.call(command)


def _cmd_console(args: argparse.Namespace) -> int:
    if args.serial:
        try:
            if _daemon_enabled(args):
                payload = _daemon_call(
                    args,
                    "serial.info",
                    {"domain": args.domain, "uri": args.uri},
                )
            else:
                payload = service.serial_info(args.domain, args.uri)
        except NotFound as exc:
            raise ValidationError(
                f"{exc} Add one with 'vemu config serial-add' while the VM "
                "is stopped."
            ) from exc
        except DaemonError as exc:
            if exc.type_name != "NotFound":
                raise
            raise ValidationError(
                f"{exc} Add one with 'vemu config serial-add' while the VM "
                "is stopped."
            ) from exc
        if args.json:
            print(json.dumps(payload, indent=2, ensure_ascii=False))
        elif args.open:
            virsh = shutil.which("virsh")
            if not virsh:
                raise ValidationError("virsh was not found.")
            return subprocess.call(
                [virsh, "-c", args.uri, "console", args.domain]
            )
        else:
            print(f"{args.domain}: serial console available; use --open")
        return 0
    if _daemon_enabled(args):
        payload = _daemon_call(
            args, "console.info", {"domain": args.domain, "uri": args.uri}
        )
    else:
        payload = service.console_info(args.domain, args.uri)
    if args.json:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        print(
            f"{args.domain}: vnc://{payload.get('host') or '127.0.0.1'}:"
            f"{payload.get('port')} (websocket {payload.get('websocket')})"
        )
    if args.open:
        viewer = shutil.which("virt-viewer") or shutil.which("remote-viewer")
        if not viewer:
            raise ValidationError(
                "No virt-viewer/remote-viewer found. Install virt-viewer or "
                "use the desktop app."
            )
        url = f"vnc://{payload.get('host') or '127.0.0.1'}:{payload.get('port')}"
        return subprocess.call([viewer, url])
    return 0


def _cmd_snapshot_create(args: argparse.Namespace) -> int:
    params = {
        "domain": args.domain,
        "label": args.label,
        "description": args.description,
        "uri": args.uri,
    }
    if _daemon_enabled(args):
        payload = _daemon_call(args, "snapshots.create", params)
    else:
        payload = service.snapshots_create(
            args.domain,
            args.label,
            args.uri,
            description=args.description,
        )
    layer = payload["layer"]
    message = (
        f"Created snapshot {layer['id']} "
        f"({layer['label'] or 'no label'}) on {args.domain}."
    )
    ejected = payload.get("ejected") or []
    if ejected:
        message += f" Ejected CD/DVD: {', '.join(str(item) for item in ejected)}."
    _emit(payload, args.json, message)
    return 0


def _cmd_snapshot_list(args: argparse.Namespace) -> int:
    if _daemon_enabled(args):
        payload = _daemon_call(
            args, "snapshots.info", {"domain": args.domain, "uri": args.uri}
        )
    else:
        payload = service.snapshots_info(args.domain, args.uri)
    if args.json:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return 0
    if not payload.get("managed"):
        snapshots = payload.get("snapshots", [])
        if not snapshots:
            print(payload.get("reason", "This VM is not a managed chain."))
            return 0
        print(
            "This VM is not a v-EMU chain. Libvirt/Cockpit snapshots "
            "below are read-only:"
        )
        columns = ["NAME", "TYPE", "TARGET", "SNAPSHOT", "SOURCE"]
        rows = []
        for snapshot in snapshots:
            disks = snapshot.get("disks", [])
            if not disks:
                rows.append(
                    [str(snapshot.get("name", "")), str(snapshot.get("kind", "")),
                     "", "", ""]
                )
            for disk in disks:
                rows.append(
                    [
                        str(snapshot.get("name", "")),
                        str(snapshot.get("kind", "")),
                        str(disk.get("target", "")),
                        str(disk.get("snapshot", "")),
                        str(disk.get("source", "")),
                    ]
                )
        _print_table(columns, rows)
        return 0
    columns = ["ID", "LABEL", "PARENT", "ACTIVE", "SIZE", "DESCRIPTION"]
    rows = []
    for layer in payload.get("layers", []):
        size = int(layer.get("actual_size", 0)) / (1024 ** 2)
        description = (
            str(layer.get("description", ""))
            .replace("\n", " ")
            .replace("\t", " ")
        )
        rows.append(
            [
                str(layer.get("id", "")),
                str(layer.get("label", "")),
                str(layer.get("parent", "")),
                "yes" if layer.get("active") else "no",
                f"{size:.1f} MiB",
                description[:80],
            ]
        )
    _print_table(columns, rows)
    return 0


def _cmd_snapshot_edit(args: argparse.Namespace) -> int:
    params = {
        "domain": args.domain,
        "layer": args.layer,
        "uri": args.uri,
    }
    if args.label is not None:
        params["label"] = args.label
    if args.description is not None:
        params["description"] = args.description
    if _daemon_enabled(args):
        payload = _daemon_call(args, "snapshots.update", params)
    else:
        payload = service.snapshots_update(
            args.domain,
            args.layer,
            label=args.label,
            description=args.description,
            uri=args.uri,
        )
    _emit(
        payload,
        args.json,
        f"Updated snapshot {args.layer} on {args.domain}.",
    )
    return 0


def _cmd_snapshot_revert(args: argparse.Namespace) -> int:
    if _daemon_enabled(args):
        payload = _daemon_call(
            args,
            "snapshots.revert",
            {"domain": args.domain, "layer": args.layer, "uri": args.uri},
        )
    else:
        payload = service.snapshots_revert(
            args.domain, args.layer, args.uri
        )
    message = f"Active layer is now {payload.get('active')} on {args.domain}."
    ejected = payload.get("ejected") or []
    if ejected:
        message += f" Ejected CD/DVD: {', '.join(str(item) for item in ejected)}."
    _emit(payload, args.json, message)
    return 0


def _cmd_snapshot_delete(args: argparse.Namespace) -> int:
    if _daemon_enabled(args):
        payload = _daemon_call(
            args,
            "snapshots.delete",
            {
                "domain": args.domain,
                "layer": args.layer,
                "force": args.force,
                "uri": args.uri,
            },
        )
    else:
        payload = service.snapshots_delete(
            args.domain, args.layer, args.uri, force=args.force
        )
    _emit(
        payload,
        args.json,
        f"Deleted snapshot {args.layer} from {args.domain}.",
    )
    return 0


def _cmd_network_status(args: argparse.Namespace) -> int:
    payload = _topology(args)
    if args.json:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return 0
    networks = payload.get("networks", [])
    if not networks:
        print("No network profiles.")
        return 0
    guests: dict[str, int] = {}
    for vm in payload.get("vms", []):
        for interface in vm.get("interfaces") or []:
            name = str(interface.get("network", ""))
            guests[name] = guests.get(name, 0) + 1
    networks = list(networks)
    known = {str(item.get("name", "")) for item in networks}
    for vm in payload.get("vms", []):
        for interface in vm.get("interfaces") or []:
            name = str(interface.get("network", ""))
            if name and name not in known:
                networks.append({"name": name, "mode": "external"})
                known.add(name)
    rows = []
    for network in networks:
        name = str(network.get("name", ""))
        runtime = network.get("runtime") or {}
        rows.append(
            [
                name,
                str(network.get("mode", "")),
                str(network.get("subnet", "")),
                "yes" if runtime.get("active") else "no",
                str(network.get("bridge", "")),
                str(len(network.get("port_forwards") or [])),
                str(guests.get(name, 0)),
            ]
        )
    _print_table(
        ["NETWORK", "MODE", "SUBNET", "ACTIVE", "BRIDGE", "FORWARDS", "GUESTS"],
        rows,
    )
    return 0


def _cmd_export_inventory(args: argparse.Namespace) -> int:
    from .yamlmini import dumps as yaml_dumps

    topology = _topology(args)
    children: dict[str, dict[str, Any]] = {}
    for network in topology.get("networks", []):
        name = str(network.get("name", ""))
        if name:
            children.setdefault(name, {"hosts": {}})
    placed: set[str] = set()
    for vm in topology.get("vms", []):
        name = str(vm.get("name", ""))
        for interface in vm.get("interfaces") or []:
            group = str(interface.get("network") or "unassigned")
            host = children.setdefault(group, {"hosts": {}})["hosts"].setdefault(
                name, {"ansible_connection": "ssh"}
            )
            ips = interface.get("ips") or []
            if ips and "ansible_host" not in host:
                host["ansible_host"] = str(ips[0])
            placed.add(name)
        if name not in placed:
            children.setdefault("unassigned", {"hosts": {}})["hosts"][name] = {
                "ansible_connection": "ssh"
            }
    if args.ansible_user:
        for group in children.values():
            for host in group["hosts"].values():
                host["ansible_user"] = args.ansible_user
    children = {
        group: data
        for group, data in children.items()
        if data.get("hosts")
    }
    inventory = {"all": {"children": children}}
    text = (
        json.dumps(inventory, indent=2, ensure_ascii=False)
        if args.json
        else yaml_dumps(inventory)
    )
    if args.dest:
        dest = Path(args.dest).expanduser()
        if dest.is_dir():
            dest = dest / "inventory.yml"
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(text, encoding="utf-8")
        print(f"Wrote Ansible inventory to {dest}")
    else:
        print(text, end="" if text.endswith("\n") else "\n")
    return 0


def _domain_names(args: argparse.Namespace) -> list[str]:
    if _daemon_enabled(args):
        return [
            str(item["name"])
            for item in _daemon_call(args, "domains.list", {"uri": args.uri})
        ]
    with LibvirtBackend(args.uri) as backend:
        return [domain.name for domain in backend.list_domains()]


def _domain_xml(args: argparse.Namespace, domain: str) -> str:
    if _daemon_enabled(args):
        payload = _daemon_call(
            args, "vm.xml", {"domain": domain, "uri": args.uri}
        )
    else:
        payload = service.vm_xml(domain, args.uri)
    return str(payload["xml"])


def _cmd_export_xml(args: argparse.Namespace) -> int:
    if args.domain:
        xml = _domain_xml(args, args.domain)
        if args.dest:
            dest = Path(args.dest).expanduser()
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(xml, encoding="utf-8")
            print(f"Wrote libvirt XML to {dest}")
        elif args.json:
            print(
                json.dumps(
                    {"domain": args.domain, "xml": xml},
                    indent=2,
                    ensure_ascii=False,
                )
            )
        else:
            print(xml, end="" if xml.endswith("\n") else "\n")
        return 0
    if not args.dest:
        raise ValidationError(
            "--dest directory is required when exporting every domain."
        )
    dest = Path(args.dest).expanduser()
    dest.mkdir(parents=True, exist_ok=True)
    written = []
    for name in _domain_names(args):
        path = dest / f"{name}.xml"
        path.write_text(_domain_xml(args, name), encoding="utf-8")
        written.append(str(path))
    if args.json:
        print(json.dumps({"files": written}, indent=2))
    else:
        for path in written:
            print(f"Wrote {path}")
    return 0


def _cmd_export(args: argparse.Namespace) -> int:
    modes = int(args.ansible_inventory) + int(args.libvirt_xml)
    if modes != 1:
        raise ValidationError(
            "Choose exactly one of --ansible-inventory or --libvirt-xml."
        )
    if args.domain and not args.libvirt_xml:
        raise ValidationError("--domain is only used with --libvirt-xml.")
    if args.ansible_inventory:
        return _cmd_export_inventory(args)
    return _cmd_export_xml(args)


def _cmd_gc(args: argparse.Namespace) -> int:
    from .chain import find_orphan_layers

    root = Path(args.root).expanduser().resolve()
    orphans = find_orphan_layers(root)
    deleted: list[str] = []
    for path in orphans:
        if args.force:
            path.unlink(missing_ok=True)
            deleted.append(str(path))
    payload = {
        "root": str(root),
        "orphans": [str(path) for path in orphans],
        "deleted": deleted,
        "dry_run": not args.force,
    }
    if args.json:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return 0
    if not orphans:
        print("No orphan chain layers found.")
        return 0
    for path in orphans:
        print(f"{'Deleted' if args.force else 'Orphan'}: {path}")
    if not args.force:
        print("Dry run: re-run with --force to delete these files.")
    return 0


def _cmd_lab_status(args: argparse.Namespace) -> int:
    params = {
        "path": args.path,
        "uri": args.uri,
        "network_config": args.network_config,
    }
    if _daemon_enabled(args):
        result = _daemon_call(args, "lab.status", params)
    else:
        result = lab.lab_status(
            args.path,
            uri=args.uri,
            network_config=args.network_config,
        )
    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0
    print(f"Lab: {result['lab']}")
    _print_table(
        ["NETWORK", "DEFINED", "ACTIVE"],
        [
            [
                item["name"],
                "yes" if item["defined"] else "no",
                "yes" if item["active"] else "no",
            ]
            for item in result["networks"]
        ],
    )
    _print_table(
        ["VM", "STATE", "NETWORKS", "TEMPLATE"],
        [
            [
                item["name"],
                item["state"],
                ", ".join(item["networks"]),
                item["template"],
            ]
            for item in result["vms"]
        ],
    )
    return 0


def _cmd_config_serial_add(args: argparse.Namespace) -> int:
    params = {"domain": args.domain, "uri": args.uri}
    if _daemon_enabled(args):
        payload = _daemon_call(args, "vm.serial_add", params)
    else:
        payload = service.vm_serial_add(args.domain, uri=args.uri)
    if args.json:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        state = "added" if payload.get("changed") else "already present"
        print(f"Serial console {state} for {args.domain}")
    return 0


def _cmd_config_tpm_add(args: argparse.Namespace) -> int:
    params = {
        "domain": args.domain,
        "version": args.version,
        "uri": args.uri,
    }
    if _daemon_enabled(args):
        payload = _daemon_call(args, "vm.tpm_add", params)
    else:
        payload = service.vm_tpm_add(
            args.domain, version=args.version, uri=args.uri
        )
    if args.json:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        print(f"Added TPM {args.version} to {args.domain}")
        if payload["pending_restart"]:
            print("Note: restart the VM to apply the change.")
    return 0


def _cmd_config_tpm_remove(args: argparse.Namespace) -> int:
    if _daemon_enabled(args):
        payload = _daemon_call(
            args, "vm.tpm_remove", {"domain": args.domain, "uri": args.uri}
        )
    else:
        payload = service.vm_tpm_remove(args.domain, uri=args.uri)
    if args.json:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        print(f"Removed TPM from {args.domain}")
    return 0


def _cmd_config_usb_add(args: argparse.Namespace) -> int:
    if _daemon_enabled(args):
        payload = _daemon_call(
            args, "vm.usb_add", {"domain": args.domain, "uri": args.uri}
        )
    else:
        payload = service.vm_usb_add(args.domain, uri=args.uri)
    if args.json:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        print(f"Added USB xHCI controller to {args.domain}")
    return 0


def _cmd_config_audio_add(args: argparse.Namespace) -> int:
    if _daemon_enabled(args):
        payload = _daemon_call(
            args, "vm.audio_add", {"domain": args.domain, "uri": args.uri}
        )
    else:
        payload = service.vm_audio_add(args.domain, uri=args.uri)
    if args.json:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        print(f"Added audio device to {args.domain}")
        if payload["pending_restart"]:
            print("Note: restart the VM to apply the change.")
    return 0


def _cmd_config_audio_remove(args: argparse.Namespace) -> int:
    if _daemon_enabled(args):
        payload = _daemon_call(
            args, "vm.audio_remove", {"domain": args.domain, "uri": args.uri}
        )
    else:
        payload = service.vm_audio_remove(args.domain, uri=args.uri)
    if args.json:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        print(f"Removed audio device from {args.domain}")
    return 0


def _cmd_config_disk_add(args: argparse.Namespace) -> int:
    params = {
        "domain": args.domain,
        "path": args.path,
        "size_gb": args.size,
        "bus": args.disk_bus,
        "format": args.format,
        "create_file": not args.no_create,
        "uri": args.uri,
    }
    if _daemon_enabled(args):
        payload = _daemon_call(args, "vm.disk_add", params)
    else:
        payload = service.vm_disk_add(
            args.domain,
            args.path,
            size_gb=args.size,
            bus=args.disk_bus,
            format=args.format,
            create_file=not args.no_create,
            uri=args.uri,
        )
    if args.json:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        disk = payload["disk"]
        print(
            f"Added {disk['source']} as {disk['target']} "
            f"({disk['bus']}, {disk['format']})"
        )
        if payload["pending_restart"]:
            print("Note: restart the VM to apply the new disk.")
    return 0


def _cmd_config_disk_remove(args: argparse.Namespace) -> int:
    params = {
        "domain": args.domain,
        "source": args.source,
        "delete_file": args.delete,
        "uri": args.uri,
    }
    if _daemon_enabled(args):
        payload = _daemon_call(args, "vm.disk_remove", params)
    else:
        payload = service.vm_disk_remove(
            args.domain,
            args.source,
            delete_file=args.delete,
            uri=args.uri,
        )
    if args.json:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        print(f"Removed disk {args.source}")
        if payload["file_deleted"]:
            print("Disk file deleted.")
        if payload["pending_restart"]:
            print("Note: restart the VM to apply the change.")
    return 0


def _cmd_config_disk_resize(args: argparse.Namespace) -> int:
    params = {
        "domain": args.domain,
        "source": args.source,
        "size_gb": args.size,
        "uri": args.uri,
    }
    if _daemon_enabled(args):
        payload = _daemon_call(args, "vm.disk_resize", params)
    else:
        payload = service.vm_disk_resize(
            args.domain, args.source, args.size, uri=args.uri
        )
    if args.json:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return 0
    if not payload.get("changed"):
        print(f"Disk is already {payload['new_size_gb']} GB.")
        return 0
    print(
        f"Resized {payload['disk']['target']} "
        f"{payload['old_size_gb']} GB → {payload['new_size_gb']} GB"
    )
    if payload.get("live"):
        print(
            "The block device was resized live; grow the partition and "
            "filesystem inside the guest."
        )
    else:
        print(
            "Start the VM, then grow the partition and filesystem inside "
            "the guest."
        )
    return 0


def _cmd_config_caps(args: argparse.Namespace) -> int:
    params = {
        "machine": args.machine,
        "arch": args.arch,
        "uri": args.uri,
    }
    if _daemon_enabled(args):
        payload = _daemon_call(args, "vm.capabilities", params)
    else:
        payload = service.vm_capabilities(
            args.machine, args.uri, arch=args.arch
        )
    if args.json:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return 0
    for key, values in payload.items():
        print(f"{key}:")
        if key == "arch":
            print(f"  {values}")
        elif key == "arches":
            for value in values:
                print(
                    f"  {value.get('name')} "
                    f"({', '.join(value.get('domain_types') or [])})"
                )
        else:
            for value in values:
                print(f"  {value}")
    return 0


def _cmd_media_list(args: argparse.Namespace) -> int:
    params = {"domain": args.domain, "uri": args.uri}
    if _daemon_enabled(args):
        payload = _daemon_call(args, "vm.media_list", params)
    else:
        payload = service.vm_media_list(args.domain, args.uri)
    if args.json:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return 0
    cdroms = payload.get("cdroms", [])
    if not cdroms:
        print("No CD/DVD drive.")
        return 0
    for cdrom in cdroms:
        source = cdrom.get("source") or "(empty)"
        print(f"{cdrom.get('target')} ({cdrom.get('bus')}): {source}")
    return 0


def _cmd_media_change(args: argparse.Namespace) -> int:
    params = {
        "domain": args.domain,
        "path": args.path,
        "target": args.target,
        "force": args.force,
        "uri": args.uri,
    }
    if _daemon_enabled(args):
        payload = _daemon_call(args, "vm.media_change", params)
    else:
        payload = service.vm_media_change(
            args.domain,
            args.path,
            target=args.target,
            force=args.force,
            uri=args.uri,
        )
    if args.json:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return 0
    print(f"Changed {payload.get('target')} to {payload['media']}")
    if payload.get("pending_restart"):
        print("Note: start the VM to use the new media.")
    return 0


def _cmd_media_eject(args: argparse.Namespace) -> int:
    params = {
        "domain": args.domain,
        "target": args.target,
        "force": args.force,
        "uri": args.uri,
    }
    if _daemon_enabled(args):
        payload = _daemon_call(args, "vm.media_eject", params)
    else:
        payload = service.vm_media_eject(
            args.domain, target=args.target, force=args.force, uri=args.uri
        )
    if args.json:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return 0
    print(
        f"Ejected media from {payload.get('target')}"
        + (" (forced)" if payload.get("force") else "")
    )
    if payload.get("pending_restart"):
        print("Note: start the VM to apply the change.")
    return 0


def _cmd_config_drift(args: argparse.Namespace) -> int:
    params = {"domain": args.domain, "uri": args.uri}
    if _daemon_enabled(args):
        payload = _daemon_call(args, "domain.drift", params)
    else:
        payload = service.domain_drift(args.domain, uri=args.uri)
    if args.json:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        print(f"Domain: {payload['domain']}")
        print(f"Managed by: {payload.get('managed_by')}")
        print(f"Drift: {payload['status']}")
        print(f"Baseline: {payload.get('baseline') or '-'}")
        print(f"Current: {payload.get('current')}")
        changes = payload.get("changes") or []
        if changes:
            print("Changes:")
            for change in changes:
                print(f"  - {change}")
    return 0


def _cmd_config_baseline(args: argparse.Namespace) -> int:
    if not args.yes:
        raise ValidationError(
            "Refreshing the baseline records the current XML. Re-run with "
            "--yes after reviewing the configuration."
        )
    params = {"domain": args.domain, "uri": args.uri}
    if _daemon_enabled(args):
        payload = _daemon_call(args, "domain.baseline_refresh", params)
    else:
        payload = service.domain_baseline_refresh(
            args.domain, uri=args.uri
        )
    if args.json:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        print("Baseline refreshed: " + ", ".join(payload["refreshed"]))
    return 0


def _cmd_config_show(args: argparse.Namespace) -> int:
    if _daemon_enabled(args):
        payload = _daemon_call(
            args, "vm.config_info", {"domain": args.domain}
        )
    else:
        payload = service.vm_config_info(args.domain)
    if args.json:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return 0
    config = payload["config"]
    print(f"Domain: {config['name']}")
    print(f"State: {payload['state']}")
    print(
        f"Memory: {config['memory_mib']} MiB "
        f"(max {config.get('memory_max_mib', config['memory_mib'])} MiB)"
    )
    print(f"vCPUs: {config['vcpus']}")
    print(f"CPU mode: {config['cpu_mode']}")
    print(f"Machine: {config['machine']} ({config['firmware']})")
    print(f"Video: {config['video']}")
    _print_table(
        ["DEVICE", "BUS", "TARGET", "FORMAT", "SIZE", "SOURCE"],
        [
            [
                disk["device"],
                disk["bus"],
                disk["target"],
                disk["format"],
                (
                    f"{disk['size_gb']} GB"
                    if disk.get("size_gb") is not None
                    else ""
                ),
                disk["source"],
            ]
            for disk in config["disks"]
        ],
    )
    _print_table(
        ["MAC", "NETWORK", "MODEL"],
        [
            [nic["mac"], nic["network"], nic["model"]]
            for nic in config["nics"]
        ],
    )
    _print_table(
        ["CONTROLLER", "INDEX", "MODEL", "SERIAL"],
        [
            [
                controller.get("type", ""),
                controller.get("index", ""),
                controller.get("model", ""),
                controller.get("serial", ""),
            ]
            for controller in config.get("controllers", [])
        ],
    )
    _print_table(
        ["DEVICE", "DETAILS"],
        [
            [device.get("type", ""), device.get("detail", "")]
            for device in config.get("other_devices", [])
        ],
    )
    return 0


def _cmd_config_profile(args: argparse.Namespace) -> int:
    if args.profile:
        if _daemon_enabled(args):
            payload = _daemon_call(
                args, "profile.set", {"profile": args.profile}
            )
        else:
            payload = service.runtime_profile_set(
                args.profile, uri=args.uri
            )
    elif _daemon_enabled(args):
        payload = _daemon_call(args, "profile.get", {})
    else:
        payload = service.runtime_profile_get()
    if args.json:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        print(
            f"Resource profile: {payload.get('label', payload['profile'])} "
            f"({payload['profile']})"
        )
        print(
            "Best effort: CPU/IO weight only; no CPU, thermal or power cap."
        )
        if payload.get("description"):
            print(payload["description"])
        applied = payload.get("applied") or []
        if applied:
            print("Applied to: " + ", ".join(applied))
        for warning in payload.get("warnings") or []:
            print(f"Warning: {warning}")
    return 0


def _cmd_config_set(args: argparse.Namespace) -> int:
    params = {
        "domain": args.domain,
        "memory_mib": args.memory,
        "vcpus": args.vcpus,
        "vcpus_max": args.vcpus_max,
        "cpu_mode": args.cpu_mode,
        "machine": args.machine,
        "video": args.video,
        "disk_bus": args.disk_bus,
        "nic_model": args.nic_model,
        "os_type": args.os_type,
    }
    if _daemon_enabled(args):
        payload = _daemon_call(args, "vm.config_update", params)
    else:
        payload = service.vm_config_update(
            args.domain,
            memory_mib=args.memory,
            vcpus=args.vcpus,
            vcpus_max=args.vcpus_max,
            cpu_mode=args.cpu_mode,
            machine=args.machine,
            video=args.video,
            disk_bus=args.disk_bus,
            nic_model=args.nic_model,
            os_type=args.os_type,
        )
    if args.json:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return 0
    if payload["changes"]:
        for change in payload["changes"]:
            print(f"  {change}")
    else:
        print("No hardware changes.")
    if payload.get("live_memory"):
        print(
            "Memory was adjusted live through the balloon device; the guest "
            "needs the balloon driver to release memory."
        )
    for warning in payload.get("warnings") or []:
        print(f"  warning: {warning}")
    if payload["pending_restart"]:
        print(
            "Note: the VM is running. Changes are in the persistent definition "
            "and apply after a restart."
        )
    return 0


def _cmd_autostart(args: argparse.Namespace) -> int:
    enabled = not args.off
    params = {
        "domain": args.domain,
        "enabled": enabled,
        "uri": args.uri,
    }
    if _daemon_enabled(args):
        payload = _daemon_call(args, "domain.autostart", params)
    else:
        payload = service.domain_set_autostart(
            args.domain, enabled, args.uri
        )
    if args.json:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return 0
    state = "enabled" if payload.get("autostart") else "disabled"
    print(f"Autostart {state} for {payload['name']}")
    return 0


def _cmd_delete_vm(args: argparse.Namespace) -> int:
    params = {
        "domain": args.domain,
        "delete_storage": args.delete_storage,
        "force": args.force,
        "uri": args.uri,
    }
    if _daemon_enabled(args):
        payload = _daemon_call(args, "domain.delete", params)
    else:
        payload = service.domain_delete(
            args.domain,
            delete_storage=args.delete_storage,
            force=args.force,
            uri=args.uri,
        )
    if args.json:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return 0
    print(f"Deleted VM {args.domain}")
    for path in payload.get("storage_deleted", []):
        print(f"  removed storage: {path}")
    for warning in payload.get("warnings", []):
        print(f"  warning: {warning}")
    return 0


def _cmd_stats(args: argparse.Namespace) -> int:
    params = {"domain": args.domain, "uri": args.uri}
    if _daemon_enabled(args):
        payload = _daemon_call(args, "domain.stats", params)
    else:
        payload = service.domain_stats(args.domain, args.uri)
    if args.json:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return 0
    print(f"{payload['name']}: {payload['state']}")
    print(f"  vCPUs: {payload.get('vcpus', 0)}")
    print(f"  CPU time: {payload.get('cpu_time_ns', 0) / 1e9:.1f}s")
    if payload.get("memory_actual_mib"):
        print(
            f"  RAM: {payload.get('memory_rss_mib', 0)}/"
            f"{payload.get('memory_actual_mib')} MiB (RSS/configured)"
        )
    return 0


def _cmd_lab_export(args: argparse.Namespace) -> int:
    params = {
        "path": args.lab,
        "dest": args.dest,
        "with_templates": args.with_templates,
    }
    if _daemon_enabled(args):
        result = _daemon_call(args, "lab.export", params)
    else:
        result = lab.lab_export(
            args.lab, args.dest, with_templates=args.with_templates
        )
    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        print(
            f"Exported lab {result['lab']} → {result['bundle']} "
            f"({result['size']} bytes, with_templates={result['with_templates']})"
        )
        for warning in result["warnings"]:
            print(f"  warning: {warning}")
    return 0


def _cmd_lab_import(args: argparse.Namespace) -> int:
    params = {
        "bundle": args.bundle,
        "dest": args.dest,
        "templates_root": args.templates_root,
        "overwrite": args.overwrite,
    }
    if _daemon_enabled(args):
        result = _daemon_call(args, "lab.import", params)
    else:
        result = lab.lab_import(
            args.bundle,
            args.dest,
            templates_root=args.templates_root,
            overwrite=args.overwrite,
        )
    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        print(f"Imported lab {result['lab']} → {result['destination']}")
        print(f"  lab.yaml: {result['lab_file']}")
        print(f"  templates: {result['templates_root']}")
        print(
            f"  templates imported: {result['templates_imported']}/"
            f"{result['templates_required']}"
        )
    return 0


def _cmd_lab_reset(args: argparse.Namespace) -> int:
    params = {
        "path": args.lab,
        "vms": args.vm,
        "force": args.force,
        "start": not args.no_start,
        "uri": args.uri,
        "network_config": args.network_config,
    }
    if _daemon_enabled(args):
        result = _daemon_call(args, "lab.reset", params)
    else:
        result = lab.lab_reset(
            args.lab,
            args.vm,
            force=args.force,
            start=not args.no_start,
            uri=args.uri,
            network_config=args.network_config,
        )
    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0
    print(f"Reset {', '.join(result['vms'])} in {result['lab']}")
    for action in result["actions"]:
        print(f"  {action}")
    for warning in result["warnings"]:
        print(f"  warning: {warning}")
    return 0


def _cmd_lab_destroy(args: argparse.Namespace) -> int:
    params = {
        "path": args.lab,
        "force": args.force,
        "keep_networks": args.keep_networks,
        "uri": args.uri,
        "network_config": args.network_config,
    }
    if _daemon_enabled(args):
        result = _daemon_call(args, "lab.destroy", params)
    else:
        result = lab.lab_destroy(
            args.lab,
            force=args.force,
            keep_networks=args.keep_networks,
            uri=args.uri,
            network_config=args.network_config,
        )
    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0
    print(f"Destroyed lab {result['lab']}")
    for action in result["actions"]:
        print(f"  {action}")
    for warning in result["warnings"]:
        print(f"  warning: {warning}")
    print("Templates and lab.yaml were kept.")
    return 0


def _cmd_create(args: argparse.Namespace) -> int:
    params = {
        "name": args.name,
        "iso": args.iso,
        "dest": args.dest,
        "disk_size_gb": args.disk_size,
        "memory": args.memory,
        "vcpus": args.vcpus,
        "network": args.network,
        "network_config": args.network_config,
        "os_type": args.os,
        "os_variant": args.os_variant,
        "arch": args.arch,
        "windows_unattend": False if args.no_unattend else None,
        "edition": args.edition,
        "admin_password": args.admin_password,
        "computer_name": args.computer_name,
        "virtio_iso": args.virtio_iso,
        "firmware": args.firmware,
        "machine": args.machine,
        "disk_bus": args.disk_bus,
        "iso_bus": args.iso_bus,
        "nic_model": args.nic_model,
        "video": args.video,
        "video_memory_mib": args.video_memory,
        "hyperv": args.hyperv,
        "cpu_mode": args.cpu_mode,
        "tpm": args.tpm,
        "secure_boot": args.secure_boot,
        "start": args.start,
        "uri": args.uri,
    }
    if _daemon_enabled(args):
        result = _daemon_call(args, "vm.create", params)
    else:
        result = service.vm_create_from_iso(
            args.name,
            args.iso,
            args.dest,
            disk_size_gb=args.disk_size,
            memory_mib=args.memory,
            vcpus=args.vcpus,
            network=args.network,
            network_config=args.network_config,
            os_type=args.os,
            os_variant=args.os_variant,
            arch=args.arch,
            windows_unattend=False if args.no_unattend else None,
            windows_edition=args.edition,
            admin_password=args.admin_password,
            computer_name=args.computer_name,
            virtio_iso=args.virtio_iso,
            firmware=args.firmware,
            machine=args.machine,
            disk_bus=args.disk_bus,
            iso_bus=args.iso_bus,
            nic_model=args.nic_model,
            video=args.video,
            video_memory_mib=args.video_memory,
            hyperv=args.hyperv,
            cpu_mode=args.cpu_mode,
            tpm=args.tpm,
            secure_boot=args.secure_boot,
            start=args.start,
            uri=args.uri,
        )
    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0
    print(f"Created {result['name']}: {result['disk']}")
    print(f"  install ISO: {result['install_iso']}")
    print(
        f"  guest OS: {result.get('os_type')} / {result.get('os_variant')} "
        f"({result.get('arch')}, unattended={result.get('windows_unattend')})"
    )
    if result.get("unattend_iso"):
        print(f"  unattend ISO: {result['unattend_iso']}")
    if result.get("unattend_floppy"):
        print(f"  answer media: {result['unattend_floppy']}")
    print(f"  defined={result['defined']} started={result['started']}")
    if result.get("admin_password"):
        print(
            f"  Windows edition: {result.get('windows_edition_label')} "
            f"(index {result.get('windows_image_index')})"
        )
        print(
            "  Administrator password: "
            f"{result['admin_password']} (change it after install)"
        )
    return 0


def _cmd_snapshot(args: argparse.Namespace) -> int:
    if not args.chain:
        raise ValidationError(
            "Use 'vemu snapshot create/list/revert/delete --domain NAME', "
            "or 'vemu snapshot --chain CHAIN' for the legacy chain form."
        )
    service.require_chain_writable(args.chain, getattr(args, "uri", "qemu:///system"))
    if _daemon_enabled(args):
        layer = _daemon_call(
            args, "chain.snapshot", {"chain": args.chain, "label": args.label}
        )
    else:
        layer = ChainManager(args.chain).snapshot(args.label).to_dict()
    _emit(
        {"id": layer["id"], "path": layer["path"], "label": layer["label"]},
        args.json,
        f"Created snapshot {layer['id']} ({layer['label'] or 'no label'}).",
    )
    return 0


def _cmd_revert(args: argparse.Namespace) -> int:
    service.require_chain_writable(args.chain, getattr(args, "uri", "qemu:///system"))
    if _daemon_enabled(args):
        layer = _daemon_call(
            args, "chain.revert", {"chain": args.chain, "layer": args.layer}
        )
    else:
        layer = ChainManager(args.chain).revert(args.layer).to_dict()
    _emit(
        {"active": layer["id"], "path": layer["path"]},
        args.json,
        f"Active layer is now {layer['id']}.",
    )
    return 0


def _cmd_delete(args: argparse.Namespace) -> int:
    service.require_chain_writable(args.chain, getattr(args, "uri", "qemu:///system"))
    if _daemon_enabled(args):
        _daemon_call(
            args,
            "chain.delete",
            {"chain": args.chain, "layer": args.layer, "force": args.force},
        )
    else:
        ChainManager(args.chain).delete(args.layer, force=args.force)
    _emit(
        {"deleted": args.layer},
        args.json,
        f"Deleted layer {args.layer}.",
    )
    return 0


def _cmd_verify(args: argparse.Namespace) -> int:
    if _daemon_enabled(args):
        result = _daemon_call(args, "chain.verify", {"chain": args.chain})
    else:
        issues = ChainManager(args.chain).verify()
        result = {"ok": not issues, "issues": issues}
    if args.json:
        print(json.dumps(result, indent=2))
    elif result["issues"]:
        print("Chain has issues:")
        for issue in result["issues"]:
            print(f"  - {issue}")
    else:
        print("Chain is healthy.")
    return 0 if result["ok"] else 1


def _cmd_show(args: argparse.Namespace) -> int:
    if _daemon_enabled(args):
        info = _daemon_call(args, "chain.show", {"chain": args.chain})
    else:
        info = ChainManager(args.chain).info()
    _print_chain_info(info, args.json)
    return 0


def _cmd_identity(args: argparse.Namespace) -> int:
    service.require_chain_writable(args.chain, getattr(args, "uri", "qemu:///system"))
    if _daemon_enabled(args):
        payload = _daemon_call(
            args,
            "identity.reset",
            {
                "chain": args.chain,
                "hostname": args.hostname,
                "machine_id": args.machine_id,
                "dry_run": args.dry_run,
            },
        )
    else:
        payload = service.identity_reset(
            args.chain,
            args.hostname,
            machine_id=args.machine_id,
            dry_run=args.dry_run,
        )
    if args.dry_run:
        text = "Dry run. Command:\n" + " ".join(payload["command"])
    else:
        text = (
            f"Reset identity on {payload['disk']}: hostname={payload['hostname']}, "
            f"machine-id={payload['machine_id']}"
        )
    _emit(payload, args.json, text)
    return 0


def _cmd_adopt(args: argparse.Namespace) -> int:
    if args.mode == "partial" and not args.yes:
        raise ValidationError(
            "Partial adoption changes the domain metadata; pass --yes."
        )
    params = {
        "domain": args.domain,
        "mode": args.mode,
        "backup_dir": args.backup_dir,
        "uri": args.uri,
    }
    if _daemon_enabled(args):
        payload = _daemon_call(args, "vm.adopt", params)
    else:
        payload = service.adopt_domain(
            args.domain,
            args.mode,
            backup_dir=args.backup_dir,
            uri=args.uri,
        )
    if args.json:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    elif payload.get("changed"):
        print(
            f"Adopted {args.domain} as {payload['managed_by']} "
            f"(backup: {payload.get('backup')})."
        )
    else:
        print(f"{args.domain} remains read-only.")
    return 0


def _cmd_rescue(args: argparse.Namespace) -> int:
    if not args.yes:
        raise ValidationError("Rescue start/shutdown requires --yes.")
    if args.stop:
        if _daemon_enabled(args):
            payload = _daemon_call(
                args,
                "vm.rescue_shutdown",
                {"domain": args.domain, "force": args.force},
            )
        else:
            payload = service.domain_rescue_shutdown(
                args.domain, uri=args.uri, force=args.force
            )
        if args.json:
            print(json.dumps(payload, indent=2, ensure_ascii=False))
        else:
            print(f"Shutdown requested for {args.domain}.")
        return 0
    params = {
        "domain": args.domain,
        "backup_dir": args.backup_dir,
        "uri": args.uri,
    }
    if _daemon_enabled(args):
        payload = _daemon_call(args, "vm.rescue_start", params)
    else:
        payload = service.domain_rescue_start(
            args.domain,
            backup_dir=args.backup_dir,
            uri=args.uri,
        )
    if args.json:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        print(
            f"Started {args.domain} for rescue; XML backup: "
            f"{payload.get('backup')}."
        )
    return 0


def _cmd_migrate(args: argparse.Namespace) -> int:
    if args.mode != "full":
        raise ValidationError(
            "Only 'full' migration is implemented; use --mode full."
        )
    if not args.dry_run and not args.yes:
        raise ValidationError(
            "Full migration rewrites the disk layout. Re-run with --yes "
            "after reviewing --dry-run."
        )
    params = {
        "domain": args.domain,
        "new_firmware": args.new_firmware,
        "import_snapshots": args.import_snapshots,
        "storage_config": args.storage_config,
        "uri": args.uri,
    }
    if _daemon_enabled(args):
        if args.dry_run:
            payload = _daemon_call(args, "vm.migration_plan", params)
        else:
            payload = _daemon_call(
                args,
                "vm.migrate_full",
                {
                    **params,
                    "accept_snapshot_loss": args.accept_snapshot_loss,
                    "start": args.start,
                    "backup_dir": args.backup_dir,
                },
            )
    else:
        if args.dry_run:
            payload = service.vm_migration_plan(
                args.domain,
                new_firmware=args.new_firmware,
                storage_config=args.storage_config,
                import_snapshots=args.import_snapshots,
                uri=args.uri,
            )
        else:
            payload = service.vm_migrate_full(
                args.domain,
                accept_snapshot_loss=args.accept_snapshot_loss,
                import_snapshots=args.import_snapshots,
                start=args.start,
                new_firmware=args.new_firmware,
                storage_config=args.storage_config,
                backup_dir=args.backup_dir,
                uri=args.uri,
            )
    if args.json:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return 0
    if args.dry_run:
        print(f"Full migration plan for {args.domain}")
        print(f"  State:        {payload.get('state')}")
        print(f"  Managed by:   {payload.get('managed_by')}")
        print(f"  Data disks:   {payload.get('data_disk_count')}")
        snapshots = ", ".join(payload.get("snapshots") or []) or "none"
        print(f"  Snapshots:    {snapshots}")
        print(f"  Estimate:     {payload.get('estimated_human')}")
        print(f"  Headroom:     {_human(payload.get('headroom_bytes', 0))}")
        print(f"  Required:     {payload.get('required_human')}")
        print(f"  Available:    {payload.get('available_human')}")
        print(f"  Destination:  {payload.get('destination') or '-'}")
        if payload.get("new_firmware"):
            firmware = payload.get("firmware") or {}
            print(
                "  New firmware: "
                f"{firmware.get('loader', '-')} "
                f"(secure={firmware.get('secure_boot')})"
            )
            print(f"  New NVRAM:    {firmware.get('nvram_path', '-')}")
        for disk in payload.get("disks") or []:
            print(
                f"  Disk {disk.get('target')}:     "
                f"{disk.get('required_human')}  "
                f"({disk.get('source')})"
            )
        for reason in payload.get("reasons") or []:
            print(f"  Blocked:      {reason}")
        print(
            "  Result:       "
            + ("ready" if payload.get("supported") else "not ready")
        )
        import_plan = payload.get("snapshot_import") or {}
        if args.import_snapshots and payload.get("snapshots"):
            if import_plan.get("supported"):
                print(
                    "  Snapshots:    import "
                    f"{len(payload.get('snapshots') or [])} snapshot(s) "
                    f"into {import_plan.get('layer_count', 0)} layers"
                )
            else:
                print(
                    "  Snapshots:    import unavailable — "
                    f"{import_plan.get('reason', 'unsupported layout')}"
                )
        if payload.get("snapshot_loss_required"):
            print(
                "  Warning:      external snapshots will not be imported; "
                "current state will be flattened."
            )
        return 0
    disks = payload.get("disks") or []
    imported = payload.get("imported_snapshots") or []
    if imported:
        print(
            f"Imported {len(imported)} libvirt snapshot(s): "
            + ", ".join(str(item) for item in imported)
        )
    if len(disks) > 1:
        print(f"Migrated {args.domain} to v-EMU full ({len(disks)} data disks):")
        for disk in disks:
            print(f"  {disk.get('target')}: {disk.get('active_disk')}")
    else:
        print(
            f"Migrated {args.domain} to v-EMU full: "
            f"{payload.get('active_disk')}"
        )
    print(f"Backup XML: {payload['backup']}")
    if payload.get("firmware_applied"):
        print(f"New firmware: {payload.get('firmware', {}).get('loader')}")
    print("Old disks were kept. Remove them only after verifying the VM.")
    return 0


def _cmd_import(args: argparse.Namespace) -> int:
    if not args.dry_run and not args.yes:
        raise ValidationError(
            "Import creates a new VM and copies/convert disk data. "
            "Re-run with --yes after reviewing --dry-run."
        )
    nics = None
    if args.nic:
        nics = []
        for value in args.nic:
            parts = value.split(":", 2)
            nics.append(
                {
                    "network": parts[0],
                    "model": parts[1] if len(parts) > 1 and parts[1] else (args.nic_model or "virtio"),
                    "mac": parts[2] if len(parts) > 2 and parts[2] else None,
                }
            )
    params = {
        "kind": args.import_kind,
        "source": args.source,
        "name": args.name,
        "preset": args.preset,
        "network": args.network,
        "vcpus": args.vcpus,
        "memory_mib": args.memory_mib,
        "firmware": args.firmware,
        "machine": args.machine,
        "disk_bus": args.disk_bus,
        "nic_model": args.nic_model,
        "video": args.video,
        "serial_console": args.serial_console,
        "data_disk_gb": args.data_disk_gb,
        "nics": nics,
        "start": args.start,
        "storage_config": args.storage_config,
        "network_config": args.network_config,
        "uri": args.uri,
    }
    if _daemon_enabled(args):
        method = "vm.import_plan" if args.dry_run else "vm.import"
        payload = _daemon_call(args, method, params)
    elif args.dry_run:
        payload = service.vm_import_plan(
            args.import_kind,
            args.source,
            args.name,
            preset=args.preset,
            network=args.network,
            storage_config=args.storage_config,
            vcpus=args.vcpus,
            memory_mib=args.memory_mib,
            firmware=args.firmware,
            machine=args.machine,
            disk_bus=args.disk_bus,
            nic_model=args.nic_model,
            video=args.video,
            serial_console=args.serial_console,
            data_disk_gb=args.data_disk_gb,
            nics=nics,
            uri=args.uri,
        )
    else:
        payload = service.vm_import(
            args.import_kind,
            args.source,
            args.name,
            preset=args.preset,
            network=args.network,
            network_config=args.network_config,
            storage_config=args.storage_config,
            vcpus=args.vcpus,
            memory_mib=args.memory_mib,
            firmware=args.firmware,
            machine=args.machine,
            disk_bus=args.disk_bus,
            nic_model=args.nic_model,
            video=args.video,
            serial_console=args.serial_console,
            data_disk_gb=args.data_disk_gb,
            nics=nics,
            start=args.start,
            uri=args.uri,
        )
    if args.json:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return 0
    if args.dry_run:
        print(f"Import plan: {args.import_kind} -> {payload.get('name')}")
        print(f"  Preset:       {payload.get('preset')}")
        print(f"  Source:       {payload.get('source')}")
        print(f"  Destination:  {payload.get('destination') or '-'}")
        print(f"  Estimate:     {payload.get('estimated_human')}")
        print(f"  Required:     {payload.get('required_human')}")
        print(f"  Available:    {payload.get('available_human')}")
        for disk in payload.get("disks") or []:
            print(
                f"  Disk {disk.get('target')}:     "
                f"{disk.get('size_human')} {disk.get('bus')} "
                f"({disk.get('source') or 'new empty disk'})"
            )
        for nic in payload.get("nics") or []:
            print(
                f"  NIC {nic.get('target')}:      "
                f"{nic.get('network')} {nic.get('model')} "
                f"MAC {nic.get('mac') or 'auto'}"
            )
        for warning in payload.get("warnings") or []:
            print(f"  Warning:      {warning}")
        for reason in payload.get("reasons") or []:
            print(f"  Blocked:      {reason}")
        print(
            "  Result:       "
            + ("ready" if payload.get("supported") else "not ready")
        )
        return 0
    print(f"Imported {payload['name']} to v-EMU.")
    print(f"Chain: {payload.get('chain')}")
    for disk in payload.get("disks") or []:
        print(f"  {disk.get('target')}: {disk.get('active_disk')}")
    return 0


def _cmd_rename(args: argparse.Namespace) -> int:
    if not args.dry_run and not args.yes:
        raise ValidationError(
            "Rename changes the domain name and may move the disk chain. "
            "Re-run with --yes after reviewing --dry-run."
        )
    params = {
        "domain": args.domain,
        "new_name": args.new_name,
        "rename_disks": not args.no_disks,
        "backup_dir": args.backup_dir,
        "uri": args.uri,
    }
    if _daemon_enabled(args):
        method = "vm.rename_plan" if args.dry_run else "vm.rename"
        payload = _daemon_call(args, method, params)
    elif args.dry_run:
        payload = service.vm_rename_plan(
            args.domain,
            args.new_name,
            rename_disks=not args.no_disks,
            uri=args.uri,
        )
    else:
        payload = service.vm_rename(
            args.domain,
            args.new_name,
            rename_disks=not args.no_disks,
            backup_dir=args.backup_dir,
            uri=args.uri,
        )
    if args.json:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return 0
    if args.dry_run:
        print(f"Rename plan for {args.domain} -> {payload['new_name']}")
        print(f"  State:        {payload.get('state')}")
        print(f"  Managed by:   {payload.get('managed_by')}")
        print(f"  Rename disks: {'yes' if payload.get('rename_disks') else 'no'}")
        if payload.get("chain_dir"):
            print(f"  Chain:        {payload['chain_dir']}")
            print(f"  New chain:    {payload['new_chain_dir']}")
        for disk in payload.get("disks") or []:
            print(f"  Disk {disk['target']}:     {disk['new']}")
        if payload.get("snapshots"):
            print(f"  Snapshots:    {', '.join(payload['snapshots'])}")
        for warning in payload.get("warnings") or []:
            print(f"  Warning:      {warning}")
        for reason in payload.get("reasons") or []:
            print(f"  Blocked:      {reason}")
        print(
            "  Result:       "
            + ("ready" if payload.get("supported") else "not ready")
        )
        return 0
    print(
        f"Renamed {payload['old_name']} -> {payload['domain']}.\n"
        f"Backup XML: {payload['backup']}"
    )
    if payload.get("rename_disks"):
        print(f"Chain: {payload['new_chain_dir']}")
    return 0


def _human(value: int | float) -> str:
    size = float(value)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if size < 1024 or unit == "TiB":
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TiB"


def _cmd_seal(args: argparse.Namespace) -> int:
    name = args.name or f"{slugify(args.domain)}-base"
    if _daemon_enabled(args):
        payload = _daemon_call(
            args,
            "seal",
            {
                "domain": args.domain,
                "dest": args.dest,
                "name": name,
                "description": args.description,
                "os_family": getattr(args, "os_family", ""),
                "sysprepped": args.sysprepped,
                "shutdown": args.shutdown,
                "in_place": args.in_place,
                "accept_bitlocker": args.accept_bitlocker,
                "uri": args.uri,
            },
        )
    else:
        payload = service.seal_template(
            args.domain,
            args.dest,
            name=name,
            description=args.description,
            os_family=getattr(args, "os_family", ""),
            sysprepped=args.sysprepped,
            shutdown=args.shutdown,
            in_place=args.in_place,
            accept_bitlocker=args.accept_bitlocker,
            uri=args.uri,
        )
    if args.json:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        print(
            f"Sealed {args.domain} as template '{payload['name']}' "
            f"({payload['base']})."
        )
        if payload["firmware"] == "efi":
            print("Firmware: UEFI")
        if payload.get("in_place"):
            print(
                "In-place: the source disk is now the immutable base; "
                "the VM was repointed to a new overlay."
            )
        removed = payload.get("removed_shares") or []
        if removed:
            print(
                "Removed VirtioFS shares from the source VM: "
                + ", ".join(removed)
                + ". Reattach them from Shared folders if needed."
            )
        if payload.get("os_family") == "windows" and not payload.get(
            "sysprepped"
        ):
            print(
                "Warning: this Windows template was sealed without Sysprep. "
                "Clones keep the same SID and machine identity; run Sysprep "
                "/generalize /oobe manually before sealing if you need "
                "unique clones."
            )
    return 0


def _cmd_template_import(args: argparse.Namespace) -> int:
    params = {
        "source": args.source,
        "dest": args.dest,
        "name": args.name,
        "description": args.description,
        "arch": args.arch,
        "machine": args.machine,
        "firmware": args.firmware,
        "disk_bus": args.disk_bus,
        "video": args.video,
        "video_memory_mib": args.video_memory,
        "nic_model": args.nic_model,
        "secure_boot": args.secure_boot,
        "tpm": args.tpm,
        "os_family": getattr(args, "os_family", ""),
        "sysprepped": args.sysprepped,
    }
    if _daemon_enabled(args):
        payload = _daemon_call(args, "template.import", params)
    else:
        payload = service.template_import(
            args.source,
            args.dest,
            args.name,
            description=args.description,
            arch=args.arch,
            machine=args.machine,
            firmware=args.firmware,
            disk_bus=args.disk_bus,
            video=args.video,
            video_memory_mib=args.video_memory,
            nic_model=args.nic_model,
            secure_boot=args.secure_boot,
            tpm=args.tpm,
            os_family=getattr(args, "os_family", ""),
            sysprepped=args.sysprepped,
        )
    if args.json:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        print(
            f"Imported template '{payload['name']}' "
            f"({payload['base_size'] / (1024 ** 3):.1f} GB) "
            f"from {args.source}"
        )
    return 0


def _cmd_template_update(args: argparse.Namespace) -> int:
    params: dict[str, object] = {"template": args.template}
    if args.os_family is not None:
        params["os_family"] = args.os_family
    if args.sysprepped is not None:
        params["sysprepped"] = args.sysprepped
    if _daemon_enabled(args):
        payload = _daemon_call(args, "template.update", params)
    else:
        payload = service.template_update(
            args.template,
            os_family=args.os_family,
            sysprepped=args.sysprepped,
        )
    if args.json:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        print(
            f"Updated template '{payload['name']}': "
            f"OS={payload.get('os_family') or 'unknown'}, "
            f"sysprepped={'yes' if payload.get('sysprepped') else 'no'}"
        )
    return 0


def _cmd_template_delete(args: argparse.Namespace) -> int:
    if _daemon_enabled(args):
        payload = _daemon_call(
            args, "template.delete", {"template": args.template}
        )
    else:
        payload = service.template_delete(args.template)
    if args.json:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        print(f"Deleted template {payload['template']} ({payload['path']})")
    return 0


def _cmd_template_export(args: argparse.Namespace) -> int:
    params = {
        "template": args.template,
        "dest": args.dest,
        "format": args.format,
    }
    if _daemon_enabled(args):
        payload = _daemon_call(args, "template.export", params)
    else:
        payload = service.template_export(
            args.template, args.dest, format=args.format
        )
    if args.json:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        print(
            f"Exported {payload['template']} → {payload['path']} "
            f"({payload['format']}, {payload['size'] / (1024 ** 3):.1f} GB)"
        )
    return 0


def _cmd_templates(args: argparse.Namespace) -> int:
    if _daemon_enabled(args):
        payload = _daemon_call(args, "templates.list", {"root": args.root})
    else:
        payload = [
            template.to_dict() for template in list_templates(args.root)
        ]
    if args.json:
        print(json.dumps({"templates": payload}, indent=2, ensure_ascii=False))
        return 0
    if not payload:
        print("No templates found.")
        return 0
    rows = []
    for template in payload:
        size = template.get("base_size")
        if size is None:
            base = Path(template["base"])
            size = base.stat().st_size if base.is_file() else 0
        rows.append(
            [
                template["name"],
                template["arch"],
                template.get("os_family")
                or ("windows" if template.get("sysprepped") else "linux"),
                template["firmware"],
                "SB="
                + ("on" if template.get("secure_boot") else "off")
                + " TPM="
                + ("on" if template.get("tpm") else "off"),
                f"{size / (1024 ** 3):.1f} GB",
                "yes" if template["sysprepped"] else "no",
            ]
        )
    _print_table(
        ["NAME", "ARCH", "OS", "FIRMWARE", "SECURITY", "SIZE", "SYSPREPPED"],
        rows,
    )
    return 0


def _location_row(location: dict[str, Any]) -> list[str]:
    selinux = location["selinux"]
    label = selinux.get("label") or "-"
    return [
        location["path"],
        location["role"],
        location["filesystem"],
        f"{location['free_bytes'] / (1024 ** 3):.1f} GB",
        f"{selinux.get('mode', 'unknown')}:{label}",
        "yes" if location["healthy"] else "no",
    ]


def _emit_location(payload: dict[str, Any], args: argparse.Namespace) -> None:
    if args.json:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return
    location = payload["location"]
    actions = payload.get("actions", [])
    selinux = location["selinux"]
    print(f"Path:   {location['path']}")
    print(f"Role:   {location['role']}")
    print(f"Filesystem: {location['filesystem']}")
    print(f"Free:   {location['free_bytes'] / (1024 ** 3):.1f} GB")
    print(
        f"SELinux: {selinux.get('mode')} · "
        f"{selinux.get('label') or 'unknown'} "
        f"(expected {selinux.get('expected')})"
    )
    print(f"Owner:  {location['owner']}  Mode: {location['mode']}")
    if actions:
        print()
        print("Actions:")
        for action in actions:
            state = "applied" if action["applied"] else "planned"
            print(
                f"  [{state}] {action['description']}: "
                f"{' '.join(action['command'])}"
            )
    if location["issues"]:
        print()
        print("Issues:")
        for issue in location["issues"]:
            print(f"  - {issue}")
    warnings = location.get("warnings") or []
    if warnings:
        print()
        print("Warnings:")
        for warning in warnings:
            print(f"  - {warning}")
    print()
    print("Healthy." if location["healthy"] else "Needs attention.")


def _cmd_storage_inspect(args: argparse.Namespace) -> int:
    if _daemon_enabled(args):
        location = _daemon_call(
            args, "storage.inspect", {"path": args.path, "role": args.role}
        )
    else:
        location = service.storage_inspect(args.path, args.role)
    if args.json:
        print(json.dumps(location, indent=2, ensure_ascii=False))
    else:
        _print_table(
            ["PATH", "ROLE", "FS", "FREE", "SELINUX", "HEALTHY"],
            [_location_row(location)],
        )
        for issue in location["issues"]:
            print(f"  - {issue}")
    return 0 if location["healthy"] else 1


def _cmd_storage_add(args: argparse.Namespace) -> int:
    validate_role(args.role)
    if _daemon_enabled(args):
        payload = _daemon_call(
            args,
            "storage.add",
            {
                "path": args.path,
                "role": args.role,
                "config": args.config,
                "dry_run": args.dry_run,
            },
        )
    else:
        payload = service.storage_add(
            args.path, args.role, config=args.config, dry_run=args.dry_run
        )
    _emit_location(payload, args)
    return 0 if payload["location"]["exists"] else 1


def _cmd_storage_repair(args: argparse.Namespace) -> int:
    validate_role(args.role)
    if _daemon_enabled(args):
        payload = _daemon_call(
            args,
            "storage.repair",
            {
                "path": args.path,
                "role": args.role,
                "dry_run": args.dry_run,
                "force_selinux": args.force_selinux,
            },
        )
    else:
        payload = service.storage_repair(
            args.path,
            args.role,
            dry_run=args.dry_run,
            force_selinux=args.force_selinux,
        )
    _emit_location(payload, args)
    return 0 if payload["location"]["exists"] else 1


def _cmd_storage_list(args: argparse.Namespace) -> int:
    if _daemon_enabled(args):
        locations = _daemon_call(
            args, "storage.list", {"config": args.config}
        )
    else:
        locations = service.storage_list(args.config)
    if args.json:
        print(
            json.dumps(
                {"locations": locations},
                indent=2,
                ensure_ascii=False,
            )
        )
        return 0
    if not locations:
        print("No storage locations registered.")
        return 0
    _print_table(
        ["PATH", "ROLE", "FS", "FREE", "SELINUX", "HEALTHY"],
        [_location_row(location) for location in locations],
    )
    return 0


def _cmd_storage_remove(args: argparse.Namespace) -> int:
    if _daemon_enabled(args):
        entry = _daemon_call(
            args, "storage.remove", {"path": args.path, "config": args.config}
        )
    else:
        entry = service.storage_remove(args.path, args.config)
    if args.json:
        print(json.dumps(entry, indent=2))
    else:
        print(f"Removed {entry['path']} from the v-EMU registry. Data was not deleted.")
    return 0


def _cmd_network_list(args: argparse.Namespace) -> int:
    if _daemon_enabled(args):
        networks = _daemon_call(
            args, "networks.list", {"config": args.config}
        )
    else:
        networks = service.networks_list(args.config)
    if args.json:
        print(json.dumps({"networks": networks}, indent=2, ensure_ascii=False))
        return 0
    if not networks:
        print("No networks registered.")
        return 0
    _print_table(
        ["NAME", "MODE", "SUBNET", "DHCP", "BRIDGE", "STATE", "FORWARDS"],
        [
            [
                item["name"],
                item["mode"],
                item["subnet"],
                "yes" if item["dhcp"] else "no",
                item["bridge"],
                "active"
                if item["runtime"]["active"]
                else ("defined" if item["runtime"]["defined"] else "new"),
                str(len(item["port_forwards"])),
            ]
            for item in networks
        ],
    )
    return 0


def _cmd_network_add(args: argparse.Namespace) -> int:
    dhcp = args.dhcp if args.dhcp is not None else args.mode == "nat"
    params = {
        "name": args.name,
        "mode": args.mode,
        "subnet": args.subnet,
        "dhcp": dhcp,
        "dns_domain": args.dns_domain,
        "description": args.description,
        "bridge": args.bridge,
        "config": args.config,
    }
    if _daemon_enabled(args):
        profile = _daemon_call(args, "network.add", params)
    else:
        profile = service.network_add(
            args.name,
            args.mode,
            args.subnet,
            dhcp,
            args.dns_domain,
            args.description,
            args.bridge,
            config=args.config,
        )
    if args.json:
        print(json.dumps(profile, indent=2, ensure_ascii=False))
    else:
        print(
            f"Added network {profile['name']}: {profile['mode']} "
            f"{profile['subnet']} · bridge {profile['bridge']}"
        )
    return 0


def _cmd_network_remove(args: argparse.Namespace) -> int:
    params = {
        "name": args.name,
        "purge": args.purge,
        "config": args.config,
    }
    if _daemon_enabled(args):
        result = _daemon_call(args, "network.remove", params)
    else:
        result = service.network_remove(
            args.name, config=args.config, purge=args.purge
        )
    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        suffix = " and removed its libvirt network" if result["purged"] else ""
        print(f"Removed network {args.name}{suffix}.")
    return 0


def _cmd_network_apply(args: argparse.Namespace) -> int:
    params = {"config": args.config, "dry_run": args.dry_run}
    if _daemon_enabled(args):
        result = _daemon_call(args, "network.apply", params)
    else:
        result = service.network_apply(
            args.config, dry_run=args.dry_run
        )
    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0
    if args.dry_run:
        for action in result.get("plan", []):
            print(f"{action['action']:>7}  {action['name']}")
        print()
        print(result["nft"]["ruleset"])
        return 0
    for item in result["networks"]:
        print(
            f"{item['action']:>7}  {item['name']}  bridge={item['bridge']} "
            f"active={item['active']}"
        )
    nft = result["nft"]
    print(
        "nft port forwards: applied"
        if nft.get("applied")
        else f"nft port forwards: {nft.get('reason', 'not applied')}"
    )
    return 0


def _cmd_network_restart(args: argparse.Namespace) -> int:
    params = {
        "name": args.name,
        "config": args.config,
        "force": args.force,
    }
    if _daemon_enabled(args):
        result = _daemon_call(args, "network.restart", params)
    else:
        result = service.network_restart(
            args.name, config=args.config, force=args.force
        )
    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        print(
            f"Restarted {args.name} · bridge {result['bridge']} "
            f"active={result['active']}"
        )
    return 0


def _cmd_network_plan(args: argparse.Namespace) -> int:
    if _daemon_enabled(args):
        result = _daemon_call(
            args, "network.plan", {"config": args.config}
        )
    else:
        result = service.network_plan(args.config)
    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0
    if result.get("error"):
        print(f"libvirt: {result['error']}")
    for action in result["actions"]:
        print(f"{action['action']:>7}  {action['name']}")
    return 0


def _cmd_network_forward_add(args: argparse.Namespace) -> int:
    params = {
        "name": args.network,
        "protocol": args.protocol,
        "host_port": args.host_port,
        "guest_ip": args.guest_ip,
        "guest_port": args.guest_port,
        "description": args.description,
        "config": args.config,
        "uri": args.uri,
    }
    if _daemon_enabled(args):
        result = _daemon_call(args, "network.port_forward_add", params)
    else:
        result = service.network_port_forward_add(
            args.network,
            args.protocol,
            args.host_port,
            args.guest_ip,
            args.guest_port,
            args.description,
            config=args.config,
            uri=args.uri,
        )
    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        print(
            f"Forward {args.protocol}/{args.host_port} → "
            f"{args.guest_ip}:{args.guest_port} on {args.network}."
        )
        if result.get("warning"):
            print(f"  warning: {result['warning']}")
    return 0


def _cmd_network_leases(args: argparse.Namespace) -> int:
    params = {
        "name": args.network,
        "config": args.config,
        "uri": args.uri,
    }
    if _daemon_enabled(args):
        result = _daemon_call(args, "network.leases", params)
    else:
        result = service.network_leases(
            args.network, config=args.config, uri=args.uri
        )
    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0
    _print_table(
        ["RESERVED MAC", "RESERVED IP", "HOSTNAME"],
        [
            [item["mac"], item["ip"], item.get("hostname", "")]
            for item in result["reservations"]
        ],
    )
    _print_table(
        ["LEASE MAC", "LEASE IP", "HOSTNAME"],
        [
            [item["mac"], item["ip"], item.get("hostname", "")]
            for item in result["leases"]
        ],
    )
    return 0


def _cmd_network_forward_remove(args: argparse.Namespace) -> int:
    params = {
        "name": args.network,
        "protocol": args.protocol,
        "host_port": args.host_port,
        "config": args.config,
    }
    if _daemon_enabled(args):
        result = _daemon_call(args, "network.port_forward_remove", params)
    else:
        result = service.network_port_forward_remove(
            args.network,
            args.protocol,
            args.host_port,
            config=args.config,
        )
    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        print(f"Removed forward {args.protocol}/{args.host_port}.")
    return 0


def _cmd_network_reservation_add(args: argparse.Namespace) -> int:
    params = {
        "name": args.network,
        "mac": args.mac,
        "ip": args.ip,
        "hostname": args.hostname,
        "config": args.config,
    }
    if _daemon_enabled(args):
        result = _daemon_call(args, "network.reservation_add", params)
    else:
        result = service.network_reservation_add(
            args.network,
            args.mac,
            args.ip,
            args.hostname,
            config=args.config,
        )
    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        print(
            f"Reserved {args.ip} for {args.mac} on {args.network}. "
            "Restart the network to apply."
        )
    return 0


def _cmd_network_reservation_remove(args: argparse.Namespace) -> int:
    params = {
        "name": args.network,
        "mac": args.mac,
        "config": args.config,
    }
    if _daemon_enabled(args):
        result = _daemon_call(args, "network.reservation_remove", params)
    else:
        result = service.network_reservation_remove(
            args.network, args.mac, config=args.config
        )
    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        print(
            f"Removed reservation {args.mac} from {args.network}. "
            "Restart the network to apply."
        )
    return 0


def _cmd_network_attach(args: argparse.Namespace) -> int:
    params = {
        "domain": args.domain,
        "network": args.network,
        "model": args.model,
        "config": args.config,
    }
    if _daemon_enabled(args):
        result = _daemon_call(args, "network.attach", params)
    else:
        result = service.network_attach(
            args.domain, args.network, model=args.model, config=args.config
        )
    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        print(
            f"Attached {args.network} to {args.domain} "
            f"({result['mac']})."
        )
    return 0


def _cmd_network_detach(args: argparse.Namespace) -> int:
    params = {"domain": args.domain, "mac": args.mac}
    if _daemon_enabled(args):
        result = _daemon_call(args, "network.detach", params)
    else:
        result = service.network_detach(args.domain, args.mac)
    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        print(f"Detached {args.mac} from {args.domain}.")
    return 0


def _cmd_daemon_serve(args: argparse.Namespace) -> int:
    from .daemon import main as daemon_main

    argv = ["--socket", args.socket, "--uri", args.uri, "--foreground"]
    if args.config:
        argv.extend(["--config", args.config])
    if args.network_config:
        argv.extend(["--network-config", args.network_config])
    if args.group:
        argv.extend(["--group", args.group])
    if args.socket_mode:
        argv.extend(["--socket-mode", args.socket_mode])
    if args.polkit:
        argv.append("--polkit")
    return daemon_main(argv)


def _cmd_daemon_ping(args: argparse.Namespace) -> int:
    result = DaemonClient(args.socket).ping()
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print(f"pong · vemud {result.get('version', 'unknown')}")
    return 0


def _cmd_daemon_status(args: argparse.Namespace) -> int:
    result = DaemonClient(args.socket).status()
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        for key, value in result.items():
            print(f"{key}: {value}")
    return 0


def _cmd_daemon_call(args: argparse.Namespace) -> int:
    params = json.loads(args.params) if args.params else {}
    if not isinstance(params, dict):
        raise ValidationError("--params must be a JSON object.")
    result = DaemonClient(args.socket).call(args.method, params)
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


def _cmd_ui(args: argparse.Namespace) -> int:
    from .ui.app import main as ui_main

    argv = ["--uri", args.uri]
    if getattr(args, "socket", None):
        argv.extend(["--socket", args.socket])
    if getattr(args, "direct", False):
        argv.append("--direct")
    if args.dark:
        argv.append("--dark")
    if args.light:
        argv.append("--light")
    if getattr(args, "no_mini_console", False):
        argv.append("--no-mini-console")
    if args.screenshot:
        argv.extend(["--screenshot", args.screenshot])
    return ui_main(argv)


def _cmd_doctor(args: argparse.Namespace) -> int:
    if _daemon_enabled(args):
        payload = _daemon_call(args, "doctor.run", {"uri": args.uri})
    else:
        payload = diagnose.run_checks(
            args.uri, config=getattr(args, "config", None)
        )
    if args.repair:
        repairs: list[dict[str, Any]] = []
        if _daemon_enabled(args):
            locations = _daemon_call(args, "storage.list", {})
        else:
            locations = service.storage_list()
        for location in locations:
            path = str(location.get("path", ""))
            role = str(location.get("role", "labs"))
            try:
                if _daemon_enabled(args):
                    result = _daemon_call(
                        args,
                        "storage.repair",
                        {
                            "path": path,
                            "role": role,
                            "dry_run": args.dry_run,
                        },
                    )
                else:
                    result = service.storage_repair(
                        path, role, dry_run=args.dry_run
                    )
                repairs.append(
                    {
                        "kind": "storage",
                        "path": path,
                        "role": role,
                        "dry_run": args.dry_run,
                        "actions": result.get("actions", []),
                        "exists": result.get("location", {}).get("exists"),
                    }
                )
            except VemuError as exc:
                repairs.append(
                    {
                        "kind": "storage",
                        "path": path,
                        "role": role,
                        "error": str(exc),
                    }
                )
            if _daemon_enabled(args):
                journal_paths = [
                    Path(item)
                    for item in _daemon_call(
                        args, "chain.journals", {"root": path}
                    )
                ]
            else:
                journal_paths = find_commit_journals(path)
            for journal in journal_paths:
                if args.dry_run:
                    repairs.append(
                        {
                            "kind": "chain",
                            "path": str(journal.parent),
                            "journal": str(journal),
                            "dry_run": True,
                            "result": {
                                "pending": True,
                                "recovered": False,
                            },
                        }
                    )
                    continue
                try:
                    if _daemon_enabled(args):
                        result = _daemon_call(
                            args,
                            "chain.recover",
                            {"chain": str(journal.parent)},
                        )
                    else:
                        result = service.chain_recover(str(journal.parent))
                    repairs.append(
                        {
                            "kind": "chain",
                            "path": str(journal.parent),
                            "journal": str(journal),
                            "dry_run": args.dry_run,
                            "result": result,
                        }
                    )
                except VemuError as exc:
                    repairs.append(
                        {
                            "kind": "chain",
                            "path": str(journal.parent),
                            "journal": str(journal),
                            "error": str(exc),
                        }
                    )
        payload["repairs"] = repairs
        if not args.dry_run:
            if _daemon_enabled(args):
                fresh = _daemon_call(args, "doctor.run", {"uri": args.uri})
            else:
                fresh = diagnose.run_checks(
                    args.uri, config=getattr(args, "config", None)
                )
            fresh["repairs"] = repairs
            payload = fresh
    if args.json:
        print(json.dumps(payload, indent=2))
        return 0 if payload["ok"] else 1
    rows = [
        [
            (
                "ok"
                if item["ok"]
                else ("WARN" if item.get("level") == "warn" else "FAIL")
            ),
            item["check"],
            str(item["detail"]),
        ]
        for item in payload["checks"]
    ]
    _print_table(["", "CHECK", "DETAIL"], rows)
    if args.repair:
        repairs = payload.get("repairs", [])
        if not repairs:
            print("No registered storage locations to repair.")
        for repair in repairs:
            if repair.get("kind") == "chain":
                if repair.get("error"):
                    print(
                        f"repair {repair['path']}: {repair['error']}"
                    )
                elif repair.get("dry_run"):
                    print(
                        f"pending commit {repair['path']} "
                        f"({repair['journal']})"
                    )
                else:
                    result = repair.get("result", {})
                    print(
                        f"recovered {repair['path']} "
                        f"({result.get('action', 'already complete')})"
                    )
                continue
            if repair.get("error"):
                print(f"repair {repair['path']}: {repair['error']}")
            else:
                action = "plan" if repair.get("dry_run") else "repaired"
                print(
                    f"{action} {repair['path']} ({repair['role']}): "
                    f"{len(repair.get('actions', []))} action(s)"
                )
                for item in repair.get("actions", []):
                    print(f"  {item}")
    return 0 if payload["ok"] else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="vemu",
        description="Lab-first desktop virtualization for Linux.",
    )
    parser.add_argument("--version", action="version", version=f"vemu {__version__}")
    parser.add_argument(
        "--socket",
        help="vemud socket path (default: VEMU_SOCKET or /run/vemu/vemud.sock).",
    )
    parser.add_argument(
        "--direct",
        action="store_true",
        help="Bypass the daemon and call the local implementation (development).",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    list_parser = sub.add_parser("list", help="List libvirt domains.")
    list_parser.add_argument("--uri", default="qemu:///system")
    list_parser.add_argument("--json", action="store_true")
    list_parser.set_defaults(func=_cmd_list)

    init = sub.add_parser("init", help="Create a sample lab.yaml.")
    init.add_argument("path", nargs="?", default="lab.yaml")
    init.add_argument("--name", help="Lab name (default: file stem).")
    init.add_argument("--dest", default="./vms", help="VM storage directory.")
    init.add_argument(
        "--template", default="fedora-44-x86", help="Template name for node1."
    )
    init.add_argument("--force", action="store_true")
    init.set_defaults(func=_cmd_init)

    autostart = sub.add_parser(
        "autostart", help="Enable or disable VM autostart on host boot."
    )
    autostart.add_argument("--domain", required=True)
    autostart.add_argument(
        "--off", action="store_true", help="Disable autostart (default: enable)."
    )
    autostart.add_argument("--uri", default="qemu:///system")
    autostart.add_argument("--json", action="store_true")
    autostart.set_defaults(func=_cmd_autostart)

    stats = sub.add_parser("stats", help="Show runtime stats for one VM.")
    stats.add_argument("--domain", required=True)
    stats.add_argument("--uri", default="qemu:///system")
    stats.add_argument("--json", action="store_true")
    stats.set_defaults(func=_cmd_stats)

    ssh_parser = sub.add_parser("ssh", help="Open an SSH session to a guest.")
    ssh_parser.add_argument("domain")
    ssh_parser.add_argument("--user", default="lab")
    ssh_parser.add_argument("--ip", help="Override the guest IP.")
    ssh_parser.add_argument("--key", help="SSH private key.")
    ssh_parser.add_argument("--port", type=int, default=22)
    ssh_parser.add_argument(
        "--remote-command",
        help="Run one command on the guest instead of an interactive shell.",
    )
    ssh_parser.add_argument("--dry-run", action="store_true")
    ssh_parser.add_argument("--uri", default="qemu:///system")
    ssh_parser.add_argument("--network-config")
    ssh_parser.set_defaults(func=_cmd_ssh)

    console_parser = sub.add_parser(
        "console", help="Show or open a VM's VNC console."
    )
    console_parser.add_argument("domain")
    console_parser.add_argument(
        "--open",
        action="store_true",
        help="Open with virt-viewer/remote-viewer, or virsh console for serial.",
    )
    console_parser.add_argument(
        "--serial",
        action="store_true",
        help="Use the serial console instead of VNC.",
    )
    console_parser.add_argument("--uri", default="qemu:///system")
    console_parser.add_argument("--json", action="store_true")
    console_parser.set_defaults(func=_cmd_console)

    delete = sub.add_parser(
        "delete", help="Undefine a VM, optionally deleting its disks."
    )
    delete.add_argument("--domain", required=True)
    delete.add_argument(
        "--delete-storage",
        action="store_true",
        help="Delete v-EMU-managed chain directories (keeps shared bases).",
    )
    delete.add_argument(
        "--force", action="store_true", help="Force off a running VM first."
    )
    delete.add_argument("--uri", default="qemu:///system")
    delete.add_argument("--json", action="store_true")
    delete.set_defaults(func=_cmd_delete_vm)

    up = sub.add_parser("up", help="Create/start the lab described by lab.yaml.")
    up.add_argument("path", nargs="?", default="lab.yaml")
    up.add_argument("--uri", default="qemu:///system")
    up.add_argument("--network-config")
    up.add_argument("--no-start", action="store_true")
    up.add_argument("--dry-run", action="store_true")
    up.add_argument("--json", action="store_true")
    up.set_defaults(func=_cmd_up)

    down = sub.add_parser("down", help="Shut down the lab described by lab.yaml.")
    down.add_argument("path", nargs="?", default="lab.yaml")
    down.add_argument("--uri", default="qemu:///system")
    down.add_argument("--force", action="store_true")
    down.add_argument("--json", action="store_true")
    down.set_defaults(func=_cmd_down)

    reset = sub.add_parser(
        "reset", help="Recreate selected lab VMs from their templates."
    )
    reset.add_argument("--lab", default="lab.yaml")
    reset.add_argument(
        "--vm",
        action="append",
        required=True,
        help="VM name from lab.yaml (repeat for multiple VMs).",
    )
    reset.add_argument(
        "--force", action="store_true", help="Force off running VMs first."
    )
    reset.add_argument(
        "--no-start", action="store_true", help="Leave reset VMs stopped."
    )
    reset.add_argument("--uri", default="qemu:///system")
    reset.add_argument("--network-config")
    reset.add_argument("--json", action="store_true")
    reset.set_defaults(func=_cmd_lab_reset)

    lab_status = sub.add_parser(
        "status", help="Show lab VM/network status."
    )
    lab_status.add_argument("path", nargs="?", default="lab.yaml")
    lab_status.add_argument("--uri", default="qemu:///system")
    lab_status.add_argument("--network-config")
    lab_status.add_argument("--json", action="store_true")
    lab_status.set_defaults(func=_cmd_lab_status)

    export = sub.add_parser(
        "export", help="Export Ansible inventory or libvirt XML."
    )
    export.add_argument(
        "--ansible-inventory",
        action="store_true",
        help="Write an Ansible inventory grouped by network.",
    )
    export.add_argument(
        "--libvirt-xml",
        action="store_true",
        help="Export domain XML.",
    )
    export.add_argument("--domain", help="Only this domain (XML mode).")
    export.add_argument("--dest", help="Output file or directory.")
    export.add_argument("--ansible-user", help="ansible_user for every host.")
    export.add_argument("--uri", default="qemu:///system")
    export.add_argument("--network-config")
    export.add_argument("--json", action="store_true")
    export.set_defaults(func=_cmd_export)

    gc = sub.add_parser(
        "gc", help="Find orphan qcow2 layers in managed chains."
    )
    gc.add_argument(
        "root",
        nargs="?",
        default=".",
        help="Chain directory or tree to scan (default: current directory).",
    )
    gc.add_argument(
        "--force", action="store_true", help="Delete orphan layers (default: dry run)."
    )
    gc.add_argument("--json", action="store_true")
    gc.set_defaults(func=_cmd_gc)

    lab_parser = sub.add_parser(
        "lab", help="Export/import a portable lab bundle."
    )
    lab_sub = lab_parser.add_subparsers(
        dest="lab_command", required=True
    )
    lab_export = lab_sub.add_parser("export", help="Export a lab bundle.")
    lab_export.add_argument("--lab", default="lab.yaml")
    lab_export.add_argument("--dest", required=True)
    lab_export.add_argument("--with-templates", action="store_true")
    lab_export.add_argument("--json", action="store_true")
    lab_export.set_defaults(func=_cmd_lab_export)

    lab_import = lab_sub.add_parser("import", help="Import a lab bundle.")
    lab_import.add_argument("--bundle", required=True)
    lab_import.add_argument("--dest", required=True)
    lab_import.add_argument("--templates-root")
    lab_import.add_argument("--overwrite", action="store_true")
    lab_import.add_argument("--json", action="store_true")
    lab_import.set_defaults(func=_cmd_lab_import)

    lab_reset = lab_sub.add_parser(
        "reset", help="Recreate selected lab VMs from their templates."
    )
    lab_reset.add_argument("--lab", default="lab.yaml")
    lab_reset.add_argument(
        "--vm",
        action="append",
        required=True,
        help="VM name from lab.yaml (repeat for multiple VMs).",
    )
    lab_reset.add_argument(
        "--force", action="store_true", help="Force off running VMs first."
    )
    lab_reset.add_argument(
        "--no-start", action="store_true", help="Leave reset VMs stopped."
    )
    lab_reset.add_argument("--uri", default="qemu:///system")
    lab_reset.add_argument("--network-config")
    lab_reset.add_argument("--json", action="store_true")
    lab_reset.set_defaults(func=_cmd_lab_reset)

    lab_destroy = lab_sub.add_parser(
        "destroy", help="Remove all lab VMs, chains and lab networks."
    )
    lab_destroy.add_argument("--lab", default="lab.yaml")
    lab_destroy.add_argument(
        "--force", action="store_true", help="Force off running lab VMs."
    )
    lab_destroy.add_argument(
        "--keep-networks",
        action="store_true",
        help="Keep lab network profiles/libvirt networks.",
    )
    lab_destroy.add_argument("--uri", default="qemu:///system")
    lab_destroy.add_argument("--network-config")
    lab_destroy.add_argument("--json", action="store_true")
    lab_destroy.set_defaults(func=_cmd_lab_destroy)

    clone = sub.add_parser("clone", help="Create linked clone chains from a template.")
    clone.add_argument(
        "--template",
        required=True,
        help="Template directory (with template.json) or a base qcow2 image.",
    )
    clone.add_argument("--dest", required=True, help="Directory for clone chains.")
    clone.add_argument("--count", type=int, default=1)
    clone.add_argument("--name", default="vm-{{n}}")
    clone.add_argument("--start-at", type=int, default=1, help="First clone index.")
    clone.add_argument("--json", action="store_true")
    clone.add_argument(
        "--define",
        action="store_true",
        help="Define a libvirt domain for every clone.",
    )
    clone.add_argument(
        "--start",
        action="store_true",
        help="Start domains after defining them (requires --define).",
    )
    clone.add_argument("--memory", type=int, help="Memory in MiB (default: template).")
    clone.add_argument("--vcpus", type=int, help="vCPUs (default: template).")
    clone.add_argument("--network", default="default")
    clone.add_argument(
        "--network-config",
        help="Network registry JSON (default: VEMU_NETWORK_CONFIG).",
    )
    clone.add_argument(
        "--firmware",
        choices=["bios", "efi"],
        help="Firmware (default: template).",
    )
    clone.add_argument(
        "--no-identity-reset",
        action="store_true",
        help="Do not reset hostname/machine-id/SSH keys on Linux clones.",
    )
    clone.add_argument(
        "--cloud-init",
        action="store_true",
        help="Attach a NoCloud seed ISO to Linux clones.",
    )
    clone.add_argument("--ci-user", default="lab")
    clone.add_argument("--ci-password")
    clone.add_argument(
        "--ci-ssh-key", help="Path to an SSH public key for the ci user."
    )
    clone.add_argument(
        "--ci-package",
        action="append",
        default=[],
        help="Package to install via cloud-init (repeatable).",
    )
    clone.add_argument(
        "--ci-run",
        action="append",
        default=[],
        help="Shell command to run via cloud-init (repeatable).",
    )
    clone.add_argument(
        "--ci-ip",
        help="Static IP with prefix for cloud-init, e.g. 192.168.50.11/24.",
    )
    clone.add_argument("--ci-gateway")
    clone.add_argument(
        "--ci-dns",
        action="append",
        default=[],
        help="DNS server for cloud-init (repeatable).",
    )
    clone.add_argument(
        "--windows-oobe",
        action="store_true",
        help=(
            "Attach the optional Windows OOBE answer USB for a sysprepped "
            "Server 2025 template."
        ),
    )
    clone.add_argument(
        "--windows-admin-password",
        default="Vemu!Lab2026",
        help="OOBE answer password (used only with --windows-oobe).",
    )
    clone.add_argument("--uri", default="qemu:///system")
    clone.set_defaults(func=_cmd_clone)

    create = sub.add_parser(
        "create", help="Create a VM from an install ISO."
    )
    create.add_argument("--name", required=True)
    create.add_argument("--iso", required=True, help="Install ISO path.")
    create.add_argument("--dest", required=True, help="Directory for the VM chain.")
    create.add_argument(
        "--os",
        choices=["windows", "linux", "hypervisor", "other"],
        default="windows",
    )
    create.add_argument(
        "--os-variant",
        default="",
        help="Guest OS variant, e.g. fedora, ubuntu, freebsd, esxi-8-9, windows-server-2025.",
    )
    create.add_argument(
        "--arch",
        choices=["x86_64", "aarch64", "riscv64"],
        default=host_arch(),
        help="Guest architecture (default: host; other arches use TCG).",
    )
    create.add_argument(
        "--no-unattend",
        action="store_true",
        help="Use the interactive Windows installer (no answer file).",
    )
    create.add_argument(
        "--edition",
        choices=sorted(WINDOWS_EDITIONS),
        default="standard",
        help="Windows Server 2025 image (default: Desktop Experience).",
    )
    create.add_argument("--admin-password", default=DEFAULT_ADMIN_PASSWORD)
    create.add_argument("--computer-name")
    create.add_argument("--virtio-iso", help="virtio-win ISO (for qemu-ga).")
    create.add_argument(
        "--firmware", choices=["bios", "efi"], default="bios"
    )
    create.add_argument("--machine", default="q35")
    create.add_argument(
        "--disk-bus", choices=["virtio", "sata", "scsi", "ide", "nvme", "usb"]
    )
    create.add_argument(
        "--iso-bus",
        choices=["sata", "scsi", "ide", "usb"],
        help="Bus for the installer ISO/CD-ROM (default: sata).",
    )
    create.add_argument(
        "--nic-model",
        choices=["virtio", "e1000e", "e1000", "rtl8139", "vmxnet3", "pcnet"],
    )
    create.add_argument(
        "--video", choices=["virtio", "vga", "qxl", "cirrus", "vmvga", "bochs"]
    )
    create.add_argument(
        "--video-memory",
        type=int,
        choices=[64, 128, 256],
        default=None,
        help="VGA/QXL video memory in MiB (Windows default: 64).",
    )
    create.add_argument(
        "--hyperv",
        dest="hyperv",
        action="store_true",
        default=None,
        help="Enable Hyper-V enlightenments (default for x86_64 Windows).",
    )
    create.add_argument(
        "--no-hyperv",
        dest="hyperv",
        action="store_false",
        help="Disable Hyper-V enlightenments.",
    )
    create.add_argument(
        "--cpu-mode",
        choices=["host-passthrough", "host-model", "maximum"],
        default="host-passthrough",
    )
    create.add_argument(
        "--tpm",
        action="store_true",
        help="Add a TPM 2.0 device (Windows 11; pair with --firmware efi).",
    )
    create.add_argument(
        "--secure-boot",
        dest="secure_boot",
        action="store_true",
        default=None,
        help="Use the Secure Boot firmware (default for x86_64 UEFI).",
    )
    create.add_argument(
        "--no-secure-boot",
        dest="secure_boot",
        action="store_false",
        help="Use the standard firmware; needed for unsigned ISOs.",
    )
    create.add_argument("--disk-size", type=int, default=40, help="Disk size in GB.")
    create.add_argument("--memory", type=int, default=4096, help="Memory in MiB.")
    create.add_argument("--vcpus", type=int, default=2)
    create.add_argument("--network", default="default")
    create.add_argument("--network-config")
    create.add_argument(
        "--start",
        action="store_true",
        help="Start the VM after defining it.",
    )
    create.add_argument("--uri", default="qemu:///system")
    create.add_argument("--json", action="store_true")
    create.set_defaults(func=_cmd_create)

    rescue = sub.add_parser(
        "rescue",
        help="Start an External/read-only VM for manual rescue.",
    )
    rescue.add_argument("--domain", required=True)
    rescue.add_argument("--backup-dir")
    rescue.add_argument(
        "--stop",
        action="store_true",
        help="Request shutdown of a running External rescue VM.",
    )
    rescue.add_argument(
        "--force",
        action="store_true",
        help="Force off if a rescue shutdown does not complete.",
    )
    rescue.add_argument("--yes", action="store_true")
    rescue.add_argument("--uri", default="qemu:///system")
    rescue.add_argument("--json", action="store_true")
    rescue.set_defaults(func=_cmd_rescue)

    adopt = sub.add_parser(
        "adopt",
        help="Adopt an External VM as read-only or partial.",
    )
    adopt.add_argument("--domain", required=True)
    adopt.add_argument(
        "--mode",
        choices=["read-only", "partial"],
        default="partial",
    )
    adopt.add_argument("--backup-dir")
    adopt.add_argument("--yes", action="store_true")
    adopt.add_argument("--uri", default="qemu:///system")
    adopt.add_argument("--json", action="store_true")
    adopt.set_defaults(func=_cmd_adopt)

    migrate = sub.add_parser(
        "migrate",
        help="Flatten a VM into a v-EMU-managed disk chain.",
    )
    migrate.add_argument("--domain", required=True)
    migrate.add_argument("--mode", choices=["full"], default="full")
    migrate.add_argument("--dry-run", action="store_true")
    migrate.add_argument("--accept-snapshot-loss", action="store_true")
    migrate.add_argument(
        "--import-snapshots",
        action="store_true",
        help="Import libvirt/Cockpit external snapshots into the v2 chain.",
    )
    migrate.add_argument(
        "--new-firmware",
        action="store_true",
        help="Use v-EMU firmware and a fresh NVRAM file.",
    )
    migrate.add_argument("--yes", action="store_true")
    migrate.add_argument("--start", action="store_true")
    migrate.add_argument("--backup-dir")
    migrate.add_argument("--storage-config")
    migrate.add_argument("--uri", default="qemu:///system")
    migrate.add_argument("--json", action="store_true")
    migrate.set_defaults(func=_cmd_migrate)

    import_cmd = sub.add_parser(
        "import",
        help="Import an OVA appliance or an existing disk image as a VM.",
    )
    import_sub = import_cmd.add_subparsers(
        dest="import_kind", required=True
    )
    for import_kind in ("disk", "ova"):
        item = import_sub.add_parser(import_kind)
        item.add_argument("--file", required=True, dest="source")
        item.add_argument("--name", required=True)
        item.add_argument(
            "--preset",
            choices=["generic", "appliance", "vcsa"],
            default="generic",
        )
        item.add_argument("--network", default="NAT")
        item.add_argument("--vcpus", type=int)
        item.add_argument("--memory", type=int, dest="memory_mib")
        item.add_argument("--firmware", choices=["bios", "efi"])
        item.add_argument("--machine")
        item.add_argument(
            "--disk-bus",
            choices=["virtio", "sata", "scsi", "ide", "nvme"],
        )
        item.add_argument(
            "--nic-model",
            choices=["virtio", "e1000e", "e1000", "rtl8139", "pcnet"],
        )
        item.add_argument(
            "--video",
            choices=["virtio", "vga", "qxl", "cirrus", "vmvga", "bochs"],
        )
        item.add_argument(
            "--nic",
            action="append",
            help="NIC mapping network[:model[:mac]]; repeat per NIC.",
        )
        item.add_argument(
            "--serial",
            dest="serial_console",
            action="store_true",
            default=None,
        )
        item.add_argument(
            "--no-serial",
            dest="serial_console",
            action="store_false",
        )
        item.add_argument("--data-disk-gb", type=int, default=0)
        item.add_argument("--start", action="store_true")
        item.add_argument("--dry-run", action="store_true")
        item.add_argument("--yes", action="store_true")
        item.add_argument("--json", action="store_true")
        item.add_argument("--storage-config")
        item.add_argument("--network-config")
        item.add_argument("--uri", default="qemu:///system")
        item.set_defaults(func=_cmd_import)

    rename = sub.add_parser(
        "rename",
        help="Rename a VM and optionally its v-EMU disk chain.",
    )
    rename.add_argument("--domain", required=True)
    rename.add_argument("--new-name", required=True)
    rename.add_argument(
        "--no-disks",
        action="store_true",
        help="Rename only the libvirt domain, not the disk chain.",
    )
    rename.add_argument("--dry-run", action="store_true")
    rename.add_argument("--yes", action="store_true")
    rename.add_argument("--backup-dir")
    rename.add_argument("--storage-config")
    rename.add_argument("--uri", default="qemu:///system")
    rename.add_argument("--json", action="store_true")
    rename.set_defaults(func=_cmd_rename)

    seal = sub.add_parser("seal", help="Seal a shut-off VM as a template.")
    seal.add_argument("--domain", required=True)
    seal.add_argument("--dest", required=True, help="Template root directory.")
    seal.add_argument("--name", help="Template name (default: <domain>-base).")
    seal.add_argument("--description", default="")
    seal.add_argument(
        "--os-family", choices=["linux", "windows"], default=""
    )
    seal.add_argument("--sysprepped", action="store_true")
    seal.add_argument(
        "--accept-bitlocker",
        action="store_true",
        help=(
            "Confirm that BitLocker/FDE is off or suspended before sealing a "
            "Windows base image (required for Windows)."
        ),
    )
    seal.add_argument("--shutdown", action="store_true", help="Shut the VM down first.")
    seal.add_argument(
        "--in-place",
        action="store_true",
        help="Use the source disk as the immutable base; repoint the VM to an overlay.",
    )
    seal.add_argument("--uri", default="qemu:///system")
    seal.add_argument("--json", action="store_true")
    seal.set_defaults(func=_cmd_seal)

    templates = sub.add_parser("templates", help="List templates in a directory.")
    templates.add_argument("--root", required=True)
    templates.add_argument("--json", action="store_true")
    templates.set_defaults(func=_cmd_templates)

    template = sub.add_parser(
        "template", help="List, import, build and export templates."
    )
    template_sub = template.add_subparsers(
        dest="template_command", required=True
    )
    template_import = template_sub.add_parser(
        "import",
        aliases=["adopt"],
        help="Import or adopt a Packer/builder qcow2 as a template.",
    )
    template_import.add_argument(
        "--from", dest="source", required=True, help="Source qcow2 image."
    )
    template_import.add_argument("--dest", required=True, help="Template root.")
    template_import.add_argument("--name", required=True)
    template_import.add_argument("--description", default="")
    template_import.add_argument("--arch", default="x86_64")
    template_import.add_argument("--machine", default="q35")
    template_import.add_argument(
        "--firmware", choices=["bios", "efi"], default="bios"
    )
    template_import.add_argument(
        "--disk-bus", choices=["virtio", "sata", "scsi", "ide", "nvme"], default="virtio"
    )
    template_import.add_argument(
        "--video", choices=["virtio", "vga", "qxl"], default="virtio"
    )
    template_import.add_argument(
        "--video-memory",
        type=int,
        choices=[64, 128, 256],
        default=None,
        help="Video memory for VGA/QXL templates.",
    )
    template_import.add_argument("--nic-model", default="virtio")
    template_import.add_argument(
        "--secure-boot",
        action="store_true",
        help="Template requires UEFI Secure Boot when cloned.",
    )
    template_import.add_argument(
        "--tpm",
        action="store_true",
        help="Template has a TPM; clones receive a new TPM state.",
    )
    template_import.add_argument("--sysprepped", action="store_true")
    template_import.add_argument(
        "--os-family", choices=["linux", "windows"], default=""
    )
    template_import.add_argument("--json", action="store_true")
    template_import.set_defaults(func=_cmd_template_import)

    template_edit = template_sub.add_parser(
        "edit", help="Update a template's OS family/sysprep metadata."
    )
    template_edit.add_argument("--template", required=True)
    template_edit.add_argument(
        "--os-family", choices=["linux", "windows"], default=None
    )
    template_edit_sysprep = template_edit.add_mutually_exclusive_group()
    template_edit_sysprep.add_argument(
        "--sysprepped", dest="sysprepped", action="store_true"
    )
    template_edit_sysprep.add_argument(
        "--no-sysprepped", dest="sysprepped", action="store_false"
    )
    template_edit.set_defaults(sysprepped=None)
    template_edit.add_argument("--json", action="store_true")
    template_edit.set_defaults(func=_cmd_template_update)

    template_delete = template_sub.add_parser(
        "delete",
        aliases=["remove"],
        help="Delete a template after checking VM/clone references.",
    )
    template_delete.add_argument("--template", required=True)
    template_delete.add_argument("--json", action="store_true")
    template_delete.set_defaults(func=_cmd_template_delete)

    template_export = template_sub.add_parser(
        "export", help="Export a template base to qcow2/raw."
    )
    template_export.add_argument("--template", required=True)
    template_export.add_argument("--dest", required=True)
    template_export.add_argument(
        "--format", choices=["qcow2", "raw"], default="qcow2"
    )
    template_export.add_argument("--json", action="store_true")
    template_export.set_defaults(func=_cmd_template_export)

    template_list = template_sub.add_parser(
        "list", help="List templates in a directory."
    )
    template_list.add_argument("--root", required=True)
    template_list.add_argument("--json", action="store_true")
    template_list.set_defaults(func=_cmd_templates)

    template_build = template_sub.add_parser(
        "build", help="Build a template by sealing a shut-off VM."
    )
    template_build.add_argument("--domain", required=True)
    template_build.add_argument("--dest", required=True)
    template_build.add_argument("--name")
    template_build.add_argument("--description", default="")
    template_build.add_argument(
        "--os-family", choices=["linux", "windows"], default=""
    )
    template_build.add_argument("--sysprepped", action="store_true")
    template_build.add_argument("--shutdown", action="store_true")
    template_build.add_argument("--in-place", action="store_true")
    template_build.add_argument("--uri", default="qemu:///system")
    template_build.add_argument("--json", action="store_true")
    template_build.set_defaults(func=_cmd_seal)

    storage = sub.add_parser("storage", help="Manage storage locations.")
    storage_sub = storage.add_subparsers(dest="storage_command", required=True)

    storage_inspect = storage_sub.add_parser("inspect", help="Inspect a location.")
    storage_inspect.add_argument("--path", required=True)
    storage_inspect.add_argument(
        "--role", choices=["labs", "templates", "isos"], default="labs"
    )
    storage_inspect.add_argument("--json", action="store_true")
    storage_inspect.set_defaults(func=_cmd_storage_inspect)

    storage_add = storage_sub.add_parser("add", help="Prepare and register a location.")
    storage_add.add_argument("--path", required=True)
    storage_add.add_argument(
        "--role", choices=["labs", "templates", "isos"], default="labs"
    )
    storage_add.add_argument("--config")
    storage_add.add_argument("--dry-run", action="store_true")
    storage_add.add_argument("--json", action="store_true")
    storage_add.set_defaults(func=_cmd_storage_add)

    storage_repair = storage_sub.add_parser("repair", help="Repair a location.")
    storage_repair.add_argument("--path", required=True)
    storage_repair.add_argument(
        "--role", choices=["labs", "templates", "isos"], default="labs"
    )
    storage_repair.add_argument("--dry-run", action="store_true")
    storage_repair.add_argument(
        "--force-selinux",
        action="store_true",
        help=(
            "Re-apply SELinux labels recursively even when the top-level "
            "directory label already matches. Requires SELinux enabled and "
            "policycoreutils tools."
        ),
    )
    storage_repair.add_argument("--json", action="store_true")
    storage_repair.set_defaults(func=_cmd_storage_repair)

    storage_list = storage_sub.add_parser("list", help="List registered locations.")
    storage_list.add_argument("--config")
    storage_list.add_argument("--json", action="store_true")
    storage_list.set_defaults(func=_cmd_storage_list)

    storage_remove = storage_sub.add_parser(
        "remove", help="Remove a location from the registry."
    )
    storage_remove.add_argument("--path", required=True)
    storage_remove.add_argument("--config")
    storage_remove.add_argument("--json", action="store_true")
    storage_remove.set_defaults(func=_cmd_storage_remove)

    network = sub.add_parser(
        "network", aliases=["net"], help="Manage lab networks."
    )
    network_sub = network.add_subparsers(dest="network_command", required=True)

    network_list = network_sub.add_parser("list", help="List network profiles.")
    network_list.add_argument("--config")
    network_list.add_argument("--json", action="store_true")
    network_list.set_defaults(func=_cmd_network_list)

    network_status = network_sub.add_parser(
        "status", help="Show runtime network, bridge and guest status."
    )
    network_status.add_argument("--config")
    network_status.add_argument("--network-config")
    network_status.add_argument("--uri", default="qemu:///system")
    network_status.add_argument("--json", action="store_true")
    network_status.set_defaults(func=_cmd_network_status)

    network_add = network_sub.add_parser("add", help="Add a network profile.")
    network_add.add_argument("--name", required=True)
    network_add.add_argument("--mode", choices=["nat", "isolated", "bridge"], default="nat")
    network_add.add_argument(
        "--bridge",
        default="",
        help="Existing host bridge name for --mode bridge (e.g. br0).",
    )
    network_add.add_argument(
        "--subnet", default="auto", help="CIDR or 'auto' (default: auto)."
    )
    network_add.add_argument(
        "--dhcp",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="DHCP/DNS (default: on for NAT, off for isolated).",
    )
    network_add.add_argument("--dns-domain", default="vemu.local")
    network_add.add_argument("--description", default="")
    network_add.add_argument("--config")
    network_add.add_argument("--json", action="store_true")
    network_add.set_defaults(func=_cmd_network_add)

    network_remove = network_sub.add_parser(
        "remove", help="Remove a network profile."
    )
    network_remove.add_argument("--name", required=True)
    network_remove.add_argument(
        "--purge",
        action="store_true",
        help="Also stop and undefine the libvirt network.",
    )
    network_remove.add_argument("--config")
    network_remove.add_argument("--json", action="store_true")
    network_remove.set_defaults(func=_cmd_network_remove)

    network_apply = network_sub.add_parser(
        "apply", help="Reconcile profiles into libvirt and nft."
    )
    network_apply.add_argument("--dry-run", action="store_true")
    network_apply.add_argument("--config")
    network_apply.add_argument("--json", action="store_true")
    network_apply.set_defaults(func=_cmd_network_apply)

    network_plan = network_sub.add_parser(
        "plan", help="Show what apply would change."
    )
    network_plan.add_argument("--config")
    network_plan.add_argument("--json", action="store_true")
    network_plan.set_defaults(func=_cmd_network_plan)

    network_leases = network_sub.add_parser(
        "leases", help="Show DHCP reservations and active leases."
    )
    network_leases.add_argument("--network", required=True)
    network_leases.add_argument("--config")
    network_leases.add_argument("--uri", default="qemu:///system")
    network_leases.add_argument("--json", action="store_true")
    network_leases.set_defaults(func=_cmd_network_leases)

    network_restart = network_sub.add_parser(
        "restart", help="Restart a libvirt network to apply live changes."
    )
    network_restart.add_argument("--name", required=True)
    network_restart.add_argument("--config")
    network_restart.add_argument(
        "--force",
        action="store_true",
        help="Restart even when running VMs are attached (may drop their network).",
    )
    network_restart.add_argument("--json", action="store_true")
    network_restart.set_defaults(func=_cmd_network_restart)

    network_forward = network_sub.add_parser(
        "forward", help="Manage NAT port forwards."
    )
    forward_sub = network_forward.add_subparsers(
        dest="forward_command", required=True
    )
    forward_add = forward_sub.add_parser("add", help="Add a port forward.")
    forward_add.add_argument("--network", required=True)
    forward_add.add_argument("--protocol", choices=["tcp", "udp"], default="tcp")
    forward_add.add_argument("--host-port", type=int, required=True)
    forward_add.add_argument("--guest-ip", required=True)
    forward_add.add_argument("--guest-port", type=int, required=True)
    forward_add.add_argument("--description", default="")
    forward_add.add_argument("--config")
    forward_add.add_argument("--uri", default="qemu:///system")
    forward_add.add_argument("--json", action="store_true")
    forward_add.set_defaults(func=_cmd_network_forward_add)

    forward_remove = forward_sub.add_parser(
        "remove", help="Remove a port forward."
    )
    forward_remove.add_argument("--network", required=True)
    forward_remove.add_argument(
        "--protocol", choices=["tcp", "udp"], default="tcp"
    )
    forward_remove.add_argument("--host-port", type=int, required=True)
    forward_remove.add_argument("--config")
    forward_remove.add_argument("--json", action="store_true")
    forward_remove.set_defaults(func=_cmd_network_forward_remove)

    network_reservation = network_sub.add_parser(
        "reservation", help="Manage DHCP static reservations."
    )
    reservation_sub = network_reservation.add_subparsers(
        dest="reservation_command", required=True
    )
    reservation_add = reservation_sub.add_parser(
        "add", help="Reserve an IP for a MAC address."
    )
    reservation_add.add_argument("--network", required=True)
    reservation_add.add_argument("--mac", required=True)
    reservation_add.add_argument("--ip", required=True)
    reservation_add.add_argument("--hostname", default="")
    reservation_add.add_argument("--config")
    reservation_add.add_argument("--json", action="store_true")
    reservation_add.set_defaults(func=_cmd_network_reservation_add)

    reservation_remove = reservation_sub.add_parser(
        "remove", help="Remove a DHCP reservation."
    )
    reservation_remove.add_argument("--network", required=True)
    reservation_remove.add_argument("--mac", required=True)
    reservation_remove.add_argument("--config")
    reservation_remove.add_argument("--json", action="store_true")
    reservation_remove.set_defaults(func=_cmd_network_reservation_remove)

    network_attach = network_sub.add_parser(
        "attach", help="Attach a network interface to a VM."
    )
    network_attach.add_argument("--domain", required=True)
    network_attach.add_argument("--network", required=True)
    network_attach.add_argument("--model", default="virtio")
    network_attach.add_argument("--config")
    network_attach.add_argument("--json", action="store_true")
    network_attach.set_defaults(func=_cmd_network_attach)

    network_detach = network_sub.add_parser(
        "detach", help="Detach a network interface by MAC."
    )
    network_detach.add_argument("--domain", required=True)
    network_detach.add_argument("--mac", required=True)
    network_detach.add_argument("--json", action="store_true")
    network_detach.set_defaults(func=_cmd_network_detach)

    snapshot = sub.add_parser(
        "snapshot",
        help="Manage snapshots by VM (or legacy --chain snapshot).",
    )
    snapshot.add_argument("--chain", help="Legacy chain directory form.")
    snapshot.add_argument("--label", default="")
    snapshot.add_argument("--json", action="store_true")
    snapshot_sub = snapshot.add_subparsers(dest="snapshot_command")
    snapshot_create = snapshot_sub.add_parser(
        "create", help="Create an offline snapshot for a VM."
    )
    snapshot_create.add_argument("--domain", required=True)
    snapshot_create.add_argument("--label", default="")
    snapshot_create.add_argument("--description", default="")
    snapshot_create.add_argument("--uri", default="qemu:///system")
    snapshot_create.add_argument("--json", action="store_true")
    snapshot_create.set_defaults(func=_cmd_snapshot_create)
    snapshot_edit = snapshot_sub.add_parser(
        "edit", help="Update snapshot name/description."
    )
    snapshot_edit.add_argument("--domain", required=True)
    snapshot_edit.add_argument("--layer", required=True)
    snapshot_edit.add_argument("--label", default=None)
    snapshot_edit.add_argument("--description", default=None)
    snapshot_edit.add_argument("--uri", default="qemu:///system")
    snapshot_edit.add_argument("--json", action="store_true")
    snapshot_edit.set_defaults(func=_cmd_snapshot_edit)
    snapshot_list = snapshot_sub.add_parser(
        "list", help="List snapshot layers for a VM."
    )
    snapshot_list.add_argument("--domain", required=True)
    snapshot_list.add_argument("--uri", default="qemu:///system")
    snapshot_list.add_argument("--json", action="store_true")
    snapshot_list.set_defaults(func=_cmd_snapshot_list)
    snapshot_revert = snapshot_sub.add_parser(
        "revert", help="Make a layer active again."
    )
    snapshot_revert.add_argument("--domain", required=True)
    snapshot_revert.add_argument("--layer", required=True)
    snapshot_revert.add_argument("--uri", default="qemu:///system")
    snapshot_revert.add_argument("--json", action="store_true")
    snapshot_revert.set_defaults(func=_cmd_snapshot_revert)
    snapshot_delete = snapshot_sub.add_parser(
        "delete", help="Delete a snapshot layer."
    )
    snapshot_delete.add_argument("--domain", required=True)
    snapshot_delete.add_argument("--layer", required=True)
    snapshot_delete.add_argument("--force", action="store_true")
    snapshot_delete.add_argument("--uri", default="qemu:///system")
    snapshot_delete.add_argument("--json", action="store_true")
    snapshot_delete.set_defaults(func=_cmd_snapshot_delete)
    snapshot.set_defaults(func=_cmd_snapshot)

    revert = sub.add_parser("revert", help="Switch the active layer.")
    revert.add_argument("--chain", required=True)
    revert.add_argument("--layer", required=True)
    revert.add_argument("--json", action="store_true")
    revert.set_defaults(func=_cmd_revert)

    delete = sub.add_parser("delete-layer", help="Delete a snapshot layer.")
    delete.add_argument("--chain", required=True)
    delete.add_argument("--layer", required=True)
    delete.add_argument("--force", action="store_true")
    delete.add_argument("--json", action="store_true")
    delete.set_defaults(func=_cmd_delete)

    verify = sub.add_parser("verify", help="Check chain integrity.")
    verify.add_argument("--chain", required=True)
    verify.add_argument("--json", action="store_true")
    verify.set_defaults(func=_cmd_verify)

    media = sub.add_parser(
        "media", help="List, change or eject CD/DVD media."
    )
    media_sub = media.add_subparsers(dest="media_command", required=True)

    media_list = media_sub.add_parser("list", help="List CD/DVD drives.")
    media_list.add_argument("--domain", required=True)
    media_list.add_argument("--uri", default="qemu:///system")
    media_list.add_argument("--json", action="store_true")
    media_list.set_defaults(func=_cmd_media_list)

    media_change = media_sub.add_parser(
        "change", help="Insert or replace an ISO in a CD/DVD drive."
    )
    media_change.add_argument("--domain", required=True)
    media_change.add_argument("--path", required=True)
    media_change.add_argument("--target", help="Drive target, e.g. sdb.")
    media_change.add_argument(
        "--force",
        action="store_true",
        help="Force the live change even if the guest holds the tray.",
    )
    media_change.add_argument("--uri", default="qemu:///system")
    media_change.add_argument("--json", action="store_true")
    media_change.set_defaults(func=_cmd_media_change)

    media_eject = media_sub.add_parser(
        "eject", help="Eject the current CD/DVD media."
    )
    media_eject.add_argument("--domain", required=True)
    media_eject.add_argument("--target", help="Drive target, e.g. sdb.")
    media_eject.add_argument(
        "--force",
        action="store_true",
        help="Force the live eject even if the guest holds the tray.",
    )
    media_eject.add_argument("--uri", default="qemu:///system")
    media_eject.add_argument("--json", action="store_true")
    media_eject.set_defaults(func=_cmd_media_eject)

    config = sub.add_parser(
        "config", help="Show or update VM hardware."
    )
    config_sub = config.add_subparsers(
        dest="config_command", required=True
    )
    config_show = config_sub.add_parser("show", help="Show VM hardware.")
    config_show.add_argument("--domain", required=True)
    config_show.add_argument("--json", action="store_true")
    config_show.set_defaults(func=_cmd_config_show)

    config_drift = config_sub.add_parser(
        "drift", help="Compare a VM XML with its v-EMU baseline."
    )
    config_drift.add_argument("--domain", required=True)
    config_drift.add_argument("--uri", default="qemu:///system")
    config_drift.add_argument("--json", action="store_true")
    config_drift.set_defaults(func=_cmd_config_drift)

    config_baseline = config_sub.add_parser(
        "baseline", help="Pin the current XML as the drift baseline."
    )
    config_baseline.add_argument("--domain")
    config_baseline.add_argument("--yes", action="store_true")
    config_baseline.add_argument("--uri", default="qemu:///system")
    config_baseline.add_argument("--json", action="store_true")
    config_baseline.set_defaults(func=_cmd_config_baseline)

    config_caps = config_sub.add_parser(
        "caps", help="Show QEMU/libvirt hardware capabilities."
    )
    config_caps.add_argument("--machine")
    config_caps.add_argument(
        "--arch",
        choices=["x86_64", "aarch64", "riscv64"],
        default=host_arch(),
    )
    config_caps.add_argument("--uri", default="qemu:///system")
    config_caps.add_argument("--json", action="store_true")
    config_caps.set_defaults(func=_cmd_config_caps)

    config_disk_add = config_sub.add_parser(
        "disk-add", help="Add a data disk to a VM."
    )
    config_disk_add.add_argument("--domain", required=True)
    config_disk_add.add_argument("--path", required=True)
    config_disk_add.add_argument("--size", type=int, default=10)
    config_disk_add.add_argument(
        "--disk-bus", choices=["virtio", "sata", "scsi", "ide", "nvme"], default="virtio"
    )
    config_disk_add.add_argument(
        "--format", choices=["qcow2", "raw"], default="qcow2"
    )
    config_disk_add.add_argument("--no-create", action="store_true")
    config_disk_add.add_argument("--uri", default="qemu:///system")
    config_disk_add.add_argument("--json", action="store_true")
    config_disk_add.set_defaults(func=_cmd_config_disk_add)

    config_disk_remove = config_sub.add_parser(
        "disk-remove", help="Remove a data disk from a VM."
    )
    config_disk_remove.add_argument("--domain", required=True)
    config_disk_remove.add_argument("--source", required=True)
    config_disk_remove.add_argument("--delete", action="store_true")
    config_disk_remove.add_argument("--uri", default="qemu:///system")
    config_disk_remove.add_argument("--json", action="store_true")
    config_disk_remove.set_defaults(func=_cmd_config_disk_remove)

    config_disk_resize = config_sub.add_parser(
        "disk-resize", help="Grow a data disk (online when running)."
    )
    config_disk_resize.add_argument("--domain", required=True)
    config_disk_resize.add_argument("--source", required=True)
    config_disk_resize.add_argument("--size", type=int, required=True)
    config_disk_resize.add_argument("--uri", default="qemu:///system")
    config_disk_resize.add_argument("--json", action="store_true")
    config_disk_resize.set_defaults(func=_cmd_config_disk_resize)

    config_serial_add = config_sub.add_parser(
        "serial-add", help="Add a pty serial/console pair (VM stopped)."
    )
    config_serial_add.add_argument("--domain", required=True)
    config_serial_add.add_argument("--uri", default="qemu:///system")
    config_serial_add.add_argument("--json", action="store_true")
    config_serial_add.set_defaults(func=_cmd_config_serial_add)

    config_tpm_add = config_sub.add_parser(
        "tpm-add", help="Add a TPM device (emulator backend)."
    )
    config_tpm_add.add_argument("--domain", required=True)
    config_tpm_add.add_argument(
        "--version", choices=["1.2", "2.0"], default="2.0"
    )
    config_tpm_add.add_argument("--uri", default="qemu:///system")
    config_tpm_add.add_argument("--json", action="store_true")
    config_tpm_add.set_defaults(func=_cmd_config_tpm_add)

    config_tpm_remove = config_sub.add_parser(
        "tpm-remove", help="Remove the TPM device."
    )
    config_tpm_remove.add_argument("--domain", required=True)
    config_tpm_remove.add_argument("--uri", default="qemu:///system")
    config_tpm_remove.add_argument("--json", action="store_true")
    config_tpm_remove.set_defaults(func=_cmd_config_tpm_remove)

    config_usb_add = config_sub.add_parser(
        "usb-add", help="Add a qemu-xhci USB controller."
    )
    config_usb_add.add_argument("--domain", required=True)
    config_usb_add.add_argument("--uri", default="qemu:///system")
    config_usb_add.add_argument("--json", action="store_true")
    config_usb_add.set_defaults(func=_cmd_config_usb_add)

    config_audio_add = config_sub.add_parser(
        "audio-add", help="Add an HDA/AC97 audio device."
    )
    config_audio_add.add_argument("--domain", required=True)
    config_audio_add.add_argument("--uri", default="qemu:///system")
    config_audio_add.add_argument("--json", action="store_true")
    config_audio_add.set_defaults(func=_cmd_config_audio_add)

    config_audio_remove = config_sub.add_parser(
        "audio-remove", help="Remove the audio device."
    )
    config_audio_remove.add_argument("--domain", required=True)
    config_audio_remove.add_argument("--uri", default="qemu:///system")
    config_audio_remove.add_argument("--json", action="store_true")
    config_audio_remove.set_defaults(func=_cmd_config_audio_remove)

    config_profile = config_sub.add_parser(
        "profile", help="Show or set the host resource scheduling profile."
    )
    config_profile.add_argument(
        "profile",
        nargs="?",
        choices=["standard", "high", "ultra"],
        default=None,
    )
    config_profile.add_argument("--uri", default="qemu:///system")
    config_profile.add_argument("--json", action="store_true")
    config_profile.set_defaults(func=_cmd_config_profile)

    config_set = config_sub.add_parser("set", help="Update VM hardware.")
    config_set.add_argument("--domain", required=True)
    config_set.add_argument("--memory", type=int)
    config_set.add_argument("--vcpus", type=int)
    config_set.add_argument(
        "--vcpus-max",
        type=int,
        help="Boot maximum vCPU count for live hotplug headroom.",
    )
    config_set.add_argument(
        "--cpu-mode", choices=["host-passthrough", "host-model", "maximum"]
    )
    config_set.add_argument("--machine")
    config_set.add_argument(
        "--video", choices=["virtio", "vga", "qxl", "cirrus", "vmvga", "bochs"]
    )
    config_set.add_argument(
        "--disk-bus", choices=["virtio", "sata", "scsi", "ide", "nvme"]
    )
    config_set.add_argument(
        "--nic-model",
        choices=["virtio", "e1000e", "e1000", "rtl8139", "vmxnet3", "pcnet"],
    )
    config_set.add_argument(
        "--os-type",
        choices=["windows", "linux", "hypervisor", "generic"],
        help="Guest OS badge used by the VM list (metadata only).",
    )
    config_set.add_argument("--json", action="store_true")
    config_set.set_defaults(func=_cmd_config_set)

    show = sub.add_parser("show", help="Show chain information.")
    show.add_argument("--chain", required=True)
    show.add_argument("--json", action="store_true")
    show.set_defaults(func=_cmd_show)

    identity = sub.add_parser(
        "identity", help="Reset hostname, machine-id and SSH host keys."
    )
    identity.add_argument("--chain", required=True)
    identity.add_argument("--hostname", required=True)
    identity.add_argument("--machine-id")
    identity.add_argument("--dry-run", action="store_true")
    identity.add_argument("--json", action="store_true")
    identity.set_defaults(func=_cmd_identity)

    doctor = sub.add_parser("doctor", help="Check host prerequisites.")
    doctor.add_argument("--uri", default="qemu:///system")
    doctor.add_argument("--config", help="Storage config path.")
    doctor.add_argument(
        "--repair",
        action="store_true",
        help="Run the storage repair plan for registered locations.",
    )
    doctor.add_argument(
        "--dry-run",
        action="store_true",
        help="With --repair, show the plan without applying it.",
    )
    doctor.add_argument("--json", action="store_true")
    doctor.set_defaults(func=_cmd_doctor)

    daemon = sub.add_parser("daemon", help="Manage the v-EMU daemon.")
    daemon_sub = daemon.add_subparsers(dest="daemon_command", required=True)

    serve = daemon_sub.add_parser("serve", help="Run vemud in the foreground.")
    serve.add_argument("--socket", default="/run/vemu/vemud.sock")
    serve.add_argument("--uri", default="qemu:///system")
    serve.add_argument("--config")
    serve.add_argument("--network-config")
    serve.add_argument("--group", default="vemu")
    serve.add_argument("--socket-mode", default="0660")
    serve.add_argument("--polkit", action="store_true")
    serve.set_defaults(func=_cmd_daemon_serve)

    ping = daemon_sub.add_parser("ping", help="Ping the daemon.")
    ping.add_argument("--socket", default=argparse.SUPPRESS)
    ping.add_argument("--json", action="store_true")
    ping.set_defaults(func=_cmd_daemon_ping)

    status = daemon_sub.add_parser("status", help="Show daemon status.")
    status.add_argument("--socket", default=argparse.SUPPRESS)
    status.add_argument("--json", action="store_true")
    status.set_defaults(func=_cmd_daemon_status)

    call = daemon_sub.add_parser("call", help="Call a daemon method (debug).")
    call.add_argument("--method", required=True)
    call.add_argument("--params", help="JSON object with method parameters.")
    call.add_argument("--socket", default=argparse.SUPPRESS)
    call.set_defaults(func=_cmd_daemon_call)

    ui = sub.add_parser("ui", help="Open the desktop shell.")
    ui.add_argument("--uri", default="qemu:///system")
    ui.add_argument("--dark", action="store_true")
    ui.add_argument("--light", action="store_true")
    ui.add_argument(
        "--no-mini-console",
        action="store_true",
        help="Disable the noVNC inspector preview for this run.",
    )
    ui.add_argument("--screenshot")
    ui.set_defaults(func=_cmd_ui)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except VemuError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
