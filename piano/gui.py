"""Piano: a small libadwaita front end for the hidden Carla + sfizz piano and MIDI recording."""

import atexit
import signal
import sys
import threading
import time
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, GLib, Gtk  # noqa: E402

from . import backend as be  # noqa: E402

APP_ID = "io.github.piano.Launcher"
_rate = be.LATENCY.split("/")
LATENCY_MS = 1000 * int(_rate[0]) / int(_rate[1])


def _row(title, subtitle="", icon=None):
    row = Adw.ActionRow(title=title, subtitle=subtitle)
    row.set_use_markup(False)          # file and device names may contain '&' etc.
    if icon:
        row.add_prefix(Gtk.Image(icon_name=icon))
    return row


def _pill(label, icon, *classes):
    btn = Gtk.Button(child=Adw.ButtonContent(label=label, icon_name=icon))
    btn.add_css_class("pill")
    for c in classes:
        btn.add_css_class(c)
    return btn


def _set_pill(btn, label, icon, add=(), remove=()):
    btn.get_child().set_label(label)
    btn.get_child().set_icon_name(icon)
    for c in remove:
        btn.remove_css_class(c)
    for c in add:
        btn.add_css_class(c)


class PianoWindow(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="Piano", default_width=460, default_height=640)
        self.cfg = be.load_config()
        self.engine = be.Engine()
        self.recorder = be.Recorder()
        self.engine_state = "stopped"          # stopped | starting | running | stopping
        self.state = {}                        # last graph state from the tick worker
        self._tick_busy = False
        self._closing = False
        self._midi_first_boot = False
        self._midi_rows = {}
        self._midi_aliases = None

        self.toasts = Adw.ToastOverlay()
        self.stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE)
        self.toasts.set_child(self.stack)

        self.title = Adw.WindowTitle(title="Piano")
        header = Adw.HeaderBar(title_widget=self.title)
        self.menu_button = Gtk.MenuButton(icon_name="open-menu-symbolic", tooltip_text="Main Menu")
        menu = Gio.Menu()
        menu.append("Change Instrument…", "win.change-sfz")
        menu.append("Change MIDI Device…", "win.change-midi")
        menu.append("Open Recordings Folder", "win.open-recordings")
        menu.append("About Piano", "win.about")
        self.menu_button.set_menu_model(menu)
        header.pack_end(self.menu_button)

        view = Adw.ToolbarView()
        view.add_top_bar(header)
        view.set_content(self.toasts)
        self.set_content(view)

        for name, cb in (("change-sfz", self._on_change_sfz), ("change-midi", self._on_change_midi),
                         ("open-recordings", lambda *_: self._open_recordings()), ("about", self._on_about)):
            act = Gio.SimpleAction.new(name, None)
            act.connect("activate", cb)
            self.add_action(act)

        self._build_missing_page()
        self._build_welcome_page()
        self._build_midi_page()
        self._build_main_page()

        self.connect("close-request", self._on_close_request)
        GLib.timeout_add(1000, self._tick)
        GLib.timeout_add(1000, self._update_record_timer)
        self._route()

    # ------------------------------------------------------------------ routing / first boot

    def _show(self, page, subtitle=""):
        self.stack.set_visible_child_name(page)
        self.title.set_subtitle(subtitle)
        self.menu_button.set_visible(page == "main")

    def _route(self):
        problems = be.check_prerequisites()
        if problems:
            self._fill_missing(problems)
            self._show("missing")
        elif not self.cfg.get("sfz") or not Path(self.cfg["sfz"]).is_file():
            self._show("welcome", "Setup")
        elif not self.cfg.get("midi"):
            self._detect_midi(first_boot=True)
        else:
            self._show("main")
            self._refresh_main()

    def _detect_midi(self, first_boot):
        """Pick the MIDI device automatically; ask only if it isn't obvious."""
        inputs = be.Graph.snapshot().midi_inputs()
        hardware = [m for m in inputs if m.hardware]
        if first_boot and len(hardware) == 1:
            self._set_midi(hardware[0])
            self.toasts.add_toast(Adw.Toast(title=f"Found {hardware[0].label}", timeout=3))
            self._show("main")
            self._refresh_main()
            return
        self._midi_first_boot = first_boot
        self.midi_group.set_description(
            "Your piano couldn't be identified automatically. Connect it with a USB cable, "
            "switch it on, and choose it below." if first_boot
            else "Choose which MIDI device plays the piano.")
        self.midi_cancel.set_visible(not first_boot)
        self._midi_aliases = None
        self._fill_midi_list(inputs, preselect=self.cfg.get("midi") or
                             (hardware[0].alias if len(hardware) == 1 else None))
        self._show("midi", "Setup" if first_boot else "")

    def _set_midi(self, port):
        self.cfg["midi"] = port.alias
        self.cfg["midi_label"] = port.label
        be.save_config(self.cfg)

    # ------------------------------------------------------------------ pages

    def _build_missing_page(self):
        self.missing_page = Adw.StatusPage(icon_name="dialog-warning-symbolic",
                                           title="Some Components Are Missing")
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        self.missing_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        self.missing_list.add_css_class("boxed-list")
        retry = _pill("Check Again", "view-refresh-symbolic", "suggested-action")
        retry.set_halign(Gtk.Align.CENTER)
        retry.connect("clicked", lambda *_: self._route())
        box.append(self.missing_list)
        box.append(retry)
        self.missing_page.set_child(Adw.Clamp(child=box, maximum_size=520))
        self.stack.add_named(self.missing_page, "missing")

    def _fill_missing(self, problems):
        self.missing_list.remove_all()
        for problem, fix in problems:
            row = _row(problem, f"Run: {fix}")
            row.set_subtitle_selectable(True)
            self.missing_list.append(row)

    def _build_welcome_page(self):
        page = Adw.StatusPage(icon_name="audio-x-generic-symbolic", title="Welcome to Piano",
                              description="To get started, choose the Salamander Grand Piano "
                                          "instrument file (<b>SalamanderGrandPianoV2.sfz</b>).")
        btn = _pill("Choose SFZ File…", "document-open-symbolic", "suggested-action")
        btn.set_halign(Gtk.Align.CENTER)
        btn.connect("clicked", lambda *_: self._choose_sfz(first_boot=True))
        page.set_child(btn)
        self.stack.add_named(page, "welcome")

    def _build_midi_page(self):
        page = Adw.PreferencesPage()
        self.midi_group = Adw.PreferencesGroup(title="MIDI Input")
        rescan = Gtk.Button(icon_name="view-refresh-symbolic", tooltip_text="Scan Again",
                            valign=Gtk.Align.CENTER)
        rescan.add_css_class("flat")
        rescan.connect("clicked", lambda *_: self._request_tick())
        self.midi_group.set_header_suffix(rescan)
        self.midi_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        self.midi_list.add_css_class("boxed-list")
        self.midi_group.add(self.midi_list)
        page.add(self.midi_group)

        buttons_group = Adw.PreferencesGroup()
        buttons = Gtk.Box(spacing=12, halign=Gtk.Align.CENTER)
        self.midi_cancel = _pill("Cancel", "window-close-symbolic")
        self.midi_cancel.connect("clicked", lambda *_: self._show("main"))
        self.midi_continue = _pill("Continue", "emblem-ok-symbolic", "suggested-action")
        self.midi_continue.connect("clicked", self._on_midi_continue)
        buttons.append(self.midi_cancel)
        buttons.append(self.midi_continue)
        buttons_group.add(buttons)
        page.add(buttons_group)
        self.stack.add_named(page, "midi")

    def _fill_midi_list(self, inputs, preselect=None):
        aliases = [m.alias for m in inputs]
        if aliases == self._midi_aliases:
            return                                   # unchanged: keep the user's selection
        selected = self._selected_midi_alias() or preselect
        self._midi_aliases = aliases
        self.midi_list.remove_all()
        self._midi_rows = {}
        group = None
        for m in inputs:
            check = Gtk.CheckButton(group=group)
            group = group or check
            row = _row(m.label, "USB / hardware MIDI" if m.hardware else "Software or wireless MIDI")
            row.add_prefix(check)
            row.set_activatable_widget(check)
            check.connect("toggled", lambda *_: self._update_midi_continue())
            self.midi_list.append(row)
            self._midi_rows[m.alias] = (check, m)
            if m.alias == selected:
                check.set_active(True)
        if not inputs:
            empty = _row("No MIDI devices found",
                         "Connect your piano with a USB cable and switch it on. "
                         "This list updates automatically.", "input-keyboard-symbolic")
            self.midi_list.append(empty)
        self._update_midi_continue()

    def _selected_midi_alias(self):
        return next((a for a, (c, _) in self._midi_rows.items() if c.get_active()), None)

    def _update_midi_continue(self):
        self.midi_continue.set_sensitive(self._selected_midi_alias() is not None)

    def _on_midi_continue(self, *_):
        alias = self._selected_midi_alias()
        if not alias:
            return
        self._set_midi(self._midi_rows[alias][1])
        self._show("main")
        self._refresh_main()
        self._request_tick()

    def _build_main_page(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=24,
                      margin_top=24, margin_bottom=24, margin_start=16, margin_end=16)

        hero = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        icon = Gtk.Image(icon_name="audio-x-generic-symbolic", pixel_size=72)
        icon.add_css_class("dim-label")
        heading = Gtk.Label(label="Piano")
        heading.add_css_class("title-1")
        status_box = Gtk.Box(spacing=8, halign=Gtk.Align.CENTER)
        self.spinner = Adw.Spinner(visible=False)
        self.status_label = Gtk.Label(label="Stopped", wrap=True, justify=Gtk.Justification.CENTER)
        self.status_label.add_css_class("dim-label")
        status_box.append(self.spinner)
        status_box.append(self.status_label)
        for w in (icon, heading, status_box):
            hero.append(w)
        box.append(hero)

        actions = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, halign=Gtk.Align.CENTER)
        self.start_btn = _pill("Start Piano", "media-playback-start-symbolic", "suggested-action")
        self.start_btn.connect("clicked", self._on_start_clicked)
        self.record_btn = _pill("Record MIDI", "media-record-symbolic")
        self.record_btn.connect("clicked", self._on_record_clicked)
        for b in (self.start_btn, self.record_btn):
            b.set_size_request(240, -1)
            actions.append(b)
        box.append(actions)

        group = Adw.PreferencesGroup()
        self.sfz_row = _row("Instrument", "", "audio-x-generic-symbolic")
        change_sfz = Gtk.Button(icon_name="document-open-symbolic", tooltip_text="Change Instrument",
                                valign=Gtk.Align.CENTER)
        change_sfz.add_css_class("flat")
        change_sfz.connect("clicked", self._on_change_sfz)
        self.sfz_row.add_suffix(change_sfz)

        self.midi_row = _row("MIDI Input", "", "input-keyboard-symbolic")
        self.midi_status = Gtk.Label(valign=Gtk.Align.CENTER)
        self.midi_row.add_suffix(self.midi_status)
        self.midi_row.add_suffix(Gtk.Image(icon_name="go-next-symbolic"))
        self.midi_row.set_activatable(True)
        self.midi_row.connect("activated", self._on_change_midi)

        self.out_row = _row("Audio Output", "", "audio-card-symbolic")

        self.rec_row = _row("Recordings", "", "folder-open-symbolic")
        self.rec_row.add_suffix(Gtk.Image(icon_name="go-next-symbolic"))
        self.rec_row.set_activatable(True)
        self.rec_row.connect("activated", lambda *_: self._open_recordings())
        for r in (self.sfz_row, self.midi_row, self.out_row, self.rec_row):
            group.add(r)
        box.append(group)

        scroller = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, vexpand=True)
        scroller.set_child(Adw.Clamp(child=box, maximum_size=480))
        self.stack.add_named(scroller, "main")

    # ------------------------------------------------------------------ main page state

    def _refresh_main(self):
        sfz = self.cfg.get("sfz", "")
        self.sfz_row.set_subtitle(Path(sfz).name if sfz else "Not set")
        self.sfz_row.set_tooltip_text(sfz)

        midi = self.state.get("midi")
        label = self.cfg.get("midi_label") or "Not set"
        self.midi_row.set_subtitle(label)
        for c in ("success", "warning"):
            self.midi_status.remove_css_class(c)
        if "midi" in self.state:     # only after the first scan
            self.midi_status.set_label("Connected" if midi else "Not connected")
            self.midi_status.add_css_class("success" if midi else "warning")

        if "sink" in self.state:
            self.out_row.set_subtitle(self.state["sink"] or "No audio output found")
        else:
            self.out_row.set_subtitle("Detecting…")
        try:
            n = sum(1 for _ in be.RECORDINGS_DIR.glob("*.mid"))
        except OSError:
            n = 0
        self.rec_row.set_subtitle(f"{n} recording{'s' if n != 1 else ''}")

        st = self.engine_state
        self.spinner.set_visible(st in ("starting", "stopping"))
        if st == "stopped":
            _set_pill(self.start_btn, "Start Piano", "media-playback-start-symbolic",
                      add=("suggested-action",), remove=("destructive-action",))
        elif st == "running":
            _set_pill(self.start_btn, "Stop Piano", "media-playback-stop-symbolic",
                      add=("destructive-action",), remove=("suggested-action",))

        if self.recorder.running:
            self._update_record_timer()          # the timer owns the status line while recording
        elif st == "running":
            where = self.state.get("sink") or "your audio output"
            self.status_label.set_label(
                f"Ready — playing through {where} · {LATENCY_MS:.1f} ms" if midi
                else "Running — waiting for your piano. Check the USB cable.")
        else:
            self.status_label.set_label({"starting": "Starting…", "stopping": "Stopping…"}.get(st, "Stopped"))
        self.start_btn.set_sensitive(st in ("stopped", "running"))

        if self.recorder.running:
            _set_pill(self.record_btn, "Stop Recording", "media-playback-stop-symbolic",
                      add=("destructive-action",))
            self.record_btn.set_sensitive(True)
        else:
            _set_pill(self.record_btn, "Record MIDI", "media-record-symbolic",
                      remove=("destructive-action",))
            can = bool(midi and midi.alsa_addr)
            self.record_btn.set_sensitive(can)
            self.record_btn.set_tooltip_text(None if can else "Connect your piano to record")

    # ------------------------------------------------------------------ periodic graph scan

    def _request_tick(self):
        self._tick(force=True)

    def _tick(self, force=False):
        if self._closing:
            return False
        if not self._tick_busy:
            self._tick_busy = True
            alias, link = self.cfg.get("midi"), self.engine.running
            threading.Thread(target=self._tick_worker, args=(alias, link), daemon=True).start()
        return force is False     # keep the periodic timer, don't duplicate it on forced ticks

    def _tick_worker(self, alias, link):
        try:
            g = be.Graph.snapshot()
            state = be.sync_links(g, alias) if link else \
                {"engine_ready": False, "midi": g.find_midi(alias), "sink": g.sink_label()}
            state["inputs"] = g.midi_inputs()
        except Exception as e:                        # pw-dump missing/hung: report, keep going
            state = {"error": str(e)}
        GLib.idle_add(self._on_tick, state)

    def _on_tick(self, state):
        self._tick_busy = False
        if self._closing or "error" in state:
            return False
        was_connected = self.state.get("midi") is not None
        self.state = state

        # engine lifecycle
        if self.engine_state in ("starting", "running") and not self.engine.running:
            tail = self.engine.log_tail()
            self.engine.stop()
            self.engine_state = "stopped"
            self._error("The piano engine stopped unexpectedly.", tail)
        elif self.engine_state == "starting":
            if state["engine_ready"]:
                self.engine_state = "running"
            elif time.monotonic() - self.engine.started_at > 60:
                tail = self.engine.log_tail()
                self._stop_engine()
                self._error("The piano engine didn't start in time.", tail)

        # MIDI device came or went
        if self.engine_state == "running" and "midi" in state:
            if was_connected and not state["midi"]:
                self.toasts.add_toast(Adw.Toast(title="Piano disconnected — check the USB cable"))
            elif not was_connected and state["midi"] and self._seen_tick:
                self.toasts.add_toast(Adw.Toast(title="Piano reconnected", timeout=2))
        self._seen_tick = True

        # recorder died on its own (usually: device unplugged)
        if self.recorder.proc is not None and not self.recorder.running:
            err = self.recorder.error()
            saved = self.recorder.stop()
            self._error("Recording stopped.", (err + "\n" if err else "") +
                        (f"Saved what was recorded to {saved.name}." if saved else "Nothing was saved."))

        if self.stack.get_visible_child_name() == "midi":
            self._fill_midi_list(state.get("inputs", []))
        self._refresh_main()
        return False

    _seen_tick = False

    # ------------------------------------------------------------------ engine

    def _on_start_clicked(self, *_):
        if self.engine_state == "stopped":
            try:
                self.engine.start(self.cfg["sfz"])
            except OSError as e:
                self._error("Couldn't start the piano engine.", str(e))
                return
            self.engine_state = "starting"
            self._refresh_main()
            self._request_tick()
        elif self.engine_state == "running":
            self._stop_engine()

    def _stop_engine(self, then=None):
        self.engine_state = "stopping"
        self._refresh_main()

        def work():
            self.engine.stop()
            GLib.idle_add(done)

        def done():
            self.engine_state = "stopped"
            self._refresh_main()
            if then:
                then()
            return False

        threading.Thread(target=work, daemon=True).start()

    # ------------------------------------------------------------------ recording

    def _on_record_clicked(self, *_):
        if self.recorder.running:
            saved = self.recorder.stop()
            self._refresh_main()
            if saved:
                toast = Adw.Toast(title=f"Saved {saved.name}", button_label="Show", timeout=6)
                toast.connect("button-clicked", lambda *_: self._open_recordings())
                self.toasts.add_toast(toast)
            else:
                self.toasts.add_toast(Adw.Toast(title="Nothing was recorded"))
            return
        midi = be.Graph.snapshot().find_midi(self.cfg.get("midi"))
        if not midi or not midi.alsa_addr:
            self._error("Your piano isn't connected.", "Connect it with a USB cable and try again.")
            return
        try:
            self.recorder.start(midi.alsa_addr)
        except OSError as e:
            self._error("Couldn't start recording.", str(e))
            return
        self._refresh_main()

    def _update_record_timer(self):
        if self._closing:
            return False
        if self.recorder.running:
            secs = int(time.monotonic() - self.recorder.started_at)
            self.status_label.set_label(f"● Recording  {secs // 60:02d}:{secs % 60:02d}")
        return True

    def _open_recordings(self):
        be.RECORDINGS_DIR.mkdir(parents=True, exist_ok=True)
        Gtk.FileLauncher.new(Gio.File.new_for_path(str(be.RECORDINGS_DIR))).launch(self, None, None)

    # ------------------------------------------------------------------ settings

    def _choose_sfz(self, first_boot):
        filt = Gtk.FileFilter(name="SFZ instruments")
        filt.add_suffix("sfz")
        filters = Gio.ListStore.new(Gtk.FileFilter)
        filters.append(filt)
        dialog = Gtk.FileDialog(title="Choose the Salamander Grand Piano SFZ file",
                                filters=filters, default_filter=filt)
        current = self.cfg.get("sfz")
        if current and Path(current).parent.is_dir():
            dialog.set_initial_folder(Gio.File.new_for_path(str(Path(current).parent)))
        dialog.open(self, None, self._on_sfz_chosen, first_boot)

    def _on_sfz_chosen(self, dialog, result, first_boot):
        try:
            f = dialog.open_finish(result)
        except GLib.Error:
            return                                     # cancelled
        path = f.get_path()
        err = be.validate_sfz(path) if path else "Please choose a local file."
        if err:
            self._error("That file can't be used.", err)
            return
        self.cfg["sfz"] = path
        be.save_config(self.cfg)
        if first_boot:
            self._route()
            return
        self._refresh_main()
        if self.engine_state == "running":
            self._stop_engine(then=lambda: self._on_start_clicked())
            self.toasts.add_toast(Adw.Toast(title="Instrument changed — restarting the piano", timeout=3))

    def _on_change_sfz(self, *_):
        self._choose_sfz(first_boot=False)

    def _on_change_midi(self, *_):
        self._detect_midi(first_boot=False)

    def _on_about(self, *_):
        Adw.AboutDialog(application_name="Piano", application_icon="audio-x-generic-symbolic",
                        version="1.0",
                        comments="Plays the Salamander Grand Piano through a hidden Carla + sfizz "
                                 "engine and records your performances as MIDI.").present(self)

    def _error(self, heading, body=""):
        dialog = Adw.AlertDialog(heading=heading, body=body)
        dialog.add_response("ok", "OK")
        dialog.present(self)

    # ------------------------------------------------------------------ shutdown

    def _on_close_request(self, *_):
        if self._closing:
            return False
        if self.recorder.running:
            dialog = Adw.AlertDialog(heading="Stop Recording?",
                                     body="A recording is in progress. It will be saved before Piano quits.")
            dialog.add_response("cancel", "Cancel")
            dialog.add_response("quit", "Save and Quit")
            dialog.set_response_appearance("quit", Adw.ResponseAppearance.SUGGESTED)
            dialog.connect("response", lambda _d, r: r == "quit" and self.shutdown())
            dialog.present(self)
            return True
        self.shutdown()
        return True

    def shutdown(self):
        """Stop recording and the hidden engine, then quit. Safe to call more than once."""
        if self._closing:
            return
        self._closing = True
        self.set_visible(False)
        self.recorder.stop()
        self.engine.stop()
        self.get_application().quit()


class PianoApp(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.DEFAULT_FLAGS)
        self.window = None

    def do_activate(self):
        if self.window is None:
            self.window = PianoWindow(self)
            for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
                GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, sig, self._on_signal)
            atexit.register(self._last_resort)
        self.window.present()

    def _on_signal(self):
        self.window.shutdown()
        return GLib.SOURCE_REMOVE

    def _last_resort(self):
        # never leave an invisible Carla running behind
        if self.window:
            self.window.recorder.stop()
            self.window.engine.stop()


def main():
    return PianoApp().run(sys.argv)
