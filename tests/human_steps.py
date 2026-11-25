# Boothbridge
# © Benoit Saint-Moulin - 2026
# Version du fichier : 1.0.0
# Fonction du fichier : Manual check list of the application, one step per entry with its zone, title, action and expected result.

# Le test de formatage ne lance jamais l'effacement d'un disque : il s'arrête avant confirmation.

STEPS = [
    (
        "Démarrage",
        "La fenêtre s'ouvre",
        "Lance Boothbridge depuis le bouton ci-dessous ou avec `python main.py`.",
        "La fenêtre principale apparaît sans erreur et reste réactive.",
    ),
    (
        "Sources",
        "Ouvrir une collection",
        "Ouvre une collection ou une playlist que tu peux utiliser pour un essai.",
        "Les playlists et les morceaux apparaissent, avec les titres et les durées disponibles.",
    ),
    (
        "Sources",
        "Retrouver des morceaux déplacés",
        "Si des morceaux sont manquants, indique leur dossier dans « Music folder ».",
        "Les fichiers retrouvés sont comptés comme disponibles ; les autres restent clairement signalés.",
    ),
    (
        "Playlist",
        "Ouvrir les détails",
        "Ouvre une playlist, sélectionne un morceau et vérifie ses cues et sa waveform.",
        "La sélection suit le morceau choisi et les cues existants restent lisibles.",
    ),
    (
        "Playlist / Historique",
        "Annuler et rétablir les modifications",
        ("Sur une playlist de test, supprime une piste, ajoute un fichier puis déplace une ligne. "
         "Utilise Ctrl+Z pour annuler chaque action et Ctrl+Y pour les rétablir. Vérifie aussi Reload."),
        ("L'ordre et le contenu reviennent exactement ; Reload restaure l'état à l'ouverture et vide l'historique. "
         "Aucun fichier audio n'est supprimé."),
    ),
    (
        "Playlist / Présence",
        "Retrouver une piste dans d'autres playlists",
        "Charge plusieurs playlists contenant une même piste et consulte sa colonne « Also in ».",
        "Le nombre exclut la playlist actuelle ; l'infobulle et la sélection indiquent les autres noms.",
    ),
    (
        "Playlist / Clavier",
        "Naviguer et éditer sans souris",
        ("Utilise Tab, les flèches, Entrée, Ctrl+F puis Échap, Ctrl+I et Ctrl+T. "
         "Sans filtre ni tri, sélectionne plusieurs lignes et utilise Alt+Haut/Bas puis Ctrl+Z. "
         "Appuie sur S dans la liste puis dans le Cue Editor ; tape s dans un champ texte."),
        ("Le focus reste visible, les champs texte gardent la saisie normale, les éditeurs ouvrent la bonne piste "
         "et le déplacement conserve la sélection. S bascule le bouton Snap compact dans chaque éditeur, "
         "mais reste du texte dans les champs. Un filtre ou un tri bloque le réordonnancement."),
    ),
    (
        "Playlist / Détection",
        "Détecter une grille et annuler un lot",
        ("Sur des copies de pistes à tempo constant, utilise Detect (Ctrl+D) dans la timeline puis ajuste à l'écoute. "
         "Dans la playlist sélectionne deux pistes et lance Detect grids, puis Ctrl+Z/Ctrl+Y. "
         "Lance un nouveau lot et utilise Cancel detection."),
        ("La grille proposée peut être corrigée manuellement. Les résultats peu fiables sont ignorés en lot, "
         "les erreurs sont signalées ; un seul Ctrl+Z annule tout le lot. Une annulation de scan n'applique aucun résultat."),
    ),
    (
        "Bibliothèque",
        "Ouvrir et déplacer le navigateur musical",
        ("Dans Preferences > Library, choisis un dossier d'essai avec des sous-dossiers. "
         "Ouvre View > Music browser (Ctrl+M), déplace la fenêtre sur le bureau ou un autre écran et redimensionne-la."),
        ("Les pistes et leurs informations sont visibles sans remplacer la collection. "
         "La fenêtre est non modale et conserve sa position à sa réouverture."),
    ),
    (
        "Bibliothèque",
        "Préécoute et glisser-déposer multiple",
        ("Clique sur une piste pour l'écouter, utilise Pause/Stop puis sélectionne plusieurs pistes avec Ctrl/Shift. "
         "Glisse-les sur la liste d'une playlist ouverte ; annule puis rétablis l'ajout."),
        ("Un seul lecteur joue à la fois. Les pistes sont ajoutées sans déplacer les fichiers ; "
         "les chemins déjà présents ne sont pas ajoutés deux fois. Ctrl+Z/Ctrl+Y couvre l'ajout."),
    ),
    (
        "Bibliothèque",
        "Rafraîchir et scanner les doublons",
        ("Ajoute un fichier au dossier depuis l'explorateur et utilise Refresh (Ctrl+R). "
         "Ajoute une copie exacte d'un audio, lance Scan duplicates puis coche Duplicates only. "
         "Essaie Cancel scan et ferme le navigateur pendant un scan."),
        ("Refresh fait apparaître les nouveaux fichiers ; les copies exactes partagent un numéro de groupe. "
         "Aucune suppression n'est faite. Annulation/fermeture laisse l'application réactive et n'applique pas de résultat tardif."),
    ),
    (
        "Playlist / Cue Editor",
        "Créer et activer un loop",
        "Dans le lecteur Playlist puis dans le Cue Editor, fais un clic droit sur un pad A-H et choisis une durée de loop ; clique ensuite sur le pad.",
        "Les deux écrans proposent les mêmes durées et la suppression. L’état actif est partagé entre les écrans ; le pad l’indique visuellement et un nouveau clic le désactive.",
    ),
    (
        "Playlist / Métronome",
        "Écouter le métronome en lecture réelle",
        "Dans un Cue Editor ou une playlist avec beat grid, active le métronome, lance la lecture, puis fais pause, reprise et un seek. Ajuste « Click offset » si besoin.",
        "Les clics tombent sur les temps de la grille, restent synchronisés après pause/reprise/seek, sans craquement ni dérive sur plusieurs minutes.",
    ),
    (
        "Playlist / ID Tag",
        "Contrôler l'éditeur d'ID Tag",
        "Clique sur « ID Tag » d'un morceau (copie de test), modifie le titre, utilise « Apply to project », puis « Write tags to audio… » et confirme. Essaie de fermer la fenêtre pendant l'écriture.",
        "Le projet est mis à jour sans toucher au fichier ; l'écriture audio crée un backup .bb-tags.bak, les tags sont relus correctement et la fermeture est bloquée pendant l'écriture.",
    ),
    (
        "Projet",
        "Backup ZIP, vérification et restauration",
        "Via le menu Tools : « Backup project and audio to ZIP… », puis « Verify ZIP backup SHA-256… », puis « Restore ZIP backup… » vers un nouveau dossier.",
        "Le ZIP et son .sha256 sont créés, la vérification réussit et la restauration produit un projet qui s'ouvre avec ses playlists et audios, sans écraser de dossier existant.",
    ),
    (
        "Playlist / Ergonomie",
        "Ouvrir les cues sans démarrer la musique",
        ("Lecture arrêtée, double-clique sur Cues puis ferme le Cue Editor. Double-clique ensuite sur le titre. "
         "Vérifie S puis Metronome sur la même ligne dans les deux éditeurs et ouvre les menus File et Tools."),
        ("Cues ouvre uniquement le Cue Editor ; le titre démarre la lecture une seule fois. "
        "Le volume et l'offset du métronome restent accessibles. File contient les actions de fichiers ; "
         "Tools regroupe Reload, vérifications, backup et opérations USB, avec les mêmes raccourcis."),
    ),
    (
        "Projet",
        "Enregistrer une copie de travail",
        "Utilise « Save project as » pour créer un nouveau fichier de projet temporaire.",
        "Le projet est enregistré sans remplacer ni modifier la collection d'origine.",
    ),
    (
        "Projet",
        "Recharger le projet",
        "Ouvre le projet temporaire créé à l'étape précédente.",
        "Les playlists, l'ordre des morceaux et les cues reviennent comme avant la fermeture.",
    ),
    (
        "Export",
        "Exporter vers un dossier",
        "Choisis un dossier local temporaire comme sortie, puis exporte en Rekordbox XML, M3U ou Traktor NML.",
        "L'export se termine et le fichier produit est présent dans le dossier choisi.",
    ),
    (
        "Export",
        "Vérifier une exportation CDJ",
        "Vers un dossier local temporaire (pas une clé USB), lance l'export CDJ/USB puis « Verify ».",
        "L'export se termine ; le contrôle indique qu'aucun fichier n'est manquant ou endommagé.",
    ),
    (
        "USB",
        "Contrôler le dialogue de formatage",
        "Ouvre « Format drive... », vérifie que les lecteurs proposés sont amovibles, puis ferme le dialogue sans formater.",
        "Le disque système n'est pas proposé. Aucune clé n'est formatée pendant ce parcours.",
    ),
]
