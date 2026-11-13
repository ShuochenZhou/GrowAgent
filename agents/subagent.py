"""Fork-return sub-agents with built-in and user-defined profiles.

The built-in profiles are ``explore`` (read-only), ``plan`` (structured
analysis), and ``general`` (all non-recursive tools). Users may add profiles in
``.growagent/agents/*.md``.
"""

from __future__ import annotations

from pathlib import Path

from .frontmatter import parse_frontmatter
from .tools import tool_definitions

# ─── Read-only tools (for explore and plan agents) ──────────

# Restrict explore and plan agents to read-only tools.
READ_ONLY_TOOLS = {"read_file", "list_files", "grep_search"}

# System prompt for the read-only repository exploration profile.
EXPLORE_PROMPT = """You are a file search specialist for GrowAgent. You excel at thoroughly navigating and exploring codebases.

=== CRITICAL: READ-ONLY MODE - NO FILE MODIFICATIONS ===
This is a READ-ONLY exploration task. You are STRICTLY PROHIBITED from:
- Creating new files (no write_file, touch, or file creation of any kind)
- Modifying existing files (no edit_file operations)
- Deleting files (no rm or deletion)
- Running ANY commands that change system state

Your role is EXCLUSIVELY to search and analyze existing code.

Your strengths:
- Rapidly finding files using glob patterns
- Searching code and text with powerful regex patterns
- Reading and analyzing file contents

Guidelines:
- Use list_files for broad file pattern matching
- Use grep_search for searching file contents with regex
- Use read_file when you know the specific file path you need to read
- Adapt your search approach based on the thoroughness level specified by the caller

NOTE: You are meant to be a fast agent that returns output as quickly as possible. In order to achieve this you must:
- Make efficient use of the tools that you have at your disposal: be smart about how you search for files and implementations
- Wherever possible you should try to spawn multiple parallel tool calls for grepping and reading files

Complete the user's search request efficiently and report your findings clearly."""

# System prompt for read-only architectural analysis and implementation planning.
PLAN_PROMPT = """You are a Plan agent — a READ-ONLY sub-agent specialized for designing implementation plans.

IMPORTANT CONSTRAINTS:
- You are READ-ONLY. You only have access to read_file, list_files, and grep_search.
- Do NOT attempt to modify any files.

Your job:
- Analyze the codebase to understand the current architecture
- Design a step-by-step implementation plan
- Identify critical files that need modification
- Consider architectural trade-offs

Return a structured plan with:
1. Summary of current state
2. Step-by-step implementation steps
3. Critical files for implementation
4. Potential risks or considerations"""

# General sub-agents receive every tool except recursive agent creation.
GENERAL_PROMPT = """You are an agent for GrowAgent. Given the user's message, you should use the tools available to complete the task. Complete the task fully—don't gold-plate, but don't leave it half-done. When you complete the task, respond with a concise report covering what was done and any key findings — the caller will relay this to the user, so it only needs the essentials.

Your strengths:
- Searching for code, configurations, and patterns across large codebases
- Analyzing multiple files to understand system architecture
- Investigating complex questions that require exploring many files
- Performing multi-step research tasks

Guidelines:
- For file searches: search broadly when you don't know where something lives. Use read_file when you know the specific file path.
- For analysis: Start broad and narrow down. Use multiple search strategies if the first doesn't yield results.
- Be thorough: Check multiple locations, consider different naming conventions, look for related files.
- NEVER create files unless they're absolutely necessary for achieving your goal. ALWAYS prefer editing an existing file to creating a new one."""

# ─── Custom agent discovery ─────────────────────────────────

# Cache custom agent discovery to avoid repeated filesystem scans.
_cached_custom_agents: dict[str, dict] | None = None
_cached_agent_signature = None


