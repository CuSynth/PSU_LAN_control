from __future__ import annotations

import argparse
import logging
import os
import sys

from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication


def apply_dark(app: QApplication) -> None:
    app.setStyle("Fusion")
    p = QPalette()
    c = QColor
    for role, col in ((QPalette.Window, "#1b1d22"), (QPalette.WindowText, "#e6e8ec"),
                      (QPalette.Base, "#14161a"), (QPalette.AlternateBase, "#1b1d22"),
                      (QPalette.Text, "#e6e8ec"), (QPalette.Button, "#2b2e35"),
                      (QPalette.ButtonText, "#e6e8ec"), (QPalette.ToolTipBase, "#2b2e35"),
                      (QPalette.ToolTipText, "#e6e8ec"), (QPalette.Highlight, "#3d7be0"),
                      (QPalette.HighlightedText, "#ffffff"), (QPalette.PlaceholderText, "#7b808a")):
        p.setColor(role, c(col))
    p.setColor(QPalette.Disabled, QPalette.ButtonText, c("#6b7079"))
    p.setColor(QPalette.Disabled, QPalette.Text, c("#6b7079"))
    app.setPalette(p)


def main(argv=None) -> int:
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    ap = argparse.ArgumentParser(description="Control app for Rigol DP832 power supplies")
    ap.add_argument("--config", default=os.path.join(root, "dp832_config.json"),
                    help="JSON config file (default: next to main.py)")
    ap.add_argument("--debug", action="store_true", help="log every SCPI command to the console")
    args = ap.parse_args(argv)

    from .bridge import setup_logging
    from .config import Config
    from .main_window import MainWindow

    app = QApplication(sys.argv[:1])
    apply_dark(app)
    import pyqtgraph as pg
    pg.setConfigOptions(antialias=True, foreground="#aeb3bc")
    cfg = Config(args.config)
    handler = setup_logging(cfg.log_dir, logging.DEBUG if args.debug else logging.INFO)
    win = MainWindow(cfg, handler)
    win.show()
    return app.exec()
