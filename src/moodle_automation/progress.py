"""Terminal progress and periodic plain-text updates for redirected output."""

from contextlib import contextmanager
from contextvars import ContextVar
from threading import Event, Thread

from rich.console import Console
from rich.progress import BarColumn, Progress, ProgressColumn, SpinnerColumn, TextColumn, TimeElapsedColumn
from rich.text import Text

_current = ContextVar("progress", default=None)
_active = ContextVar("progress_tasks", default=())


class Counts(ProgressColumn):
    def render(self, task):
        if task.total is None:
            return Text(f"{int(task.completed)}件処理" if task.completed else "実行中")
        return Text(f"{int(task.completed)}/{int(task.total)}  {task.percentage:.0f}%")


class Reporter:
    def __init__(self, console=None, heartbeat_seconds=15):
        self.console = console or Console(stderr=True)
        self.heartbeat_seconds = heartbeat_seconds
        self.live = self.console.is_terminal and not self.console.is_dumb_terminal
        self.progress = Progress(
            SpinnerColumn(),
            TextColumn("{task.description}", markup=False),
            BarColumn(bar_width=20),
            Counts(),
            TimeElapsedColumn(),
            TextColumn("{task.fields[detail]}", markup=False),
            console=self.console,
            disable=not self.live,
            transient=True,
            refresh_per_second=4,
        )
        self.stop = Event()

    def __enter__(self):
        self.token = _current.set(self)
        self.progress.start()
        self.worker = Thread(target=self._heartbeat, daemon=True)
        self.worker.start()
        return self

    def __exit__(self, *exc):
        self.stop.set()
        self.worker.join()
        self.progress.stop()
        _current.reset(self.token)

    def _heartbeat(self):
        while not self.stop.wait(self.heartbeat_seconds):
            if not self.live:
                self.snapshot()

    def snapshot(self):
        for task in self.progress.tasks:
            count = f"{int(task.completed)}/{int(task.total)}" if task.total is not None else "実行中"
            self.console.print(
                f"{task.description}: {count} / 経過 {int(task.elapsed or 0)}秒 / {task.fields['detail']}",
                markup=False,
                highlight=False,
            )

    def message(self, message):
        self.console.print(message, markup=False, highlight=False)


@contextmanager
def stage(label, total=None, detail=""):
    reporter = _current.get()
    if reporter is None:
        yield None
        return
    task = reporter.progress.add_task(label, total=total, detail=detail)
    token = _active.set((*_active.get(), task))
    reporter.message(
        f"開始: {label}"
        + (f"（全{total}件）" if total is not None else "")
        + (f" — {detail}" if detail else "")
    )
    outcome = "完了"
    try:
        yield task
    except BaseException:
        outcome = "中断"
        raise
    finally:
        current = next(t for t in reporter.progress.tasks if t.id == task)
        if total is not None and current.completed < total and outcome == "完了":
            outcome = "中断"
        reporter.message(f"{outcome}: {label}（経過 {int(current.elapsed or 0)}秒）")
        reporter.progress.remove_task(task)
        _active.reset(token)


def detail(message):
    reporter = _current.get()
    if reporter and _active.get():
        reporter.progress.update(_active.get()[-1], detail=message)


def track(items, label, total=None, name=None):
    if total is None and hasattr(items, "__len__"):
        total = len(items)
    with stage(label, total=total) as task:
        reporter = _current.get()
        for index, item in enumerate(items, 1):
            message = name(item) if name else f"{index}件目"
            detail(message)
            if reporter and not reporter.live:
                reporter.message(f"{label}: {index}/{total if total is not None else '?'} — {message}")
            yield item
            if reporter:
                reporter.progress.advance(task)


def drain(queue):
    """Allow resource discovery to append links while the iterator is running."""
    while queue:
        yield queue.pop(0)
