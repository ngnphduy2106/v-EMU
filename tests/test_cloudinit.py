from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from vemu import service
from vemu.cloudinit import (
    CloudInitConfig,
    build_cloud_init_iso,
    render_meta_data,
    render_network_config,
    render_user_data,
)
from vemu.errors import ValidationError

QEMU_IMG = shutil.which("qemu-img")


class CloudInitRenderTests(unittest.TestCase):
    def test_meta_data(self) -> None:
        config = CloudInitConfig(
            hostname="node-1", instance_id="iid-test"
        )
        text = render_meta_data(config)
        self.assertIn("instance-id: \"iid-test\"", text)
        self.assertIn("local-hostname: \"node-1\"", text)

    def test_user_data(self) -> None:
        config = CloudInitConfig(
            hostname="node-1",
            user="lab",
            password="LabPass123",
            ssh_keys=["ssh-ed25519 AAAA test"],
            packages=["vim", "tcpdump"],
            runcmd=["systemctl enable sshd"],
        )
        text = render_user_data(config)
        self.assertIn("#cloud-config", text)
        self.assertIn("name: \"lab\"", text)
        self.assertIn("ssh-ed25519 AAAA test", text)
        self.assertIn("vim", text)
        self.assertIn("systemctl enable sshd", text)
        self.assertIn("chpasswd:", text)
        self.assertIn("ssh_pwauth: true", text)

    def test_user_data_requires_credentials(self) -> None:
        with self.assertRaises(ValidationError):
            render_user_data(CloudInitConfig(hostname="node-1"))

    def test_static_network_config(self) -> None:
        config = CloudInitConfig(
            hostname="node-1",
            password="LabPass123",
            mac="52:54:00:aa:bb:cc",
            static_ip="192.168.50.11/24",
            gateway="192.168.50.1",
            dns=["1.1.1.1"],
        )
        text = render_network_config(config)
        self.assertIsNotNone(text)
        self.assertIn("macaddress: \"52:54:00:aa:bb:cc\"", text)
        self.assertIn("192.168.50.11/24", text)
        self.assertIn("via: \"192.168.50.1\"", text)

    def test_static_requires_mac(self) -> None:
        with self.assertRaises(ValidationError):
            CloudInitConfig(
                hostname="node-1",
                password="LabPass123",
                static_ip="192.168.50.11/24",
            ).validate()

    def test_build_iso(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = CloudInitConfig(
                hostname="node-1", password="LabPass123"
            )
            iso = build_cloud_init_iso(tmp, config)
            self.assertTrue(iso.is_file())
            self.assertGreater(iso.stat().st_size, 0)
            self.assertTrue((Path(tmp) / "cloud-init" / "meta-data").is_file())
            self.assertTrue(
                (Path(tmp) / "cloud-init" / "user-data").is_file()
            )


@unittest.skipUnless(QEMU_IMG, "qemu-img is required")
class CloudInitCloneTests(unittest.TestCase):
    def test_clone_with_seed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            base = root / "base.qcow2"
            subprocess.run(
                [QEMU_IMG, "create", "-f", "qcow2", str(base), "8M"],
                check=True,
                capture_output=True,
            )
            key = root / "id.pub"
            key.write_text("ssh-ed25519 AAAA test", encoding="utf-8")
            result = service.clone_create(
                str(base),
                str(root / "labs"),
                count=1,
                name="node-{{n}}",
                cloud_init=True,
                cloud_init_ssh_key_file=str(key),
                cloud_init_packages=["vim"],
                define=False,
            )
            item = result["created"][0]
            self.assertTrue(Path(item["cloud_init_iso"]).is_file())
            self.assertFalse(item["defined"])

    def test_clone_cloud_init_requires_credentials(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            base = root / "base.qcow2"
            subprocess.run(
                [QEMU_IMG, "create", "-f", "qcow2", str(base), "8M"],
                check=True,
                capture_output=True,
            )
            with self.assertRaises(ValidationError):
                service.clone_create(
                    str(base),
                    str(root / "labs"),
                    count=1,
                    name="node-{{n}}",
                    cloud_init=True,
                    define=False,
                )

    def test_static_ip_requires_define(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            base = root / "base.qcow2"
            subprocess.run(
                [QEMU_IMG, "create", "-f", "qcow2", str(base), "8M"],
                check=True,
                capture_output=True,
            )
            with self.assertRaises(ValidationError):
                service.clone_create(
                    str(base),
                    str(root / "labs"),
                    count=1,
                    name="node-{{n}}",
                    cloud_init=True,
                    cloud_init_password="LabPass123",
                    cloud_init_ip="192.168.50.11/24",
                    define=False,
                )

    def test_cli_parser(self) -> None:
        from vemu.cli import build_parser

        args = build_parser().parse_args(
            [
                "clone",
                "--template",
                "/tmp/template",
                "--dest",
                "/tmp/labs",
                "--cloud-init",
                "--ci-user",
                "student",
                "--ci-package",
                "vim",
                "--ci-package",
                "tcpdump",
                "--ci-ip",
                "192.168.50.11/24",
            ]
        )
        self.assertTrue(args.cloud_init)
        self.assertEqual(args.ci_user, "student")
        self.assertEqual(args.ci_package, ["vim", "tcpdump"])
        self.assertEqual(args.ci_ip, "192.168.50.11/24")


if __name__ == "__main__":
    unittest.main()
