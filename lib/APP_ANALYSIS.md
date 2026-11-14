# HomeVoice app review (September 2026)

This review covers the Flutter app's `lib/` folder (41 files, about 26.5k lines of Dart) and the hub code it talks to. **No Dart or Flutter toolchain was available** (pub.dev and the SDK are blocked here), so none of this was compiled or run. Every changed file passes a Dart syntax parser, and two separate compile-level read-throughs found no definite errors. Every contract with the hub was checked against the hub's source. **Treat the first `flutter build` as the final check.**

What changed: 33 app files (+1,272 / −739 lines), and several hub files (listed at the end).

---

## 1. Voice navigation (the main complaint)

Voice navigation works like this. The phone sends the spoken text with a `ui` context (the current screen, what is on it, and which actions are allowed). The hub's `nlu/app_nav.py` decides whether it is *navigation* or a *house command*. If it is navigation, the phone acts on the `ui` frame the hub sends back.

**Hub matcher.** It was tested on 223 realistic spoken phrases. Before the fix it got **39 wrong**: 8 house commands were taken as navigation, and 31 navigations were missed. **Now 0 are wrong.** The corpus is now a permanent test: `tests/test_app_nav.py`.

| Before | Now |
|---|---|
| "open the front door" opened the **camera feed** named Front Door instead of unlocking the door | Loose verbs only pick a camera if you say "camera" or "feed", or say "show/view" and no other device has that name |
| "please go back", "go back please", "can you open settings", "open scenes thanks" went to the house | Polite words and a repeated wake word are stripped first |
| "scroll down a bit / a little / more" dimmed a light ("down a little") | Recognised as scrolling |
| "open the front" (a clipped "front door") or "start" went to the home screen | Those aliases were removed |
| "press the garden button" opened the device's sheet | "press" never selects a device by name |
| A scene called "home" or "alerts" opened a screen instead of running the scene | Scene names and triggers are never treated as navigation |
| "go back to devices", "check notifications", "go to the top", "next page", "school down" (mis-transcribed "scroll"), "create a new automation", "make a new rule", "set up a new device" weren't understood | All understood |
| "go back" waited for the audio clip to be decoded | Navigation is checked before decoding |

**App side.**

- **Silent failures.** "Scroll down" or "select X" did nothing, with no feedback, when there was no list or a sheet was covering it. The screen now reports whether it handled the command, and the app says "Nothing to scroll here." or 'Open Devices to pick "X"'.
- **Popups in the way.** "Select porch light" did nothing while another device's sheet was open, and "open scenes" opened on top of the sheet. Dialogs and sheets now close first, as a tap outside them would.
- **"Go back" skipped unsaved-changes guards.** It bypassed the rule builder's "discard changes?" guard. It now goes through the same path as the back gesture.
- **"Open devices" while on Devices** wiped the list and lost your scroll position. It now says "Already here."
- **Leftover edit id.** A rule id left over from a refused tap could silently answer the spoken "edit a rule" question. It is now cleared.
- **Notices without a button** were offered to voice and did nothing when picked. Only actionable notices are offered now.
- **Boards couldn't be picked by voice.** They can now.
- **Settings and Profile** couldn't be scrolled by voice. Now they can.

---

## 2. Connection and protocol

- **High: the tailnet address could never connect.** The app checked `*.ts.net` addresses against the phone's system certificates, but the hub serves its own pinned certificate everywhere. Every such address failed after a 25-second timeout on every sweep. All addresses now use the pinned-key check.
- **High: the hub filled up with dead connections.** The server never times out idle sockets, so each Wi-Fi blip left a half-open connection until the 8-connection limit was hit ("too many connections"). **Hub fix:** a new session from the same phone now replaces its old ones. The app also retries on that refusal instead of giving up.
- **High: a dropped connection left the old question stuck on screen.** The app believed a flow was still running, so every action said "Finish what is on screen first". Any disconnect now clears the pending questions and flow screens.
- **High: tapping an action while disconnected jammed the app.** It set "busy" and nothing ever cleared it. It now refuses the tap while disconnected.
- **High: the app didn't reconnect when reopened.** With the wake word on (the default), it never redialled after coming back from the background. Fixed.
- **Medium:**
  - A revoked phone didn't get the "pair again" screen.
  - A hub that was full or throttling showed "Is this really the hub?".
  - A broken keystore crashed the handshake silently.
