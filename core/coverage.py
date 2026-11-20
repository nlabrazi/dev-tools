import json
import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.progress import BarColumn, Progress, SpinnerColumn, TaskProgressColumn, TextColumn, TimeElapsedColumn
from rich.prompt import Prompt

from utils.console import ask_yes_no, ui_panel, ui_table

console = Console()

SKIP_DIRS = {
    '.git', 'node_modules', '.nuxt', '.output', '.next', '.venv', 'venv', 'vendor',
    'dist', 'build', 'target', 'out', '.cache', '.turbo', '__pycache__', '.vscode-test',
    'coverage', 'htmlcov', 'test-results', 'playwright-report', '.pytest_cache',
    'android', 'ios', 'tmp', 'log', 'storage', '.nexus-speech', 'site-packages',
}
E2E_DIRS = {'e2e', 'e2e-tests'}
PROJECT_MARKERS = {'.git', 'package.json', 'pyproject.toml', 'Gemfile', 'go.mod', 'Cargo.toml',
                   'composer.json'}
PY_FILES = {'pyproject.toml', 'pytest.ini', 'setup.cfg', 'setup.py', 'tox.ini', '.coveragerc'}
REQUIREMENTS = re.compile(r'^requirements.*\.txt$')
CI_FILES = ('Jenkinsfile', 'Makefile', '.gitlab-ci.yml', 'pytest.ini', 'pyproject.toml',
            'setup.cfg', 'tox.ini')

JS_TEST = re.compile(r'\.(test|spec|unit)\.[cm]?[jt]sx?$')
PY_TEST = re.compile(r'^(test_\w*|\w*_test)\.py$')
RB_TEST = re.compile(r'_(spec|test)\.rb$')
TRACKED_ARTEFACT = re.compile(
    r'(^|/)((coverage|htmlcov|\.nyc_output)/|\.coverage$|coverage\.xml$|lcov\.info$)')

RUNNERS = (
    ('Vitest', re.compile(r'\bvitest\b')),
    ('Jest', re.compile(r'\bjest\b')),
    ('node:test', re.compile(r'\bnode\b[^&|;]*\s--test\b')),
    ('Playwright', re.compile(r'\bplaywright\s+test\b')),
)
JS_COVERAGE_CMD = re.compile(
    r'--coverage\b|\bc8\b|\bnyc\b|--experimental-test-coverage|--test-coverage\b|\btest:coverage\b')
VITEST_PROVIDERS = ('@vitest/coverage-v8', '@vitest/coverage-istanbul')
SHARED_VITEST = '@nlabrazi/config-vitest'

OK, TODO, INSTALL = 'ok', 'todo', 'install'
RANK = {OK: 0, TODO: 1, INSTALL: 2}
SECTIONS = {
    OK: ('✔', 'green', 'PRÊTS'),
    TODO: ('●', 'yellow', 'À FINALISER — outil présent, commande manquante'),
    INSTALL: ('✖', 'red', 'À INSTALLER — aucun outil de coverage utilisable'),
    None: ('○', 'dim', 'HORS SCOPE'),
}


@dataclass
class Unit:
    """Une suite de tests mesurable (un package.json, un projet Python ou Ruby)."""
    rel: Path
    stack: str
    tests: int
    status: str | None          # None = E2E uniquement (non mesuré)
    how: str = ''               # commande coverage existante
    action: str = ''            # prochaine étape
    artefacts: tuple = ()       # fichiers/dossiers générés à ignorer
    notes: list[str] = field(default_factory=list)


@dataclass
class Project:
    path: Path
    git: bool = True
    skip: str = ''
    units: list[Unit] = field(default_factory=list)
    missing_ignore: list[str] = field(default_factory=list)
    tracked: list[str] = field(default_factory=list)

    @property
    def name(self) -> str:
        return self.path.name

    @property
    def measured(self) -> list[Unit]:
        return [u for u in self.units if u.status]

    @property
    def status(self) -> str | None:
        if self.skip or not self.measured:
            return None
        return max((u.status for u in self.measured if u.status), key=lambda s: RANK.get(s, 99))


