# piano.sh

A Linux utility that turns a digital piano into a convincing acoustic-piano experience by bridging MIDI input into a hidden Carla + sfizz engine and routing the output to your system audio.

> Built for the practical dream of having an acoustic grand piano without the space, cost, or maintenance.

## Why this exists

If you have a digital piano, a MIDI-capable keyboard, or a USB MIDI interface, this project helps you:

- play through a realistic sampled grand piano engine,
- route audio to your speakers or headphones,
- record performances as MIDI,
- keep the setup running from a simple GUI.

This is especially useful on Linux with PipeWire and Carla.

## Features

- MIDI input discovery from hardware devices and USB MIDI bridges
- Automatic PipeWire connection management
- Headless Carla engine with sfizz sample playback
- GUI launcher for selecting an SFZ instrument
- MIDI recording support for live performances
- Installable desktop entry and local bin wrappers

## What it does

The project does three main jobs:

1. Detects your digital piano's MIDI output on PipeWire/ALSA
2. Starts Carla with a generated sfizz project and a selected `.sfz` instrument
3. Wires MIDI into the piano engine and audio out to your default sink

The result is a simple “make my MIDI keyboard sound like a real piano” workflow.

## Requirements

You will need:

- Linux
- PipeWire
- Flatpak
- Carla installed via Flatpak
- sfizz plugin for Carla
- `pipewire-bin` utilities such as `pw-link` and `pw-dump`
- `alsa-utils` for `arecordmidi`
- A MIDI-capable digital piano or keyboard
- An `.sfz` piano sample set such as Salamander Grand Piano V2

## Recommended setup

- A MIDI cable or USB MIDI interface
- A digital piano or MIDI keyboard
- A working PipeWire sound system
- A stereo output device for playback

## Installation

Clone the repo:

```bash
git clone https://github.com/ENux-Distro/piano.sh.git
cd piano.sh
```

Install the project:

```bash
make install
```

This installs the launcher scripts, module files, sample data, and a desktop entry under your local prefix.

To view available targets:

```bash
make help
```

## Usage

Launch the GUI:

```bash
piano-gui
```

The GUI will help you:

- choose an `.sfz` piano file,
- start the hidden engine,
- connect the MIDI interface,
- route sound to the default sink,
- record MIDI if needed.

You can also run the underlying shell script directly:

```bash
./bin/piano.sh
```

To record a MIDI performance:

```bash
./bin/piano.sh midi
```

## Project layout

```text
piano.sh/
├── bin/
│   ├── piano-gui
│   └── piano.sh
├── data/
│   ├── piano.carxp
│   └── piano.desktop
├── piano/
│   ├── __init__.py
│   ├── __main__.py
│   ├── backend.py
│   └── gui.py
├── Makefile
├── .gitignore
└── README.md
```

## How it works

- The GUI lives in `piano/gui.py`
- The backend logic in `piano/backend.py` discovers devices and manages PipeWire links
- The shell script in `bin/piano.sh` launches Carla and keeps the MIDI/audio graph connected
- The sample project is generated dynamically and passed to sfizz inside Carla

## Troubleshooting

### Carla or sfizz is not available

Install the required Flatpak dependencies:

```bash
flatpak install flathub studio.kx.carla
flatpak install --user flathub org.freedesktop.LinuxAudio.Plugins.sfizz
```

### MIDI device not detected

Check that your keyboard or piano is connected and visible to PipeWire/ALSA. Use tools like:

```bash
aconnect -l
pw-link -i
pw-link -o
```

### No audio output

Make sure your default audio sink is available and that PipeWire is running.

### GUI does not start

Ensure Python 3 is installed and the project is installed correctly:

```bash
python3 -m piano
```

## Notes

This project is intentionally simple and focused on a single use case: making digital piano output feel more like an acoustic piano on Linux.

The codebase balances a thin shell entrypoint with a Python backend and GUI so the audio plumbing is easier to reason about and maintain.

## License

This project does not currently declare a license in the repository metadata. If you plan to redistribute or modify it, please check the repository and add an appropriate license before publishing.

## Credits

- Carla
- sfizz
- PipeWire
- Linux audio tooling
- The digital piano community

## Final thought

If you want a grand piano sound without buying a room-sized acoustic instrument, this project is a practical Linux-first workaround.

Use it for fun, for practice, and for the pure joy of turning digital keys into something that feels a little more alive.
