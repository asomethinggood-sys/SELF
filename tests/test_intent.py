"""The sentence itself is an instruction — 1.7's floor under her hands.

1.6 could only act when the model happened to write a ``tool`` block. On the live
account it often did not, so "turn off my live clock" got a paragraph and the
clock stayed on. That is the bug these tests exist for: with a model that says
nothing but words, the account still does what it was told — through the same
console the typed command uses, so there is one implementation, one reply and one
audit trail.

The other half is restraint: a conversation must stay a conversation. Every
"None" below is as important as every "clock off".
"""

from __future__ import annotations

import pytest

from havoc.services.intent import detect

FEATURES = {"clock", "poetry", "roasts", "persona", "away", "backups", "triggers"}
COMMANDS = {
    "clock", "poetry", "fal", "fight", "joke", "poem", "quote", "coin", "random",
    "remind", "note", "translate", "search", "help", "status", "about", "reminders",
}


def _lines(text: str) -> tuple[str, ...]:
    found = detect(text, features=FEATURES, commands=COMMANDS)
    return found.lines if found else ()


# ---------------------------------------------------------------------------
# His own sentences
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "text",
    (
        "yura turn off my live clock on my profile",
        "turn off the live clock",
        "switch the profile clock off please",
        "ساعت رو خاموش کن",
        "یورا ساعت زنده رو خاموش کن",
        "clock off",
    ),
)
def test_turning_the_clock_off_is_understood(text: str) -> None:
    assert _lines(text) == ("clock off",)


def test_turning_the_clock_on_is_understood() -> None:
    assert _lines("turn my clock back on") == ("clock on",)


def test_a_question_about_the_clock_is_not_an_order() -> None:
    # "why is it there?" is talk; it must not switch anything.
    assert _lines("why is the clock always in my profile?") == ()
    assert _lines("do you like the clock on my profile?") == ()


def test_asking_for_the_clock_status_is_understood() -> None:
    assert _lines("what is the clock status") == ("clock status",)
    assert _lines("ساعت روشنه؟") == ("clock status",)


def test_two_things_in_one_breath_are_two_things() -> None:
    """"turn off fal and poetry" did half the job in the first draft."""
    assert set(_lines("leena turn off fal and poetry")) == {"disable fal", "feature poetry off"}
    assert set(_lines("فال و شعر رو خاموش کن")) == {"disable fal", "feature poetry off"}


def test_a_feature_switch_goes_through_the_feature_switch() -> None:
    assert _lines("disable roasts") == ("feature roasts off",)
    assert _lines("turn poetry on") == ("feature poetry on",)


def test_a_command_with_no_feature_is_switched_off_by_name() -> None:
    # `fal` and `fight` are commands, not features: `.disable` is their switch.
    assert _lines("turn off fal") == ("disable fal",)
    assert _lines("enable fal") == ("enable fal",)


# ---------------------------------------------------------------------------
# Time
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("text", "expected"),
    (
        ("set a timer in one hour", ("remind 1h timer",)),
        ("remind me to drink water in 35 minutes", ("remind 35m drink water",)),
        ("remind me about the bridge in 2 hours", ("remind 2h the bridge",)),
        ("remind me in 10 minutes", ("remind 10m timer",)),
        ("یادم بنداز تا یک ساعت دیگه آب بخورم", ("remind 1h آب بخورم",)),
        ("یادم بنداز ۲۰ دقیقه دیگه زنگ بزنم", ("remind 20m زنگ بزنم",)),
    ),
)
def test_a_reminder_keeps_its_subject(text: str, expected: tuple[str, ...]) -> None:
    assert _lines(text) == expected


# ---------------------------------------------------------------------------
# The other shapes
# ---------------------------------------------------------------------------
def test_a_note_is_remembered() -> None:
    assert _lines("remember that the bridge is north") == ("note add the bridge is north",)
    assert _lines("یادت باشه کلید زیر گلدونه") == ("note add کلید زیر گلدونه",)


def test_a_translation_names_the_language() -> None:
    assert _lines("translate good morning to german") == ("translate de good morning",)
    assert _lines("صبح بخیر رو به انگلیسی ترجمه کن") == ("translate en صبح بخیر",)


