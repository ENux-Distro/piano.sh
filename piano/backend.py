"""Backend for the Piano GUI: config, PipeWire/MIDI discovery, hidden Carla engine, MIDI recorder.

Everything here is plain Python (no GTK) so it can be tested from a terminal.
"""

import json
import os
import re
import shutil
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from xml.sax.saxutils import escape


def _xdg(var, default):
    return Path(os.environ.get(var) or Path.home() / default) / "piano"


if os.environ.get("PIANO_HOME"):                     # everything in one place (tests)
    _config = _data = Path(os.environ["PIANO_HOME"])
    _engine = _config / ".engine"
else:                                                # ~/.config/piano, ~/.local/share/piano, ~/.cache/piano
    _config = _xdg("XDG_CONFIG_HOME", ".config")
    _data = _xdg("XDG_DATA_HOME", ".local/share")
    _engine = _xdg("XDG_CACHE_HOME", ".cache") / "engine"

CONFIG_FILE = _config / "piano-gui.json"
ENGINE_DIR = _engine
PROJECT_FILE = ENGINE_DIR / "piano.carxp"
ENGINE_LOG = ENGINE_DIR / "carla.log"
RECORDINGS_DIR = _data / "recordings"

CARLA_APP = "studio.kx.carla"
SFIZZ_EXTENSION = "org.freedesktop.LinuxAudio.Plugins.sfizz"
PLUGIN_NAME = "Piano"          # Carla plugin name -> PipeWire node "<prefix>.<n>/Piano"
CLIENT_PREFIX = "PianoApp"     # Carla --cnprefix
LATENCY = "256/48000"          # 256 samples @ 48 kHz, ~5.3 ms


# --------------------------------------------------------------------------- config

def load_config():
    try:
        return json.loads(CONFIG_FILE.read_text())
    except (OSError, ValueError):
        return {}


def save_config(cfg):
    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = CONFIG_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(cfg, indent=2) + "\n")
    tmp.replace(CONFIG_FILE)


def validate_sfz(path):
    """Return an error message, or None if the file looks like a usable SFZ instrument."""
    p = Path(path)
    if p.suffix.lower() != ".sfz":
        return "Please choose a .sfz file."
    try:
        with p.open(errors="replace") as f:
            head = f.read(200_000)
    except OSError as e:
        return f"Can't read the file: {e.strerror}."
    if "<region>" not in head and "<group>" not in head:
        return "This file doesn't look like an SFZ instrument."
    return None


# --------------------------------------------------------------------------- prerequisites

def carla_runtime_branch():
    """The freedesktop SDK branch Carla's runtime uses (e.g. '25.08'), or None."""
    try:
        out = subprocess.run(["flatpak", "info", CARLA_APP], capture_output=True, text=True, timeout=15).stdout
    except (OSError, subprocess.TimeoutExpired):
        return None
    m = re.search(r"Runtime:\s*\S+/[^/]+/(?:[\d.]+-)?([\d.]+)\s*$", out, re.M)
    return m.group(1) if m else None


def check_prerequisites():
    """Return a list of (problem, fix-command) tuples; empty when everything is installed."""
    problems = []
    if not shutil.which("flatpak"):
        return [("Flatpak is not installed.", "sudo apt install flatpak")]
    branch = carla_runtime_branch()
    if branch is None:
        problems.append(("Carla (Flatpak) is not installed.",
                         f"flatpak install flathub {CARLA_APP}"))
    else:
        ok = subprocess.run(["flatpak", "info", f"{SFIZZ_EXTENSION}//{branch}"],
                            capture_output=True).returncode == 0
        if not ok:
            problems.append(("The sfizz plugin for Carla is not installed.",
                             f"flatpak install --user flathub {SFIZZ_EXTENSION}//{branch}"))
    for tool, pkg in (("pw-link", "pipewire-bin"), ("pw-dump", "pipewire-bin"), ("arecordmidi", "alsa-utils")):
        if not shutil.which(tool):
            problems.append((f"'{tool}' is missing.", f"sudo apt install {pkg}"))
    return problems


# --------------------------------------------------------------------------- PipeWire graph

