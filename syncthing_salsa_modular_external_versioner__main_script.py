#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Syncthing SALSA MOEVS: standard-agnostic local-space-assuring module-optional external versioning script.
Finally (and after 13 years) storage-size-aware simple-file-versioning for syncthing. Python cross platform Linux/Mac/Windows.

This repo is tweaked, tested, iterated, read-through, architected by [me](https://github.com/tdbe) but slopped using Qwen 3.8 Flash Next 180B a6B Q4. Provided for free as-is (it's what I'm using, especially for Signal backups).

Syncthing runs this right before replacing/deleting a file that changed on another device. This script does, in order:

  1. MOVES the file to  <folder>/.stversions/<subfolders>/<base>~<YYYYMMDD-hhmmss><.ext>
     (Syncthing-style timestamp taken from the file's mtime, subfolder structure mirrored. 
     Syncthing requires the file to be gone from the synced folder when we return or it throws a sync error.)
  2. syncthing_salsa_module__version_count_based_pruner.py: keep at most KEEP_VERSIONS newest
     versions per file chain.
  3. syncthing_salsa_module__time_based_pruner.py: delete versions older than DAYS.
  4. syncthing_salsa_module__stversions_total_size_pruner.py: if the archive still exceeds MAX_SIZE, 
     delete oldest-first until back under the cap.

The system-log output goes to the Windows Application event log or (Linux/macOS) syslog via 'logger'; popups
use WScript.Shell / osascript / zenity / kdialog.

Settings live in plain-text 'KEY = VALUE' files, one per script:
    syncthing_salsa_settings__main_script_01.txt
    syncthing_salsa_settings__time_based_pruner_01.txt
    syncthing_salsa_settings__stversions_total_size_pruner_01.txt
    syncthing_salsa_settings__version_count_based_pruner_01.txt
Settings files are looked up next to this script by default.

Usage: The Syncthing command looks like this and can point to any custom settings file(s) to correspond with different synced folders:

  python "/opt/Syncthing-salsa-moevs/syncthing_salsa_modular_external_versioner__main_script.py" "%FOLDER_PATH%" "%FILE_PATH%" --version-count-based-pruner-settings "./your_custom_syncthing_salsa_settings__version_count_based_pruner_05.txt"
  
Just paste the above command in Syncthing in your folder's settings under External Versioning. Note you must leave the "%FOLDER_PATH%" "%FILE_PATH%" as is in the commmand, they are params supplied by syncthing to us here. And point to valid settings files (or leave those params blank and it uses the global files):
--main-settings "./syncthing_salsa_settings__main_script_01.txt" --version-count-based-pruner-settings "./syncthing_salsa_settings__version_count_based_pruner_01.txt" --time-based-pruner-settings "./syncthing_salsa_settings__time_based_pruner_01.txt" --total-size-based-pruner-settings "./syncthing_salsa_settings__stversions_total_size_pruner_01.txt"

Relative settings paths resolve against THIS SCRIPT's folder (Syncthing'sworking directory is unpredictable); 
absolute paths are used as-is. 
Settings are re-read on every run, so edits take effect immediately.

