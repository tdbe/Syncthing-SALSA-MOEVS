# Syncthing SALSA MOEVS: 

## Standard-Agnostic Local-Space-Assuring Module-Optional External Versioning Script.

Finally (and after 13 years) a storage-size-aware and actually customizable file-versioning script for [Syncthing](https://syncthing.net). Python and cross platform Linux/Mac/Windows (but for now only tested on syncthing in windows).

Covers not just Simple file versioning, but also a sane (actually configurable, damnit) [Staggered](https://docs.syncthing.net/users/versioning.html#staggered-file-versioning) file versioning scheme.

These modules (can be combined and) enable what Syncthing's builtin versioning doesn't have: **retention limits based on version-chain size or total size**, plus other time window and age and per-file version settings.

This repo is tweaked, tested, iterated, read-through, architected by [me](https://github.com/tdbe) but slopped using Qwen 3.8 Flash Next 180B a6B Q4. Provided for free as-is (it's what I'm using, especially for Signal backups ([see syncthing ticket](https://github.com/syncthing/syncthing/issues/9581))). It's only in charge of the .stversions subfolder(s) of your synced syncthing folder(s), and works even with Receive-Encrypted file versions.

### The Staggered Versioning module (a customizable replacement for Syncthing's builtin "Staggered File Versioning"):

Per file version chain, versions are aged by the `~YYYYMMDD-hhmmss` timestamp in their name and then:

1. **Newest group**: the newest `KEEP_NEWEST` versions are always kept regardless of the rules below - but only as many of them as fit within `TOTAL_SIZE_OF_NEWEST_VERSIONS`, never fewer than `KEEP_AT_LEAST_NEWEST`.
2. **Your own intervals** instead of Syncthing's fixed 1h/1d/30d: `INTERVALS_DAYS` is a comma-separated list of cumulative day boundaries (e.g. `0.5, 60` = the first half day, then up to 60 days). The oldest version landing in each interval is kept.
3. **Tail until maximum age**: past the last interval, the oldest version in every `EVERY_DAYS` bucket is kept (`EVERY_DAYS <= 0` disables tail staggering). Older than `MAX_AGE` days gets deleted; `MAX_AGE = 0` keeps forever whatever survived the intervals and tail.
4. **Oldest group size budget**: everything rules 2 and 3 kept must fit within one per-file `TOTAL_SIZE_OF_OLDEST_VERSIONS` (walking from the newest survivor down), never fewer than `KEEP_AT_LEAST_OLDEST`.

If a single version is bigger than both size budgets and both `KEEP_AT_LEAST_*` are 0, no versions of that file are kept at all.

## Usage: 

Download this repo and place it in a folder anywhere. Then go to one of your Syncthing Folders settings in the Syncthing GUI and paste this command in the Exterenal Versioning field. You can also point to any custom settings file(s) to correspond with different synced folders:
```
  python "/opt/Syncthing-salsa-moevs/syncthing_salsa_modular_external_versioner__main_script.py" "%FOLDER_PATH%" "%FILE_PATH%" --version-count-based-pruner-settings "./your_custom_syncthing_salsa_settings__version_count_based_pruner_05.txt"
``` 
Note you must leave the "%FOLDER_PATH%" "%FILE_PATH%" as is in the commmand, they are the params supplied by syncthing to us. And point to valid settings files (or leave those params blank and it uses the global files):
```
--main-settings "./syncthing_salsa_settings__main_script_01.txt" --version-count-based-pruner-settings "./syncthing_salsa_settings__version_count_based_pruner_01.txt" --time-based-pruner-settings "./syncthing_salsa_settings__time_based_pruner_01.txt" --staggered-pruner-settings "./syncthing_salsa_settings__staggered_pruner_01.txt" --total-size-based-pruner-settings "./syncthing_salsa_settings__stversions_total_size_pruner_01.txt"
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

### Daylight savings: 
The timestamp appended to the filename inside .stversions (e.g., filename~20261019-005421.ext) is derived directly from the file's Modification Time (mtime). So theoretically at 3AM on DST day you could back up within 1h .stversions out of order.

### Sub-second versions:
The only non-syncthing-standard behaviour I have is that if a file gets edited and versioned more than once in the same second, I differentiate them with a `_number` after the timestamp (the version chains behaviour & sorting etc remains syncthing compliant):
```
Examples inside .stversions:
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

### Cleanup Interval:
Syncthing has a time value for its builtin Versioning systems, called "Cleanup Interval". Syncthing Salsa versioning doesn't have a schedule or interval to delay cleanup, it runs the cleanup right after receiving a new version. I can only imagine an interval being slightly necessary if you run on a very large folder that is updated very often and runs off a HDD. And even then the recently scanned clusters would be quickly re-accessible. For now I'd rather have all operations be self-contained per sync-event.

Note: In case anyone doesn't know: versioning events are synchronous (not parallel), one event needs to finish successfully before the next starts. Syncthing stops the synced folder with an error otherwise.

## How it works

Note: the file deletions are done with the `os.remove()` command - which is the equivalent of sending that file to trash and emptying that file from trash (aka unlinking or marking that file's space as reusable in the storage drive). The purpose is to save space, and it only runs on .stversions folders.

When Syncthing is about to replace or delete a file due to a change coming
from another device, it calls our script, which:

1. **Moves** the file into `<folder>/.stversions/<subfolders>/` renamed
   Syncthing-style: `base~YYYYMMDD-hhmmss.ext` (stamp from the file's mtime).
   Subfolder structure is mirrored.
2. Runs the **version-count pruner**: Operates per file version chain. Caps the maximum number of versions of a file to this many versions (deletes the oldest). It sorts files by name, because the timestamp format, appended to the otherwise unique file name, is equivalent to a sort by date.
3. Runs the **time-based pruner**: Deletes the oldest files within .stversions, if they are older than DAYS. The date is the file creation time (I'm not parsing the file names for the timestamp, for speed reasons (we're listing the entire .stversions folder, not just one verion chain)). Keep a number `MIN_KEEP_FILES` even beyond the expiration date.
4. Runs the **staggered pruner**: Syncthing's builtin staggered versioning redone to your spec - per version chain it keeps the newest x versions always, the oldest version in each of your own day-intervals and X-days tail buckets, all under per-file size budgets (see the module section above).
5. Runs the **total-size pruner**: Until the total .stversions folder size is less than max size, it prunes the oldest files by creation date (on mac and windows) (on linux it might be the last modified date). Doesn't track version chains, only global age of all files in .stversions. Can also `MIN_KEEP_FILES`.

Note: These concurrent pruner modules work even on syncthing "Receive Encrypted" folders. When restoring an encrypted stversion be sure to keep the folder structure of the specific file but remove the time stamp appended to the file name.
