import argparse
import sys
import time
import unittest
from pathlib import Path

from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.table import Table


PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

console = Console(file=sys.__stdout__)


def _test_name(test: unittest.case.TestCase) -> str:
    try:
        return test.id()
    except Exception:
        return str(test)


class RichTestResult(unittest.TestResult):
    def __init__(self, *, buffer_output: bool, failfast: bool) -> None:
        super().__init__()
        self.buffer = buffer_output
        self.failfast = failfast
        self._test_started_at = 0.0
        self.successes: list[tuple[str, float]] = []
        self.failure_details: list[tuple[str, str, str]] = []

    def startTest(self, test: unittest.case.TestCase) -> None:
        super().startTest(test)
        self._test_started_at = time.perf_counter()

    def _elapsed(self) -> float:
        return time.perf_counter() - self._test_started_at

    def addSuccess(self, test: unittest.case.TestCase) -> None:
        super().addSuccess(test)
        label = _test_name(test)
        elapsed = self._elapsed()
        self.successes.append((label, elapsed))
        console.print(f"[bold green]PASS[/] {label} [dim]({elapsed:.3f}s)[/]")

    def addFailure(self, test: unittest.case.TestCase, err: tuple[type[BaseException], BaseException, object]) -> None:
        super().addFailure(test, err)
        label = _test_name(test)
        elapsed = self._elapsed()
        console.print(f"[bold red]FAIL[/] {label} [dim]({elapsed:.3f}s)[/]")
        self.failure_details.append((label, "Failure", self.failures[-1][1]))

    def addError(self, test: unittest.case.TestCase, err: tuple[type[BaseException], BaseException, object]) -> None:
        super().addError(test, err)
        label = _test_name(test)
        elapsed = self._elapsed()
        console.print(f"[bold red]ERROR[/] {label} [dim]({elapsed:.3f}s)[/]")
        self.failure_details.append((label, "Error", self.errors[-1][1]))

    def addSkip(self, test: unittest.case.TestCase, reason: str) -> None:
        super().addSkip(test, reason)
        label = _test_name(test)
        elapsed = self._elapsed()
        console.print(f"[bold yellow]SKIP[/] {label} [dim]({elapsed:.3f}s)[/] [yellow]{reason}[/]")

    def addExpectedFailure(
        self,
        test: unittest.case.TestCase,
        err: tuple[type[BaseException], BaseException, object],
    ) -> None:
        super().addExpectedFailure(test, err)
        label = _test_name(test)
        elapsed = self._elapsed()
        console.print(f"[bold cyan]XFAIL[/] {label} [dim]({elapsed:.3f}s)[/]")

    def addUnexpectedSuccess(self, test: unittest.case.TestCase) -> None:
        super().addUnexpectedSuccess(test)
        label = _test_name(test)
        elapsed = self._elapsed()
        console.print(f"[bold magenta]XPASS[/] {label} [dim]({elapsed:.3f}s)[/]")


def build_suite(test_names: list[str], start_dir: str, pattern: str) -> unittest.TestSuite:
    loader = unittest.defaultTestLoader
    if test_names:
        return loader.loadTestsFromNames(test_names)

    resolved_start_dir = Path(start_dir)
    if not resolved_start_dir.is_absolute():
        resolved_start_dir = PROJECT_ROOT / resolved_start_dir

    return loader.discover(
        start_dir=str(resolved_start_dir),
        pattern=pattern,
        top_level_dir=str(PROJECT_ROOT),
    )


def print_summary(result: RichTestResult, duration: float) -> None:
    if result.failure_details:
        console.print()
        console.rule("[bold red]Failure Details[/]")
        for label, kind, details in result.failure_details:
            console.print(
                Panel.fit(
                    details.rstrip(),
                    title=f"{kind}: {label}",
                    border_style="red",
                )
            )

    summary = Table(box=box.SIMPLE, show_header=False)
    summary.add_row("Passed", f"[green]{len(result.successes)}[/]")
    summary.add_row("Failed", f"[red]{len(result.failures)}[/]")
    summary.add_row("Errors", f"[red]{len(result.errors)}[/]")
    summary.add_row("Skipped", f"[yellow]{len(result.skipped)}[/]")
    summary.add_row("Expected Failures", f"[cyan]{len(result.expectedFailures)}[/]")
    summary.add_row("Unexpected Successes", f"[magenta]{len(result.unexpectedSuccesses)}[/]")
    summary.add_row("Total", str(result.testsRun))
    summary.add_row("Duration", f"{duration:.3f}s")

    status = "SUCCESS" if result.wasSuccessful() else "FAILED"
    border_style = "green" if result.wasSuccessful() else "red"
    console.print()
    console.print(Panel.fit(summary, title=f"Test Run {status}", border_style=border_style))


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the test suite with a rich, colored output.")
    parser.add_argument("tests", nargs="*", help="Optional dotted test paths, e.g. tests.test_changelog")
    parser.add_argument("-s", "--start-dir", default="tests", help="Test discovery start directory")
    parser.add_argument("-p", "--pattern", default="test*.py", help="Test filename pattern for discovery")
    parser.add_argument("-f", "--failfast", action="store_true", help="Stop on first failure or error")
    parser.add_argument(
        "--no-buffer",
        action="store_true",
        help="Do not capture stdout/stderr from tests",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    suite = build_suite(args.tests, args.start_dir, args.pattern)
    result = RichTestResult(buffer_output=not args.no_buffer, failfast=args.failfast)

    console.print(Panel.fit("Running test suite", title="Dev Tools", border_style="cyan"))
    console.print()

    started_at = time.perf_counter()
    result.startTestRun()
    try:
        suite.run(result)
    finally:
        result.stopTestRun()
    duration = time.perf_counter() - started_at

    print_summary(result, duration)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
