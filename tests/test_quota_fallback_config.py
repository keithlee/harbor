import pytest

from harbor.config.workflow import load_models

BASE = '[controller]\nmodel = "claude-sonnet-5-5"\nreasoning_effort = "medium"\n'


def models(tmp_path, extra=""):
    path = tmp_path / "config.toml"
    path.write_text(BASE + extra)
    return load_models(path)


def test_absent_section_is_none(tmp_path):
    assert "quota_fallback" not in models(tmp_path)  # absent keeps existing policy digests


def test_valid_section_resolves_roles_and_inherits_controller(tmp_path):
    result = models(tmp_path, """
[quota_fallback]
max_switches = 3
[quota_fallback.controller]
provider = "codex"
model = "gpt-6.1-terra"
reasoning_effort = "medium"
[quota_fallback.reviewer]
model = "gpt-6.2-sol"
[quota_fallback.first_mate]
provider = "codex"
model = "gpt-6-luna"
""")["quota_fallback"]
    assert result["max_switches"] == 3
    assert result["controller"] == {"provider": "codex", "model": "gpt-6.1-terra", "reasoning_effort": "medium"}
    assert result["roles"]["planner"] == {"model": "gpt-6.1-terra", "reasoning_effort": "medium"}
    assert result["roles"]["implementor"] == {"model": "gpt-6.1-terra", "reasoning_effort": "medium"}
    assert result["roles"]["reviewer"]["model"] == "gpt-6.2-sol"
    assert result["roles"]["reviewer"]["reasoning_effort"] == "medium"
    assert result["first_mate"] == {"provider": "codex", "model": "gpt-6-luna", "reasoning_effort": None}


def test_max_switches_defaults_to_two(tmp_path):
    result = models(tmp_path, '[quota_fallback.controller]\nprovider = "codex"\nmodel = "gpt-6.1-terra"\nreasoning_effort = "high"\n')
    assert result["quota_fallback"]["max_switches"] == 2
    assert result["quota_fallback"]["first_mate"] is None


def test_unknown_key_rejected(tmp_path):
    with pytest.raises(ValueError, match="quota_fallback"):
        models(tmp_path, '[quota_fallback]\nbogus = 1\n[quota_fallback.controller]\nprovider = "codex"\nmodel = "gpt-6.1-terra"\n')
    with pytest.raises(ValueError, match="quota_fallback"):
        models(tmp_path, '[quota_fallback.controller]\nprovider = "codex"\nmodel = "gpt-6.1-terra"\nextra = 1\n')


def test_gpt_model_rejected_for_claude(tmp_path):
    with pytest.raises(ValueError, match="quota_fallback"):
        models(tmp_path, '[quota_fallback.controller]\nprovider = "claude"\nmodel = "gpt-6.1-terra"\n')


def test_claude_model_rejected_for_codex(tmp_path):
    with pytest.raises(ValueError, match="quota_fallback"):
        models(tmp_path, '[quota_fallback.controller]\nprovider = "codex"\nmodel = "claude-sonnet-5-5"\n')


def test_unknown_provider_rejected(tmp_path):
    with pytest.raises(ValueError, match="quota_fallback"):
        models(tmp_path, '[quota_fallback.controller]\nprovider = "gemini"\nmodel = "x"\n')


def test_bad_claude_effort_rejected(tmp_path):
    with pytest.raises(ValueError, match="quota_fallback"):
        models(tmp_path, '[quota_fallback.controller]\nprovider = "claude"\nmodel = "claude-opus-5-5"\nreasoning_effort = "minimal"\n')


@pytest.mark.parametrize("value", ["-1", "true", '"two"', "1.5"])
def test_bad_max_switches_rejected(tmp_path, value):
    with pytest.raises(ValueError, match="quota_fallback"):
        models(tmp_path, f'[quota_fallback]\nmax_switches = {value}\n[quota_fallback.controller]\nprovider = "codex"\nmodel = "gpt-6.1-terra"\n')


def test_section_requires_controller_or_first_mate(tmp_path):
    with pytest.raises(ValueError, match="quota_fallback"):
        models(tmp_path, "[quota_fallback]\nmax_switches = 1\n")


def test_first_mate_only_fallback_is_valid(tmp_path):
    result = models(tmp_path, '[quota_fallback.first_mate]\nprovider = "codex"\nmodel = "gpt-6-luna"\n')["quota_fallback"]
    assert result["controller"] is None
    assert result["roles"] == {}
    assert result["first_mate"]["model"] == "gpt-6-luna"
