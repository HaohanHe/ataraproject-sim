# survey26 command-line tool

`survey26` is the official command-line tool of the GOSIM 2026 Agentic Observer Hackathon. It performs, from a terminal or from a coding agent, every action that the website offers to a contestant: profile, team, team variables, project versions, evaluations, results, the final version, the leaderboard and the Kimi Coding Plan code. It acts with your account and is subject to exactly the same permissions, daily limits and quotas as the website.

## 1. Installation

The tool is available in two equivalent builds with the same commands, options, `--json` output and exit codes (version 1.8.0): a single Python file that requires Python 3.9 or later and no other packages, and a single prebuilt binary that requires nothing at all.

```bash
# Option A: install the survey26 command
pip install "git+https://github.com/gosimfoundation/hackathon-survey26#subdirectory=cli"

# Option B: download the single file
curl -fsSLO https://create.gosim.org/survey26/platform/survey26.py
python3 survey26.py --help
```

**Option C: prebuilt binary.** Download the file for your system, make it executable and place it on your `PATH`. Checksums: [SHA256SUMS](https://github.com/gosimfoundation/hackathon-survey26/releases/download/cli-v1.8.0/SHA256SUMS).

| System | Download |
| --- | --- |
| macOS (Apple silicon) | [survey26-macos-arm64](https://github.com/gosimfoundation/hackathon-survey26/releases/download/cli-v1.8.0/survey26-macos-arm64) |
| macOS (Intel) | [survey26-macos-x86_64](https://github.com/gosimfoundation/hackathon-survey26/releases/download/cli-v1.8.0/survey26-macos-x86_64) |
| Linux x86_64 (static) | [survey26-linux-x86_64](https://github.com/gosimfoundation/hackathon-survey26/releases/download/cli-v1.8.0/survey26-linux-x86_64) |
| Linux aarch64 (static) | [survey26-linux-aarch64](https://github.com/gosimfoundation/hackathon-survey26/releases/download/cli-v1.8.0/survey26-linux-aarch64) |
| Windows x86_64 | [survey26-windows-x86_64.exe](https://github.com/gosimfoundation/hackathon-survey26/releases/download/cli-v1.8.0/survey26-windows-x86_64.exe) |

```bash
curl -fsSL -o survey26 https://github.com/gosimfoundation/hackathon-survey26/releases/download/cli-v1.8.0/survey26-linux-x86_64
chmod +x survey26 && ./survey26 --version   # survey26 1.8.0
```

On macOS, a binary downloaded with a browser must first be released from quarantine: `xattr -d com.apple.quarantine survey26`. On Windows, run `survey26-windows-x86_64.exe` from PowerShell or the command prompt.

`survey26 --help` lists all commands; `survey26 <command> --help` describes each one. The usage guide for coding agents can be downloaded as [survey26-AGENTS.md](__BASE_URL__survey26-AGENTS.md); place it in your project so that your coding agent reads it.

## 2. Personal API token

1. Log in on the website and open **Profile → Personal API tokens**. Enter a name (for example `laptop` or `agent`) and create a token. The token begins with `s26_` and is shown only once; the website stores only a fingerprint of it.
2. Make the token available to the tool, preferably through the environment:

```bash
export SURVEY26_TOKEN=s26_...
survey26 whoami
```

Alternatively, `survey26 login --token-stdin` reads the token from standard input and stores it in `~/.config/survey26/config.json` (readable only by you). `survey26 logout` removes the stored copy.

3. A token acts as you. It cannot do anything that your account cannot do on the website, and organizer functions are never available with a token. Each account may have up to 5 active tokens; the profile page shows when each token was created and last used. Revoke a token there as soon as it is no longer needed or may have been exposed: it stops working immediately. Requests are limited to 120 per minute per account.
4. Anyone who has your token can act as you. Do not put tokens in repositories, project ZIPs, logs, screenshots or chats. A token cannot be used to create or revoke tokens; this is done only on the profile page.

### Temporary Kimi relay (local development)

This is a temporary Kimi allowance from the organizers to help with local development and debugging. It's limited and may change or end at any time. Platform evaluations and the final use the model service each team saves in 'Keys and network' — please make sure yours is set up.

Teams on the leaderboard (one scored formal evaluation in the online phase) can use it from their own machines through an OpenAI-compatible API: base URL `https://vdiemcofukuxglqsmlyz.supabase.co/functions/v1/kimi-relay/v1`, your personal API token (`s26_…`) as the API key, model `kimi-for-coding`. Each team gets about 20,000 requests and 40M tokens per day, at most 2 concurrent requests, and `max_tokens` is capped at 8192; your team's remaining allowance is shown under **Profile → Temporary Kimi relay** and by `survey26 relay status`. Please do not save it as your evaluation model service.

```bash
export OPENAI_BASE_URL=https://vdiemcofukuxglqsmlyz.supabase.co/functions/v1/kimi-relay/v1
export OPENAI_API_KEY=$SURVEY26_TOKEN
```

## 3. Commands

| Website | Command |
| --- | --- |
| Account and profile | `whoami`, `profile show`, `profile set --nickname … --github …`, `profile avatar set FILE`, `profile avatar clear` |
| Find teammates | `teammates list [--looking]`, `teammates visibility show\|on\|off [--blurb … --contact … --seeking …]`, `teammates contact USER_ID`, `teammates invite USER_ID` |
| Team | `team show`, `team members`, `team create NAME [--max-size N]`, `team join CODE`, `team leave`, `team code [--regenerate]`, `team set --name … --max-size … --lock/--unlock`, `team transfer USER_ID`, `team kick USER_ID`, `team disband`, `team directory`, `team request TEAM_ID`, `team invite-uid UID` (captain) |
| Friends | `friends list` (your UID, friends, requests, blocked), `friends add UID`, `friends accept ID`, `friends decline ID`, `friends cancel ID`, `friends remove USER_ID`, `friends block USER_ID`, `friends unblock USER_ID` |
| Notifications | `invites list`, `invites accept ID`, `invites decline ID`, `invites cancel ID` |
| Keys and network | `env show`, `env model --provider kimi\|moonshot\|deepseek\|openai\|anthropic\|zhipu\|custom --key - [--model …] [--prefix NAME] [--replace]` (the “Add a model service” form), `env set NAME --value-stdin [--plain]`, `env set NAME --from-env VAR`, `env unset NAME`, `env disable NAME` / `env enable NAME` (switch off/on, kept), `env tag NAME model\|none` (model-related or not), `env domains set HOST…`, `env domains clear`, `env route [direct\|cn\|overseas] [--fallback\|--no-fallback]` |
| Step 1 · Upload a project | `project upload FILE.zip [--title …]`, `project submit-repo https://github.com/OWNER/REPO [--branch BRANCH] [--subdir FOLDER] [--title …]` (a `…/tree/BRANCH/FOLDER` or `…/commit/SHA` link works too; the exact commit is saved at submission) |
| Step 2 · Review and confirm | `project list [--all]`, `project wait REV`, `project show REV --files`, `project logs REV`, `project confirm REV`, `project withdraw REV`, `project download REV`, `project evidence REV --notes … --code-url …` |
| Step 3 · Evaluate | `quota`, `eval start REV [--no-model] [--phase …]`, `eval selfcheck REV [--no-model] [--phase …]` (evaluate 3 times and average; `--no-model` = “this evaluation without a model”), `eval list [--phase …]`, `eval show BATCH`, `eval wait [BATCH]` |
| Results | `results show BATCH [--phase …]`, `results log RUN [--tail N \| --full \| -o agent.log]`, `results download RUN`, `results download-all [BATCH] [--phase …]`, `results cancel BATCH` (cancel a queued evaluation that has not started) |
| Final version | `final show`, `final set REV`, `final clear` |
| Leaderboard | `leaderboard [--phase online\|practice-projects\|practice] [--card v4-a] [--mine]`, `competition` |
| Kimi Coding Plan and credits | `kimi status`, `kimi claim` (captain), `credits list`, `credits claim PROVIDER` |
| Temporary Kimi relay | `relay status` (base URL, model and your team's remaining allowance today) |

Evaluations go to the same phase as the website's evaluate button: the online competition while it runs, practice afterwards. When the organizers offer an optional extra phase, `survey26 competition` and `survey26 quota` show it by name; it is unscored (not on any leaderboard) and has its own daily evaluations. Evaluate there only on purpose, with `--phase extra` (or the phase's slug); `eval list`, `eval show`, `eval wait`, `results show` and `results download-all` accept the same `--phase` to list or pick `latest` within one phase. `results cancel BATCH` cancels a queued evaluation of your team while none of its cards has started (also an evaluation the dispatcher is about to start but no runner has taken yet), as the website's **Cancel** button does: it is not counted toward today's evaluations and appears on no leaderboard. An evaluation that has started cannot be cancelled (`evaluation_started`). In an “evaluate 3 times” self-check, cancelling one evaluation cancels every evaluation of that set that has not started; those already running or finished are kept. The online `leaderboard` also prints the unranked baseline rows (official examples' averages) where their score would place them, as on the website; `--json` returns them under `baselines`.

`REV` (a project version), `BATCH` (an evaluation) and `RUN` (one card of an evaluation) accept the full ID or a unique prefix of at least 4 characters, as printed by `project list`, `eval list` and `eval show`. `latest` refers to the most recent evaluation.

## 4. A complete workflow

```bash
survey26 env model --provider kimi --key - < key.txt       # OPENAI_API_KEY (secret), OPENAI_BASE_URL, OPENAI_MODEL
survey26 project upload agent.zip --title "my agent"        # uses 1 of the 10 daily uploads
survey26 project wait 1a2b3c4d                              # preparation and public test; exit 9 if it failed
survey26 project logs 1a2b3c4d                              # build log and agent.log of the public test
survey26 project show 1a2b3c4d --files                      # execution settings and adapter files
survey26 project confirm 1a2b3c4d --yes                     # does not use an evaluation
survey26 eval start 1a2b3c4d --yes                          # uses 1 of today's evaluations
survey26 eval wait --timeout 3600                           # scores per card when finished
survey26 results log RUN_ID --tail 100
survey26 results download-all latest -o results.zip
survey26 final set 1a2b3c4d
```

`env model` writes the same three variables as the website's **Add a model service** form, with the same provider presets: the key as a secret, the base URL and the model as plain values. Without `--prefix` the names are `OPENAI_API_KEY`, `OPENAI_BASE_URL` and `OPENAI_MODEL` (`ANTHROPIC_*` for Anthropic); with `--prefix KIMI` they are `KIMI_API_KEY` and so on, so that several providers can be configured side by side. Existing variables are not overwritten unless `--replace` is given (exit code 2 otherwise). `--key -` reads the key from standard input so that it does not appear in the shell history.

`env route` shows or sets the team's egress route for evaluations, the same setting as **Egress route** under Keys and network: `direct` (default), `cn` (China route) or `overseas` (overseas route). With a route, the program's outbound connections go through that platform route; with `--fallback` (the default) a connection goes direct when the route is unavailable, with `--no-fallback` it is refused instead. The run's network record shows the path each destination took.

## 5. Output and exit codes

Every command accepts `--json` and then prints exactly one JSON object on standard output:

```json
{"ok": true, "command": "eval start", "data": {"batch_id": "…", "phase": "online", "revision_id": "…", "repeat": false}}
{"ok": false, "command": "eval start", "error": {"code": "daily_limit", "message": "The daily evaluation limit has been reached.", "exit_code": 8}}
```

`error.code` is the code the website uses; `error.message` is the website's message (English or Chinese, chosen by `--lang` or `SURVEY26_LANG`).

| Exit code | Meaning |
| --- | --- |
| 0 | Success |
| 1 | Refused by the server (see `error.code`) |
| 2 | Usage error, ambiguous ID, or confirmation required (`--yes`) |
| 3 | Missing, invalid or revoked token; account suspended |
| 4 | Not found |
| 5 | Too many requests; wait one minute |
| 6 | Network or server temporarily unavailable; retry later |
| 7 | A `wait` command reached its `--timeout` |
| 8 | A daily or team limit was reached (evaluations, uploads, active evaluation, team size, 20 actions by UID per day) |
| 9 | The awaited preparation or evaluation finished unsuccessfully |

Actions that the website confirms with a dialog (evaluating a version again, the 3-evaluation self-check, cancelling a queued evaluation, withdrawing a version, clearing the final version, leaving, transferring, removing members, disbanding, submitting the same project again within a few minutes) ask for confirmation in an interactive terminal. With `--json` or without a terminal the tool never prompts: such actions fail with exit code 2 unless `--yes` is given. `project wait` and `eval wait` check the status every 15 and 20 seconds by default (`--interval`, at least 5) and stop after `--timeout` seconds.

## 6. Guidance for coding agents

1. Read the token from `SURVEY26_TOKEN`. Never print, log, commit or include it in a project ZIP.
2. Use `--json` and act on `ok`, `error.code` and the exit code.
3. Each `eval start` uses one of the team's evaluations for the day, and `eval selfcheck` uses three. Evaluate only versions that passed their public test, never in an unattended loop, and agree the evaluation budget with your team.
4. Set secrets with `env set NAME --value-stdin` or `--from-env VAR`, not as command arguments, so that they do not appear in shell history or process lists.
5. Use `project wait`, `eval wait` and their `--timeout` instead of polling in a tight loop. Exit code 5 means waiting one minute; exit code 6 means retrying later.
6. Before `project confirm`, inspect `project show REV --files`: confirming states that your team reviewed the execution settings and adapter files.
7. Every decision during an evaluation must be made by your program; the rules on human participation apply in the same way to the command line.
8. The cards that an evaluation runs at the same time (in the online competition A–D first, then A1–D1) each run in their own container. To spread model requests over several keys, save them as KIMI_KEY_1 … KIMI_KEY_8 (up to 20 variables) and let each container pick one, at random or by `task_card.card_id` from the `initialize` message. On HTTP 429, wait and retry instead of failing.

```python
import os, random, zlib

def pick_key(init_payload):
    keys = [os.environ[k] for k in sorted(os.environ) if k.startswith("KIMI_KEY_")]
    if not keys:
        return os.environ.get("KIMI_API_KEY")
    card = (init_payload.get("task_card") or {}).get("card_id", "")
    return keys[zlib.crc32(card.encode()) % len(keys)] if card else random.choice(keys)
```

## 7. Not available from the command line

- Registration, login, password change and password reset: these require the website and are not possible with an API token.
- Creating and revoking API tokens: only on the profile page, so that a leaked token cannot create further tokens.
- Organizer functions: never available with a token, also for organizer accounts.
- The former team model API settings and the in-browser model relay: superseded by **Keys and network** (`survey26 env`).
- The local CSV session and the CSV submission history of the earlier practice round: these formats are no longer accepted.

## 8. Contacts

- Night caretaker Johnny: `POST https://vdiemcofukuxglqsmlyz.supabase.co/functions/v1/sophon/contact-johnny` with `{"token": "$SOPHON_RUN_TOKEN"}` (only works inside a running evaluation).
