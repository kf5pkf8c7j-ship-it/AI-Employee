"""Run the test conversations in evals/conversations.toml against the AI model.

    uv run cozysetup-eval                          # gpt-6-sol, low effort, 3 runs each
    uv run cozysetup-eval --model gpt-6-luna       # the same conversations on another model
    uv run cozysetup-eval --effort medium
    uv run cozysetup-eval --only 03,19 --runs 1    # a cheap partial run

This calls the real API and costs money, so it asks before starting.
Each conversation runs in its own temporary database with a fixed "today".
Results go to data/evals/<time>_<model>_<effort>/ (report.md + logs).
"""

from __future__ import annotations

import argparse
import re
import sys
import tempfile
import tomllib
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

import openai

from cozysetup.agent import EFFORT, Agent, ConversationLog, Usage
from cozysetup.bookings import BookingService
from cozysetup.business_info import BusinessInfo, load_business_info
from cozysetup.database import DEFAULT_DB_PATH, HandoffType, connect
from cozysetup.settings import MODEL, PRICE_PER_MILLION, PROJECT_DIR, MissingApiKey, load_api_key
from cozysetup.tools import Conversation

DEFAULT_FILE = PROJECT_DIR / "evals" / "conversations.toml"
DEFAULT_RESULTS_DIR = DEFAULT_DB_PATH.parent / "evals"
# A tiny valid JPG header - the AI never sees images, only that one arrived.
TEST_SCREENSHOT = b"\xff\xd8\xff\xe0" + bytes(100)
ARABIC_LETTER = re.compile(r"[؀-ۿ]")

CHECKS = {
    "new_bookings", "booking", "setup_status", "handoffs", "tools_called", "tools_not_called",
    "no_tools", "tool_order", "replies_contain", "replies_contain_any", "replies_never_contain",
    "reply_script",
}
BOOKING_FIELDS = {"status", "payment_choice", "location_id", "date", "amount_now", "language"}
CONVERSATION_FIELDS = {"id", "name", "safety", "customer", "image_with", "setup", "expect"}
SETUP_FIELDS = {"date", "location", "name", "phone", "payment", "status"}
SETUP_STATUSES = {"pending_payment", "payment_submitted", "confirmed"}


class EvalFileError(Exception):
    """The conversations file has a mistake."""


# --- The conversations file ------------------------------------------------------

@dataclass(frozen=True)
class SetupBooking:
    date: date
    location: str
    name: str
    phone: str
    payment: str
    status: str


@dataclass(frozen=True)
class TestConversation:
    __test__ = False   # tells pytest this is not a test class, despite its name
    id: str
    name: str
    safety: bool
    customer: tuple[str, ...]
    image_with: int | None
    setup: tuple[SetupBooking, ...]
    expect: dict


@dataclass(frozen=True)
class EvalSettings:
    today: datetime            # the fixed "now", in the business's timezone
    runs: int
    other_pass_rate: float


def load_conversations(info: BusinessInfo, path: Path = DEFAULT_FILE) -> tuple[EvalSettings, list[TestConversation]]:
    try:
        with open(path, "rb") as file:
            data = tomllib.load(file)
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise EvalFileError(f"Can't read {path}: {error}") from None

    raw = data.get("settings", {})
    settings = EvalSettings(
        today=raw["today"].replace(tzinfo=info.timezone),
        runs=int(raw["runs"]),
        other_pass_rate=float(raw["other_pass_rate"]),
    )
    phrases = list(raw.get("confirmed_phrases", []))

    conversations, seen = [], set()
    for item in data.get("conversation", []):
        where = f"conversation {item.get('id', '?')}"
        _no_unknown(item, CONVERSATION_FIELDS, where)
        if item["id"] in seen:
            raise EvalFileError(f"{where}: the id is used twice")
        seen.add(item["id"])

        expect = dict(item.get("expect", {}))
        _no_unknown(expect, CHECKS, f"{where} expect")
        _no_unknown(expect.get("booking", {}), BOOKING_FIELDS, f"{where} expect.booking")
        for key in ("replies_contain", "replies_contain_any", "replies_never_contain"):
            if key in expect:   # "@confirmed_phrases" stands for the shared list
                expect[key] = [p for text in expect[key] for p in (phrases if text == "@confirmed_phrases" else [text])]

        setup = []
        for entry in item.get("setup", []):
            _no_unknown(entry, SETUP_FIELDS, f"{where} setup")
            if entry["status"] not in SETUP_STATUSES:
                raise EvalFileError(f"{where} setup: unknown status {entry['status']!r}")
            setup.append(SetupBooking(**entry))

        customer = tuple(item["customer"])
        image_with = item.get("image_with")
        if image_with is not None and not 1 <= image_with <= len(customer):
            raise EvalFileError(f"{where}: image_with must be a message number from 1 to {len(customer)}")

        conversations.append(TestConversation(
            id=item["id"], name=item["name"], safety=bool(item["safety"]), customer=customer,
            image_with=image_with, setup=tuple(setup), expect=expect,
        ))
    return settings, conversations


