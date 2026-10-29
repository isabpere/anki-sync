#!/usr/bin/env python3
"""
Google Sheet -> AnkiWeb vocabulary sync (cloud version, no laptop needed)

Runs on GitHub Actions. Each run:
  1. logs in to AnkiWeb and downloads a temporary copy of your collection
  2. reads every tab listed under SETTINGS (A = German word, B = meaning,
     C = example / note)
  3. German -> meaning cards go into "deck", meaning -> German cards into
     "reverse_deck"; new words are added, edited words are updated
  4. syncs the changes back to AnkiWeb with a NORMAL sync (the same kind your
     phone does), so your reviews on other devices are kept

Safety: this script never does a "full upload". If AnkiWeb ever asks for one,
it stops without changing anything on AnkiWeb.

Your AnkiWeb email and password are read from the GitHub secrets
ANKIWEB_USERNAME and ANKIWEB_PASSWORD - never write them into this file.
"""

import csv
import html
import io
import os
import re
import sys
import tempfile
import urllib.error
import urllib.request

# ============================ SETTINGS ============================
# One block per tab of your Google Sheet. For each tab fill in:
#   "link"         - the "Publish to web" CSV link of THAT tab
#   "deck"         - deck for German -> meaning cards (use the EXACT name of an
#                    existing deck, or a new name and it will be created)
#   "reverse_deck" - deck for meaning -> German cards (created automatically)
# To add another tab later, copy a whole { ... }, block and change the values.
SHEETS = [
    {   # Tab 1 - your first vocabulary list
        "link":         "PASTE_LINK_OF_TAB_1_HERE",
        "deck":         "PUT_YOUR_EXISTING_DECK_NAME_HERE",
        "reverse_deck": "PUT_YOUR_EXISTING_REVERSE_DECK_NAME_HERE",
    },
    {   # Tab 2 - the new vocabulary list
        "link":         "PASTE_LINK_OF_TAB_2_HERE",
        "deck":         "PUT_DECK_NAME_FOR_TAB_2_HERE",
        "reverse_deck": "PUT_REVERSE_DECK_NAME_FOR_TAB_2_HERE",
    },
]

NOTE_TYPE = "Einfach"        # card type for German -> meaning ("Basic" in English Anki)
REVERSE_CARDS = True         # also make meaning -> German cards
HAS_HEADER_ROW = True        # True if row 1 holds titles like "Das Wort | Bedeutung"
UPDATE_CHANGED = True        # update cards when you edit a row in the sheet
TAG = "google-sheet"         # tag added to every card created by this script
# ==================================================================

# Card type used for the reverse cards (same one the laptop script uses).
REVERSE_NOTE_TYPE = "Vokabel umgekehrt (Google Sheet)"
REVERSE_FIELDS = ["Wort", "Bedeutung", "Beispiel"]
REVERSE_FRONT = "{{Bedeutung}}"
REVERSE_BACK = ("{{FrontSide}}\n\n<hr id=answer>\n\n{{Wort}}"
                "{{#Beispiel}}<br><br><i>{{Beispiel}}</i>{{/Beispiel}}")
CARD_CSS = """.card {
    font-family: arial;
    font-size: 20px;
    text-align: center;
    color: black;
    background-color: white;
}
"""


# ----------------------------- sheet ------------------------------

class SheetError(Exception):
    pass


def to_csv_url(url):
    """Turn a normal Google Sheets link into a CSV download link."""
    if "/edit" in url or "/view" in url:
        m = re.search(r"/spreadsheets/d/([a-zA-Z0-9_-]+)", url)
        if m:
            gid = re.search(r"gid=(\d+)", url)
            csv_url = f"https://docs.google.com/spreadsheets/d/{m.group(1)}/export?format=csv"
            return csv_url + (f"&gid={gid.group(1)}" if gid else "")
    return url


