"""Start-up check: the mower_sdk imported is the distribution the manifest requires.

navimow-sdk (upstream) and navimow-sdk-community both install the mower_sdk
package, and Home Assistant never uninstalls a requirement. Whichever was
installed last owns the files, so the distribution Home Assistant installed
for this integration and the package Python imports can differ: after the
upstream package was installed again, its files run under the community
distribution's name, and the community SDK's own warning cannot run at all.

This module imports nothing but mower_sdk (present in both editions),
packaging (which Home Assistant ships) and the standard library, so it can run
before any module of this integration that uses community-only names. PROBLEM
is the message setup refuses with, or None when the imported package is the
installed distribution's code, or a newer copy of it (a checkout installed in
editable mode and bumped since, whose recorded version lags the module's).
"""
from __future__ import annotations

import importlib.metadata
from collections.abc import Callable
from pathlib import Path

import mower_sdk
from packaging.version import InvalidVersion, Version

DISTRIBUTION = "navimow-sdk-community"


def _distribution_init() -> str | None:
    """Where the installed distribution's mower_sdk/__init__.py lives."""
    try:
        dist = importlib.metadata.distribution(DISTRIBUTION)
    except importlib.metadata.PackageNotFoundError:
        return None
    return str(dist.locate_file("mower_sdk/__init__.py"))


def _same_file(first: str | None, second: str | None) -> bool:
    if not first or not second:
        return False
    try:
        return Path(first).resolve() == Path(second).resolve()
    except OSError:
        return False


def _community_code(module: object) -> bool:
    """Whether the mower_sdk imported is the community edition's code: its
    facade restores a location record, which upstream's never has."""
    facade = getattr(module, "NavimowSDK", None)
    return callable(getattr(facade, "restore_location", None))


def _older(imported: str | None, installed: str) -> bool:
    """Whether the version imported is older than the distribution's metadata
    says: a stale copy in front on the path. A newer one is a checkout bumped
    since its editable install, and runs."""
    if imported is None:
        return True
    try:
        return Version(imported) < Version(installed)
    except InvalidVersion:
        return imported != installed


def check(
    version_of: Callable[[str], str] = importlib.metadata.version,
    distribution_init: Callable[[], str | None] = _distribution_init,
    module: object = mower_sdk,
) -> str | None:
    """None when mower_sdk is the installed navimow-sdk-community's code, else why not.

    The lookups are parameters so the tests can fake them. What is probed is
    what the integration needs: the distribution installed, the code the
    community edition's, and no older than the distribution says. A mismatch
    is a consistency failure, not a diagnosis: the message lists what was
    found and the causes that produce it.
    """
    imported = getattr(module, "__version__", None)
    location = getattr(module, "__file__", None)
    try:
        installed: str | None = version_of(DISTRIBUTION)
    except importlib.metadata.PackageNotFoundError:
        installed = None
    if installed is not None and _community_code(module) and not _older(imported, installed):
        return None

    found = (
        f"{DISTRIBUTION} {installed} is installed"
        if installed is not None
        else f"{DISTRIBUTION} is not installed"
    )
    message = (
        f"{found}, but the mower_sdk package imported is version {imported} from "
        f"{location}: the installation is inconsistent (another distribution's files, "
        "or a copy shadowing it on the path). "
    )
    if _same_file(location, distribution_init()):
        # The distribution's own files were overwritten, by the upstream
        # package installed after it.
        return message + (
            "Uninstall navimow-sdk and navimow-sdk-community, then restart Home "
            "Assistant, which installs navimow-sdk-community again."
        )
    directory = str(Path(location).parent) if location else "the mower_sdk copy"
    return message + (
        f"Remove or correct the copy in {directory} first: reinstalling the "
        "distributions leaves it in front on the path. Then restart Home Assistant."
    )


PROBLEM: str | None = check()
