# Dev Tools

Outil Python pour automatiser des workflows Git locaux sur plusieurs repositories :

- génération de commits
- création et merge de PRs entre branche d'intégration et branche de base
- mise à jour de changelogs
- synchronisation des branches par défaut

L'exécution est interactive : `run.py` parcourt les étapes et vous demande confirmation avant les actions importantes.

## Prérequis

- Python 3
- `git`
- `gh` pour les actions GitHub CLI
- Ollama en local si vous voulez la génération assistée par LLM

## Installation

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Utilisation

Mode réel :

```bash
py run.py --prod
```

Mode simulation :

```bash
py run.py --dry-run
```

Aide CLI :

```bash
py run.py --help
```

Le `--help` liste maintenant :

- les modes disponibles
- des exemples de lancement
- les principales variables d'environnement reconnues

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

Configuration Git :

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

## Tests

Suite complète :

```bash
python3 -m tests
```

Quelques variantes :

```bash
python3 -m tests tests.test_commit
python3 -m tests tests.test_merge
python3 -m unittest -v
```
