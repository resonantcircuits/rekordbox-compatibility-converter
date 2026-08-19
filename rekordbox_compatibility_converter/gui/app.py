"""Entrypoint for Rekordbox Format Checker GUI."""

def main():
    try:
        import customtkinter
        from .modern_app import main as modern_main
        modern_main()
    except Exception:
        # Fallback to standard ttk app
        import tkinter as tk
        from .modern_app import ModernRekordboxGUI
        app = ModernRekordboxGUI()
        app.mainloop()


if __name__ == "__main__":
    main()