- **Medium: errors about one bad frame ended the whole flow.** The hub sends some errors (bad frame, audio too large) mid-flow and carries on, but the app tore everything down. They no longer do.
- **Medium: Tailscale sign-in could close other screens.** Finishing a keyless sign-in minutes later wiped whatever other question was on screen. Now it only closes its own.
- **Medium: a harmless refusal could cancel a running request.** The hub now says which request it refused (`action`), and the app only lowers that request's own flags.
- **Low: menu crashes looked like voice-command failures.** A crash in a menu action now shows the menu's own failure window.

---

## 3. Voice pipeline (listening, speaking, recording)

- **High: probable build error.** `listenFor`, `pauseFor` and `localeId` were passed to `SpeechListenOptions`, which doesn't take them in speech_to_text 6.3 to 7.x. They were moved to `listen()`.
- **High: the listening window closed while you were speaking.** Saying the wake word just after a reconnect: the background notices check finished and closed the window mid-sentence.
- **High: the wake word went dead until restart** if transcription or cleaning threw an error. It now returns to idle.
- **High: the phone heard itself.** A window opened right after the phone spoke caught its own last words. A choice prompt ending "…or cancel to stop" could cancel itself. There is now a 300 ms hold-off after speech.
- **High: overlapping speech cut sentences short.** A timed-out old sentence could cut off a newer one. Fixed.
- **Medium: long commands were thrown away.** Anyone still talking at 8 seconds lost the whole command as "heard nothing". It is now sent if speech was detected.
- **Medium: silence was sent as a voice sample.** An enrolment or challenge window that timed out sent silence (it counted bytes, not speech), so the voice check failed.
- **Medium: the flow got stuck on some screens.** A form, registration, rule builder or pin map arriving while "thinking" stuck the flow for 60 seconds.
- **Medium: spoken lines were cut off or talked over.**
  - Spoken warnings were cut after their first word by the result that followed them.
  - Speaking over an open window made it listen forever and record the phone's own voice.
- **Medium: voice answers went to the wrong question.**
  - A screen's voice list could steal the answer to the hub's own question.
  - A late voice pick answered twice.
  - Saying "cancel" to a question was stored as the answer (a device could be named "cancel").
- **Low:**
  - Forms were never introduced by name (the app read `title`; the hub sends `text`).
  - Two spoken lines played while the wake word could hear them.
- **Removed:** an unused on-screen mirror of the voice state, plus a duplicated audio conversion.

---

## 4. Screens

**Rule builder (automations)**

- **High:** a "flip" action reopened as "turn off", so the next Save changed what the rule did.
- **High:** a display line's hold ("leave it up" / for N minutes) was never saved, so a "leave it up" line silently became a cycling one. There is now a hold selector, and Save stays disabled until an incomplete hold is filled in.
- **Medium:**
  - ELSE and revert blocks couldn't be removed once added.
  - The threshold "falls below" side now uses the hub's `edge` field.
  - Editing a "changes by" trigger dropped the "by" value.
  - A missing device or scene in a drop-down crashed the screen in debug builds.

**Pin map**

- Several placements passed on the phone and were then refused by the hub at Save: a bus on an occupied pin, a second bus of the same kind, a pin used twice, moving a shared line onto a busy pin. All are now refused on the phone.
- GPIO16/17, which the module may keep for its own memory, are no longer offered. **Hub fix:** they are reported as reserved for new boards too.

**Choice screen**

- **High:** in a narrowed multi-select, the "None of those" row was sent together with real picks, so "All of them" never worked.

**Question (prompt) screen**

- **High:** an empty answer couldn't be sent. Many hub questions use empty to mean "skip" or "keep" ("(optional)", "Enter to skip", "Enter keeps…"), so the only way past them was Cancel, which abandoned the whole flow.

**Web and camera screens (security)**

- A sign-in page could navigate to plain `http://` on the internet. Now only in-house addresses may use http.
- Cookies are now cleared on every exit.

