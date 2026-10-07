# LED Poster web app: UI references from Mobbin

Covers spec section "Web app" (`docs/spec.md`). The app is a mobile-first PWA, so all searches used the iOS platform. Every reference links to the Mobbin screen. Nanoleaf, Govee and LIFX are not on Mobbin, so Philips Hue, Alexa, IKEA, Dyson, Hatch and Sonos stand in for them.

---

## 1. Home / device control

**Layout (top to bottom)**
- Header row: device name ("LED Poster", tappable to rename) on the left, a connection dot and a settings gear on the right. Hue puts the room name and the power toggle together in a colored header bar. Copy that: put the **power switch in the header row** so it is always reachable without scrolling.
- **Live preview hero**: a 2:3 canvas, about 60-65% of the viewport width, centred on pure black. This is the "device image" slot that IKEA (bulb with halo) and Alexa (glowing power orb) use. We draw the real wall in it instead of a picture of a product. Add a small dot/flat toggle in the canvas corner.
- Status strip directly under the canvas, in one line of monospace chips: `Rainbow · 15 fps · 2.1 A · limiter`. Show the limiter chip in amber only when the limiter is active.
- Brightness: a full-width slider with the label on the left and the value on the right ("Brightness  64%"), like Alexa and Hatch. Auto-brightness goes in a toggle row right under it. When auto is on, grey out the slider and show a "set by sensor" caption. Do not hide the slider.
- Current mode card: thumbnail + mode name + chevron. It opens the Mode picker (screen 2). Alexa's "Colour: Warm White >" row works the same way.
- Bottom tab bar (Hue style, 4-5 tabs): **Home, Modes, Library, Playlist, More** (Schedule and Settings go under More).

**Copy:** the glow behind the preview. Alexa's power orb glows orange, and IKEA's bulb has a soft halo in the light colour. Tint a soft blurred glow behind the canvas with the average frame colour.
**Avoid:** Alexa's huge power button as the hero. The preview is the hero, and power goes in the header. IKEA's vertical tick slider is too hard to read for an exact percentage.

**References used**
- Philips Hue, room control with header toggle + scenes: https://mobbin.com/screens/336dcbcf-d967-4550-8142-5dc314ac1c57
- Amazon Alexa, light device sheet (power, brightness, colour row): https://mobbin.com/screens/c46d5090-0323-453f-92e7-dce7b3b9c71d
- IKEA Home smart, single light with halo + presets: https://mobbin.com/screens/fef54acd-4816-44d7-b987-141018058119
- Hatch Sleep, labelled brightness sliders in cards: https://mobbin.com/screens/e3d60776-066b-4bc1-bfe1-ab6dd27b63e6

---

## 2. Mode picker + per-mode settings

**Layout**
- A 2-column grid of mode cards (Hue "Effects"). Each card has a looping animated thumbnail rendered at wall aspect, the mode name bottom-left, and a play/stop button bottom-right. The active card gets a tinted fill and a stop icon. Hue marks the active "Prism" card with a pink fill and a square stop icon.
- Tapping a card applies the mode right away. Tapping it again, or the "..." button, opens a **bottom sheet** of settings for that mode.
- Settings sheet (Mercedes ambient lighting): grouped sections with small-caps headers (COLOUR, MOTION, TEXT).
  - Colours: a horizontal row of swatch chips (Fi colour sheet) + a "custom" chip that opens a picker. Mercedes uses horizontally scrolling palette tiles for multi-colour presets.
  - Speed: a slider with Slow/Fast end labels (Mercedes "Off ... Max").
  - Text (marquee modes): a single-line input with a live character count.
- A pinned mini-preview at the top of the sheet, so you can see the result while changing values. Riveo puts the canvas above its parameter rows.
- The sheet has no Save button; changes apply live. Add "Reset to defaults" at the bottom.

**Copy:** Hue's dark card grid with the play affordance, and Riveo's segmented scrubbers with numeric values for detailed parameters.
**Avoid:** the Mercedes Cancel/Save modal. Every change already shows on the wall, so a separate Save step adds nothing.

**References used**
- Philips Hue, Effects grid with active state: https://mobbin.com/screens/0cbffc5c-4a20-4886-b687-3d60c33f4cd5
- Mercedes-Benz, ambient lighting colour/animation/brightness sheet: https://mobbin.com/screens/89daf8bc-3d54-4b0b-915b-e1ca82560b2b
- Fi, colour swatch bottom sheet: https://mobbin.com/screens/50ef5951-0195-4be4-a7df-c02604f61438
- Riveo, preview canvas + parameter scrubbers: https://mobbin.com/screens/929b17ce-a309-498f-9edf-d4b2614f3e1a

