"""Start-up check: the mower_sdk imported is the distribution the manifest requires.

navimow-sdk (upstream) and navimow-sdk-community both install the mower_sdk
package, and Home Assistant never uninstalls a requirement. Whichever was
installed last owns the files, so the distribution Home Assistant installed
for this integration and the package Python imports can differ: after the
upstream package was installed again, its files run under the community
distribution's name, and the community SDK's own warning cannot run at all.

This module imports nothing but mower_sdk (present in both editions) and the
standard library, so it can run before any module of this integration that
uses community-only names. PROBLEM is the message setup refuses with, or
None when the imported package is the installed distribution.
"""
from __future__ import annotations

import importlib.metadata
from collections.abc import Callable
from pathlib import Path

import mower_sdk

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


def check(
    version_of: Callable[[str], str] = importlib.metadata.version,
    distribution_init: Callable[[], str | None] = _distribution_init,
    module: object = mower_sdk,
) -> str | None:
    """None when mower_sdk is the installed navimow-sdk-community, else why not.

    The lookups are parameters so the tests can fake them. A mismatch is a
    consistency failure, not a diagnosis: the message lists what was found and
    the causes that produce it.
    """
    imported = getattr(module, "__version__", None)
    location = getattr(module, "__file__", None)
    try:
        installed: str | None = version_of(DISTRIBUTION)
    except importlib.metadata.PackageNotFoundError:
        installed = None
    if installed is not None and installed == imported:
        return None

    found = (
        f"{DISTRIBUTION} {installed} is installed"
        if installed is not None
        else f"{DISTRIBUTION} is not installed"
    )
    message = (
        f"{found}, but the mower_sdk package imported is version {imported} from "
        f"{location}: the installation is inconsistent (another distribution's files, "
        "a copy shadowing it on the path, or an editable checkout). "
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