def read_text(path: Path | str) -> str:
    try:
        return Path(path).read_text(encoding='utf-8', errors='replace')
    except OSError:
        return ''


def read_json(path: Path | str) -> dict[str, Any]:
    try:
        data = json.loads(read_text(path) or '{}')
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def git(root: Path | str, *args: str, timeout: float = 10.0) -> subprocess.CompletedProcess[str] | None:
    try:
        return subprocess.run(
            ['git', '-C', str(root), *args],
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None


def plural(n: int, word: str) -> str:
    return f'{n} {word}' + ('s' if n > 1 and not word.endswith('s') else '')


def rel_str(rel: Path) -> str:
    return './' if rel == Path('.') else f'{rel.as_posix()}/'


def walk(root: Path):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        yield Path(dirpath), dirnames, filenames


def count_tests(folder: Path, pattern: re.Pattern, exclude_dirs: set[Path] = set()) -> tuple[int, int]:
    unit_tests, e2e_tests = 0, 0
    for dirpath, dirnames, filenames in os.walk(folder):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and (Path(dirpath) / d) not in exclude_dirs]
        for f in filenames:
            if pattern.search(f):
                if any(part in E2E_DIRS for part in (Path(dirpath) / f).relative_to(folder).parts):
                    e2e_tests += 1
                else:
                    unit_tests += 1
    return unit_tests, e2e_tests


def installed_version(base: Path, package: str) -> str | None:
    for cur in [base, *base.parents]:
        data = read_json(cur / 'node_modules' / package / 'package.json')
        if data.get('version'):
            return data['version']
    return None


def find_command(folders: list[Path], pattern: re.Pattern) -> str:
    for folder in dict.fromkeys(folders):
        candidates = [folder / n for n in CI_FILES]
        candidates += sorted((folder / '.github' / 'workflows').glob('*.y*ml'))
        candidates += sorted(folder.glob('*.sh'))
        for file in candidates:
            match = pattern.search(read_text(file)) if file.is_file() else None
            if match:
                return f'{match.group(0).strip()} ({file.relative_to(folders[-1]).as_posix()})'
    return ''


def find_projects(root: Path) -> list[Path]:
    if not root.is_dir():
        return []
    projects = []
    for entry in sorted(root.iterdir()):
        if entry.is_dir() and entry.name not in SKIP_DIRS:
            if any((entry / marker).exists() for marker in PROJECT_MARKERS):
                projects.append(entry)
    return projects


def repo_owner(repo: Path) -> str | None:
    res = git(repo, 'remote', 'get-url', 'origin')
    if not res or res.returncode != 0:
        return None
    m = re.search(r'[:/]([\w.-]+)/[\w.-]+?(\.git)?$', res.stdout.strip())
    return m.group(1) if m else None


def discover_units(path: Path) -> tuple[list[Path], dict[Path, list[Path]], list[Path], list[Path]]:
    js, workspaces, py, rb = [], {}, [], []
    for dirpath, _, filenames in walk(path):
        if 'package.json' in filenames:
            js.append(dirpath)
            patterns = read_json(dirpath / 'package.json').get('workspaces', [])
            if isinstance(patterns, dict):
                patterns = patterns.get('packages', [])
            ws = sorted({p for pat in patterns if isinstance(pat, str) for p in dirpath.glob(pat) if (p / 'package.json').is_file()})
            if ws:
                workspaces[dirpath] = ws
        if any(f in PY_FILES or REQUIREMENTS.search(f) for f in filenames):
            py.append(dirpath)
        if 'Gemfile' in filenames:
            rb.append(dirpath)
    members = {m for group in workspaces.values() for m in group}
    return [d for d in js if d not in members], workspaces, py, rb