This versioner and its pruners works even on syncthing Receive Encrypted folders. When restoring an encrypted stversion be sure to keep the folder structure of the specific file but remove the time stamp appended to the file name.
"""

import argparse
import datetime as dt
import os
import shutil
import subprocess
import sys
import time

import syncthing_salsa_module__version_count_based_pruner
import syncthing_salsa_module__time_based_pruner
import syncthing_salsa_module__stversions_total_size_pruner
from syncthing_salsa_module__stversions_total_size_pruner import fmt_size

# ---------------------------------------------------------- platform --
IS_WINDOWS = sys.platform == 'win32'
IS_MACOS   = sys.platform == 'darwin'
IS_LINUX   = sys.platform.startswith('linux')

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

# Default settings file locations (relative to this script). The Syncthing
# command can override each with --main-settings / --version-count-based-pruner-settings /
# --time-based-pruner-settings / --total-size-based-pruner-settings params.
MAIN_SETTINGS_PATH = os.path.join(_SCRIPT_DIR, 'syncthing_versioner_main_settings.txt')

version_count_based_pruner_settings_PATH = os.path.join(_SCRIPT_DIR, 'syncthing_versioner_prune_version_count_based_pruner_settings.txt')

time_based_pruner_settings_PATH = os.path.join(_SCRIPT_DIR, 'syncthing_versioner_syncthing_salsa_module__time_based_pruner_settings.txt')

total_size_based_pruner_settings_PATH = os.path.join(_SCRIPT_DIR, 'syncthing_versioner_prune_total_total_size_based_pruner_settings.txt')

# Fallback defaults, used only if the main settings file is missing/incomplete.
LOG_FILE = '' # '' = no file logging
VERBOSE = False # True = emit chatty 'verbose' messages too
OUTPUT_TO_SYSTEM_LOG = False # Windows event log / Linux+macOS syslog
OUTPUT_TO_POPUP_MESSAGE = False
LOG_PREFIX = '[Syncthing Custom Versioner]'

_NO_WINDOW = getattr(subprocess, 'CREATE_NO_WINDOW', 0)


# ------------------------------------------------------ settings files --
def load_settings(path):
    """Parse a 'KEY = VALUE' text file into {KEY: value-string}.

    Blank lines and '#' comments are ignored; surrounding quotes on values
    are stripped. Returns None if the file does not exist / can't be read.
    NOTE: '#' always starts a comment - don't use '#' inside values.
    """
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


def resolve_settings_path(path):
    """Relative settings paths resolve against this script's folder."""
    return path if os.path.isabs(path) else os.path.join(_SCRIPT_DIR, path)


# ------------------------------------------------------------ logging --
def _ps_quote(text):
    """Make a string safe inside a PowerShell single-quoted string."""
    return text.replace("'", "''")


def _osa_quote(text):
    """Make a string safe inside an AppleScript double-quoted string."""
    return text.replace('\\', '\\\\').replace('"', '\\"')


def _system_log(message):
    """Windows: Application event log (EventId 420, category 69, Warning).
    Linux/macOS: syslog / unified log via 'logger' at warning priority."""
    try:
        if IS_WINDOWS:
            ps = "[System.Diagnostics.EventLog]::WriteEntry('Application', '%s', 'Warning', 420, 69)" \
                 % _ps_quote('%s %s' % (LOG_PREFIX, message))
            subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', ps],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           creationflags=_NO_WINDOW, timeout=15)
        else:
            subprocess.run(['logger', '-t', 'SyncthingVersioner', '-p', 'user.warning',
                            '--', '%s %s' % (LOG_PREFIX, message)],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15)
    except Exception:
        pass  # logging must never break versioning


