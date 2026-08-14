import json
import struct
from pathlib import Path

from app.main import BASE_DIR, templates


def png_size(path: Path) -> tuple[int, int]:
    data = path.read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    return struct.unpack(">II", data[16:24])


def test_manifest_has_installable_app_metadata():
    manifest = json.loads((BASE_DIR / "static" / "manifest.webmanifest").read_text())
    assert manifest["id"] == "/"
    assert manifest["start_url"] == "/"
    assert manifest["display"] == "standalone"
    assert any(icon["purpose"] == "maskable" for icon in manifest["icons"])


def test_install_icons_have_expected_sizes():
    assert png_size(BASE_DIR / "static" / "icon-192.png") == (192, 192)
    assert png_size(BASE_DIR / "static" / "icon-512.png") == (512, 512)
    assert png_size(BASE_DIR / "static" / "icon-maskable-512.png") == (512, 512)
    assert png_size(BASE_DIR / "static" / "apple-touch-icon.png") == (180, 180)


def test_base_template_links_manifest_and_apple_icon():
    source = templates.env.loader.get_source(templates.env, "base.html")[0]
    assert 'rel="manifest"' in source
    assert 'rel="apple-touch-icon"' in source
