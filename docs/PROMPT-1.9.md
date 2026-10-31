# The perfect view — one voice, and the reins

> **What this is.** The brief for the build after 1.8, written from what he keeps
> saying and what keeps going wrong. It is short on purpose: three promises, the
> data each one needs, and a script that decides whether they hold.
>
> **How to use it.** Read §1. Then build §2, §3, §4 in that order. Every promise
> carries a test that runs; a promise that only reads well is not kept.

---

## 1. The picture

He sends one message. **One thing comes back.** Not a card and a sentence, not a
file and a paragraph, not the answer the rules made and then the answer a model
made. One bubble, with everything in it: her words, what she did, the picture she
drew. If she sent words *into* the room as the deed itself, that deed is the
bubble and she adds nothing.

That is the first half. The second half is the reins:

> *"In a group people can reply to her. I might tell her not to answer specific
> people — by name or id — or to heart their messages instead of answering."*

He wants to shape **who** she answers and **how**, per person, in his words, and
to see it written down where he can take it back.

And under both: **understanding**. A sentence he types is an instruction, not a
topic. If he can say it, she can do it — and if she cannot, she says so in one
line instead of answering around it.

---

## 2. One voice

**2.1 One bubble.** A turn produces at most one *message*. That message may carry
text, a receipt card, and a file — but it is one send. A file plus a sentence is a
file **with a caption**, not two messages.

**2.2 The deed is not repeated.** When an action already put words into the room
that gave the order (`send --here hi --repeat 3`), the room has its answer. She
does not add a receipt on top of it.

**2.3 One author.** On a turn where her hands ran, the sentence is hers and the
result is hers: the command's own text is *folded into* her bubble, never sent
beside it. This is already true for text commands; it must be true for files too.

**2.4 Nothing silent.** If the deed and the words are both empty, she still says
one line. A turn may be quiet only when the deed itself spoke.

*Where it lives:* `services/yura_chat.py` (composition), `services/self_console.py`
(capture, including media), `features/persona.py` (delivery).

---

## 3. The reins

**3.1 A rule per person.** A rule says what she does with one person's messages:

| mode     | what she does                                                   |
|----------|-----------------------------------------------------------------|
| `answer` | the default: answer them like anyone else                        |
| `ignore` | never answer — no model call, no words, no tokens                |
| `react`  | no answer; a reaction with their emoji, on the message they sent |

**3.2 Scope.** A rule may be global or tied to one chat (`--here`), so "in this
group, don't answer him" does not silence him everywhere. A chat rule beats a
global one; no rule means `answer`.

**3.3 Naming.** A rule is written with a name she knows (`sara`), a handle
(`@sara`), or an id (`483920`). The number must work on the first day, before the
person has ever written.

**3.4 Reversible, and visible.** `list` shows every rule with its mode and scope.
`clear` removes one. Nothing is hidden: `.why` names the rule that kept her quiet.

**3.5 Never guessed.** A rule is only set by a sentence that says so. "She didn't
answer Sara" is not "never answer Sara".

*Where it lives:* `migrations.py` (`reply_rules`), `services/storage.py`,
`features/reach.py` (`.rules`), `features/persona.py` (the three gates),
`services/intent.py` (the shapes).

---

## 4. Understanding

**4.1 The shapes he actually types**, in English and Persian, land on the same
three commands:

```
don't answer sara            → rules ignore sara
don't answer 483920          → rules ignore 483920
به سارا جواب نده             → rules ignore سارا
answer sara again            → rules answer sara
به سارا جواب بده             → rules answer سارا
react to sara with a heart   → rules react sara ❤️
جواب سارا نده، قلب بده       → rules react sara ❤️
don't answer him here        → rules ignore <the message's sender> --here
```

**4.2 When the sentence points at a message** (a reply), the person is the sender
of that message, not the word "him".

**4.3 A rule beats every other rule**, including the reply-to-her path, the
DM path, and the every-ninth-answer-is-a-reaction habit.

---

## 5. The script (definition of done)

Run each line through `scripts/try_message.py` (offline) and then live:

1. `یورا فال بگیر` → **one** message: the fortune image with her line as its caption.
2. `یورا say I am a good girl and repeat 3 times` → the deed's messages only
   (plus the two queued), no receipt on top.
3. `یورا به سارا جواب نده` → a card naming the rule; Sara's next message gets
   nothing, and `.why` says `ignored`.
4. `یورا به سارا قلب بده` → Sara's next message gets ❤️ and no words.
5. `یورا به سارا جواب بده` → Sara is normal again.
6. `.rules list` shows exactly the rules that exist, with scope.

`pytest -q` green. `scripts/audit_live.py` overall ok.
