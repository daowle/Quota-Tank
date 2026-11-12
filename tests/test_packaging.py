"""MSIX 매니페스트 만들기. makeappx·서명 없이 확인할 수 있는 부분만 본다."""
import sys
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import build_msix  # noqa: E402

NS = {"f": "http://schemas.microsoft.com/appx/manifest/foundation/windows10",
      "uap10": "http://schemas.microsoft.com/appx/manifest/uap/windows10/10",
      "desktop": "http://schemas.microsoft.com/appx/manifest/desktop/windows10",
      "rescap": "http://schemas.microsoft.com/appx/manifest/foundation/windows10/restrictedcapabilities"}


class MsixManifestTest(unittest.TestCase):
    def test_version_is_four_part_with_zero_revision(self):
        self.assertEqual(build_msix.msix_version("v1.2.3"), "1.2.3.0")
        self.assertEqual(build_msix.msix_version("0.0.1"), "0.0.1.0")
        for bad in ("1.2", "v1.2.3-beta", "1.2.70000", "", None):
            with self.assertRaises(ValueError):
                build_msix.msix_version(bad)

    def test_publisher_is_escaped_and_must_be_a_subject(self):
        xml = build_msix.render_manifest("v2.0.0", 'CN=Dev, O="A & B, Inc.", C=KR', "A & B")
        root = ET.fromstring(xml)
        identity = root.find("f:Identity", NS)
        self.assertEqual(identity.get("Publisher"), 'CN=Dev, O="A & B, Inc.", C=KR')
        self.assertEqual(identity.get("Version"), "2.0.0.0")
        self.assertEqual(root.find("f:Properties/f:PublisherDisplayName", NS).text, "A & B")
        for bad in ("Dev", "CN=x\nO=y", 'CN=x"><Capability Name="broadFileSystemAccess"/>'):
            with self.assertRaises(ValueError):
                build_msix.render_manifest("v1.0.0", bad, "x")

    def test_manifest_keeps_integrity_startup_task_and_minimal_capabilities(self):
        root = ET.fromstring(build_msix.render_manifest("v1.0.0", "CN=Dev", "Dev"))
        self.assertEqual(root.find("f:Properties/uap10:PackageIntegrity/uap10:Content", NS).get("Enforcement"), "on")
        task = root.find(".//desktop:StartupTask", NS)
        self.assertEqual(task.get("Enabled"), "false")  # 설치만으로 자동 실행되지 않는다
        caps = [c.get("Name") for c in root.find("f:Capabilities", NS)]
        self.assertEqual(caps, ["runFullTrust"])  # broadFileSystemAccess 같은 추가 권한을 요구하지 않는다


if __name__ == "__main__":
    unittest.main()