def _popup(message):
    """Fire-and-forget popup, auto-closes after 10 min. Never waited on.
    Windows: WScript.Shell.Popup | macOS: osascript | Linux: zenity/kdialog."""
    try:
        if IS_WINDOWS:
            ps = "(New-Object -ComObject WScript.Shell).Popup('%s', 6000, '%s', 0)" \
                 % (_ps_quote(message), _ps_quote(LOG_PREFIX))
            subprocess.Popen(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', ps],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             creationflags=_NO_WINDOW)
        elif IS_MACOS:
            script = 'display dialog "%s" with title "%s" buttons {"OK"} giving up after 6000' \
                     % (_osa_quote(message), _osa_quote(LOG_PREFIX))
            subprocess.Popen(['osascript', '-e', script],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:  # Linux and other unixes
            if shutil.which('zenity'):
                cmd = ['zenity', '--info', '--title=%s' % LOG_PREFIX,
                       '--timeout=600', '--text=%s' % message]
            elif shutil.which('kdialog'):
                cmd = ['kdialog', '--title', LOG_PREFIX, '--passivepopup', message, '6000']
            else:
                return  # no popup tool (headless) - silently skip
            subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass


def log(message, level=''):
    """Write a message to every enabled output.

    level='verbose' marks a chatty message: it is eaten unless VERBOSE is true.
    Outputs: LOG_FILE (if set), stderr (when VERBOSE, so Syncthing's own
    debug log can capture it), system log / popup (if their flags are set).
    """
    if level == 'verbose' and not VERBOSE:
        return
    if LOG_FILE:
        try:
            d = os.path.dirname(LOG_FILE)
            if d:
                os.makedirs(d, exist_ok=True)
            with open(LOG_FILE, 'a', encoding='utf-8') as fh:
                fh.write('%s  %s\n' % (dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S'), message))
        except OSError:
            pass
    if VERBOSE:
        print(message, file=sys.stderr)
    if OUTPUT_TO_SYSTEM_LOG:
        _system_log(message)
    if OUTPUT_TO_POPUP_MESSAGE:
        _popup(message)


# ------------------------------------------------------------- moving --
def version_stamp(src):
    """The ~YYYYMMDD-hhmmss tag: the file's mtime, like Syncthing does."""
    try:
        ts = os.path.getmtime(src)
    except OSError:
        ts = time.time()
    return dt.datetime.fromtimestamp(ts).strftime('%Y%m%d-%H%M%S')


def remove_empty_dirs(root):
    for dirpath, _d, _f in os.walk(root, topdown=False):
        if os.path.normcase(dirpath) == os.path.normcase(root):
            continue
        try:
            if not os.listdir(dirpath):
                os.rmdir(dirpath)
        except OSError:
            pass


# --------------------------------------------------------------- main --
def main(argv):
    global LOG_FILE, VERBOSE, OUTPUT_TO_SYSTEM_LOG, OUTPUT_TO_POPUP_MESSAGE, LOG_PREFIX

    ap = argparse.ArgumentParser(description='Syncthing SALSA MOEVS: modular versioning including stfolder size based, version chain length based, and age based.')
    ap.add_argument('tokens', nargs='*', help='"%%FOLDER_PATH%%" "%%FILE_PATH%%"')
    ap.add_argument('--main-settings',     default='', help='override the main script settings file path')
    ap.add_argument('--version-count-based-pruner-settings', default='', help='override the version count based pruner settings file path')
    ap.add_argument('--time-based-pruner-settings',     default='', help='override the version age based pruner settings file path')
    ap.add_argument('--total-size-based-pruner-settings',     default='', help='override the stversions total size based pruner settings file path')
    ap.add_argument('--cleanup-only', action='store_true', help='only run the pruners')
    ap.add_argument('--dry-run', action='store_true', help='test run without doing any actual file operations')
    ap.add_argument('--verbose', action='store_true', help='verbose logging within whatever logging means are active')
    args = ap.parse_args(argv)

    # 1. main settings (stderr warnings only - the log itself isn't configured yet)
    warn = lambda msg: print('syncthing-salsa-versioner: %s' % msg, file=sys.stderr)
    main_path = resolve_settings_path(args.main_settings) if args.main_settings else MAIN_SETTINGS_PATH
    settings = load_settings(main_path)
    if settings is None:
        warn('syncthing-salsa-versioner: WARN  main settings file not found: %s (using built-in defaults)' % main_path)
        settings = {}
    LOG_FILE                = settings.get('LOG_FILE', LOG_FILE)
    VERBOSE                 = _get_bool(settings, 'VERBOSE', VERBOSE, warn)
    OUTPUT_TO_SYSTEM_LOG    = _get_bool(settings, 'OUTPUT_TO_SYSTEM_LOG', OUTPUT_TO_SYSTEM_LOG, warn)
    OUTPUT_TO_POPUP_MESSAGE = _get_bool(settings, 'OUTPUT_TO_POPUP_MESSAGE', OUTPUT_TO_POPUP_MESSAGE, warn)
    LOG_PREFIX              = settings.get('LOG_PREFIX', LOG_PREFIX)
    _warn_unknown(settings, ('LOG_FILE', 'VERBOSE', 'OUTPUT_TO_SYSTEM_LOG',
                             'OUTPUT_TO_POPUP_MESSAGE', 'LOG_PREFIX'), 'main', warn)
    VERBOSE = VERBOSE or args.verbose # an explicit --verbose arg always wins

    # 2. what file did Syncthing hand us to version?
    tokens = [t for t in args.tokens if t and t.strip()]
    #if not tokens:  # Syncthing >= 2.1.2 also exports FOLDER_PATH / FILE_PATH env vars
    #    env_folder = os.environ.get('FOLDER_PATH', '').strip()
    #    env_file   = os.environ.get('FILE_PATH', '').strip()
    #    if env_folder:
    #        tokens = [env_folder] + ([env_file] if env_file else [])
    if not tokens:
        log('ERROR no folder path - configure "%%FOLDER_PATH%%" "%%FILE_PATH%%" in the versioning command')
        print('syncthing-salsa-versioner: no folder path given', file=sys.stderr)
        return 1

    folder = tokens[0].rstrip('\\/')
    rel    = tokens[1] if len(tokens) > 1 else ''
    src    = os.path.join(folder, rel) if rel else folder

    # Re-join tokens in case paths arrived unquoted and got split on spaces.
    if not os.path.exists(src) and len(tokens) > 2:
        for i in range(len(tokens) - 1, 0, -1): # longest prefix that is a real folder
            cand = ' '.join(tokens[:i])
            if os.path.isdir(cand):
                folder = cand.rstrip('\\/')
                rel    = ' '.join(tokens[i:])
                src    = os.path.join(folder, rel)
                break

    # %FILE_PATH% may arrive absolute in some setups - normalise to relative.
    if rel and os.path.isabs(rel) and \
       os.path.normcase(rel).startswith(os.path.normcase(folder) + os.sep):
        rel = rel[len(folder) + 1:]
        src = os.path.join(folder, rel)

    version_dir = os.path.join(folder, '.stversions') # always inside the synced folder
    os.makedirs(version_dir, exist_ok=True)

    log('folder: %s' % folder, 'verbose')
    log("file: '%s'" % rel, 'verbose')
    log('version archive: %s' % version_dir, 'verbose')

    fail = False
    try:
        # 3. MOVE the file in, stamped
        if args.cleanup_only:
            log('cleanup only - nothing to move', 'verbose')
        elif not rel:
            log('SKIP  no file path given (folder level event)')
        elif not os.path.exists(src):
            log('SKIP  source already gone: %s' % src)
        else:
            rel_dir = os.path.dirname(rel)
            dest_dir = os.path.join(version_dir, rel_dir) if rel_dir else version_dir
            os.makedirs(dest_dir, exist_ok=True)

            stem, ext = os.path.splitext(os.path.basename(rel))
            stamp  = version_stamp(src)
            dest   = os.path.join(dest_dir, '%s~%s%s' % (stem, stamp, ext))
            n = 1
            while os.path.exists(dest):            # same-second collision -> _1, _2, ...
                dest = os.path.join(dest_dir, '%s~%s_%d%s' % (stem, stamp, n, ext))
                n += 1

            if args.dry_run:
                log('would move  %s  ->  %s' % (src, dest), 'verbose')
            else:
                try:
                    shutil.move(src, dest)
                except OSError as exc:
                    log('ERROR could not version %s: %s' % (src, exc))
                    fail = True
                if not fail and os.path.exists(src):
                    log('ERROR %s still exists after versioning' % src)
                    fail = True

        # 4. fetch the settings for the pruner modules
        vcbp_path = resolve_settings_path(args.version_count_based_pruner_settings) if args.version_count_based_pruner_settings else version_count_based_pruner_settings_PATH
        
        tbp_path = resolve_settings_path(args.time_based_pruner_settings) if args.time_based_pruner_settings else time_based_pruner_settings_PATH
        
        tsbp_path = resolve_settings_path(args.total_size_based_pruner_settings) if args.total_size_based_pruner_settings else total_size_based_pruner_settings_PATH

        # 5. run the pruner modules in order
        deleted1, freed1 = syncthing_salsa_module__version_count_based_pruner.prune(version_dir, 
        settings_path=vcbp_path, dry_run=args.dry_run, log=log)
        
        deleted2, freed2 = syncthing_salsa_module__time_based_pruner.prune(version_dir, 
        settings_path=tbp_path, dry_run=args.dry_run, log=log)
        
        deleted3, freed3, total, count = syncthing_salsa_module__stversions_total_size_pruner.prune(version_dir, 
        settings_path=tsbp_path, dry_run=args.dry_run, log=log)

        if (deleted1 + deleted2 + deleted3) and not args.dry_run:
            remove_empty_dirs(version_dir)

        log('OK      %s -> archive %s in %d file(s); version-count pruned %d (%s), time pruned %d (%s), total-size pruned %d (%s)'
            % (rel or '(cleanup only)', fmt_size(total), count,
               deleted1, fmt_size(freed1), deleted2, fmt_size(freed2), deleted3, fmt_size(freed3)))
    except Exception as exc: # noqa: BLE001 - never crash on Syncthing
        log('ERROR %s' % exc)
        print('syncthing-salsa-versioner: %s' % exc, file=sys.stderr)
        fail = True

    return 1 if fail else 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))