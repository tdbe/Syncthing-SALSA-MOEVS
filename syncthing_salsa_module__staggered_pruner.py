# -*- coding: utf-8 -*-
"""
Staggered-versioning pruner for the Syncthing .stversions folder: a customizable
re-implementation of Syncthing's builtin "Staggered File Versioning", with
configurable intervals and per-file size caps.

Syncthing's builtin staggered versioning keeps per-file version chains, with the very rigid and weird time rules described here: https://docs.syncthing.net/users/versioning.html#staggered-file-versioning

Here the fixed intervals are replaced by YOUR list of intervals (in
days, (also supports 0.5 days)) and every rule additionally gets a per-file-chain size budget.

Operates per file version chain (subfolder + base name + extension, like the
version-count pruner). Age of a version = time between NOW and the
~YYYYMMDD-hhmmss(_N) timestamp embedded in its name (Syncthing-standard, taken
from the file's mtime at versioning time). 

Files whose names contain no valid '~' timestamp are left untouched.

The rules, per chain (newest to oldest):

  1. NEWEST GROUP: the newest KEEP_NEWEST versions are always kept regardless
     of the interval rules below - but only as many of them as fit within
     TOTAL_SIZE_OF_NEWEST_VERSIONS (counted from the newest version), never
     fewer than KEEP_AT_LEAST_NEWEST.
  2. INTERVALS: INTERVALS_DAYS is a comma-separated list of cumulative day
     boundaries, e.g. '0.5, 60' defines the bands 'up to 12h old' and
     'between 12h and 60d old'. The OLDEST version landing in each band is
     kept (one per band, oldest preserved).
  3. Tail end (until maximum age): versions older than the last interval boundary
     are bucketed every EVERY_DAYS days and the oldest version of each bucket
     is kept. EVERY_DAYS <= 0 disables tail staggering (versions are kept based on 
     the last interval), but MAX_AGE still applies unless it is 0. 
     If MAX_AGE > 0, anything older than MAX_AGE days is deleted outright; 
     MAX_AGE = 0 means nothing is deleted based on MAX_AGE.
     Positive INTERVALS_DAYS entries and EVERY_DAYS below MIN_STAGGER_DAYS
     (a setting, default ~1 hour) are clamped to MIN_STAGGER_DAYS. 
     (MIN_STAGGER_DAYS <= 0 disables the floor.)
  4. OLDEST SIZE BUDGET: all versions kept by rules 2+3 (the "oldest group")
     must fit within TOTAL_SIZE_OF_OLDEST_VERSIONS per version-chain; oldest 
     survivors no longer fitting the size budget are deleted, but the chain doesn't 
     go below KEEP_AT_LEAST_OLDEST versions. 
     If the selection comes up short of KEEP_AT_LEAST_OLDEST (e.g. everything
     aged out past MAX_AGE), the newest not-yet-kept versions are promoted to
     satisfy the minimum.

  Everything else in the chain is deleted. So even if a single version is bigger
  than both size budgets (and both KEEP_AT_LEAST_* are 0), NO version of that
  file is kept at all.

Settings (from the 'KEY = VALUE' file whose path the main script passes in):
    ENABLED, KEEP_NEWEST, TOTAL_SIZE_OF_NEWEST_VERSIONS, KEEP_AT_LEAST_NEWEST,
    INTERVALS_DAYS, MIN_STAGGER_DAYS, EVERY_DAYS, MAX_AGE,
    TOTAL_SIZE_OF_OLDEST_VERSIONS, KEEP_AT_LEAST_OLDEST
"""

import datetime as dt
import os
import re

# Fallback defaults, used only if the settings file is missing/incomplete.
DEFAULT_ENABLED = False # opt-in: flip to true in the settings file
DEFAULT_KEEP_NEWEST = 2 # rule 1: always keep this many newest versions per file (subject to its size budget, floored by KEEP_AT_LEAST_NEWEST)
DEFAULT_TOTAL_SIZE_OF_NEWEST_VERSIONS = '' # '' = no size limit on the newest group; else e.g. 2GB
DEFAULT_KEEP_AT_LEAST_NEWEST = 1 # never drop the newest group below this many versions, even past its size budget
DEFAULT_INTERVALS_DAYS = '' # comma-separated cumulative day boundaries, e.g. '0.5, 60'
DEFAULT_EVERY_DAYS = 30.0 # tail staggering bucket in days; <= 0 disables tail staggering
DEFAULT_MAX_AGE = 400 # days; 0 = keep forever whatever survives the intervals and tail
DEFAULT_TOTAL_SIZE_OF_OLDEST_VERSIONS = '' # '' = no size limit on the interval/tail survivors group
DEFAULT_KEEP_AT_LEAST_OLDEST = 0 # never drop the oldest group below this many versions, even past its size budget

