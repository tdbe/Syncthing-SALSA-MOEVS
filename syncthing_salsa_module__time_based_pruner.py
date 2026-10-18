# -*- coding: utf-8 -*-
"""
Time-based pruner for the Syncthing .stversions folder.

Lists every file in the archive (flat, including subfolders) and deletes
all files older than DAYS, by creation time.
Windows : st_ctime IS the creation time.
macOS   : st_birthtime (real creation time).
Linux   : no birthtime in os.stat -> st_ctime (inode change time; the
          same-volume move into .stversions sets it to the archive
          moment, which is the ordering we want anyway).

DAYS <= 0 disables the age check without disabling the pruner. Never drops
the archive below MIN_KEEP_FILES files. Names are not parsed at all.

Settings (from the 'KEY = VALUE' file whose path the main script passes in):
    ENABLED, DAYS, MIN_KEEP_FILES
"""

import os
import time

# Fallback defaults, used only if the settings file is missing/incomplete.
DEFAULT_ENABLED = True # opt-in: flip to true in the settings file
DEFAULT_DAYS = 400
DEFAULT_MIN_KEEP_FILES = 1


# ---- settings-file helpers (intentionally duplicated per pruner) ----
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

def creation_time(st):
    """Best available 'archived at' timestamp, cross-platform.
    Windows : st_ctime IS the creation time.
    macOS   : st_birthtime (real creation time).
    Linux   : no birthtime in os.stat -> st_ctime (inode change time; the
              same-volume move into .stversions sets it to the archive
              moment, which is the ordering we want anyway)."""
    return getattr(st, 'st_birthtime', st.st_ctime)

def prune(version_dir, settings_path='', dry_run=False, log=None):
    """Delete files older than DAYS. Returns (deleted, freed_bytes)."""
    log = log if log else (lambda message, level='': None)

    settings = load_settings(settings_path) if settings_path else {}
    if settings is None:
        log('WARN  time: settings file not found: %s (using defaults)' % settings_path)
        settings = {}
    enabled  = _get_bool(settings, 'ENABLED', DEFAULT_ENABLED, log)
    days     = _get_int(settings, 'DAYS', DEFAULT_DAYS, log)
    min_keep = _get_int(settings, 'MIN_KEEP_FILES', DEFAULT_MIN_KEEP_FILES, log)
    _warn_unknown(settings, ('ENABLED', 'DAYS', 'MIN_KEEP_FILES'), 'time', log)

    if not enabled:
        log('  time: disabled', 'verbose')
        return 0, 0
    if days <= 0:
        log('  time: DAYS = %d (<= 0) - nothing to do' % days, 'verbose')
        return 0, 0

    cutoff = time.time() - days * 86400
    files = []   # [(path, size, ctime), ...]
    for dirpath, _dirnames, filenames in os.walk(version_dir):
        for name in filenames:
            p = os.path.join(dirpath, name)
            try:
                st = os.stat(p)
            except OSError:
                continue
            files.append((p, st.st_size, creation_time(st)))

    old = sorted((f for f in files if f[2] < cutoff), key=lambda f: (f[2], f[0]))
    log('  time: %d file(s), %d older than %d day(s)' % (len(files), len(old), days), 'verbose')

    deleted = freed = 0
    for p, size, _ct in old:
        if (len(files) - deleted) <= min_keep:
            log('  time: minimum number of files to keep reached - stopping', 'verbose')
            break
        if dry_run:
            log('  time: would delete %s' % p, 'verbose')
        else:
            try:
                os.remove(p)
            except OSError as exc:                         # in use / gone already
                log('WARN  time: could not delete %s: %s' % (p, exc))
                continue
            log('  time: deleted %s' % p, 'verbose')
        deleted += 1
        freed   += size
    return deleted, freed