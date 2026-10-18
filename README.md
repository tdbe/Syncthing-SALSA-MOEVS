# Syncthing SALSA MOEVS: 

## Standard-Agnostic Local-Space-Assuring Module-Optional External Versioning Script.

Finally (and after 13 years) a storage-size-aware simple-file-versioning script for [Syncthing](https://syncthing.net). Python and cross platform Linux/Mac/Windows (but for now only tested on syncthing in windows).

This repo is tweaked, tested, iterated, read-through, architected by [me](https://github.com/tdbe) but slopped using Qwen 3.8 Flash Next 180B a6B Q4. Provided for free as-is (it's what I'm using, especially for Signal backups ([see syncthing ticket](https://github.com/syncthing/syncthing/issues/9581))).

The optional modules add what Syncthing's builtin versioning doesn't have: **retention limits based on total archive size**, plus age and per-file version caps.

### TODO: also make a sane (configurable) staggered versioning module

## Usage: 

The exterenal versioning Syncthing command looks like this and can point to any custom settings file(s) to correspond with different synced folders:
```
  python "/opt/Syncthing-salsa-moevs/syncthing_salsa_modular_external_versioner__main_script.py" "%FOLDER_PATH%" "%FILE_PATH%" --version-count-based-pruner-settings "./your_custom_syncthing_salsa_settings__version_count_based_pruner_05.txt"
``` 
Just paste the above command in Syncthing in your folder's settings under External Versioning. Note you must leave the "%FOLDER_PATH%" "%FILE_PATH%" as is in the commmand, they are params supplied by syncthing to us here. And point to valid settings files (or leave those params blank and it uses the global files):
```
--main-settings "./syncthing_salsa_settings__main_script_01.txt" --version-count-based-pruner-settings "./syncthing_salsa_settings__version_count_based_pruner_01.txt" --time-based-pruner-settings "./syncthing_salsa_settings__time_based_pruner_01.txt" --total-size-based-pruner-settings "./syncthing_salsa_settings__stversions_total_size_pruner_01.txt"
```

Settings are re-read on every run. When you edit a file, it reloads settings.
You can test first with `--dry-run --verbose` (and `--cleanup-only` to just run the pruners).

## Logging: 

The (non-debug) syncthing log will display only if there was a fatal error from the external versioner (and stalls the sync). I added options inside salsa you can turn on: 
- custom log file
- OS event log: Windows Application event log or (Linux/macOS) syslog via 'logger'
- OS Shell popup: Windows: WScript.Shell.Popup; macOS: osascript; Linux: zenity/kdialog.
- a `--verbose` flag, otherwise it's just one line logged per run.


## Quirks:

Daylight savings: the timestamp appended to the filename inside .stversions (e.g., filename~20261019-005421.ext) is derived directly from the file's Modification Time (mtime). So theoretically at 3AM on DST day you could back up within 1h .stversions out of order.

Sub-second versions: The only non-syncthing-standard behaviour I have is that if a file gets edited and versioned more than once in the same second, I differentiate them with a `_number` after the timestamp (the version chains behaviour & sorting etc remains syncthing compliant):
```
examples inside .stversions:
pron~20251231-235905.txt
pron~20251231-235905_1.txt
pron~20251231-235905_2.txt
.nomedia~20261231-235959
.nomedia~20261231-235959_1
sfw_h~20261231-235959.264
sfw_h~20261231-235959_1.264
tildes~are_fine.dots_too~20261231-235959.txt
tildes~are_fine.dots_too~20261231-235959_1.txt
```

## How it works

When Syncthing is about to replace or delete a file due to a change coming
from another device, it calls our script, which:

1. **Moves** the file into `<folder>/.stversions/<subfolders>/` renamed
   Syncthing-style: `base~YYYYMMDD-hhmmss.ext` (stamp from the file's mtime).
   Subfolder structure is mirrored.
2. Runs the **version-count pruner**: Operates per file version chain. Caps the maximum number of versions of a file to this many versions (deletes the oldest). It sorts files by name, because the timestamp format, appended to the otherwise unique file name, is equivalent to a sort by date.
3. Runs the **time-based pruner**: Deletes the oldest files within .stversions, if they are older than DAYS. The date is the file creation time (I'm not parsing the file names for the timestamp, for speed reasons (we're listing the entire .stversions folder, not just one verion chain)). Keep a number `MIN_KEEP_FILES` even beyond the expiration date.
4. Runs the **total-size pruner**: Until the total .stversions folder size is less than max size, it prunes the oldest files by creation date (on mac and windows) (on linux it might be the last modified date). Doesn't track version chains, only global age of all files in .stversions. Can also `MIN_KEEP_FILES`.

These concurrent pruner modules work even on syncthing "Receive Encrypted" folders. When restoring an encrypted stversion be sure to keep the folder structure of the specific file but remove the time stamp appended to the file name.