@pytest.mark.parametrize(
    ("text", "line"),
    (
        # ``.abilities`` reads the live registry; ``.help`` is the static list.
        ("what can you do", "abilities"),
        ("who are you", "about"),
        ("show me my reminders", "reminders"),
        ("tell me a joke", "joke"),
        ("who is in my contacts", "contacts"),
        ("who did i block", "blocklist"),
        ("what is in my saved messages", "diary"),
        ("leave this group", "leave"),
    ),
)
def test_a_lookup_that_asks_for_something_is_understood(text: str, line: str) -> None:
    assert _lines(text) == (line,)


def test_topics_are_understood() -> None:
    assert _lines("show the topics") == ("topics",)
    assert _lines("start a new topic called Night Shift") == ("topic new Night Shift",)


def test_peeking_and_wiping_are_understood() -> None:
    assert _lines("what is in this invite https://t.me/+AbCdEf") == ("peek https://t.me/+AbCdEf",)
    assert _lines("wipe this conversation") == ("clearhistory",)
    assert _lines("clear the history with @sara") == ("clearhistory @sara",)


def test_a_draft_and_the_archive_are_understood() -> None:
    assert _lines("write a draft to @sara saying running late") == ("draft @sara running late",)
    assert _lines("leave a draft for @sara: I will call you") == ("draft @sara I will call you",)
    assert _lines("archive @noise") == ("archive @noise",)
    assert _lines("unarchive @noise") == ("unarchive @noise",)


def test_renaming_and_slow_mode_carry_their_arguments() -> None:
    assert _lines("rename this group to Night Shift") == ("settitle Night Shift",)
    assert _lines("slow mode 5m") == ("slowmode 5m",)
    assert _lines("slowmode off") == ("slowmode off",)


def test_a_search_stays_a_search() -> None:
    assert _lines("search for the bridge in this chat") == ("search the bridge",)


# ---------------------------------------------------------------------------
# Restraint
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "text",
    (
        "hey, how was your day?",
        "i had a long day",
        "why do you always answer in two paragraphs",
        "do you like the clock on my profile?",
        "find it funny, don't you",
        "I find it funny",
        "what do you think about poetry?",
        "امروز چطوری؟",
        "",
        # Keyword-anchored matching opened the `.about` card in the middle of a
        # sentence; every lookup rule is anchored at the start now.
        "i was thinking about you",
        "i wrote a poem today",
        "can you show me the fal",
        "what do you think about poetry?",
    ),
)
def test_conversation_is_not_an_instruction(text: str) -> None:
    assert detect(text, features=FEATURES, commands=COMMANDS) is None


def test_a_very_long_message_is_not_read_as_a_command() -> None:
    assert detect("turn off " + "very " * 200, features=FEATURES, commands=COMMANDS) is None


# ---------------------------------------------------------------------------
# End to end: a model that only writes words
# ---------------------------------------------------------------------------
class WordsOnly:
    """What the live free model actually does: a paragraph, no tool block."""

    model = "stub"
    available = True

    def __init__(self, reply: str = "باشه دَدّا ✦") -> None:
        self.reply = reply
        self.calls: list = []

    async def converse(self, messages, *, max_tokens: int = 400, temperature: float = 0.8):
        self.calls.append(messages)
        return self.reply

    async def converse_tools(self, messages, *, tools, max_tokens: int = 400):
        self.calls.append(messages)
        return self.reply, []


async def test_the_clock_goes_off_even_when_the_model_only_talks(runtime) -> None:
    runtime.ai = WordsOnly()
    await runtime.feature_manager.set_enabled("clock", True)
    answer = await runtime.yura_chat.answer(
        "turn off my live clock on my profile", chat_id=100, is_owner=True
    )
    assert await runtime.store.get_setting("clock.enabled", "unset") is False
    assert answer.rendered is True
    assert "clock off" in answer.text
    assert "```" not in answer.text and '"tool"' not in answer.text


async def test_a_timer_is_set_even_when_the_model_only_talks(runtime) -> None:
    runtime.ai = WordsOnly()
    await runtime.yura_chat.answer("set a timer in one hour", chat_id=100, is_owner=True)
    reminders = await runtime.store.list_reminders(status="pending", limit=5)
    assert len(reminders) == 1