**Pairing (security)**

- A QR code with `/ @ ? #` in its host could make the phone dial somewhere other than what the address box showed. Such codes are now refused.
- The scanner only accepts real pairing codes.

**PIN entry**

- Half-typed digits carried over to the next PIN question.
- "Show digits" stayed on for the next question.
- Screen readers couldn't find the PIN field.

**Build error**

- `network_editor.dart` imported `'../../app_state.dart'`, a path that doesn't exist, so the app would not have built. Fixed.

**Other screens**

- Stale-answer protection now uses a question serial.
- Controllers were disposed while still in use.
- Statistics showed "unavailable" as a live reading.
- Notification buttons were enabled while busy.

**Removed:** dead text-table fallbacks in Devices and Scenes, and 13 unused AppState members (metrics, old enrolment helpers and others).

---

## 4b. Option selection and pin selection (second deep pass)

### Picking from a list (choices), by tap and by voice

The hub's answer matcher (`nlu/choice_match.py`) was rewritten. It was tested on 267 spoken answers against realistic lists. **Before:** 28 wrong picks and 51 missed. **Now:** 0 wrong. The corpus is a permanent test: `tests/test_choice_match.py`, 269 checks. The dangerous fixes:

| You said | Before | Now |
|---|---|---|
| "kitchen lights" (list also has "kitchen light" and "kitchen night light") | kitchen **light** | kitchen lights |
| "kitchen light" on a multi-select | **all 4** ticked | kitchen light |
| "all except the fan" | all 5, **fan included** | the other 4 |
| "everything except the porch light" | **only** the porch light | the other 4 |
| "anna" (list has Ann and Anna) | Ann | Anna |
| "the first and the third", "kitchen or hall" | only the first | both |
| "three" (list: Bulb 3, Bulb 2, Desk lamp) | Bulb 3 (guessed) | asks |
| "yes decline" | decline | nothing, so it asks again |
| "cancel", "never mind", "none", "neither" | could pick something | never picks anything |

Newly understood:
- "the last one", "the 2nd one"
- "bulb to" / "lamp for" (speech-to-text mishearings of the numbers)
- "um the kitchen light", "it's Ada", "my name is Emeka"
- "yeah", "sure", "nope", "no thanks" on yes/no lists
- "both", "the kitchen and the hall"

**App side**

- **High: ticked rows sent instead of the words.** Speaking on a multi-select with rows already ticked sent the ticked rows and **threw the words away**, then sent a second, stray answer.
- **High: the old list stayed live.** After a spoken answer, the list stayed tappable, so a tap could answer the hub's *next* question. It now closes like a tap does and shows "working" until the hub replies.
- **High: phone-side "all" check.** The phone sent every item for "all except the fan". The hub now reads "all" from the words.
- **Medium: "none" cancelled the whole flow.** Saying "none" to a "Did you mean…?" round cancelled everything. Now the hub shows the whole list again.
- **Medium: lists read aloud for questions already answered.** The app answers some questions silently, but the phone still read those lists out and opened the mic. They're now skipped.

**Hub side**

- A stale tap from a replaced list ended the flow. The question is now asked again.
- "None of those" no longer uses up one of the three tries.
- Padded yes/no answers ("yes please", "that's right") now work in "Did you mean…?" rounds.

### Placing devices on board pins (pin map), by tap and by voice

**Wrong pins by voice.**
- **High: "pin 26" could land on GPIO2.** When 26 was taken, spoken pin numbers went to the hub's fuzzy matcher, which scored "pin 26" at 0.91 against "pin 2" and put the device on GPIO2. The phone now reads pin numbers exactly.
- **What it understands:** "pin 26", "GPIO 26", "twenty-six", "two six", "D26", RX2/TX2/VP/VN, and "pin for" = 4 / "pin to" = 2 (speech-to-text mishearings).
- **Anything unclear is refused, never guessed:** two numbers at once, "pin twenty" (there is no GPIO20).
- **New commands:** "put a relay on pin 26", "remove the fan", "move the relay to 27".

