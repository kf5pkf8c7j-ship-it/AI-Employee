"""Tests for the Instagram test list (--only), usernames, the heartbeat and `status` (Step 7.6).

Pretend AI, pretend Instagram, made-up accounts, temporary databases - nothing real.
"""

import pytest

from cozysetup.instagram_status import WORKER_SILENT_AFTER, instagram_status
from cozysetup.instagram_worker import NOT_ON_TEST_LIST, PROFILE_RETRY, InstagramProfiles
from cozysetup.senders import SendError
from cozysetup.serve_instagram import main
from tests.test_agent import PretendOpenAI, answer, text
from tests.test_instagram_worker import CUSTOMER, DISCLOSURE, NOW, OTHER_CUSTOMER, PretendSender, Setup, image


class Profiles:
    """Pretend username lookups: {instagram id: (username, name)}; missing ids fail."""

    def __init__(self, known):
        self.known = dict(known)
        self.asked = []

    def __call__(self, customer_id):
        self.asked.append(customer_id)
        if customer_id not in self.known:
            raise RuntimeError("Instagram didn't answer")
        return self.known[customer_id]


@pytest.fixture
def make(tmp_path):
    made = []

    def build(**parts):
        setup = Setup(tmp_path / "cozysetup.db", **parts)
        made.append(setup)
        return setup

    yield build
    for setup in made:
        setup.close()


TESTERS = {CUSTOMER: ("tester.one", "Tester One"), OTHER_CUSTOMER: ("a.real.customer", "Real Customer")}


# --- Only the accounts on the list get AI replies ---------------------------------------------------

def test_a_listed_tester_is_answered_and_their_username_is_learned_once(make):
    profiles = Profiles(TESTERS)
    s = make(client=PretendOpenAI(answer(text("Hello!")), answer(text("It's 50 KWD."))),
             profiles=profiles, only={"@tester.one"})
    s.receive(s.dm("Hi"))
    s.worker.run_once()
    s.receive(s.dm("How much?"))
    s.worker.run_once()
    assert [t for _, t in s.sender.sent] == [DISCLOSURE, "Hello!", "It's 50 KWD."]
    stored = s.conversation()
    assert (stored.username, stored.display_name) == ("tester.one", "Tester One")
    assert profiles.asked == [CUSTOMER]                         # looked up once, not every message


def test_someone_not_on_the_list_never_gets_an_ai_reply(make):
    s = make(client=PretendOpenAI(), profiles=Profiles(TESTERS), only={"tester.one"})
    s.receive(s.dm("Hi, is Thursday free?", customer=OTHER_CUSTOMER), s.dm(customer=OTHER_CUSTOMER,
                                                                            attachments=image()))
    results = s.worker.run_once()
    assert s.client.requests == [] and s.sender.sent == [] and s.download.urls == []
    assert [r.what for r in results] == ["not_answered", "not_answered"]
    assert "@a.real.customer (Real Customer)" in results[0].detail
    assert {(m["status"], m["error"]) for m in s.inbound()} == {("waiting", NOT_ON_TEST_LIST)}
    assert s.replies() == []
    assert s.worker.run_once() == []                            # reported once, not every round
    assert instagram_status(s.db, s.clock()).not_on_test_list == 2


def test_with_the_list_an_unknown_username_is_never_answered_and_looked_up_again_later(make):
    profiles = Profiles({})
    s = make(client=PretendOpenAI(answer(text("Hello!"))), profiles=profiles, only={"tester.one"})
    s.receive(s.dm("Hi"))
    s.worker.run_once()
    s.worker.run_once()                                         # too soon to ask Instagram again
    assert s.client.requests == [] and s.sender.sent == []
    assert profiles.asked == [CUSTOMER]

    profiles.known[CUSTOMER] = ("tester.one", None)            # Instagram answers now
    s.clock.advance(minutes=PROFILE_RETRY.total_seconds() / 60)
    s.worker.run_once()
    assert [t for _, t in s.sender.sent] == [DISCLOSURE, "Hello!"]