def analyze_js(root: Path, folder: Path, workspaces: list[Path], exclude: set[Path]) -> Unit | None:
    sources = [folder, *workspaces]
    deps: dict[str, Any] = {}
    root_pkg = read_json(folder / 'package.json')
    root_deps = {**root_pkg.get('dependencies', {}), **root_pkg.get('devDependencies', {})}
    all_scripts: list[tuple[Path, str, str]] = []
    for source in sources:
        pkg = read_json(source / 'package.json')
        for key in ('dependencies', 'devDependencies', 'optionalDependencies'):
            if isinstance(pkg.get(key), dict):
                deps.update(pkg[key])
        if isinstance(pkg.get('scripts'), dict):
            all_scripts += [
                (source, s_name, s_cmd)
                for s_name, s_cmd in pkg['scripts'].items()
                if isinstance(s_cmd, str)
            ]

    tests, e2e = count_tests(folder, JS_TEST, exclude)
    if not tests:
        if e2e:
            return Unit(folder.relative_to(root), 'Playwright', e2e, None)
        return None

    runners = [name for name, rx in RUNNERS if any(rx.search(c) for _, _, c in all_scripts)]
    runner = next((r for r in runners if r != 'Playwright'), None)
    if not runner:
        runner = next((name for name, _ in RUNNERS if name.lower() in root_deps), 'Tests JS/TS')

    cov = next(((s, n) for s, n, c in all_scripts if JS_COVERAGE_CMD.search(c)), None)
    script = cov[1] if cov else None
    where = cov[0].relative_to(folder).as_posix() if cov and cov[0] != folder else ''
    unit = Unit(folder.relative_to(root), runner, tests, OK, artefacts=('coverage/',))

    def _pkg_version(package: str) -> str | None:
        return next(filter(None, (installed_version(s, package) for s in sources)), None)

    if runner == 'Vitest':
        v_ver = _pkg_version('vitest') or deps.get('vitest', '')
        provider = next((p for p in VITEST_PROVIDERS if p in deps or _pkg_version(p)), None)
        transitive = SHARED_VITEST if provider and provider not in root_deps and SHARED_VITEST in deps else None
        p_ver = _pkg_version(provider) if provider else None
        label_stack = f'Vitest {v_ver}' if v_ver else 'Vitest'
        unit.stack = label_stack

        if script:
            unit.how = f'npm run {script}' + (f'  (dans {where}/)' if where else '')
        elif provider:
            unit.status = TODO
            unit.action = 'ajouter "test:coverage": "vitest run --coverage" dans package.json'
        else:
            unit.status = INSTALL
            target_p = f'@vitest/coverage-v8@{v_ver}' if v_ver else '@vitest/coverage-v8'
            target_p += '  (à la racine)' if workspaces else ''
            unit.action = f'npm i -D {target_p}'

        if provider and transitive:
            unit.notes.append(f'{provider} {p_ver or ""} installé (via {transitive})'.strip())
        elif provider:
            unit.notes.append(f'{provider} {p_ver or ""} installé'.strip())
        if v_ver and p_ver and v_ver != p_ver:
            unit.notes.append(f'⚠ décalage vitest {v_ver} ≠ provider {p_ver}')
    elif runner == 'Jest':
        j_ver = _pkg_version('jest') or deps.get('jest', '')
        unit.stack = f'Jest {j_ver}'.strip()
        if script:
            unit.how = f'npm run {script}' + (f'  (dans {where}/)' if where else '')
        else:
            unit.status = TODO
            unit.action = 'ajouter "test:coverage": "jest --coverage" dans package.json'
    elif runner == 'node:test':
        c8_ver = _pkg_version('c8') or deps.get('c8', '')
        unit.stack = 'node:test'
        if script:
            unit.how = f'npm run {script}' + (f'  (dans {where}/)' if where else '')
        elif c8_ver:
            unit.status = TODO
            unit.action = 'ajouter "test:coverage": "c8 npm test" dans package.json'
        else:
            unit.status = INSTALL
            script_names = {n for _, n, _ in all_scripts}
            test_cmd = next((k for k in ('test:unit', 'test') if k in script_names), 'test')
            unit.action = f'npm i -D c8  puis  "test:coverage": "c8 npm run {test_cmd}"'
    else:
        if script:
            unit.how = f'npm run {script}' + (f'  (dans {where}/)' if where else '')
        else:
            unit.status = TODO
            unit.action = 'configurer la commande de coverage dans package.json'

    if e2e:
        unit.notes.append(f'+ {plural(e2e, "spec")} E2E (non mesurées)')
    if workspaces:
        names = sorted(str(w.relative_to(folder)) for w in workspaces)
        unit.notes.append(f'{len(workspaces)} workspaces : {", ".join(names)}')
    return unit


