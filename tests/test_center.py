# Boothbridge
# © Benoit Saint-Moulin - 2026
# Version du fichier : 1.0.0
# Fonction du fichier : Entry point of the test center, which opens its window or runs the same sessions headless.

# Sans argument, ouvre la fenêtre. Avec --monkey, --memory ou --random, la même séance tourne sans fenêtre (voir tests/center/cli.py).
# python tests/test_center.py

from __future__ import annotations

import json
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TESTS = Path(__file__).resolve().parent
REPORT = TESTS / "report.txt"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from djapp import APP_NAME, VERSION
from djapp.ui import theme
from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QDoubleSpinBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSplitter,
    QTabWidget,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from tests import APP_ROOT
from tests.center import cli, header, new_run_path, save_run
from tests.center.panels import (
    MemoryPanel,
    MonkeyPanel,
    RandomPanel,
    ReportsPanel,
    seconds_text,
)
from tests.center.runproc import RunProcess, child_env
from tests.center.suiterun import MARK

try:
    import human_steps
except ImportError:
    from tests import human_steps


# ------------------------------------------------------------
# discover unittest modules without including this GUI launcher
def available_suites() -> list[tuple[str, str]]:
    labels = {
        "test_drives": "Lecteurs amovibles (simulation)",
        "test_fat32": "Format FAT32 (image temporaire)",
        "test_timeline": "Éditeur avancé de cues",
        "test_lifecycle": "Cycle de vie : threads, fuites, fichiers temporaires",
        "test_leaks_final": "Fuites mesurées : fenêtre Log à la fermeture, plafonds, copies, écritures, analyses",
        "test_tagwrite": "Écriture sûre des tags audio",
        "test_tageditor": "Éditeur d'ID Tag",
        "test_backup": "Backup ZIP, SHA-256, vérification et restauration",
        "test_backup_ui": "Backup ZIP depuis l'interface",
        "test_metronome": "Métronome synchronisé",
        "test_beatdetect": "Détection BPM et première pulsation",
        "test_timeline_detect": "Détection de grille dans la timeline",
        "test_batch_detect": "Détection en lot et annulation groupée",
        "test_row_menu": "Menu du clic droit sur un morceau",
        "test_player_changes": "Fichiers modifiés par un lecteur, playlists sans fichier",
        "test_never_decrypt": "Jamais de déchiffrement des bases Pioneer",
        "test_tagfile": "Tags audio maison : ID3, FLAC, Ogg, MP4, WAV, AIFF",
        "test_pdb_oracle": "export.pdb comparé à un vrai export rekordbox",
        "test_anlz_oracle": "Fichiers ANLZ comparés à un export rekordbox, cues et couleurs",
        "test_export_message": "Message de fin d export : morceaux écrits et fichiers absents",
        "test_engine": "Lecture de Denon Engine DJ (m.db, blobs, playlists)",
        "test_fuzz_readers": "Fuzz : fichiers audio, bases Engine",
        "test_review_tags": "Relecture indépendante : tags MP4, WAV, sauvegarde, Engine",
        "test_review_v1": "Relecture sceptique avant la 1.0.0 : réglages, ligne de commande, chemins, tags",
        "test_review_final": "Relecture avant livraison : pochette, écriture, iTunes, Engine, aide",
        "test_itunes": "Lecture de Library.xml (iTunes, Music)",
        "test_tree": "Arbre des sources : rien du passé, rien de personnel, renommage",
        "test_keyboard": "Raccourcis et navigation clavier",
        "test_details": "Playlist, historique et présence dans les autres playlists",
        "test_launcher": "Lanceur : un processus de travail sans parent part en silence",
        "test_library": "Bibliothèque musicale et doublons exacts",
        "test_musicbrowser": "Navigateur flottant, préécoute et glisser-déposer",
        "test_regression": "Non-régression complète : sources, exports, tags et backup",
        "test_center_tools": "Outils du centre de test : monkey, aléatoire, mémoire",
        "test_analysis_optimization": "Optimisation de l'analyse audio",
        "test_wave_pace": "Waveform : chaque colonne au bon moment (44,1 kHz)",
        "test_app_cli_options": "Ligne de commande : --help et options inconnues",
        "test_free": "Programme libre : ni clé, ni verrou, tous les formats ouverts",
        "test_audio_buffer": "Tampon audio du lecteur",
        "test_audio_decoders": "Décodeurs audio",
        "test_audiocheck": "Vérification des fichiers audio",
        "test_audiocheck_ui": "Vérification des fichiers audio : interface",
        "test_backup_panel": "Panneau Backup",
        "test_bad_numbers": "Nombres invalides (inf, nan) dans les collections",
        "test_cache_backup": "Sauvegarde et restauration du cache d'analyses",
        "test_fat32_spec": "FAT32 : géométrie et structures selon la spécification",
        "test_format_dialog": "Formatage USB : dernière confirmation, un seul disque visé",
        "test_export_faults": "Exports interrompus : écriture en échec, annulation, copie manquante",
        "test_export_fit": "Exports : valeurs extrêmes, noms de périphériques, deux morceaux au même nom",
        "test_folder_loading": "Chargement d'un dossier de musique",
        "test_formats_final": "Formats de fichiers : valeurs de types inattendus, jonctions, noms trop longs, projet",
        "test_support": "Lien de soutien Ko-fi une seule fois, constructeur public et privé",
        "test_release": "Sommes de contrôle et script d'installation",
        "test_signal_slots": "Fonctions reliées aux signaux Qt : acceptent tous les arguments (programme compilé par Cython)",
        "test_data_folder": "Dossier de données : %APPDATA%/BSM, mode portable, migration sans écraser, drapeau du zip",
        "test_pdb_edge_cases": "Base CDJ : listes imbriquées, vides, émoji, textes très longs, plus de 65535 morceaux",
        "test_i18n": "Langues : mécanique des traductions, catalogues complets en fr, de, es et zh",
        "test_export_mixxx": "Export Mixxx : base neuve, playlists, cues, refus d'une base existante",
        "test_export_lists": "Exports CSV, JSON, PLS et XSPF : fichiers bien formés, tableur protégé",
        "test_review_exports": "Relecture des exports : refus Mixxx, dossier relatif, tableur, noms de formats",
        "test_export_vdj_itunes":"Exports VirtualDJ et iTunes XML : playlists, dossiers, noms difficiles",
        "test_hostile_json": "Projets JSON hostiles",
        "test_key_artwork": "Clé CDJ : pochettes lors d'une fusion",
        "test_key_cancel": "Clé CDJ : annulation de la lecture",
        "test_key_collision": "Clé CDJ : deux morceaux au même chemin",
        "test_key_edges": "Clé CDJ : cas limites du lecteur",
        "test_key_foreign": "Clé CDJ : fichiers écrits par rekordbox",
        "test_key_fuzz": "Clé CDJ : fichiers abîmés (fuzz)",
        "test_key_manifest": "Clé CDJ : checksums après une fusion",
        "test_key_merge": "Clé CDJ : fusion des playlists",
        "test_key_merge_ui": "Clé CDJ : fenêtres de fusion et d'ouverture",
        "test_key_open": "Clé CDJ : ouvrir une clé",
        "test_key_reader": "Clé CDJ : lecteur",
        "test_key_reuse": "Clé CDJ : analyses conservées, annulation",
        "test_main_window_drop": "Fenêtre principale : glisser-déposer, infobulles, cases à cocher",
        "test_native_hardening": "Durcissement du module natif",
        "test_pioneer_integrity": "Intégrité des fichiers Pioneer",
        "test_player": "Lecteur audio",
        "test_cue_palette": "Clé CDJ : couleurs des hot cues (palette du lecteur)",
        "test_playlist_remove_rename": "Renommer et retirer une playlist",
        "test_post_player_verification": "Vérification après usage sur un lecteur",
        "test_release_packaging": "Empaquetage de la version",
        "test_release_review_fixes": "Corrections de la revue de version",
        "test_review_lifecycle": "Cycle de vie : fenêtres et fermeture",
        "test_review_i18n": "Revue des langues : processus de formatage, textes du formateur, traductions Qt du build",
        "test_tagwrite_preserve": "Tags : champs inchangés, totaux, copies temporaires",
        "test_theme": "Thème",
        "test_timing_exports": "Durées des exports",
        "test_blind_spots": "Collections inhabituelles et fichiers verrouillés un instant",
        "test_untrusted_inputs": "Entrées non fiables",
        "test_usage_dialog": "Aide intégrée : sommaire, recherche, texte",
        "test_xml_memory": "Lecteurs XML : lecture en flux, plafonds de mémoire",
        "test_waveform": "Calcul et cache des waveforms",
        "test_zoomwave": "Waveform zoomée",
    }
    # test_key_support only holds the helpers of the test_key_* files, it has no test to run
    return [
        (path.stem, labels.get(path.stem, path.stem.removeprefix("test_").replace("_", " ").capitalize()))
        for path in sorted(TESTS.glob("test_*.py"))
        if path.stem not in {"test_center", "__init__", "test_key_support"}
    ]


