"""Qt operator console. IPC and artwork loading stay off the GUI thread."""

import copy
import json
import sys
import uuid
from concurrent.futures import ThreadPoolExecutor

from PySide6.QtCore import QDateTime, QLocale, QObject, QProcess, QRectF, QSize, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QColor, QIcon, QPainter, QPalette, QPixmap
from PySide6.QtNetwork import QNetworkAccessManager, QNetworkRequest
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QToolButton, QComboBox, QDialog, QFileDialog, QFormLayout, QFrame,
    QGridLayout, QHBoxLayout, QInputDialog, QLabel, QLineEdit, QMainWindow, QMenu,
    QMessageBox, QPushButton, QScrollArea, QSizePolicy, QSlider, QSpinBox, QStyle, QSystemTrayIcon,
    QTabWidget, QTextEdit, QVBoxLayout, QWidget,
)

from .config import MUSIC, SOURCES, new_route, new_zone, profile_snapshot, validate
from .ipc import request


def button(text, action):
    widget = QPushButton(text)
    widget.clicked.connect(action)
    return widget


def check(text, checked, action=None):
    widget = QCheckBox(text)
    widget.setChecked(checked)
    if action:
        widget.toggled.connect(action)
    return widget


CONTROL_ICONS = {
    "speaker": '<path d="M5 12h5l7-6v20l-7-6H5zM21 11a8 8 0 0 1 0 10m4-14a13 13 0 0 1 0 18"/>',
    "muted": '<path d="M4 12h5l7-6v20l-7-6H4zM22 12l8 8m0-8-8 8"/>',
    "note": '<path d="M12 23V8l15-3v15M12 12l15-3"/>'
            '<ellipse cx="8" cy="24" rx="4" ry="3"/><ellipse cx="23" cy="21" rx="4" ry="3"/>',
    "spotify": '<circle cx="16" cy="16" r="14" fill="#1685ef" stroke="none"/>'
               '<path stroke="white" d="M8 12q8-3 16 2M9 17q7-2 13 2M11 21q5-1 9 2"/>',
    "airplay": '<path d="M7 23a11 11 0 1 1 18 0M11 20a6 6 0 1 1 10 0M16 20l7 8H9z"/>',
    "music_assistant": '<path d="M4 13v6m6-10v14m6-19v24m6-19v14m6-10v6"/>',
    "local": '<rect x="3" y="5" width="26" height="18" rx="2"/>'
             '<path d="M16 23v5m-7 0h14"/>',
}


def control_icon(key):
    color = QApplication.palette().color(QPalette.ColorRole.Highlight).name()
    svg = (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32" fill="none" '
           f'stroke="{color}" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">'
           f'{CONTROL_ICONS[key]}</svg>')
    pixmap = QPixmap(32, 32)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    QSvgRenderer(svg.encode()).render(painter)
    painter.end()
    return QIcon(pixmap)


def action_tile(key, label, action):
    tile = QToolButton()
    tile.setObjectName("sourceTile")
    tile.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
    tile.setText(label)
    tile.setIcon(control_icon(key))
    tile.setIconSize(QSize(32, 32))
    tile.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextUnderIcon)
    tile.clicked.connect(action)
    return tile


def mute_button(label, muted, action):
    control = QToolButton()
    control.setObjectName("muteButton")
    control.setIconSize(QSize(24, 24))
    control.setCheckable(True)
    control.setChecked(muted)

    def update(checked):
        description = f"{label} · {'Demping opheffen' if checked else 'Dempen'}"
        control.setIcon(control_icon("muted" if checked else "speaker"))
        control.setAccessibleName(description)
        control.setToolTip(description)

    update(muted)
    control.toggled.connect(update)
    control.toggled.connect(action)
    return control


class Switch(QCheckBox):
    def __init__(self, label, checked, action):
        super().__init__()
        self.setAccessibleName(label)
        self.setToolTip(label)
        self.setFixedSize(48, 30)
        self.setChecked(checked)
        self.toggled.connect(action)

    def hitButton(self, position):
        return self.rect().contains(position)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        palette = QApplication.palette()
        on = self.isChecked()
        painter.setBrush(palette.color(QPalette.ColorRole.Highlight if on else QPalette.ColorRole.Mid))
        painter.drawRoundedRect(QRectF(0, 2, 48, 26), 13, 13)
        painter.setBrush(QColor("#ffffff"))
        painter.drawEllipse(QRectF(26 if on else 4, 6, 18, 18))
        if self.hasFocus():
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(palette.color(QPalette.ColorRole.Highlight))
            painter.drawRoundedRect(QRectF(0.5, 0.5, 47, 29), 14, 14)


def slider(value, action, low=0, high=100, suffix="%"):
    content = QWidget()
    row = QHBoxLayout(content)
    row.setContentsMargins(0, 0, 0, 0)
    control = QSlider(Qt.Orientation.Horizontal)
    control.setRange(low, high)
    control.setValue(value)
    label = QLabel(f"{value}{suffix}")
    label.setMinimumWidth(50)
    control.valueChanged.connect(lambda val: label.setText(f"{val}{suffix}"))
    control.valueChanged.connect(action)
    row.addWidget(control, 1)
    row.addWidget(label)
    return content


def spin(value, high, action=None, low=0, suffix=" ms"):
    control = QSpinBox()
    control.setRange(low, high)
    control.setValue(value)
    control.setSuffix(suffix)
    if action:
        control.valueChanged.connect(action)
    return control