def _no_unknown(table: dict, allowed: set[str], where: str) -> None:
    unknown = set(table) - allowed
    if unknown:
        raise EvalFileError(f"{where}: unknown field(s) {sorted(unknown)} (misspelled?)")


# --- Running one conversation ------------------------------------------------------

@dataclass(frozen=True)
class Check:
    name: str
    passed: bool
    detail: str


@dataclass
class RunResult:
    conversation: TestConversation
    run: int
    transcript: list[tuple[str, str]] = field(default_factory=list)   # ("customer"/"ai", text)
    tools: list[str] = field(default_factory=list)
    checks: list[Check] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    error: str | None = None

    @property
    def passed(self) -> bool:
        return self.error is None and all(check.passed for check in self.checks)


def run_conversation(
    conversation: TestConversation, run: int, *, client, info: BusinessInfo, settings: EvalSettings,
    model: str, effort: str, log_dir: Path,
) -> RunResult:
    result = RunResult(conversation, run)
    with tempfile.TemporaryDirectory() as workspace:
        db = connect(Path(workspace) / "eval.db")
        try:
            service = BookingService(db, info, clock=lambda: settings.today,
                                     proofs_dir=Path(workspace) / "payment_proofs")
            setup_refs = [_create_setup_booking(service, booking) for booking in conversation.setup]
            handoffs_before = {handoff.id for handoff in service.list_handoffs(status=None)}

            log = ConversationLog(log_dir / f"{conversation.id}_run{run}.jsonl")
            chat = Conversation(channel="eval", channel_user_id=f"eval-{conversation.id}-{run}")
            agent = Agent(client, service, chat, model=model, effort=effort, log=log)

            problems = []
            for number, text in enumerate(conversation.customer, start=1):
                images = [TEST_SCREENSHOT] if number == conversation.image_with else []
                result.transcript.append(("customer", text + (" [+ screenshot]" if images else "")))
                reply = agent.reply(text, images=images)
                result.transcript.append(("ai", reply.text))
                result.tools += reply.tools_used
                _add_usage(result.usage, reply.usage)
                if reply.handed_off_after_problem:
                    problems.append(f"after message {number}")

            new_handoffs = [h for h in service.list_handoffs(status=None) if h.id not in handoffs_before]
            new_bookings = [b for b in service.list_bookings() if b.reference not in setup_refs]
            statuses = {ref: service.get_booking(ref).status.value for ref in setup_refs}
            result.checks = evaluate(conversation.expect, result, new_bookings, new_handoffs, statuses, problems)
        except Exception as error:   # a crash in one conversation must not stop the whole run
            result.error = f"{type(error).__name__}: {error}"
        finally:
            db.close()
    return result


def _create_setup_booking(service: BookingService, booking: SetupBooking) -> str:
    created = service.create_owner_booking(
        booking_date=booking.date, location_id=booking.location, customer_name=booking.name,
        customer_phone=booking.phone, payment_choice=booking.payment, paid=booking.status == "confirmed",
    ).booking
    if booking.status == "payment_submitted":
        service.attach_payment_proof(created.reference, booking.phone, TEST_SCREENSHOT)
    return created.reference


def _add_usage(total: Usage, usage: Usage) -> None:
    total.input_tokens += usage.input_tokens
    total.output_tokens += usage.output_tokens
    total.cache_read_tokens += usage.cache_read_tokens
    total.cache_write_tokens += usage.cache_write_tokens
    total.requests += usage.requests


# --- Checking -------------------------------------------------------------------------

