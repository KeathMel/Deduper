#!/usr/bin/env python3
"""
dedupe_gui.py - find duplicate photos, look at them side by side, move the
                ones you do not want.

    python3 dedupe_gui.py

Pick folders to scan, pick where duplicates go, press SCAN. Every group is
shown as thumbnails: the green-bordered one is the keeper, tick the ones you
want gone. Nothing is deleted - files are MOVED, mirroring their original
layout, so you can put one back if you change your mind.

Needs PyQt6 and Pillow:
    sudo pacman -S python-pyqt6 python-pillow
"""
import hashlib
import os
import shutil
import sys

from PyQt6.QtCore import Qt, QThread, pyqtSignal, QSize
from PyQt6.QtGui import QPixmap, QFont
from PyQt6.QtWidgets import (
    QApplication, QWidget, QMainWindow, QVBoxLayout, QHBoxLayout, QGridLayout,
    QLabel, QPushButton, QListWidget, QListWidgetItem, QLineEdit, QSpinBox,
    QCheckBox, QComboBox, QFileDialog, QProgressBar, QScrollArea, QSplitter,
    QFrame, QMessageBox, QSizePolicy,
)

try:
    from PIL import Image
    HAVE_PIL = True
except Exception:
    HAVE_PIL = False

EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif", ".tif", ".tiff",
        ".heic", ".heif", ".avif", ".jfif"}

THUMB = 190

CSS = """
QMainWindow, QWidget { background: #16181d; color: #d8dce3;
    font-family: 'Inter', 'Cantarell', sans-serif; font-size: 13px; }
QLabel#h { color: #8b93a1; font-size: 11px; font-weight: 600;
    letter-spacing: 1px; }
QLabel#big { font-size: 15px; font-weight: 600; color: #eef1f6; }
QLabel#dim { color: #7d8595; font-size: 12px; }
QPushButton { background: #23262d; border: 1px solid #32363f;
    border-radius: 6px; padding: 7px 14px; color: #d8dce3; }
QPushButton:hover { background: #2b2f38; border-color: #434956; }
QPushButton:disabled { color: #5a6070; background: #1d2026; }
QPushButton#go { background: #2f6f4f; border-color: #3d8a63; color: #eafff3;
    font-weight: 600; }
QPushButton#go:hover { background: #38815c; }
QPushButton#danger { background: #7a3030; border-color: #943c3c;
    color: #ffecec; font-weight: 600; }
QPushButton#danger:hover { background: #8c3838; }
QPushButton#danger:disabled { background: #2a2023; border-color: #352a2d;
    color: #6a5a5e; }
QLineEdit, QSpinBox, QComboBox { background: #1b1e24; border: 1px solid #2f333c;
    border-radius: 6px; padding: 6px 8px; color: #d8dce3; }
QLineEdit:focus, QSpinBox:focus { border-color: #4b7fa8; }
QListWidget { background: #1b1e24; border: 1px solid #2f333c;
    border-radius: 6px; padding: 4px; }
QListWidget::item { padding: 7px 8px; border-radius: 4px; }
QListWidget::item:selected { background: #2c3b4c; color: #eaf2fb; }
QProgressBar { background: #1b1e24; border: 1px solid #2f333c;
    border-radius: 6px; height: 8px; text-align: center; }
QProgressBar::chunk { background: #4b7fa8; border-radius: 5px; }
QScrollArea { border: none; background: #16181d; }
QCheckBox { spacing: 7px; }
QFrame#card { background: #1b1e24; border: 1px solid #2f333c;
    border-radius: 8px; }
QFrame#keeper { background: #1b2420; border: 2px solid #3d8a63;
    border-radius: 8px; }
QFrame#sep { background: #262a32; max-height: 1px; }
"""


# ----------------------------------------------------------------- helpers

def human(n):
    n = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return "%.1f %s" % (n, unit)
        n /= 1024.0


