from __future__ import annotations

import json
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PLUGIN_DIR_NAME = "astrbot_plugin_yysls_news"
VERSION = "0.1.13"
ROOT_FILES = (
    "metadata.yaml",
    "main.py",
    "_conf_schema.json",
    "requirements.txt",
    "README.md",
)
PACKAGE_DIRS = ("plugin_core", "pages")
REQUIRED_ARCHIVE_FILES = (
    f"{PLUGIN_DIR_NAME}/metadata.yaml",
    f"{PLUGIN_DIR_NAME}/main.py",
    f"{PLUGIN_DIR_NAME}/_conf_schema.json",
    f"{PLUGIN_DIR_NAME}/requirements.txt",
    f"{PLUGIN_DIR_NAME}/pages/manage/index.html",
    f"{PLUGIN_DIR_NAME}/pages/manage/app.js",
    f"{PLUGIN_DIR_NAME}/plugin_core/store.py",
    f"{PLUGIN_DIR_NAME}/plugin_core/renderer.py",
)


def main() -> None:
    for filename in ROOT_FILES:
        if not (ROOT / filename).is_file():
            raise SystemExit(f"Required plugin file is missing: {filename}")
    json.loads((ROOT / "_conf_schema.json").read_text(encoding="utf-8"))
    if f"version: v{VERSION}" not in (ROOT / "metadata.yaml").read_text(encoding="utf-8"):
        raise SystemExit("Plugin metadata version does not match the archive version")

    files = [ROOT / filename for filename in ROOT_FILES]
    for directory in PACKAGE_DIRS:
        folder = ROOT / directory
        if not folder.is_dir():
            raise SystemExit(f"Required plugin directory is missing: {directory}")
        files.extend(
            path
            for path in folder.rglob("*")
            if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc"
        )

    output_dir = ROOT / "dist"
    output_dir.mkdir(parents=True, exist_ok=True)
    archive_path = output_dir / f"{PLUGIN_DIR_NAME}-v{VERSION}.zip"
    with zipfile.ZipFile(
        archive_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9
    ) as archive:
        for path in sorted(files):
            relative = path.relative_to(ROOT).as_posix()
            archive.write(path, f"{PLUGIN_DIR_NAME}/{relative}")

    with zipfile.ZipFile(archive_path, "r") as archive:
        names = set(archive.namelist())
    missing = [name for name in REQUIRED_ARCHIVE_FILES if name not in names]
    if missing:
        archive_path.unlink(missing_ok=True)
        raise SystemExit(f"Plugin archive validation failed: missing {missing}")
    print(archive_path)


if __name__ == "__main__":
    main()
