from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from vemu.yamlmini import YAMLError, load, loads


LAB = """
# sample
lab: rhcsa-practice
arch: x86_64
dest: /home/VM/vemu-labs
network:
  mgmt:   { mode: nat, subnet: auto, forwards: ["2222:22"] }
  server: { mode: isolated, subnet: 192.168.10.0/24 }
vms:
  node1:
    template: fedora-44-x86
    cpus: 2
    memory: 2G
    networks: [mgmt, server]
    ip: { server: 192.168.10.11 }
    cloud_init:
      user: lab
      ssh_key: ~/.ssh/id_ed25519.pub
      packages: [vim, tcpdump]
  node2:
    template: fedora-44-x86
    cpus: 1
    memory: 1G
    networks:
      - server
    cloud_init:
      user: lab
      password: "LabPass123"
      runcmd:
        - systemctl enable sshd
        - echo hello # inline comment
"""


class YamlMiniTests(unittest.TestCase):
    def test_load_lab(self) -> None:
        data = loads(LAB)
        self.assertEqual(data["lab"], "rhcsa-practice")
        self.assertEqual(data["dest"], "/home/VM/vemu-labs")
        self.assertEqual(data["network"]["mgmt"]["mode"], "nat")
        self.assertEqual(
            data["network"]["mgmt"]["forwards"], ["2222:22"]
        )
        self.assertEqual(
            data["vms"]["node1"]["networks"], ["mgmt", "server"]
        )
        self.assertEqual(
            data["vms"]["node1"]["cloud_init"]["packages"],
            ["vim", "tcpdump"],
        )
        self.assertEqual(
            data["vms"]["node2"]["cloud_init"]["runcmd"],
            ["systemctl enable sshd", "echo hello"],
        )

    def test_scalars(self) -> None:
        data = loads(
            "a: 1\nb: true\nc: null\nd: 'x y'\ne: \"z\"\nf: 1.5\n"
        )
        self.assertEqual(data, {
            "a": 1,
            "b": True,
            "c": None,
            "d": "x y",
            "e": "z",
            "f": 1.5,
        })

    def test_list_of_mappings(self) -> None:
        data = loads(
            "items:\n  - name: one\n    value: 1\n  - name: two\n    value: 2\n"
        )
        self.assertEqual(
            data["items"],
            [{"name": "one", "value": 1}, {"name": "two", "value": 2}],
        )

    def test_bad_indent(self) -> None:
        with self.assertRaises(YAMLError):
            loads("a:\n   b: 1\n  c: 2\n")

    def test_load_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "lab.yaml"
            path.write_text(LAB, encoding="utf-8")
            self.assertEqual(load(path)["lab"], "rhcsa-practice")

    def test_dump_roundtrip(self) -> None:
        from vemu.yamlmini import dumps

        data = {
            "lab": "x",
            "dest": "/tmp/labs",
            "networks": {"mgmt": {"mode": "nat", "dhcp": True}},
            "vms": {
                "node1": {
                    "template": "base",
                    "networks": ["mgmt"],
                    "cloud_init": {"user": "lab", "packages": ["vim"]},
                }
            },
        }
        self.assertEqual(loads(dumps(data)), data)



if __name__ == "__main__":
    unittest.main()