def read_sheet(url):
    try:
        with urllib.request.urlopen(to_csv_url(url), timeout=60) as resp:
            text = resp.read().decode("utf-8-sig")
    except urllib.error.HTTPError as e:
        raise SheetError(f"Google returned an error ({e.code}). Check the link and that the tab is "
                         "published to the web.")
    except urllib.error.URLError:
        raise SheetError("Could not download the sheet. Check the link.")

    if "<html" in text[:1000].lower():
        raise SheetError("Got a web page instead of the sheet data. Use the 'Publish to web' "
                         "CSV link of the tab.")

    rows = list(csv.reader(io.StringIO(text)))
    if HAS_HEADER_ROW:
        rows = rows[1:]
    clean = []
    for row in rows:
        row = [cell.strip() for cell in row]
        while row and not row[-1]:
            row.pop()
        if len(row) >= 2 and row[0] and row[1]:
            clean.append(row)
    return clean


def norm(value):
    """Compare text the way it looks, ignoring Anki's HTML and extra spaces."""
    value = re.sub(r"<br\s*/?>", " ", value, flags=re.I)
    value = re.sub(r"<[^>]+>", "", value)
    value = html.unescape(value).replace("\xa0", " ")
    return " ".join(value.split()).casefold()


def to_html(value):
    return html.escape(value, quote=False).replace("\n", "<br>")


def forward_fields(row, fields):
    """German -> meaning. Column A on the front; B on the back, and any further
    columns (the example) added below it in italics."""
    if len(row) <= len(fields):
        return {f: to_html(v) for f, v in zip(fields, row)}
    values = {f: to_html(v) for f, v in zip(fields[:-1], row)}
    rest = [v for v in row[len(fields) - 1:] if v]
    values[fields[-1]] = to_html(rest[0]) + "".join(
        f"<br><br><i>{to_html(v)}</i>" for v in rest[1:])
    return values


def reverse_fields(row, fields):
    """Meaning -> German. Bedeutung on the front, Wort + Beispiel on the back."""
    example = "<br>".join(to_html(v) for v in row[2:] if v)
    return {"Wort": to_html(row[0]), "Bedeutung": to_html(row[1]), "Beispiel": example}


def check_settings():
    links = []
    for number, sheet in enumerate(SHEETS, start=1):
        for key in ("link", "deck", "reverse_deck"):
            value = str(sheet.get(key, "")).strip()
            if not value or "PASTE_" in value or "PUT_" in value:
                sys.exit(f'Tab {number}: "{key}" is not filled in yet. Edit anki_cloud_sync.py '
                         "and fill it in under SETTINGS.")
        links.append(sheet["link"].strip())
        if sheet["deck"].strip() == sheet["reverse_deck"].strip():
            sys.exit(f'Tab {number}: "deck" and "reverse_deck" must be two different deck names.')
    if len(set(links)) < len(links):
        sys.exit("Two tabs have the same link. Each tab needs its own 'Publish to web' link.")


# ----------------------------- anki -------------------------------

def ensure_reverse_note_type(col):
    if col.models.by_name(REVERSE_NOTE_TYPE):
        return
    model = col.models.new(REVERSE_NOTE_TYPE)
    for name in REVERSE_FIELDS:
        col.models.add_field(model, col.models.new_field(name))
    template = col.models.new_template("Bedeutung → Wort")
    template["qfmt"] = REVERSE_FRONT
    template["afmt"] = REVERSE_BACK
    col.models.add_template(model, template)
    model["css"] = CARD_CSS
    col.models.add(model)
    print(f'Created card type "{REVERSE_NOTE_TYPE}" for the reverse cards.')


def sync_cards(col, rows, deck, note_type, build_fields, label):
    """Add / update one kind of card for every row. Returns number of changes."""
    model = col.models.by_name(note_type)
    fields = col.models.field_names(model)
    deck_id = col.decks.id(deck, create=True)

    # Cards already in the deck, keyed by their first field (the German word)
    existing = {}
    for note_id in col.find_notes(f'"deck:{deck}" "note:{note_type}"'):
        note = col.get_note(note_id)
        existing[norm(note.fields[0])] = note

    added = updated = 0
    seen = set()
    for row in rows:
        key = norm(row[0])
        if key in seen:
            continue  # same word twice in the sheet
        seen.add(key)
        values = build_fields(row, fields)

        if key in existing:
            if UPDATE_CHANGED:
                note = existing[key]
                changed = False
                for field, value in values.items():
                    if norm(note[field]) != norm(value):
                        note[field] = value
                        changed = True
                if changed:
                    col.update_note(note)
                    updated += 1
        else:
            note = col.new_note(model)
            for field, value in values.items():
                note[field] = value
            note.tags = [TAG]
            col.add_note(note, deck_id)
            added += 1

    print(f'{label} (deck "{deck}"): added {added} new card(s), updated {updated}.')
    return added + updated


