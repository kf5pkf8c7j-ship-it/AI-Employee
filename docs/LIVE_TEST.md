# The first controlled live Instagram test

A step-by-step checklist. Only the accounts you name with `--only` ever get an AI reply;
everyone else's DMs wait for you. Do it on the **practice database** first.

Replace `@tester.one` with the Instagram account you send the test DMs from
(it must be an accepted Instagram Tester of the app).

---

## 1. Before you start

- [ ] Stop anything still running from earlier tests (old `serve`, old tunnel): Ctrl+C in their
      windows. **An old `serve` must not keep running** — the next step upgrades the database, and
      old code can't read the upgraded file (every Meta delivery would fail).
- [ ] `.env` has `OPENAI_API_KEY`, `IG_ACCESS_TOKEN`, `IG_ACCOUNT_ID`, `META_APP_SECRET`
      (the **App settings → Basic** App Secret) and `WEBHOOK_VERIFY_TOKEN`.
- [ ] The token works (read-only, sends nothing):
      `uv run python -m cozysetup.check_instagram`
- [ ] Upgrade the practice database (a backup `practice.db.before-v6.bak` is made first):
      `uv run cozysetup-instagram status --db data/practice/practice.db`
- [ ] Old test messages: anything older than 24 hours is ignored automatically. Anything newer from
      the tester **will be answered** when `work` starts — check with `status` first.

## 2. Start, in this order (one Terminal window each)

1. The receiver:
   `uv run cozysetup-instagram serve --db data/practice/practice.db`
2. The tunnel: `cloudflared tunnel --url http://127.0.0.1:8000`
   — if its address changed, update the Callback URL in the Meta dashboard
   (…`/webhooks/instagram`) and click **Verify and save**. The `serve` window shows
   "webhook verified by Meta".
3. The worker, **test mode**:
   `uv run cozysetup-instagram work --only @tester.one --db data/practice/practice.db`
   It must say: `from ONLY @tester.one (test mode)`.
4. The outbox:
   `uv run cozysetup-outbox watch --db data/practice/practice.db`

## 3. The test, from the tester's phone

| # | Send from @tester.one | Expect |
|---|---|---|
| 1 | "Hi, how much is the setup?" | The automated-assistant notice, then the price |
| 2 | A voice note | The "can't open voice notes" reply, no AI |
| 3 | Book: location, a date 2+ days ahead, payment choice, name, phone | The booking summary |
| 4 | "yes" | "Your booking CS-… has been created" + Wamd details |
| 5 | A screenshot (any image) | "We've received your screenshot" |

Then, as the owner:

- [ ] `uv run cozysetup-admin --db data/practice/practice.db conversation @tester.one` — the whole chat
- [ ] `… pending`, `… proof CS-XXXX`, `… approve CS-XXXX "test"`
- [ ] The outbox window shows the confirmation **sent**; it arrives on the tester's phone
- [ ] Reply to the tester yourself in the Instagram app → the worker shows `owner_replied`, and
      `… conversation @tester.one` shows **AI paused**
- [ ] `… resume @tester.one`, send one more message from the tester → the AI answers again
- [ ] `… overview` — the INSTAGRAM section shows the worker running and nothing failed

If anyone else writes during the test, the worker shows `not_answered` and the overview counts
them: answer them yourself in the Instagram app.

## 4. Stop

Ctrl+C in each window (worker first, then outbox, serve, tunnel). Nothing is lost: messages that
arrive while the worker is stopped are answered when it starts again (if under 24 hours old).

## If something goes wrong

- **Stop the worker first** (Ctrl+C) — then nothing more is sent.
- A customer should not get AI replies: `… pause @username`.
- `uv run cozysetup-instagram status --db …` and `… handoffs` show what failed.
- Every AI conversation is logged in `data/practice/conversations/instagram_*.jsonl`.