def card(title):
    frame = QFrame()
    frame.setObjectName("card")
    layout = QVBoxLayout(frame)
    layout.setContentsMargins(16, 14, 16, 14)
    layout.setSpacing(10)
    label = QLabel(title)
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setObjectName("cardTitle")
    layout.addWidget(label)
    return frame, layout


def scroll(widget):
    area = QScrollArea()
    area.setWidgetResizable(True)
    area.setFrameShape(QFrame.Shape.NoFrame)
    area.setWidget(widget)
    return area


def device_combo(devices, selected=""):
    combo = QComboBox()
    for key, description in devices.items():
        combo.addItem(description, key)
    if selected and selected not in devices:
        combo.addItem(f"Niet verbonden · {selected}", selected)
    if selected:
        combo.setCurrentIndex(combo.findData(selected))
    return combo


class Bridge(QObject):
    finished = Signal(str, object, object)

    def __init__(self):
        super().__init__()
        self.pool = ThreadPoolExecutor(max_workers=1)

    def submit(self, action, **params):
        future = self.pool.submit(request, action, **params)

        def deliver(result):
            error = result.exception()
            self.finished.emit(action, None if error else result.result(), error)

        future.add_done_callback(deliver)


class Window(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Scouting Audio Manager")
        self.resize(1440, 880)
        self.setMinimumSize(820, 540)
        self.bridge = Bridge()
        self.bridge.finished.connect(self.received)
        self.info = {}
        self.cfg = None
        self.revision = 0
        self.instance = None
        self.busy = False
        self.dirty = False
        self.rebuild = False
        self.commands = []
        self.notice = ""
        self.zone_labels = {}
        self.active_sources = None
        self.source_labels = {}
        self.service_buttons = {}
        self.now = None
        self.network = QNetworkAccessManager(self)
        self.art_url = ""
        self.art_image = QPixmap()
        self.current_page = 0
        self.base_palette = QApplication.palette()
        root = QWidget()
        layout = QVBoxLayout(root)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        header = QFrame()
        header.setObjectName("header")
        top = QHBoxLayout(header)
        top.setContentsMargins(24, 12, 24, 12)
        logo = QLabel("⚜")
        logo.setObjectName("logo")
        top.addWidget(logo)
        title = QLabel("Scouting Audio Manager")
        title.setObjectName("appTitle")
        top.addWidget(title, 1)
        self.status = QLabel("Audiomixer verbinden…")
        self.status.setObjectName("status")
        top.addWidget(self.status)
        self.date = QLabel()
        top.addWidget(self.date)
        layout.addWidget(header)

        body = QHBoxLayout()
        body.setSpacing(0)
        sidebar = QFrame()
        sidebar.setObjectName("sidebar")
        sidebar.setFixedWidth(178)
        navigation = QVBoxLayout(sidebar)
        navigation.setContentsMargins(10, 24, 10, 16)
        self.navigation = []
        self.tabs = QTabWidget()
        self.tabs.tabBar().hide()
        self.tabs.currentChanged.connect(self.change_page)
        for index, (icon, name) in enumerate((("▦", "Dashboard"), ("☷", "Routing"),
                                                ("◖", "Zones"), ("⚙", "Instellingen"))):
            control = button(f"{icon}   {name}", lambda _, i=index: self.select_page(i))
            control.setObjectName("navigation")
            control.setCheckable(True)
            self.navigation.append(control)
            navigation.addWidget(control)
            self.tabs.addTab(QWidget(), name)
        self.change_page(0)
        navigation.addStretch()
        body.addWidget(sidebar)
        body.addWidget(self.tabs, 1)
        layout.addLayout(body, 1)
        self.setCentralWidget(root)
        self.tray = QSystemTrayIcon(QIcon.fromTheme(
            "scouting-audio-manager", self.style().standardIcon(QStyle.StandardPixmap.SP_MediaVolume)), self)
        self.setWindowIcon(self.tray.icon())
        self.tray.activated.connect(self.show_from_tray)
        menu = QMenu(self)
        menu.addAction("Dashboard openen", self.showNormal)
        menu.addAction("Afsluiten", self.quit_app)
        self.tray.setContextMenu(menu)
        self.poll_timer = QTimer(self, interval=700)
        self.poll_timer.timeout.connect(self.pump)
        self.poll_timer.start()
        self.save_timer = QTimer(self, interval=140, singleShot=True)
        self.save_timer.timeout.connect(self.pump)
        self.pump()

    def select_page(self, index):
        self.tabs.setCurrentIndex(index)
        self.change_page(index)

    def change_page(self, index):
        if 0 <= index < len(self.navigation):
            self.current_page = index
            for page, control in enumerate(self.navigation):
                control.setChecked(page == index)

    def set_value(self, path, value, rebuild=False):
        parent = self.cfg
        for segment in path[:-1]:
            parent = parent[segment]
        parent[path[-1]] = value
        self.changed(rebuild)

    def changed(self, rebuild=False):
        self.dirty = True
        self.rebuild |= rebuild
        self.save_timer.start()

    def command(self, action, **params):
        self.commands.append((action, params))
        self.pump()

    def pump(self):
        if self.busy:
            return
        self.busy = True
        if self.dirty:
            self.dirty = False
            self.bridge.submit("save", config=copy.deepcopy(self.cfg), revision=self.revision,
                               instance=self.instance)
        elif self.commands:
            action, params = self.commands.pop(0)
            self.bridge.submit(action, **params)
        else:
            self.bridge.submit("state")

    def received(self, action, payload, error):
        self.busy = False
        if error:
            self.notice = str(error)
            self.status.setText(self.notice)
            if action == "save":
                self.cfg = None
                self.dirty = False
                self.rebuild = True
            return
        if action in ("web_settings", "web_configure"):
            self.web_enabled.setChecked(payload["enabled"])
            self.web_password.setText(payload["password"])
            self.web_url.setText(payload["url"] or "Geen lokaal IP-adres gevonden")
            self.web_status.setText("Webbediening actief" if payload["running"] else "Webbediening uit")
            return
        if action == "logs":
            self.show_logs(payload)
            return
        first = self.cfg is None
        changed_from_elsewhere = (action != "save" and
                                  (payload["revision"], payload["instance"]) != (self.revision, self.instance)
                                  and not self.dirty)
        if first or changed_from_elsewhere or action == "save":
            self.revision, self.instance = payload["revision"], payload["instance"]
        if first or changed_from_elsewhere:
            self.cfg = payload["config"]
        if first or changed_from_elsewhere or (action == "save" and self.rebuild and not self.dirty):
            self.rebuild = False
            self.build()
        if action != "state":
            self.notice = ""
        self.info = payload
        self.refresh_labels()
        if self.dirty or self.commands:
            QTimer.singleShot(0, self.pump)

    def build(self):
        self.apply_theme()
        self.zone_labels.clear()
        self.source_labels.clear()
        self.service_buttons.clear()
        selected = self.current_page
        self.tabs.blockSignals(True)
        while self.tabs.count():
            page = self.tabs.widget(0)
            self.tabs.removeTab(0)
            page.deleteLater()
        for name, make in (("Dashboard", self.dashboard), ("Routing", self.routing),
                           ("Zones", self.zones), ("Instellingen", self.settings)):
            self.tabs.addTab(make(), name)
        self.tabs.setCurrentIndex(selected)
        self.tabs.blockSignals(False)
        self.tray.setVisible(self.cfg["tray"] and QSystemTrayIcon.isSystemTrayAvailable())

    def apply_theme(self):
        palette = QPalette(self.base_palette)
        theme = self.cfg["theme"]
        dark = theme == "Donker" or (theme == "Systeem" and
                                     self.base_palette.color(QPalette.ColorRole.Window).lightness() < 128)
        colors = {
            "Window": "#101216" if dark else "#f8f9fb",
            "Base": "#191d23" if dark else "#ffffff",
            "AlternateBase": "#1d222a" if dark else "#f1f3f6",
            "Button": "#20252d" if dark else "#f5f7fa",
            "WindowText": "#f4f6fa" if dark else "#151a22",
            "Text": "#f4f6fa" if dark else "#151a22",
            "ButtonText": "#f4f6fa" if dark else "#151a22",
            "Midlight": "#353b45" if dark else "#dbe0e7",
            "Mid": "#454c57" if dark else "#d6dce5",
            "Highlight": "#238df6" if dark else "#005dc7",
            "HighlightedText": "#ffffff",
            "PlaceholderText": "#acb4c0" if dark else "#596575",
        }
        for role, color in colors.items():
            palette.setColor(getattr(QPalette.ColorRole, role), QColor(color))
        QApplication.setPalette(palette)
        for name in ("header", "sidebar"):
            frame = self.findChild(QFrame, name)
            frame.setStyleSheet(f"QFrame#{name} {{ background: {colors['Base']}; "
                                f"color: {colors['WindowText']}; border-color: {colors['Midlight']}; }}")
            frame.setPalette(palette)
        for control in self.navigation:
            control.setPalette(palette)

    def dashboard(self):
        page = QWidget()
        layout = QHBoxLayout(page)
        layout.setContentsMargins(18, 20, 18, 20)
        layout.setSpacing(16)
        left, right = QVBoxLayout(), QVBoxLayout()
        layout.addLayout(left, 5)
        layout.addLayout(right, 6)
        left.setSpacing(14)
        right.setSpacing(14)
        playing, controls = card("Nu aan het spelen")
        left.addWidget(playing)
        self.art = QLabel("♫")
        self.art.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.art.setFixedSize(144, 144)
        self.art.setObjectName("artwork")
        track = QHBoxLayout()
        track.setSpacing(18)
        track.addWidget(self.art)
        track_text = QVBoxLayout()
        track_text.addStretch()
        track.addLayout(track_text, 1)
        controls.addLayout(track)
        self.track_title = QLabel()
        self.track_title.setObjectName("trackTitle")
        self.track_title.setWordWrap(True)
        self.track_title.setMinimumHeight(62)
        self.track_artist = QLabel()
        for label in (self.track_title, self.track_artist):
            label.setTextFormat(Qt.TextFormat.PlainText)
            track_text.addWidget(label)
        track_text.addStretch()
        self.position = QSlider(Qt.Orientation.Horizontal)
        self.position.sliderReleased.connect(lambda: self.transport("seek", self.position.value()))
        self.clock = QLabel()
        controls.addWidget(self.position)
        self.duration_label = QLabel()
        times = QHBoxLayout()
        times.addWidget(self.clock)
        times.addStretch()
        times.addWidget(self.duration_label)
        controls.addLayout(times)
        transport = QHBoxLayout()
        self.transport_buttons = []
        transport.addStretch()
        for name, action in (("⏮", "previous"), ("▶", "play-pause"), ("⏭", "next")):
            control = button(name, lambda _, op=action: self.transport(op))
            control.setObjectName("play" if action == "play-pause" else "transport")
            control.setAccessibleName({"previous": "Vorige", "play-pause": "Afspelen of pauzeren",
                                       "next": "Volgende"}[action])
            self.transport_buttons.append(control)
            transport.addWidget(control)
        transport.addStretch()
        controls.addLayout(transport)

        frame, actions = card("Audiobronnen")
        left.addWidget(frame)
        sources_row = QHBoxLayout()
        for key in MUSIC:
            control = action_tile(key, SOURCES[key].replace(" ", "\n", 1), lambda _, sid=key: self.toggle_receiver(sid))
            control.setCheckable(True)
            self.service_buttons[key] = control
            sources_row.addWidget(control, 1)
        computer = action_tile("local", "Computer", lambda: self.select_page(1))
        sources_row.addWidget(computer, 1)
        actions.addLayout(sources_row)
        self.active_sources = QLabel()
        self.active_sources.setWordWrap(True)
        self.active_sources.setTextFormat(Qt.TextFormat.PlainText)
        actions.addWidget(self.active_sources)
        quick, quick_box = card("Snelle acties")
        quick_row = QHBoxLayout()
        muted = self.cfg["mute"]
        mute_action = action_tile("speaker" if muted else "muted",
                                  "Demping\nopheffen" if muted else "Alle zones\ndempen", self.mute_all)
        mute_action.setCheckable(True)
        mute_action.setChecked(muted)
        quick_row.addWidget(mute_action, 1)
        quick_row.addWidget(action_tile("note", "Alle zones\naan", self.enable_all), 1)
        quick_box.addLayout(quick_row)
        left.addWidget(quick)
        if self.cfg["profiles"]:
            profiles = QComboBox()
            profiles.addItems(sorted(self.cfg["profiles"]))
            profile_row = QHBoxLayout()
            profile_row.addWidget(profiles, 1)
            profile_row.addWidget(button("Profiel laden", lambda: self.profile("load", profiles.currentText())))
            quick_box.addLayout(profile_row)
        frame, mix = card("Mastervolume")
        right.addWidget(frame)
        master_row = QHBoxLayout()
        master_row.addWidget(slider(self.cfg["master"], lambda v: self.set_value(("master",), v)), 1)
        master_row.addWidget(mute_button("Mastervolume", self.cfg["mute"],
                                         lambda v: self.set_value(("mute",), v, rebuild=True)))
        mix.addLayout(master_row)
        zone_panel, zone_list = card("Zones")
        right.addWidget(zone_panel, 1)
        for zid, zone in self.cfg["zones"].items():
            frame, mixer = card(zone["name"])
            frame.setObjectName("zoneCard")
            zone_list.addWidget(frame)
            status = QLabel()
            self.zone_labels[zid] = status
            mixer.addWidget(status)
            names = [source["name"] for sid, source in self.cfg["sources"].items()
                     if self.cfg["routes"].get(sid, {}).get(zid, {}).get("enabled")]
            sources = QLabel(" + ".join(names) or "Geen bronnen gekoppeld")
            sources.setTextFormat(Qt.TextFormat.PlainText)
            sources.setWordWrap(True)
            mixer.addWidget(sources)
            row = QHBoxLayout()
            row.addWidget(slider(zone["volume"], lambda v, key=zid: self.set_value(("zones", key, "volume"), v)), 1)
            row.addWidget(mute_button(zone["name"], zone["mute"],
                                      lambda v, key=zid: self.set_value(("zones", key, "mute"), v)))
            heading = mixer.itemAt(0).widget()
            mixer.removeWidget(heading)
            zone_header = QHBoxLayout()
            zone_header.addWidget(heading, 1)
            zone_header.addWidget(Switch("Zone aan", zone["enabled"],
                                         lambda v, key=zid: self.set_value(("zones", key, "enabled"), v)))
            mixer.insertLayout(0, zone_header)
            row.addWidget(button("EQ", lambda _, key=zid: self.equalizer(key)))
            mixer.addLayout(row)
        if not self.cfg["zones"]:
            zone_list.addWidget(button("Eerste zone toevoegen", lambda: self.zone_dialog()))
        zone_list.addStretch()
        left.addStretch()
        return scroll(page)

    def profile_controls(self, parent):
        frame, layout = card("Profielen")
        profiles = QComboBox()
        profiles.addItems(sorted(self.cfg["profiles"]))
        layout.addWidget(profiles)
        row = QHBoxLayout()
        row.addWidget(button("Laden", lambda: self.profile("load", profiles.currentText())))
        row.addWidget(button("Opslaan als", lambda: self.profile("save", profiles.currentText())))
        row.addWidget(button("Verwijderen", lambda: self.profile("delete", profiles.currentText())))
        layout.addLayout(row)
        parent.addWidget(frame)

    def profile(self, operation, selected):
        if operation == "save":
            name, ok = QInputDialog.getText(self, "Profiel opslaan", "Profielnaam", text=selected)
            if not ok or not name.strip():
                return
            name = name.strip()
            if name in self.cfg["profiles"] and not self.confirm("Bestaand profiel vervangen?"):
                return
            self.cfg["profiles"][name] = profile_snapshot(self.cfg)
        elif not selected:
            return
        elif operation == "load":
            self.cfg.update(copy.deepcopy(self.cfg["profiles"][selected]))
            self.cfg["mute"] = True
        elif self.confirm(f"Profiel {selected} verwijderen?"):
            del self.cfg["profiles"][selected]
        self.changed(True)

    def routing(self):
        page = QWidget()
        outer = QVBoxLayout(page)
        outer.setContentsMargins(20, 22, 20, 22)
        outer.setSpacing(14)
        heading = QLabel("Routing")
        heading.setObjectName("pageTitle")
        outer.addWidget(heading)
        header = QHBoxLayout()
        header.addWidget(QLabel("Bronnen en bestemmingen"), 1)
        header.addWidget(button("Ingang toevoegen", self.input_dialog))
        outer.addLayout(header)
        matrix = QGridLayout()
        for col, zone in enumerate(self.cfg["zones"].values(), 1):
            matrix.addWidget(QLabel(zone["name"]), 0, col)
            matrix.setColumnMinimumWidth(col, 145)
        for row, (sid, source) in enumerate(self.cfg["sources"].items(), 1):
            frame, inside = card(source["name"])
            inside.addWidget(slider(source["volume"], lambda v, k=sid: self.set_value(("sources", k, "volume"), v)))
            inside.addWidget(check("Bron dempen", source["mute"],
                                   lambda v, k=sid: self.set_value(("sources", k, "mute"), v)))
            status = QLabel()
            self.source_labels[sid] = status
            inside.addWidget(status)
            if source["device"]:
                inside.addWidget(button("Ingang bewerken", lambda _, k=sid: self.input_dialog(k)))
            matrix.addWidget(frame, row, 0)
            for col, zid in enumerate(self.cfg["zones"], 1):
                cell = QFrame()
                cell.setObjectName("card")
                box = QVBoxLayout(cell)
                route = self.cfg["routes"].get(sid, {}).get(zid, new_route())
                apply = lambda prop, value, s=sid, z=zid: self.set_route(s, z, prop, value)
                box.addWidget(check("Verbinden", route["enabled"], lambda v, fn=apply: fn("enabled", v)))
                box.addWidget(slider(route["volume"], lambda v, fn=apply: fn("volume", v)))
                box.addWidget(check("Mute", route["mute"], lambda v, fn=apply: fn("mute", v)))
                delay = spin(route["delay"], 500, lambda v, fn=apply: fn("delay", v))
                delay.setPrefix("Vertraging ")
                box.addWidget(delay)
                matrix.addWidget(cell, row, col)
        outer.addLayout(matrix)
        outer.addStretch()
        return scroll(page)

    def set_route(self, sid, zid, key, value):
        route = self.cfg["routes"].setdefault(sid, {}).setdefault(zid, new_route())
        route[key] = value
        self.changed(key == "enabled")

    def zones(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(20, 22, 20, 22)
        layout.setSpacing(14)
        heading = QLabel("Zones")
        heading.setObjectName("pageTitle")
        layout.addWidget(heading)
        toolbar = QHBoxLayout()
        toolbar.addWidget(button("Zone toevoegen", lambda: self.zone_dialog()))
        toolbar.addWidget(button("Synchronisatiepuls afspelen", self.sync_dialog))
        toolbar.addStretch()
        layout.addLayout(toolbar)
        for zid, zone in self.cfg["zones"].items():
            frame, body = card(zone["name"])
            body.addWidget(QLabel(zone["device"]))
            row = QHBoxLayout()
            row.addWidget(button("Bewerken", lambda _, k=zid: self.zone_dialog(k)))
            row.addWidget(button("Equalizer", lambda _, k=zid: self.equalizer(k)))
            row.addWidget(button("Verwijderen", lambda _, k=zid: self.delete_zone(k)))
            body.addLayout(row)
            layout.addWidget(frame)
        layout.addStretch()
        return scroll(page)

    def settings(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(20, 22, 20, 22)
        layout.setSpacing(14)
        heading = QLabel("Instellingen")
        heading.setObjectName("pageTitle")
        layout.addWidget(heading)
        for sid, settings in self.cfg["services"].items():
            frame, body = card(SOURCES[sid])
            form = QFormLayout()
            name = QLineEdit(settings["name"])
            name.editingFinished.connect(lambda edit=name, k=sid: self.set_text(("services", k, "name"), edit))
            form.addRow("Ontvangernaam", name)
            form.addRow(check("Automatisch starten met de engine", settings["auto"],
                              lambda v, k=sid: self.set_value(("services", k, "auto"), v)))
            if sid == "music_assistant":
                url = QLineEdit(settings["url"])
                url.setPlaceholderText("ws://server:8927/sendspin")
                url.editingFinished.connect(
                    lambda: self.set_text(("services", "music_assistant", "url"), url, True))
                form.addRow("Sendspin-serveradres", url)
            body.addLayout(form)
            layout.addWidget(frame)

        layout.addWidget(QLabel("Google Cast · geen compatibele softwareontvanger opgenomen"))
        frame, body = card("Audio")
        form = QFormLayout()
        form.addRow(check("Eén muziekdienst tegelijk", self.cfg["exclusive"],
                          lambda v: self.set_value(("exclusive",), v)))
        rate = QComboBox()
        rate.addItems(["44100", "48000", "96000"])
        rate.setCurrentText(str(self.cfg["rate"]))
        rate.currentTextChanged.connect(lambda text: self.set_value(("rate",), int(text)))
        form.addRow("Samplerate van de routes", rate)
        form.addRow("Routebuffer", spin(self.cfg["buffer"], 100, lambda v: self.set_value(("buffer",), v), 5))
        form.addRow("Lokale virtuele uitgang", QLabel("Scouting Audio"))
        body.addLayout(form)
        layout.addWidget(frame)

        frame, body = card("Systeem")
        theme = QComboBox()
        theme.addItems(("Systeem", "Licht", "Donker"))
        theme.setCurrentText(self.cfg["theme"])
        theme.currentTextChanged.connect(lambda text: (self.set_value(("theme",), text), self.apply_theme()))
        body.addWidget(theme)
        body.addWidget(check("Sluiten naar systeemvak", self.cfg["tray"],
                             lambda v: self.set_value(("tray",), v, True)))
        body.addWidget(check("Dashboard verborgen openen", self.cfg["background"],
                             lambda v: self.set_value(("background",), v)))
        row = QHBoxLayout()
        row.addWidget(button("Engine automatisch starten", lambda: self.command("autostart", enabled=True)))
        row.addWidget(button("Autostart uitschakelen", lambda: self.command("autostart", enabled=False)))
        body.addLayout(row)
        row = QHBoxLayout()
        row.addWidget(button("Importeren", self.import_config))
        row.addWidget(button("Exporteren", self.export_config))
        row.addWidget(button("Logboek", lambda: self.command("logs")))
        body.addLayout(row)
        layout.addWidget(frame)
        frame, body = card("Webbediening")
        form = QFormLayout()
        self.web_enabled = check("Bedienen via het lokale netwerk", False)
        self.web_url = QLabel()
        self.web_url.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.web_password = QLineEdit()
        self.web_password.setEchoMode(QLineEdit.EchoMode.Password)
        reveal = check("Wachtwoord tonen", False, lambda visible: self.web_password.setEchoMode(
            QLineEdit.EchoMode.Normal if visible else QLineEdit.EchoMode.Password))
        self.web_status = QLabel()
        form.addRow(self.web_enabled)
        form.addRow("Adres voor je telefoon", self.web_url)
        form.addRow("Wachtwoord", self.web_password)
        form.addRow(reveal)
        form.addRow(self.web_status)
        body.addLayout(form)
        body.addWidget(button("Webinstellingen opslaan", lambda: self.command(
            "web_configure", enabled=self.web_enabled.isChecked(), password=self.web_password.text())))
        layout.addWidget(frame)
        QTimer.singleShot(0, lambda: self.command("web_settings"))
        self.profile_controls(layout)
        layout.addStretch()
        return scroll(page)

    def set_text(self, path, edit, allow_empty=False):
        value = edit.text().strip()
        if value or allow_empty:
            self.set_value(path, value)

    def dialog(self, title):
        dialog = QDialog(self)
        dialog.setWindowTitle(title)
        dialog.setMinimumWidth(400)
        return dialog, QFormLayout(dialog)

    def zone_dialog(self, zid=None):
        current = self.cfg["zones"].get(zid, new_zone("", ""))
        dialog, form = self.dialog("Zone bewerken" if zid else "Zone toevoegen")
        name = QLineEdit(current["name"])
        taken = {z["device"] for key, z in self.cfg["zones"].items() if key != zid}
        devices = {k: v for k, v in self.info.get("outputs", {}).items() if k not in taken}
        device = device_combo(devices, current["device"])
        volume = spin(current["volume"], 100, suffix="%")
        delay = spin(current["delay"], 500)
        form.addRow("Naam", name)
        form.addRow("Fysieke uitgang", device)
        form.addRow("Volume", volume)
        form.addRow("Zonevertraging", delay)
        form.addRow(button("Opslaan", dialog.accept))
        if dialog.exec() and name.text().strip() and device.currentData():
            zone = dict(current, name=name.text().strip(), device=device.currentData(),
                        volume=volume.value(), delay=delay.value())
            self.cfg["zones"][zid or uuid.uuid4().hex] = zone
            self.changed(True)

    def input_dialog(self, sid=None):
        source = self.cfg["sources"].get(sid, {"name": "", "volume": 50, "mute": True, "device": ""})
        dialog, form = self.dialog("Audio-ingang")
        name = QLineEdit(source["name"])
        device = device_combo(self.info.get("inputs", {}), source["device"])
        form.addRow("Naam", name)
        form.addRow("Fysieke ingang", device)
        form.addRow(button("Opslaan", dialog.accept))
        if sid:
            def delete():
                if self.confirm("Audio-ingang en bijbehorende routes verwijderen?"):
                    del self.cfg["sources"][sid]
                    self.cfg["routes"].pop(sid, None)
                    self.changed(True)
                    dialog.reject()
            form.addRow(button("Verwijderen", delete))
        if dialog.exec() and name.text().strip() and device.currentData():
            self.cfg["sources"][sid or uuid.uuid4().hex] = dict(
                source, name=name.text().strip(), device=device.currentData())
            self.changed(True)

    def delete_zone(self, zid):
        if self.confirm("Zone en bijbehorende routes verwijderen?"):
            del self.cfg["zones"][zid]
            for row in self.cfg["routes"].values():
                row.pop(zid, None)
            self.changed(True)

    def equalizer(self, zid):
        zone = self.cfg["zones"][zid]
        dialog, form = self.dialog("Equalizer · " + zone["name"])
        form.addRow(check("Equalizer aan", zone["eq"],
                          lambda v: self.set_value(("zones", zid, "eq"), v)))
        controls = []
        for key, label in (("bass", "Laag · 100 Hz"), ("mid", "Midden · 1 kHz"), ("treble", "Hoog · 10 kHz")):
            control = slider(zone[key], lambda v, k=key: self.set_value(("zones", zid, k), v),
                             -12, 12, " dB")
            controls.append(control.findChild(QSlider))
            form.addRow(label, control)
        form.addRow(button("Reset naar neutraal", lambda: [control.setValue(0) for control in controls]))
        form.addRow(button("Gereed", dialog.accept))
        dialog.exec()

    def sync_dialog(self):
        dialog, form = self.dialog("Synchronisatie testen")
        checks = {zid: check(zone["name"], self.info.get("zones", {}).get(zid) == "Verbonden")
                  for zid, zone in self.cfg["zones"].items()}
        for item in checks.values():
            form.addRow(item)
        form.addRow(button("Puls afspelen", dialog.accept))
        if dialog.exec():
            self.command("test", zones=[zid for zid, item in checks.items() if item.isChecked()])

    def refresh_labels(self):
        info = self.info
        summary = "●  Audio actief" if info["active_sources"] else (
            "●  Audio gereed" if info["online"] else "○  Audio offline")
        self.date.setText(QLocale("nl_NL").toString(QDateTime.currentDateTime(), "ddd d MMM HH:mm"))
        details = self.notice or info["message"]
        self.status.setText(summary)
        self.status.setToolTip(details or summary)
        for zid, label in self.zone_labels.items():
            label.setText(info["zones"].get(zid, "Wacht op PipeWire"))
        for key, control in self.service_buttons.items():
            state = info["services"].get(key, "Wacht op engine")
            control.setChecked(info["wanted"].get(key, False))
            control.setToolTip(state)
            control.setAccessibleDescription(state)
        active = [self.cfg["sources"][key]["name"] for key in info.get("active_sources", [])
                  if key in self.cfg["sources"]]
        self.active_sources.setText("Actief: " + (", ".join(active) if active else "Geen audiostream"))
        for key, label in self.source_labels.items():
            source = self.cfg["sources"].get(key)
            if not source:
                continue
            if source["device"]:
                text = "Verbonden" if source["device"] in info["inputs"] else "Niet verbonden"
            elif key in info["suppressed"]:
                text = "Gedempt door muziekprioriteit"
            else:
                text = info["services"].get(key, "Virtuele uitgang · Scouting Audio")
            label.setText(text)
        self.now = info["now"]
        meta = self.now
        current = meta.get("position", 0)
        duration = meta.get("duration", 0)
        self.track_title.setText(meta.get("title") or "Geen muziek geselecteerd")
        self.track_artist.setText(" · ".join(filter(None, (meta.get("artist"),
                                                          SOURCES.get(meta.get("source"))))))
        self.clock.setText(f"{current // 60}:{current % 60:02d}")
        self.duration_label.setText(f"{duration // 60}:{duration % 60:02d}")
        if not self.position.isSliderDown():
            self.position.setRange(0, max(duration, current))
            self.position.setValue(current)
        self.position.setEnabled(bool(duration) and meta.get("source") == "spotify")
        for control in self.transport_buttons:
            control.setEnabled(meta.get("controllable", False))
        self.transport_buttons[1].setText("Ⅱ" if meta.get("playing") else "▶")
        self.update_art(meta.get("art", ""))

    def update_art(self, url):
        if url != self.art_url:
            self.art_url = url
            self.art_image = QPixmap()
            target = QUrl(url)
            if target.isLocalFile():
                self.art_image.load(target.toLocalFile())
            elif target.scheme() == "https" and target.host() == "i.scdn.co":
                self.fetch_spotify_art(url)
            elif target.scheme() in ("https", "http"):
                req = QNetworkRequest(target)
                req.setTransferTimeout(5000)
                reply = self.network.get(req)

                def finish():
                    if self.art_url == url and reply.bytesAvailable() <= 8 * 1024 * 1024:
                        self.art_image.loadFromData(reply.readAll())
                        self.update_art(url)
                    reply.deleteLater()

                reply.finished.connect(finish)
        if self.art_image.isNull():
            self.art.setText("♫")
        else:
            self.art.setPixmap(self.art_image.scaled(
                QSize(144, 144), Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation))

    def fetch_spotify_art(self, url):
        process = QProcess(self)

        def finish(code, status):
            if code == 0 and status == QProcess.ExitStatus.NormalExit and self.art_url == url:
                self.art_image.loadFromData(process.readAllStandardOutput())
                self.update_art(url)
            process.deleteLater()

        process.finished.connect(finish)
        process.errorOccurred.connect(
            lambda error: process.deleteLater() if error == QProcess.ProcessError.FailedToStart else None)
        process.start("curl", ["-4fsS", "--max-time", "5", "--max-filesize", "8388608", "--", url])

    def toggle_receiver(self, key):
        self.command("receiver", source=key, wanted=not self.info.get("wanted", {}).get(key, False))

    def enable_all(self):
        for zone in self.cfg["zones"].values():
            zone["enabled"] = True
        self.changed(True)

    def mute_all(self):
        muted = not self.cfg["mute"]
        self.set_value(("mute",), muted, rebuild=True)
        self.sender().setText("Demping\nopheffen" if muted else "Alle zones\ndempen")
        self.sender().setIcon(control_icon("speaker" if muted else "muted"))

    def transport(self, command, position=None):
        if self.now:
            self.command("transport", source=self.now["source"], command=command, position=position)

    def confirm(self, text):
        return QMessageBox.question(self, "Scouting Audio Manager", text) == QMessageBox.StandardButton.Yes

    def import_config(self):
        path, _ = QFileDialog.getOpenFileName(self, "Configuratie importeren", filter="JSON (*.json)")
        if path:
            try:
                with open(path, encoding="utf-8-sig") as file:
                    imported = validate(json.load(file))
                imported["mute"] = True
                if self.confirm("Huidige configuratie vervangen?"):
                    self.cfg = imported
                    self.changed(True)
            except (ValueError, OSError) as error:
                QMessageBox.warning(self, "Importeren mislukt", str(error))

    def export_config(self):
        path, _ = QFileDialog.getSaveFileName(self, "Configuratie exporteren",
                                              "scouting-audio.json", "JSON (*.json)")
        if path:
            try:
                with open(path, "w", encoding="utf-8") as file:
                    json.dump(self.cfg, file, ensure_ascii=False, indent=2)
            except OSError as error:
                QMessageBox.warning(self, "Exporteren mislukt", str(error))

    def show_logs(self, text):
        dialog = QDialog(self)
        dialog.setWindowTitle("Logboek audiomotor")
        dialog.resize(850, 480)
        layout = QVBoxLayout(dialog)
        view = QTextEdit()
        view.setReadOnly(True)
        view.setPlainText(text)
        layout.addWidget(view)
        dialog.exec()

    def show_from_tray(self, reason):
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            self.showNormal()
            self.raise_()
            self.activateWindow()

    def quit_app(self):
        self.poll_timer.stop()
        if self.dirty or self.busy:
            QTimer.singleShot(200, self.quit_app)
            if self.dirty and not self.busy:
                self.pump()
            return
        QApplication.quit()

    def closeEvent(self, event):
        event.ignore()
        if self.tray.isVisible():
            self.hide()
        else:
            self.quit_app()


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("Scouting Audio Manager")
    app.setStyle("Fusion")
    app.setStyleSheet("""
        QWidget { font-family: "Inter", "sans-serif"; font-size: 14px; }
        QFrame#header, QFrame#sidebar { background: palette(base); }
        QFrame#header { border-bottom: 1px solid palette(midlight); }
        QFrame#sidebar { border-right: 1px solid palette(midlight); }
        QLabel#logo { font-size: 36px; color: palette(highlight); padding-right: 12px; }
        QLabel#status { color: palette(highlight); padding-right: 16px; }
        QLabel#artwork { min-width: 144px; min-height: 144px; }
        QLabel#pageTitle { font-size: 28px; font-weight: 700; }
        QTabWidget::pane { border: 0; }
        QPushButton#navigation { text-align: left; padding: 6px 12px; border: 0; background: transparent; }
        QPushButton#navigation:checked, QPushButton#play {
            background: palette(highlight); color: white; font-weight: 700;
        }
        QPushButton#navigation:hover:!checked { background: palette(alternate-base); }
        QToolButton#sourceTile {
            min-width: 74px; min-height: 76px; max-height: 76px; background: palette(button);
            border: 1px solid palette(midlight); border-radius: 8px;
        }
        QToolButton#sourceTile:hover, QToolButton#muteButton:hover, QToolButton#muteButton:checked {
            border-color: palette(highlight);
        }
        QToolButton#muteButton {
            min-width: 42px; min-height: 38px; border: 1px solid palette(midlight);
            background: palette(button); border-radius: 8px;
        }
        QToolButton#sourceTile:checked { border: 1px solid palette(highlight); color: palette(highlight); }
        QPushButton#play, QPushButton#transport { font-size: 23px; min-width: 44px; min-height: 40px; }
        QPushButton#transport { background: transparent; border: 0; }
        QFrame#zoneCard { background: palette(alternate-base); border: 1px solid palette(midlight);
                          border-radius: 12px; }
        QLabel#appTitle { font-size: 27px; font-weight: 700; }
        QLabel#cardTitle { font-size: 19px; font-weight: 700; }
        QLabel#trackTitle { font-size: 24px; font-weight: 700; }
        QLabel#artwork { background: palette(alternate-base); border-radius: 12px; font-size: 45px; }
        QFrame#card { background: palette(base); border: 1px solid palette(midlight); border-radius: 12px; }
        QPushButton, QComboBox, QSpinBox, QLineEdit { min-height: 32px; padding: 2px 8px; }
        QPushButton, QComboBox, QSpinBox, QLineEdit {
            background: palette(button); border: 1px solid palette(midlight); border-radius: 8px;
        }
        QPushButton:hover, QComboBox:hover { border-color: palette(highlight); }
        QPushButton:disabled { color: palette(mid); }
        QCheckBox { min-height: 28px; }
        QSlider::groove:horizontal { height: 6px; background: palette(midlight); border-radius: 3px; }
        QSlider::sub-page:horizontal { background: palette(highlight); border-radius: 3px; }
        QSlider::handle:horizontal { background: palette(highlight); width: 16px; margin: -5px 0; border-radius: 8px; }
    """)
    window = Window()
    if "--background" in sys.argv:
        def initial_background(action, data, error):
            if data and action == "state" and data["config"]["background"] and window.tray.isVisible():
                window.hide()
            window.bridge.finished.disconnect(initial_background)
        window.bridge.finished.connect(initial_background)
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