def sha256(path, chunk=1 << 20):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            b = f.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def dhash(path, size=8):
    with Image.open(path) as im:
        im = im.convert("L").resize((size + 1, size), Image.LANCZOS)
        px = im.tobytes()
    bits = 0
    for row in range(size):
        base = row * (size + 1)
        for col in range(size):
            bits <<= 1
            if px[base + col] > px[base + col + 1]:
                bits |= 1
    return bits


def pixels(path):
    try:
        with Image.open(path) as im:
            w, h = im.size
        return w * h
    except Exception:
        return 0


def dims(path):
    try:
        with Image.open(path) as im:
            return "%d x %d" % im.size
    except Exception:
        return "?"


# ----------------------------------------------------------------- scanning

class Scanner(QThread):
    progress = pyqtSignal(int, int, str)
    finished_ok = pyqtSignal(list, int)
    failed = pyqtSignal(str)

    def __init__(self, folders, trash, exact_only, threshold, min_kb, keep):
        super().__init__()
        self.folders = folders
        self.trash = os.path.abspath(os.path.expanduser(trash or "."))
        self.exact_only = exact_only
        self.threshold = threshold
        self.min_kb = min_kb
        self.keep = keep
        self._stop = False

    def stop(self):
        self._stop = True

    # -- walking

    def _find(self):
        skip = {os.path.basename(self.trash), "__pycache__", "node_modules"}
        out = []
        for root in self.folders:
            root = os.path.abspath(os.path.expanduser(root))
            for dirpath, dirnames, filenames in os.walk(root):
                if self._stop:
                    return out
                dirnames[:] = [d for d in dirnames
                               if d not in skip and not d.startswith(".")]
                for fn in filenames:
                    if os.path.splitext(fn)[1].lower() not in EXTS:
                        continue
                    p = os.path.join(dirpath, fn)
                    if os.path.abspath(p).startswith(self.trash + os.sep):
                        continue
                    try:
                        if os.path.getsize(p) < self.min_kb * 1024:
                            continue
                    except OSError:
                        continue
                    out.append(p)
        return out

    # -- exact

    def _exact(self, paths):
        by_size = {}
        for p in paths:
            try:
                by_size.setdefault(os.path.getsize(p), []).append(p)
            except OSError:
                pass
        groups, seen = [], set()
        cand = [g for g in by_size.values() if len(g) > 1]
        total = sum(len(g) for g in cand)
        done = 0
        for g in cand:
            if self._stop:
                break
            by_hash = {}
            for p in g:
                done += 1
                if done % 25 == 0:
                    self.progress.emit(done, total, "hashing bytes")
                try:
                    by_hash.setdefault(sha256(p), []).append(p)
                except OSError:
                    pass
            for same in by_hash.values():
                if len(same) > 1:
                    groups.append(same)
                    seen.update(same)
        return groups, seen

    # -- similar

    def _similar(self, paths):
        hashes = {}
        for i, p in enumerate(paths):
            if self._stop:
                break
            if i % 20 == 0:
                self.progress.emit(i, len(paths), "looking at pictures")
            try:
                hashes[p] = dhash(p)
            except Exception:
                pass

        buckets = {}
        for p, h in hashes.items():
            for key in ((0, h >> 48), (1, (h >> 32) & 0xFFFF)):
                buckets.setdefault(key, []).append(p)

        parent = {p: p for p in hashes}

        def root(x):
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        for members in buckets.values():
            if len(members) < 2 or len(members) > 400:
                continue
            for i in range(len(members)):
                for j in range(i + 1, len(members)):
                    a, b = members[i], members[j]
                    ra, rb = root(a), root(b)
                    if ra == rb:
                        continue
                    if bin(hashes[a] ^ hashes[b]).count("1") <= self.threshold:
                        parent[rb] = ra

        clusters = {}
        for p in hashes:
            clusters.setdefault(root(p), []).append(p)
        return [sorted(v) for v in clusters.values() if len(v) > 1]

    # -- keeper

    def _keeper(self, group):
        def score(p):
            try:
                st = os.stat(p)
            except OSError:
                return (0, 0, 0)
            if self.keep == "oldest":
                return (-st.st_mtime, 0, 0)
            return (pixels(p) if HAVE_PIL else 0, -st.st_mtime, st.st_size)
        return max(group, key=score)

    def run(self):
        try:
            self.progress.emit(0, 0, "scanning folders")
            paths = self._find()
            if self._stop:
                self.finished_ok.emit([], 0)
                return

            self.progress.emit(0, 0, "%d images, comparing" % len(paths))
            exact, seen = self._exact(paths)

            similar = []
            if not self.exact_only and HAVE_PIL and not self._stop:
                rest = [p for p in paths if p not in seen]
                similar = self._similar(rest)

            out = []
            for kind, groups in (("EXACT", exact), ("SIMILAR", similar)):
                for g in groups:
                    keep = self._keeper(g)
                    out.append({"kind": kind, "files": g, "keep": keep})
            self.finished_ok.emit(out, len(paths))
        except Exception as e:
            self.failed.emit("%s: %s" % (type(e).__name__, e))