@dataclass(frozen=True)
class MidiPort:
    name: str          # pw-link name, e.g. "Midi-Bridge:USB func for MIDI: MIDI OUT 1 (capture)"
    alias: str         # stable id across replugs, e.g. "USB func for MIDI:USB func for MIDI MIDI OUT 1"
    label: str         # human readable
    hardware: bool
    alsa_addr: str     # "24:0" for arecordmidi, or "" if not an ALSA port


class Graph:
    """One snapshot of the PipeWire graph (from pw-dump)."""

    def __init__(self, objects):
        self.nodes, self.ports, self.links, self.default_sink = {}, {}, set(), None
        for o in objects:
            kind = o.get("type", "").rsplit(":", 1)[-1]
            props = ((o.get("info") or {}).get("props")) or {}
            if kind == "Node":
                self.nodes[o["id"]] = props
            elif kind == "Port":
                self.ports[o["id"]] = props
            elif kind == "Link":
                info = o.get("info") or {}
                self.links.add((info.get("output-port-id"), info.get("input-port-id")))
            elif kind == "Metadata":
                for entry in o.get("metadata") or []:
                    if entry.get("key") == "default.audio.sink":
                        val = entry.get("value")
                        self.default_sink = val.get("name") if isinstance(val, dict) else None

    @classmethod
    def snapshot(cls):
        out = subprocess.run(["pw-dump"], capture_output=True, text=True, timeout=10).stdout
        return cls(json.loads(out or "[]"))

    def full_name(self, port_id):
        p = self.ports[port_id]
        return f"{self.nodes.get(p.get('node.id'), {}).get('node.name', '?')}:{p.get('port.name')}"

    def _find(self, pred):
        return [pid for pid, p in self.ports.items() if pred(p, self.nodes.get(p.get("node.id"), {}))]

    # -- MIDI sources
    def midi_inputs(self):
        result = []
        # only real MIDI devices (ALSA/Bluetooth bridges), not other apps' MIDI outputs
        for pid in self._find(lambda p, n: p.get("port.direction") == "out"
                              and n.get("media.class") == "Midi/Bridge"
                              and "midi" in str(p.get("format.dsp", "")).lower()):
            p = self.ports[pid]
            alias = p.get("port.alias") or self.full_name(pid)
            if "Midi Through" in alias:
                continue
            m = re.search(r"client_(\d+):capture_(\d+)", str(p.get("object.path", "")))
            label = re.sub(r"\s*\((capture|playback)\)$", "", str(p.get("port.name", "")))
            if not m:   # non-ALSA bridge (e.g. Bluetooth): port names like "out" say nothing
                node = self.nodes.get(p.get("node.id"), {})
                label = node.get("node.description") or node.get("node.name") or label
                if node.get("device.api") == "bluez5":
                    label = "Bluetooth MIDI"
            result.append(MidiPort(
                name=self.full_name(pid), alias=alias, label=label,
                hardware=bool(p.get("port.physical")) and "api.alsa.card" in p,
                alsa_addr=f"{m.group(1)}:{m.group(2)}" if m else ""))
        return sorted(result, key=lambda m: (not m.hardware, m.label.lower()))

    def find_midi(self, alias):
        return next((m for m in self.midi_inputs() if m.alias == alias), None) if alias else None

    # -- our engine's ports
    def engine_ports(self):
        """{'Left Output': id, 'Right Output': id, 'events-in': id} for our Carla node, or {}."""
        res = {}
        for pid in self._find(lambda p, n: str(n.get("node.name", "")).startswith(CLIENT_PREFIX)
                              and str(n.get("node.name", "")).endswith("/" + PLUGIN_NAME)):
            res[self.ports[pid].get("port.name")] = pid
        return res

    # -- output device
    def sink_ports(self):
        """(FL id, FR id) of the default audio sink, falling back to the first sink."""
        sinks = [nid for nid, n in self.nodes.items() if n.get("media.class") == "Audio/Sink"]
        target = next((nid for nid in sinks if self.nodes[nid].get("node.name") == self.default_sink),
                      sinks[0] if sinks else None)
        if target is None:
            return None
        chans = {p.get("audio.channel"): pid for pid, p in self.ports.items()
                 if p.get("node.id") == target and p.get("port.direction") == "in"}
        fl, fr = chans.get("FL") or chans.get("MONO"), chans.get("FR") or chans.get("MONO")
        return (fl, fr) if fl and fr else None

    def sink_label(self):
        ports = self.sink_ports()
        if not ports:
            return None
        n = self.nodes.get(self.ports[ports[0]].get("node.id"), {})
        return n.get("node.description") or n.get("node.name")


