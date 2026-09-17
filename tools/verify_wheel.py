"""Inspect the actual wheel, including every local script/style referenced by its UI."""
from __future__ import annotations

import argparse
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlsplit
from zipfile import ZipFile


class Assets(HTMLParser):
    def __init__(self):
        super().__init__()
        self.paths = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        value = attrs.get("src") if tag == "script" else attrs.get("href") if tag == "link" else None
        if value and not urlsplit(value).scheme and not value.startswith("//"):
            self.paths.append(urlsplit(value).path.lstrip("/"))


def verify(path: Path) -> None:
    with ZipFile(path) as wheel:
        names = set(wheel.namelist())
        required = ["fpulse/frontend_dist/index.html", "fpulse/seed_data/orders.csv",
                    "fpulse/static/swagger-ui/swagger-ui-bundle.js", "fpulse/CHANGELOG.md"]
        for name in required:
            if name not in names:
                raise ValueError(f"Missing wheel asset: {name}")
        for prefix, suffix in [("fpulse/connectors/manifests/", ".json"), ("fpulse/docs/", ".md")]:
            if not any(n.startswith(prefix) and n.endswith(suffix) for n in names):
                raise ValueError(f"Missing wheel asset group: {prefix}")
        parser = Assets()
        parser.feed(wheel.read(required[0]).decode("utf-8"))
        if not any(p.endswith(".js") for p in parser.paths):
            raise ValueError("UI contains no local JavaScript entry point")
        for asset in parser.paths:
            if "fpulse/frontend_dist/" + asset not in names:
                raise ValueError(f"UI references missing wheel asset: {asset}")
    print(f"Wheel assets verified: {path.name}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("wheel", type=Path)
    verify(parser.parse_args().wheel)