DEFAULT_MIN_STAGGER_DAYS = 0.04165 # ~1 hour (setting MIN_STAGGER_DAYS): positive INTERVALS_DAYS entries / EVERY_DAYS smaller than this are clamped up to it (version-name timestamps only have second resolution, and tiny tail buckets just bloat the archive). <= 0 disables the floor.


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


def _get_float(settings, key, default, log):
    value = settings.get(key)
    if value is None or not str(value).strip():
        return default
    try:
        return float(value)
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


def _parse_budget(settings, key, log):
    """Size budget string -> bytes; '' = None (no limit); bad value -> None + WARN."""
    raw = str(settings.get(key, '')).strip()
    if not raw:
        return None
    try:
        return parse_size(raw)
    except ValueError as exc:
        log('WARN  staggered: %s - no size limit applied for %s' % (exc, key))
        return None


def _parse_intervals(raw, min_days, log):
    """'0.5, 60' -> sorted unique positive float boundaries [0.5, 60.0].
    Positive entries below min_days (when min_days > 0) are clamped up to it."""
    boundaries = []
    for token in str(raw).split(','):
        token = token.strip()
        if not token:
            continue
        try:
            days = float(token)
        except ValueError:
            log('WARN  staggered: invalid INTERVALS_DAYS entry %r ignored' % token)
            continue
        if days <= 0:
            log('WARN  staggered: non-positive INTERVALS_DAYS entry %r ignored' % token)
            continue
        if 0 < min_days and days < min_days:
            log('WARN  staggered: INTERVALS_DAYS entry %r below the MIN_STAGGER_DAYS floor - treated as %s days' % (token, min_days))
            days = min_days
        boundaries.append(days)
    return sorted(set(boundaries))

# ----------------------------------------------------------------------

def split_version_name(name):
    """
    'base~20260614-180023.txt' -> ('base', '20260614-180023', '.txt')
    'base~20260614-180023' -> ('base', '20260614-180023', '')
    'base~20260614-180023_1' -> ('base', '20260614-180023_1', '')
    Returns None if there is no '~'.
    """
    tilde = name.rfind('~')
    if tilde < 0:
        return None
    dot = name.rfind('.', tilde)
    if dot < 0:
        return name[:tilde], name[tilde + 1:], ''
    return name[:tilde], name[tilde + 1:dot], name[dot:]


def parse_stamp(stamp):
    """'20260614-180023' / '20260614-180023_2' -> (datetime, sub_second_seq).
    Returns None if the stamp can't be parsed (file is then left untouched)."""
    m = re.match(r'^(\d{8}-\d{6})(?:_(\d+))?$', stamp)
    if not m:
        return None
    try:
        base = dt.datetime.strptime(m.group(1), '%Y%m%d-%H%M%S')
    except ValueError:
        return None
    return base, int(m.group(2) or 0)

# ----------------------------------------------------------------------