def _pw_link(*args):
    return subprocess.run(["pw-link", *args], capture_output=True, text=True, timeout=5).returncode == 0


def sync_links(graph, midi_alias):
    """Make sure MIDI -> engine -> default sink is wired. Returns a dict describing the state."""
    state = {"engine_ready": False, "midi": graph.find_midi(midi_alias), "sink": graph.sink_label()}
    eng = graph.engine_ports()
    if not {"Left Output", "Right Output", "events-in"} <= eng.keys():
        return state
    state["engine_ready"] = True

    midi = state["midi"]
    if midi:
        src = next(pid for pid, p in graph.ports.items() if graph.full_name(pid) == midi.name)
        if (src, eng["events-in"]) not in graph.links:
            _pw_link(midi.name, graph.full_name(eng["events-in"]))

    sink = graph.sink_ports()
    if sink:
        for out, dst in ((eng["Left Output"], sink[0]), (eng["Right Output"], sink[1])):
            # drop links to other sinks (e.g. after the default device changed)
            for (o, i) in graph.links:
                if o == out and i != dst:
                    _pw_link("-d", graph.full_name(o), graph.full_name(i))
            if (out, dst) not in graph.links:
                _pw_link(graph.full_name(out), graph.full_name(dst))
    return state


# --------------------------------------------------------------------------- hidden Carla engine

PROJECT_TEMPLATE = """<?xml version='1.0' encoding='UTF-8'?>
<!DOCTYPE CARLA-PROJECT>
<CARLA-PROJECT VERSION='2.5'>
 <EngineSettings>
  <ForceStereo>false</ForceStereo>
  <PreferPluginBridges>false</PreferPluginBridges>
  <PreferUiBridges>true</PreferUiBridges>
  <UIsAlwaysOnTop>false</UIsAlwaysOnTop>
  <MaxParameters>200</MaxParameters>
  <UIBridgesTimeout>4000</UIBridgesTimeout>
 </EngineSettings>

 <Transport>
  <BeatsPerMinute>120</BeatsPerMinute>
 </Transport>

 <Plugin>
  <Info>
   <Type>LV2</Type>
   <Name>{name}</Name>
   <URI>http://sfztools.github.io/sfizz</URI>
  </Info>

  <Data>
   <Active>Yes</Active>
   <ControlChannel>1</ControlChannel>
   <Options>0x1f0</Options>
{params}
   <CustomData>
    <Type>http://lv2plug.in/ns/ext/atom#Path</Type>
    <Key>http://sfztools.github.io/sfizz:sfzfile</Key>
    <Value>{sfz}</Value>
   </CustomData>
  </Data>
 </Plugin>
</CARLA-PROJECT>
"""

# (index, name, symbol, value): 256 voices, 32 KB preload, -4 dB headroom against clipping
SFIZZ_PARAMS = [
    (0, "Volume", "volume", -4), (1, "Polyphony", "num_voices", 256),
    (2, "Oversampling factor", "oversampling", 1), (3, "Preload size", "preload_size", 32768),
    (5, "Scala root key", "scala_root_key", 60), (6, "Tuning frequency", "tuning_frequency", 440),
    (7, "Stretched tuning", "stretched_tuning", 0), (8, "Sample quality", "sample_quality", 2),
    (9, "Oscillator quality", "oscillator_quality", 1), (18, "Sustain cancels release", "sustain_cancels_release", 0),
]


