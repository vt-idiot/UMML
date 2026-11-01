"""Per-character picker for big packs: which characters a mod supplies.

Unticking a character leaves it stock (or lets the next mod in the profile
supply it). "Fill gaps only" makes the mod rank below every normal mod, so it
only supplies characters nothing else covers.
"""

from __future__ import annotations

import queue
import sqlite3
import threading
import tkinter as tk
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from tkinter import messagebox, ttk
from typing import Any

from .ui_windows import present_toplevel
from .veteran_local_portraits import LocalPortraitCache
from .veteran_master_data import CHARACTER_NAME_CATEGORY, discover_master_mdb

THUMBNAIL = 40


@dataclass
class CharacterRow:
    chara_id: str
    name: str
    active: int
    waiting: int
    not_in_game: int
    selected: bool
    covered_by: str = ""
    mixed_with: str = ""

    @property
    def status(self) -> str:
        if not self.selected:
            return "left out"
        if self.mixed_with:
            return f"CHECK: one part mixes files with {self.mixed_with}"
        if self.covered_by:
            return f"partly overridden by {self.covered_by}"
        parts = []
        if self.active:
            parts.append(f"{self.active} active")
        if self.waiting:
            parts.append(f"{self.waiting} waiting for download")
        if self.not_in_game and not self.active and not self.waiting:
            parts.append("not in this game yet")
        return ", ".join(parts) or "no files"


def build_character_rows(
    record: Any,
    profile: Any,
    resolution: Any,
    names: dict[str, str],
    mod_names: dict[str, str],
) -> list[CharacterRow]:
    """Rows for every character the mod has files for, live or not."""

    active = Counter(
        record.target_characters[target]
        for target in record.files
        if target in record.target_characters
    )
    waiting = Counter(
        record.target_characters[target]
        for target in record.pending_targets
        if target in record.target_characters
    )
    dormant = Counter(record.dormant_characters.values())
    excluded = set(profile.excluded_characters.get(record.id, ()))
    enabled = record.id in profile.enabled
    overridden = (
        getattr(resolution, "characters_overridden", {}).get(record.id, {})
        if resolution is not None
        else {}
    )
    mixed: dict[str, set[str]] = {}
    for item in getattr(resolution, "mixed_groups", ()) if resolution is not None else ():
        if record.id in item.mods and item.character:
            mixed.setdefault(item.character, set()).update(
                mod for mod in item.mods if mod != record.id
            )

    rows = []
    for chara_id in sorted(set(active) | set(waiting) | set(dormant)):
        covered = mixed_with = ""
        if enabled:
            covered = ", ".join(mod_names.get(mod, mod) for mod in overridden.get(chara_id, ()))
            mixed_with = ", ".join(
                mod_names.get(mod, mod) for mod in sorted(mixed.get(chara_id, ()))
            )
        rows.append(
            CharacterRow(
                chara_id=chara_id,
                name=names.get(chara_id) or f"Character {chara_id}",
                active=active[chara_id],
                waiting=waiting[chara_id],
                not_in_game=dormant[chara_id],
                selected=chara_id not in excluded,
                covered_by=covered,
                mixed_with=mixed_with,
            )
        )
    rows.sort(key=lambda row: (row.active == 0 and row.waiting == 0, row.name.casefold()))
    return rows


def load_character_names(master: Path | None) -> tuple[dict[str, str], dict[str, int]]:
    """Character names and one trainee card per character, from master.mdb."""

    names: dict[str, str] = {}
    cards: dict[str, int] = {}
    if master is None or not master.is_file():
        return names, cards
    try:
        connection = sqlite3.connect(f"file:{master.resolve().as_posix()}?mode=ro", uri=True)
    except sqlite3.Error:
        return names, cards
    try:
        try:
            for index, text in connection.execute(
                'SELECT "index", text FROM text_data WHERE category = ?',
                (CHARACTER_NAME_CATEGORY,),
            ):
                if text:
                    names[str(index)] = str(text)
        except sqlite3.Error:
            pass
        try:
            for chara_id, card_id in connection.execute(
                "SELECT chara_id, MIN(id) FROM card_data GROUP BY chara_id"
            ):
                if chara_id and card_id:
                    cards[str(chara_id)] = int(card_id)
        except sqlite3.Error:
            pass
    finally:
        connection.close()
    return names, cards