def analyze_py(root: Path, folder: Path, exclude: set[Path]) -> Unit | None:
    tests, _ = count_tests(folder, PY_TEST, exclude)
    if not tests:
        return None

    runner = 'pytest' if any((folder / f).exists() for f in ('pytest.ini', '.pytest_cache')) else None
    if not runner:
        for py_file in PY_FILES:
            if 'pytest' in read_text(folder / py_file):
                runner = 'pytest'
                break
    runner = runner or 'unittest'

    ci_command = find_command([folder, root], re.compile(r'coverage\s+run\b|--cov\b'))
    reqs = [folder / f.name for f in folder.iterdir() if REQUIREMENTS.search(f.name)]
    reqs_text = ' '.join(read_text(r) for r in reqs)
    has_cov = any(re.search(rf'^{tool}\b', reqs_text, re.M) for tool in ('coverage', 'pytest-cov'))
    how = ci_command or ('pytest --cov' if runner == 'pytest' and has_cov else
                         'coverage run' if has_cov else None)

    unit = Unit(folder.relative_to(root), runner, tests, OK if how else INSTALL,
                how=how or '',
                artefacts=('.coverage', 'htmlcov/'))

    target = ('requirements-test.txt' if (folder / 'requirements-test.txt').exists()
              else 'requirements-dev.txt' if (folder / 'requirements-dev.txt').exists()
              else 'requirements.txt')
    run = 'pytest --cov' if runner == 'pytest' else 'python -m coverage run -m unittest'

    if has_cov:
        if not how:
            unit.status, unit.action = TODO, f'lancer « {run} » (Jenkinsfile ou config de test)'
    else:
        package = 'pytest-cov' if runner == 'pytest' else 'coverage'
        unit.status, unit.action = INSTALL, f'ajouter {package} à {target}, puis « {run} »'
    return unit


def analyze_rb(root: Path, folder: Path, exclude: set[Path]) -> Unit | None:
    tests, _ = count_tests(folder, RB_TEST, exclude)
    if not tests:
        return None
    runner = 'RSpec' if (folder / 'spec').is_dir() else 'Minitest'
    helpers = ['spec/spec_helper.rb', 'spec/rails_helper.rb', 'test/test_helper.rb', '.simplecov']
    helper = helpers[0] if runner == 'RSpec' else helpers[2]
    declared = re.search(r'gem\s+["\']simplecov["\']', read_text(folder / 'Gemfile'))
    started = next((h for h in helpers if 'SimpleCov.start' in read_text(folder / h)), None)
    unit = Unit(folder.relative_to(root), runner, tests, OK, artefacts=('coverage/',))
    if declared and started:
        unit.how = f'SimpleCov ({started})'
    elif declared:
        unit.status = TODO
        unit.action = f'ajouter « require "simplecov"; SimpleCov.start » en tête de {helper}'
    else:
        unit.status = INSTALL
        unit.action = f'gem "simplecov", require: false (groupe :test), puis SimpleCov.start dans {helper}'
    return unit


def check_git(project: Project) -> None:
    missing: list[str] = []
    for unit in project.measured:
        for artefact in unit.artefacts:
            probe = (unit.rel / artefact.rstrip('/')).as_posix()
            probe += '/probe' if artefact.endswith('/') else ''
            result = git(project.path, 'check-ignore', '-q', '--no-index', '--', probe)
            if result and result.returncode == 1 and artefact not in missing:
                missing.append(artefact)
    project.missing_ignore = missing
    listing = git(project.path, 'ls-files', '-z')
    if listing and listing.returncode == 0:
        project.tracked = [f for f in listing.stdout.split('\0') if TRACKED_ARTEFACT.search(f)]