def evaluate(expect: dict, result: RunResult, new_bookings, new_handoffs, setup_statuses, problems) -> list[Check]:
    replies = [text for who, text in result.transcript if who == "ai"]
    all_replies = "\n".join(replies).casefold()
    checks = [Check("no problems", not problems,
                    "none" if not problems else f"automatic handoff {', '.join(problems)}")]

    def add(name: str, passed: bool, detail: str) -> None:
        checks.append(Check(name, passed, detail))

    if "new_bookings" in expect:
        add("new_bookings", len(new_bookings) == expect["new_bookings"],
            f"expected {expect['new_bookings']}, got {len(new_bookings)}")

    if "booking" in expect:
        if len(new_bookings) != 1:
            add("booking", False, f"expected exactly 1 new booking to inspect, got {len(new_bookings)}")
        else:
            booking = new_bookings[0]
            actual = {
                "status": booking.status.value,
                "payment_choice": booking.payment_choice.value,
                "location_id": booking.location_id,
                "date": booking.booking_date,
                "amount_now": booking.amounts.amount_now / 1000,
                "language": booking.language.value if booking.language else None,
            }
            wrong = [f"{key}: expected {value!r}, got {actual[key]!r}"
                     for key, value in expect["booking"].items() if actual[key] != value]
            add("booking", not wrong, "; ".join(wrong) or "all values as expected")

    if "setup_status" in expect:
        wrong = [f"{ref}: expected {status}, got {setup_statuses.get(ref)}"
                 for ref, status in expect["setup_status"].items() if setup_statuses.get(ref) != status]
        add("setup_status", not wrong, "; ".join(wrong) or "unchanged as expected")

    if "handoffs" in expect:
        actual = sorted(h.type.value for h in new_handoffs)
        add("handoffs", actual == sorted(expect["handoffs"]), f"expected {sorted(expect['handoffs'])}, got {actual}")

    if "tools_called" in expect:
        missing = [t for t in expect["tools_called"] if t not in result.tools]
        add("tools_called", not missing, f"missing: {missing}" if missing else f"used: {result.tools}")

    if "tools_not_called" in expect:
        used = [t for t in expect["tools_not_called"] if t in result.tools]
        add("tools_not_called", not used, f"should not have used: {used}" if used else "none of them used")

    if expect.get("no_tools"):
        add("no_tools", not result.tools, f"used: {result.tools}" if result.tools else "no tools used")

    if "tool_order" in expect:
        order = expect["tool_order"]
        firsts = [result.tools.index(t) if t in result.tools else None for t in order]
        in_order = None not in firsts and firsts == sorted(firsts)
        add("tool_order", in_order, f"expected {' -> '.join(order)}, used {result.tools}")

    if "replies_contain" in expect:
        missing = [t for t in expect["replies_contain"] if t.casefold() not in all_replies]
        add("replies_contain", not missing, f"missing: {missing}" if missing else "all present")

    if "replies_contain_any" in expect:
        found = [t for t in expect["replies_contain_any"] if t.casefold() in all_replies]
        add("replies_contain_any", bool(found),
            f"found: {found}" if found else f"none of {expect['replies_contain_any']}")

    if "replies_never_contain" in expect:
        found = [t for t in expect["replies_never_contain"] if t.casefold() in all_replies]
        add("replies_never_contain", not found, f"found: {found}" if found else "none present")

    if "reply_script" in expect:
        if expect["reply_script"] == "arabic":
            bad = [i for i, reply in enumerate(replies, 1) if not ARABIC_LETTER.search(reply)]
            add("reply_script", not bad, f"replies without Arabic letters: {bad}" if bad else "all Arabic")
        else:
            bad = [i for i, reply in enumerate(replies, 1) if ARABIC_LETTER.search(reply)]
            add("reply_script", not bad, f"replies with Arabic letters: {bad}" if bad else "all Latin letters")

    return checks


# --- Scoring and the report ------------------------------------------------------------

@dataclass(frozen=True)
class Score:
    conversation: TestConversation
    passes: int
    runs: int
    required: str
    ok: bool


def score(results: list[RunResult], settings: EvalSettings) -> list[Score]:
    scores = []
    conversations = {r.conversation.id: r.conversation for r in results}   # keeps the file's order
    for conversation in conversations.values():
        runs = [r for r in results if r.conversation.id == conversation.id]
        passes = sum(r.passed for r in runs)
        if conversation.safety:
            ok, required = passes == len(runs), "every run (safety)"
        else:
            ok, required = passes / len(runs) >= settings.other_pass_rate, f"≥ {settings.other_pass_rate:.0%}"
        scores.append(Score(conversation, passes, len(runs), required, ok))
    return scores


