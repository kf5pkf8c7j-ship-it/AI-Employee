# CozySetup.kw — running the AI employee day to day

All commands run from the project folder (`cd ~/AI-Employee`).
Every command that changes something shows what will happen and asks
`Confirm? [y/N]` first. Pressing Enter means **No**.

---

## Your daily routine

**1. Start with the overview.**
```
uv run cozysetup-admin overview
```
It shows everything that needs you: open handoffs, payment screenshots to
check, messages to send yourself, failed messages, today's setup (with what to
collect on arrival), and the next 7 days.

**2. Check payments, then approve or reject.**
```
uv run cozysetup-admin pending                 # bookings waiting for payment or approval
uv run cozysetup-admin proof CS-0001           # open the payment screenshot
uv run cozysetup-admin approve CS-0001 "25 KWD received"
uv run cozysetup-admin reject CS-0001 "no transfer in the account"
```
A screenshot is **not** proof — always check your account first.
Approving confirms the booking, blocks the date, and queues the customer's
confirmation and reminder.

**3. Messages you send yourself** (bookings you added with `add`).
```
uv run cozysetup-admin outbox                  # shows the exact text and the phone number
uv run cozysetup-admin mark-sent 3 "sent on WhatsApp"
```

**4. Handoffs — everything the AI passed to you.**
```
uv run cozysetup-admin handoffs
uv run cozysetup-admin resolve 2 "called the customer, postponed to 8 Oct"
```

**5. After a setup.**
```
uv run cozysetup-admin complete CS-0001
```

---

## Other owner actions

| Situation | Command |
|---|---|
| A booking taken by phone, or a same-day booking you accept | `uv run cozysetup-admin add --date 2026-10-01 --location julaia --name "Ahmad" --phone 99999999 --payment full` (add `--paid` if you already received the money) |
| Cancel | `uv run cozysetup-admin cancel CS-0001 "reason"` — refunds are done by you, outside the system. Tell the customer yourself. |
| Move to another date | `uv run cozysetup-admin reschedule CS-0001 2026-10-08 "reason"` — tell the customer yourself. |
| Everything about one booking | `uv run cozysetup-admin show CS-0001` |
| One message in full | `uv run cozysetup-admin message 3` |
| All bookings | `uv run cozysetup-admin bookings` (`--all` for past ones) |

---

## Keep the outbox sender running

Confirmations and reminders are sent by a separate program. Keep it running
in its own Terminal window:
```
uv run cozysetup-outbox watch
```
It checks every minute, sends what is due, and prints only when something
happens. Stop it with Ctrl+C. To send once and stop: `uv run cozysetup-outbox run`.

- A message that can't be delivered is retried after 5 and then 30 minutes.
  After 3 failed attempts you get a **handoff**, and the booking's reminder
  is cancelled. If you then contact the customer yourself, record it with
  `mark-sent`.
- Reminders go out 3 hours before the setup (3 PM). A reminder is never sent
  after 6 PM, and a confirmation never after the booking date.
- No real channel (WhatsApp, SMS, …) is connected yet: messages to chat
  customers are written to `data/outbox_delivered.log`.

---

## Instagram DMs

Two programs work together, each in its own Terminal window:
```
uv run cozysetup-instagram serve     # receives DMs from Meta and records them (no replies)
uv run cozysetup-instagram work      # answers them with the AI and sends the replies - for real
```
Meta reaches `serve` through the HTTPS tunnel. `work` answers the customer's messages in order;
several messages sent in a row get one answer. The first reply in each conversation starts with
the automated-assistant notice. Voice notes, videos, stickers, reels and shares get the
"unsupported" reply. Messages older than 24 hours are not answered.

**When you reply yourself in the Instagram app, the AI stops answering that customer** — it
won't talk over you. The customer's later messages are kept, so the AI knows the conversation
when it takes over again. To let the AI answer again:
```
uv run cozysetup-instagram conversations      # shows every conversation, and which are paused
uv run cozysetup-instagram resume CUSTOMER_ID # the id shown in the list
```

If a reply can't be sent (after 3 tries, or because Instagram's 24-hour reply window has closed),
you get a handoff with the exact text, so you can send it yourself.

Not connected yet: confirmations and reminders for Instagram bookings are not sent on Instagram.
The outbox gives you a handoff for them instead — send them yourself.

---

## Practising safely

The practice chat and the practice database never touch your real bookings:
```
uv run cozysetup-chat                                               # you are the customer
uv run cozysetup-outbox --db data/practice/practice.db watch        # in another window
uv run cozysetup-admin --db data/practice/practice.db overview      # the owner's view of practice
```
Confirmations and reminders delivered to your practice conversation appear
in the chat (📩), or type `/inbox`.

To start the practice over: delete `data/practice/practice.db`.

---

## Before changing the AI's instructions, rules or model

Run the test conversations against the real AI **before** committing a change:
```
uv run cozysetup-eval                        # 23 conversations × 3 runs, about $0.25
uv run cozysetup-eval --only 20 --runs 3     # a single conversation, a few cents
```
All must pass: safety conversations in every run, the others in at least 90%.
The report and every transcript are saved in `data/evals/`.

The free automatic tests (no AI, no cost):
```
uv run pytest
```

---

## Where things are

| What | Where |
|---|---|
| Your business facts, prices, wording | `config/business.toml` — check it with `uv run python -m cozysetup.show_business_info` |
| The API key | `.env` — never commit it, never paste it into a chat |
| Real bookings | `data/cozysetup.db` |
| Database backups made before upgrades | `data/*.before-v2.bak`, `.before-v3.bak`, `.before-v4.bak` |
| Payment screenshots | `data/payment_proofs/` |
| Messages "delivered" to chat customers | `data/outbox_delivered.log` |
| The AI's Instagram conversation logs | `data/conversations/instagram_*.jsonl` |
| Practice data | `data/practice/` |
| Test conversation reports | `data/evals/` |

Everything in `data/` stays on this computer and is never saved in git.
Back up the `data/` folder regularly — it holds your real bookings.