# ----------------------------------------------------------------- thumb card

class Card(QFrame):
    """One image in a group: thumbnail, facts, keep/remove."""

    def __init__(self, path, is_keeper, on_keep, on_toggle, checked):
        super().__init__()
        self.path = path
        self.setObjectName("keeper" if is_keeper else "card")
        self.setFixedWidth(THUMB + 26)
        self.setSizePolicy(QSizePolicy.Policy.Fixed,
                           QSizePolicy.Policy.Maximum)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 12, 12, 12)
        lay.setSpacing(7)

        thumb = QLabel()
        thumb.setFixedSize(THUMB, THUMB)
        thumb.setAlignment(Qt.AlignmentFlag.AlignCenter)
        pm = QPixmap(path)
        if pm.isNull():
            thumb.setText("no preview")
            thumb.setObjectName("dim")
        else:
            thumb.setPixmap(pm.scaled(
                QSize(THUMB, THUMB),
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation))
        lay.addWidget(thumb)

        name = QLabel(os.path.basename(path))
        name.setWordWrap(True)
        f = QFont()
        f.setBold(True)
        name.setFont(f)
        lay.addWidget(name)

        try:
            sz = human(os.path.getsize(path))
        except OSError:
            sz = "?"
        facts = QLabel("%s  ·  %s" % (dims(path) if HAVE_PIL else "", sz))
        facts.setObjectName("dim")
        lay.addWidget(facts)

        d = os.path.dirname(path)
        home = os.path.expanduser("~")
        if d.startswith(home):
            d = "~" + d[len(home):]
        if len(d) > 60:
            d = "..." + d[-57:]
        folder = QLabel(d)
        folder.setObjectName("dim")
        folder.setWordWrap(True)
        folder.setToolTip(path)
        lay.addWidget(folder)

        if is_keeper:
            tag = QLabel("KEEPING THIS ONE")
            tag.setObjectName("h")
            tag.setStyleSheet("color:#6fcf9b;")
            lay.addWidget(tag)
        else:
            cb = QCheckBox("move this one away")
            cb.setChecked(checked)
            cb.toggled.connect(lambda v, p=path: on_toggle(p, v))
            lay.addWidget(cb)
            btn = QPushButton("keep this instead")
            btn.clicked.connect(lambda _, p=path: on_keep(p))
            lay.addWidget(btn)


# ----------------------------------------------------------------- window

