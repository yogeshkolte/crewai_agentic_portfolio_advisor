"""Which model each agent uses, read from models.jsonc.

Each agent file points here: "llm": {"python": "models.<agent file name>"}. CrewAI reads that
attribute when it loads the crew, so the model is picked at the start of every run.

Order of precedence for an agent:
  1. "agents" in models.jsonc (mix providers, one agent at a time)
  2. the provider's own "agents" entry (e.g. covered calls on gpt-5 under DeepSeek)
  3. the provider's model for the agent's tier
The provider is MODEL_PROVIDER from the environment (or .env), else "provider" in models.jsonc.
The plain-English reviews use GUARDRAIL_JUDGE_MODEL if set, else the provider's "judge".

Command line:
  uv run python models.py                 # show each agent's model and whether its key is set
  uv run python models.py use openai      # make openai the default provider
"""
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CONFIG = ROOT / "models.jsonc"

try:  # crewai run loads .env too; this covers scripts and the command line
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
except ImportError:
    pass

# API key each provider needs (any one of the names).
KEYS = {
    "openai": ("OPENAI_API_KEY",),
    "deepseek": ("DEEPSEEK_API_KEY",),
    "anthropic": ("ANTHROPIC_API_KEY",),
    "gemini": ("GEMINI_API_KEY", "GOOGLE_API_KEY"),
}
# Extra package a provider needs, and how to install it.
INSTALL = {
    "anthropic": ("anthropic", 'uv add "crewai[anthropic]"'),
    "gemini": ("google.genai", 'uv add "crewai[google-genai]"'),
}
# Only these accept reasoning_effort.
_EFFORT = {"openai", "deepseek"}

_cache: dict = {}


def _config() -> dict:
    import json
    from guardrails import _strip_jsonc
    return json.loads(_strip_jsonc(CONFIG.read_text()))


def provider() -> str:
    cfg = _config()
    name = os.getenv("MODEL_PROVIDER") or cfg["provider"]
    if name not in cfg["providers"]:
        raise ValueError(f"Unknown model provider '{name}'. Choose one of: {', '.join(cfg['providers'])} "
                         f"(set in models.jsonc or MODEL_PROVIDER).")
    return name


def _as_spec(value) -> dict:
    return {"model": value} if isinstance(value, str) else dict(value)


def spec(agent: str) -> dict:
    """Model settings for one agent, e.g. {"model": "deepseek/deepseek-flash", "reasoning_effort": "low"}."""
    cfg, prov = _config(), provider()
    p = cfg["providers"][prov]
    if agent in cfg.get("agents", {}):
        return _as_spec(cfg["agents"][agent])
    if agent in p.get("agents", {}):
        return _as_spec(p["agents"][agent])
    if agent not in cfg["tiers"]:
        raise ValueError(f"models.jsonc: no tier for agent '{agent}'; add it under \"tiers\".")
    return _as_spec(p[cfg["tiers"][agent]])


def judge_spec() -> dict:
    if os.getenv("GUARDRAIL_JUDGE_MODEL"):
        return {"model": os.environ["GUARDRAIL_JUDGE_MODEL"], "reasoning_effort": "low"}
    s = _as_spec(_config()["providers"][provider()]["judge"])
    s.setdefault("reasoning_effort", "low")
    return s


def _provider_of(model: str) -> str:
    return model.split("/", 1)[0] if "/" in model else "openai"


def make_llm(s: dict):
    """Build a CrewAI LLM, dropping settings the provider doesn't take."""
    from crewai import LLM
    s = dict(s)
    prov = _provider_of(s["model"])
    if prov not in _EFFORT:
        s.pop("reasoning_effort", None)
    if prov in INSTALL:
        module, cmd = INSTALL[prov]
        try:
            __import__(module)
        except ImportError:
            raise RuntimeError(f"{s['model']} needs an extra package. Run: {cmd}") from None
    return LLM(**s)


def missing_keys() -> list:
    """One line per provider whose API key is not set, for the models this run uses."""
    cfg = _config()
    needed: dict = {}
    for agent in cfg["tiers"]:
        needed.setdefault(_provider_of(spec(agent)["model"]), []).append(agent)
    if os.getenv("GUARDRAIL_REVIEW", "on").lower() != "off":
        needed.setdefault(_provider_of(judge_spec()["model"]), []).append("plain-English reviews")
    problems = []
    for prov, users in needed.items():
        names = KEYS.get(prov)
        if names and not any(os.getenv(n) for n in names):
            problems.append(f"{' or '.join(names)} is not set (used by: {', '.join(users)})")
    return problems


def __getattr__(agent: str):
    """models.<agent> -> that agent's LLM (built once, on first use)."""
    if agent.startswith("__"):
        raise AttributeError(agent)
    if agent not in _cache:
        _cache[agent] = make_llm(spec(agent))
    return _cache[agent]


def _show() -> None:
    cfg = _config()
    print(f"Provider: {provider()}" + ("  (from MODEL_PROVIDER)" if os.getenv("MODEL_PROVIDER") else "  (from models.jsonc)"))
    print(f"Choices: {', '.join(cfg['providers'])}\n")
    rows = [(a, spec(a)) for a in cfg["tiers"]] + [("plain-English reviews", judge_spec())]
    for agent, s in rows:
        effort = s.get("reasoning_effort") if _provider_of(s["model"]) in _EFFORT else None
        print(f"  {agent:30} {s['model']}" + (f" ({effort})" if effort else ""))
    problems = missing_keys()
    print("\nAPI keys: " + ("all set" if not problems else "\n  " + "\n  ".join(problems)))


def _use(name: str) -> None:
    if name not in _config()["providers"]:
        sys.exit(f"Unknown provider '{name}'. Choose one of: {', '.join(_config()['providers'])}")
    text = CONFIG.read_text()
    new, n = re.subn(r'("provider"\s*:\s*)"[^"]*"', rf'\1"{name}"', text, count=1)
    if not n:
        sys.exit('models.jsonc: "provider" line not found')
    CONFIG.write_text(new)
    print(f"Default provider is now {name}.\n")
    if os.getenv("MODEL_PROVIDER"):
        print(f"Note: MODEL_PROVIDER={os.environ['MODEL_PROVIDER']} is set and wins over models.jsonc.\n")
    _show()


if __name__ == "__main__":
    sys.path.insert(0, str(ROOT))
    if len(sys.argv) == 3 and sys.argv[1] == "use":
        _use(sys.argv[2])
    elif len(sys.argv) == 1:
        _show()
    else:
        sys.exit(__doc__)
