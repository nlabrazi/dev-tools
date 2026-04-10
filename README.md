# Dev Tools

Outil Python pour automatiser des workflows Git locaux sur plusieurs repositories.

Le runner est interactif : il déroule les étapes une par une et demande confirmation avant chaque action.

## Ce que fait l'outil

- auto-commit sur la branche d'intégration configurée
- création et merge de PRs entre branche d'intégration et branche de base
- mise à jour de changelogs
- synchronisation des branches de base locales

Par défaut, l'ordre d'exécution est le suivant :

1. auto-commit
2. merge vers les branches de base
3. mise à jour des changelogs
4. synchronisation des branches de base

## Prérequis

- Python 3.10+
- `git`
- `gh` si vous utilisez les étapes de création/merge de PR via GitHub CLI
- Ollama si vous voulez la génération assistée des messages de commit et de PR

Notes :

- si Ollama est désactivé ou indisponible, l'outil retombe sur un fallback heuristique
- un host Ollama distant nécessite un opt-in explicite via les variables de sécurité dédiées

## Installation

Linux / macOS :

```bash
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -r requirements.txt
```

Windows PowerShell :

```powershell
py -m venv .venv
.venv\Scripts\Activate.ps1
py -m pip install -r requirements.txt
```

## Utilisation

Linux / macOS :

```bash
python3 run.py --dry-run
python3 run.py --prod
python3 run.py --help
```

Windows :

```powershell
py run.py --dry-run
py run.py --help
```

Règles :

- `--dry-run` simule les actions sans modifier les repositories
- `--prod` exécute les actions réelles
- les deux options sont mutuellement exclusives et l'une des deux est obligatoire

Le `--help` expose :

- les modes d'exécution
- des exemples de lancement
- les principales variables d'environnement reconnues
- la stratégie de résolution de branche de base

## Configuration

`run.py` charge automatiquement un fichier `.env` à la racine du projet s'il existe.

Règles :

- `.env` est optionnel
- les variables déjà définies dans votre shell restent prioritaires
- un exemple complet est fourni dans [.env.example](/home/kaox/code/bricolage/dev-tools/.env.example)

Exemple minimal pour un usage local :

```dotenv
DEVTOOLS_ROOT_DIRS=/home/you/code/pers:/home/you/code/bricolage
DEVTOOLS_HEAD_BRANCH=staging
DEVTOOLS_REMOTE=origin
OLLAMA_HOST=http://localhost:11434
```

## Variables utiles

Configuration Git / repositories :

- `DEVTOOLS_ROOT_DIRS` : liste de répertoires racine à scanner, séparés par le séparateur système
- `DEVTOOLS_REMOTE` : remote Git utilisé par défaut, `origin` par défaut
- `DEVTOOLS_HEAD_BRANCH` : branche d'intégration, `staging` par défaut
- `DEVTOOLS_BASE_BRANCH` : force une branche de base au lieu de résoudre `origin/HEAD`
- `DEVTOOLS_GIT_TIMEOUT` : timeout par défaut des commandes Git, `60` secondes
- `GH_PR_MERGE_TIMEOUT` : attente max pour constater un merge effectif, `90` secondes

Configuration Ollama :

- `ENABLE_OLLAMA=0` : désactive totalement l'usage d'Ollama
- `OLLAMA_HOST` : URL du serveur Ollama, `http://localhost:11434` par défaut
- `OLLAMA_MODEL` : modèle utilisé, `llama3.2` par défaut
- `OLLAMA_TIMEOUT` : timeout HTTP Ollama, `60` secondes
- `OLLAMA_NUM_CTX` : taille de contexte Ollama
- `OLLAMA_MAX_FILES` : nombre max de fichiers injectés dans le prompt de commit
- `OLLAMA_MAX_DIFF_CHARS` : taille max du diff injecté dans le prompt de commit
- `OLLAMA_MAX_PR_SUMMARY_CHARS` : taille max du résumé injecté dans le prompt de PR

Garde-fous sécurité :

- `OLLAMA_ALLOW_REMOTE=1` : autorise un `OLLAMA_HOST` non local
- `OLLAMA_ALLOW_REMOTE_CONTEXT=1` : autorise l'envoi du diff Git et des résumés de commits à un host distant
- `OLLAMA_DEBUG=1` : affiche un aperçu tronqué des réponses Ollama
- `OLLAMA_DEBUG=full` : affiche le contenu complet
- `OLLAMA_DEBUG_MAX_CHARS` : taille de l'aperçu debug tronqué, `400` par défaut

Important :

- si `OLLAMA_HOST` est local, aucune variable de sécurité supplémentaire n'est nécessaire
- si `OLLAMA_HOST` est distant, il faut un opt-in explicite
- `OLLAMA_ALLOW_REMOTE=1` autorise uniquement le host distant
- `OLLAMA_ALLOW_REMOTE_CONTEXT=1` autorise en plus l'envoi de diff Git et de résumés de commits vers ce host

## Tests

Runner recommandé :

```bash
python3 -m tests
```

Exécuter un module de test ciblé :

```bash
python3 -m tests tests.test_commit
python3 -m tests tests.test_merge
```

Arrêter au premier échec :

```bash
python3 -m tests --failfast
```

Alternative standard `unittest` :

```bash
python3 -m unittest -v
```
