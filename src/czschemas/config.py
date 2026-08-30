"""Site configuration.  Everything about *what* to publish lives in the manifest tree."""

from __future__ import annotations

import dataclasses
import tomllib
from pathlib import Path

from . import lint
from .model import ValidationError


@dataclasses.dataclass(frozen=True)
class Config:
    base_url: str
    custom_domain: bool
    linters: dict[str, list[str]]

    @classmethod
    def load(cls, path: Path) -> Config:
        raw = tomllib.loads(path.read_text())
        site = raw.get("site", {})
        base_url = site.get("base_url")
        if not isinstance(base_url, str) or not base_url.startswith("https://"):
            raise ValidationError("manifest site.base_url must be an HTTPS URL")
        custom_domain = site.get("custom_domain", False)
        if not isinstance(custom_domain, bool):
            raise ValidationError("manifest site.custom_domain must be a boolean")
        return cls(base_url.rstrip("/"), custom_domain, lint.load(raw.get("lint")))