def analyze_project(path: Path, me: str | None, include_forks: bool) -> Project:
    project = Project(path, git=(path / '.git').exists())
    owner = repo_owner(path) if project.git else None
    if owner and me and owner != me and not include_forks:
        project.skip = f'fork externe ({owner})'
        return project

    js, workspaces, py, rb = discover_units(path)
    every = set(js) | set(py) | set(rb)
    units = [analyze_js(path, d, workspaces.get(d, []), every - {d}) for d in js]
    units += [analyze_py(path, d, every - {d}) for d in py]
    units += [analyze_rb(path, d, every - {d}) for d in rb]
    project.units = sorted(filter(None, units), key=lambda u: (len(u.rel.parts), str(u.rel)))

    if not project.measured:
        e2e = sum(u.tests for u in project.units)
        project.skip = f'E2E Playwright uniquement ({plural(e2e, "spec")})' if project.units else 'aucun test'
        if not project.git:
            project.skip += ' · pas de dépôt git'
    elif project.git:
        check_git(project)
    return project


def audit_coverage(
    root_dirs: list[str | Path],
    *,
    include_forks: bool = False,
    progress_callback: Any = None,
) -> list[Project]:
    paths: list[Path] = []
    for r in root_dirs:
        root_path = Path(r)
        paths.extend(find_projects(root_path))
    paths = sorted(paths, key=lambda p: p.name)

    me = None
    for p in paths:
        if (p / '.git').exists():
            me = repo_owner(p)
            if me:
                break

    projects: list[Project] = []
    total = len(paths)
    for idx, p in enumerate(paths, 1):
        if progress_callback:
            progress_callback(idx, total, p.name)
        projects.append(analyze_project(p, me, include_forks))
    return projects


def audit_coverage_with_progress(
    root_dirs: list[str | Path],
    *,
    include_forks: bool = False,
    description: str = "Audit coverage",
    target_console: Console | None = None,
) -> list[Project]:
    out = target_console or console
    out.print()
    with Progress(
        SpinnerColumn(style="bold cyan"),
        TextColumn("[bold cyan]{task.description}"),
        BarColumn(bar_width=28),
        TaskProgressColumn(),
        TimeElapsedColumn(),
        console=out,
        transient=True,
    ) as progress:
        task_id = progress.add_task(f"Initialisation de l'{description.lower()}...", total=1)

        def _on_progress(current: int, total: int, repo_name: str) -> None:
            progress.update(
                task_id,
                total=total or 1,
                completed=current,
                description=f"[cyan]({current}/{total})[/] {description} · [bold white]{repo_name}[/]",
            )

        return audit_coverage(
            root_dirs,
            include_forks=include_forks,
            progress_callback=_on_progress,
        )


