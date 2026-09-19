"""
Tests for telegram_poller.py.

This is the one component that accepts input from outside the VM and the one
that can restart the scanner, so the tests that matter are the refusals:

  - a message from any other chat is ignored, and gets NO reply
  - anything that is not /login does nothing
  - a rejected token never restarts anything
  - a token the scanner cannot read back never restarts anything

Nothing here touches the network, Kite, or systemd — every boundary is patched.
"""

import pytest

import telegram_poller as tp


@pytest.fixture
def wired(monkeypatch):
    """Patch every boundary: Telegram, Kite, systemd. Returns a call log."""
    calls = {"sent": [], "deleted": [], "login": [], "restart": 0,
             "cached": True, "login_raises": None}

    monkeypatch.setattr(tp.config, "TELEGRAM_BOT_TOKEN", "bot-token")
    monkeypatch.setattr(tp.config, "TELEGRAM_CHAT_ID", "12345")
    monkeypatch.setattr(tp.config, "KITE_API_KEY", "k")
    monkeypatch.setattr(tp.config, "KITE_API_SECRET", "s")

    monkeypatch.setattr(tp, "_reply", lambda text: calls["sent"].append(text))
    monkeypatch.setattr(tp, "_delete", lambda mid: calls["deleted"].append(mid))

    def fake_login(api_key, api_secret, request_token):
        calls["login"].append(request_token)
        if calls["login_raises"]:
            raise calls["login_raises"]

    def fake_cached(api_key):
        return object() if calls["cached"] else None

    def fake_restart():
        calls["restart"] += 1
        return True

    monkeypatch.setattr(tp, "complete_login", fake_login)
    monkeypatch.setattr(tp, "try_cached_session", fake_cached)
    monkeypatch.setattr(tp, "_restart_scanner", fake_restart)
    return calls


def update(text="/login abc123", chat_id="12345", message_id=7):
    return {"update_id": 1,
            "message": {"message_id": message_id, "chat": {"id": chat_id},
                        "text": text}}


# ------------------------------------------------------------- the refusals

def test_a_message_from_another_chat_is_silently_ignored(wired):
    """Silence, not an error. A reply confirms to an unknown sender that
    something is here and listening."""
    tp._handle_update(update(chat_id="99999"))
    assert wired["sent"] == []
    assert wired["login"] == [] and wired["restart"] == 0
    assert wired["deleted"] == []


def test_a_numeric_chat_id_still_matches(wired):
    """Telegram sends chat.id as an int; config carries a string."""
    tp._handle_update(update(chat_id=12345))
    assert wired["login"] == ["abc123"]


def test_non_login_text_does_nothing(wired):
    for text in ("hello", "/status", "/restart", "/help", ""):
        tp._handle_update(update(text=text))
    assert wired["sent"] == [] and wired["login"] == []
    assert wired["restart"] == 0


def test_a_rejected_token_never_restarts(wired):
    wired["login_raises"] = RuntimeError("Invalid `request_token`")
    tp._handle_update(update())
    assert wired["restart"] == 0
    assert wired["sent"][0].startswith("❌")
    assert "single-use" in wired["sent"][0]


def test_an_unreadable_cache_never_restarts(wired):
    """Kite accepting the token is not the scanner being able to load it. This
    is the check that stops a restart into a silently unauthenticated scanner."""
    wired["cached"] = False
    tp._handle_update(update())
    assert wired["restart"] == 0
    assert "cannot read it back" in wired["sent"][0]


def test_a_bare_login_explains_itself(wired):
    tp._handle_update(update(text="/login"))
    assert wired["login"] == [] and wired["restart"] == 0
    assert "Usage" in wired["sent"][0]


# ------------------------------------------------------------- the happy path

def test_a_valid_login_exchanges_restarts_and_confirms(wired):
    tp._handle_update(update())
    assert wired["login"] == ["abc123"]
    assert wired["restart"] == 1
    assert wired["sent"] == ["✅ Logged in — scanner restarted and running."]


def test_the_request_token_is_deleted_from_the_chat(wired):
    tp._handle_update(update(message_id=42))
    assert wired["deleted"] == [42]


def test_the_message_is_deleted_even_when_the_exchange_fails(wired):
    """The token is spent either way; it should not sit in the chat."""
    wired["login_raises"] = RuntimeError("nope")
    tp._handle_update(update(message_id=42))
    assert wired["deleted"] == [42]


def test_a_scanner_that_does_not_come_up_is_reported(wired, monkeypatch):
    """Replying 'logged in' while the unit sits failed would recreate the exact
    silence this feature exists to end."""
    monkeypatch.setattr(tp, "_restart_scanner", lambda: False)
    tp._handle_update(update())
    assert wired["sent"][0].startswith("⚠️")
    assert "did not come up" in wired["sent"][0]


# --------------------------------------------------------------- robustness

def test_an_update_with_no_message_is_ignored(wired):
    for payload in ({"update_id": 1}, {"update_id": 2, "message": None},
                    {"update_id": 3, "message": {"chat": {"id": "12345"}}}):
        tp._handle_update(payload)
    assert wired["login"] == [] and wired["restart"] == 0


def test_an_edited_message_is_handled_like_a_new_one(wired):
    """Editing a message to add the token is an easy mistake to make on a
    phone; it arrives as edited_message and would otherwise vanish."""
    tp._handle_update({"update_id": 1,
                       "message": None,
                       "edited_message": {"message_id": 3,
                                          "chat": {"id": "12345"},
                                          "text": "/login xyz"}})
    assert wired["login"] == ["xyz"]


# ------------------------------------------------- the .env corruption case

def test_a_corrupted_chat_id_is_rejected_not_normalised():
    """REGRESSION, found in production. The VM's .env held a TELEGRAM_CHAT_ID
    with a heredoc terminator concatenated onto the end (e.g.
    123456789EOF). Telegram is lenient (it parsed the leading integer, so
    outbound alerts were unaffected), which is why nobody noticed."""
    assert tp.validated_chat_id("123456789EOF") is None
    assert tp.validated_chat_id("123456789ZZZ") is None
    assert tp.validated_chat_id("") is None

    # Whitespace is a typo, not corruption — trim it.
    assert tp.validated_chat_id(" 123456789" + chr(10)) == "123456789"
    assert tp.validated_chat_id(123456789) == "123456789"
    # Groups and supergroups are negative.
    assert tp.validated_chat_id("-1001234567890") == "-1001234567890"


def test_a_corrupted_chat_id_blocks_every_message(wired, monkeypatch):
    """It must not fall back to matching loosely. A fuzzy allowlist is the wrong
    direction for the one check between a stranger and a scanner restart."""
    monkeypatch.setattr(tp.config, "TELEGRAM_CHAT_ID", "123456789EOF")
    tp._handle_update(update(chat_id="123456789"))
    assert wired["login"] == [] and wired["restart"] == 0
