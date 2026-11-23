"""Test-only upstream transport fixtures; production parsers and UI stay real."""
from contextlib import contextmanager
from unittest.mock import patch


def _release_series(kind):
    from arca import engine, updates
    current = updates.parse_version(engine.Engine().version)
    if current == (0, 0):
        raise ValueError("bundled engine version is unavailable")
    major, minor = current
    versions = [f"{major}.{minor + 1:02d}"]
    if kind == "long":
        versions += [f"{major + 1}.{n:02d}" for n in range(1, 14)]
    elif kind != "short":
        raise ValueError(kind)
    return current, versions


def history_text(kind, series=None):
    current, versions = series or _release_series(kind)
    baseline = f"{current[0]}.{current[1]:02d}"
    return "\n\n".join(
        f"{version}  2026-10-22\n- Synthetic regression fixture CVE-2099-0001 security fix.\n"
        "- Synthetic release notes exercise the actual history parser and dialog layout."
        for version in reversed(versions)
    ) + f"\n\n{baseline}  2026-10-21\n- Baseline, excluded from the update span.\n"


@contextmanager
def upstream(kind):
    from arca import updates
    series = _release_series(kind)
    latest = series[1][-1]
    history = history_text(kind, series).encode()
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
