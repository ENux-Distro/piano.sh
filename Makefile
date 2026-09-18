PREFIX   ?= $(HOME)/.local
BINDIR   ?= $(PREFIX)/bin
DATADIR  ?= $(PREFIX)/share
APPDIR   ?= $(DATADIR)/piano
DESKTOPDIR ?= $(DATADIR)/applications

APP_ID   := io.github.piano.Launcher
BINS     := piano-gui piano.sh
MODULES  := $(wildcard piano/*.py)

.PHONY: help install uninstall

help:
	@echo "make install     install to $(PREFIX) (binaries: $(BINDIR), desktop entry: $(DESKTOPDIR))"
	@echo "make uninstall   remove the installed files (settings and recordings are kept)"

install:
	install -d "$(DESTDIR)$(APPDIR)/piano" "$(DESTDIR)$(APPDIR)/bin" "$(DESTDIR)$(APPDIR)/data" \
		"$(DESTDIR)$(BINDIR)" "$(DESTDIR)$(DESKTOPDIR)"
	install -m 644 $(MODULES) "$(DESTDIR)$(APPDIR)/piano/"
	install -m 755 $(addprefix bin/,$(BINS)) "$(DESTDIR)$(APPDIR)/bin/"
	install -m 644 data/piano.carxp "$(DESTDIR)$(APPDIR)/data/"
	# symlinks, so the launchers can find their install dir through readlink -f
	for b in $(BINS); do ln -sfn "$(APPDIR)/bin/$$b" "$(DESTDIR)$(BINDIR)/$$b"; done
	sed "s|^Exec=.*|Exec=$(BINDIR)/piano-gui|" data/piano.desktop > "$(DESTDIR)$(DESKTOPDIR)/$(APP_ID).desktop"
	chmod 644 "$(DESTDIR)$(DESKTOPDIR)/$(APP_ID).desktop"
	-command -v update-desktop-database >/dev/null && update-desktop-database -q "$(DESTDIR)$(DESKTOPDIR)"

uninstall:
	for b in $(BINS); do rm -f "$(DESTDIR)$(BINDIR)/$$b"; done
	rm -f "$(DESTDIR)$(DESKTOPDIR)/$(APP_ID).desktop"
	rm -rf "$(DESTDIR)$(APPDIR)/piano" "$(DESTDIR)$(APPDIR)/bin" "$(DESTDIR)$(APPDIR)/data"
	# recordings live in $(APPDIR)/recordings: only remove the dir if nothing else is left
	-rmdir "$(DESTDIR)$(APPDIR)" 2>/dev/null
	-command -v update-desktop-database >/dev/null && update-desktop-database -q "$(DESTDIR)$(DESKTOPDIR)"