def render_coverage_audit(
    projects: list[Project],
    *,
    details: bool = False,
    target_console: Console | None = None,
) -> None:
    out = target_console or console
    counts = {OK: 0, TODO: 0, INSTALL: 0, None: 0}
    for p in projects:
        counts[p.status] += 1

    summary_text = (
        f"AUDIT COVERAGE · {len(projects)} projets\n"
        f"[bold green]✔ {counts[OK]} prêts[/]   "
        f"[bold yellow]● {counts[TODO]} à finaliser[/]   "
        f"[bold red]✖ {counts[INSTALL]} à installer[/]   "
        f"[dim]○ {counts[None]} hors scope[/]"
    )
    out.print(ui_panel(summary_text, title="Coverage Control Deck", compact=True))

    for status_key in (OK, TODO, INSTALL, None):
        group = [p for p in projects if p.status == status_key]
        if not group:
            continue
        mark, style_name, title = SECTIONS[status_key]
        out.print(f"\n[{style_name} bold]{mark} {title} ({len(group)})[/]")

        for p in group:
            tag = "  [bold yellow]⚠ .gitignore[/]" if p.missing_ignore else ""
            if p.skip:
                out.print(f"  [white]{p.name:<24}[/] [dim]{p.skip}[/]")
                continue

            if len(p.measured) == 1:
                u = p.measured[0]
                action_str = f" [dim]({u.how})[/]" if u.status == OK and u.how else ""
                files_str = f"{plural(u.tests, 'fichier')} de test"
                sub_rel = f"{rel_str(u.rel)} · " if u.rel != Path('.') else ""
                out.print(f"  [bold white]{p.name:<24}[/] [cyan]{sub_rel}{u.stack}[/] · {files_str}{action_str}{tag}")
                if u.status != OK:
                    out.print(f"  {' ' * 24} [{style_name}]→ {u.action}[/]")
                if details:
                    for note in u.notes:
                        out.print(f"  {' ' * 24} [dim]· {note}[/]")
            else:
                out.print(f"  [bold white]{p.name}[/]{tag}")
                for u in p.measured:
                    u_mark, u_style, _ = SECTIONS[u.status]
                    action_str = f" [dim]({u.how})[/]" if u.status == OK and u.how else ""
                    files_str = f"{plural(u.tests, 'fichier')} de test"
                    out.print(f"    [{u_style}]{u_mark}[/] [cyan]{rel_str(u.rel):<8}[/] {u.stack} · {files_str}{action_str}")
                    if u.status != OK:
                        out.print(f"      [{u_style}]→ {u.action}[/]")
                    if details:
                        for note in u.notes:
                            out.print(f"      [dim]· {note}[/]")

    missing_ignore_projects = [p for p in projects if p.missing_ignore and p.measured]
    if missing_ignore_projects:
        out.print(f"\n[bold yellow]⚠ .GITIGNORE À COMPLÉTER ({len(missing_ignore_projects)})[/]")
        for p in missing_ignore_projects:
            rules = "   ".join(f"[bold cyan]{r}[/]" for r in p.missing_ignore)
            out.print(f"  [bold white]{p.name:<24}[/] {rules}")
        out.print("  [dim]Sans « / » initial, coverage/ couvre aussi les sous-dossiers (monorepos).[/]")


def resolve_unit_coverage_command(project: Project, unit: Unit) -> tuple[str, Path] | None:
    """Trouve la commande exécutable de test coverage pour une unité de projet."""
    folder = project.path / unit.rel

    pkg = read_json(folder / 'package.json')
    scripts = pkg.get('scripts', {})
    if 'test:coverage' in scripts:
        return 'npm run test:coverage', folder
    if 'test:cov' in scripts:
        return 'npm run test:cov', folder

    if unit.how and unit.how.startswith('npm run '):
        cmd = unit.how.split(' (')[0].strip()
        return cmd, folder

    if unit.stack.startswith('Vitest'):
        return 'npx vitest run --coverage', folder
    if unit.stack.startswith('Jest'):
        return 'npx jest --coverage', folder
    if 'pytest' in unit.stack.lower():
        pytest_bin = '.venv/bin/pytest' if (folder / '.venv' / 'bin' / 'pytest').is_file() else 'pytest'
        return f'{pytest_bin} --cov', folder
    if 'unittest' in unit.stack.lower():
        py_bin = '.venv/bin/python' if (folder / '.venv' / 'bin' / 'python').is_file() else 'python3'
        if (folder / 'tests' / '__main__.py').is_file():
            return f'{py_bin} -m coverage run --source=core,utils,run -m tests && {py_bin} -m coverage report', folder
        return f'{py_bin} -m coverage run -m unittest discover -s tests -v && {py_bin} -m coverage report', folder
    if 'rspec' in unit.stack.lower():
        return 'bundle exec rspec', folder

    if unit.how:
        cmd = unit.how.split(' (')[0].strip()
        return cmd, folder

    return None


def resolve_project_coverage_command(project: Project) -> tuple[str, Path] | None:
    """Trouve la commande de test coverage appropriée pour un projet."""
    if not project.measured:
        return None
    return resolve_unit_coverage_command(project, project.measured[0])


