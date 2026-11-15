# Third-party software

Boothbridge uses the open source libraries below. None of their source code is copied into this
repository: they are installed with `pip install -r requirements.txt`, and a packaged build
carries them next to the program with their own licences.

| Library | Used for | Licence |
| --- | --- | --- |
| PySide6 and Qt (Shiboken) | the windows and the preview player | LGPL-3.0 |
| FFmpeg libraries (shipped with Qt) | the preview player, and the decoding of AAC files for the waveform and the tempo | LGPL-2.1-or-later |
| defusedxml | reading XML collections safely | Python Software Foundation License |
| NumPy | numeric arrays | BSD-3-Clause |
| SciPy | filters and signal processing | BSD-3-Clause |
| librosa | audio loading fallback | ISC |
| SoundFile (libsndfile) | audio decoding | BSD-3-Clause (library: LGPL-2.1) |
| soxr | resampling, through librosa | LGPL-2.1-or-later |
| Pillow | artwork resizing | MIT-CMU |

Reading and writing audio tags is done by the program's own code, written from the public format
specifications of ID3, FLAC, Ogg, MP4 and RIFF/AIFF.

Their transitive dependencies (numba, llvmlite, scikit-learn, joblib and others) are under BSD,
MIT, Apache-2.0 or similar licences. The full text of each licence ships with the package that
carries it.

The LGPL libraries are loaded as separate shared libraries and can be replaced by the user.

Boothbridge itself is under the PolyForm Noncommercial License 1.0.0, see [LICENSE](LICENSE).
