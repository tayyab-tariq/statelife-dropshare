from PySide6.QtWidgets import QLabel, QMainWindow, QWidget


class MainWindow(QMainWindow):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("StateLife DropShare")
        self.setAccessibleName("DropShare")
        # A label's accessible name is its text, so screen readers read what is on screen.
        self.setCentralWidget(QLabel("StateLife DropShare"))
