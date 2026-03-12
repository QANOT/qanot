# TOOLS.md - Local Notes

Skills define _how_ tools work. This file is for _your_ specifics — the stuff that's unique to your setup.

## What Goes Here

Things like:

- Camera names and locations
- SSH hosts and aliases
- Preferred voices for TTS
- Speaker/room names
- Device nicknames
- Anything environment-specific

## Examples

```markdown
### Cameras

- living-room → Main area, 180° wide angle
- front-door → Entrance, motion-triggered

### SSH

- home-server → 192.168.1.100, user: admin

### TTS

- Preferred voice: "Nova" (warm, slightly British)
- Default speaker: Kitchen HomePod
```

## Why Separate?

Skills are shared. Your setup is yours. Keeping them apart means you can update skills without losing your notes, and share skills without leaking your infrastructure.

---

## Web Search

You have a `web_search` tool. Use it for:
- Real-time information (weather, news, prices, sports scores)
- Facts you're unsure about — verify before answering
- Anything that may have changed after your training data

Do NOT search for things you already know well. Only search when the answer requires current data.

## Reminders

You can set reminders using `cron_create`:
- **One-shot**: Use `at` with ISO 8601 timestamp (e.g. "remind me at 5pm" → calculate timestamp, use `at`)
- **Recurring**: Use `schedule` with cron expression (e.g. "every morning at 9" → `"0 9 * * *"`)
- **mode**: Use `systemEvent` for simple text reminders, `isolated` for tasks needing your tools

When the user says "remind me" or "eslatib qo'y":
1. Calculate the exact ISO 8601 timestamp from their request
2. Write the reminder text as what they should see when it fires (not "I will remind you" but the actual reminder content)
3. Use `at` parameter for one-shot reminders (auto-deletes after firing)
4. Confirm with the user what time you scheduled it for

---

Add whatever helps you do your job. This is your cheat sheet.
