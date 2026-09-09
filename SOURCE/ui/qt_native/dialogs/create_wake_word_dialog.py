"""
Create Wake Word Dialog
========================

Dialog for creating custom wake words with:
- Wake word input with validation
- Tips for good wake words
- Training progress with stages
- Background training support
"""

from __future__ import annotations

from PySide6.QtCore import QTimer, Signal
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import (
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
)

from core.logging_config import get_logger

logger = get_logger(__name__)

# Training features are Phase 5+ experimental features
# Import conditionally to avoid hard dependency
try:
    from experimental.phase5_training.training_orchestrator import (
        TrainingProgress,
        TrainingStage,
        get_training_orchestrator,
        validate_wake_word,
    )

    TRAINING_AVAILABLE = True
except ImportError:
    TRAINING_AVAILABLE = False
    TrainingProgress = None  # type: ignore[assignment, misc]
    TrainingStage = None  # type: ignore[assignment, misc]
    get_training_orchestrator = None  # type: ignore[assignment, misc]
    validate_wake_word = None  # type: ignore[assignment, misc]
    logger.debug("Training features not available")


class CreateWakeWordDialog(QDialog):
    """
    Dialog for creating a custom wake word.

    Signals:
        wake_word_created(str): Emitted when a wake word is successfully created
    """

    wake_word_created = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._orchestrator = get_training_orchestrator()
        self._is_training = False
        self._setup_ui()

    def _setup_ui(self):
        """Set up the dialog UI."""
        self.setWindowTitle("Create Custom Wake Word")
        self.setMinimumWidth(500)
        self.setMinimumHeight(400)

        # Dark theme
        self.setStyleSheet("""
            QDialog {
                background: #1e1e1e;
                color: #e0e0e0;
            }
            QLabel {
                color: #e0e0e0;
            }
            QLineEdit {
                padding: 10px;
                border: 1px solid #3d3d3d;
                border-radius: 6px;
                background: #2d2d2d;
                color: #e0e0e0;
                font-size: 16px;
            }
            QLineEdit:focus {
                border-color: #5a8abc;
            }
            QPushButton {
                padding: 10px 20px;
                border-radius: 6px;
                font-size: 14px;
            }
            QProgressBar {
                border: 1px solid #3d3d3d;
                border-radius: 6px;
                background: #2d2d2d;
                text-align: center;
                color: #e0e0e0;
            }
            QProgressBar::chunk {
                background: #4a7c4a;
                border-radius: 5px;
            }
        """)

        layout = QVBoxLayout(self)
        layout.setSpacing(16)
        layout.setContentsMargins(24, 24, 24, 24)

        # Title
        title = QLabel("Create Custom Wake Word")
        title.setStyleSheet("font-size: 20px; font-weight: bold; color: #e0e0e0;")
        layout.addWidget(title)

        # Input section
        input_frame = QFrame()
        input_frame.setStyleSheet("""
            QFrame {
                background: #252525;
                border: 1px solid #3d3d3d;
                border-radius: 8px;
                padding: 16px;
            }
        """)
        input_layout = QVBoxLayout(input_frame)

        input_label = QLabel("What would you like to say to wake up the app?")
        input_label.setStyleSheet("font-size: 14px; color: #c0c0c0;")
        input_layout.addWidget(input_label)

        self._input = QLineEdit()
        self._input.setPlaceholderText("e.g., cheese, hey buddy, computer")
        self._input.textChanged.connect(self._on_input_changed)
        self._input.returnPressed.connect(self._on_create_clicked)
        input_layout.addWidget(self._input)

        # Validation message
        self._validation_label = QLabel("")
        self._validation_label.setStyleSheet("font-size: 12px;")
        self._validation_label.setWordWrap(True)
        input_layout.addWidget(self._validation_label)

        layout.addWidget(input_frame)

        # Tips section
        tips_frame = QFrame()
        tips_frame.setStyleSheet("""
            QFrame {
                background: #252530;
                border: 1px solid #3d3d4d;
                border-radius: 8px;
                padding: 12px;
            }
        """)
        tips_layout = QVBoxLayout(tips_frame)

        tips_title = QLabel("Tips for a good wake word:")
        tips_title.setStyleSheet("font-weight: bold; color: #a0a0c0;")
        tips_layout.addWidget(tips_title)

        tips = [
            "2-4 syllables work best (e.g., 'computer', 'hey buddy')",
            "Avoid common words you say often",
            "Unique sounds reduce false triggers",
            "Phrases like 'hey X' are very reliable",
        ]
        for tip in tips:
            tip_label = QLabel(f"  - {tip}")
            tip_label.setStyleSheet("color: #8080a0; font-size: 12px;")
            tips_layout.addWidget(tip_label)

        layout.addWidget(tips_frame)

        # Training time estimate
        self._time_label = QLabel("Training takes about 10-15 minutes")
        self._time_label.setStyleSheet("color: #888; font-size: 12px;")
        layout.addWidget(self._time_label)

        # Progress section (hidden initially)
        self._progress_frame = QFrame()
        self._progress_frame.setStyleSheet("""
            QFrame {
                background: #252525;
                border: 1px solid #3d3d3d;
                border-radius: 8px;
                padding: 16px;
            }
        """)
        progress_layout = QVBoxLayout(self._progress_frame)

        self._progress_title = QLabel("Training...")
        self._progress_title.setStyleSheet("font-size: 14px; font-weight: bold;")
        progress_layout.addWidget(self._progress_title)

        self._progress_bar = QProgressBar()
        self._progress_bar.setMinimum(0)
        self._progress_bar.setMaximum(100)
        self._progress_bar.setValue(0)
        progress_layout.addWidget(self._progress_bar)

        self._progress_message = QLabel("")
        self._progress_message.setStyleSheet("color: #888; font-size: 12px;")
        self._progress_message.setWordWrap(True)
        progress_layout.addWidget(self._progress_message)

        # Progress stages
        self._stages_label = QLabel("")
        self._stages_label.setStyleSheet("color: #666; font-size: 11px; font-family: monospace;")
        self._stages_label.setWordWrap(True)
        progress_layout.addWidget(self._stages_label)

        self._progress_frame.setVisible(False)
        layout.addWidget(self._progress_frame)

        layout.addStretch()

        # Buttons
        button_row = QHBoxLayout()

        self._cancel_btn = QPushButton("Cancel")
        self._cancel_btn.setStyleSheet("""
            QPushButton {
                background: transparent;
                border: 1px solid #4d4d4d;
                color: #a0a0a0;
            }
            QPushButton:hover {
                background: #3d3d3d;
            }
        """)
        self._cancel_btn.clicked.connect(self._on_cancel_clicked)
        button_row.addWidget(self._cancel_btn)

        button_row.addStretch()

        self._create_btn = QPushButton("Create")
        self._create_btn.setStyleSheet("""
            QPushButton {
                background: #4a7c4a;
                border: none;
                color: white;
            }
            QPushButton:hover {
                background: #5a9c5a;
            }
            QPushButton:disabled {
                background: #3a4a3a;
                color: #888;
            }
        """)
        self._create_btn.setEnabled(False)
        self._create_btn.clicked.connect(self._on_create_clicked)
        button_row.addWidget(self._create_btn)

        layout.addLayout(button_row)

    def _on_input_changed(self, text: str):
        """Handle input text changes."""
        if not text.strip():
            self._validation_label.setText("")
            self._create_btn.setEnabled(False)
            return

        # Validate
        result = validate_wake_word(text)

        if result.is_valid:
            if result.suggestions:
                self._validation_label.setText(", ".join(result.suggestions))
                self._validation_label.setStyleSheet("color: #c0a040; font-size: 12px;")
            else:
                self._validation_label.setText("Looks good!")
                self._validation_label.setStyleSheet("color: #40a040; font-size: 12px;")
            self._create_btn.setEnabled(True)
        else:
            self._validation_label.setText(result.error_message)
            self._validation_label.setStyleSheet("color: #c04040; font-size: 12px;")
            self._create_btn.setEnabled(False)

    def _on_create_clicked(self):
        """Handle create button click."""
        wake_word = self._input.text().strip()

        if not wake_word:
            return

        # Validate again
        result = validate_wake_word(wake_word)
        if not result.is_valid:
            QMessageBox.warning(self, "Invalid Wake Word", result.error_message)
            return

        # Start training
        self._start_training(wake_word)

    def _start_training(self, wake_word: str):
        """Start the training process."""
        self._is_training = True

        # Update UI
        self._input.setEnabled(False)
        self._create_btn.setVisible(False)
        self._progress_frame.setVisible(True)
        self._progress_title.setText(f"Creating '{wake_word}'...")
        self._cancel_btn.setText("Cancel Training")

        # Start training in background (no callback - we poll instead for thread safety)
        self._orchestrator.start_training_background(wake_word)

        # Poll for progress updates (thread-safe)
        self._poll_timer = QTimer(self)
        self._poll_timer.timeout.connect(self._poll_training_progress)
        self._poll_timer.start(200)  # Poll every 200ms for responsive UI

    def _poll_training_progress(self):
        """Poll for training progress updates (thread-safe)."""
        progress = self._orchestrator.get_progress()

        # Update progress bar
        self._progress_bar.setValue(int(progress.overall_progress * 100))

        # Update message
        self._progress_message.setText(progress.message)

        # Update stages
        stages_text = []
        stage_order = [
            (TrainingStage.VALIDATING, "Validating"),
            (TrainingStage.GENERATING_SAMPLES, "Generating samples"),
            (TrainingStage.TRAINING_MODEL, "Training model"),
            (TrainingStage.REGISTERING, "Registering"),
        ]

        for stage, name in stage_order:
            if progress.stage > stage:
                stages_text.append(f"[OK] {name}")
            elif progress.stage == stage:
                stages_text.append(f"[..] {name}")
            else:
                stages_text.append(f"[  ] {name}")

        self._stages_label.setText("\n".join(stages_text))

        # Check for completion states
        if progress.stage == TrainingStage.COMPLETE:
            self._poll_timer.stop()
            self._on_training_complete(progress)
        elif progress.stage == TrainingStage.ERROR:
            self._poll_timer.stop()
            self._on_training_error(progress)
        elif progress.stage == TrainingStage.CANCELLED:
            self._poll_timer.stop()
            self._on_training_cancelled()

    def _on_training_complete(self, progress: TrainingProgress):
        """Handle training completion."""
        self._is_training = False

        QMessageBox.information(
            self,
            "Success!",
            f"'{progress.wake_word}' is now available as a wake word!\n\n"
            f"Accuracy: {progress.accuracy:.1%}\n\n"
            "Select it in Settings when you are ready to use it.",
        )

        self.wake_word_created.emit(progress.wake_word)
        self.accept()

    def _on_training_error(self, progress: TrainingProgress):
        """Handle training error."""
        self._is_training = False

        QMessageBox.critical(
            self,
            "Training Failed",
            f"Failed to create wake word:\n\n{progress.error_message}",
        )

        # Reset UI
        self._input.setEnabled(True)
        self._create_btn.setVisible(True)
        self._progress_frame.setVisible(False)
        self._cancel_btn.setText("Cancel")

    def _on_training_cancelled(self):
        """Handle training cancellation."""
        self._is_training = False

        # Reset UI
        self._input.setEnabled(True)
        self._create_btn.setVisible(True)
        self._progress_frame.setVisible(False)
        self._cancel_btn.setText("Cancel")

    def _on_cancel_clicked(self):
        """Handle cancel button click."""
        if self._is_training:
            reply = QMessageBox.question(
                self,
                "Cancel Training?",
                "Training is in progress. Are you sure you want to cancel?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )

            if reply == QMessageBox.StandardButton.Yes:
                self._orchestrator.request_cancel()
        else:
            self.reject()

    def closeEvent(self, a0: QCloseEvent | None) -> None:
        """Handle dialog close."""
        if a0 is None:
            return
        if self._is_training:
            a0.ignore()
            self._on_cancel_clicked()
        else:
            a0.accept()
