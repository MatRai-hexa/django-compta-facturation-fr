"""Registre des plateformes agréées disponibles (complété par INVOICING["PLATFORMS"])."""
from django.utils.module_loading import import_string

from .. import conf
from .base import Event, Platform, PlatformError, Received
from .manual import ManualPlatform
from .sandbox import SandboxPlatform

BUILTIN = [ManualPlatform, SandboxPlatform]


def registry():
    classes = list(BUILTIN) + [import_string(path) for path in conf.get("PLATFORMS")]
    return {cls.code: cls for cls in classes}


def choices():
    return [(code, cls.label) for code, cls in registry().items()]


def current():
    from ..models import InvoicingSettings

    code = InvoicingSettings.get().platform
    cls = registry().get(code, ManualPlatform)
    return cls(conf.get("PLATFORM_OPTIONS").get(cls.code, {}))


__all__ = ["Event", "Platform", "PlatformError", "Received", "choices", "current", "registry"]
