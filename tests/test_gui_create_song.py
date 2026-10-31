"""The Create a Song section (owner, 2026-10-31): the Band menu is the roster's pick for the voice and style, changeable."""

import json

import customtkinter as ctk

import lyricvideo.gui as gui
from lyricvideo import song_maker
from lyricvideo.gui import LyricVideoGUI
from lyricvideo.song_maker import NEW_BAND
from tests.test_gui_perf_lists import app, isolated  # noqa: F401 -- fixtures


def test_the_band_menu_offers_the_rosters_band_for_the_voice_and_style(app):
    song_maker.ROSTER_PATH.parent.mkdir(parents=True, exist_ok=True)
    song_maker.ROSTER_PATH.write_text(json.dumps({"artists": [
        {"name": "Burning Ember", "style": "Hard rock", "voice": "female", "status": "active"},
        {"name": "Copperleaf", "style": "Acoustic", "voice": "male", "status": "active"},
    ]}), encoding="utf-8")
    app._build_create_song_section(ctk.CTkFrame(app.root))

    assert app.create_band_var.get() == NEW_BAND                     # Female + Acoustic: no band yet
    app.create_voice_var.set("Male")
    app._refresh_band_choices()
    assert app.create_band_var.get() == "Copperleaf"
    app.create_voice_var.set("Female")
    app.create_style_var.set("Hard rock")
    app._refresh_band_choices()
    assert app.create_band_var.get() == "Burning Ember"
    assert list(app.create_band_menu.cget("values")) == ["Burning Ember", NEW_BAND, "Copperleaf"]


def test_a_song_is_not_started_without_a_sentence_about_it(app, monkeypatch):
    errors = []
    monkeypatch.setattr(gui.messagebox, "showerror", lambda title, text: errors.append(text))
    monkeypatch.setattr(LyricVideoGUI, "_run_create_song", lambda self, *a: errors.append("started"))
    app._build_create_song_section(ctk.CTkFrame(app.root))

    app._on_create_song()

    assert errors == ["Say in one sentence what the song is about."] and not app._running


def _ready(app, monkeypatch, band):
    started = []
    monkeypatch.setattr(LyricVideoGUI, "_run_create_song", lambda self, *a: started.append(a))
    monkeypatch.setattr(gui.threading, "Thread", _Inline)
    song_maker.ROSTER_PATH.parent.mkdir(parents=True, exist_ok=True)
    song_maker.ROSTER_PATH.write_text(json.dumps({"artists": [
        {"name": "Burning Ember", "style": "Hard rock", "voice": "female", "status": "active"},
    ]}), encoding="utf-8")
    app._build_create_song_section(ctk.CTkFrame(app.root))
    app.create_about_entry.insert(0, "a love gone wrong")
    app.create_band_var.set(band)
    app.root.after = lambda ms, func=None, *a: func(*a) if func is not None and ms == 0 else None
    return started


class _Inline:
    def __init__(self, target, args=(), daemon=None):
        self.target, self.args = target, args

    def start(self):
        self.target(*self.args)


def test_a_roster_band_starts_the_song_without_asking_musicbrainz(app, monkeypatch):
    started = _ready(app, monkeypatch, "Burning Ember")
    monkeypatch.setattr(song_maker, "musicbrainz_has_artist", lambda name: (_ for _ in ()).throw(AssertionError(name)))

    app._on_create_song()

    assert started == [("Female", "Acoustic", "Burning Ember", "a love gone wrong")]


def test_a_typed_name_a_real_act_has_is_used_only_if_the_owner_says_so(app, monkeypatch):
    asked = []
    started = _ready(app, monkeypatch, "Burning Embers")
    monkeypatch.setattr(song_maker, "musicbrainz_has_artist", lambda name: name == "Burning Embers")
    monkeypatch.setattr(gui.messagebox, "askyesno", lambda title, text: asked.append(title) or False)

    app._on_create_song()
    assert asked == ["Band name taken"] and started == [] and not app._running

    monkeypatch.setattr(gui.messagebox, "askyesno", lambda title, text: True)
    app._on_create_song()
    assert started == [("Female", "Acoustic", "Burning Embers", "a love gone wrong")]


def test_a_free_typed_name_starts_at_once_and_an_unchecked_one_asks(app, monkeypatch):
    asked = []
    started = _ready(app, monkeypatch, "Gravel Saints")
    monkeypatch.setattr(gui.messagebox, "askyesno", lambda title, text: asked.append(title) or False)
    monkeypatch.setattr(song_maker, "musicbrainz_has_artist", lambda name: False)

    app._on_create_song()
    assert asked == [] and len(started) == 1

    app._running = False
    monkeypatch.setattr(song_maker, "musicbrainz_has_artist", lambda name: (_ for _ in ()).throw(RuntimeError("offline")))
    app._on_create_song()
    assert asked == ["Band name not checked"] and len(started) == 1
