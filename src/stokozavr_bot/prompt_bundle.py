from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path
from typing import Protocol

PROMPT_FILES = (
    "personality.md",
    "sales_psychology.md",
    "dialogue_rules.md",
    "objections.md",
    "company_memory.md",
    "customer_memory_rules.md",
    "sales_scenarios.md",
)

_REPO_PROMPTS = Path(__file__).resolve().parents[2] / "prompts"


class _PromptDir(Protocol):
    def __truediv__(self, other: str): ...

    def joinpath(self, *parts: str): ...


def _candidate_dirs() -> Iterator[Path | _PromptDir]:
    env_dir = os.environ.get("STOKOZAVR_PROMPTS_DIR")
    if env_dir:
        yield Path(env_dir)
    yield _REPO_PROMPTS
    try:
        from importlib.resources import files

        yield files("stokozavr_bot") / "prompts"
    except (ModuleNotFoundError, FileNotFoundError, TypeError, ValueError, OSError):
        return


def _join(directory: Path | _PromptDir, name: str):
    if isinstance(directory, Path):
        return directory / name
    return directory.joinpath(name)


def _read_bundle(directory: Path | _PromptDir) -> str | None:
    parts: list[str] = []
    for name in PROMPT_FILES:
        path = _join(directory, name)
        try:
            if not path.is_file():
                return None
            parts.append(path.read_text(encoding="utf-8").strip())
        except (OSError, FileNotFoundError, TypeError, ValueError, AttributeError):
            return None
    return "\n\n".join(parts)


def load_prompt_bundle() -> str:
    for directory in _candidate_dirs():
        bundle = _read_bundle(directory)
        if bundle:
            return bundle
    raise FileNotFoundError("Не найден полный набор prompts для Стокозавра")
