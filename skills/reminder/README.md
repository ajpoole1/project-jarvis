# reminder skill

Prints a free-text message to stdout and exits 0. The schedules dispatcher
forwards any non-empty stdout to Discord — so printing IS posting. This skill
never posts to Discord itself.

## Usage

```bash
python3 skills/reminder/skill.py --message "Pick up Ellie at 3pm"
# Output: ⏰ Reminder Pick up Ellie at 3pm

python3 skills/reminder/skill.py --message "Check the oven" --prefix "🔥"
# Output: 🔥 Check the oven
```

## Arguments

| Argument | Required | Default | Notes |
|---|---|---|---|
| `--message` | Yes | — | Reminder text to print |
| `--prefix` | No | `⏰ Reminder` | Prepended to the message with a space |

Empty `--message` → error to stderr, exit 1.

## Environment variables

None. Stdlib only — no virtualenv needed.

## Pairing with schedules — one-off reminders

To schedule a one-off reminder via Jarvis:

```
schedules propose
  --skill reminder
  --schedule once@2026-06-10T14:30
  --description "Reminder: dentist appointment"
  --args '["--message","dentist appointment at 3pm"]'
```

After AJ approves, the dispatcher fires the reminder once at 14:30 local time
(America/Toronto) and retires the job (`enabled=0`).

## Shell metacharacter constraint

The schedules `_validate_args()` check rejects any arg containing shell
metacharacters: `& | > < \ $( ${`

**Reminder messages must avoid these characters.** Use textual alternatives:

| Avoid | Use instead |
|---|---|
| `&` | `and` |
| `>` | `greater than` |
| `<` | `less than` |
| `\|` | `or` / `pipe` |
| `$` | `dollars` / spell out |

Example — instead of `"budget > $500"` write `"budget greater than 500 dollars"`.