# ------------------------------------------------------------------
# runs the suites one after the other, each in its own child process (QProcess).
#
# A job is a suite name and, to run again only what failed, the ids of its tests
class TestWorker(QObject):
    __test__ = False  # not a test case, whatever its name says

    output = Signal(str)
    suite_started = Signal(str, int, int)
    suite_finished = Signal(str, int)
    test_event = Signal(str, dict)
    report_ready = Signal(str, bool, int)

    def __init__(self, jobs: list[tuple[str, list[str]]], timeout: float = 0.0):
        super().__init__()
        self.jobs = jobs
        self.suites = [name for name, _ids in jobs]
        self.timeout = timeout
        self.cancel_requested = False
        self.index = 0
        self.passed = 0
        self.failed = 0
        self.lines: list[str] = []
        self.proc: RunProcess | None = None
        self.finished = False
        self.codes: dict[str, int] = {}
        self.seconds: dict[str, float] = {}
        self.began = 0.0
        self.suite_began = 0.0

    def isRunning(self) -> bool:
        return not self.finished

    def wait(self, ms: int = 3000) -> bool:
        if self.proc is not None:
            self.proc.wait(ms)
        return self.finished

    def start(self):
        self.began = time.monotonic()
        self.lines = [
            f"{APP_NAME} {VERSION} - rapport de tests",
            f"Date : {datetime.now().astimezone():%Y-%m-%d %H:%M:%S %Z}",
            "",
        ]
        QTimer.singleShot(0, self.next_suite)

    def stop(self):
        self.cancel_requested = True
        if self.proc is not None:
            self.proc.stop()

    def next_suite(self):
        if self.cancel_requested or self.index >= len(self.jobs):
            self.finish()
            return
        suite, ids = self.jobs[self.index]
        self.index += 1
        self.suite_started.emit(suite, self.index, len(self.jobs))
        self.lines.append(f"===== {suite} =====")
        self.output.emit(f"\n===== {suite} =====")
        self.suite_began = time.monotonic()
        self.proc = RunProcess(self)
        self.proc.line.connect(lambda text, s=suite: self.on_line(s, text))
        self.proc.done.connect(lambda code, timed_out, stopped, s=suite: self.suite_done(s, code, timed_out))
        names = ids or [f"tests.{suite}"]
        self.proc.start(["-m", "tests.center.suiterun", *names], self.timeout, child_env())

    def on_line(self, suite: str, text: str):
        if text.startswith(MARK):
            try:
                self.test_event.emit(suite, json.loads(text[len(MARK):]))
            except ValueError:
                pass
            return
        self.lines.append(text)
        self.output.emit(text)

    def suite_done(self, suite: str, code: int, timed_out: bool):
        if code == -1 and not self.cancel_requested:
            message = f"Impossible de lancer {suite}"
            self.lines.append(message)
            self.output.emit(message)
        if self.cancel_requested and code != 0:
            self.lines.append("Interrompu.")
        elif timed_out:
            self.lines.append("Temps limite atteint.")
            self.failed += 1
        elif code:
            self.failed += 1
        else:
            self.passed += 1
        self.codes[suite] = code
        self.seconds[suite] = time.monotonic() - self.suite_began
        self.suite_finished.emit(suite, code)
        self.lines.append("")
        QTimer.singleShot(0, self.next_suite)

    def finish(self):
        cancelled = self.cancel_requested
        state = "interrompu" if cancelled else ("échec" if self.failed else "réussi")
        self.lines.extend((f"Résultat : {state}", f"Suites réussies : {self.passed}", f"Suites en échec : {self.failed}"))
        self.finished = True
        self.report_ready.emit("\n".join(self.lines) + "\n", cancelled, self.failed)