def write_report(path: Path, results: list[RunResult], scores: list[Score], *, model: str, effort: str,
                 cost: float) -> None:
    verdict = "PASSED" if all(s.ok for s in scores) else "FAILED"
    lines = [
        f"# CozySetup evaluation - {verdict}",
        "",
        f"Model: `{model}` · effort: `{effort}` · {len(results)} conversation runs · "
        f"estimated cost ${cost:.2f}",
        "",
        "| Conversation | Safety | Passed | Required | Result |",
        "|---|---|---|---|---|",
    ]
    for s in scores:
        lines.append(f"| {s.conversation.id} {s.conversation.name} | {'yes' if s.conversation.safety else ''} "
                     f"| {s.passes}/{s.runs} | {s.required} | {'✅' if s.ok else '❌'} |")

    failed = [r for r in results if not r.passed]
    lines += ["", "## Failed runs", "" if failed else "None."]
    for r in failed:
        lines.append(f"### {r.conversation.id}, run {r.run}")
        if r.error:
            lines.append(f"- **Error:** {r.error}")
        lines += [f"- **{c.name}:** {c.detail}" for c in r.checks if not c.passed]
        lines.append("")

    lines += ["", "## Transcripts (for reviewing the tone)", ""]
    for r in results:
        lines.append(f"### {r.conversation.id}, run {r.run} - {'passed' if r.passed else 'FAILED'}")
        lines.append(f"Tools: {', '.join(r.tools) or 'none'}")
        lines.append("")
        for who, text in r.transcript:
            speaker = "**Customer:**" if who == "customer" else "**CozySetup:**"
            lines.append(f"{speaker} " + text.replace("\n", "  \n"))
            lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")


# --- The command ------------------------------------------------------------------------------

def main(
    argv: list[str] | None = None,
    *,
    client=None,
    ask: Callable[[str], str] = input,
    results_dir: Path = DEFAULT_RESULTS_DIR,
) -> int:
    """`client`, `ask` and `results_dir` let tests use a pretend model and temporary folders."""
    parser = argparse.ArgumentParser(prog="cozysetup-eval",
                                     description="Run the test conversations against the AI model.")
    parser.add_argument("--model", default=MODEL, choices=sorted(PRICE_PER_MILLION))
    parser.add_argument("--effort", default=EFFORT, choices=["none", "low", "medium", "high", "xhigh", "max"])
    parser.add_argument("--runs", type=int, help="runs per conversation (default: from the file)")
    parser.add_argument("--only", help="comma-separated conversation numbers or ids, e.g. 03,19")
    parser.add_argument("--file", type=Path, default=DEFAULT_FILE)
    parser.add_argument("-y", "--yes", action="store_true", help="don't ask before starting")
    args = parser.parse_args(argv)

    info = load_business_info()
    try:
        settings, conversations = load_conversations(info, args.file)
    except EvalFileError as error:
        print(f"✗ {error}", file=sys.stderr)
        return 1
    if args.only:
        wanted = [part.strip() for part in args.only.split(",")]
        conversations = [c for c in conversations if any(c.id == w or c.id.startswith(w + "_") for w in wanted)]
        if not conversations:
            print(f"✗ No conversation matches {args.only!r}", file=sys.stderr)
            return 1
    runs = args.runs or settings.runs

    total_runs = len(conversations) * runs
    total_messages = sum(len(c.customer) for c in conversations) * runs
    print(f"{len(conversations)} conversations × {runs} runs = {total_runs} runs "
          f"({total_messages} customer messages) on {args.model}, effort {args.effort}.")
    print("This calls the real API and costs money.")
    if not args.yes:
        try:
            answer = ask("Start? [y/N] ")
        except EOFError:
            answer = ""
        if answer.strip().lower() not in ("y", "yes"):
            print("Nothing run.")
            return 1

    if client is None:
        try:
            client = openai.OpenAI(api_key=load_api_key())
        except MissingApiKey as error:
            print(f"✗ {error}", file=sys.stderr)
            return 1

    out_dir = results_dir / f"{datetime.now():%Y-%m-%d_%H-%M-%S}_{args.model}_{args.effort}"
    out_dir.mkdir(parents=True, exist_ok=True)
    results = []
    for conversation in conversations:
        for run in range(1, runs + 1):
            result = run_conversation(conversation, run, client=client, info=info, settings=settings,
                                      model=args.model, effort=args.effort, log_dir=out_dir / "logs")
            results.append(result)
            mark = "✓" if result.passed else "✗"
            print(f"  {mark} {conversation.id} run {run}")

    scores = score(results, settings)
    total = Usage()
    for result in results:
        _add_usage(total, result.usage)
    cost = total.cost_usd(args.model)
    report = out_dir / "report.md"
    write_report(report, results, scores, model=args.model, effort=args.effort, cost=cost)

    print()
    for s in scores:
        print(f"  {'✅' if s.ok else '❌'} {s.conversation.id:<26} {s.passes}/{s.runs}  (needs {s.required})")
    passed = all(s.ok for s in scores)
    print(f"\n{'PASSED' if passed else 'FAILED'} - {sum(s.ok for s in scores)}/{len(scores)} conversations "
          f"meet their requirement · {total.requests} requests · about ${cost:.2f}")
    print(f"Report and transcripts: {report}")
    return 0 if passed else 2


if __name__ == "__main__":
    sys.exit(main())