def main():
    check_settings()

    username = os.environ.get("ANKIWEB_USERNAME", "").strip()
    password = os.environ.get("ANKIWEB_PASSWORD", "")
    if not username or not password:
        sys.exit("AnkiWeb login missing. Add the repository secrets ANKIWEB_USERNAME and "
                 "ANKIWEB_PASSWORD (Settings -> Secrets and variables -> Actions).")

    # Read all tabs first, so a broken link never leaves AnkiWeb half-updated
    tabs = []
    for number, sheet in enumerate(SHEETS, start=1):
        try:
            rows = read_sheet(sheet["link"].strip())
            print(f"Tab {number}: read {len(rows)} words from the sheet.")
            tabs.append((number, sheet, rows))
        except SheetError as e:
            print(f"Tab {number}: SKIPPED - {e}")
    if not tabs:
        sys.exit("No tab could be read, nothing to do.")

    from anki.collection import Collection
    from anki.errors import SyncError
    from anki.sync import SyncAuth

    workdir = tempfile.mkdtemp()
    col = Collection(os.path.join(workdir, "collection.anki2"))
    try:
        # 1. Log in and download the collection from AnkiWeb
        try:
            auth = col.sync_login(username, password, None)
        except SyncError as e:
            sys.exit(f"AnkiWeb login failed: {e}. Check the ANKIWEB_USERNAME / ANKIWEB_PASSWORD "
                     "secrets (your AnkiWeb email and password).")

        out = col.sync_collection(auth, False)
        if out.new_endpoint:
            auth = SyncAuth(hkey=auth.hkey, endpoint=out.new_endpoint)
        if out.required == out.FULL_UPLOAD:
            sys.exit("Your AnkiWeb collection is empty. Sync Anki on your laptop or phone first.")
        if out.required in (out.FULL_DOWNLOAD, out.FULL_SYNC):
            print("Downloading your collection from AnkiWeb...")
            col.close_for_full_sync()
            col.full_upload_or_download(auth=auth, server_usn=None, upload=False)
            col.reopen(after_full_sync=True)

        if col.card_count() == 0:
            sys.exit("The downloaded collection has no cards - stopping to be safe.")
        print(f"Downloaded collection with {col.card_count()} cards.")

        if not col.models.by_name(NOTE_TYPE):
            names = ", ".join(m.name for m in col.models.all_names_and_ids())
            sys.exit(f'Note type "{NOTE_TYPE}" not found. Set NOTE_TYPE to one of: {names}')
        if REVERSE_CARDS:
            ensure_reverse_note_type(col)

        # 2. Add / update cards
        changes = 0
        for number, sheet, rows in tabs:
            print(f"\n=== Tab {number} ===")
            changes += sync_cards(col, rows, sheet["deck"].strip(), NOTE_TYPE,
                                  forward_fields, "German -> meaning")
            if REVERSE_CARDS:
                changes += sync_cards(col, rows, sheet["reverse_deck"].strip(),
                                      REVERSE_NOTE_TYPE, reverse_fields, "Meaning -> German")

        print()
        if not changes:
            print("Nothing new - AnkiWeb is already up to date.")
            return

        # 3. Upload the changes with a normal sync (never a full upload)
        out = col.sync_collection(auth, False)
        if out.required in (out.NO_CHANGES, out.NORMAL_SYNC):
            print("Synced with AnkiWeb. Sync Anki on your phone to get the new cards.")
        else:
            sys.exit("AnkiWeb asked for a full sync, so NOTHING was uploaded (to protect your "
                     "reviews). Open Anki on your laptop, sync it, then run this again.")
    finally:
        try:
            col.close()
        except Exception:
            pass


if __name__ == "__main__":
    main()
