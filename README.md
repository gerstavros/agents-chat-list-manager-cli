# Agents Chat List Manager (CLI)

A terminal / SSH-friendly app for managing Claude Code, Qwen Code, codewhale-tui, and opencode conversation history not only
on your computer but also remotely on servers via ssh.

It is based on the GUI version

## Building the binary

```bash
./build.sh
```

This creates a local build venv (`.buildenv/`, gitignored), installs
PyInstaller into it, and produces `dist/chatlistctl` — a single-file
executable.

**PyInstaller does not cross-compile.** Build on the same OS/architecture you
plan to deploy to. For Linux x86_64 servers, build on a Linux x86_64 machine
(or a matching Docker container). The resulting binary links against the
build machine's glibc, so the target server's glibc must be the same version
or newer.

## Usage

```
chatlistctl                 # same as chatlistctl browse
chatlistctl browse          # interactive TUI
chatlistctl list [--tool ID] [--search TEXT] [--days N] [--sort updated|title|messages|tool] [--asc] [--json]
chatlistctl show <id> [--raw] [--tail N]
chatlistctl export <id> -o out.md [--format markdown|text]
chatlistctl delete <id> [-y]
chatlistctl paths
chatlistctl config set-path <tool_id> <path>
chatlistctl config reset-path <tool_id>
```

### Interactive TUI (`browse`)

Full-screen terminal UI (built with the stdlib `curses` module — no extra
dependency), for browsing over SSH without memorizing session IDs:

| Key | Action |
|---|---|
| `↑`/`↓` or `j`/`k` | move selection |
| `PgUp`/`PgDn`, `Home`/`End` | jump by page / to start / to end |
| `Enter` | view the selected conversation's transcript |
| `d` | delete the selected conversation (asks `[y/N]` first) |
| `/` | search by title/project substring |
| `t` | cycle the tool filter (All → claude_code → qwen_code → codewhale_tui → opencode → All) |
| `e` | export the selected conversation (prompts for output path) |
| `r` | rescan |
| `q` | quit (in the transcript view, `q` or `←` goes back to the list) |

Same `--claude-dir`/`--qwen-dir`/`--codewhale-dir`/`--opencode-dir` one-off overrides work here
too, e.g. `chatlistctl browse --qwen-dir /data/qwen-home`.

`<id>` accepts a full `tool_id:session_id`, a bare `session_id`, or an
unambiguous prefix of one (shown in the `list` output's ID column).

Every command that reads/writes conversations also accepts one-off overrides
for that invocation, without touching persisted config:

```
--claude-dir PATH  --qwen-dir PATH  --codewhale-dir PATH  --opencode-dir PATH
```

Persisted overrides (`chatlistctl config set-path ...`) are stored the same
way as the GUI app's settings:

- Linux: `~/.config/agentchatmanager/config.json`
- macOS: `~/Library/Application Support/agentchatmanager/config.json`
- Windows: `%APPDATA%\agentchatmanager\config.json`

## Examples

```bash
# What's here, sorted by most recently updated
chatlistctl list

# Only Claude Code sessions touched in the last 7 days
chatlistctl list --tool claude_code --days 7

# Read a transcript, last 20 messages only
chatlistctl show a47fcaa7 --tail 20

# Export and pull it back locally
chatlistctl export a47fcaa7 -o /tmp/session.md
scp server:/tmp/session.md .

# Clean up an old session non-interactively (e.g. in a cron/script)
chatlistctl delete a47fcaa7 --yes

# This server's qwen-code data lives somewhere non-standard
chatlistctl config set-path qwen_code /data/qwen-home

# opencode data dir (default: ~/.local/share/opencode, honors $OPENCODE_DATA)
chatlistctl list --tool opencode
chatlistctl config set-path opencode /data/opencode-home
```

## Running from source code

```bash
python3 cli.py list
```

Requires Python 3.10+, stdlib only — no pip install needed to run from
source. PyInstaller is only a build-time dependency for producing the binary.

## Adding support for another tool

Add `core/adapters/your_tool.py` implementing
`ToolAdapter` (see `core/adapters/base.py`), decorate the class with
`@register`, and add it to the explicit import list in
`core/registry.py::discover_adapters()` (required for frozen/PyInstaller
builds — dynamic `pkgutil` discovery only works when running from source,
since a frozen binary has no real files on disk to scan).

## Tests

```bash
python3 -m unittest discover -s tests -v
```

This project has its own self-contained suite (stdlib `unittest`), run
against fixture files under `tests/fixtures/` plus a synthetic SQLite
`opencode.db` built in a temp dir for the opencode adapter — no GUI project
checkout and no real `~/.claude` / `~/.qwen` / `~/.codewhale` /
`~/.local/share/opencode` data required. It mirrors
the GUI project's adapter tests but imports from `core/` instead of `app/`,
so this repo can be developed and tested independently.

If you change `core/`, run this suite. Note that `core/` is still a
hand-maintained copy of the GUI's `app/` — keep both code trees and both
test suites in sync by hand.
