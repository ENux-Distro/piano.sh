#!/usr/bin/env bash
# Set up piano.sh on Debian running under WSL2 (Windows 11 / WSLg).
#
# Installs: the GUI dependencies, PipeWire + WirePlumber + ALSA tools, Flatpak, Carla and its sfizz plugin,
# the "Piano in 162" SFZ library (from a magnet link), and piano.sh itself. Audio is bridged to WSLg's
# PulseAudio server so sound comes out of Windows.
#
# Usage:   bash personal/setup-wsl-debian.sh        (run as your normal user, not root; it uses sudo)
# Options (environment variables):
#   SFZ_DIR=~/Emir_Piyano/sfzs   where the piano library is stored
#   SKIP_SFZ=1                   don't download the piano library
set -euo pipefail

MAGNET='magnet:?xt=urn:btih:4c1609b3098a309a32b6ce848dc3711b5182d9c9&dn=Ivy%20Audio%20-%20Piano%20in%20162%20sfz.zip&tr=udp%3a%2f%2ftracker.openbittorrent.com%3a80%2fannounce&tr=udp%3a%2f%2ftracker.publicbt.com%3a80%2fannounce&tr=udp%3a%2f%2ftracker.ccc.de%3a80%2fannounce'
CARLA_APP="studio.kx.carla"
SFIZZ_EXT="org.freedesktop.LinuxAudio.Plugins.sfizz"
SFZ_DIR="${SFZ_DIR:-$HOME/Emir_Piyano/sfzs}"
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

say()  { printf '\n\033[1;34m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33mWARNING: %s\033[0m\n' "$*" >&2; }
die()  { printf '\033[1;31mERROR: %s\033[0m\n' "$*" >&2; exit 1; }

# ---------------------------------------------------------------- checks
[ "$(id -u)" -ne 0 ] || die "Run this as your normal user (it calls sudo itself)."
grep -qi microsoft /proc/version || warn "This doesn't look like WSL; continuing anyway."
command -v apt-get >/dev/null || die "apt-get not found: this script is for Debian/Ubuntu."
sudo -v

# ---------------------------------------------------------------- packages
say "Installing packages"
sudo apt-get update
sudo apt-get install -y --no-install-recommends \
    ca-certificates curl git make unzip ffmpeg aria2 dbus-user-session \
    python3 python3-gi gir1.2-gtk-4.0 gir1.2-adw-1 \
    pipewire pipewire-bin pipewire-pulse pipewire-alsa wireplumber \
    pulseaudio-utils alsa-utils libspa-0.2-modules \
    flatpak

# ---------------------------------------------------------------- WSL config: systemd (needed for PipeWire services)
say "Checking WSL systemd"
if [ "$(ps -p 1 -o comm=)" != "systemd" ]; then
    if ! grep -qs '^\s*systemd\s*=\s*true' /etc/wsl.conf; then
        printf '\n[boot]\nsystemd=true\n' | sudo tee -a /etc/wsl.conf >/dev/null
        warn "Enabled systemd in /etc/wsl.conf. Run 'wsl --shutdown' in Windows, reopen Debian, then run this script again."
        exit 0
    fi
    warn "systemd is enabled in /etc/wsl.conf but not running; run 'wsl --shutdown' from Windows and reopen Debian."
fi

# ---------------------------------------------------------------- PipeWire -> WSLg
# WSLg exposes Windows audio as a PulseAudio server. PipeWire talks to it through a pulse-tunnel sink, and the
# piano app then sees an ordinary PipeWire output.
say "Configuring PipeWire to output through WSLg"
WSLG_PULSE="/mnt/wslg/PulseServer"
[ -S "$WSLG_PULSE" ] || warn "$WSLG_PULSE not found: WSLg audio isn't available (update WSL: 'wsl --update')."
mkdir -p "$HOME/.config/pipewire/pipewire.conf.d"
cat > "$HOME/.config/pipewire/pipewire.conf.d/50-wslg.conf" <<CONF
# Sends audio to Windows through WSLg's PulseAudio server (written by setup-wsl-debian.sh).
context.modules = [
    { name = libpipewire-module-pulse-tunnel
      args = {
          tunnel.mode = sink
          pulse.server.address = "unix:$WSLG_PULSE"
          pulse.latency = 40
          node.name = "wslg-output"
          node.description = "Windows Audio (WSLg)"
          audio.rate = 48000
          audio.channels = 2
          audio.position = [ FL FR ]
          stream.props = { node.latency = 256/48000 }
      }
    }
]
CONF
# Keep the clock at 48 kHz with small buffers so the piano feels responsive.
mkdir -p "$HOME/.config/pipewire/pipewire.conf.d"
cat > "$HOME/.config/pipewire/pipewire.conf.d/60-piano-latency.conf" <<'CONF'
context.properties = {
    default.clock.rate          = 48000
    default.clock.allowed-rates = [ 48000 ]
    default.clock.quantum       = 256
    default.clock.min-quantum   = 64
}
CONF

