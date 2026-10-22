# Messaging: fixed conversation layout and read receipts (plan)

Status: decided. The owner accepted every recommendation in section 4 on
22 October 2026: two ticks in a group or broadcast only when everyone who
could see the message has read it, with "Read by n of m" for the sender; a
whole-school setting `messages.read_receipts`, default on, all or nothing; a
new read-only Field Access switch, Read receipts; no ticks for a watching
parent or an inspector.
Applies to school-fe (staff), parent-fe (parents) and student-fe (pupils), and
to `schools.vs_messages` in the backend.

## 1. What is asked

1. **Layout.** Inside a conversation, the person you are talking to stays
   fixed at the top, the message box stays fixed at the bottom, and only the
   messages scroll between them, as in WhatsApp.
2. **Read receipts.** On your own messages, one tick means sent, two ticks
   mean read. In all three apps.

## 2. What is there today

- **parent-fe and student-fe:** the whole page scrolls. The composer is sticky
  above the floating tab bar, but the header (name, role, child) is part of
  the page and scrolls away with older messages. The tab bar stays visible
  inside a conversation and takes space from the messages.
- **school-fe:** a conversation pane beside the inbox from `lg` up, one pane
  with a Back button below that. Its header and composer are not pinned on a
  phone either.
- **Backend:** every participant already carries a read watermark
  (`Participant.last_read_message_id`, `last_read_at`) that only moves
  forward, written by `POST .../read/`. "Has Kunle read message 903?" is
  "is Kunle's watermark at least 903". No new table is needed. Inspectors and
  a parent watching a pupil conversation never move a watermark, so they can
  never turn a tick blue.

## 3. Plan

### Phase 1. Fixed conversation layout (frontend only, all three apps)

- The open conversation becomes a full-height column: header, message list,
  composer. Only the message list scrolls (its own scroll area); the header and
  composer never move.
- Height follows the visible screen (`100dvh`, and `visualViewport` so the
  composer rides above the phone keyboard instead of being covered by it),
  with the safe-area insets for phones with a home bar.
- On a phone the floating tab bar is hidden while a conversation is open, as
  WhatsApp hides its tabs. Back returns to the list and the tab bar.
- Opening a conversation lands on the newest message. A new message scrolls
  into view only when the reader is already at the bottom; otherwise a
  "New messages" pill appears and jumps down when tapped. Loading older
  messages at the top keeps the reader's place.
- school-fe: the same column inside the conversation pane on desktop and
  tablet, and full screen on a phone.
- Checks: each app's tests, type check, lint and build, the 375 / 768 / desktop
  overflow probe, and phone screenshots with a long conversation and with the
  keyboard open.

### Phase 2. Receipts in the backend (`schools.vs_messages`)

- Each message the caller sent gains `receipt`:
  `{"state": "sent" | "read", "read_count": n, "recipient_count": n}`.
  Messages from others carry no receipt.
- **Direct conversations** (staff, parent, pupil): `read` once the other
  person's watermark has reached the message.
- **Groups and broadcasts:** counted over the people who could see that
  message (open members whose join point is before it, the sender excluded);
  `read` when all of them have read it (question 1). `read_count` and
  `recipient_count` give "Read by 12 of 40" to the sender only.
- **Cost:** one extra query per message page (the conversation's open
  watermarks), compared in memory, so a page of 50 messages in a group of 40
  costs the same as a direct conversation. No migration.
- **Seeing a tick change:** a cheap `GET .../messages/?after=<id>` returns new
  messages and the receipts of the caller's recent messages, polled every
  10 seconds while a conversation is open and the app is in front, and once
  on returning to it. No sockets.
- **Who sees receipts:** the sender only, on their own messages. Never an
  inspector, never a watching parent (question 4), never in exports or alerts.
- **School setting** (question 2), **Field Access** (question 3), tests for
  every kind of conversation, the later-join rule, a removed member, a
  deactivated reader, inspection not marking read, the setting off, and each
  Field Access state.

### Phase 3. Ticks in the three apps

- On your own message: a small clock while it is sending, one grey tick once
  the server has stored it, two ticks (coloured) once read, a red "Not sent,
  tap to retry" when it fails (the draft and its client token are kept, as
  today).
- Groups and broadcasts: tapping your message shows "Read by 12 of 40".
- When receipts are off for the school or for the reader's role, no ticks are
  drawn at all, rather than ticks that never turn.
- Tests per app for each state, plus the polling stopping when the app is in
  the background.

### Phase 4. Documents and guides

- Queue the M32 FRD and MRD revisions in `todo.md` (Documents owed).
- Update `docs/messaging/messaging-design.md` section 14, the staff Messages
  guide and the Field Access guide, and the Field Access ledger.

## 4. Questions for the owner

1. **Groups and broadcasts: when do the ticks turn?** Recommended: two ticks
   only when everyone who could see the message has read it, and the sender
   taps to see "Read by 12 of 40".
2. **Who may switch receipts off?** Recommended: a school setting
   `messages.read_receipts`, default on, all or nothing, so nobody sees
   receipts while it is off (as in WhatsApp, where turning yours off also
   stops you seeing others').
3. **Field Access.** Recommended: a new read-only switch, Read receipts, on
   `school.messages`, separate from Read status and unread counts.
4. **A parent watching a child's conversation, and an inspector.** Recommended:
   no ticks for either. They read a conversation they are not part of.

## 5. Order

Phase 1 needs no answers and can start at once. Phases 2 and 3 wait for the
answers to section 4, then go backend first. Phase 4 follows each phase.