# ------------------------------------------------------
class HumanTestDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Parcours de test manuel")
        self.resize(700, 480)
        self.steps = human_steps.STEPS
        self.index = 0
        self.answers: dict[int, tuple[str, str]] = {}
        self.report_text = ""

        layout = QVBoxLayout(self)
        self.position = QLabel()
        self.position.setStyleSheet("font-weight: 600; color: #00b4d8;")
        self.heading = QLabel()
        self.heading.setStyleSheet("font-size: 15pt; font-weight: 600;")
        self.action = QLabel()
        self.action.setWordWrap(True)
        self.expected = QLabel()
        self.expected.setWordWrap(True)
        self.expected.setStyleSheet("color: #adb5bd;")
        self.note = QPlainTextEdit()
        self.note.setPlaceholderText("Pour un KO, décris brièvement ce qui s'est passé.")
        self.note.setMaximumHeight(84)
        layout.addWidget(self.position)
        layout.addWidget(self.heading)
        layout.addWidget(QLabel("À FAIRE"))
        layout.addWidget(self.action)
        layout.addWidget(QLabel("RÉSULTAT ATTENDU"))
        layout.addWidget(self.expected)
        layout.addWidget(self.note)

        row = QHBoxLayout()
        self.launch = QPushButton("Lancer Boothbridge")
        self.launch.clicked.connect(self.launch_app)
        row.addWidget(self.launch)
        row.addStretch(1)
        for label, answer in (("OK", "ok"), ("KO", "ko"), ("Passer", "skip")):
            button = QPushButton(label)
            button.clicked.connect(lambda _checked=False, value=answer: self.answer(value))
            row.addWidget(button)
        layout.addLayout(row)
        self.show_step()

    def show_step(self):
        zone, title, action, expected = self.steps[self.index]
        self.position.setText(f"{zone} · Étape {self.index + 1} sur {len(self.steps)}")
        self.heading.setText(title)
        self.action.setText(action)
        self.expected.setText(expected)
        _answer, note = self.answers.get(self.index, ("", ""))
        self.note.setPlainText(note)

    def launch_app(self):
        try:
            subprocess.Popen([sys.executable, "-m", "djapp"], cwd=APP_ROOT)
        except OSError as error:
            QMessageBox.critical(self, "Lancement impossible", str(error))

    def answer(self, value: str):
        if value == "ko" and not self.note.toPlainText().strip():
            QMessageBox.information(self, "Précision requise", "Ajoute une courte note pour décrire le problème.")
            return
        self.answers[self.index] = (value, self.note.toPlainText().strip())
        if self.index + 1 < len(self.steps):
            self.index += 1
            self.show_step()
        else:
            self.finish()

    def finish(self):
        ok = sum(answer == "ok" for answer, _note in self.answers.values())
        ko = [(self.steps[i][0], self.steps[i][1], note) for i, (_answer, note) in self.answers.items()
              if self.answers[i][0] == "ko"]
        skipped = sum(answer == "skip" for answer, _note in self.answers.values())
        lines = [
            f"{APP_NAME} {VERSION} - parcours manuel",
            f"Date : {datetime.now().astimezone():%Y-%m-%d %H:%M:%S %Z}",
            f"{ok} OK, {len(ko)} KO, {skipped} passées sur {len(self.steps)} étapes.",
            "",
        ]
        lines.extend(f"KO - {zone} / {title} : {note}" for zone, title, note in ko)
        if not ko:
            lines.append("Aucun KO signalé.")
        self.report_text = "\n".join(lines) + "\n"
        self.accept()


