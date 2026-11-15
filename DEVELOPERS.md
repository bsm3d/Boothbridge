# Boothbridge developer guide

This guide is for someone who wants to read, run, build or change the code. It is short on purpose. It says where things are and how they fit together, and the code is the reference for the rest. Boothbridge is free, the complete source is in this repository, and there is no key, no offer and no hidden part.

## Contents

1. [What it is](#what-it-is)
2. [Method](#method)
3. [Formats](#formats)
4. [Architecture](#architecture)
5. [Data model](#data-model)
6. [Readers and writers](#readers-and-writers)
7. [CDJ USB key export](#cdj-usb-key-export)
8. [Audio analysis](#audio-analysis)
9. [Editors](#editors)
10. [Languages](#languages)
11. [Settings and files](#settings-and-files)
12. [Safeguards](#safeguards)
13. [Run, command line and build](#run-command-line-and-build)
14. [Coding conventions](#coding-conventions)
15. [Contributing](#contributing)
16. [Legal basis](#legal-basis)

## What it is

Boothbridge is a hub for DJ playlists. Each DJ program keeps its collection in its own format. Boothbridge reads those collections into one model in memory, lets you edit playlists, cues and beat grids, and writes the result back out in another format. It is not a DJ program, it does not mix, and it works alongside the programs it connects.

```
   readers                    model                     writers
   (sources/)              (model.py)                 (export/)

 Traktor NML  ----+                          +---->  Traktor NML
 rekordbox XML ---+                          +---->  rekordbox XML
 VirtualDJ -------+                          +---->  VirtualDJ lists
 iTunes / Music --+     Node tree of         +---->  iTunes XML
 Engine DJ -------+---> Track and Cue  ------+---->  Mixxx database
 Mixxx -----------+     + editors            +---->  M3U8, PLS, XSPF
 M3U / M3U8 ------+                          +---->  CSV, JSON
 music folder ----+                          +---->  CDJ USB key
 CDJ USB key -----+                          +---->  ZIP backup
 project (JSON) --+
```

It reads and writes ordinary files only. It has no network code: nothing is uploaded, nothing is sent, nothing is downloaded. The only outgoing action is opening a web page or a folder in the system browser or file manager when you ask for it.

## Method

The formats were worked out from public documentation and from files that the user exported with the program that owns them, then compared with what Boothbridge writes until the other program accepted it. Where a CDJ player is involved, the player and rekordbox were the judges: a key that rekordbox opens and a player reads is right.

This is black box work. No program or firmware of another vendor was disassembled or decompiled, and no code or resource of another program is in the repository. The encrypted databases of other programs are never opened, `protected.py` refuses them (see [Safeguards](#safeguards)). Anyone who contributes follows the same rule.

## Formats

Only the rekordbox XML is an official interface meant for other programs, and no other format is described as official.

| Format | Direction | How it is known |
| --- | --- | --- |
| rekordbox XML | read, write | Documented by its vendor for third party tools |
| Traktor NML | read, write | Plain readable XML, checked on files exported by the program |
| iTunes or Music `Library.xml` | read, write | Plain XML that the program exports itself (File > Library > Export Library), public format |
| VirtualDJ `database.xml`, `.vdjfolder` | read, write | Plain XML and text files, public documentation |
| Denon Engine DJ `m.db` | read | Plain SQLite, opened read only, third party use documented by the vendor |
| Mixxx `mixxxdb.sqlite` | read, write | Open source program, public SQLite schema |
| M3U, M3U8, PLS, XSPF | read M3U, write all | Plain text standards, XSPF has a public specification |
| CSV, JSON | write | Plain text |
| Music folder | read | No collection: the audio files and their tags |
| Boothbridge project | read, write | Own JSON format (`sources/project.py`) |
| CDJ USB key (`export.pdb`, ANLZ files) | read, write | No official specification. Observed on keys exported by rekordbox, compared with public research and tried on a player |

The writers that were built from documentation and never opened in the program they are meant for carry a "(beta)" mention in the format list: VirtualDJ, Mixxx, iTunes XML, PLS and XSPF (`settings.BETA_EXPORTS`).

The Traktor NML, rekordbox XML and CDJ key writers are the ones checked against real files of the other program.

## Architecture

Python 3.11 or newer, PySide6 for the windows, numpy and scipy for the audio, soundfile for decoding, defusedxml for XML, Pillow for artwork. A small C++ core speeds up the heaviest loops, and the Python code stays the reference and the fallback.

```
main.py ---> djapp/app.py ---+--- command line (--export, --verify)
                             +--- main window (ui/)
                                      |
                  +-------------------+-------------------+
                  |                   |                   |
             sources/            model.py             export/
             (readers)        (Track, Cue, Node)      (writers)
                  |                                       |
                  +---- tags.py, analysis.py, beatdetect.py ----+
                                  settings.py, i18n.py
```

### Module map of `djapp/`

| Module | Role |
| --- | --- |
| `brand.py` | The name, the version, the author and the names of the files the program writes |
| `app.py`, `__main__.py` | Start, command line, logging, language, main window |
| `settings.py` | Settings (JSON in the data folder), the list of export formats, data and cache folders |
| `model.py` | `Track`, `Cue`, `Node` and the tree helpers |
| `keys.py` | One table for the key notations (classical, Open Key, Camelot) |
| `i18n.py`, `lang/` | `tr`, `trn` and the catalogs of the interface languages |
| `integrity.py` | Startup check of the compiled files of a build |
| `protected.py` | Refuses the encrypted databases of other programs |
| `native.py` | Loader of the C++ core (`core/`), everything works without it |
| `analysis.py` | Audio decoding and the three waveform bands |
| `beatdetect.py` | Tempo and first beat of tracks that have no grid |
| `tags.py`, `tagfile.py`, `tagwrite.py` | Read artwork and tags, own tag reader and writer, confirmed tag writes with a backup |
| `audiocheck.py` | Header and decoding checks of audio files, one process per file |
| `library.py` | Read-only scans of a music folder and exact duplicate detection |
| `backup.py`, `cachebackup.py` | ZIP project backup with a checksum file, backup of the analysis cache |
| `drives.py`, `fat32.py` | List removable drives and format one as FAT32 for the players |
| `sources/` | Readers: `__init__.py` (detect, load, shared limits), `nml`, `rbxml`, `vdj`, `itunes`, `engine`, `mixxx`, `m3u`, `folder`, `project`, `key` |
| `export/` | Writers: `__init__.py` (`WRITERS`, `run`), `nml`, `rbxml`, `vdj`, `itunes`, `mixxx`, `m3u`, `lists` (CSV, JSON, PLS, XSPF) |
| `export/common.py`, `files.py`, `fit.py` | What the writers share: audio copy, names, report, and `fit()` that brings a track into the ranges the formats can store |
| `export/manifest.py` | SHA-256 of every file an export writes, and its verification |
| `export/cdj/` | The CDJ key writer, see the next sections: `usb.py` (conductor), `pdbwrite.py`, `devicesql.py`, `anlz.py`, `reference.py`, `keymerge.py`, `validate.py`, and the readers `pdbread.py`, `anlzread.py` |
| `ui/` | Windows: `main_window.py`, `details.py` (playlist), `player.py`, `timeline.py`, `zoomwave.py`, `waveform.py`, `tageditor.py`, `musicbrowser.py`, `metronome.py`, `audiocheck.py`, `backup.py`, `formatter.py`, `dialogs.py`, `theme.py`, `worker.py` (background jobs) |

Other folders: `tests/` (the test suite), `core/` (C++ source), `tools/` (`i18n_check.py`, `make_logo.py`, `rename.py`), `installer/` (Inno Setup script), `branding/` and `images/` (logos and diagrams).

The name of the program is a value of `APP_NAME` in `brand.py`. `tools/rename.py` gives the program another name.

## Data model

Units are fixed once in `model.py`. Readers convert on the way in, writers on the way out.

| Object | What it holds |
| --- | --- |
| `Track` | Tags (title, artist, album, genre, label, comment, remixer, composer), `path`, `duration` (seconds), `bpm`, `grid` (first beat, ms), `key`, `rating`, `color`, `plays`, dates, `cues`, `audio_id` |
| `Cue` | `name`, `kind`, `start` and `length` (ms), `hotcue` (slot 0 to 7, -1 for a memory cue), `color`. A loop has `length` above 0 |
| `Node` | A `folder`, a `playlist` or a `smartlist`. A folder holds `children`, a playlist holds `tracks`. A smartlist keeps its search text and is never evaluated |

Conventions:

| Value | Convention |
| --- | --- |
| Key | An index 0 to 23: 0 to 11 the majors from C, 12 to 23 the minors from Cm, `None` when unknown. `keys.py` maps it to the three notations and `keys.parse` reads any of them |
| Rating | 0 to 5 stars. The writers scale it to what their format uses |
| Cue kinds | The Traktor numbers: cue, fade in, fade out, load, grid, loop. Each format converts at its own boundary |
| Beat grid | One constant tempo (`bpm`) and one first beat (`grid`) per track. A variable grid is reduced to that |
| Colour | A rekordbox colour id 1 to 8, 0 for none |
| Identity | `Track.uid` is `audio_id`, or the path when there is none. `unique_tracks()` lists each uid once and gives the track order of an export |

A reader returns a list of root `Node`. Tracks are shared between playlists, so an edit on a track shows everywhere.

## Readers and writers

### A reader

`sources/__init__.py` holds the two entry points:

- `detect(path)` returns the kind of a file or folder ("traktor", "rekordbox", "mixxx", "m3u", "virtualdj", "itunes", "engine", "folder", "project", "key") from its place, its extension and, for XML and databases, the first bytes or a table name.
- `load(path, music_root, progress, notes)` refuses the protected databases first, calls `detect`, then the `load` function of the matching module, and turns parsing errors into a `ValueError` that says what is wrong with the file.

A reader module exposes `load(path, music_root="", progress=None) -> list[Node]`. It uses `Stream` for XML (one pass, `defusedxml`, bounded depth and counts), `num()` and `position()` for numbers that could be hostile, and `relocator(music_root)` to find audio that moved.

### A writer

`export/__init__.py` has the table `WRITERS`: the name of a format, the module of the package and the function to call. `run(fmt, nodes, out, cfg, progress, cancel)` makes the output folder, opens the checksum manifest, calls the writer, saves the manifest and, when the setting asks for it, reads the whole export back and compares it. The CDJ key is the one format that does not go through the table, it has its own conductor in `export/cdj/usb.py`.

A writer has this shape:

```python
def export(nodes, out, copy=False, verify=False, progress=None, cancel=None, man=None) -> Report
```

It passes every track through `fit()`, writes with `write_atomic()` and records what it wrote in the manifest. Writers report through `Report` (missing files, errors, counts) and stop when the `cancel` event is set.

### Worked example: a new list format

The M3U writer is the smallest. Adding, say, a text list that holds one title per line takes four steps.

1. Write `export/titles.py` with the signature above. Walk the tree with `playlists(nodes)`, build the text, write it with `write_atomic(path, data)`, then `man.add(path, sha256_bytes(data))` if `man` is given.
2. Register it in `export/__init__.py`: `"Titles": ("titles", "export")` in `WRITERS`.
3. Add `"Titles"` to `settings.EXPORTS`, which feeds the format list of the window and the command line. If the format was written from documentation only and never opened in its program, add it to `settings.BETA_EXPORTS` too.
4. If the text is new, add its translations (see [Languages](#languages)).

A reader is the same idea in the other direction: a module in `sources/` with `load()`, a test in `detect()` that recognises the file, a branch in `read_collection()`, and the name in `KINDS`. Keep the limits of the other readers: refuse a file that is too large before reading it, bound counts and depth, and never store a number that is not finite.

## CDJ USB key export

The CDJ USB key is written by Boothbridge itself. rekordbox is not needed and takes no part in it. The target player is the CDJ-2000NXS2, tested by the author. Boothbridge writes the original Device Library format (`export.pdb` and the analysis files) and never the newer OneLibrary database. AlphaTheta's notice for customers using USB devices lists that original format for the CDJ-2000NXS2 and older models, XDJ-700, XDJ-1000MK2, CDJ-900NXS2, XDJ-XZ, XDJ-RX3, XDJ-RR and CDJ-3000; none of them except the CDJ-2000NXS2 has been tried. The players that read only OneLibrary (CDJ-3000X, XDJ-AZ, OPUS-QUAD and OMNIS-DUO) are not supported.

A Pioneer CDJ reads a key through a database and analysis files, not through the audio folder. Boothbridge writes that structure so a player can load playlists, cues, beat grids and waveforms. The layout was observed on keys exported by rekordbox. The code is the reference, `export/cdj/usb.py` is where to start.

```
<key>/
  Contents/<artist>/<album>/<file>        the audio, copied and checked
  PIONEER/
    rekordbox/
      export.pdb                          the database: tracks, playlists, lookups
      export.pdb.bak                      the database that was there before
    USBANLZ/P###/########/
      ANLZ0000.DAT, ANLZ0000.EXT          per track: beat grid, cues, waveforms
    Artwork/00001/a<n>.jpg                square cover pictures
    *SETTING*.DAT                         player settings, written when absent, chosen switches patched
  boothbridge_checksums.json              SHA-256 of everything written
```

### Player settings files

The `PIONEER` folder on the key holds four settings files that Boothbridge writes when they are absent: `MYSETTING.DAT` (player), `MYSETTING2.DAT` (player, extended), `DJMMYSETTING.DAT` (mixer) and `DEVSETTING.DAT` (device). All four follow the same envelope: a 96-byte header (u32 header_size `0x60`, brand 32 bytes, app name 32 bytes `rekordbox`, version 32 bytes), a u32 body_size, a body, a CRC16-XMODEM checksum and two filler bytes. The CRC is computed over the body alone for the player files, and over header plus body for the mixer file. `reference.py` has the constants, the defaults and the builder. `settingsread.py` reads them back: `read(name, data)` returns the body and the list of failed checks (`short`, `header`, `app`, `size`, `checksum`, `end`, `magic`), `check(pioneer_dir)` runs it on the files of a key, `apply(pioneer_dir, changes)` changes single bytes of `MYSETTING.DAT` when it validates and has the 40 byte layout, and nothing else is ever rewritten. The user's choices (`SWITCHES` in `reference.py`, nine on/off positions, off `0x80`, on `0x81`) travel as the string `player_settings` of the config.

**MYSETTING.DAT byte map (player, body_size 40).** Positions are 1-indexed within the body. Positions 1-8 are the magic (`0x78563412 0x02000000`). Positions 9-38 carry the settings, each a single byte, with `0x80` as the base for most choices. Unmapped positions default to `0x00`.

| Position | Default | Setting |
| --- | --- | --- |
| 1-8 | `78 56 34 12 02 00 00 00` | player body magic |
| 9 | `0x81` | on air display: on |
| 10 | `0x83` | LCD brightness: three |
| 11 | `0x81` | quantize: on |
| 12 | `0x88` | auto cue level: memory |
| 13 | `0x80` | language: english |
| 14 | `0x01` | (unknown) |
| 15 | `0x82` | jog ring brightness: bright |
| 16 | `0x81` | jog ring indicator: on |
| 17 | `0x81` | slip flashing: on |
| 18 | `0x01` | (unknown) |
| 19 | `0x01` | (unknown) |
| 20 | `0x01` | (unknown) |
| 21 | `0x82` | disc slot illumination: bright |
| 22 | `0x80` | eject lock: unlock |
| 23 | `0x80` | sync: off |
| 24 | `0x80` | play mode: continue |
| 25 | `0x80` | quantize beat value: one |
| 26 | `0x81` | hot cue auto load: on |
| 27 | `0x81` | hot cue colour: on |
| 28-29 | `0x00` | (unmapped) |
| 30 | `0x80` | needle lock: unlock |
| 31-32 | `0x00` | (unmapped) |
| 33 | `0x80` | time mode: elapsed |
| 34 | `0x80` | jog mode: CDJ |
| 35 | `0x81` | auto cue: on |
| 36 | `0x80` | master tempo: off |
| 37 | `0x81` | tempo range: ten |
| 38 | `0x80` | phase meter: type 1 |
| 39-40 | `0x00` | (padding) |

The mapping was established by black-box testing: change one setting on the player, export, read the byte that changed. Four positions (14, 18, 19, 20) have no known setting. A cross-reference with a rekordbox Golden Sample (148 bytes, October 2026) found zero contradictions and six positions where the user had changed the default.

`MYSETTING2.DAT` uses the same envelope with no body magic (the body starts directly with the settings) and holds extended settings (vinyl speed adjust, jog display mode, pad brightness, beat jump value, among others). `DJMMYSETTING.DAT` has its own magic and its CRC covers the header too. `DEVSETTING.DAT` is a device descriptor (brand `PIONEER DJ`, version `7.2.11`). All four are in `reference.py`.

### Order of writing

```
 1  plan the target path of each track on the key
 2  read the playlists already on the key, merge the new ones (Keep) or start clean (Replace)
 3  copy the audio  ........................  in parallel with  ..  analysis (waveforms, grid, cues)
 4  drop what never reached the key (disk full, unreadable file)
 5  artwork
 6  export.pdb, last, after its predecessor was saved as export.pdb.bak
 7  player settings (only when the key has none), checksum manifest
 8  read everything back and compare to the checksums (optional)
```

The order is the point: a player learns about a track only from the database, so the database is written last. A cut in the middle leaves audio without a database entry, never a database pointing at missing files.

### Checks before publishing

- `validate.py` checks the generated bytes (database structure, analysis sections and counts, the path inside each analysis file) before anything reaches the drive.
- `write_verified()` writes next to the target, reads it back from the drive, compares the hash and only then replaces the file.
- A playlist none of whose files was found is not written, and the report names it.
- Analysis results are cached by a hash of everything that changes them. When the analysis writer changes, `ANLZ_VERSION` in `usb.py` is raised so old entries are no longer found.

### Keep or replace

With the setting `key_mode` at `keep` (the default), the playlists already on the key stay and the new ones are merged in: same folder and same name takes the place of the old playlist, anything else is added. `replace` writes a database with the new playlists only. A key whose database cannot be read is refused in keep mode before anything is written. `keymerge.py` does this, and the old database is always saved as `export.pdb.bak`.

### Reading a key and formatting one

`sources/key.py` with `pdbread.py` and `anlzread.py` read a key back into playlists, treating it as data written by someone else: every length is checked, loops are bounded, paths cannot leave the key. `drives.py` and `fat32.py` format a USB key as FAT32 (any size) for the players. Raw disk access needs administrator rights, so the writing is done by the program started again through the system's elevation prompt, and it checks the drive again before writing. Only removable drives are listed, never the system disk or the one the program runs from.

## Audio analysis

The collections already carry tempo, grid, key and cues, so the audio is needed for two things: the waveforms (CDJ export and editors), and the tempo and first beat of tracks that have none (editors only). An export never detects a tempo.

**Waveform.** `analysis.py` decodes with soundfile at a reduced rate, splits the signal into a low, a mid and a high band with two simple filters, takes the peak of each band per column and normalises on the whole track. A fine resolution is computed once and the overviews are taken from it. The columns become the waveform sections of the analysis files and the waveforms of the editors, which cache them on disk. `native.py` runs the heaviest loops in C++ when the library is there.

**Tempo and first beat.** `beatdetect.py` builds an onset curve from the decoded signal, looks for a period by autocorrelation between plausible tempos, refines the best candidates and decides with how well the pulse repeats. It then picks the first beat as the first position of the beat lattice with a real onset under it. It also measures how steady the tempo is across the track and returns a confidence value: a live recording that drifts has no constant grid and is reported as unreliable instead of being forced. It is a help, not an oracle. Fast tempos can come out at the double of the one you feel, and the interface offers the half. **Musical key.** It is not computed from the audio. It comes from the collection or from the tag of the file, and `keys.py` converts between notations.

## Editors

The playlist window (`ui/details.py`) sorts, reorders and edits playlists and runs the tempo detection on selected tracks. `ui/player.py` is a two deck preview player with crossfade, and `ui/timeline.py` with `ui/zoomwave.py` edit the cues, loops and the grid of one track on a zoomable waveform, with undo and redo (`ui/cuehistory.py`). `ui/tageditor.py` edits tags in the project first, and writes them to the audio file only after a confirmed, separate action (`tagwrite.py`). `ui/musicbrowser.py` is a read-only floating library, `ui/metronome.py` a click track that follows the preview. Editors work on the model in memory. A project (JSON) saves that work, and loading a project restores it.

## Languages

The interface exists in English (the language of the code), French, German, Spanish and Simplified Chinese. The log, the command line and the errors raised by readers and writers stay in English.

- `djapp/i18n.py` has `tr("English text", name=value)` and `trn(n, "{n} track", "{n} tracks")`. The English text is the key, values go in as named fields so a translation can reorder the words, and one sentence is one key. A missing translation shows the English text.
- `djapp/lang/` has one catalog module per area (`core`, `ui_main`, `ui_details`, `ui_dialogs`, `ui_edit`), each with `CATALOG = {"fr": {...}, "de": {...}, "es": {...}, "zh": {...}}`, listed in `lang/__init__.py` (`PARTS`).
- `python tools/i18n_check.py [--lang fr] [--files]` reads every `tr` and `trn` call with the syntax tree and lists the texts that are missing, have other fields than the key, or are not plain strings (a variable or an f-string cannot be translated).

To add a language:

1. Add its code and its name written in that language to `LANGUAGES` in `i18n.py`.
2. Add a dictionary for the code in each catalog of `lang/`.
3. Run `tools/i18n_check.py --lang <code>` until nothing is missing.
4. The buttons of the standard Qt dialogs follow Qt's own translations: `app.qt_translator` loads them, and the list `KEEP_QT_TRANSLATIONS` in `build.py` names the files a build keeps.

## Settings and files

| File or folder | Where | What |
| --- | --- | --- |
| `boothbridge.json` | Data folder | Settings. Written through a temporary file and a replace. A broken file gives the defaults and a log line |
| `boothbridge.log` | Data folder | The log. Paths and track names may appear in it, read it before sharing it |
| `anlz/` | Cache folder | Analysis files per track (`<hash>.DAT` and `.EXT`), reused by the next export |
| `wave/` | Cache folder | Waveform data for the editors |
| `boothbridge_checksums.json` | In each export folder | SHA-256, size and time of every file the export wrote |
| `export.pdb.bak` | Next to `export.pdb` on a key | The database that the export replaced or merged into |
| `<audio file>.bb-tags.bak` | Next to an audio file | The first copy of a file whose tags were written. It is never overwritten |
| `.bb-tags-*` | Next to an audio file | The working copy of a tag write, left behind only if the program is killed |
| `*.zip` and `*.zip.sha256` | Where you choose | A project backup and its checksum file |

The data folder (`settings.data_dir()`) is `%APPDATA%\BSM\Boothbridge` on Windows and `~/.config/BSM/Boothbridge` elsewhere. A portable copy, the zip, has a `portable.flag` file beside the program and keeps the settings and the log in its own folder instead (the installer does not carry the flag). The cache folder (`settings.cache_dir()`) is the `cache` folder of the home folder, in both cases. `BOOTHBRIDGE_HOME` replaces the home folder (the tests set it to a temporary folder, so that no test touches your settings). `settings.migrate()` runs once at the start: it copies the settings of an older version (beside the program) and moves its caches (`%LOCALAPPDATA%\Boothbridge`), never over what is already there.

The settings keys and their defaults are in `settings.DEFAULTS`.

## Safeguards

| Safeguard | What it does | Where |
| --- | --- | --- |
| Copy verification | Audio is copied in blocks and compared with its SHA-256, the whole export can be read back from the drive and compared with the manifest | `export/files.py`, `export/manifest.py` |
| Verify later | `--verify OUTPUT` or Tools > Verify an export rechecks a stored export. A file a player may rewrite (database, analysis) is reported as modified, not as damaged | `manifest.verify` |
| Atomic writes | A generated file is written beside its target and replaced when complete, so it is whole or absent | `manifest.write_atomic`, `replace` |
| Read back | Generated database and analysis bytes are validated, written, read back from the drive and compared before they replace anything | `cdj/validate.py` |
| Retry on a lock | Windows refuses a file for a moment while an antivirus or the indexer looks at it, the operation is retried for about two seconds | `manifest.settle` |
| Safe tag writing | Edit a copy, read it back, keep the first `.bb-tags.bak`, check the source did not change, replace atomically | `tagwrite.py` |
| Backups | A ZIP is built beside its destination, read back, hashed, then moved into place. A restore goes into a new folder only and checks names, sizes, hashes and a ratio limit first | `backup.py` |
| Input limits | A collection file above 256 MiB is refused before it is read, element counts and depth are bounded, numbers that are not finite are dropped | `sources/__init__.py` |
| Safe XML | XML goes through `defusedxml`: entities and external references are refused | `sources/__init__.py` |
| Archive limits | A backup archive is bounded in entries, central directory size and expansion ratio, names are validated, multi-volume and encrypted archives are refused | `backup.py` |
| Protected databases | `master.db`, `exportLibrary.db`, `.itl` and `.musiclibrary`, and any database file that does not start with the SQLite header, are never opened | `protected.py` |
| Startup check | A build compares its compiled files with a list of SHA-256 written at build time and stops on a difference. A run from the sources is not checked | `integrity.py` |
| Closing | The main window refuses to close while a job writes | `ui/main_window.py` |

None of this makes a copy of your collection unnecessary. Keep the originals of your files.

## Run, command line and build

### From source

Python 3.11 or newer, on Windows, macOS or Linux:

```bash
pip install -r requirements.txt
python main.py
```

### Tests

```bash
pip install -r tests/requirements.txt
python -m pytest -q tests
```

The suite runs offscreen (no window opens). Some tests are skipped on purpose when a file is missing: the ones that compare with a key written by rekordbox (the author does not publish files written by other programs), and the ones of the native core when it is not built. `python tests/test_center.py` opens the test center, a window that runs suites, random sessions, memory measurements and fuzzing.

### Command line

No window, for scripts:

```bash
python main.py --export COLLECTION OUTPUT [--format "Rekordbox XML"] [--music FOLDER] [--keep | --replace]
python main.py --verify OUTPUT
python main.py --help
```

`--format` takes any name of `settings.EXPORTS`, in any case. `--keep` and `--replace` apply to a CDJ key. The exit code is 0 on success, 1 on an error and 2 on a wrong command line, and the report goes to `boothbridge.log` and the console.

### Build the executable (Windows)

`Build.bat` creates a `.venv`, installs `requirements.txt`, PyInstaller, Cython and PyArmor, and runs `python build.py`. You need Python 3.11 or newer on the PATH and MinGW gcc (MSYS2 ucrt64 in `C:\msys64\ucrt64\bin`, or on the PATH, or named by `CC` and `BB_CXX`). `build.py --no-zip` skips the zip.

```
Build.bat -> .venv, pip install
   |
build.py
   +- clear dist/ and build/ (renamed first, stops if a copy is running)
   +- compile_core.py
   |     stage: copy djapp/ and main.py to build/stage
   |     core/fastcore.cpp -> fastcore.dll beside native.py
   |     Cython translates every module to C, gcc builds the native .pyd
   |     PyArmor: sources/__init__.py and export/__init__.py
   |     seal: SHA-256 of every native file into djapp/_seal.py
   +- PyInstaller (boothbridge.spec) -> dist/Boothbridge/
   +- prune Qt parts and source files, check the folder against the seal
   +- LICENSE.txt and THIRD_PARTY.md copied in
   +- dist/Portable_Boothbridge-<version>-win64.zip
installer/Boothbridge.iss (Inno Setup) packs dist/Boothbridge/ as it is
```

Points to know:

- In a build every module becomes a native one, except `djapp/__init__.py`, `__main__.py` and `integrity.py`, which run before the seal is checked and stay Python. The two `__init__` files that carry logic (`sources/`, `export/`) go through PyArmor.
- The C++ core in `core/` is plain C functions loaded with ctypes: flat arrays in, bytes out. `python compile_core.py --native` builds only that library next to the sources. The compiler is `BB_CXX` if set, then `g++` on the PATH, then the MSYS2 ucrt64 one, then `clang++`. With `BB_PURE_PYTHON=1` the Python versions run instead, and they give the same files.
- Build from a folder with a short path, for example `C:\bb`. The compilers and the cache they make produce long paths and Windows may refuse them.
- The installer upgrades an existing install and deletes the old `runtime` folder first: the startup check stops the program if a native file it does not know is left there.

## Coding conventions

Read the existing code first and follow it, it is the style guide.

- Most files start with the author line and one line saying what the file is for.
- No docstrings. Comments are plain, short, and say why, not what.
- English everywhere in the code, the comments and the documentation. Texts of the interface go through `tr`.
- `ruff check` and `ruff format` set the rules, with 88 columns (`ruff.toml`). `from __future__ import annotations` at the top.
- Units and ranges are the ones of [Data model](#data-model), convert at the boundary of a format and nowhere else.
- The Python versions of what the C++ core does stay the reference and the fallback. A change to one side is made on both.
- A new text of the interface is a `tr` call with a plain string, then its four translations, then `tools/i18n_check.py`.
- No tag library and no decryption library are added: the program writes tags with its own code and never opens an encrypted database.
- No dash longer than a hyphen in texts, comments or messages. Use a comma, a colon or parentheses.

## Contributing

Changes are welcome for personal and noncommercial use. There is no promise of support, of a reply or of a date: this is a one person project made in free time.

- Open an issue or send a change that does one thing and says why.
- Follow the method above: only public documentation and files exported by the user, no disassembly, no code or resource of another program, no file written by another vendor's program in the repository.
- A change is accepted only if its author writes this sentence in the issue or the pull request: "I wrote this change myself, and I grant Benoit Saint-Moulin a perpetual, worldwide, irrevocable licence to use, modify, distribute and sublicense it under any terms, including commercial ones." The author is the only holder of the rights on the code as a whole and has to stay able to license it, which a contribution without that sentence would prevent. A change without it is not merged.
- The author may decline any change without giving a reason.

## Legal basis

Boothbridge is an interoperability tool. File formats and the functionality of a program are not protected by copyright (Court of Justice of the EU, SAS Institute v World Programming, C-406/10), and the lawful user of a program may observe, study and test how it works (Directive 2009/24/EC, article 5.3), without the decompilation that article 6 would regulate, because none is done. The names of other programs and devices belong to their owners, Boothbridge is independent and not affiliated with any of them. This is the author's understanding of the position in the European Union and it is not legal advice. The code is under the PolyForm Noncommercial License 1.0.0 (`LICENSE`), third party libraries are listed in `THIRD_PARTY.md`.