def run_repo_coverage(
    project: Project,
    *,
    skip_confirm: bool = False,
    target_console: Console | None = None,
) -> bool:
    out = target_console or console
    commands = [
        (unit, resolved[0], resolved[1])
        for unit in project.measured
        if (resolved := resolve_unit_coverage_command(project, unit)) is not None
    ]
    if not commands:
        out.print(
            ui_panel(
                f"Aucune commande de coverage détectée pour {project.name}.",
                title="Coverage Impossible",
                tone="warning",
                compact=True,
            )
        )
        return False

    details_lines = [f"[bold white]Projet:[/] [cyan]{project.name}[/]"]
    for unit, cmd, cwd in commands:
        sub_label = f" ({rel_str(unit.rel)})" if len(commands) > 1 else ""
        details_lines.append(f"[bold white]Dossier{sub_label}:[/] [dim]{cwd}[/]")
        details_lines.append(f"[bold white]Commande{sub_label}:[/] [bold green]{cmd}[/]")

    out.print(
        ui_panel(
            "\n".join(details_lines),
            title="Exécution Coverage",
            compact=True,
        )
    )

    if not skip_confirm:
        prompt_label = commands[0][1] if len(commands) == 1 else f"les {len(commands)} suites de {project.name}"
        if not ask_yes_no(f"Lancer « {prompt_label} » maintenant ?", default="y"):
            out.print("[yellow]Exécution annulée.[/]")
            return False

    all_ok = True
    for unit, cmd, cwd in commands:
        out.print(f"\n[bold cyan]Lancement de « {cmd} » ({rel_str(unit.rel)})...[/]\n")
        try:
            proc = subprocess.run(cmd, cwd=str(cwd), shell=True, check=False)
            if proc.returncode != 0:
                all_ok = False
        except OSError as err:
            out.print(f"[bold red]Erreur d'exécution : {err}[/]")
            all_ok = False
    return all_ok


def run_all_repos_coverage(
    projects: list[Project],
    *,
    target_console: Console | None = None,
) -> list[tuple[str, bool]]:
    out = target_console or console
    if not projects:
        out.print(
            ui_panel(
                "Aucun projet mesurable à exécuter.",
                title="Coverage · ALL",
                tone="warning",
                compact=True,
            )
        )
        return []

    ordered = sorted(projects, key=lambda p: p.name.casefold())
    names_preview = ", ".join(p.name for p in ordered)
    out.print(
        ui_panel(
            f"[bold white]Projets ({len(ordered)}):[/] [cyan]{names_preview}[/]",
            title="Exécution Coverage · ALL",
            compact=True,
        )
    )

    if not ask_yes_no(
        f"Lancer les tests avec coverage sur les {len(ordered)} projets maintenant ?",
        default="y",
    ):
        out.print("[yellow]Exécution annulée.[/]")
        return []

    results: list[tuple[str, bool]] = []
    total = len(ordered)
    for idx, proj in enumerate(ordered, start=1):
        out.print()
        out.rule(f"[bold cyan]({idx}/{total}) {proj.name}[/]")
        ok = run_repo_coverage(proj, skip_confirm=True, target_console=out)
        results.append((proj.name, ok))

    table = ui_table(
        title=f"Bilan Coverage · Tous les Projets ({total})",
        show_lines=True,
    )
    table.add_column("Projet", style="bold white", no_wrap=True)
    table.add_column("Suites", justify="center", style="cyan", no_wrap=True)
    table.add_column("Statut", justify="center", no_wrap=True)

    suites_by_name = {p.name: len(p.measured) for p in ordered}
    passed = 0
    for proj_name, ok in results:
        if ok:
            passed += 1
        badge = "[bold green]✔ PASS[/]" if ok else "[bold red]✖ FAIL[/]"
        table.add_row(proj_name, str(suites_by_name.get(proj_name, 0)), badge)

    out.print()
    out.print(table)

    failed = total - passed
    tone = "success" if failed == 0 else "danger"
    out.print(
        ui_panel(
            f"[bold green]✔ {passed} réussi(s)[/]   [bold red]✖ {failed} en échec[/]   [dim]({total} au total)[/]",
            title="Synthèse Globale Coverage",
            tone=tone,
            compact=True,
        )
    )
    return results