---

## 3. Media library grid + per-item settings

**Grid**
- A 3-column grid with 2px gutters (Snapchat camera roll, LinkedIn picker). Tiles are **2:3 portrait**, not square. Each thumbnail is rendered at wall resolution and scaled up with `image-rendering: pixelated`.
- Badges: duration bottom-left ("0:12") on a dark pill, like Snapchat and LinkedIn. Type icon (GIF/video/image/pixel) top-left. Selection check bottom-right in multi-select mode.
- Upload: a FAB "+" bottom-right (VSCO) plus a drop zone on desktop. While files upload, a **progress banner pinned at the top**: "Uploading 2 of 5", a thin progress bar and Cancel. VSCO "Syncing to Cloud 0/1" and Tinder's dark toast with a thumbnail work this way. Each uploading tile also shows its own progress ring (Tinder spinner on each tile).
- Transcoding: the tile is dimmed and shows a "Converting..." label with an indeterminate bar.
- Failed: the tile gets a red "!" badge, like the HBO Max download-failed icon. Tapping it opens a bottom sheet with a **plain-language reason + the list of formats that work + Retry / Delete**. The HBO sheet has "Download Failed" in red, an explanation, and Retry/Delete/Go-to actions. A one-off toast (Posh: "Video size must be less than 15MB") is fine for errors at pick time.
- Filter chips above the grid: All / Videos / Images / Pixel art (Snapchat's "Videos" dropdown pill).

**Per-item editor (full-screen, dark)**
- The preview fills the top 60% at 2:3, drawn by the same dot renderer as Home. Crop/position is a drag-and-pinch frame over the source with corner brackets (Apple Photos, Reddit).
- Below the preview: a horizontal **segmented row for scaling mode** (Fit / Fill / Stretch / Pixel). Apple Photos uses "SQUARE  WALLPAPER  9:16" and Reddit "Original Freeform Square".
- Filter presets: a row of thumbnail chips under it (Google Photos "Enhance / Dynamic").
- Bottom tool tabs, like Apple Photos (Adjust / Filters / Crop) and LinkedIn: **Fit · Filter · Speed · Dither**. Each tab swaps the control area: a speed scrubber with a "1.0x" readout (LinkedIn zoom ruler), and a dithering on/off switch + strength.
- Top bar: Cancel left, Save right (Apple Photos yellow "Done" pill). Here Save is correct, because the edit is not live on the wall until it is sent.

**Avoid:** square tiles, because they crop the 2:3 content. Also avoid Saturn's generic "Oops, something went wrong" alert; the spec requires the error to say what went wrong.

**References used**
- Snapchat, video grid with duration badges + import progress: https://mobbin.com/screens/06c0d87c-9790-4545-b3db-ae977a012a0b
- VSCO, sync banner + FAB + upload badges: https://mobbin.com/screens/5c30731c-3fbd-47f4-b000-aee56cfb2615
- Tinder, upload toast + spinner on each tile: https://mobbin.com/screens/44e28fc0-84f2-41ed-9222-28d32089d063
- HBO Max, failed item badge + Retry/Delete sheet: https://mobbin.com/screens/2c6b51a6-80b2-40f0-8513-83e693c55da3
- Apple Photos, crop with aspect presets + Adjust/Filters/Crop tabs: https://mobbin.com/screens/5bcf365a-c53b-471d-95e5-08dc4d9fa79d
- LinkedIn, crop with zoom ruler + bottom tools: https://mobbin.com/screens/1390e851-257b-48df-87ed-90a8e42a0b46

---

## 4. Playlist editor

**Layout**
- Header (Spotify playlist): playlist name, a meta line "12 items · 6 min loop", then a control row with **Shuffle** and **Loop** icon toggles on the left and a big round **Play on wall** button on the right.
- Action chips under it: + Add, Edit, Sort (Spotify "Add / Mix / Edit / Sort" pill row).
- Rows: a 2:3 thumbnail (40x60), the name, a "Video · 0:12" subline, then **a duration chip** ("8 s") on the right, and a drag handle (three lines) at the far right. Spotify's queue has a handle on every row, and the now-playing row is in green text with a play button.
- Per-item duration: tap the chip to open a small stepper popover (5 s / 10 s / 30 s / "full clip"). Spotify has nothing for this. Future Pro shows the duration inside the row card ("Running 45 min").
- Reorder: long-press anywhere on the row, or drag the handle. The lifted row gets a raised surface and an outline, like Klarna's "Klarna cards" row while it is dragged.
- Remove: swipe left to show a red "Remove" pill (NYTimes queue). In Edit mode, also show a red minus at the left of each row (Garmin).
- Bottom bar (Spotify queue): Shuffle · Loop · Timer, as three large equal tiles. This is useful on the playing state.

**Copy:** Spotify's dark list density (about 56px rows) and the green "now playing" highlight, which shows which item is on the wall now.
**Avoid:** a separate reorder screen with Save (Starling/Luma). Reorder in place and save automatically.

**References used**
- Spotify, queue with handles + Shuffle/Repeat/Timer: https://mobbin.com/screens/f86dc1c9-50a6-4dda-a162-85f41f167386
- Spotify, playlist header with shuffle/play + Add/Edit/Sort chips: https://mobbin.com/screens/e64c32b5-563a-43b0-b073-a2452f5a2eae
- NYTimes, queue swipe-to-remove + handles: https://mobbin.com/screens/aab4e7b5-889a-4be4-977f-158494911103
- Garmin Connect, dark edit list with delete + handle: https://mobbin.com/screens/6fa3a735-309f-4022-a1d1-5ff182b29a2f
- Klarna, lifted row while it is dragged: https://mobbin.com/screens/f6b05568-9203-4f66-9cef-b2726ab8eedf

---

## 5. Schedule

**Layout**
- At the top, a master "Schedule active" toggle with a caption (MyDyson). Under it, a **24h ring** showing the on period as a coloured arc, with draggable handles for on and off times. Starlink's sleep ring has Sleep and Wake handles, and Polestar's charging ring has the duration in the centre ("12h 0m"). Under the ring, two rows: "On  07:00" and "Off  23:30". Tapping a row opens a native time picker.
- Day selector: 7 round day chips S M T W T F S (Hue automation), filled when selected.
- Several schedules: a list of cards, each with a name, time range, days summary and an inline toggle (Rivian "Morning Weekdays 08:00 Mon, Tue, Wed"), plus a "+" card at the end.
- **Quiet hours** section: its own card with a time range and a "Max brightness" slider + value (Hatch "Nighttime Brightness 10%").
- Footer note on time zone: "Times use the wall's clock (Europe/London)". MyDyson warns when the machine's time zone differs from the phone's, and it is worth copying.

**Avoid:** the SmartThings wheel picker modal that is stacked on another modal. Use Hue's or Starlink's dark full-page form.

**References used**
- Starlink, sleep schedule ring with toggle: https://mobbin.com/screens/e684f44d-281e-49ce-91a2-644fd3aeeb10
- Philips Hue, start/end + repeat day chips: https://mobbin.com/screens/b3dced36-5720-4558-8cda-0278cd01fc50
- MyDyson, dark schedule list, week strip, time zone note: https://mobbin.com/screens/5bfdea73-9690-44ec-8ce5-06487605050f
- Rivian, schedule card with inline toggle: https://mobbin.com/screens/030e8b2b-f9be-46d0-8466-dc36b6c72afb

---

## 6. Settings

**Layout:** inset grouped list (Hue bridge settings, SmartThings). Small-caps section headers, rows with the label on the left and the value or chevron on the right, and grey help text under groups.
- **DEVICE**: Software version (read-only value), IP address (read-only), Hostname, Admin password.
- **NETWORK**: Wi-Fi (current SSID, then the network list from screen 7), Static IP.
- **DISPLAY / MATRIX** (behind an "Advanced" disclosure): slowdown, PWM bits, chain mapping, limiter ceiling. These are number steppers with a short caption under each (Meta Quest "Tracking Frequency" row has a caption).
- **INTEGRATIONS**: MQTT broker, DDP toggle, game-hook token. The token row has Copy and Regenerate buttons.
- **MAINTENANCE**: separate buttons with an icon, a title and a description under each (WHOOP "FIRMWARE CHECK / REBOOT DEVICE / ERASE DEVICE DATA"): Check for update, Restart, Panel ID test, Factory reset.
- Destructive actions: red outlined buttons at the very bottom (Hue "Clean up / Remove"). Each one opens a confirmation sheet that says exactly what will be lost (Meta Quest "Are you sure? This will remove..." with a red Reset). Factory reset also shows a red warning box (Starlink: "will clear your WiFi network name, password...").

**References used**
- Philips Hue, bridge settings (version, IP, network, red Remove): https://mobbin.com/screens/f157d305-bed9-4570-9e8a-c566d4cccb39
- WHOOP, device maintenance actions with descriptions: https://mobbin.com/screens/359a664b-0c3d-488b-99f2-b272bffd8673
- Meta Quest, advanced settings + factory reset confirmation: https://mobbin.com/screens/583f42e5-12da-4ad3-9f01-fc75a0ccbd1a
- Starlink, factory reset warning callout: https://mobbin.com/screens/afbb112f-6e82-4fe7-9838-693bcb8aa752

---

## 7. First-time Wi-Fi setup (captive portal) + login / first admin password

The captive portal runs in the OS's captive mini-browser, so keep it to one column, no tab bar, no JS-heavy parts, and large tap targets.

**Network list** (MyDyson, Xbox, Hatch)
- Title "Choose your Wi-Fi network" and the subtitle "LED Poster can see these networks".
- Rows: SSID on the left, then a lock icon and a signal-strength icon on the right (Xbox shows both). Sort the list by signal strength. Put "Refresh" at the top right (Alexa "Refresh networks").
- Footer links: "Join other network..." for a hidden SSID (Hatch), and "I can't see my network" (MyDyson).

**Password** (Hatch, Meta AI)
- Show the network name prominently ("Wi-Fi password for Digiana"), with one password field, a show/hide eye, and a full-width **Connect** button that is disabled while the field is empty. MyDyson adds a "This network has no password" checkbox.

**Joining status** (MyDyson stepper, Sonos sheet)
- A 4-step progress row: Hotspot → Joining → Getting IP → Done, with checks filling in (MyDyson's purple steps).
- Copy line: "Connecting LED Poster to *Digiana*..." (Sonos). On success, show the new address `http://ledposter.local` + IP, and a note that the phone must go back to its normal Wi-Fi. On failure, show the reason ("Wrong password", "Network not found") and a "Try again" button that returns to the password step.

**First-run admin password / login**
- Create: one field + show/hide, then a live checklist of rules underneath (Rocket Money green check rows, Origin inline checks), then Continue. Keep the rules short (for example, at least 8 characters), because this is a home device.
- Login: device name + wall icon at the top, then one password field and "Sign in". There is no username, since there is only one admin.

**Avoid:** the Alexa or SmartThings "Connecting..." screens that show only a spinner and no steps. If the join fails, the user cannot tell which step went wrong.

**References used**
- MyDyson, dark network list + "I can't see my network": https://mobbin.com/screens/854fc0c7-9993-439a-a33e-788b737a9c62
- Xbox, lock + signal icons, Join/Refresh: https://mobbin.com/screens/6e07ea42-f3c9-4b72-8414-70aa428b589c
- Hatch Sleep, Wi-Fi password for a named network: https://mobbin.com/screens/15638958-9ed9-4043-952e-69c18b661801
- MyDyson, connection stepper: https://mobbin.com/screens/b2f3205c-9ecc-4ce5-bc29-f926e4880c34
- Sonos, "Connecting X to network" sheet: https://mobbin.com/screens/65ebf24b-317a-4efc-9d39-f0f93f7ed53c
- Rocket Money, set password with live rule checklist: https://mobbin.com/screens/c0ba7943-75bb-4fa4-842e-f1f92193a2c0

---

## Shared design language

**Theme: dark by default.** The wall is light on black, and the app should look like it (Hue, Spotify, MyDyson and Starlink are all dark). This also keeps the phone from washing out the wall in a dark room.
- Background `#000` behind the preview canvas only. App background `#0E0E10`, raised cards/sheets `#1A1A1E`, dividers `#2A2A30`.
- Text: primary `#F2F2F5`, secondary `#9A9AA3`. Do not use secondary grey below 4.5:1 contrast on the card colour.
- One accent for the active/on state (for example a warm LED amber `#FFB020`, or green like Spotify's "now playing"). Destructive red `#FF4D4F` only for reset/delete/error. Amber also for the limiter warning.
- Let the content provide colour: tint the glow behind the preview and the active mode card from the current frame (Hue does this with its header gradient).

**Spacing and shape**
- 4px base grid. 16px screen padding, 12px gaps between cards, 8px inside chip rows.
- Radii: cards 16px, sheets 20px (top corners), chips/pills fully rounded, thumbnails 6px. The preview canvas gets 12px plus a 1px `#ffffff14` border.
- List rows at least 52px tall. All tap targets at least 44x44px.

**Controls**
- Sliders: full width, label on the left, value on the right above the track, 28px thumb, filled track in the accent colour. Send updates while dragging (throttled to about 10/s) so the wall responds live.
- Toggles: iOS-style switches for booleans. Segmented controls for choosing 1 of 3-5 options (scaling mode, flat/dot view).
- Bottom sheets for secondary settings (mode settings, item errors, duration picker), with a grab handle and drag to dismiss.
- Changes apply live (power, brightness, mode, playlist order) with no Save button. Use explicit Save/Connect only where an action is staged or disruptive: media edits, Wi-Fi, network, matrix tuning.
- Status uses monospace numerals (`tabular-nums`) for fps, current draw, durations and times, so values don't jitter as they update.
- Navigation: a 5-tab bottom bar (Home, Modes, Library, Playlist, More) on mobile. On desktop (768px and wider), switch to a left rail and keep the preview pinned in a right column.

---

## Built screens (milestones 4 and 6)

What was built in `web/static/` and which references above each screen follows. Milestone 6 added the section 7 Wi-Fi setup screens and the Settings network rows.

| Screen | Route | References used | How it was built |
|---|---|---|---|
| Login / first admin password | `/login` | Rocket Money (live rule checklist), Hatch (single field with show/hide), MyDyson (one-column dark form) | One password field, no username. First run shows "Set an admin password" with the 8-character rule ticking green; afterwards the same page is plain sign-in. |
| Home | `#/home` | Philips Hue (power toggle in the header row, 5-tab bar), Amazon Alexa (brightness slider with value, "Colour >"-style current-mode row), IKEA Home smart (device hero with halo: here the live dot preview with a glow tinted from the frame), Hatch (labelled slider in a card) | 2:3 canvas at 62% width on pure black, Dots/Flat segmented toggle under it, monospace pills `mode · fps · A · limiter` (limiter pill amber only when active). Auto-brightness disables the slider instead of hiding it. Engine offline: banner plus "no signal" over a dimmed canvas. |
| Modes | `#/modes` | Philips Hue Effects grid (2 columns, active card tinted with a stop icon, play affordance), Mercedes ambient lighting sheet (small-caps groups), Fi colour swatches, Riveo (preview pinned above parameters) | Cards come from `GET /api/modes`; "..." opens a bottom sheet with that mode's settings and a live mini preview. Changes apply immediately; "Reset to defaults" at the bottom, no Save. M5 adds effects/clock/text by appending to the list with slider/color/text settings, which the sheet already renders. |
| Library | `#/library` | Snapchat and LinkedIn (3-column 2:3 grid, 2px gutters, duration pill bottom-left, type badge top-left), VSCO (progress banner pinned at the top, FAB), Tinder (per-tile progress), HBO Max (red "!" badge and a Retry/Delete sheet with the plain-language reason) | Filter chips All/Videos/GIFs/Images/Pixel art scroll horizontally. Desktop also gets a drop zone. Busy tiles dim with "Converting..." and an indeterminate bar. The failure sheet shows the transcoder's message verbatim, which includes the formats that work. |
| Upload options (sheet) | FAB, single file | Apple Photos (aspect presets as a segmented row), Riveo (preview above controls) | Before a single file is sent, the sheet shows it through the same dot renderer with scale/filter/position emulated client-side, so the letterbox or crop is visible before uploading. Several files at once upload with defaults. |
| Item editor | `#/item/<id>` | Apple Photos (crop frame, bottom tool tabs Adjust/Filters/Crop became Fit/Filter/Crop/Speed/Dither, Save top right), LinkedIn (crop frame and a value control for speed), Google Photos (filter chips became the Auto/Smooth/Pixel segmented row) | The preview plays 12 cached frames from `/api/media/{id}/frames` while the settings match the cache ("as on the wall"); once a visual setting changes it switches to a client-side emulation of the source until Save re-transcodes. Crop is a draggable rectangle over the source plus numeric fields. Save is explicit because the edit is not live on the wall until re-transcoded. |
| Playlist | `#/playlist` | Spotify playlist header (name, meta line, round Play, Shuffle/Loop toggles, Add/Edit chips) and queue (drag handle on every row, ~60px rows), NYTimes (remove per row), Klarna (lifted row while dragging) | Reorder by dragging the handle (pointer events, touch included); saved on drop. The duration chip opens a sheet: Full clip / 5 / 10 / 15 / 30 / 60 s / custom. The playing playlist's chip shows a play mark. Per-row "now playing" needs the engine to report the current item (not in the M3 status), so it is left for M5. |
| More | `#/more` | Hue's fifth-tab pattern for secondary screens | Schedule, Settings, API docs, Sign out, version line. |
| Schedule | `#/schedule` | Starlink (24h ring with the on period as an arc, master toggle), Philips Hue (start/end rows, round day chips), Hatch ("Nighttime brightness" slider for quiet hours), MyDyson (time zone note) | The ring is a non-draggable SVG (arcs for the on period and quiet hours, a white dot for now); times use native `<input type=time>`. Everything saves on change. One schedule, as the spec asks for on/off times and a quiet-hours cap. |
| Settings | `#/settings` | Philips Hue bridge settings (inset grouped list, version row, IP and network rows), Meta Quest (advanced disclosure with captions under rows), WHOOP (maintenance actions as rows with a description) | Groups: DEVICE (version, admin password), NETWORK (M6: Wi-Fi row with SSID, signal bars and a chevron to `/setup`; IP address row opening a DHCP/Static sheet; Hostname row with a validated sheet; a collapsed "Reserve it in your router" card with the MAC address), API TOKENS, MATRIX behind "Advanced", INTEGRATIONS, MAINTENANCE (Panel ID test, Show address on the wall, Start Wi-Fi setup hotspot). Update, restart, factory reset and the game-hook token belong to M8/M5. |
| Static IP with rollback (sheet) | `#/settings` IP row, `#/settings/confirm` | Hue (value rows), Starlink (warning callout for a disruptive change) | Segmented DHCP / Static, address, prefix (fixed to the current subnet), gateway, DNS; server-side validation errors are shown inline. Apply shows a 5s countdown then sends the browser to the new address; there, after sign-in, the Settings page shows an amber banner with a live `m:ss` countdown and "Keep this address". No confirm within 2 minutes: the systemd timer reverts to DHCP. |
| Wi-Fi setup wizard: network list | `/setup` | MyDyson (dark list, "I can't see my network"), Xbox (lock + signal icons, Refresh top right), Hatch ("Join other network" for hidden SSIDs), Alexa (Refresh networks) | One column, no tab bar, for captive mini-browsers. Rows sorted by signal with a lock for secured networks and a 4-bar signal icon; 802.1X rows are disabled with a reason. Refresh in hotspot mode warns that the hotspot drops for ~15s. The crumb line shows "Step n of 3" on a fresh wall. |
| Wi-Fi setup wizard: password | `/setup` | Hatch ("Wi-Fi password for X", show/hide eye, Connect disabled while empty), MyDyson ("This network has no password" checkbox) | Connect stays disabled until 8+ characters or the no-password box is ticked. In hotspot mode a note says the hotspot switches off while joining and to watch the wall. |
| Wi-Fi setup wizard: joining | `/setup` | MyDyson (4-step stepper Hotspot, Joining, Getting IP, Done with green checks), Sonos ("Connecting X to Y" copy) | Polls `/api/network/setup` every 2s. Failure marks the step red and shows the reason ("Wrong password", "Network not found") with Try again / Pick another; success shows `http://<hostname>.local` + IP and tells the phone to go back to its own Wi-Fi. If the phone loses the hotspot mid-join (expected), a note explains what to do and the page probes `<hostname>.local` in case the phone has already moved. |
| Wi-Fi setup wizard: admin password / sign in | `/setup` step 1 | Rocket Money (rule checklist), Hatch (one field with eye) | On a wall with no admin password the wizard sets it first (docs/DEVIATIONS.md, threat model); on a wall that has one, the same screen is a sign-in. |
| Desktop (900px and wider) | all | Shared design language: left rail, preview pinned in a right column | The preview card moves into the right column so it stays visible on every screen. |

As built: `#0E0E10` background, `#1A1A1E` cards, `#2A2A30` dividers, amber `#FFB020` accent (also the limiter warning), `#FF4D4F` destructive, `#1DB954` for the connected dot. 16px screen padding, 12px gaps, 16px card radius, 20px sheet radius, 44px minimum tap targets, `tabular-nums` for live values, `prefers-reduced-motion` disables animations. Installable PWA: manifest plus a service worker that caches only the app shell.