async def test_a_stranger_gets_no_hands(runtime) -> None:
    """Talking is free; doing is the owner's."""
    runtime.ai = WordsOnly()
    await runtime.yura_chat.answer(
        "turn off my live clock on my profile", chat_id=100, is_owner=False
    )
    assert await runtime.store.get_setting("clock.enabled", "unset") == "unset"


async def test_an_ordinary_sentence_changes_nothing(runtime) -> None:
    runtime.ai = WordsOnly("just a plain answer ╰(*´︶`*)╯")
    answer = await runtime.yura_chat.answer("how was your day?", chat_id=100, is_owner=True)
    assert answer.rendered is False
    assert "◈" not in answer.text


async def test_the_model_running_the_tool_is_not_run_twice(runtime) -> None:
    """When the block worked, the sentence reader stays out of the way."""
    from havoc.services.yura_chat import YuraChat  # noqa: F401 - the real class

    class BlockThenText:
        model = "stub"
        available = True

        def __init__(self) -> None:
            self.n = 0

        async def converse(self, messages, *, max_tokens: int = 400, temperature: float = 0.8):
            return "done"

        async def converse_tools(self, messages, *, tools, max_tokens: int = 400):
            self.n += 1
            if self.n == 1:
                return (
                    '```tool\n{"tool": "command", "args": {"command": "clock", "args": "off"}}\n```',
                    [],
                )
            return "تمومه", []

    runtime.ai = BlockThenText()
    await runtime.feature_manager.set_enabled("clock", True)
    await runtime.yura_chat.answer("turn off the live clock", chat_id=100, is_owner=True)
    reminders = await runtime.store.list_reminders(status="pending", limit=5)
    assert reminders == []
    assert await runtime.store.get_setting("clock.enabled", "unset") is False


# ---------------------------------------------------------------------------
# The small, unambiguous asks. Each of these used to be answered with a
# sentence — a paragraph of "the time here is…" — and nothing happened.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("sentence", "expected"),
    [
        ("what time is it", "time"),
        ("what's the time in Tehran?", "time tehran"),
        ("what time is it in tokyo", "time tokyo"),
        ("ساعت چنده", "time"),
        ("what's today's date", "time"),
        ("flip a coin", "coin"),
        ("toss a coin for me", "coin"),
        ("شیر یا خط", "coin"),
        ("random number", "random"),
        ("give me a random number between 1 and 10", "random 1 10"),
        ("what's 12 times 8", "calc 12 * 8"),
        ("what is 3 plus 4", "calc 3 + 4"),
        ("calculate 45 / 9", "calc 45 / 9"),
        ("make a backup", "backup export"),
        ("back up the database", "backup export"),
        ("run a selfcheck", "selfcheck"),
        ("check yourself", "selfcheck"),
        # 1.8: the richer card — connection, scheduler, model, queue, last error.\n        ("are you ok", "diagnose"),
        ("ping", "ping"),
        ("how fast are you", "ping"),
        ("what language is this", "detect"),
        ("این چه زبانیه", "detect"),
        ("tell me a poem", "poem"),
        ("read me a hafez poem", "poem"),
        ("what's on my agenda", "agenda today"),
        ("what's on my agenda this week", "agenda week"),
        ("what do i have scheduled", "agenda today"),
        ("show my bookmarks", "bookmark list"),
        ("summarize this", "summarize"),
        ("summarize the last 20 messages", "summarize 20"),
        ("tldr", "summarize"),
        ("what version are you", "version"),
        ("ورژن چیه", "about"),
    ],
)
def test_everyday_asks_become_actions(sentence: str, expected: str) -> None:
    intent = detect(sentence)
    assert intent is not None, f"{sentence!r} was answered instead of acted on"
    assert intent.lines[0] == expected


@pytest.mark.parametrize(
    "sentence",
    [
        "the time in that poem about was beautiful",
        "i love that poem you sent yesterday",
        "the time we spent together was nice",
        "you are so random sometimes",
        "tell me about your day",
        "can you back me up on this",
        "i read a poem yesterday",
        "do you like the clock on my profile?",
        "شعر قشنگی بود",
    ],
)
def test_a_sentence_that_merely_mentions_the_words_is_still_conversation(sentence: str) -> None:
    assert detect(sentence) is None


