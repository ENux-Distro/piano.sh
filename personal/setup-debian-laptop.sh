#!/usr/bin/env bash
# Set up piano.sh on a real (bare-metal) Debian laptop with a desktop session.
#
# Installs: the GUI dependencies, PipeWire + WirePlumber + ALSA tools, Flatpak, Carla and its sfizz plugin,
# qBittorrent (for downloading the "Piano in 162" SFZ library yourself), and piano.sh itself. Unlike the WSL script there's no
# audio bridge: PipeWire talks to the sound card directly, and USB MIDI works through ALSA.
#
# Usage:   bash personal/setup-debian-laptop.sh      (run as your normal user, not root; it uses sudo)
set -euo pipefail

CARLA_APP="studio.kx.carla"
SFIZZ_EXT="org.freedesktop.LinuxAudio.Plugins.sfizz"
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
NEED_RELOGIN=0

say()  { printf '\n\033[1;34m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33mWARNING: %s\033[0m\n' "$*" >&2; }
die()  { printf '\033[1;31mERROR: %s\033[0m\n' "$*" >&2; exit 1; }

# ---------------------------------------------------------------- checks
[ "$(id -u)" -ne 0 ] || die "Run this as your normal user (it calls sudo itself)."
command -v apt-get >/dev/null || die "apt-get not found: this script is for Debian/Ubuntu."
if grep -qi microsoft /proc/version; then
    die "This is WSL. Use personal/setup-wsl-debian.sh instead."
fi
sudo -v

# ---------------------------------------------------------------- packages
say "Installing packages"
sudo apt-get update
sudo apt-get install -y --no-install-recommends \
    ca-certificates curl git make unzip ffmpeg qbittorrent \
    python3 python3-gi gir1.2-gtk-4.0 gir1.2-adw-1 \
    pipewire pipewire-bin pipewire-pulse pipewire-alsa wireplumber \
    pipewire-audio-client-libraries pulseaudio-utils alsa-utils rtkit \
    flatpak

# ---------------------------------------------------------------- PipeWire as the sound server
say "Switching to PipeWire (replacing PulseAudio if present)"
# PipeWire's pulse server and PulseAudio can't both own the socket
if dpkg -s pulseaudio >/dev/null 2>&1; then
    systemctl --user disable --now pulseaudio.service pulseaudio.socket 2>/dev/null || true
    sudo apt-get remove -y pulseaudio 2>/dev/null || warn "Couldn't remove PulseAudio; remove it manually if sound conflicts."
fi
mkdir -p "$HOME/.config/pipewire/pipewire.conf.d"
# 48 kHz and small buffers so the piano feels responsive (the app asks for 256 samples)
cat > "$HOME/.config/pipewire/pipewire.conf.d/60-piano-latency.conf" <<'CONF'
context.properties = {
    default.clock.rate          = 48000
    default.clock.allowed-rates = [ 48000 ]
    default.clock.quantum       = 256
    default.clock.min-quantum   = 64
}
CONF

if systemctl --user show-environment >/dev/null 2>&1; then
    systemctl --user daemon-reload
    systemctl --user --now enable pipewire.socket pipewire-pulse.socket wireplumber.service 2>&1 | tail -2 || true
    systemctl --user restart pipewire pipewire-pulse wireplumber \
        || warn "Couldn't restart PipeWire; log out and back in."
else
    warn "No systemd user session here (are you in SSH without a login session?). Log in on the laptop and re-run."
fi

# ---------------------------------------------------------------- groups, realtime audio
say "Setting up audio permissions"
for g in audio; do
    if ! id -nG "$USER" | tr ' ' '\n' | grep -qx "$g"; then
        sudo usermod -aG "$g" "$USER" && NEED_RELOGIN=1
    fi
done
# let the audio group use realtime scheduling and lock memory (what jackd/pipewire expect)
if [ ! -f /etc/security/limits.d/95-audio.conf ]; then
    sudo tee /etc/security/limits.d/95-audio.conf >/dev/null <<'CONF'
@audio  -  rtprio   95
@audio  -  memlock  unlimited
CONF
    NEED_RELOGIN=1
fi

sleep 2
if command -v wpctl >/dev/null && wpctl status >/dev/null 2>&1; then
    echo "PipeWire is running."
else
    warn "PipeWire isn't answering yet; it should start at your next login."
fi

# ---------------------------------------------------------------- MIDI (USB piano via ALSA)
say "Enabling ALSA MIDI"
sudo modprobe snd-seq 2>/dev/null || true
sudo modprobe snd-usb-audio 2>/dev/null || true      # includes USB MIDI class devices
printf 'snd-seq\nsnd-usb-audio\n' | sudo tee /etc/modules-load.d/piano-midi.conf >/dev/null
if [ -e /dev/snd/seq ]; then
    echo "ALSA sequencer available."
else
    warn "/dev/snd/seq is missing; reboot, then check 'aconnect -l' with the piano plugged in."
fi

# ---------------------------------------------------------------- Flatpak, Carla, sfizz
say "Installing Carla and sfizz (Flatpak)"
flatpak remote-add --user --if-not-exists flathub https://dl.flathub.org/repo/flathub.flatpakrepo
flatpak install --user -y flathub "$CARLA_APP"
BRANCH="$(flatpak info "$CARLA_APP" | sed -nE 's/^\s*Runtime:\s*\S+\/[^/]+\/(([0-9.]+-)?)([0-9.]+)\s*$/\3/p' | head -n1)"
[ -n "$BRANCH" ] || die "Couldn't work out Carla's runtime branch (needed for the sfizz plugin)."
flatpak install --user -y flathub "${SFIZZ_EXT}//${BRANCH}"

# ---------------------------------------------------------------- install piano.sh
say "Installing piano.sh"
make -C "$REPO_DIR" install
case ":$PATH:" in *":$HOME/.local/bin:"*) ;; *)
    echo 'export PATH="$HOME/.local/bin:$PATH"' >> "$HOME/.profile"
    warn "Added ~/.local/bin to PATH in ~/.profile; log out and back in." ;;
esac

say "Done"
cat <<MSG
Start the app with:  piano-gui   (or from the application menu)
In the app, open qBittorrent to download Piano in 162, pick its .sfz file, plug in the piano's USB cable, and choose your speakers in Audio Output.
MSG
if [ "$NEED_RELOGIN" = 1 ]; then
    warn "You were added to the 'audio' group / realtime limits changed: log out and back in (or reboot) before using the app."
fi