def test_without_the_list_a_failed_lookup_never_stops_a_reply(make):
    s = make(client=PretendOpenAI(answer(text("Hello!"))), profiles=Profiles({}))
    s.receive(s.dm("Hi"))
    s.worker.run_once()
    assert [t for _, t in s.sender.sent] == [DISCLOSURE, "Hello!"]
    assert s.conversation().username is None


def test_usernames_on_the_list_ignore_capitals_and_the_at_sign(make):
    s = make(client=PretendOpenAI(answer(text("Hello!"))), profiles=Profiles({CUSTOMER: ("Tester.One", None)}),
             only={" @TESTER.one "})
    s.receive(s.dm("Hi"))
    s.worker.run_once()
    assert len(s.sender.sent) == 2


def test_the_owner_replying_to_someone_not_on_the_list_still_pauses_the_ai(make):
    s = make(client=PretendOpenAI(), profiles=Profiles(TESTERS), only={"tester.one"})
    s.receive(s.dm("Hi", customer=OTHER_CUSTOMER), s.dm("Hello, how can I help?", customer=OTHER_CUSTOMER,
                                                        echo=True))
    results = s.worker.run_once()
    assert [r.what for r in results] == ["not_answered", "owner_replied"]
    assert s.conversation(OTHER_CUSTOMER).ai_paused
    assert s.client.requests == [] and s.sender.sent == []


def test_replies_saved_earlier_are_not_sent_to_someone_off_the_list(make):
    everyone = make(client=PretendOpenAI(answer(text("Hello!"))), sender=PretendSender(SendError("busy")),
                    profiles=Profiles(TESTERS))
    everyone.receive(everyone.dm("Hi", customer=OTHER_CUSTOMER))
    everyone.worker.run_once()                                  # saved, but Instagram was busy
    assert {r["status"] for r in everyone.replies()} == {"pending"}

    test_mode = make(client=PretendOpenAI(), profiles=Profiles(TESTERS), only={"tester.one"})
    test_mode.clock.advance(minutes=10)
    test_mode.worker.run_once()
    assert test_mode.sender.sent == []
    assert {r["status"] for r in test_mode.replies()} == {"pending"}


def test_messages_from_people_off_the_list_are_ignored_after_24_hours(make):
    s = make(client=PretendOpenAI(), profiles=Profiles(TESTERS), only={"tester.one"})
    s.receive(s.dm("Hi", customer=OTHER_CUSTOMER))
    s.worker.run_once()
    s.clock.advance(hours=25)
    s.worker.run_once()
    assert s.inbound()[0]["status"] == "ignored"


def test_instagram_profiles_asks_for_username_and_name():
    calls = []

    def get(path, params, token):
        calls.append((path, params, token))
        return {"username": "sara.k", "name": "Sara", "id": path}

    assert InstagramProfiles("TOKEN", get=get)("123") == ("sara.k", "Sara")
    assert calls == [("123", {"fields": "username,name"}, "TOKEN")]


# --- Heartbeat and status -------------------------------------------------------------------------------

def test_every_round_leaves_a_heartbeat(make):
    s = make(client=PretendOpenAI(answer(text("Hello!"))))
    s.worker.run_once()
    status = instagram_status(s.db, s.clock())
    assert status.worker_last_round_at == NOW and status.worker_details == "nothing to do"
    assert not status.worker_silent
    s.receive(s.dm("Hi"))
    s.clock.advance(seconds=5)
    s.worker.run_once()
    status = instagram_status(s.db, s.clock())
    assert status.worker_details == "answered 1, sent 2"


def test_status_notices_when_the_worker_has_stopped(make):
    s = make()
    assert instagram_status(s.db, s.clock()).worker_silent       # never ran
    s.worker.run_once()
    s.clock.advance(minutes=WORKER_SILENT_AFTER.total_seconds() / 60 + 1)
    status = instagram_status(s.db, s.clock())
    assert status.worker_silent and status.needs_attention
    assert "is it still running?" in status.lines(str)[0]