say "Starting PipeWire user services"
if systemctl --user show-environment >/dev/null 2>&1; then
    systemctl --user daemon-reload
    systemctl --user enable pipewire.socket pipewire-pulse.socket wireplumber.service >/dev/null 2>&1 || true
    systemctl --user restart pipewire pipewire-pulse wireplumber || warn "Couldn't start PipeWire via systemd."
else
    warn "No systemd user session; PipeWire isn't started automatically. Run 'wsl --shutdown' and reopen Debian."
fi
sleep 2
if command -v wpctl >/dev/null && wpctl status >/dev/null 2>&1; then
    # make the Windows output the default, so the app's "System Default" works
    SINK_ID="$(wpctl status | awk '/Windows Audio \(WSLg\)/ {gsub(/[^0-9]/,"",$2); print $2; exit}')"
    [ -n "${SINK_ID:-}" ] && wpctl set-default "$SINK_ID" || warn "WSLg output not visible in PipeWire yet; choose it in the app's Audio Output."
else
    warn "PipeWire isn't answering yet."
fi

# ---------------------------------------------------------------- Flatpak, Carla, sfizz
say "Installing Carla and sfizz (Flatpak)"
flatpak remote-add --user --if-not-exists flathub https://dl.flathub.org/repo/flathub.flatpakrepo
flatpak install --user -y flathub "$CARLA_APP"
BRANCH="$(flatpak info "$CARLA_APP" | sed -nE 's/^\s*Runtime:\s*\S+\/[^/]+\/(([0-9.]+-)?)([0-9.]+)\s*$/\3/p' | head -n1)"
[ -n "$BRANCH" ] || die "Couldn't work out Carla's runtime branch (needed for the sfizz plugin)."
flatpak install --user -y flathub "${SFIZZ_EXT}//${BRANCH}"

# ---------------------------------------------------------------- Piano in 162 via magnet link
if [ "${SKIP_SFZ:-0}" != "1" ]; then
    say "Downloading Piano in 162 (Ivy Audio) into $SFZ_DIR"
    mkdir -p "$SFZ_DIR"
    if compgen -G "$SFZ_DIR/*/IvyAudio-PianoIn162-*.sfz" >/dev/null; then
        echo "Already present, skipping."
    else
        DL="$(mktemp -d)"
        # stop seeding as soon as it's finished; keep it from running forever if there are no peers
        timeout 3600 aria2c --dir="$DL" --seed-time=0 --bt-save-metadata=true --follow-torrent=true \
            --summary-interval=30 "$MAGNET" || warn "Download failed or timed out: no peers? Re-run later, or place the library in $SFZ_DIR yourself."
        for z in "$DL"/*.zip "$DL"/*/*.zip; do
            [ -f "$z" ] && unzip -q -o "$z" -d "$SFZ_DIR"
        done
        # when the torrent delivers a plain folder instead of a zip
        for d in "$DL"/*/; do
            [ -d "$d" ] && compgen -G "$d/*.sfz" >/dev/null && cp -rn "$d" "$SFZ_DIR/"
        done
        rm -rf "$DL"
    fi
    compgen -G "$SFZ_DIR/*/IvyAudio-PianoIn162-*.sfz" >/dev/null || compgen -G "$SFZ_DIR/IvyAudio-PianoIn162-*.sfz" >/dev/null \
        || warn "Piano in 162 isn't in $SFZ_DIR yet. Pick any .sfz file in the app instead."
fi

# ---------------------------------------------------------------- install piano.sh
say "Installing piano.sh"
make -C "$REPO_DIR" install
case ":$PATH:" in *":$HOME/.local/bin:"*) ;; *)
    echo 'export PATH="$HOME/.local/bin:$PATH"' >> "$HOME/.profile"
    warn "Added ~/.local/bin to PATH in ~/.profile; reopen the terminal." ;;
esac

# ---------------------------------------------------------------- MIDI check
say "Checking for MIDI support"
if [ ! -e /dev/snd/seq ]; then
    sudo modprobe snd-seq 2>/dev/null || true
    sudo modprobe snd-seq-midi 2>/dev/null || true
    sudo modprobe snd-usb-audio 2>/dev/null || true
fi
if [ -e /dev/snd/seq ]; then
    echo "ALSA MIDI is available."
else
    warn "No ALSA MIDI device (/dev/snd/seq). The stock WSL2 kernel has no USB-MIDI/ALSA sequencer support, so the app
         will not see your Kawai until you use a kernel that has CONFIG_SND_SEQUENCER and CONFIG_SND_USB_AUDIO.
         Cable it with usbipd-win (Windows):  usbipd list ; usbipd bind --busid <ID> ; usbipd attach --wsl --busid <ID>
         Everything else (sound, Carla, SFZ) is set up and will work once MIDI is available."
fi

say "Done"
cat <<MSG
Start the app with:  piano-gui   (or from the app menu)
In the app, pick the .sfz under $SFZ_DIR and set Audio Output to "Windows Audio (WSLg)" if it isn't the default.
MSG
