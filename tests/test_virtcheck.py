from __future__ import annotations

import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from vemu.virtcheck import (
    _exit_code,
    _shell_output,
    format_status,
    virtualization_status,
)


class VirtualizationStatusTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.cpuinfo = self.root / "cpuinfo"
        self.kvm = self.root / "kvm"
        self.kvm.write_text("", encoding="utf-8")
        self.kvm.chmod(0o660)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _write_cpuinfo(self, flags: str, vendor: str = "GenuineIntel") -> None:
        self.cpuinfo.write_text(
            f"vendor_id\t: {vendor}\nflags\t\t: {flags}\n",
            encoding="utf-8",
        )

    def _status(self, flags: str, *, kvm: bool = True, **kwargs):
        self._write_cpuinfo(flags)
        return virtualization_status(
            cpuinfo=self.cpuinfo,
            kvm_path=self.kvm if kvm else self.root / "missing-kvm",
            arch=kwargs.pop("arch", "x86_64"),
            virtualization=kwargs.pop("virtualization", "none"),
            **kwargs,
        )

    def test_intel_vmx_with_kvm_is_ok(self) -> None:
        status = self._status("fpu vmx ssse3")
        self.assertTrue(status["ok"])
        self.assertEqual(status["reason"], "ok")
        self.assertTrue(status["cpu_virtualization"])
        self.assertEqual(status["cpu_label"], "Intel VT-x")
        self.assertEqual(status["cpu_flags"], ["vmx"])
        self.assertIn("available", status["kvm_detail"])

    def test_amd_svm_with_kvm_is_ok(self) -> None:
        self._write_cpuinfo("fpu svm", vendor="AuthenticAMD")
        status = virtualization_status(
            cpuinfo=self.cpuinfo, kvm_path=self.kvm, virtualization="none"
        )
        self.assertTrue(status["ok"])
        self.assertEqual(status["cpu_label"], "AMD-V (SVM)")
        self.assertEqual(status["cpu_vendor"], "AMD")

    def test_disabled_bios_reports_cpu_and_vt_d(self) -> None:
        status = self._status("fpu ssse3", kvm=False)
        self.assertFalse(status["ok"])
        self.assertEqual(status["reason"], "cpu-disabled")
        self.assertIn("BIOS/UEFI", status["hint"])
        self.assertIn("VT-d/IOMMU", status["hint"])
        self.assertIn("Intel VT-x", status["detail"])

    def test_nested_guest_hint(self) -> None:
        status = self._status(
            "fpu hypervisor", kvm=False, virtualization="kvm"
        )
        self.assertEqual(status["reason"], "cpu-disabled")
        self.assertIn("nested virtualization", status["hint"])

    def test_enabled_cpu_but_missing_device(self) -> None:
        status = self._status("fpu vmx", kvm=False)
        self.assertFalse(status["ok"])
        self.assertEqual(status["reason"], "kvm-missing")
        self.assertIn("CPU exposes Intel VT-x", status["detail"])
        self.assertIn("kvm_intel", status["hint"])
        self.assertIn("module", status["kvm_detail"])

    def test_device_without_cpu_flag_is_rejected(self) -> None:
        status = self._status("fpu ssse3", kvm=True)
        self.assertFalse(status["ok"])
        self.assertEqual(status["reason"], "cpu-disabled")
        self.assertIn("/dev/kvm exists", status["detail"])

    def test_kvm_permission_is_ok_with_warning(self) -> None:
        self._write_cpuinfo("fpu vmx")
        with mock.patch("vemu.virtcheck.os.access", return_value=False):
            status = virtualization_status(
                cpuinfo=self.cpuinfo,
                kvm_path=self.kvm,
                virtualization="none",
            )
        self.assertTrue(status["ok"])
        self.assertEqual(status["reason"], "kvm-permission")
        self.assertIn("not accessible", status["kvm_detail"])
        self.assertIn("kvm group", status["hint"])

    def test_arm_host_does_not_require_x86_flags(self) -> None:
        status = self._status("", kvm=True, arch="aarch64")
        self.assertTrue(status["ok"])
        self.assertFalse(status["x86"])
        self.assertEqual(status["reason"], "ok")
        self.assertIn("does not use x86", status["cpu_detail"])

    def test_arm_host_requires_kvm_device(self) -> None:
        status = self._status("", kvm=False, arch="aarch64")
        self.assertFalse(status["ok"])
        self.assertEqual(status["reason"], "kvm-missing")
        self.assertIn("aarch64", status["detail"])


