"""Plex path -> container path translation.

Plex reports paths from its own filesystem view. When Plex runs in a different
container or on a different host, those paths mean nothing here, and every
file-derived fact silently becomes uncomputable. Radarr and Sonarr both ship a
remote-path-mapping feature for exactly this reason.

Three outcomes, because each needs a different fix:

    mapped    a prefix matched and the file is really there
    missing   a prefix matched but the file isn't there  -> wrong local side,
              or Plex is holding a stale entry
    unmapped  no prefix matched                          -> add a mapping
"""
import hashlib
import os
from dataclasses import dataclass
from pathlib import PurePosixPath

from backend.models.settings import PathMapping

MAPPED = "mapped"
MISSING = "missing"
UNMAPPED = "unmapped"
UNKNOWN = "unknown"


@dataclass(frozen=True)
class MappedFile:
    status: str
    local_path: str | None
    size: int | None
    mtime: int | None
    fingerprint: str | None


def _is_windows_path(path: str) -> bool:
    return "\\" in path or (len(path) > 1 and path[1] == ":")


def translate(plex_path: str, mappings: list[PathMapping]) -> str | None:
    """Longest-prefix match, case-insensitive when the Plex side looks like Windows.

    With no mappings configured we identity-map, which is the common case where
    Plexlection and Plex see the same filesystem.
    """
    if not plex_path:
        return None
    if not mappings:
        return plex_path

    for mapping in sorted(mappings, key=lambda m: len(m.plex), reverse=True):
        win = _is_windows_path(mapping.plex)
        prefix = mapping.plex.rstrip("\\/")
        haystack, needle = (plex_path, prefix)
        if win:
            haystack, needle = plex_path.lower(), prefix.lower()

        sep = "\\" if win else "/"
        if haystack == needle:
            return mapping.local
        if haystack.startswith(needle + sep):
            rest = plex_path[len(prefix):].lstrip("\\/").replace("\\", "/")
            return str(PurePosixPath(mapping.local) / rest) if rest else mapping.local

    return None


def fingerprint(local_path: str, size: int, mtime: int, deep: bool = False) -> str:
    """Identity of a file's contents for cache invalidation.

    Keyed on (path, size, mtime) and never the inode: this library lives on a
    mergerfs union, which does not keep inodes stable across a rebalance.

    `deep` additionally mixes the first and last 1MiB, catching a replacement
    that preserved both size and mtime. Costs a seek and 2MiB per item.
    """
    h = hashlib.sha1(f"{local_path}|{size}|{mtime}".encode("utf-8", "replace"))
    if deep and size > 0:
        chunk = 1024 * 1024
        try:
            with open(local_path, "rb") as fh:
                h.update(fh.read(chunk))
                if size > chunk * 2:
                    fh.seek(-chunk, os.SEEK_END)
                    h.update(fh.read(chunk))
        except OSError:
            pass  # fall back to the metadata-only fingerprint
    return h.hexdigest()


def resolve(plex_path: str | None, mappings: list[PathMapping], deep: bool = False) -> MappedFile:
    """Translate and stat in one step. Blocking — call inside to_thread."""
    if not plex_path:
        return MappedFile(UNKNOWN, None, None, None, None)

    local = translate(plex_path, mappings)
    if local is None:
        return MappedFile(UNMAPPED, None, None, None, None)

    try:
        st = os.stat(local)
    except OSError:
        # With no mappings configured we identity-map, so a stat failure means
        # "you haven't told us where these files are" — not "your mapping is
        # wrong". Reporting that as `missing` sends the user looking for a
        # deleted file when what they actually need is a mapping, and it hides
        # the prefix from the Add-mapping helper.
        return MappedFile(UNMAPPED if not mappings else MISSING, local, None, None, None)

    size, mtime = st.st_size, int(st.st_mtime)
    return MappedFile(MAPPED, local, size, mtime, fingerprint(local, size, mtime, deep))


def unmapped_prefixes(plex_paths: list[str], mappings: list[PathMapping]) -> dict[str, int]:
    """Directory prefixes no mapping covers, with counts.

    This is what makes the Settings UI actionable: instead of "1,834 items are
    unmapped", it can say "/data/movies (1,834)" with a button that pre-fills it.

    The suggestion is the **longest common directory** across the unmapped
    paths, not a fixed depth. A fixed depth is wrong in both directions: too
    shallow and the prefix has no clean container equivalent (suggesting
    `/srv/media` when the mount is the `Videos` directory inside it), too deep
    and it suggests a per-title directory. The common ancestor is the mount
    point by construction.
    """
    unmapped: list[str] = []
    identity = not mappings
    for path in plex_paths:
        # When nothing is configured every path "translates" to itself, so the
        # translate() check would report no prefixes at all — exactly when the
        # user most needs one suggested.
        if not identity and translate(path, mappings) is not None:
            continue
        unmapped.append(path)

    if not unmapped:
        return {}

    # Split into posix/windows families; they can't share a prefix.
    families: dict[bool, list[str]] = {}
    for path in unmapped:
        families.setdefault(_is_windows_path(path), []).append(path)

    counts: dict[str, int] = {}
    for is_win, paths in families.items():
        sep = "\\" if is_win else "/"
        # Drop the filename; we're after a directory.
        dirs = [p.rsplit(sep, 1)[0] for p in paths]
        split = [[c for c in d.split(sep) if c] for d in dirs]

        common: list[str] = []
        for parts in zip(*split):
            first = parts[0]
            match = all(
                (p.lower() == first.lower()) if is_win else (p == first) for p in parts
            )
            if not match:
                break
            common.append(first)

        if not common:
            # Nothing shared — fall back to the top-level component of each.
            for parts in split:
                prefix = (sep if paths[0].startswith(sep) else "") + sep.join(parts[:1])
                counts[prefix] = counts.get(prefix, 0) + 1
            continue

        prefix = sep.join(common)
        if paths[0].startswith(sep):
            prefix = sep + prefix
        counts[prefix] = len(paths)

    return counts