def test_status_command(make, capsys):
    s = make(client=PretendOpenAI(), profiles=Profiles(TESTERS), only={"tester.one"})
    s.receive(s.dm("Hi", customer=OTHER_CUSTOMER))
    s.worker.run_once()
    assert main(["status", "--db", str(s.path)], clock=s.clock) == 1        # 1 = something needs you
    out = capsys.readouterr().out
    assert "Worker running" in out
    assert "1 message(s) from people not on the test list" in out


# --- The work command insists on a list (or an explicit "everyone") -------------------------------------

def test_work_refuses_to_start_without_saying_who_it_may_answer(make, capsys):
    s = make()
    with pytest.raises(SystemExit):
        main(["work", "--once", "--db", str(s.path)], client=PretendOpenAI(), sender=PretendSender(),
             profiles=Profiles({}))
    assert "one of the arguments --only --answer-everyone is required" in capsys.readouterr().err


@pytest.mark.parametrize("value", ["", " , ", "@"])
def test_an_empty_list_is_refused(make, capsys, value):
    s = make()
    with pytest.raises(SystemExit):
        main(["work", "--once", "--only", value, "--db", str(s.path)], client=PretendOpenAI(),
             sender=PretendSender(), profiles=Profiles({}))
    assert "name at least one @username" in capsys.readouterr().err


def test_the_list_and_everyone_cannot_be_combined(make, capsys):
    s = make()
    with pytest.raises(SystemExit):
        main(["work", "--only", "@a", "--answer-everyone", "--db", str(s.path)])
    assert "not allowed with argument" in capsys.readouterr().err


def test_work_says_who_it_answers(make, capsys):
    s = make()
    main(["work", "--only", "@Tester.One,@second", "--db", str(s.path)], client=PretendOpenAI(),
         sender=PretendSender(), profiles=Profiles({}), clock=s.clock, sleep=lambda seconds: None, max_rounds=1)
    assert "from ONLY @second, @tester.one (test mode)" in capsys.readouterr().out


def test_work_with_the_list_answers_only_the_tester(make):
    s = make()
    s.receive(s.dm("Hi"), s.dm("Hi too", customer=OTHER_CUSTOMER))
    client, sender = PretendOpenAI(answer(text("Hello tester!"))), PretendSender()
    assert main(["work", "--once", "--only", "@tester.one", "--db", str(s.path)], client=client, sender=sender,
                profiles=Profiles(TESTERS), clock=s.clock) == 0
    assert sender.sent == [(CUSTOMER, DISCLOSURE), (CUSTOMER, "Hello tester!")]


# --- Database version 6 -----------------------------------------------------------------------------------

def test_a_version_5_database_is_upgraded_keeping_its_conversations(tmp_path):
    import sqlite3

    from cozysetup.database import MIGRATIONS, SCHEMA_V1, SCHEMA_VERSION, connect
    path = tmp_path / "cozysetup.db"
    db = sqlite3.connect(path)
    db.executescript(f"BEGIN; {SCHEMA_V1} PRAGMA user_version = 1; COMMIT;")
    for version in (2, 3, 4, 5):
        db.executescript(f"BEGIN; {MIGRATIONS[version]} PRAGMA user_version = {version}; COMMIT;")
    db.execute("INSERT INTO conversations (channel, channel_user_id, ai_paused, created_at, updated_at) "
               "VALUES ('instagram', ?, 1, 'x', 'x')", (CUSTOMER,))
    db.commit()
    db.close()

    db = connect(path)
    assert db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION == 6
    row = db.execute("SELECT * FROM conversations").fetchone()
    assert (row["channel_user_id"], row["ai_paused"], row["username"], row["profile_checked_at"]) == \
        (CUSTOMER, 1, None, None)
    assert db.execute("SELECT COUNT(*) FROM heartbeats").fetchone()[0] == 0
    db.close()
    backup = sqlite3.connect(tmp_path / "cozysetup.db.before-v6.bak")
    assert backup.execute("PRAGMA user_version").fetchone()[0] == 5
    backup.close()