class VirtualizationOutputTests(unittest.TestCase):
    def _status(self) -> dict:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cpuinfo = root / "cpuinfo"
            kvm = root / "kvm"
            cpuinfo.write_text("flags : fpu vmx\n", encoding="utf-8")
            kvm.write_text("", encoding="utf-8")
            return virtualization_status(
                cpuinfo=cpuinfo,
                kvm_path=kvm,
                arch="x86_64",
                virtualization="none",
            )

    def test_format_status_mentions_result_and_reason(self) -> None:
        text = format_status(self._status())
        self.assertIn("Result: OK", text)
        self.assertIn("/dev/kvm: available", text)

    def test_shell_output_is_sourceable(self) -> None:
        output = _shell_output(self._status())
        namespace: dict[str, str] = {}
        exec(output, namespace)
        self.assertEqual(namespace["VIRT_OK"], "1")
        self.assertEqual(namespace["VIRT_REASON"], "ok")
        self.assertEqual(namespace["VIRT_ARCH"], "x86_64")

    def test_shell_mode_always_exits_zero(self) -> None:
        from vemu import virtcheck

        with mock.patch.object(
            virtcheck, "virtualization_status", return_value=self._status()
        ), redirect_stdout(io.StringIO()):
            self.assertEqual(virtcheck.main(["--shell"]), 0)

    def test_exit_codes_distinguish_bios_and_device(self) -> None:
        missing = virtualization_status(
            cpuinfo="/dev/null",
            kvm_path="/tmp/definitely-not-a-kvm-device",
            arch="x86_64",
            virtualization="none",
        )
        self.assertEqual(_exit_code(missing, False), 2)
        self.assertEqual(_exit_code(missing, True), 2)
        enabled = dict(missing)
        enabled["reason"] = "kvm-missing"
        self.assertEqual(_exit_code(enabled, False), 3)
        self.assertEqual(_exit_code(enabled, True), 0)



class HostArchTests(unittest.TestCase):
    def test_domain_type_follows_host(self) -> None:
        from vemu.virtcheck import domain_type_for

        with mock.patch.dict("os.environ", {"VEMU_HOST_ARCH": "aarch64"}):
            self.assertEqual(domain_type_for("aarch64"), "kvm")
            self.assertEqual(domain_type_for("x86_64"), "qemu")
        with mock.patch.dict("os.environ", {"VEMU_HOST_ARCH": "x86_64"}):
            self.assertEqual(domain_type_for("x86_64"), "kvm")
            self.assertEqual(domain_type_for("aarch64"), "qemu")

    def test_performance_cpuset_picks_big_cores(self) -> None:
        from vemu.virtcheck import performance_cpuset

        with tempfile.TemporaryDirectory() as root:
            # GX10-like: X925 at 3.9 GHz on 5-9/15-19, A725 elsewhere.
            for cpu in range(20):
                freq = 3900000 if cpu in (*range(5, 10), *range(15, 20)) else 2808000
                path = Path(root, f"cpu{cpu}", "cpufreq")
                path.mkdir(parents=True)
                (path / "cpuinfo_max_freq").write_text(str(freq))
            with mock.patch.dict("os.environ", {"VEMU_HOST_ARCH": "aarch64"}):
                self.assertEqual(performance_cpuset(root), "5-9,15-19")
            with mock.patch.dict("os.environ", {"VEMU_HOST_ARCH": "x86_64"}):
                self.assertEqual(performance_cpuset(root), "")

    def test_arm_host_domain_uses_kvm_and_pins(self) -> None:
        import xml.etree.ElementTree as ET

        from vemu.domain import Disk, DomainSpec

        with mock.patch.dict("os.environ", {"VEMU_HOST_ARCH": "aarch64"}), \
                mock.patch("vemu.domain.performance_cpuset", return_value="5-9"):
            spec = DomainSpec(
                name="arm-kvm",
                arch="aarch64",
                machine="virt",
                firmware="efi",
                disks=[Disk(path="/tmp/arm-kvm.qcow2")],
            )
            root = ET.fromstring(spec.to_xml())
        self.assertEqual(root.get("type"), "kvm")
        self.assertEqual(root.find("cpu").get("mode"), "host-passthrough")
        self.assertEqual(root.find("vcpu").get("cpuset"), "5-9")
        self.assertIsNone(root.find("features/tcg"))


if __name__ == "__main__":
    unittest.main()