class Engine:
    """Carla running headless (--no-gui): no windows, no taskbar entry, nothing on any workspace."""

    def __init__(self):
        self.proc = None
        self.started_at = 0.0

    @property
    def running(self):
        return self.proc is not None and self.proc.poll() is None

    def write_project(self, sfz):
        params = "\n".join(
            f"   <Parameter>\n    <Index>{i}</Index>\n    <Name>{n}</Name>\n"
            f"    <Symbol>{s}</Symbol>\n    <Value>{v}</Value>\n   </Parameter>\n"
            for i, n, s, v in SFIZZ_PARAMS)
        ENGINE_DIR.mkdir(parents=True, exist_ok=True)
        PROJECT_FILE.write_text(PROJECT_TEMPLATE.format(name=PLUGIN_NAME, params=params, sfz=escape(str(sfz))))

    def _cleanup(self):
        # Carla drops a per-run "Carla_<n>/" folder of sample symlinks next to the project
        for d in ENGINE_DIR.glob("Carla_*"):
            shutil.rmtree(d, ignore_errors=True)

    def start(self, sfz):
        if self.running:
            return
        self._cleanup()
        self.write_project(sfz)
        cmd = ["flatpak", "run", f"--env=PIPEWIRE_LATENCY={LATENCY}"]
        home = Path.home().resolve()
        for d in {Path(sfz).resolve().parent, ENGINE_DIR.resolve()}:
            if home not in d.parents and d != home:      # the Carla Flatpak only sees $HOME by default
                cmd.append(f"--filesystem={d}")
        cmd += [CARLA_APP, "--no-gui", "--cnprefix", CLIENT_PREFIX, str(PROJECT_FILE)]
        log = open(ENGINE_LOG, "w")
        # own process group: killing `flatpak run` alone leaves Carla alive inside the sandbox
        self.proc = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT,
                                     stdin=subprocess.DEVNULL, start_new_session=True)
        log.close()
        self.started_at = time.monotonic()

    def stop(self):
        if self.proc is None:
            return
        # `flatpak run` exits at once on SIGTERM while Carla inside the sandbox is still
        # shutting down, so wait for the whole process group to disappear, not just our child.
        pgid = self.proc.pid
        try:
            os.killpg(pgid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        started, killed = time.monotonic(), False
        while self._group_alive(pgid) and time.monotonic() - started < 11:
            if not killed and time.monotonic() - started > 8:
                try:
                    os.killpg(pgid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                killed = True
            time.sleep(0.1)
        self.proc = None
        self._cleanup()

    def _group_alive(self, pgid):
        self.proc.poll()                      # reap our direct child so it doesn't linger as a zombie
        try:
            os.killpg(pgid, 0)
        except ProcessLookupError:
            return False
        # zombies still count for killpg; treat a group of only zombies as gone
        for stat in Path("/proc").glob("[0-9]*/stat"):
            try:
                fields = stat.read_text().rsplit(")", 1)[1].split()
            except OSError:
                continue
            if int(fields[2]) == pgid and fields[0] != "Z":
                return True
        return False

    def log_tail(self, lines=6):
        try:
            text = ENGINE_LOG.read_text(errors="replace").splitlines()
        except OSError:
            return ""
        keep = [l for l in text if not re.search(r"singleton|not available|scheduling parameters", l, re.I)]
        return "\n".join(keep[-lines:])


# --------------------------------------------------------------------------- MIDI recorder

class Recorder:
    def __init__(self):
        self.proc = None
        self.path = None
        self.started_at = 0.0

    @property
    def running(self):
        return self.proc is not None and self.proc.poll() is None

    def start(self, alsa_addr):
        RECORDINGS_DIR.mkdir(parents=True, exist_ok=True)
        self.path = RECORDINGS_DIR / f"piano-{time.strftime('%Y-%m-%d_%H-%M-%S')}.mid"
        self.proc = subprocess.Popen(["arecordmidi", "-p", alsa_addr, str(self.path)],
                                     stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                                     stdin=subprocess.DEVNULL, text=True)
        self.started_at = time.monotonic()
        return self.path

    def stop(self):
        """Stop and finalize the file. Returns the saved path (or None if nothing was saved)."""
        if self.proc is None:
            return None
        if self.proc.poll() is None:
            self.proc.send_signal(signal.SIGINT)     # arecordmidi writes the file on SIGINT
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()
        self.proc = None
        path, self.path = self.path, None
        return path if path and path.exists() and path.stat().st_size > 0 else None

    def error(self):
        """stderr of a recorder that died on its own."""
        if self.proc and self.proc.poll() is not None:
            return (self.proc.stderr.read() or "").strip()
        return ""
