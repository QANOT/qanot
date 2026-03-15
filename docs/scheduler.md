# Cron Scheduler

Qanot AI includes an APScheduler-based cron system for running scheduled tasks. The agent can create, update, and delete cron jobs through natural conversation.

## How Cron Jobs Work

Cron jobs are defined in `{cron_dir}/jobs.json`. Each job has a name, cron schedule, execution mode, and a prompt that tells the agent what to do.

```json
[
  {
    "name": "daily-summary",
    "schedule": "0 20 * * *",
    "mode": "isolated",
    "prompt": "Summarize today's conversations and update MEMORY.md",
    "enabled": true
  }
]
```

### Cron Expression Format

Standard 5-field cron: `minute hour day month day_of_week`

| Expression | Meaning |
|------------|---------|
| `0 */4 * * *` | Every 4 hours |
| `0 20 * * *` | Daily at 20:00 |
| `30 9 * * 1-5` | Weekdays at 9:30 |
| `0 0 1 * *` | First day of each month |
| `*/15 * * * *` | Every 15 minutes |

The timezone from config is used for scheduling (default: `Asia/Tashkent`).

## Execution Modes

### isolated

Spawns an independent agent with its own conversation history, context tracker, and session writer.

```json
{
  "mode": "isolated",
  "prompt": "Check for overdue tasks in the workspace"
}
```

**How it works:**

1. A fresh `Agent` instance is created with `prompt_mode="minimal"` (only SOUL.md + TOOLS.md + session info)
2. The prompt is sent as a user message
3. The agent runs its full tool loop (up to 25 iterations)
4. Results are logged to a dedicated session file (`cron-{name}-{timestamp}.jsonl`)
5. If the agent writes to `proactive-outbox.md`, the content is sent to all allowed users

**Use for:** Background tasks that should not interfere with ongoing user conversations. Examples: memory cleanup, periodic web checks, data processing.

### systemEvent

Injects the prompt into the main agent's message queue as a system event.

```json
{
  "mode": "systemEvent",
  "prompt": "Remind the user about their 3pm meeting"
}
```

**How it works:**

1. The prompt is put into the scheduler's message queue
2. The Telegram adapter's proactive loop picks it up
3. The main agent processes it as a regular turn (with full conversation context)

**Use for:** Tasks that need the current conversation context or should appear as part of the ongoing conversation. Examples: reminders, time-based follow-ups, scheduled check-ins.

## Default Heartbeat Job

A heartbeat job is automatically created if it does not exist in `jobs.json`:

```json
{
  "name": "heartbeat",
  "schedule": "0 */4 * * *",
  "mode": "isolated",
  "prompt": "HEARTBEAT: Read HEARTBEAT.md and perform self-improvement checks:\n1. Check proactive-tracker.md -- overdue behaviors?\n2. Pattern check -- repeated requests to automate?\n3. Outcome check -- decisions >7 days old to follow up?\n4. Memory -- context %, update MEMORY.md with distilled learnings\n5. Proactive surprise -- anything to delight human?\nIf you have a message for the human, write it to /data/workspace/proactive-outbox.md",
  "enabled": true
}
```

This runs every 4 hours, prompting the agent to review its own state, clean up memory, and optionally send a proactive message to the user.

## Proactive Messaging

Cron jobs can send messages to users through the `proactive-outbox.md` mechanism:

1. An isolated cron job writes content to `{workspace_dir}/proactive-outbox.md`
2. After the job completes, the scheduler checks the outbox
3. If content exists, it is sent to all `allowed_users` via Telegram
4. The outbox is cleared

This is the only way for isolated cron jobs to communicate with users. System event jobs communicate directly through the conversation.

## Managing Cron Jobs

### Via Tools (in conversation)

The agent can manage cron jobs through natural conversation:

```
User: Set up a daily summary at 8pm
Agent: [calls cron_create with name="daily-summary", schedule="0 20 * * *", ...]
```

Available tools: `cron_create`, `cron_list`, `cron_update`, `cron_delete`. See [Tools](tools.md) for parameters.

### cron_create Parameters

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `name` | string | Yes | Unique job name |
| `prompt` | string | Yes | Reminder text or task prompt |
| `schedule` | string | No* | Cron expression (e.g. `0 9 * * *`) for recurring jobs |
| `at` | string | No* | ISO 8601 timestamp (e.g. `2026-03-12T17:00:00+05:00`) for one-shot reminders |
| `mode` | string | No | `"systemEvent"` (text delivery) or `"isolated"` (full agent). Default: `"systemEvent"` |
| `delete_after_run` | boolean | No | Auto-delete job after execution. Default: `true` for `at` reminders, `false` for recurring |
| `timezone` | string | No | IANA timezone override for this job (e.g. `"Asia/Tashkent"`, `"Europe/London"`) |

\* Either `schedule` or `at` must be provided. Use `schedule` for recurring jobs and `at` for one-shot reminders.

**One-shot reminders:** When using the `at` parameter, the job runs once at the specified ISO 8601 timestamp and is automatically deleted afterward (`delete_after_run` is forced to `true`).

**Per-job timezone:** By default, jobs use the global timezone from config. The `timezone` parameter overrides this for a specific job, useful when the user needs reminders in a different timezone.

### Via jobs.json (manual)

Edit `{cron_dir}/jobs.json` directly. Changes take effect after restarting the bot, or the agent can call `cron_update` to trigger a reload.

## Job Reloading

When a cron tool modifies `jobs.json`, it calls `scheduler.reload_jobs()` which:

1. Removes all existing `cron_*` jobs from the APScheduler
2. Re-reads `jobs.json` from disk
3. Ensures the heartbeat job exists
4. Re-adds all enabled jobs

This means changes take effect immediately without restarting the bot.

## Error Handling

- **Failed isolated jobs:** Errors are logged but do not affect the main bot
- **Failed system events:** Errors are logged and retried on the next proactive loop iteration
- **Invalid cron expressions:** Jobs with expressions that are not 5 fields are skipped with a warning

## Architecture Notes

- The scheduler uses `AsyncIOScheduler` from APScheduler 3.x
- Each isolated job gets a fresh `Agent` with `prompt_mode="minimal"` to keep system prompts small
- The scheduler shares the same `LLMProvider` and `ToolRegistry` as the main agent
- The message queue is an `asyncio.Queue` bridging the scheduler and the Telegram adapter
