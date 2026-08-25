from pathlib import Path

from stokozavr_bot.prompt_bundle import PROMPT_FILES, load_prompt_bundle

REPO_PROMPTS = Path(__file__).resolve().parents[1] / "prompts"


def test_prompt_files_are_seven_in_fixed_order():
    assert PROMPT_FILES == (
        "personality.md",
        "sales_psychology.md",
        "dialogue_rules.md",
        "objections.md",
        "company_memory.md",
        "customer_memory_rules.md",
        "sales_scenarios.md",
    )


def test_prompt_bundle_loads_repo_files_in_fixed_order():
    bundle = load_prompt_bundle()

    headings = [
        "# Личность Ивана",
        "# Психология продаж",
        "# Правила диалога",
        "# Возражения",
        "# Память компании",
        "# Память клиента",
        "# Сценарии",
    ]
    positions = [bundle.index(heading) for heading in headings]
    assert positions == sorted(positions)
    assert "бакалея" in bundle.lower()
    assert "персональный менеджер" in bundle


def test_prompt_bundle_prefers_env_directory(tmp_path, monkeypatch):
    for index, name in enumerate(PROMPT_FILES, start=1):
        (tmp_path / name).write_text(f"ENV-{index}-{name}", encoding="utf-8")
    monkeypatch.setenv("STOKOZAVR_PROMPTS_DIR", str(tmp_path))

    bundle = load_prompt_bundle()

    assert bundle.split("\n\n") == [
        f"ENV-{index}-{name}" for index, name in enumerate(PROMPT_FILES, start=1)
    ]
    assert "Личность Ивана" not in bundle


def test_prompt_bundle_falls_back_to_repo_when_env_dir_is_incomplete(tmp_path, monkeypatch):
    (tmp_path / "personality.md").write_text("incomplete", encoding="utf-8")
    monkeypatch.setenv("STOKOZAVR_PROMPTS_DIR", str(tmp_path))

    bundle = load_prompt_bundle()

    assert "# Личность Ивана" in bundle
    assert (REPO_PROMPTS / "company_memory.md").read_text(encoding="utf-8").strip() in bundle


def test_pyproject_force_includes_prompts_in_wheel():
    text = (
        Path(__file__).resolve().parents[1].joinpath("pyproject.toml").read_text(encoding="utf-8")
    )

    assert "force-include" in text
    assert '"prompts"' in text or "'prompts'" in text
    assert "stokozavr_bot/prompts" in text
