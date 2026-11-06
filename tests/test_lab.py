from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from vemu import lab
from vemu.errors import ValidationError

LAB_YAML = """
version: 1
lab: test-lab
dest: ./labs
templates_root: ./templates
networks:
  mgmt: { mode: nat, subnet: auto }
  server: { mode: isolated, subnet: 192.168.50.0/24, dhcp: false }
vms:
  node1:
    template: fedora-cloud
    cpus: 2
    memory: 2G
    networks: [mgmt, server]
    ip: { server: 192.168.50.11 }
    cloud_init:
      user: lab
      ssh_key: ~/.ssh/id_ed25519.pub
      packages: [vim, tcpdump]
"""


class LabTests(unittest.TestCase):
    def test_load_spec(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "lab.yaml"
            path.write_text(LAB_YAML, encoding="utf-8")
            spec = lab.load_lab(path)
            self.assertEqual(spec.name, "test-lab")
            self.assertEqual(spec.dest, (Path(tmp) / "labs").resolve())
            self.assertEqual(
                spec.templates_root, (Path(tmp) / "templates").resolve()
            )
            self.assertEqual(list(spec.networks), ["mgmt", "server"])
            self.assertEqual(spec.vms["node1"]["cpus"], 2)

    def test_memory(self) -> None:
        self.assertEqual(lab._memory_mib("2G"), 2048)
        self.assertEqual(lab._memory_mib("512M"), 512)
        self.assertEqual(lab._memory_mib(4096), 4096)
        with self.assertRaises(ValidationError):
            lab._memory_mib("many")

    def test_dry_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "lab.yaml"
            path.write_text(LAB_YAML, encoding="utf-8")
            result = lab.lab_up(path, dry_run=True)
            self.assertTrue(result["dry_run"])
            self.assertIn("network mgmt: plan", result["actions"])
            self.assertIn("node1: create from", result["actions"][-1])

    def test_missing_lab(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(Exception):
                lab.load_lab(Path(tmp) / "missing.yaml")

    def test_cli_parser(self) -> None:
        from vemu.cli import build_parser

        parser = build_parser()
        up = parser.parse_args(["up", "lab.yaml", "--dry-run"])
        self.assertEqual(up.func.__name__, "_cmd_up")
        self.assertTrue(up.dry_run)
        down = parser.parse_args(["down", "lab.yaml", "--force"])
        self.assertEqual(down.func.__name__, "_cmd_down")
        status = parser.parse_args(["status", "lab.yaml"])
        self.assertEqual(status.func.__name__, "_cmd_lab_status")
        exported = parser.parse_args(
            ["lab", "export", "--dest", "/tmp/bundle.tar.gz"]
        )
        self.assertEqual(exported.func.__name__, "_cmd_lab_export")
        imported = parser.parse_args(
            ["lab", "import", "--bundle", "/tmp/b.tar.gz", "--dest", "/tmp/x"]
        )
        self.assertEqual(imported.func.__name__, "_cmd_lab_import")
        reset = parser.parse_args(
            ["lab", "reset", "--lab", "lab.yaml", "--vm", "node1", "--force"]
        )
        self.assertEqual(reset.func.__name__, "_cmd_lab_reset")
        self.assertEqual(reset.vm, ["node1"])
        self.assertTrue(reset.force)
        destroy = parser.parse_args(
            [
                "lab",
                "destroy",
                "--lab",
                "lab.yaml",
                "--force",
                "--keep-networks",
            ]
        )
        self.assertEqual(destroy.func.__name__, "_cmd_lab_destroy")
        self.assertTrue(destroy.force)
        self.assertTrue(destroy.keep_networks)

    def test_reset_validates_selection(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "lab.yaml"
            path.write_text(LAB_YAML, encoding="utf-8")
            with self.assertRaises(ValidationError):
                lab.lab_reset(path, [])
            with self.assertRaises(ValidationError):
                lab.lab_reset(path, ["node2"])

    def test_bundle_with_templates(self) -> None:
        from vemu.template import Template

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            lab_dir = root / "lab"
            lab_dir.mkdir()
            template_dir = root / "templates" / "base"
            template_dir.mkdir(parents=True)
            base = template_dir / "base.qcow2"
            base.write_bytes(b"qcow2")
            Template(
                name="base", base=str(base), description="test"
            ).save(template_dir)
            (lab_dir / "lab.yaml").write_text(
                LAB_YAML.replace("fedora-cloud", "base").replace(
                    "./templates", str(root / "templates")
                ),
                encoding="utf-8",
            )
            bundle = root / "lab.tar.gz"
            exported = lab.lab_export(
                lab_dir / "lab.yaml", bundle, with_templates=True
            )
            self.assertTrue(Path(exported["bundle"]).is_file())
            dest = root / "imported"
            imported = lab.lab_import(
                bundle, dest, templates_root=root / "imported-templates"
            )
            self.assertEqual(imported["templates_imported"], 1)
            self.assertTrue((dest / "lab.yaml").is_file())
            self.assertTrue(
                (root / "imported-templates" / "base" / "base.qcow2").is_file()
            )
            self.assertIn("dest:", (dest / "lab.yaml").read_text())



if __name__ == "__main__":
    unittest.main()