def fix_missing_gitignores(
    projects: list[Project],
    *,
    target_console: Console | None = None,
) -> int:
    out = target_console or console
    targets = [p for p in projects if p.missing_ignore and p.git]
    if not targets:
        out.print(
            ui_panel(
                "Tous les fichiers .gitignore sont déjà parfaitement configurés !",
                title="Gitignore OK",
                tone="success",
                compact=True,
            )
        )
        return 0

    out.print()
    table = ui_table(title="Fichiers .gitignore à mettre à jour")
    table.add_column("Dépôt", style="bold white")
    table.add_column("Règles à ajouter", style="bold cyan")

    for p in targets:
        table.add_row(p.name, ", ".join(p.missing_ignore))
    out.print(table)

    if not ask_yes_no("Ajouter ces règles dans les .gitignore respectifs ?", default="y"):
        out.print("[yellow]Mise à jour annulée.[/]")
        return 0

    updated = 0
    for p in targets:
        gi_path = p.path / '.gitignore'
        existing = read_text(gi_path)
        additions = []
        for rule in p.missing_ignore:
            rule_clean = rule if rule.endswith('/') or '.' in rule else f"{rule}/"
            if rule_clean not in existing:
                additions.append(rule_clean)

        if additions:
            prefix = "\n" if existing and not existing.endswith("\n") else ""
            content = f"{prefix}\n# Code coverage\n" + "\n".join(additions) + "\n"
            with open(gi_path, 'a', encoding='utf-8') as f:
                f.write(content)
            updated += 1

    out.print(
        ui_panel(
            f"{updated} fichier(s) .gitignore mis à jour avec succès !",
            title="Gitignore Mis à Jour",
            tone="success",
            compact=True,
        )
    )
    return updated


def run_coverage_workflow(root_dirs: list[str]) -> None:
    from run import section_title

    section_title("Code Coverage", "📊")

    while True:
        table = ui_table(
            title="Code Coverage Workflows",
            caption="Choisissez une action, ou q pour revenir au menu principal.",
        )
        table.add_column("Clé", justify="center", style="bold cyan", no_wrap=True)
        table.add_column("Action", style="bold white", no_wrap=True)
        table.add_column("Description", style="white")

        table.add_row("1", "Audit global", "Synthèse de l'état du coverage sur l'ensemble des dépôts.")
        table.add_row("2", "Audit détaillé", "Versions installées, dépendances transitives, runners et tests E2E.")
        table.add_row("3", "Lancer le coverage", "Choisir un projet et exécuter sa suite de tests avec couverture.")
        table.add_row("4", "Sécuriser les .gitignore", "Ajouter automatiquement les règles manquantes (coverage/, htmlcov...).")
        table.add_row("q", "Retour", "Revenir au menu principal dev-tools.")

        console.print(table)

        choice = Prompt.ask(
            "\n[bold white]Action coverage[/]",
            choices=["1", "2", "3", "4", "q"],
            default="1",
            show_choices=False,
        ).lower()

        if choice == "q":
            break

        if choice in ("1", "2"):
            projects = audit_coverage_with_progress(
                root_dirs,
                description="Audit coverage",
            )
            render_coverage_audit(projects, details=(choice == "2"))

        elif choice == "3":
            projects = audit_coverage_with_progress(
                root_dirs,
                description="Découverte des projets",
            )
            measured = [p for p in projects if p.measured]
            if not measured:
                console.print(ui_panel("Aucun projet mesurable trouvé.", title="Rien à tester", tone="warning"))
                continue

            from run import ask_repository
            repo_choices = [(p.name, str(p.path)) for p in measured]
            selected = ask_repository(
                repo_choices,
                title="Lancer Coverage · Choix du Dépôt",
                caption="Sélectionnez un dépôt ou ALL pour exécuter les tests avec couverture.",
                allow_all=True,
            )
            if isinstance(selected, list):
                selected_paths = {path for _, path in selected}
                target_projects = [p for p in measured if str(p.path) in selected_paths]
                run_all_repos_coverage(target_projects)
            elif selected is not None:
                target_proj = next(p for p in measured if str(p.path) == selected[1])
                run_repo_coverage(target_proj)

        elif choice == "4":
            projects = audit_coverage_with_progress(
                root_dirs,
                description="Vérification des .gitignore",
            )
            fix_missing_gitignores(projects)