STATUS_COLORS = {"ok": theme.GREEN, "echec": theme.RED, "erreur": theme.RED, "saute": theme.FG_MUTED}
BAD = ("echec", "erreur")


# ------------------------------------------------------------
class TestCenter(QMainWindow):
    __test__ = False

    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"{APP_NAME} {VERSION} - Centre de test")
        self.resize(1250, 780)
        self.worker: TestWorker | None = None
        self.report_text = ""
        self.items: dict[str, QListWidgetItem] = {}
        self.tree_items: dict[str, QTreeWidgetItem] = {}
        self.test_items: dict[str, QTreeWidgetItem] = {}
        self.results: dict[str, list[dict]] = {}
        self.failed_ids: dict[str, list[str]] = {}
        self.counts = {"ok": 0, "echec": 0, "erreur": 0, "saute": 0}
        self.began = 0.0

        self.tabs = QTabWidget()
        self.setCentralWidget(self.tabs)
        self.tabs.addTab(self.build_suites(), "Suites")
        self.random = RandomPanel()
        self.monkey = MonkeyPanel()
        self.memory = MemoryPanel()
        self.reports = ReportsPanel()
        for panel, name in ((self.random, "Aléatoire"), (self.monkey, "Monkey"), (self.memory, "Mémoire"),
                            (self.reports, "Rapports")):
            self.tabs.addTab(panel, name)
        for panel in (self.random, self.monkey, self.memory):
            panel.run_finished.connect(lambda _path: self.reports.refresh())
        self.reports.replay_requested.connect(self.replay_record)

    # ---- the Suites tab

    def build_suites(self) -> QWidget:
        root = QWidget()
        layout = QVBoxLayout(root)

        toolbar = QHBoxLayout()
        self.run_all_button = QPushButton("Tout lancer")
        self.run_all_button.setObjectName("go")
        self.run_all_button.setToolTip("Lance toutes les suites visibles (le filtre les réduit).")
        self.run_all_button.clicked.connect(self.run_all)
        toolbar.addWidget(self.run_all_button)
        self.run_selected_button = QPushButton("Lancer la sélection")
        self.run_selected_button.clicked.connect(self.run_selected)
        toolbar.addWidget(self.run_selected_button)
        self.retry_button = QPushButton("Relancer les échecs")
        self.retry_button.setToolTip("Relance seulement les tests qui ont échoué à la dernière séance.")
        self.retry_button.setEnabled(False)
        self.retry_button.clicked.connect(self.run_failed)
        toolbar.addWidget(self.retry_button)
        self.manual_button = QPushButton("Test manuel guidé")
        self.manual_button.clicked.connect(self.run_manual)
        toolbar.addWidget(self.manual_button)
        self.open_app_button = QPushButton("Ouvrir Boothbridge")
        self.open_app_button.clicked.connect(self.open_app)
        toolbar.addWidget(self.open_app_button)
        toolbar.addStretch(1)
        self.stop_button = QPushButton("Arrêter")
        self.stop_button.setEnabled(False)
        self.stop_button.clicked.connect(self.stop_tests)
        toolbar.addWidget(self.stop_button)
        layout.addLayout(toolbar)

        note = QLabel("Le formatage FAT32 est testé sur une image temporaire - aucun disque réel n'est touché.")
        note.setWordWrap(True)
        note.setStyleSheet("color: #adb5bd; padding: 2px 0 6px;")
        layout.addWidget(note)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        self.filter = QLineEdit()
        self.filter.setPlaceholderText("Filtrer les suites et les tests")
        self.filter.setClearButtonEnabled(True)
        self.filter.textChanged.connect(self.apply_filter)
        left_layout.addWidget(self.filter)
        marks = QHBoxLayout()
        self.check_all_button = QPushButton("Tout cocher")
        self.check_all_button.clicked.connect(lambda: self.set_checks(Qt.CheckState.Checked))
        self.check_none_button = QPushButton("Tout décocher")
        self.check_none_button.clicked.connect(lambda: self.set_checks(Qt.CheckState.Unchecked))
        marks.addWidget(self.check_all_button)
        marks.addWidget(self.check_none_button)
        left_layout.addLayout(marks)
        self.suite_list = QListWidget()
        self.suite_list.setMinimumWidth(250)
        for name, label in available_suites():
            item = QListWidgetItem(label)
            item.setData(Qt.ItemDataRole.UserRole, name)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Checked)
            self.suite_list.addItem(item)
            self.items[name] = item
        left_layout.addWidget(self.suite_list, 1)
        splitter.addWidget(left)

        right = QSplitter(Qt.Orientation.Vertical)
        self.results_tree = QTreeWidget()
        self.results_tree.setHeaderLabels(["Suite / test", "État", "Durée"])
        self.results_tree.setColumnWidth(0, 520)
        self.results_tree.setColumnWidth(1, 80)
        self.results_tree.currentItemChanged.connect(self.show_test)
        right.addWidget(self.results_tree)
        self.detail = QPlainTextEdit()
        self.detail.setReadOnly(True)
        self.detail.setFont(QFont("Consolas", 9))
        self.detail.setPlaceholderText("Sélectionne un test en échec pour voir son message.")
        self.detail.setMaximumHeight(150)
        right.addWidget(self.detail)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setPlaceholderText("Les résultats des tests apparaîtront ici.")
        self.log.setFont(QFont("Consolas", 9))
        right.addWidget(self.log)
        right.setSizes([260, 90, 220])
        splitter.addWidget(right)
        splitter.setSizes([340, 880])
        layout.addWidget(splitter, 1)

        footer = QHBoxLayout()
        self.progress = QProgressBar()
        self.progress.setValue(0)
        self.status = QLabel("Prêt.")
        self.summary = QLabel("")
        self.suite_timeout = QDoubleSpinBox()
        self.suite_timeout.setRange(0, 600)
        self.suite_timeout.setDecimals(0)
        self.suite_timeout.setSuffix(" min")
        self.suite_timeout.setSpecialValueText("sans limite")
        self.suite_timeout.setValue(30)
        self.suite_timeout.setToolTip("Temps limite d'une suite : au-delà, son processus est arrêté.")
        self.copy_button = QPushButton("Copier le rapport")
        self.copy_button.clicked.connect(self.copy_report)
        footer.addWidget(self.progress, 2)
        footer.addWidget(self.status, 3)
        footer.addWidget(self.summary, 3)
        footer.addWidget(QLabel("Limite par suite"))
        footer.addWidget(self.suite_timeout)
        footer.addWidget(self.copy_button)
        layout.addLayout(footer)
        return root

    def visible_suites(self) -> list[str]:
        return [name for name, item in self.items.items() if not item.isHidden()]

    def apply_filter(self, text: str):
        needle = text.strip().lower()
        for name, item in self.items.items():
            tests = [c.text(0).lower() for c in self.child_items(name)]
            hit = not needle or needle in name.lower() or needle in item.text().lower() or any(needle in t for t in tests)
            item.setHidden(not hit)
            top = self.tree_items.get(name)
            if top is not None:
                top.setHidden(not hit)
                for child in self.child_items(name):
                    child.setHidden(bool(needle) and needle not in child.text(0).lower()
                                    and needle not in name.lower() and needle not in item.text().lower())

    def child_items(self, name: str) -> list[QTreeWidgetItem]:
        top = self.tree_items.get(name)
        return [top.child(i) for i in range(top.childCount())] if top is not None else []

    def set_checks(self, state):
        for item in self.items.values():
            if not item.isHidden():
                item.setCheckState(state)

    def run_all(self):
        self.start_tests([(name, []) for name in self.visible_suites()])

    def run_selected(self):
        selected = [name for name, item in self.items.items()
                    if item.checkState() == Qt.CheckState.Checked and not item.isHidden()]
        if not selected:
            QMessageBox.information(self, "Aucun test sélectionné", "Coche au moins une suite à lancer.")
            return
        self.start_tests([(name, []) for name in selected])

    def run_failed(self):
        jobs = []
        for name, ids in self.failed_ids.items():
            whole = any(i.startswith("unittest.loader") for i in ids) or not ids
            jobs.append((name, [] if whole else ids))
        if jobs:
            self.start_tests(jobs)

    def start_tests(self, jobs: list[tuple[str, list[str]]]):
        if self.worker and self.worker.isRunning():
            return
        if not jobs:
            self.status.setText("Aucune suite à lancer.")
            return
        self.log.clear()
        self.detail.clear()
        self.results_tree.clear()
        self.tree_items.clear()
        self.test_items.clear()
        self.results = {name: [] for name, _ids in jobs}
        self.failed_ids = {}
        self.counts = dict.fromkeys(self.counts, 0)
        self.summary.setText("")
        self.report_text = ""
        self.began = time.monotonic()
        for name, _ids in jobs:
            top = QTreeWidgetItem([self.items[name].text() if name in self.items else name, "attente", ""])
            top.setData(0, Qt.ItemDataRole.UserRole, name)
            self.results_tree.addTopLevelItem(top)
            self.tree_items[name] = top
        self.progress.setRange(0, len(jobs))
        self.progress.setValue(0)
        self.set_running(True)
        self.worker = TestWorker(jobs, self.suite_timeout.value() * 60)
        self.worker.output.connect(self.log.appendPlainText)
        self.worker.suite_started.connect(self.on_suite_started)
        self.worker.suite_finished.connect(self.on_suite_finished)
        self.worker.test_event.connect(self.on_test)
        self.worker.report_ready.connect(self.on_report_ready)
        self.worker.start()

    def set_running(self, running: bool):
        for button in (self.run_all_button, self.run_selected_button, self.manual_button, self.open_app_button,
                       self.check_all_button, self.check_none_button):
            button.setEnabled(not running)
        self.retry_button.setEnabled(not running and bool(self.failed_ids))
        self.suite_list.setEnabled(not running)
        self.stop_button.setEnabled(running)

    def on_suite_started(self, suite: str, index: int, total: int):
        label = self.items[suite].text() if suite in self.items else suite
        self.status.setText(f"Suite {index}/{total} : {label}…")
        if suite in self.items:
            self.items[suite].setForeground(QColor(theme.ACCENT))
        top = self.tree_items.get(suite)
        if top is not None:
            top.setText(1, "en cours")
            self.results_tree.scrollToItem(top)

    def short_id(self, suite: str, ident: str) -> str:
        return ident.removeprefix(f"tests.{suite}.")

    def on_test(self, suite: str, event: dict):
        top = self.tree_items.get(suite)
        if top is None:
            return
        status = event.get("status", "ok")
        self.counts[status] = self.counts.get(status, 0) + 1
        self.results.setdefault(suite, []).append(event)
        child = QTreeWidgetItem([self.short_id(suite, event.get("id", "?")), status, f"{event.get('ms', 0)} ms"])
        child.setForeground(1, QColor(STATUS_COLORS.get(status, theme.FG)))
        child.setData(0, Qt.ItemDataRole.UserRole, event)
        top.addChild(child)
        self.test_items[event.get("id", "")] = child
        if status in BAD:
            self.failed_ids.setdefault(suite, []).append(event["id"])
            top.setExpanded(True)
        self.refresh_summary()

    def refresh_summary(self):
        c = self.counts
        seconds = time.monotonic() - self.began if self.began else 0.0
        self.summary.setText(f"{c['ok']} réussis, {c['echec']} échecs, {c['erreur']} erreurs, {c['saute']} sautés"
                             f" - {seconds_text(seconds)}")

    def show_test(self, item, _previous=None):
        event = item.data(0, Qt.ItemDataRole.UserRole) if item else None
        if isinstance(event, dict):
            self.detail.setPlainText(event.get("msg", "") or "Aucun message.")
        else:
            self.detail.clear()

    def on_suite_finished(self, suite: str, code: int):
        item = self.items.get(suite)
        failures = len(self.failed_ids.get(suite, []))
        color = theme.GREEN if code == 0 else theme.RED
        if item is not None:
            item.setForeground(QColor(color))
        top = self.tree_items.get(suite)
        if top is not None:
            seconds = self.worker.seconds.get(suite, 0.0) if self.worker else 0.0
            tests = self.results.get(suite, [])
            top.setText(1, "réussie" if code == 0 else "échec")
            top.setForeground(1, QColor(color))
            top.setText(2, f"{seconds_text(seconds)}, {len(tests)} tests" + (f", {failures} en échec" if failures else ""))
            if code and not failures:
                self.failed_ids.setdefault(suite, [])  # crashed before reporting a test: run it whole again
        self.progress.setValue(self.progress.value() + 1)

    def on_report_ready(self, report: str, cancelled: bool, failed: int):
        self.report_text = report
        self.log.appendPlainText("\n" + report.split("Résultat :")[-1].strip())
        self.set_running(False)
        state = "Tests interrompus." if cancelled else ("Terminé avec des échecs." if failed else "Tous les tests ont réussi.")
        self.status.setText(state)
        self.refresh_summary()
        self.save_report(report)
        self.save_record(cancelled, failed)
        self.reports.refresh()

    def save_record(self, cancelled: bool, failed: int):
        suites = [{"name": name, "code": self.worker.codes.get(name, -1), "seconds": round(self.worker.seconds.get(name, 0), 2),
                   "tests": self.results.get(name, [])} for name in self.worker.suites]
        record = header("suites", "Suites de tests", [sys.executable, "-m", "tests.center.suiterun"],
                        {"suites": self.worker.suites, "jobs": [[n, ids] for n, ids in self.worker.jobs]})
        record.update(verdict="interrompu" if cancelled else ("echec" if failed else "ok"),
                      summary=self.summary.text(), details={"suites": suites, "counts": self.counts})
        try:
            save_run(new_run_path("suites"), record)
        except OSError as error:
            self.status.setText(f"Rapport JSON non enregistré : {error}")

    def save_report(self, report: str):
        try:
            REPORT.write_text(report, encoding="utf-8")
        except OSError as error:
            self.status.setText(f"Rapport non enregistré : {error}")

    def stop_tests(self):
        if self.worker and self.worker.isRunning():
            self.status.setText("Arrêt en cours…")
            self.worker.stop()

    def run_manual(self):
        dialog = HumanTestDialog(self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.report_text = dialog.report_text
            self.log.setPlainText(dialog.report_text)
            self.status.setText("Parcours manuel terminé.")
            self.save_report(dialog.report_text)
            self.progress.setRange(0, len(human_steps.STEPS))
            self.progress.setValue(len(human_steps.STEPS))

    def open_app(self):
        try:
            subprocess.Popen([sys.executable, "-m", "djapp"], cwd=APP_ROOT)
            self.status.setText("Boothbridge lancé.")
        except OSError as error:
            QMessageBox.critical(self, "Lancement impossible", str(error))
            self.status.setText("Le lancement de Boothbridge a échoué.")

    def copy_report(self):
        QApplication.clipboard().setText(self.report_text or "Aucun rapport pour le moment.")
        self.status.setText("Rapport copié dans le presse-papiers.")

    # ---- replaying a saved report

    def replay_record(self, record: dict):
        kind = record.get("kind")
        panels = {"random": (self.random, 1), "monkey": (self.monkey, 2), "memory": (self.memory, 3)}
        if kind == "suites":
            self.tabs.setCurrentIndex(0)
            names = [n for n, _ids in record.get("params", {}).get("jobs", [])] or record.get("params", {}).get("suites", [])
            self.start_tests([(n, []) for n in names if n in self.items])
            return
        if kind in panels:
            panel, index = panels[kind]
            if panel.running():
                self.reports.status.setText("Cet onglet a déjà une séance en cours.")
                return
            self.tabs.setCurrentIndex(index)
            panel.replay(record)

    # whatever runs in a child process is stopped: the window never waits for it
    def closeEvent(self, event):
        if self.worker and self.worker.isRunning():
            self.worker.stop()
            if not self.worker.wait(3000):
                self.status.setText("Le test s'arrête. Ferme la fenêtre après sa fin.")
                event.ignore()
                return
        for panel in (self.random, self.monkey, self.memory):
            if panel.running() and not panel.wait_stopped():
                event.ignore()
                return
        event.accept()

def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName(f"{APP_NAME} - Centre de test")
    app.setStyleSheet(theme.STYLE)
    window = TestCenter()
    window.show()
    return app.exec()


if __name__ == "__main__":
    if cli.wants_cli(sys.argv[1:]):
        raise SystemExit(cli.main(sys.argv[1:]))
    raise SystemExit(main())