async def test_a_coin_flip_is_a_flip_not_a_paragraph(runtime) -> None:
    ai = WordsOnly("heads or tails, darling ✦")
    runtime.ai = ai
    answer = await runtime.yura_chat.answer("flip a coin", chat_id=100, is_owner=True)
    assert "Heads" in answer.text or "Tails" in answer.text
    assert answer.text != ai.reply
    assert answer.rendered is True


async def test_asking_the_time_answers_with_the_time(runtime) -> None:
    runtime.ai = WordsOnly("it is always now ✦")
    answer = await runtime.yura_chat.answer("what time is it", chat_id=100, is_owner=True)
    assert "◷" in answer.text or "UTC" in answer.text
    assert '```' not in answer.text


#: Forty-five things he might say that are not instructions. Every one of these
#: used to be — and must stay — conversation: she answers them in her own voice.
CONVERSATION_CORPUS = (
    "i missed you today",
    "you were quiet all morning",
    "that is a nice name",
    "i can't sleep",
    "good night yura",
    "do you remember the poem i sent",
    "what do you think of me",
    "i am tired",
    "tell me something you never told anyone",
    "why do you always answer like that",
    "i bought a new phone",
    "do you ever get bored",
    "my day was long",
    "someone was rude to me in the group",
    "this song reminds me of you",
    "i am reading a book about time travel",
    "the time will come",
    "i wish you were here",
    "you are beautiful",
    "say something in russian",
    "what is your favourite colour",
    "how old are you",
    "do you dream",
    "are you happy",
    "i am working late",
    "my brother is visiting",
    "we should meet",
    "the weather is awful",
    "can you keep a secret",
    "stop being dramatic",
    "that hurts",
    "i need a hug",
    "شب بخیر",
    "دلم برات تنگ شده",
    "امروز خیلی خسته بودم",
    "چی فکر میکنی",
    "تو خیلی مهربونی",
    "بگو یه چیزی",
    "من عاشق شعرَم",
    "ساعت‌ها دیر میگذره",
    "بریم بخوابیم",
    "منو دوست داری",
)


@pytest.mark.parametrize("sentence", CONVERSATION_CORPUS)
def test_plain_conversation_is_never_turned_into_a_command(sentence: str) -> None:
    assert detect(sentence) is None, f"{sentence!r} would have stolen her answer"


# ---------------------------------------------------------------------------
# The requests in his screenshots: a rule for his inbox, a name, a bio.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("sentence", "expected"),
    [
        (
            "Set an auto reply to every private chat. If anyone messages me say he's not here lil bro",
            "autoreply add --type any --private => he's not here lil bro",
        ),
        ("if anyone messages me say he's not here lil bro", "autoreply add --type any --private => he's not here lil bro"),
        ("if anyone dms me, tell them I'm busy", "autoreply add --type any --private => i'm busy"),
        ("set an autoreply for DMs saying I'll be back at 9", "autoreply add --type any --private => i'll be back at 9"),
        ("reply to every private chat with he is gone", "autoreply add --type any --private => he is gone"),
        ("set an auto reply saying brb", "autoreply add --type any --any => brb"),
        ("change my profile name to him", "profile name him"),
        ("change my name to Yura", "profile name yura"),
        ("set my bio to wandering soul", "bio set wandering soul"),
        ("i'm away", "away on I am away right now"),
        ("i'm back", "away off"),
    ],
)
def test_a_request_about_her_behaviour_becomes_a_console_line(sentence: str, expected: str) -> None:
    intent = detect(sentence)
    assert intent is not None, f"{sentence!r} was answered instead of acted on"
    assert intent.lines[0] == expected


def test_an_auto_reply_is_switched_on_with_the_rule() -> None:
    """"Set an auto reply" includes turning the feature on — a stored rule with the
    feature off is a rule that never speaks."""
    intent = detect("if anyone messages me say brb")
    assert intent is not None
    assert intent.lines == ("autoreply add --type any --private => brb", "autoreply on")


@pytest.mark.parametrize(
    "sentence",
    [
        "i set an autoreply yesterday lol",
        "my name is Sina",
        "what do you think of my name",
        "i'm not sure about that",
        "i was away for a week",
        "do you like my bio",
    ],
)
def test_a_sentence_about_things_is_not_an_order_about_them(sentence: str) -> None:
    assert detect(sentence) is None