def _discover_custom_agents() -> dict[str, dict]:
    """Discover user and project agent profiles keyed by name."""
    global _cached_custom_agents, _cached_agent_signature
    roots = [Path.home() / ".growagent" / "agents", Path.cwd() / ".growagent" / "agents"]
    signature = []
    for root in roots:
        for path in sorted(root.glob("*.md")):
            try:
                info = path.stat()
                signature.append((str(path.resolve()), info.st_mtime_ns, info.st_size, info.st_ino))
            except OSError:
                continue
    signature = (str(Path.cwd().resolve()), tuple(signature))
    if _cached_custom_agents is not None and signature == _cached_agent_signature:
        return _cached_custom_agents

    agents: dict[str, dict] = {}
    # User-level (lower priority)
    _load_agents_from_dir(Path.home() / ".growagent" / "agents", agents)
    # Project-level (higher priority, overwrites)
    _load_agents_from_dir(Path.cwd() / ".growagent" / "agents", agents)

    _cached_custom_agents = agents
    _cached_agent_signature = signature
    return agents


def _load_agents_from_dir(directory: Path, agents: dict[str, dict]) -> None:
    """Load Markdown agent profiles from a directory into the target mapping."""
    if not directory.is_dir():
        return
    for entry in directory.iterdir():
        if not entry.suffix == ".md":
            continue
        try:
            raw = entry.read_text()
            result = parse_frontmatter(raw)
            meta = result.meta
            name = meta.get("name") or entry.stem
            allowed_tools = None
            if "allowed-tools" in meta:
                # allowed-tools is a comma-separated allowlist; omission uses all non-agent tools.
                allowed_tools = [s.strip() for s in meta["allowed-tools"].split(",")]
            agents[name] = {
                "name": name,
                "description": meta.get("description", ""),
                "allowed_tools": allowed_tools,
                "system_prompt": result.body,
            }
        except Exception:
            # Isolate malformed profiles so other agents still load.
            pass




def get_sub_agent_config(agent_type: str) -> dict:
    """Build the system prompt and tool set for an agent profile."""
    custom = _discover_custom_agents().get(agent_type)
    if custom:
        if custom["allowed_tools"] is not None:
            # Honor an explicit custom-profile tool allowlist.
            tools = [t for t in tool_definitions if t["name"] in custom["allowed_tools"] and t["name"] != "agent"]
        else:
            # Prevent recursive sub-agent creation.
            tools = [t for t in tool_definitions if t["name"] != "agent"]
        return {"system_prompt": custom["system_prompt"], "tools": tools}

    # Built-in explore and plan profiles share the read-only tool set.
    read_only = [t for t in tool_definitions if t["name"] in READ_ONLY_TOOLS]

    if agent_type == "explore":
        return {"system_prompt": EXPLORE_PROMPT, "tools": read_only}
    elif agent_type == "plan":
        return {"system_prompt": PLAN_PROMPT, "tools": read_only}
    else:  # general
        return {"system_prompt": GENERAL_PROMPT, "tools": [t for t in tool_definitions if t["name"] != "agent"]}


# Agent catalog for the system prompt


def get_available_agent_types() -> list[dict[str, str]]:
    """Return descriptions for all built-in and custom agent profiles."""
    types = [
        {"name": "explore", "description": "Fast, read-only codebase search and exploration"},
        {"name": "plan", "description": "Read-only analysis with structured implementation plans"},
        {"name": "general", "description": "Full tools for independent tasks"},
    ]
    for name, defn in _discover_custom_agents().items():
        types.append({"name": name, "description": defn["description"]})
    return types


def build_agent_descriptions() -> str:
    """Format custom agent profiles for inclusion in the system prompt."""
    types = get_available_agent_types()
    if len(types) <= 3:
        return ""  # Only built-in types, already in system prompt

    custom = types[3:]
    lines = ["\n# Custom Agent Types", ""]
    for t in custom:
        lines.append(f"- **{t['name']}**: {t['description']}")
    return "\n".join(lines)


def reset_agent_cache() -> None:
    """Clear cached profiles after tests or explicit runtime configuration changes."""
    global _cached_custom_agents
    _cached_custom_agents = None
