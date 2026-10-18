# -*- coding: utf-8 -*-
"""
Per-version-chain max-version-number pruner for the Syncthing .stversions folder.

Assumes every archived file is named with Syncthing-style standard timestamp:

    <base>~<YYYYMMDD-hhmmss><.ext>        (e.g. myFile~20260614-180023.txt)
    and in case of sub-second clash, additional:
    <base>~<YYYYMMDD-hhmmss_1><.ext>        (e.g. myFile~20260614-180023_1.txt)
    <base>~<YYYYMMDD-hhmmss_2><.ext>        (e.g. myFile~20260614-180023_2.txt)

Groups all archived files into version chains by (subfolder + base + ext (without the timestamp)),
Sorts each chain by the ~ timestamp embedded in the name (the zero-padded stamp makes name order == chronological order), 
And deletes everything older than the newest KEEP_VERSIONS entries of that chain.

Note: Files whose names contain no '~' are not part of any chain and are left untouched.

Settings (from the 'KEY = VALUE' file whose path the main script passes in):
    ENABLED, KEEP_VERSIONS
"""

import os

# Fallback defaults, used only if the settings file is missing/incomplete.
DEFAULT_ENABLED = True
DEFAULT_KEEP_VERSIONS = 2


# ---- settings-file helpers (intentionally duplicated per pruner so each
# ---- pruner file stays standalone) ----
def load_settings(path):
    if not path or not os.path.exists(path):
        return None
    settings = {}
    try:
        with open(path, 'r', encoding='utf-8-sig') as fh:
            for line in fh:
                line = line.split('#', 1)[0].strip()
                if not line or '=' not in line:
                    continue
                key, _, value = line.partition('=')
                value = value.strip()
                if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
                    value = value[1:-1]
                settings[key.strip().upper()] = value
    except OSError:
        return None
    return settings


def _get_bool(settings, key, default, log):
    value = settings.get(key)
    if value is None:
        return default
    if value.lower() in ('1', 'true', 'yes', 'on'):
        return True
    if value.lower() in ('0', 'false', 'no', 'off'):
        return False
    log('WARN  invalid boolean for %s: %r - using %s' % (key, value, default))
    return default


def _get_int(settings, key, default, log):
    value = settings.get(key)
    if value is None:
        return default
    try:
        return int(value)
    except ValueError:
        log('WARN  invalid number for %s: %r - using %s' % (key, value, default))
        return default


def _warn_unknown(settings, known, name, log):
    for key in sorted(set(settings) - set(known)):
        log('WARN  %s: unknown setting "%s" ignored' % (name, key))

# ----------------------------------------------------------------------

def split_version_name(name):
    """
    'base~20260614-180023.txt' -> ('base', '20260614-180023', '.txt')
    'base~20260614-180023' -> ('base', '20260614-180023', '')
    'base~20260614-180023_1' -> ('base', '20260614-180023_1', '')
    '.nomedia~20260614-180023' -> ('.nomedia', '20260614-180023', '')
    
    Returns None if there is no '~'.
    """
    tilde = name.rfind('~')
    if tilde < 0:
        return None
    dot = name.rfind('.', tilde)
    if dot < 0:
        return name[:tilde], name[tilde + 1:], ''
    return name[:tilde], name[tilde + 1:dot], name[dot:]


def prune(version_dir, settings_path='', dry_run=False, log=None):
    """Enforce KEEP_VERSIONS per chain. Returns (deleted, freed_bytes)."""
    log = log if log else (lambda message, level='': None)

    settings = load_settings(settings_path) if settings_path else {}
    if settings is None:
        log('WARN  per-file: settings file not found: %s (using defaults)' % settings_path)
        settings = {}
    enabled = _get_bool(settings, 'ENABLED', DEFAULT_ENABLED, log)
    keep    = _get_int(settings, 'KEEP_VERSIONS', DEFAULT_KEEP_VERSIONS, log)
    _warn_unknown(settings, ('ENABLED', 'KEEP_VERSIONS'), 'per-file', log)

    if not enabled:
        log('  per-file: disabled', 'verbose')
        return 0, 0
    if keep < 0:
        keep = 0

    chains = {}   # (dir, base, ext) -> [(stamp, path, size), ...]
    for dirpath, _dirnames, filenames in os.walk(version_dir):
        for name in filenames:
            parsed = split_version_name(name)
            if parsed is None:
                continue                                   # not a version file - leave alone
            base, stamp, ext = parsed
            path = os.path.join(dirpath, name)
            try:
                size = os.stat(path).st_size
            except OSError:
                continue
            chains.setdefault((dirpath, base, ext), []).append((stamp, path, size))

    deleted = freed = 0
    for (dirpath, base, ext), entries in sorted(chains.items()):
        if len(entries) <= keep:
            continue
        entries.sort(key=lambda e: e[0])                   # oldest stamp first
        for stamp, path, size in entries[:len(entries) - keep]:
            if dry_run:
                log('  per-file: would delete %s' % path, 'verbose')
            else:
                try:
                    os.remove(path)
                except OSError as exc:                     # in use / gone already
                    log('WARN  per-file: could not delete %s: %s' % (path, exc))
                    continue
                log('  per-file: deleted %s' % path, 'verbose')
            deleted += 1
            freed   += size
    return deleted, freed