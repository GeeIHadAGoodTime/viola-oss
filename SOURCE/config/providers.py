"""External compute provider configurations.

Users can connect their own AI subscriptions as compute providers.
Haiku delegates complex tasks to these providers when available.

Each provider entry describes:
  - How to launch the provider's MCP server (command + args)
  - Which env var enables it (requires)
  - Human-readable setup instructions for first-time users
"""

from __future__ import annotations

EXTERNAL_PROVIDERS: dict[str, dict[str, str | list[str]]] = {
    "codex": {
        "name": "OpenAI Codex",
        "description": "Delegates complex tasks to user's ChatGPT subscription",
        "transport": "stdio",
        "command": "npx",
        "args": ["codex", "mcp-server"],
        "requires": "codex_enabled",
        "setup_instructions": (
            "1. Install Node.js 22+ (https://nodejs.org)\n"
            "2. Run: npm install -g @openai/codex\n"
            "3. Run: codex login  (sign in with your ChatGPT account)\n"
            "4. Set VIOLA_CODEX_ENABLED=true in your .env"
        ),
    },
    # Ollama does not yet support MCP server mode natively.
    # When it does, uncomment and verify the command/args.
    # "ollama": {
    #     "name": "Ollama (Local)",
    #     "description": "Delegates to locally running models — no API key needed",
    #     "transport": "stdio",
    #     "command": "ollama",
    #     "args": ["serve", "--mcp"],
    #     "requires": "ollama_enabled",
    #     "setup_instructions": (
    #         "1. Install Ollama (https://ollama.com)\n"
    #         "2. Pull a model: ollama pull llama3\n"
    #         "3. Set VIOLA_OLLAMA_ENABLED=true in your .env"
    #     ),
    # },
}