class CharacterPickerDialog(tk.Toplevel):
    def __init__(
        self,
        parent: tk.Misc,
        *,
        mod_name: str,
        rows: list[CharacterRow],
        fill_gaps: bool,
        portraits: LocalPortraitCache | None = None,
        cards: dict[str, int] | None = None,
    ):
        super().__init__(parent)
        self.title(f"Characters in {mod_name}")
        self.transient(parent.winfo_toplevel())
        self.minsize(620, 460)
        self.result: tuple[set[str], bool] | None = None
        self.rows = {row.chara_id: row for row in rows}
        self.selected = {row.chara_id for row in rows if row.selected}
        self.portraits = portraits
        self.cards = cards or {}
        self._images: dict[str, Any] = {}
        self._jobs: queue.Queue = queue.Queue()

        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)
        outer = ttk.Frame(self, padding=14)
        outer.grid(row=0, column=0, rowspan=3, sticky="nsew")
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(1, weight=1)

        ttk.Label(
            outer,
            text=(
                "Untick a character to leave them stock in this profile (or let the next mod "
                "supply them). Files resolve in profile order like UMML: a later mod wins file by "
                "file. CHECK means one part of the character (for example an outfit's mesh and "
                "its textures) now uses files from two mods."
            ),
            style="Muted.TLabel",
            wraplength=640,
            justify="left",
        ).grid(row=0, column=0, sticky="ew", pady=(0, 10))

        style = ttk.Style(self)
        style.configure("Characters.Treeview", rowheight=THUMBNAIL + 6)
        frame = ttk.Frame(outer)
        frame.grid(row=1, column=0, sticky="nsew")
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(0, weight=1)
        self.tree = ttk.Treeview(
            frame,
            columns=("use", "id", "status"),
            show="tree headings",
            selectmode="extended",
            style="Characters.Treeview",
        )
        for column, title, width, anchor in (
            ("#0", "Character", 260, "w"),
            ("use", "Use", 50, "center"),
            ("id", "ID", 60, "center"),
            ("status", "Status", 240, "w"),
        ):
            self.tree.heading(column, text=title)
            self.tree.column(column, width=width, anchor=anchor)
        scroll = ttk.Scrollbar(frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        scroll.grid(row=0, column=1, sticky="ns")
        self.tree.bind("<Double-1>", lambda _event: self._toggle(self.tree.selection()))
        self.tree.bind("<space>", lambda _event: self._toggle(self.tree.selection()))
        self.tree.bind("<Button-1>", self._clicked, add="+")

        bulk = ttk.Frame(outer)
        bulk.grid(row=2, column=0, sticky="ew", pady=(10, 0))
        ttk.Button(bulk, text="All", command=lambda: self._set_all(True)).pack(side="left")
        ttk.Button(bulk, text="None", command=lambda: self._set_all(False)).pack(side="left", padx=6)
        ttk.Button(bulk, text="Invert", command=self._invert).pack(side="left")
        self.portrait_button = ttk.Button(
            bulk, text="Load portraits", command=self._load_missing_portraits
        )
        self.portrait_button.pack(side="left", padx=(16, 0))
        if portraits is None or not portraits.available:
            self.portrait_button.configure(state="disabled")
        self.summary = ttk.Label(bulk, style="Muted.TLabel")
        self.summary.pack(side="right")

        self.fill_gaps = tk.BooleanVar(value=fill_gaps)
        ttk.Checkbutton(
            outer,
            text=(
                "Fill gaps only: rank this mod below every other mod, so it only supplies "
                "characters nothing else covers"
            ),
            variable=self.fill_gaps,
        ).grid(row=3, column=0, sticky="w", pady=(10, 0))

        buttons = ttk.Frame(outer)
        buttons.grid(row=4, column=0, sticky="ew", pady=(12, 0))
        ttk.Button(buttons, text="Cancel", command=self.destroy).pack(side="right")
        ttk.Button(
            buttons, text="Save to profile", style="Accent.TButton", command=self._save
        ).pack(side="right", padx=(0, 8))

        for row in rows:
            self.tree.insert(
                "",
                "end",
                iid=row.chara_id,
                text=f"  {row.name}",
                image=self._image_for(row.chara_id) or "",
                values=("", row.chara_id, ""),
            )
        self._render()
        present_toplevel(self, parent)

    # -- selection -------------------------------------------------------

    def _clicked(self, event) -> None:
        if self.tree.identify_column(event.x) == "#1":
            item = self.tree.identify_row(event.y)
            if item:
                self._toggle((item,))

    def _toggle(self, items) -> None:
        for item in items:
            if item in self.selected:
                self.selected.discard(item)
            else:
                self.selected.add(item)
        self._render()

    def _set_all(self, value: bool) -> None:
        self.selected = set(self.rows) if value else set()
        self._render()

    def _invert(self) -> None:
        self.selected = set(self.rows) - self.selected
        self._render()

    def _render(self) -> None:
        for chara_id, row in self.rows.items():
            row.selected = chara_id in self.selected
            self.tree.set(chara_id, "use", "[x]" if row.selected else "[ ]")
            self.tree.set(chara_id, "status", row.status)
        self.summary.configure(text=f"{len(self.selected)} of {len(self.rows)} characters used")

    def _save(self) -> None:
        self.result = (set(self.rows) - self.selected, bool(self.fill_gaps.get()))
        self.destroy()

    # -- portraits -------------------------------------------------------

    def _image_for(self, chara_id: str):
        if self.portraits is None:
            return None
        card = self.cards.get(chara_id)
        path = self.portraits.cached(card) if card else None
        if path is None:
            return None
        try:
            from PIL import Image, ImageTk

            with Image.open(path) as image:
                image = image.convert("RGBA")
                image.thumbnail((THUMBNAIL, THUMBNAIL))
                photo = ImageTk.PhotoImage(image, master=self)
        except Exception:
            return None
        self._images[chara_id] = photo
        return photo

    def _load_missing_portraits(self) -> None:
        if self.portraits is None:
            return
        missing = [
            (chara_id, card)
            for chara_id in self.rows
            if chara_id not in self._images and (card := self.cards.get(chara_id))
        ]
        if not missing:
            self.summary.configure(text="All available portraits are loaded")
            return
        self.portrait_button.configure(state="disabled", text="Loading portraits...")
        cache = self.portraits

        def work():
            for chara_id, card in missing:
                try:
                    result = cache.extract(card)
                except Exception:
                    result = None
                self._jobs.put((chara_id, bool(result and result.portrait)))
            self._jobs.put(None)

        threading.Thread(target=work, daemon=True).start()
        self.after(100, self._poll_portraits)

    def _poll_portraits(self) -> None:
        if not self.winfo_exists():
            return
        try:
            while True:
                job = self._jobs.get_nowait()
                if job is None:
                    self.portrait_button.configure(state="normal", text="Load portraits")
                    return
                chara_id, ok = job
                if ok and self.tree.exists(chara_id):
                    image = self._image_for(chara_id)
                    if image is not None:
                        self.tree.item(chara_id, image=image)
        except queue.Empty:
            pass
        self.after(100, self._poll_portraits)


def pick_characters(app, page) -> None:
    """Library action: open the picker for the selected mod and save the result."""

    if getattr(app, "_busy", False):
        app.status.set("Wait for the current Manager task to finish")
        return
    mod_id = page.selected_id()
    if not mod_id:
        app.status.set("Select a mod first")
        return
    try:
        record = app.store.get_mod(mod_id)
        profile = app.profile()
        try:
            resolution = app.current_resolution()
        except Exception:
            resolution = None
        master = discover_master_mdb(app)
        names, cards = load_character_names(master)
        mod_names = {mod.id: mod.name for mod in app.store.list_mods()}
        rows = build_character_rows(record, profile, resolution, names, mod_names)
        portraits = LocalPortraitCache.from_app(
            app, app.store.paths.root / "veterans" / "media-cache"
        )
    except Exception as exc:
        messagebox.showerror("Could not load characters", str(exc), parent=app.root)
        return
    if not rows:
        app.status.set(f"{record.name} has no character-specific files")
        return

    dialog = CharacterPickerDialog(
        app.root,
        mod_name=record.name,
        rows=rows,
        fill_gaps=mod_id in profile.fill_gaps,
        portraits=portraits,
        cards=cards,
    )
    app.root.wait_window(dialog)
    if dialog.result is None:
        return
    excluded, fill_gaps = dialog.result
    profile = app.profile()
    if excluded:
        profile.excluded_characters[mod_id] = sorted(excluded)
    else:
        profile.excluded_characters.pop(mod_id, None)
    if fill_gaps and mod_id not in profile.fill_gaps:
        profile.fill_gaps.append(mod_id)
    elif not fill_gaps and mod_id in profile.fill_gaps:
        profile.fill_gaps.remove(mod_id)
    app.store.save_profile(profile)
    app.refresh()
    if page.tree.exists(mod_id):
        page.tree.selection_set(mod_id)
    app.status.set(
        f"{record.name}: {len(rows) - len(excluded)} of {len(rows)} characters used"
        + (", fill gaps only" if fill_gaps else "")
        + f" in {profile.name}"
    )