def prune(version_dir, settings_path='', dry_run=False, log=None):
    """Enforce the staggered schedule + per-chain size budgets.
    Returns (deleted, freed_bytes)."""
    log = log if log else (lambda message, level='': None)

    settings = load_settings(settings_path) if settings_path else {}
    if settings is None:
        log('WARN  staggered: settings file not found: %s (using defaults)' % settings_path)
        settings = {}
    enabled       = _get_bool(settings, 'ENABLED', DEFAULT_ENABLED, log)
    keep_newest   = _get_int(settings, 'KEEP_NEWEST', DEFAULT_KEEP_NEWEST, log)
    newest_budget = _parse_budget(settings, 'TOTAL_SIZE_OF_NEWEST_VERSIONS', log)
    least_newest  = _get_int(settings, 'KEEP_AT_LEAST_NEWEST', DEFAULT_KEEP_AT_LEAST_NEWEST, log)
    min_stagger   = _get_float(settings, 'MIN_STAGGER_DAYS', DEFAULT_MIN_STAGGER_DAYS, log)
    if min_stagger < 0:
        min_stagger = 0.0
    boundaries    = _parse_intervals(settings.get('INTERVALS_DAYS', DEFAULT_INTERVALS_DAYS), min_stagger, log)
    every_days    = _get_float(settings, 'EVERY_DAYS', DEFAULT_EVERY_DAYS, log)
    max_age       = _get_float(settings, 'MAX_AGE', DEFAULT_MAX_AGE, log)
    oldest_budget = _parse_budget(settings, 'TOTAL_SIZE_OF_OLDEST_VERSIONS', log)
    least_oldest  = _get_int(settings, 'KEEP_AT_LEAST_OLDEST', DEFAULT_KEEP_AT_LEAST_OLDEST, log)
    _warn_unknown(settings, ('ENABLED', 'KEEP_NEWEST', 'TOTAL_SIZE_OF_NEWEST_VERSIONS',
                             'KEEP_AT_LEAST_NEWEST', 'MIN_STAGGER_DAYS', 'INTERVALS_DAYS',
                             'EVERY_DAYS', 'MAX_AGE', 'TOTAL_SIZE_OF_OLDEST_VERSIONS',
                             'KEEP_AT_LEAST_OLDEST'), 'staggered', log)

    if not enabled:
        log('  staggered: disabled', 'verbose')
        return 0, 0
    keep_newest  = max(keep_newest, 0)
    least_newest = max(least_newest, 0)
    least_oldest = max(least_oldest, 0)
    if max_age < 0:
        max_age = 0.0
    if 0 < min_stagger and 0 < every_days < min_stagger:
        log('WARN  staggered: EVERY_DAYS %s below the MIN_STAGGER_DAYS floor - treated as %s days' % (every_days, min_stagger))
        every_days = min_stagger

    log('  staggered: intervals %s days, tail every %s day(s), max age %s, newest keep %d (%s budget), oldest budget %s, floors %d/%d, stagger floor %s'
        % (boundaries if boundaries else '(none)', every_days,
           ('%g day(s)' % max_age) if max_age > 0 else 'forever',
           keep_newest, fmt_size(newest_budget) if newest_budget is not None else 'no',
           fmt_size(oldest_budget) if oldest_budget is not None else 'none',
           least_newest, least_oldest,
           ('%g day(s)' % min_stagger) if min_stagger > 0 else 'off'), 'verbose')

    chains = {}   # (dir, base, ext) -> [(stamp_dt, seq, path, size), ...]
    for dirpath, _dirnames, filenames in os.walk(version_dir):
        for name in filenames:
            parsed = split_version_name(name)
            if parsed is None:
                continue                                   # not a version file - leave alone
            base, stamp, ext = parsed
            when = parse_stamp(stamp)
            if when is None:
                log('  staggered: unparseable stamp, left alone: %s' % name, 'verbose')
                continue
            path = os.path.join(dirpath, name)
            try:
                size = os.stat(path).st_size
            except OSError:
                continue
            chains.setdefault((dirpath, base, ext), []).append((when[0], when[1], path, size))

    now = dt.datetime.now()
    deleted = freed = 0
    for (dirpath, base, ext), entries in sorted(chains.items()):
        entries.sort(key=lambda e: (e[0], e[1]), reverse=True)   # newest first
        n = len(entries)

        # rule 1: newest group = up to KEEP_NEWEST as long as they fit the budget, floor KEEP_AT_LEAST_NEWEST
        fit = 0
        cum = 0
        for _when, _seq, _path, size in entries:
            if newest_budget is not None and cum + size > newest_budget:
                break
            cum += size
            fit += 1
        count_newest = min(keep_newest, fit)
        count_newest = min(max(count_newest, least_newest), n)

        # rules 2+3: oldest version per interval band / per tail bucket
        oldest_in = {}                                   # bucket key -> index (newest-first walk -> final = oldest)
        for i in range(count_newest, n):
            age_days = (now - entries[i][0]).total_seconds() / 86400.0
            if max_age > 0 and age_days > max_age:
                continue                                 # past maximum age - expired candidate
            bucket = None
            for bi, boundary in enumerate(boundaries):
                if age_days <= boundary:
                    bucket = ('b', bi)
                    break
            if bucket is None:
                # tail buckets are aligned at age zero (windows 0-EVERY_DAYS, EVERY_DAYS-2*EVERY_DAYS, ...)
                # every_days <= 0 disables tail staggering: each version gets its own bucket (all survive this rule)
                bucket = ('t', int(age_days // every_days)) if every_days > 0 else ('t', i)
            oldest_in[bucket] = i

        # rule 4: fit the interval/tail survivors into the oldest budget, newest survivor first, floor KEEP_AT_LEAST_OLDEST
        selected = sorted(oldest_in.values())            # ascending index == newest first
        keep_idx = set(range(count_newest))
        cum = 0
        for i in selected:
            if oldest_budget is not None and cum + entries[i][3] > oldest_budget:
                break
            cum += entries[i][3]
            keep_idx.add(i)
        if len(keep_idx) - count_newest < least_oldest:
            pool = [i for i in selected if i not in keep_idx]                       # next selected survivors first
            pool += [i for i in range(count_newest, n) if i not in keep_idx and i not in pool]  # then anything else
            for i in pool:
                if len(keep_idx) - count_newest >= least_oldest:
                    break
                keep_idx.add(i)

        for i, (_when, _seq, path, size) in enumerate(entries):
            if i in keep_idx:
                continue
            if dry_run:
                log('  staggered: would delete %s (%s)' % (path, fmt_size(size)), 'verbose')
            else:
                try:
                    os.remove(path)
                except OSError as exc:                     # in use / gone already
                    log('WARN  staggered: could not delete %s: %s' % (path, exc))
                    continue
                log('  staggered: deleted %s (%s)' % (path, fmt_size(size)), 'verbose')
            deleted += 1
            freed   += size
    return deleted, freed
