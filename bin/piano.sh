#!/bin/bash

ROOT="$(dirname "$(dirname "$(readlink -f "$0")")")"
PROJECT_FILE="$ROOT/data/piano.carxp"
# Same place as the GUI
RECORDINGS="${XDG_DATA_HOME:-$HOME/.local/share}/piano/recordings"

# Carla/sfizz node and port names (see piano.carxp)
NODE="SalamanderGrandPianoV2"
MIDI_IN_SRC="Midi-Bridge:USB func for MIDI: MIDI OUT 1 (capture)"
PLUGIN_MIDI_IN="$NODE:events-in"
PLUGIN_OUT_L="$NODE:Left Output"
PLUGIN_OUT_R="$NODE:Right Output"

# Low-latency buffer: 256 samples @ 48 kHz (~5.3 ms).
# PipeWire reads PIPEWIRE_LATENCY (PW_QUANTUM is not a PipeWire variable).
# Use PIPEWIRE_QUANTUM instead to force 256 even if another app asks for more.
LATENCY="256/48000"

# 1. Launch Carla
echo "Launching Carla with $PROJECT_FILE (buffer $LATENCY)..."
flatpak run --env=PIPEWIRE_LATENCY="$LATENCY" studio.kx.carla "$PROJECT_FILE" &
CARLA_PID=$!

# 2. Wait for the sfizz node's ports to appear (loading samples takes a moment)
echo "Waiting for PipeWire nodes to initialize..."
for _ in $(seq 1 60); do
    pw-link -o | grep -qxF "$PLUGIN_OUT_R" && pw-link -i | grep -qxF "$PLUGIN_MIDI_IN" && break
    sleep 0.5
done
if ! pw-link -o | grep -qxF "$PLUGIN_OUT_R"; then
    echo "ERROR: $NODE ports did not appear. Is Carla running?"
    kill "$CARLA_PID" 2>/dev/null
    exit 1
fi

# 3. Auto-detect active output ports (sink playback ports are input ports)
LEFT_OUT=$(pw-link -i | grep -i "playback_FL\|playback_1" | head -n 1)
RIGHT_OUT=$(pw-link -i | grep -i "playback_FR\|playback_2" | head -n 1)

# 4. Connect MIDI (Carla may already restore it from the project; ignore "exists")
pw-link "$MIDI_IN_SRC" "$PLUGIN_MIDI_IN" 2>/dev/null

# Keep MIDI connected: if the Kawai drops off USB and comes back, relink it.
(
    CONNECTED=1
    while kill -0 "$CARLA_PID" 2>/dev/null; do
        if pw-link -o | grep -qxF "$MIDI_IN_SRC"; then
            if ! pw-link -l | grep -A1 -xF "$MIDI_IN_SRC" | grep -qF "$PLUGIN_MIDI_IN"; then
                pw-link "$MIDI_IN_SRC" "$PLUGIN_MIDI_IN" 2>/dev/null \
                    && echo "[$(date +%T)] Kawai MIDI reconnected."
            fi
            CONNECTED=1
        elif [ "$CONNECTED" = 1 ]; then
            echo "[$(date +%T)] WARNING: Kawai MIDI disconnected (USB). Check the cable/port."
            CONNECTED=0
        fi
        sleep 1
    done
) &

# 5. Connect audio
if [ -n "$LEFT_OUT" ] && [ -n "$RIGHT_OUT" ]; then
    pw-link "$PLUGIN_OUT_L" "$LEFT_OUT" 2>/dev/null
    pw-link "$PLUGIN_OUT_R" "$RIGHT_OUT" 2>/dev/null
    echo "Connected Audio Out -> $LEFT_OUT / $RIGHT_OUT"
else
    pw-link "$PLUGIN_OUT_L" "alsa_output.pci-0000_00_1f.3.analog-stereo:playback_FL" 2>/dev/null
    pw-link "$PLUGIN_OUT_R" "alsa_output.pci-0000_00_1f.3.analog-stereo:playback_FR" 2>/dev/null
fi

echo "Piano ready!"

# 6. MIDI recording mode
if [ "$1" = "midi" ]; then

    mkdir -p "$RECORDINGS"

    # Find Kawai MIDI port
    MIDI_PORT=$(aconnect -l | awk '
        /client [0-9]+: '\''USB func for MIDI'\''/ {
            gsub(/client /, "", $2)
            gsub(/:/, "", $2)
            client=$2
        }
        /USB func for MIDI MIDI OUT 1/ {
            print client ":0"
            exit
        }
    ')

    if [ -z "$MIDI_PORT" ]; then
        echo "ERROR: Kawai MIDI device not found."
        kill "$CARLA_PID" 2>/dev/null
        exit 1
    fi

    echo "Kawai MIDI port: $MIDI_PORT"

    RECORDING="$RECORDINGS/piano-$(date +%Y-%m-%d_%H-%M-%S).mid"

    echo
    echo "MIDI recording enabled."
    echo "Recording to:"
    echo "$RECORDING"
    echo
    echo "Press Ctrl+C to stop recording."

    arecordmidi -p "$MIDI_PORT" "$RECORDING"

    echo
    echo "Recording saved!"
    echo "$RECORDING"

else

    echo "MIDI recording disabled."
    echo "Use './piano.sh midi' to record a MIDI performance."

fi

# 7. Keep terminal open tracking Carla
wait "$CARLA_PID"