**Voice stopped working on sheets and dialogs (high).**
- The screen re-armed its word list on every redraw, so voice broke on the device sheet, and the board stayed deaf after the sheet closed.
- A spoken pin at the name box could crash the screen.
- Every sheet and dialog now takes over the mic properly and hands it back when it closes.

**Multi-pin devices asked for a line twice (high).** Examples:
- An HC-SR04 started at its echo pin asked for echo again.
- A card reader joining an existing SPI bus asked for a bus line again, and the hub then dropped the card reader.

It now moves on to the next unfilled line.

**Devices silently dropped at Save (high).** The phone now refuses or holds these, so the hub never has to drop them:
- family limits (PWM 16, rotary 2, DHT 4, one card reader…), which the hub now sends in `describe()`
- GPIO16/17 while it's unknown whether the module uses them for its memory
- a device type that no longer exists in the catalogue
- a device with a missing line
- names containing `;`

The hub also refuses blank names and `;` from the phone. A new test (`tests/test_pin_map.py`, 119 checks, including 400 random boards) proves that whatever the phone allows the hub keeps, and whatever it refuses the hub refuses.

**Drawing and exit fixes.**
- GPIO0, 9 and 10 are drawn now, in a "not on a 30-pin header" row.
- The "pins free" count was wrong; fixed.
- A newer board frame replaces the whole screen.
- A double tap on X can no longer close the screen underneath.

## 5. Hub changes made for the app

| File | Change |
|---|---|
| `nlu/app_nav.py`, `app/ws_server.py` | The navigation matcher fixes above. Scene names are passed in. Navigation is checked before audio decode. |
| `app/ws_server.py` | A new session from the same phone replaces its dead ones. Refusals name the refused request (`action`). Menu crashes carry `menu:<action>`. |
| `app/shell.py` | **Cancel now means cancel:** a cancelled question raises a cancel instead of returning an empty answer. Before, empty was read as "skip / finish", so Cancel could leave a Wi-Fi network open, count the recovery words as acknowledged, and so on. |
| `app/shell.py` | The recovery phrase is asked as a secret field. The show-on-home toggle flips what the card actually shows. Scene rows are no longer printed to phones. |
| `nlu/automations.py` | Flip actions are described as "flip X", not "turn X on". |
| `esp_firmware/esp_builder.py` | GPIO16/17 are reported as reserved when the module is unknown or keeps them. |
| `tests/test_app_nav.py` (new) | The 223-phrase voice-navigation corpus. |
| `nlu/choice_match.py`, `app/shell.py` (`choose`) | The answer matcher was rewritten, plus the choice-round fixes above. `tests/test_choice_match.py` is new. |
| `esp_firmware/esphome_catalog.py`, `esp_builder.py` | `describe()` sends family limits. Bus sharing compares component codes, so custom I²C drivers join the built-in bus. Names from the phone are validated. `tests/test_pin_map.py` is new. |

---

## 6. Before you build: check these

1. **mobile_scanner version.** `scan_screen.dart` uses the API from roughly version 6 or later. The old changelog said to pin `^3.5.7`/`^5.2.3`, and those **will not compile**. Use a current version, or tell me which one you pin.
2. **speech_to_text.** Needs 6.3 or later (for `SpeechListenOptions`).
3. **webview_flutter.** Needs 4.4 or later.
4. **Flutter.** Needs 3.24 or later (`PopScope.onPopInvokedWithResult`) and 3.27 or later (`Color.withValues`).
5. **Deploy the hub and app together.** The hub now names refused requests, raises on cancel and replaces sessions. The app handles all of these and also works with an older hub, but older apps don't know the new behaviour.

## 7. Open items

- **A voice command right after connecting can be refused as "busy".** The phone's own background notices check holds the hub for a moment. It is rare; say it again.
- **Transcription runs on the UI thread.** It may freeze the screen for about a second after each command. Moving it off the UI thread is a bigger change.
- **The noise-removal model is never created.** It is wired up but not passed in, so it never runs.
- **The hub doesn't send device limits to the pin map** (PWM 16, rotary 2, DHT 4). The phone can't warn you in advance; the hub drops extras at Save.
- **A rule the hub refuses closes the builder.** It should reopen with your blocks kept.
