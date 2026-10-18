# -*- coding: utf-8 -*-
"""
Global size-cap pruner for the Syncthing .stversions folder.

Lists every file in the archive (flat, including subfolders), sums their
sizes, and if the total exceeds MAX_SIZE, deletes the OLDEST files (by
file creation time) one by one until the total is back under the cap.
Names are not parsed here at all - a file is whatever its size and creation
date say it is. When ENABLED = false, nothing is ever deleted, but the
archive size is still measured (so the summary line stays accurate).

Windows : st_ctime IS the creation time.
macOS   : st_birthtime (real creation time).
Linux   : no birthtime in os.stat -> st_ctime (inode change time; the
          same-volume move into .stversions sets it to the archive
          moment, which is the ordering we want anyway).

Settings (from the 'KEY = VALUE' file whose path the main script passes in):
    ENABLED, MAX_SIZE, MIN_KEEP_FILES
"""

import os
import re

# Fallback defaults, used only if the settings file is missing/incomplete.
DEFAULT_ENABLED        = True
DEFAULT_MAX_SIZE       = '20GB'
DEFAULT_MIN_KEEP_FILES = 1 # keep at least this many files in the .stversions folder even if it's larger than the MAX_SIZE. Doesn't identify version chains, it's not "x files per version chain" it's x files per total files in .stversions (you can use the version-based modules for that).


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

def parse_size(value):
    """'20GB' / '500MB' / '1.5TB' / '2000000000' -> bytes."""
    m = re.match(r'^(\d+(?:\.\d+)?)\s*(PB|TB|GB|MB|KB|B)?$', str(value).strip(), re.IGNORECASE)
    if not m:
        raise ValueError('cannot parse size %r (use 20GB, 500MB, 1.5TB, 2000000000, ...)' % value)
    mult = {'B': 1, 'KB': 1024**1, 'MB': 1024**2,
            'GB': 1024**3, 'TB': 1024**4, 'PB': 1024**5}[(m.group(2) or 'B').upper()]
    return int(round(float(m.group(1)) * mult))


def fmt_size(n):
    for unit, div in (('PB', 1024**5), ('TB', 1024**4), ('GB', 1024**3),
                      ('MB', 1024**2), ('KB', 1024)):
        if n >= div:
            return '%.2f %s' % (n / div, unit)
    return '%d B' % n
    
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
    """Enforce MAX_SIZE. Returns (deleted, freed, total_after, count_after)."""
    log = log if log else (lambda message, level='': None)

    settings = load_settings(settings_path) if settings_path else {}
    if settings is None:
        log('WARN  size: settings file not found: %s (using defaults)' % settings_path)
        settings = {}
    enabled  = _get_bool(settings, 'ENABLED', DEFAULT_ENABLED, log)
    max_str  = settings.get('MAX_SIZE', DEFAULT_MAX_SIZE)
    min_keep = _get_int(settings, 'MIN_KEEP_FILES', DEFAULT_MIN_KEEP_FILES, log)
    _warn_unknown(settings, ('ENABLED', 'MAX_SIZE', 'MIN_KEEP_FILES'), 'size', log)

    files = [] # [(path, size, ctime), ...]
    for dirpath, _dirnames, filenames in os.walk(version_dir):
        for name in filenames:
            p = os.path.join(dirpath, name)
            try:
                st = os.stat(p)
            except OSError:
                continue
            files.append((p, st.st_size, creation_time(st)))
    total = sum(f[1] for f in files)
    log('  size: %s in %d file(s), limit %s%s'
        % (fmt_size(total), len(files), max_str, '' if enabled else ' (disabled)'), 'verbose')

    if not enabled:
        return 0, 0, total, len(files)
    try:
        max_bytes = parse_size(max_str)
    except ValueError as exc:
        log('ERROR size: %s - no size pruning this run' % exc)
        return 0, 0, total, len(files)

    deleted = freed = 0
    if total > max_bytes:
        files.sort(key=lambda f: (f[2], f[0])) # oldest creation time first
        for p, size, _ct in files:
            if total <= max_bytes:
                break
            if (len(files) - deleted) <= min_keep:
                log('  size: minimum number of files to keep reached - stopping', 'verbose')
                break
            if dry_run:
                log('  size: would delete %s (%s)' % (p, fmt_size(size)), 'verbose')
            else:
                try:
                    os.remove(p)
                except OSError as exc: # in use / gone already
                    log('WARN  size: could not delete %s: %s' % (p, exc))
                    continue
                log('  size: deleted %s (%s)' % (p, fmt_size(size)), 'verbose')
            deleted += 1
            freed   += size
            total   -= size
        if total > max_bytes:
            log('WARN  size: archive still %s over limit - only %d file(s) left'
                % (fmt_size(total - max_bytes), len(files) - deleted))

    return deleted, freed, total, len(files) - deleted