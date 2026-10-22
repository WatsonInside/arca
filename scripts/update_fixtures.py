"""Test-only upstream transport fixtures; production parsers and UI stay real."""
from contextlib import contextmanager
from unittest.mock import patch


def history_text(kind):
    versions = ["26.05"]
    if kind == "long":
        versions += [f"27.{n:02d}" for n in range(1, 14)]
    elif kind != "short":
        raise ValueError(kind)
    return "\n\n".join(
        f"{version}  2026-10-22\n- Synthetic regression fixture CVE-2099-0001 security fix.\n"
        "- Synthetic release notes exercise the actual history parser and dialog layout."
        for version in reversed(versions)
    ) + "\n\n26.04  2026-10-01\n- Baseline, excluded from the update span.\n"


@contextmanager
def upstream(kind):
    from arca import updates
    latest = "27.13" if kind == "long" else "26.05"
    history = history_text(kind).encode()
    page = (f'Download 7-Zip {latest} (2026-10-22) '
            f'<a href="a/7z{latest.replace(".", "")}-mac.tar.xz">macOS</a>').encode()

    def get(url, timeout=15.0):
        if url == updates.PAGE_URL:
            return page
        if url == updates.HISTORY_URL:
            return history
        raise AssertionError(f"Unexpected update fixture request: {url}")

    with patch.object(updates, "_get", get):
        yield
