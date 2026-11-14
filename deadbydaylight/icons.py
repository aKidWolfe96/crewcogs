"""Finds the official game icons you exported with FModel.

Put the export in `deadbydaylight/Icons/` (or `assets/`), keeping FModel's
folders. Each game asset path like
`/Game/UI/UMGAssets/Icons/Perks/England/iconPerks_bloodWarden` is matched by
its exact relative path first, then by file name. Anything missing returns
None and the cards fall back to the drawn badges.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

ROOTS = ("Icons", "icons", "assets", "Assets")
EXTS = (".png", ".webp", ".jpg", ".jpeg")
STRIP = ("/game/ui/umgassets/icons/", "/game/ui/icons/", "ui/umgassets/icons/", "ui/icons/", "/game/")


class IconLibrary:
    def __init__(self, base: Path):
        self.base = base
        self.by_path: dict[str, Path] = {}
        self.by_name: dict[str, list[Path]] = {}
        self._bytes: dict[Path, Optional[bytes]] = {}
        self.scan()

    def scan(self):
        self.by_path.clear()
        self.by_name.clear()
        self._bytes.clear()
        for root_name in ROOTS:
            root = self.base / root_name
            if not root.is_dir():
                continue
            for dirpath, _dirs, files in os.walk(root):
                for f in files:
                    stem, ext = os.path.splitext(f)
                    if ext.lower() not in EXTS:
                        continue
                    full = Path(dirpath) / f
                    rel = full.relative_to(root).with_suffix("").as_posix().lower()
                    self.by_path.setdefault(rel, full)
                    self.by_name.setdefault(stem.lower(), []).append(full)

    @property
    def count(self) -> int:
        return len(self.by_path)

    def locate(self, asset: Optional[str]) -> Optional[Path]:
        if not asset:
            return None
        p = asset.strip().lower()
        for prefix in STRIP:
            if p.startswith(prefix):
                p = p[len(prefix):]
                break
        p = os.path.splitext(p)[0].lstrip("/")
        if p in self.by_path:
            return self.by_path[p]
        hits = self.by_name.get(os.path.basename(p))
        return hits[0] if hits else None

    def get(self, asset: Optional[str]) -> Optional[bytes]:
        path = self.locate(asset)
        if path is None:
            return None
        if path not in self._bytes:
            try:
                self._bytes[path] = path.read_bytes()
            except OSError:
                self._bytes[path] = None
        return self._bytes[path]
