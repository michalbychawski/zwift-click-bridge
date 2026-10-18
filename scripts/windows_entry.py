"""PyInstaller entry point -- points straight at the GUI's main(), bypassing the
generated console-script wrapper which PyInstaller's static analysis can miss."""
from zwift_click_bridge.app import main

if __name__ == "__main__":
    main()