class Window(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Photo duplicates")
        self.resize(1280, 820)
        self.groups = []
        self.marked = set()
        self.scanner = None

        root = QWidget()
        self.setCentralWidget(root)
        outer = QVBoxLayout(root)
        outer.setContentsMargins(16, 14, 16, 14)
        outer.setSpacing(12)

        # --- folders row

        top = QHBoxLayout()
        top.setSpacing(16)

        left = QVBoxLayout()
        left.setSpacing(6)
        lbl = QLabel("FOLDERS TO SCAN")
        lbl.setObjectName("h")
        left.addWidget(lbl)
        self.folders = QListWidget()
        self.folders.setFixedHeight(94)
        home = os.path.expanduser("~/Pictures")
        if os.path.isdir(home):
            self.folders.addItem(home)
        left.addWidget(self.folders)
        brow = QHBoxLayout()
        add = QPushButton("add folder")
        add.clicked.connect(self.add_folder)
        rem = QPushButton("remove")
        rem.clicked.connect(self.rem_folder)
        brow.addWidget(add)
        brow.addWidget(rem)
        brow.addStretch(1)
        left.addLayout(brow)
        top.addLayout(left, 3)

        right = QVBoxLayout()
        right.setSpacing(6)
        lbl2 = QLabel("MOVE DUPLICATES INTO")
        lbl2.setObjectName("h")
        right.addWidget(lbl2)
        trow = QHBoxLayout()
        self.trash = QLineEdit(os.path.expanduser("~/Pictures_duplicates"))
        br = QPushButton("browse")
        br.clicked.connect(self.pick_trash)
        trow.addWidget(self.trash)
        trow.addWidget(br)
        right.addLayout(trow)

        orow = QHBoxLayout()
        orow.setSpacing(10)
        self.exact_only = QCheckBox("byte-identical only")
        orow.addWidget(self.exact_only)
        orow.addWidget(QLabel("how alike:"))
        self.threshold = QSpinBox()
        self.threshold.setRange(0, 12)
        self.threshold.setValue(5)
        self.threshold.setToolTip("0 = identical looking, 5 = sane, "
                                  "10 = loose (more false matches)")
        orow.addWidget(self.threshold)
        orow.addWidget(QLabel("skip under KB:"))
        self.min_kb = QSpinBox()
        self.min_kb.setRange(0, 100000)
        self.min_kb.setValue(8)
        orow.addWidget(self.min_kb)
        orow.addWidget(QLabel("keep:"))
        self.keep = QComboBox()
        self.keep.addItems(["best quality", "oldest"])
        orow.addWidget(self.keep)
        orow.addStretch(1)
        right.addLayout(orow)
        top.addLayout(right, 4)

        outer.addLayout(top)

        # --- scan row

        srow = QHBoxLayout()
        srow.setSpacing(12)
        self.scan_btn = QPushButton("SCAN")
        self.scan_btn.setObjectName("go")
        self.scan_btn.setMinimumWidth(120)
        self.scan_btn.clicked.connect(self.start_scan)
        srow.addWidget(self.scan_btn)
        self.bar = QProgressBar()
        self.bar.setTextVisible(False)
        self.bar.hide()
        srow.addWidget(self.bar, 1)
        self.status = QLabel("ready" if HAVE_PIL else
                             "Pillow missing - only byte-identical will work")
        self.status.setObjectName("dim")
        srow.addWidget(self.status, 2)
        outer.addLayout(srow)

        sep = QFrame()
        sep.setObjectName("sep")
        outer.addWidget(sep)

        # --- results

        split = QSplitter(Qt.Orientation.Horizontal)

        lpane = QWidget()
        ll = QVBoxLayout(lpane)
        ll.setContentsMargins(0, 0, 0, 0)
        ll.setSpacing(6)
        gl = QLabel("GROUPS")
        gl.setObjectName("h")
        ll.addWidget(gl)
        self.glist = QListWidget()
        self.glist.currentRowChanged.connect(self.show_group)
        ll.addWidget(self.glist)
        split.addWidget(lpane)

        rpane = QWidget()
        rl = QVBoxLayout(rpane)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.setSpacing(6)
        self.ghead = QLabel("press SCAN")
        self.ghead.setObjectName("big")
        rl.addWidget(self.ghead)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.inner = QWidget()
        self.grid = QGridLayout(self.inner)
        self.grid.setContentsMargins(2, 2, 2, 2)
        self.grid.setSpacing(12)
        self.grid.setAlignment(Qt.AlignmentFlag.AlignTop |
                               Qt.AlignmentFlag.AlignLeft)
        self.scroll.setWidget(self.inner)
        rl.addWidget(self.scroll, 1)
        split.addWidget(rpane)

        split.setSizes([360, 900])
        outer.addWidget(split, 1)

        # --- bottom

        brow2 = QHBoxLayout()
        self.total = QLabel("")
        self.total.setObjectName("dim")
        brow2.addWidget(self.total)
        brow2.addStretch(1)
        self.all_btn = QPushButton("tick every duplicate")
        self.all_btn.clicked.connect(self.tick_all)
        self.all_btn.setEnabled(False)
        brow2.addWidget(self.all_btn)
        self.none_btn = QPushButton("untick all")
        self.none_btn.clicked.connect(self.tick_none)
        self.none_btn.setEnabled(False)
        brow2.addWidget(self.none_btn)
        self.move_btn = QPushButton("MOVE 0 FILES")
        self.move_btn.setObjectName("danger")
        self.move_btn.setMinimumWidth(180)
        self.move_btn.setEnabled(False)
        self.move_btn.clicked.connect(self.do_move)
        brow2.addWidget(self.move_btn)
        outer.addLayout(brow2)

    # ------------------------------------------------------------ folders

    def add_folder(self):
        d = QFileDialog.getExistingDirectory(self, "Folder to scan",
                                             os.path.expanduser("~"))
        if d and not any(self.folders.item(i).text() == d
                         for i in range(self.folders.count())):
            self.folders.addItem(d)

    def rem_folder(self):
        r = self.folders.currentRow()
        if r >= 0:
            self.folders.takeItem(r)

    def pick_trash(self):
        d = QFileDialog.getExistingDirectory(self, "Where duplicates go",
                                             os.path.expanduser("~"))
        if d:
            self.trash.setText(d)

    # ------------------------------------------------------------ scanning

    def start_scan(self):
        if self.scanner and self.scanner.isRunning():
            self.scanner.stop()
            return
        folders = [self.folders.item(i).text()
                   for i in range(self.folders.count())]
        if not folders:
            QMessageBox.warning(self, "Nothing to scan",
                                "Add at least one folder.")
            return

        self.groups, self.marked = [], set()
        self.glist.clear()
        self.clear_grid()
        self.ghead.setText("working...")
        self.update_total()
        self.bar.setRange(0, 0)
        self.bar.show()
        self.scan_btn.setText("STOP")
        for w in (self.move_btn, self.all_btn, self.none_btn):
            w.setEnabled(False)

        self.scanner = Scanner(
            folders, self.trash.text(), self.exact_only.isChecked(),
            self.threshold.value(), self.min_kb.value(),
            "oldest" if self.keep.currentText() == "oldest" else "best")
        self.scanner.progress.connect(self.on_progress)
        self.scanner.finished_ok.connect(self.on_done)
        self.scanner.failed.connect(self.on_failed)
        self.scanner.start()

    def on_progress(self, done, total, msg):
        if total:
            self.bar.setRange(0, total)
            self.bar.setValue(done)
            self.status.setText("%s  (%d/%d)" % (msg, done, total))
        else:
            self.bar.setRange(0, 0)
            self.status.setText(msg)

    def on_failed(self, msg):
        self.bar.hide()
        self.scan_btn.setText("SCAN")
        self.status.setText("failed - " + msg)

    def on_done(self, groups, n_images):
        self.bar.hide()
        self.scan_btn.setText("SCAN")
        self.groups = groups
        self.marked = {p for g in groups for p in g["files"]
                       if p != g["keep"]}

        self.glist.clear()
        for g in groups:
            dupes = [p for p in g["files"] if p != g["keep"]]
            waste = sum(os.path.getsize(p) for p in dupes
                        if os.path.exists(p))
            it = QListWidgetItem("%s   %d copies   %s\n%s" % (
                g["kind"], len(g["files"]), human(waste),
                os.path.basename(g["keep"])))
            self.glist.addItem(it)

        dup_n = sum(len(g["files"]) - 1 for g in groups)
        self.status.setText("%d images scanned, %d groups, %d duplicates"
                            % (n_images, len(groups), dup_n))
        on = bool(groups)
        for w in (self.all_btn, self.none_btn):
            w.setEnabled(on)
        self.update_total()
        if groups:
            self.glist.setCurrentRow(0)
        else:
            self.ghead.setText("nothing duplicated - you are clean")
            self.clear_grid()

    # ------------------------------------------------------------ display

    def clear_grid(self):
        while self.grid.count():
            it = self.grid.takeAt(0)
            w = it.widget()
            if w:
                w.deleteLater()

    def show_group(self, row):
        self.clear_grid()
        if row < 0 or row >= len(self.groups):
            return
        g = self.groups[row]
        self.ghead.setText("%s  ·  %d copies" % (g["kind"], len(g["files"])))
        per_row = max(1, (self.scroll.viewport().width() - 20) // (THUMB + 38))
        for i, p in enumerate(g["files"]):
            card = Card(p, p == g["keep"], self.make_keeper,
                        self.toggle, p in self.marked)
            self.grid.addWidget(card, i // per_row, i % per_row)

    def make_keeper(self, path):
        row = self.glist.currentRow()
        if row < 0:
            return
        g = self.groups[row]
        old = g["keep"]
        g["keep"] = path
        self.marked.discard(path)
        self.marked.add(old)
        self.show_group(row)
        self.update_total()

    def toggle(self, path, on):
        if on:
            self.marked.add(path)
        else:
            self.marked.discard(path)
        self.update_total()

    def tick_all(self):
        self.marked = {p for g in self.groups for p in g["files"]
                       if p != g["keep"]}
        self.show_group(self.glist.currentRow())
        self.update_total()

    def tick_none(self):
        self.marked = set()
        self.show_group(self.glist.currentRow())
        self.update_total()

    def update_total(self):
        live = [p for p in self.marked if os.path.exists(p)]
        size = sum(os.path.getsize(p) for p in live)
        self.move_btn.setText("MOVE %d FILES" % len(live))
        self.move_btn.setEnabled(bool(live))
        self.total.setText("%d ticked  ·  %s would be freed"
                           % (len(live), human(size)) if live else "")

    # ------------------------------------------------------------ moving

    def do_move(self):
        live = sorted(p for p in self.marked if os.path.exists(p))
        if not live:
            return
        dest_root = os.path.abspath(os.path.expanduser(self.trash.text()))
        if not dest_root or dest_root == "/":
            QMessageBox.warning(self, "No", "Pick a destination folder.")
            return

        ok = QMessageBox.question(
            self, "Move them?",
            "Move %d files into\n%s\n\nNothing is deleted. You can drag any "
            "of them back." % (len(live), dest_root),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        if ok != QMessageBox.StandardButton.Yes:
            return

        moved, failed, errs = 0, 0, []
        for p in live:
            rel = os.path.relpath(p, "/")
            dest = os.path.join(dest_root, rel)
            try:
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                stem, ext = os.path.splitext(dest)
                n = 1
                while os.path.exists(dest):
                    dest = "%s__%d%s" % (stem, n, ext)
                    n += 1
                shutil.move(p, dest)
                moved += 1
            except Exception as e:
                failed += 1
                if len(errs) < 5:
                    errs.append("%s - %s" % (os.path.basename(p), e))

        self.marked = {p for p in self.marked if os.path.exists(p)}
        for g in self.groups:
            g["files"] = [p for p in g["files"] if os.path.exists(p)]
        self.groups = [g for g in self.groups if len(g["files"]) > 1]

        self.on_done(self.groups, 0)
        self.status.setText("moved %d into %s%s" % (
            moved, dest_root, ", %d failed" % failed if failed else ""))
        msg = "Moved %d files into\n%s" % (moved, dest_root)
        if errs:
            msg += "\n\nFailed:\n" + "\n".join(errs)
        QMessageBox.information(self, "Done", msg)

    def closeEvent(self, e):
        if self.scanner and self.scanner.isRunning():
            self.scanner.stop()
            self.scanner.wait(3000)
        e.accept()


def main():
    app = QApplication(sys.argv)
    app.setStyleSheet(CSS)
    w = Window()
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
